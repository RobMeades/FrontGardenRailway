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

"""
FGR Log Server - Receives logs from nodes via FGR protocol.

This script listens for FGR protocol log messages from nodes, writes
them to both SQLite database and systemd journal using LibLogger,
and provides crash dump capture and web serving functionality.
"""

import sys
import os
import socket
import argparse
import signal
import time
import threading
import base64
import json
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

# Import LibLogger
from LibLogger import LibLogger


class FGRLogServer:
    """FGR Protocol Log Server with crash dump capture"""

    def __init__(self, bind_address: str = '0.0.0.0', web_bind: str = '10.10.2.10',
                 port: int = 5001, web_port: int = 8060, db_path: str = None,
                 retention_days: int = 30, node_cfg_path: str = None, staging_path: str = '.'):
        self.bind_address = bind_address
        self.web_bind = web_bind
        self.port = port
        self.web_port = web_port
        self.db_path = Path(db_path) if db_path else None
        self.retention_days = retention_days
        self.staging_path = staging_path

        # Load configuration map if available
        self.node_cfg_path = node_cfg_path or str(script_dir / "nodes_esp32_deploy.json")
        self.node_cfg = {}
        self._load_node_config()

        # Initialize LibLogger
        self.logger = LibLogger()
        self.logger.init(self.db_path)

        # Server state
        self.server_socket: Optional[socket.socket] = None
        self.client_threads: Dict[socket.socket, threading.Thread] = {}
        self.active_crashes: Dict[str, dict] = {}
        self.running = True

        # Statistics
        self.stats = {
            'connections': 0,
            'log_messages': 0,
            'errors': 0,
            'crashes': 0
        }
        self.lock = threading.Lock()

        # Initialize crash dump tables
        self._init_crash_tables()

        # Start crash dump cleanup thread
        self._start_cleanup_thread()

        # Launch the web server for crash dumps
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

    def _load_node_config(self):
        """Load the inventory deployment layout mapping definitions."""
        try:
            if os.path.exists(self.node_cfg_path):
                with open(self.node_cfg_path, "r") as f:
                    self.node_cfg = json.load(f)
                    print(f"[LogServer] Loaded node config from {self.node_cfg_path}")
        except Exception as e:
            print(f"[LogServer] Warning: Failed to load node mappings: {e}")

    def _init_crash_tables(self):
        """Create crash_dumps table if it doesn't exist (database mode only)."""
        if not self.logger.is_db_available():
            print("[LogServer] Database not available, crash dumps will not be saved")
            return

        self.logger.execute_sql("""
            CREATE TABLE IF NOT EXISTS crash_dumps (
                crash_id TEXT PRIMARY KEY,
                timestamp_utc TEXT NOT NULL,
                ip TEXT NOT NULL,
                fw_hash TEXT NOT NULL,
                core_blob BLOB NOT NULL
            )
        """)

        # Create index for performance
        self.logger.execute_sql("CREATE INDEX IF NOT EXISTS idx_crash_ip ON crash_dumps(ip)")
        self.logger.execute_sql("CREATE INDEX IF NOT EXISTS idx_crash_time ON crash_dumps(timestamp_utc)")

        print("[LogServer] Crash dump tables ready")

    def _start_cleanup_thread(self):
        """Start background thread to clean up old crash dumps."""
        def cleanup_loop():
            while self.running:
                time.sleep(86400)  # Once per day

                if self.logger.is_db_available() and self.retention_days > 0:
                    # Keep only last 1000 crash dumps regardless of age
                    # (crash dumps are important, but we don't need millions)
                    try:
                        self.logger.execute_sql("""
                            DELETE FROM crash_dumps
                            WHERE crash_id NOT IN (
                                SELECT crash_id FROM crash_dumps
                                ORDER BY timestamp_utc DESC
                                LIMIT 1000
                            )
                        """)
                        print("[LogServer] Cleaned up old crash dumps")
                    except Exception as e:
                        print(f"[LogServer] Cleanup error: {e}")

        cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        cleanup_thread.start()

    def _intercept_and_parse_crash(self, ip: str, line: str):
        """
        State machine parsing standard firmware crashes across shared connections.
        """
        # 1. Catch the unique firmware hash signature
        if "BACKTRACE:" in line:
            parts = line.split("BACKTRACE:")
            if len(parts) > 1:
                # Extracts raw SHA256 build footprint hash emitted right after crash boot
                fw_hash = parts[1].strip().split()[0]
                if len(fw_hash) == 64:  # Validate it is a standard sha256 sequence
                    self.active_crashes[ip] = {"hash": fw_hash, "lines": []}
                    print(f"[LogServer] Started capturing crash for {ip}, hash={fw_hash[:16]}...")

        # 2. Slice and pack the Base64 stream lines
        elif "CORE_DUMP:" in line and ip in self.active_crashes:
            if "START" in line:
                return
            elif "END" in line:
                crash_data = self.active_crashes.pop(ip, None)
                if crash_data and crash_data["lines"]:
                    # Format crash_id as: timestamp_node_ip (e.g., 1781301107_10.10.3.5)
                    crash_id = f"{int(time.time())}_{ip}"
                    raw_b64 = "".join(crash_data["lines"])  # Concatenate raw B64

                    try:
                        core_binary = base64.b64decode(raw_b64)  # Decode on-the-fly

                        # Insert into Database using LibLogger's SQL executor
                        if self.logger.is_db_available():
                            insert_sql = """
                                INSERT INTO crash_dumps (crash_id, timestamp_utc, ip, fw_hash, core_blob)
                                VALUES (?, ?, ?, ?, ?)
                            """
                            self.logger.execute_sql(insert_sql, (
                                crash_id,
                                datetime.now(timezone.utc).isoformat(),
                                ip,
                                crash_data["hash"],
                                core_binary
                            ))

                            with self.lock:
                                self.stats['crashes'] += 1


                            # Journal Link (The trigger for your PC)
                            # The URL protocol handler will pick this up!
                            # and crash_decoder.py, when triggered, will
                            # know what to do
                            link_msg = f"🛑 CRASH! Decode: http://127.0.0.1:8080//{crash_id}"
                            self.logger.admin_log(link_msg, log_level=3)  # ERROR level
                            print(f"[LogServer] Crash captured: {crash_id} -> {link_msg}")
                        else:
                            print(f"[LogServer] Database not available, crash {crash_id} not saved")

                    except Exception as e:
                        print(f"[LogServer] Failed to decode crash for {ip}: {e}")
                        with self.lock:
                            self.stats['errors'] += 1
            else:
                # Isolate clean base64 data string chunk discarding trailing console layout arrows
                clean_chunk = line.split("CORE_DUMP:")[1].strip().rstrip(">")
                self.active_crashes[ip]["lines"].append(clean_chunk)

    def _start_web_server(self):
        """Start minimal web server for crash data endpoints."""
        server_instance = self

        class CrashDataHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass  # Suppress web requests

            def do_GET(self):
                try:
                    path = self.path

                    # Handle /data/{crash_id} - returns raw binary core dump
                    if path.startswith('/data/'):
                        crash_id = path.split('/')[-1]
                        print(f"[LogServer] /data/ request for crash ID {crash_id}")

                        if not server_instance.logger.is_db_available():
                            self.send_error(503, "Database not available")
                            return

                        result = server_instance.logger.execute_sql(
                            "SELECT core_blob FROM crash_dumps WHERE crash_id = ?",
                            (crash_id,)
                        )

                        if result and len(result) > 0:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/octet-stream")
                            self.end_headers()
                            self.wfile.write(result[0]['core_blob'])
                            print(f"[LogServer] Returned {len(result[0]['core_blob'])} byte(s)")
                        else:
                            self.send_error(404, "Crash dump not found")
                        return

                    # Handle /meta/{crash_id} - returns JSON metadata
                    elif path.startswith('/meta/'):
                        crash_id = path.split('/')[-1]
                        print(f"[LogServer] /meta/ request for crash ID {crash_id}")

                        if not server_instance.logger.is_db_available():
                            self.send_error(503, "Database not available")
                            return

                        result = server_instance.logger.execute_sql(
                            "SELECT fw_hash FROM crash_dumps WHERE crash_id = ?",
                            (crash_id,)
                        )

                        if result and len(result) > 0:
                            self.send_response(200)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            response = json.dumps({"fw_hash": result[0]['fw_hash']})
                            self.wfile.write(response.encode('utf-8'))
                            print(f"[LogServer] Returned metadata for {crash_id}")
                        else:
                            self.send_error(404, "Crash dump not found")
                        return

                    # Anything else -> 404
                    else:
                        self.send_error(404, "Not found")

                except Exception as e:
                    server_instance.logger.admin_log(f"Web API error: {str(e)}", log_level=3)
                    self.send_response(500)
                    self.send_header("Content-Type", "text/plain")
                    self.end_headers()
                    self.wfile.write(str(e).encode('utf-8'))

        def web_worker():
            try:
                httpd = HTTPServer((self.web_bind, self.web_port), CrashDataHandler)
                print(f"[LogServer] Crash data endpoints:")
                print(f"[LogServer]   - Core dump: http://{self.web_bind}:{self.web_port}/data/{{crash_id}}")
                print(f"[LogServer]   - Metadata:  http://{self.web_bind}:{self.web_port}/meta/{{crash_id}}")

                self.logger.admin_log(f"Web server started on {self.web_bind}:{self.web_port}", log_level=6)

                while self.running:
                    httpd.handle_request()
            except Exception as e:
                self.logger.admin_log(f"Web server died: {str(e)}", log_level=3)
                print(f"[LogServer] Web server fatal failure: {e}")

        web_thread = threading.Thread(target=web_worker, daemon=True)
        web_thread.start()

    def _handle_client(self, client_socket: socket.socket, client_address: tuple) -> None:
        """Handle a single client connection."""
        ip = client_address[0]
        port = client_address[1]

        client_socket.settimeout(1.0)
        print(f"[LogServer] New connection from {ip}:{port}")

        try:
            while self.running:
                try:
                    msg = receive_message(client_socket, timeout=1.0)
                    if msg is None:
                        # Check if socket is still alive
                        try:
                            client_socket.setblocking(False)
                            if not client_socket.recv(1, socket.MSG_PEEK):
                                break
                        except (BlockingIOError, socket.timeout):
                            pass
                        except (OSError, ConnectionError):
                            break
                        finally:
                            client_socket.setblocking(True)
                        continue

                    log_text = msg.get_log_message()
                    log_level = msg.reference

                    with self.lock:
                        self.stats['log_messages'] += 1

                    # Check for crash dump data
                    self._intercept_and_parse_crash(ip, log_text)

                    # FIX: Determine message_type based on content (restoring old behavior)
                    message_type = 'LOG'
                    if 'metrics:' in log_text:
                        message_type = 'METRIC'

                    # Log to journal and database using LibLogger
                    self.logger.log(
                        source='NODE',
                        node_ip=ip,
                        message=log_text,
                        log_level=log_level,
                        log_tag=self._get_level_name(log_level),
                        message_type=message_type
                    )

                except socket.timeout:
                    continue
                except (ConnectionResetError, BrokenPipeError):
                    break
                except Exception as e:
                    with self.lock:
                        self.stats['errors'] += 1
                    print(f"[LogServer] Client handling exception [{ip}]: {e}")
                    continue

        finally:
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
            print(f"[LogServer] Connection closed from {ip}:{port}")

    def start(self) -> None:
        """Start the log server."""
        # Create server socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.server_socket.bind((self.bind_address, self.port))
            self.server_socket.listen(5)
            self.server_socket.settimeout(1.0)

            print(f"\n{'='*60}")
            print("FGR Log Server with Crash Capture")
            print(f"{'='*60}")
            print(f"Log server listening on: {self.bind_address}:{self.port}")
            print(f"Web interface: http://{self.web_bind}:{self.web_port}/")
            print(f"Database: {self.db_path if self.db_path else 'disabled'}")
            print(f"Crash URL format: http://{self.web_bind}:{self.web_port}/{{crash_id}}")
            print(f"{'='*60}")
            print("Press Ctrl+C to stop\n")

            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()

                    with self.lock:
                        self.stats['connections'] += 1
                        client_thread = threading.Thread(
                            target=self._handle_client,
                            args=(client_socket, client_address),
                            daemon=True,
                            name=f"Client-{client_address[0]}"
                        )
                        self.client_threads[client_socket] = client_thread
                        client_thread.start()

                except socket.timeout:
                    continue
                except OSError as e:
                    if self.running:
                        print(f"[LogServer] Socket error: {e}")
                    break

        finally:
            self.stop()

    def stop(self) -> None:
        """Stop the log server."""
        print("\n[LogServer] Shutting down...")
        self.running = False

        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass

        # Shutdown LibLogger
        self.logger.shutdown()

        print("\n=== Final Server Statistics ===")
        print(f"Total Client Connections  : {self.stats['connections']}")
        print(f"Log Messages Ingested     : {self.stats['log_messages']}")
        print(f"Crashes Captured          : {self.stats['crashes']}")
        print(f"Errors                    : {self.stats['errors']}")
        print("========================================")
        print("Server shutdown complete.")


def main():
    parser = argparse.ArgumentParser(
        description="FGR Protocol Log Server with Crash Capture",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--port', '-p', type=int, default=5001,
                        help='Port to listen on for log streams')
    parser.add_argument('--web-port', '-w', type=int, default=8060,
                        help='Port for crash web dashboard')
    parser.add_argument('--bind-address', '-b', type=str, default='0.0.0.0',
                        help='Address to bind log server to')
    parser.add_argument('--web-bind', type=str, default='10.10.2.10',
                        help='Explicit IP address for web interface')
    parser.add_argument('--db-path', '-d', type=str, default=None,
                        help='Path to SQLite database file')
    parser.add_argument('--retention-days', '-r', type=int, default=30,
                        help='Crash dump retention (0 = unlimited)')
    parser.add_argument('--node-cfg', default=None,
                        help='Path to nodes_esp32_deploy.json for ELF mapping')

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