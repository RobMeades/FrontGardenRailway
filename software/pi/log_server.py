#!/usr/bin/env python3

# Copyright 2026 Rob Meades
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Written by Google Gemini :-)

"""
Log Server for FGR Nodes

This script listens for FGR protocol log messages from nodes, writes
them to the Linux systemd journal, and streams them into a unified
SQLite database on an external SSD.
"""

import sys
import socket
import argparse
import signal
import time
import threading
import sqlite3
import json
import random
import queue
import re
from pathlib import Path
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone

# Add the protocol directory to Python path
script_dir = Path(__file__).resolve().parent
protocol_dir = script_dir.parent / 'protocol'
sys.path.insert(0, str(protocol_dir))

# Import the generated FGR protocol module
try:
    from fgr_protocol import (
        FGRMsg, FGRMsgType, FGRLogLevel, receive_message, send_message
    )
except ImportError as e:
    print(f"Error: Cannot import fgr_protocol module: {e}")
    sys.exit(1)

# Try to import systemd journal support
try:
    from systemd import journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("Warning: python-systemd not installed, falling back to console output")

# Map FGR log levels to systemd priorities
LOG_LEVEL_TO_PRIORITY = {
    FGRLogLevel.FGR_LOG_LEVEL_DEBUG: journal.LOG_DEBUG if HAS_SYSTEMD else 7,
    FGRLogLevel.FGR_LOG_LEVEL_INFO: journal.LOG_INFO if HAS_SYSTEMD else 6,
    FGRLogLevel.FGR_LOG_LEVEL_WARN: journal.LOG_WARNING if HAS_SYSTEMD else 4,
    FGRLogLevel.FGR_LOG_LEVEL_ERROR: journal.LOG_ERR if HAS_SYSTEMD else 3,
}

DEFAULT_PRIORITY = journal.LOG_INFO if HAS_SYSTEMD else 6


class FGRLogServer:
    """FGR Protocol Log Server"""

    def __init__(self, bind_address: str = '0.0.0.0', port: int = 5001,
                 db_path: str = None, retention_days: int = 30):
        self.bind_address = bind_address
        self.port = port
        self.db_path = db_path
        self.retention_days = retention_days
        self.server_socket: Optional[socket.socket] = None
        self.client_threads: Dict[socket.socket, threading.Thread] = {}
        self.running = True

        self.stats = {
            'connections': 0,
            'log_messages': 0,
            'errors': 0,
            'db_writes': 0
        }
        self.lock = threading.Lock()

        # Async Queue pipeline for batch DB writing to protect Pi Zero I/O
        self.db_queue = queue.Queue(maxsize=10000)
        self.stop_db_worker = threading.Event()
        self.stop_cleanup = threading.Event()

        if self.db_path:
            self._init_database()
            self._start_db_worker()
            self._start_cleanup_thread()
        else:
            print("No database path provided - running in log-only mode")

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum: int, frame) -> None:
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False

    def _get_level_name(self, level: int) -> str:
        level_names = {
            FGRLogLevel.FGR_LOG_LEVEL_DEBUG: 'DEBUG',
            FGRLogLevel.FGR_LOG_LEVEL_INFO: 'INFO',
            FGRLogLevel.FGR_LOG_LEVEL_WARN: 'WARN',
            FGRLogLevel.FGR_LOG_LEVEL_ERROR: 'ERROR',
        }
        return level_names.get(level, f'LEVEL_{level}')

    def _write_to_journal(self, message: str, level: int, device_info: Dict[str, str]) -> None:
        """Write a log message to the systemd journal"""
        try:
            priority = LOG_LEVEL_TO_PRIORITY.get(level, DEFAULT_PRIORITY)
            ip = device_info.get('addr', 'unknown')
            enhanced_message = f"[{ip}] {message}"

            if HAS_SYSTEMD:
                extra_fields = {
                    'SYSLOG_IDENTIFIER': 'fgr-log-server',
                    'PRIORITY': str(priority),
                    'FGR_DEVICE_ADDR': ip,
                    'FGR_DEVICE_PORT': str(device_info.get('port', 'unknown')),
                    'FGR_LOG_LEVEL': str(level),
                    'FGR_LOG_LEVEL_NAME': self._get_level_name(level),
                    'SOURCE_IP': ip,
                }
                journal.send(enhanced_message, priority=priority, **extra_fields)
            else:
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                print(f"[{timestamp}] [{ip}:{device_info['port']}] [{self._get_level_name(level)}] {message}")
        except Exception as e:
            print(f"Journal write failed: {e}")

    def _init_database(self):
        """Initialize unified data lake schema"""
        if not self.db_path:
            return

        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(self.db_path, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS device_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_ip TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    epoch_time INTEGER NOT NULL,
                    log_level INTEGER NOT NULL,
                    log_tag TEXT,
                    message_type TEXT NOT NULL, -- 'LOG' or 'METRIC'
                    message TEXT NOT NULL,
                    extracted_json TEXT
                )
            ''')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_node_ip ON device_logs(node_ip)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_epoch ON device_logs(epoch_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_logs_tag ON device_logs(log_tag)")
            conn.commit()
            print(f"Unified database initialized at {self.db_path}")
        finally:
            conn.close()

    def _parse_esp32_log(self, log_text: str) -> Tuple[Optional[str], str]:
        """
        Parses standard ESP32 logs like: "W (9934) BACKTRACE: message"
        Returns: (extracted_tag_or_None, dynamic_cleaned_body)
        """
        match = re.match(r'^[EIVWD]\s+\(\d+\)\s+([^:]+):\s*(.*)$', log_text.strip())
        if match:
            tag, body = match.groups()
            return tag.strip(), body.strip()
        return None, log_text

    def _extract_json(self, text: str) -> Optional[str]:
        """Validates and extracts raw JSON text if present"""
        start = text.find('{')
        if start == -1:
            return None

        brace_count = 0
        end = -1
        for i in range(start, len(text)):
            if text[i] == '{':
                brace_count += 1
            elif text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end = i
                    break

        if end == -1:
            return None

        candidate = text[start:end+1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            return None

    def _queue_log_for_storage(self, node_ip: str, log_level: int, log_text: str) -> None:
        """Route and push logs into the async DB engine queue"""
        if not self.db_path:
            return

        # Extract ESP32 structural metadata
        log_tag, body = self._parse_esp32_log(log_text)

        # Determine payload profile type
        json_payload = self._extract_json(body)
        message_type = 'METRIC' if json_payload else 'LOG'

        log_row = (
            node_ip,
            datetime.now(timezone.utc).isoformat(),
            int(time.time()),
            log_level,
            log_tag,
            message_type,
            log_text,
            json_payload
        )

        try:
            self.db_queue.put_nowait(log_row)
        except queue.Full:
            with self.lock:
                self.stats['errors'] += 1
            # Fail silently to keep server network processing hot if disk is stalling

    def _start_db_worker(self):
        """Launches background SQLite engine processing thread"""
        def worker_loop():
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            while not self.stop_db_worker.is_set() or not self.db_queue.empty():
                batch = []
                # Drain queue up to batch size 100 to maximize WAL write-throughput
                while len(batch) < 100:
                    try:
                        # Short timeout allows checking stop events cleanly
                        row = self.db_queue.get(timeout=0.2)
                        batch.append(row)
                    except queue.Empty:
                        break

                if batch:
                    try:
                        conn.executemany('''
                            INSERT INTO device_logs (
                                node_ip, timestamp_utc, epoch_time, log_level,
                                log_tag, message_type, message, extracted_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', batch)
                        conn.commit()

                        with self.lock:
                            self.stats['db_writes'] += len(batch)
                    except Exception as e:
                        print(f"Database batch write crash: {e}")
                    finally:
                        for _ in batch:
                            self.db_queue.task_done()
            conn.close()

        threading.Thread(target=worker_loop, daemon=True).start()

    def _start_cleanup_thread(self):
        self.stop_cleanup = threading.Event()
        def cleanup_loop():
            self.stop_cleanup.wait(60)
            while not self.stop_cleanup.is_set():
                if self.db_path and self.retention_days > 0:
                    cutoff_time = int(time.time()) - (self.retention_days * 86400)
                    conn = sqlite3.connect(self.db_path, timeout=10.0)
                    try:
                        cursor = conn.execute("DELETE FROM device_logs WHERE epoch_time < ?", (cutoff_time,))
                        if cursor.rowcount > 0:
                            conn.commit()
                            print(f"Cleaned up {cursor.rowcount} old log items.")
                            if cursor.rowcount > 20000:
                                conn.execute("VACUUM")
                    except Exception as e:
                        print(f"Error executing database rotation: {e}")
                    finally:
                        conn.close()
                self.stop_cleanup.wait(86400)

        threading.Thread(target=cleanup_loop, daemon=True).start()

    def _handle_client(self, client_socket: socket.socket, client_address: tuple) -> None:
        device_info = {'addr': client_address[0], 'port': client_address[1]}
        client_socket.settimeout(1.0)
        print(f"New connection from {client_address[0]}:{client_address[1]}")

        try:
            while self.running:
                try:
                    msg = receive_message(client_socket, timeout=1.0)
                    if msg is None:
                        # Dynamic network checking to bypass hidden dead sockets
                        try:
                            client_socket.setblocking(False)
                            if not client_socket.recv(1, socket.MSG_PEEK):
                                break
                        except (BlockingIOError, socket.timeout):
                            pass
                        except OSError:
                            break
                        finally:
                            client_socket.setblocking(True)
                        continue

                    log_text = msg.get_log_message()
                    log_level = msg.reference

                    with self.lock:
                        self.stats['log_messages'] += 1

                    self._write_to_journal(log_text, log_level, device_info)
                    self._queue_log_for_storage(device_info['addr'], log_level, log_text)

                except Exception as e:
                    with self.lock:
                        self.stats['errors'] += 1
                    print(f"Client handling exception [{client_address[0]}]: {e}")
                    continue
        finally:
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except: pass
            try:
                client_socket.close()
            except: pass
            with self.lock:
                if client_socket in self.client_threads:
                    del self.client_threads[client_socket]
            print(f"Connection closed from {client_address[0]}:{client_address[1]}")

    def start(self) -> None:
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((self.bind_address, self.port))
            self.server_socket.listen(5)
            self.server_socket.settimeout(1.0)

            print(f"FGR Log Server listening on {self.bind_address}:{self.port}")
            print(f"Data Pipeline: Async batch logging enabled to optimize Pi Zero SSD performance")
            print("Press Ctrl+C to stop\n")

            last_accept_per_ip = {}
            last_global_accept = 0.0

            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()
                    ip = client_address[0]
                    current_time = time.time()

                    # Global rate limit checking
                    if current_time - last_global_accept < 0.5 and last_global_accept > 0:
                        time.sleep(0.5 - (current_time - last_global_accept))
                        current_time = time.time()

                    # Per-IP connection staggering checking
                    last_time = last_accept_per_ip.get(ip, 0)
                    if last_time > 0 and current_time - last_time < 1.0:
                        time.sleep(1.0 - (current_time - last_time))
                        current_time = time.time()

                    if ip not in last_accept_per_ip:
                        time.sleep(random.uniform(0, 0.5))
                        current_time = time.time()

                    last_global_accept = current_time
                    last_accept_per_ip[ip] = current_time

                    with self.lock:
                        self.stats['connections'] += 1
                        client_thread = threading.Thread(
                            target=self._handle_client,
                            args=(client_socket, client_address),
                            daemon=True
                        )
                        self.client_threads[client_socket] = client_thread
                        client_thread.start()

                except socket.timeout:
                    continue
                except OSError as e:
                    if self.running:
                        print(f"Socket error: {e}")
                    break
        finally:
            self.stop()

    def stop(self) -> None:
        print("\nStopping ingestion server pipeline...")
        self.running = False
        self.stop_cleanup.set()
        self.stop_db_worker.set()

        if self.server_socket:
            try:
                self.server_socket.close()
            except: pass

        print("\n=== Final Pipeline Server Statistics ===")
        print(f"Total Client Connections  : {self.stats['connections']}")
        print(f"Log Messages Ingested     : {self.stats['log_messages']}")
        print(f"Database Rows Written     : {self.stats['db_writes']}")
        print(f"System Operational Errors : {self.stats['errors']}")
        print("========================================")
        print("Server shutdown complete.")


def main():
    parser = argparse.ArgumentParser(description='FGR Protocol Unified Log Server')
    parser.add_argument('--port', '-p', type=int, default=5001, help='Port to listen on (default: 5001)')
    parser.add_argument('--bind-address', '-b', type=str, default='0.0.0.0', help='Address to bind to')
    parser.add_argument('--db-path', '-d', type=str, default=None, help='Path to unified SQLite file on SSD')
    parser.add_argument('--retention-days', '-r', type=int, default=30, help='Log retention days (0 = unlimited)')
    args = parser.parse_args()

    if args.port < 1 or args.port > 65535:
        print(f"Error: Invalid port {args.port}")
        sys.exit(1)

    server = FGRLogServer(
        bind_address=args.bind_address,
        port=args.port,
        db_path=args.db_path,
        retention_days=args.retention_days
    )

    try:
        server.start()
    except KeyboardInterrupt:
        server.stop()
    except Exception as e:
        print(f"Fatal server failure: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()