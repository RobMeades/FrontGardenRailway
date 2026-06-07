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
import os
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
import base64
from pathlib import Path
from typing import Optional, Dict, Tuple
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

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
    def __init__(self, bind_address: str = '0.0.0.0', web_bind: str = '10.10.2.10', port: int = 5001,
                 web_port: int = 8060, db_path: str = None, retention_days: int = 30,
                 node_cfg_path: str = None, staging_path: str = '.'):
        self.bind_address = bind_address
        self.web_bind = web_bind
        self.port = port
        self.web_port = web_port
        self.db_path = db_path
        self.retention_days = retention_days
        self.staging_path = staging_path

        # Load configuration map if available, otherwise fallback gracefully
        self.node_cfg_path = node_cfg_path or str(script_dir / "nodes_esp32_deploy.json")
        self.node_cfg = {}
        self._load_node_config()

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
        self.db_queue = queue.Queue(maxsize=10000)
        self.stop_db_worker = threading.Event()
        self.stop_cleanup = threading.Event()

        if self.db_path:
            self._init_database()
            self._start_db_worker()
            self._start_cleanup_thread()
        else:
            print("No database path provided - running in log-only mode")

        # Launch the web daemon processing engine!
        self._start_web_server()

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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS crash_dumps (
                    crash_id TEXT PRIMARY KEY,
                    timestamp_utc TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    fw_hash TEXT NOT NULL,
                    core_blob BLOB NOT NULL
                )
            ''')
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
                if self.db_path:
                    conn = sqlite3.connect(self.db_path, timeout=10.0)
                    try:
                        if self.retention_days > 0:
                            cutoff_time = int(time.time()) - (self.retention_days * 86400)
                            cursor = conn.execute("DELETE FROM device_logs WHERE epoch_time < ?", (cutoff_time,))
                            if cursor.rowcount > 0:
                                print(f"Cleaned up {cursor.rowcount} old log items.")
                        conn.execute('''
                            DELETE FROM crash_dumps
                            WHERE crash_id NOT IN (
                                SELECT crash_id FROM crash_dumps
                                ORDER BY timestamp_utc DESC
                                LIMIT 1000
                            )
                        ''')
                        conn.commit()
                        # Check if maintenance is needed (using total changes or rowcount from last op)
                        if conn.total_changes > 0:
                            # Only VACUUM if we've actually deleted a significant amount
                            # to avoid excessive disk wear on the SSD
                            if conn.total_changes > 20000:
                                conn.execute("VACUUM")
                    except Exception as e:
                        print(f"Error executing database rotation: {e}")
                    finally:
                        conn.close()
                self.stop_cleanup.wait(86400)

        threading.Thread(target=cleanup_loop, daemon=True).start()

    def _load_node_config(self):
        """
        Loads the inventory deployment layout mapping definitions.
        Called on startup and dynamically inside web routines for instant reloads.
        """
        try:
            if os.path.exists(self.node_cfg_path):
                with open(self.node_cfg_path, "r") as f:
                    self.node_cfg = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load node deployment mappings from {self.node_cfg_path}: {e}")

    def _intercept_and_parse_crash(self, ip: str, line: str):
        """
        State machine parsing standard firmware crashes across shared connections safely.
        """
        # 1. Catch the unique firmware hash signature
        if "BACKTRACE:" in line:
            parts = line.split("BACKTRACE:")
            if len(parts) > 1:
                # Extracts raw SHA256 build footprint hash emitted right after crash boot
                fw_hash = parts[1].strip().split()[0]
                if len(fw_hash) == 64:  # Validate it is a standard sha256 sequence
                    self.active_crashes[ip] = {"hash": fw_hash, "lines": []}

        # 2. Slice and pack the Base64 stream lines
        elif "CORE_DUMP:" in line and ip in self.active_crashes:
            if "START" in line:
                return
            elif "END" in line:
                crash_data = self.active_crashes.pop(ip, None)
                if crash_data and crash_data["lines"]:
                    crash_id = f"{int(time.time())}_{ip}"
                    raw_b64 = "".join(crash_data["lines"]) # Concatenate raw B64
                    core_binary = base64.b64decode(raw_b64) # Decode on-the-fly

                    # Insert into Database
                    conn = sqlite3.connect(self.db_path)
                    conn.execute("INSERT INTO crash_dumps VALUES (?, ?, ?, ?, ?)",
                                (crash_id, datetime.now(timezone.utc).isoformat(), ip, crash_data["hash"], core_binary))
                    conn.commit()
                    conn.close()

                    # Journal Link (The trigger for your PC)
                    # The URL protocol handler will pick this up!
                    link_msg = f"🛑 CRASH! Decode: http://127.0.0.1:8080/{crash_id}"


                    self._write_to_journal(link_msg, FGRLogLevel.FGR_LOG_LEVEL_WARN, {"addr": ip})
            else:
                # Isolate clean base64 data string chunk discarding trailing console layout arrows
                clean_chunk = line.split("CORE_DUMP:")[1].strip().rstrip(">")
                self.active_crashes[ip]["lines"].append(clean_chunk)

    def _start_web_server(self):
        # Create a tiny internal scoping bridge to allow our HTTP handler context access
        server_instance = self

        class CrashDashboardHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass # Suppress standard web requests out of console to keep logging clean

            def do_GET(self):
                try:
                    parsed_url = urlparse(self.path)

                    if parsed_url.path == '/crash':
                        query_params = parse_qs(parsed_url.query)
                        crash_id = query_params.get('id', [None])[0]

                        if not crash_id:
                            self.send_error(400, "Bad Request: Missing crash identification 'id' parameter.")
                            return

                        # Reload config on the fly to get newest build tracks dynamically
                        server_instance._load_node_config()
                        response_text = server_instance.decode_crash_dump(crash_id)

                        self.send_response(200)
                        self.send_header("Content-Type", "text/html; charset=utf-8")
                        self.end_headers()
                        self.wfile.write(response_text.encode('utf-8'))
                    elif parsed_url.path.startswith('/data/'):
                        crash_id = parsed_url.path.split('/')[-1]
                        conn = sqlite3.connect(server_instance.db_path)
                        row = conn.execute("SELECT core_blob, fw_hash FROM crash_dumps WHERE crash_id=?", (crash_id,)).fetchone()
                        conn.close()

                        if row:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.end_headers()
                            self.wfile.write(row[0]) # The binary BLOB
                    elif parsed_url.path.startswith('/meta/'):
                        crash_id = parsed_url.path.split('/')[-1]
                        conn = sqlite3.connect(server_instance.db_path)
                        row = conn.execute("SELECT fw_hash FROM crash_dumps WHERE crash_id=?", (crash_id,)).fetchone()
                        conn.close()

                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"fw_hash": row[0]}).encode('utf-8'))
                    else:
                        self.send_error(404)
                except Exception as route_err:
                    server_instance._write_to_journal(
                        f"Dashboard Route Routing Error: {str(route_err)}", 3, {"addr": server_instance.web_bind}
                    )
                    self.send_response(500)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    err_html = f"<h3>Internal Route Engine Failure</h3><pre>{str(route_err)}</pre>"
                    self.wfile.write(err_html.encode('utf-8'))

        def web_worker():
            try:
                # Bind specifically to the web_bind interface instead of the general bind_address
                httpd = HTTPServer((self.web_bind, self.web_port), CrashDashboardHandler)
                print(f"Interactive Crash Web Dashboard serving at http://{self.web_bind}:{self.web_port}/")

                # Write an explicit startup notification directly to the systemd journal
                server_instance._write_to_journal(
                    f"Web Dashboard thread started successfully on {self.web_bind}:{self.web_port}",
                    7, {"addr": self.web_bind}
                )

                while self.running:
                    httpd.handle_request()
            except Exception as e:
                server_instance._write_to_journal(
                    f"CRITICAL: Web server thread died fatally: {str(e)}",
                    3, {"addr": self.web_bind}
                )
                print(f"Web server fatal failure: {e}")

        threading.Thread(target=web_worker, daemon=True).start()

    def _handle_client(self, client_socket: socket.socket, client_address: tuple) -> None:
        device_info = {'addr': client_address[0], 'port': client_address[1]}
        client_socket.settimeout(1.0)
        print(f"New connection from {client_address[0]}:{client_address[1]}")

        try:
            # Set the socket briefly to non-blocking to clear any desynced
            # buffered stream trash waiting on the wire before entering the parser loop.
            client_socket.setblocking(False)
            purged_bytes = 0
            try:
                while True:
                    trash = client_socket.recv(4096)
                    if not trash:
                        break
                    purged_bytes += len(trash)
            except (BlockingIOError, socket.timeout):
                # No data left to clear - stream is clean
                pass
            except OSError:
                return  # Connection dropped during flush pass
            finally:
                client_socket.setblocking(True)
                client_socket.settimeout(1.0)

            if purged_bytes > 0:
                print(f"Purged {purged_bytes} residual desynced stream bytes from {client_address[0]}")

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

                    self._intercept_and_parse_crash(device_info['addr'], log_text)
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

    parser = argparse.ArgumentParser(
        description="FGR Protocol Unified Log Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--port', '-p', type=int, default=5001, help='Port to listen on for log streams (default: 5001)')
    parser.add_argument('--web-port', '-w', type=int, default=8060, help='Port to serve the interactive web crash dashboard on (default: 8060)')
    parser.add_argument('--bind-address', '-b', type=str, default='0.0.0.0', help='Address to bind to')
    parser.add_argument('--web-bind', type=str, default='10.10.2.10', help='Explicit Ethernet IP address to bind the web interface to')
    parser.add_argument('--db-path', '-d', type=str, default=None, help='Path to unified SQLite file on SSD')
    parser.add_argument('--retention-days', '-r', type=int, default=30, help='Log retention days (0 = unlimited)')
    parser.add_argument('--staging', '-s', type=str, default=".", help='Path to the local staging and archive directory root')
    parser.add_argument('--node-cfg', default=None, help='Path to nodes_esp32_deploy.json to map crash-dumps to ELF symbols')

    args = parser.parse_args()

    if args.port < 1 or args.port > 65535:
        print(f"Error: Invalid port {args.port}")
        sys.exit(1)

    server = FGRLogServer(
        bind_address=args.bind_address,
        web_bind=args.web_bind,
        port=args.port,
        web_port=args.web_port,
        db_path=args.db_path,
        retention_days=args.retention_days,
        staging_path=args.staging,
        node_cfg_path=args.node_cfg
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