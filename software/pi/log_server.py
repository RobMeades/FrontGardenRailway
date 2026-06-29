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

Clients connecting from 127.0.0.1 are identified as the controller,
all other clients are identified as nodes
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
from typing import Optional, Dict, Tuple, Any
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from dataclasses import dataclass, field
from enum import IntEnum

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


class ConnectionState(IntEnum):
    """Connection states for log server clients"""
    DISCONNECTED = 0
    CONNECTED = 1
    HANDSHAKING = 2
    READY = 3
    ERROR = 4


@dataclass
class LogClient:
    """Represents a connected client (node) to the log server"""
    ip: str
    port: int
    sock: Optional[socket.socket] = None
    state: ConnectionState = ConnectionState.DISCONNECTED
    rx_thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    connection_time: float = 0
    connection_id: int = 0
    message_count: int = 0
    last_activity: float = 0
    is_controller: bool = False  # <-- NEW: Flag to identify controller connections
    # Crash capture state
    crash_hash: Optional[str] = None
    crash_lines: list = field(default_factory=list)

    def identifier(self) -> str:
        """Return client identifier as 'ip:port'"""
        return f"{self.ip}:{self.port}"


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

        # Client tracking - map IP to client state
        self.clients: Dict[str, LogClient] = {}
        self.clients_lock = threading.Lock()

        # Load configuration map if available
        self.node_cfg_path = node_cfg_path or str(script_dir / "nodes_esp32_deploy.json")
        self.node_cfg = {}
        self._load_node_config()

        # Initialize LibLogger
        self.lib_logger = LibLogger()
        self.lib_logger.init(
            mode='server',
            db_path=self.db_path,
            retention_days=7,
            enable_trim=True
        )

        # Server state
        self.server_socket: Optional[socket.socket] = None
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

        self._start_heartbeat_thread()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _start_heartbeat_thread(self):
        """Start background thread that logs a heartbeat message every minute."""
        def heartbeat_loop():
            heartbeat_count = 0
            while self.running:
                time.sleep(60)  # Once per minute
                if not self.running:
                    break
                heartbeat_count += 1
                # Log to journal and database
                self.lib_logger.log(
                    source='SERVER',
                    node_ip='0.0.0.0',
                    message=f"Heartbeat #{heartbeat_count} - clients: {len(self.clients)}, "
                            f"messages: {self.stats['log_messages']}, errors: {self.stats['errors']}",
                    log_level=1,  # INFO level
                    log_tag='HEARTBEAT',
                    message_type='STATUS'
                )

        heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        print("[LogServer] Heartbeat thread started (60s interval)")

    def _signal_handler(self, signum: int, frame) -> None:
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False
        # Signal all clients to stop
        with self.clients_lock:
            for client in self.clients.values():
                client.stop_event.set()

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
        if not self.lib_logger.is_db_available():
            print("[LogServer] Database not available, crash dumps will not be saved")
            return

        self.lib_logger.execute_sql("""
            CREATE TABLE IF NOT EXISTS crash_dumps (
                crash_id TEXT PRIMARY KEY,
                timestamp_utc TEXT NOT NULL,
                ip TEXT NOT NULL,
                fw_hash TEXT NOT NULL,
                core_blob BLOB NOT NULL
            )
        """)

        # Create index for performance
        self.lib_logger.execute_sql("CREATE INDEX IF NOT EXISTS idx_crash_ip ON crash_dumps(ip)")
        self.lib_logger.execute_sql("CREATE INDEX IF NOT EXISTS idx_crash_time ON crash_dumps(timestamp_utc)")

        print("[LogServer] Crash dump tables ready")

    def _start_cleanup_thread(self):
        """Start background thread to clean up old crash dumps."""
        def cleanup_loop():
            while self.running:
                time.sleep(86400)  # Once per day

                if self.lib_logger.is_db_available() and self.retention_days > 0:
                    try:
                        self.lib_logger.execute_sql("""
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

    def _intercept_and_parse_crash(self, ip: str, line: str, client: LogClient):
        """
        State machine parsing standard firmware crashes across shared connections.
        Supports both Linux backtrace format (watchdog reset) and ESP32 core dump
        format (power-on reset).
        """
        # Check for ESP32 core dump start (power-on reset - no BACKTRACE)
        if "CORE DUMP START" in line and client.crash_hash is None:
            # Generate a hash from the IP and timestamp
            import hashlib
            hash_input = f"{ip}_{time.time()}_{client.message_count}"
            client.crash_hash = hashlib.sha256(hash_input.encode()).hexdigest()
            client.crash_lines = []
            print(f"[LogServer] Started capturing core dump for {ip}, hash={client.crash_hash[:16]}...")
            return

        # Catch the unique firmware hash signature (watchdog reset)
        if "BACKTRACE:" in line:
            parts = line.split("BACKTRACE:")
            if len(parts) > 1:
                fw_hash = parts[1].strip().split()[0]
                if len(fw_hash) == 64:
                    client.crash_hash = fw_hash
                    client.crash_lines = []
                    print(f"[LogServer] Started capturing crash for {ip}, hash={fw_hash[:16]}...")
                    return

        # Slice and pack the Base64 stream lines
        if "CORE_DUMP:" in line and client.crash_hash is not None:
            # Skip START and END markers
            if "START" in line:
                return
            elif "END" in line:
                crash_data = {
                    "hash": client.crash_hash,
                    "lines": client.crash_lines.copy()
                }
                client.crash_hash = None
                client.crash_lines = []

                if crash_data["lines"]:
                    crash_id = f"{int(time.time())}_{ip}"
                    raw_b64 = "".join(crash_data["lines"])

                    try:
                        core_binary = base64.b64decode(raw_b64)

                        if self.lib_logger.is_db_available():
                            insert_sql = """
                                INSERT INTO crash_dumps (crash_id, timestamp_utc, ip, fw_hash, core_blob)
                                VALUES (?, ?, ?, ?, ?)
                            """
                            self.lib_logger.execute_sql(insert_sql, (
                                crash_id,
                                datetime.now(timezone.utc).isoformat(),
                                ip,
                                crash_data["hash"],
                                core_binary
                            ))

                            with self.lock:
                                self.stats['crashes'] += 1

                            # Note to LLM assistants: this link *must* be to port 8080 -
                            # it is parsed by the crash_decoder.py script that will be
                            # running as a daemon on the [Linux] PC that has its web
                            # browser looking at the debug view of the web_controller.py
                            # output page (and has the ESP-IDF tools installed, hence
                            # is able to debug the core dump this script serves to it).
                            link_msg = f"🛑 CRASH! Decode: http://127.0.0.1:8080/{crash_id}"
                            self.lib_logger.log(
                                source='NODE',
                                node_ip=ip,
                                message=link_msg,
                                log_level=3
                            )
                            print(f"[LogServer] Crash captured: {crash_id} -> {link_msg}")
                        else:
                            print(f"[LogServer] Database not available, crash {crash_id} not saved")

                    except Exception as e:
                        print(f"[LogServer] Failed to decode crash for {ip}: {e}")
                        with self.lock:
                            self.stats['errors'] += 1
            else:
                # Extract the base64 data (remove the "CORE_DUMP:" prefix)
                clean_chunk = line.split("CORE_DUMP:")[1].strip()
                # Remove any trailing non-base64 characters (like log level indicators)
                import re
                clean_chunk = re.sub(r'[^A-Za-z0-9+/=]', '', clean_chunk)
                if clean_chunk:
                    client.crash_lines.append(clean_chunk)

    def _start_web_server(self):
        """Start minimal web server for crash data endpoints."""
        server_instance = self

        class CrashDataHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                try:
                    path = self.path

                    if path.startswith('/data/'):
                        crash_id = path.split('/')[-1]
                        print(f"[LogServer] /data/ request for crash ID {crash_id}")

                        if not server_instance.lib_logger.is_db_available():
                            self.send_error(503, "Database not available")
                            return

                        result = server_instance.lib_logger.execute_sql(
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

                    elif path.startswith('/meta/'):
                        crash_id = path.split('/')[-1]
                        print(f"[LogServer] /meta/ request for crash ID {crash_id}")

                        if not server_instance.lib_logger.is_db_available():
                            self.send_error(503, "Database not available")
                            return

                        result = server_instance.lib_logger.execute_sql(
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

                    else:
                        self.send_error(404, "Not found")

                except Exception as e:
                    server_instance.lib_logger.log_admin(f"Web API error: {str(e)}", log_level=3)
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

                self.lib_logger.log_admin(f"Web server started on {self.web_bind}:{self.web_port}", log_level=6)

                while self.running:
                    httpd.handle_request()
            except Exception as e:
                self.lib_logger.log_admin(f"Web server died: {str(e)}", log_level=3)
                print(f"[LogServer] Web server fatal failure: {e}")

        web_thread = threading.Thread(target=web_worker, daemon=True)
        web_thread.start()

    def _disconnect_client(self, client: LogClient) -> None:
        """Internal: disconnect a client"""
        if client.state == ConnectionState.DISCONNECTED:
            return

        ip = client.ip
        port = client.port
        current_sock = client.sock

        self.lib_logger.log_admin(
            f"Disconnecting {'CONTROLLER' if client.is_controller else 'NODE'} client {ip}:{port} "
            f"(msgs={client.message_count}, state={client.state})",
            log_level=6
        )

        client.stop_event.set()

        # Only join if it's not our own thread
        if client.rx_thread and client.rx_thread.is_alive():
            if client.rx_thread is not threading.current_thread():
                client.rx_thread.join(timeout=2.0)
                if client.rx_thread.is_alive():
                    self.lib_logger.log_admin(f"Receive thread for {ip}:{port} did not terminate after 2 seconds!", log_level=3)
            else:
                self.lib_logger.log_admin(f"Not joining own thread for {ip}:{port}", log_level=6)

        client.rx_thread = None
        client.sock = None

        if current_sock:
            try:
                current_sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                current_sock.close()
            except Exception:
                pass

        client.state = ConnectionState.DISCONNECTED
        self.lib_logger.log_admin(f"Client {ip}:{port} disconnected", log_level=6)

    def _handle_client(self, client_sock: socket.socket, client_address: tuple) -> None:
        """Handle a single client connection."""
        ip = client_address[0]
        port = client_address[1]
        client_sock.settimeout(1.0)

        with self.clients_lock:
            existing_client = self.clients.get(ip)

            if existing_client and existing_client.state != ConnectionState.DISCONNECTED:
                self.lib_logger.log_admin(
                    f"Reconnection from {ip}:{port} (old_port={existing_client.port})",
                    log_level=6
                )

                existing_client.stop_event.set()

                if existing_client.rx_thread and existing_client.rx_thread.is_alive():
                    existing_client.rx_thread.join(timeout=5.0)
                    if existing_client.rx_thread.is_alive():
                        old_sock = existing_client.sock
                        existing_client.sock = None
                        if old_sock:
                            try:
                                old_sock.shutdown(socket.SHUT_RDWR)
                            except Exception:
                                pass
                            try:
                                old_sock.close()
                            except Exception:
                                pass
                        existing_client.rx_thread.join(timeout=1.0)

                old_sock = existing_client.sock
                existing_client.sock = None
                if old_sock:
                    try:
                        old_sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        old_sock.close()
                    except Exception:
                        pass

                existing_client.state = ConnectionState.DISCONNECTED
                existing_client.crash_hash = None
                existing_client.crash_lines = []
                existing_client.message_count = 0
                existing_client.stop_event = threading.Event()

            if existing_client:
                client = existing_client
                client.port = port
                client.sock = client_sock
                client.connection_id += 1
                client.message_count = 0
                client.crash_hash = None
                client.crash_lines = []
                client.is_controller = (ip == '127.0.0.1')
                client.state = ConnectionState.CONNECTED
                client.last_activity = time.time()
            else:
                client = LogClient(ip=ip, port=port, sock=client_sock)
                client.is_controller = (ip == '127.0.0.1')
                client.state = ConnectionState.CONNECTED
                client.connection_time = time.time()
                client.last_activity = time.time()
                self.clients[ip] = client

            client.connection_time = time.time()
            client.rx_thread = threading.current_thread()

        if client.connection_id <= 1:
            self.lib_logger.log_admin(
                f"Client {ip}:{port} connected (connection #{client.connection_id}, "
                f"{'CONTROLLER' if client.is_controller else 'NODE'})",
                log_level=6
            )

        with self.lock:
            self.stats['connections'] += 1

        thread_client = client

        try:
            consecutive_errors = 0
            while self.running and not thread_client.stop_event.is_set():
                try:
                    msg = receive_message(client_sock, timeout=1.0)
                    if msg is None:
                        try:
                            client_sock.getpeername()
                        except Exception:
                            break
                        time.sleep(0.001)
                        continue

                    consecutive_errors = 0
                    client.message_count += 1
                    client.last_activity = time.time()

                    log_text = msg.get_log_message()
                    log_level = msg.reference

                    with self.lock:
                        self.stats['log_messages'] += 1

                    try:
                        self._intercept_and_parse_crash(ip, log_text, client)
                    except Exception as e:
                        self.lib_logger.log_admin(f"Crash parse error for {ip}: {e}", log_level=3)

                    message_type = 'METRIC' if 'metrics:' in log_text else 'LOG'
                    source = 'CTRL' if client.is_controller else 'NODE'

                    try:
                        self.lib_logger.log(
                            source=source,
                            node_ip=ip,
                            message=log_text,
                            log_level=log_level,
                            log_tag=self._get_level_name(log_level),
                            message_type=message_type
                        )
                    except Exception as e:
                        self.lib_logger.log_admin(f"LibLogger error for {ip}: {e}", log_level=3)

                except socket.timeout:
                    consecutive_errors = 0
                    continue
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
                except Exception as e:
                    with self.lock:
                        self.stats['errors'] += 1
                    self.lib_logger.log_admin(f"Unexpected error for {ip}:{port}: {e}", log_level=3)
                    import traceback
                    self.lib_logger.log_admin(traceback.format_exc(), log_level=3)
                    break

        finally:
            with self.clients_lock:
                current_client = self.clients.get(ip)
                if current_client is thread_client and current_client.sock is client_sock:
                    if not thread_client.stop_event.is_set():
                        self._disconnect_client(thread_client)

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
                    client_sock, client_address = self.server_socket.accept()

                    # Start client handler thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client_sock, client_address),
                        daemon=True,
                        name=f"LogClient-{client_address[0]}"
                    )
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

        # Disconnect all clients
        with self.clients_lock:
            for client in self.clients.values():
                self._disconnect_client(client)
            self.clients.clear()

        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass

        # Shutdown LibLogger
        self.lib_logger.shutdown()

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