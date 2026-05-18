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

# All written by DeepSeek :-)

"""
Log Server for FGR ESP32 Devices

This script listens for FGR protocol log messages from ESP32 devices
and writes them to the Linux systemd journal.

The script handles:
- TCP socket connections from multiple devices
- Automatic detection and parsing of FGR log messages
- Writing logs to the system journal with appropriate priorities
- Graceful shutdown on SIGTERM/SIGINT
- Client reconnection handling

Usage:
    python3 fgr_log_server.py [--port PORT] [--bind-address ADDR]

Options:
    --port PORT             Port to listen on (default: 5001)
    --bind-address ADDR     Address to bind to (default: 0.0.0.0)
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
from pathlib import Path
from typing import Optional, Dict, Set
from datetime import datetime, timezone

# Add the protocol directory to Python path
# Get the directory where THIS script is located
script_dir = Path(__file__).resolve().parent

# Navigate up to the common directory where fgr_protocol.py lives
protocol_dir = script_dir.parent / 'protocol'
sys.path.insert(0, str(protocol_dir))

# Import the generated FGR protocol module
try:
    from fgr_protocol import (
        FGRMsg, FGRMsgType, FGRLogLevel, receive_message, send_message
    )
except ImportError as e:
    print(f"Error: Cannot import fgr_protocol module: {e}")
    print(f"Looking in: {protocol_dir}")
    print("Please ensure fgr_protocol.py is in the protocol directory")
    print("and that you've run the generator script first:")
    print("  python3 generate_fgr_protocol.py fgr_protocol.h protocol/fgr_protocol.py")
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
            'bytes_received': 0,
            'errors': 0,
            'metrics_stored': 0
        }
        self.lock = threading.Lock()
        self.stop_cleanup = threading.Event()

        # Initialize database only if path provided (creates tables, no persistent connection)
        if self.db_path:
            self._init_database()
            self._start_cleanup_thread()
        else:
            print("No database path provided - running in log-only mode")

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals"""
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False

    def _get_level_name(self, level: int) -> str:
        """Get human-readable log level name"""
        level_names = {
            FGRLogLevel.FGR_LOG_LEVEL_DEBUG: 'DEBUG',
            FGRLogLevel.FGR_LOG_LEVEL_INFO: 'INFO',
            FGRLogLevel.FGR_LOG_LEVEL_WARN: 'WARN',
            FGRLogLevel.FGR_LOG_LEVEL_ERROR: 'ERROR',
        }
        return level_names.get(level, f'LEVEL_{level}')

    def _get_priority_from_level(self, level: int) -> int:
        """Convert FGR log level to systemd priority"""
        return LOG_LEVEL_TO_PRIORITY.get(level, DEFAULT_PRIORITY)

    def _write_to_journal(self, message: str, level: int,
                        device_info: Dict[str, str]) -> None:
        """Write a log message to the systemd journal"""
        try:
            priority = self._get_priority_from_level(level)

            # Prepend the IP address to the message for easier reading
            ip = device_info.get('addr', 'unknown')
            enhanced_message = f"[{ip}] {message}"

            if HAS_SYSTEMD:
                # Send to systemd journal with metadata
                extra_fields = {
                    'SYSLOG_IDENTIFIER': 'fgr-log-server',
                    'PRIORITY': str(priority),
                    'FGR_DEVICE_ADDR': device_info.get('addr', 'unknown'),
                    'FGR_DEVICE_PORT': str(device_info.get('port', 'unknown')),
                    'FGR_LOG_LEVEL': str(level),
                    'FGR_LOG_LEVEL_NAME': self._get_level_name(level),
                    'SOURCE_IP': device_info.get('addr', 'unknown'),
                }

                journal.send(enhanced_message, priority=priority, **extra_fields)
            else:
                # Fallback to console output
                timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                level_name = self._get_level_name(level)
                print(f"[{timestamp}] [{device_info['addr']}:{device_info['port']}] "
                    f"[{level_name}] {message}")

        except Exception as e:
            print(f"Journal write failed for {device_info.get('addr', 'unknown')}: {e}")

    def _init_database(self):
        """Initialize database schema (called once at startup)"""
        if not self.db_path:
            return

        # Ensure directory exists
        db_dir = Path(self.db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        # Use a temporary connection to create tables
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_ip TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    epoch_time INTEGER NOT NULL,
                    data TEXT NOT NULL
                )
            ''')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_node_ip ON metrics(node_ip)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_metrics_epoch_time ON metrics(epoch_time)")
            conn.commit()
            print(f"Database initialized at {self.db_path}")
        finally:
            conn.close()

    def _extract_json_from_log(self, log_text: str) -> Optional[str]:
        """Extract JSON from a log message that may have prefixes"""
        if not log_text:
            return None

        # Find the first '{' character
        start = log_text.find('{')
        if start == -1:
            return None

        # Find the matching '}' at the end (simple approach - assumes valid JSON)
        # Count brackets to handle nested objects
        brace_count = 0
        end = -1
        for i in range(start, len(log_text)):
            if log_text[i] == '{':
                brace_count += 1
            elif log_text[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end = i
                    break

        if end == -1:
            return None

        return log_text[start:end+1]

    def _store_metrics(self, node_ip: str, log_text: str) -> None:
        """Store raw metrics JSON in database - creates a new connection per call"""
        if not self.db_path:
            return

        if not log_text.strip().startswith('{'):
            return

        try:
            # Validate JSON
            json.loads(log_text)

            # Create a fresh connection for this thread
            conn = sqlite3.connect(self.db_path, timeout=5.0)
            try:
                conn.execute('''
                    INSERT INTO metrics (node_ip, timestamp_utc, epoch_time, data)
                    VALUES (?, ?, ?, ?)
                ''', (
                    node_ip,
                    datetime.now(timezone.utc).isoformat(),
                    int(time.time()),
                    log_text
                ))
                conn.commit()

                with self.lock:
                    self.stats['metrics_stored'] += 1

                if self.stats['metrics_stored'] % 100 == 0:
                    print(f"Stored {self.stats['metrics_stored']} metrics so far")
            finally:
                conn.close()

        except json.JSONDecodeError:
            pass  # Not valid JSON, ignore silently
        except Exception as e:
            print(f"Error storing metrics for {node_ip}: {e}")

    def _start_cleanup_thread(self):
        """Start a background thread for database cleanup"""
        self.stop_cleanup = threading.Event()

        def cleanup_loop():
            # Run initial cleanup after 1 minute
            self.stop_cleanup.wait(60)
            while not self.stop_cleanup.is_set():
                self._cleanup_old_metrics()
                self.stop_cleanup.wait(86400)  # 24 hours, but can be interrupted

        cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        cleanup_thread.start()
        print("Database cleanup thread started (runs daily)")

    def _cleanup_old_metrics(self):
        """Delete metrics older than retention_days"""
        if not self.db_path or self.retention_days <= 0:
            return

        cutoff_time = int(time.time()) - (self.retention_days * 86400)

        # Create a fresh connection for cleanup
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        try:
            cursor = conn.execute(
                "DELETE FROM metrics WHERE epoch_time < ?",
                (cutoff_time,)
            )
            deleted = cursor.rowcount
            if deleted > 0:
                conn.commit()
                print(f"Deleted {deleted} old metrics (older than {self.retention_days} days)")

                # VACUUM occasionally to reclaim space
                if deleted > 10000:
                    conn.execute("VACUUM")
                    print("Database vacuumed to reclaim space")
        except Exception as e:
            print(f"Error cleaning up old metrics: {e}")
        finally:
            conn.close()

    def _handle_client(self, client_socket: socket.socket,
                    client_address: tuple) -> None:
        """Handle a connected client - runs in its own thread"""
        device_info = {
            'addr': client_address[0],
            'port': client_address[1]
        }

        client_socket.settimeout(1.0)
        print(f"New connection from {client_address[0]}:{client_address[1]}")

        try:
            while self.running:
                try:
                    msg = receive_message(client_socket, timeout=1.0)

                    if msg is None:
                        # No message received within timeout
                        # Do a quick check to see if socket is still alive
                        try:
                            client_socket.setblocking(False)
                            data = client_socket.recv(1, socket.MSG_PEEK)
                            client_socket.setblocking(True)
                            if not data:
                                # Socket closed by remote
                                print(f"Client {client_address[0]}:{client_address[1]} closed connection")
                                break
                        except (BlockingIOError, socket.timeout):
                            # No data available, but socket still alive
                            pass
                        except (ConnectionResetError, BrokenPipeError, OSError):
                            # Socket is dead
                            print(f"Client {client_address[0]}:{client_address[1]} connection lost")
                            break
                        finally:
                            client_socket.setblocking(True)
                        continue

                    # Process the message
                    with self.lock:
                        self.stats['log_messages'] += 1
                        log_text = msg.get_log_message()
                        log_level = msg.reference
                        self._write_to_journal(log_text, log_level, device_info)

                    # Extract JSON if present (outside lock to avoid blocking)
                    json_text = self._extract_json_from_log(log_text)
                    if json_text:
                        try:
                            json.loads(json_text)
                            self._store_metrics(device_info['addr'], json_text)
                        except json.JSONDecodeError:
                            pass  # Not valid JSON, ignore

                except Exception as e:
                    with self.lock:
                        self.stats['errors'] += 1
                    print(f"Error handling client {client_address[0]}:{client_address[1]}: {e}")
                    continue

        finally:
            # Properly close the socket to prevent CLOSE-WAIT
            try:
                client_socket.shutdown(socket.SHUT_RDWR)
            except:
                pass
            try:
                client_socket.close()
            except:
                pass

            with self.lock:
                if client_socket in self.client_threads:
                    del self.client_threads[client_socket]

            print(f"Connection closed from {client_address[0]}:{client_address[1]}")

    def start(self) -> None:
        """Start the log server with rate limiting to protect WiFi air interface"""
        # Create server socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((self.bind_address, self.port))
            self.server_socket.listen(5)
            self.server_socket.settimeout(1.0)  # Allow checking running flag

            print(f"FGR Log Server listening on {self.bind_address}:{self.port}")
            print(f"Systemd journal support: {'Enabled' if HAS_SYSTEMD else 'Disabled'}")
            print(f"Protocol module loaded from: {protocol_dir}")
            print("Global rate limiting enabled: min 0.5s between connections")
            print("Press Ctrl+C to stop")
            print()

            # Rate limiting variables
            last_accept_per_ip = {}  # Track last accept time per IP
            last_global_accept = 0.0  # Track last accept time globally
            min_global_interval = 0.5  # Minimum 500ms between ANY connections
            min_per_ip_interval = 1.0  # Minimum 1 second between reconnects from same IP

            # Main accept loop
            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()
                    ip = client_address[0]

                    current_time = time.time()

                    # FIRST: Global rate limiting - ensure minimum time between ANY connections
                    time_since_last_global = current_time - last_global_accept
                    if time_since_last_global < min_global_interval and last_global_accept > 0:
                        wait_time = min_global_interval - time_since_last_global
                        print(f"Global rate limit: waiting {wait_time:.2f}s before accepting connection from {ip}")
                        time.sleep(wait_time)
                        current_time = time.time()  # Update after sleep

                    # SECOND: Per-IP staggering for reconnections
                    last_time = last_accept_per_ip.get(ip, 0)
                    if last_time > 0 and current_time - last_time < min_per_ip_interval:
                        wait_time = min_per_ip_interval - (current_time - last_time)
                        print(f"Per-IP staggering for {ip}: waiting {wait_time:.2f}s")
                        time.sleep(wait_time)
                        current_time = time.time()  # Update after sleep

                    # THIRD: Add random jitter for first connections to desynchronize
                    if ip not in last_accept_per_ip:
                        random_delay = random.uniform(0, 0.5)
                        print(f"First connection from {ip}, adding {random_delay:.2f}s random jitter")
                        time.sleep(random_delay)
                        current_time = time.time()  # Update after sleep

                    # Update tracking
                    last_global_accept = current_time
                    last_accept_per_ip[ip] = current_time

                    with self.lock:
                        self.stats['connections'] += 1
                        # Start a new thread for each client
                        client_thread = threading.Thread(
                            target=self._handle_client,
                            args=(client_socket, client_address),
                            daemon=True
                        )
                        self.client_threads[client_socket] = client_thread
                        client_thread.start()

                except socket.timeout:
                    # Timeout occurred, just loop again to check running flag
                    continue
                except OSError as e:
                    if self.running:
                        print(f"Socket error: {e}")
                    break

        except Exception as e:
            print(f"Failed to start server: {e}")
            sys.exit(1)
        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the log server - daemon threads will exit automatically"""
        print("\nShutting down...")
        self.running = False

        # Signal cleanup thread to stop (so it doesn't wait 24h)
        if hasattr(self, 'stop_cleanup'):
            self.stop_cleanup.set()

        # Close server socket to unblock accept() in main loop
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass

        # Print statistics
        print("\n=== Server Statistics ===")
        print(f"Total connections: {self.stats['connections']}")
        print(f"Log messages received: {self.stats['log_messages']}")
        if self.db_path:
            print(f"Metrics stored: {self.stats['metrics_stored']}")
        print(f"Total bytes received: {self.stats['bytes_received']}")
        print(f"Errors: {self.stats['errors']}")
        print("=========================")
        print("Server stopped")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='FGR Protocol Log Server for ESP32 devices'
    )
    parser.add_argument(
        '--port', '-p',
        type=int,
        default=5001,
        help='Port to listen on (default: 5001)'
    )
    parser.add_argument(
        '--bind-address', '-b',
        type=str,
        default='0.0.0.0',
        help='Address to bind to (default: 0.0.0.0)'
    )
    parser.add_argument(
        '--db-path', '-d',
        type=str,
        default=None,
        help='Path to SQLite database file (e.g. /mnt/ssd/fgr_metrics.db), default no database, log only mode'
    )
    parser.add_argument(
        '--retention-days', '-r',
        type=int,
        default=30,
        help='Number of days to retain metrics (default: 30, 0 = unlimited)'
    )
    args = parser.parse_args()

    # Validate port range
    if args.port < 1 or args.port > 65535:
        print(f"Error: Invalid port number {args.port}")
        sys.exit(1)

    # Create and start server
    server = FGRLogServer(
        bind_address=args.bind_address,
        port=args.port,
        db_path=args.db_path,
        retention_days=args.retention_days
    )

    try:
        server.start()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
        server.stop()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()