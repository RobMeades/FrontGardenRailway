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
import struct
import time
import threading
from pathlib import Path
from typing import Optional, Dict, Set
from datetime import datetime

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

    def __init__(self, bind_address: str = '0.0.0.0', port: int = 5001):
        self.bind_address = bind_address
        self.port = port
        self.server_socket: Optional[socket.socket] = None
        self.client_threads: Dict[socket.socket, threading.Thread] = {}
        self.running = True
        self.stats = {
            'connections': 0,
            'log_messages': 0,
            'bytes_received': 0,
            'errors': 0
        }
        self.lock = threading.Lock()

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

    def _handle_client(self, client_socket: socket.socket,
                       client_address: tuple) -> None:
        """Handle a connected client - runs in its own thread"""
        device_info = {
            'addr': client_address[0],
            'port': client_address[1]
        }

        # Set socket to blocking mode with a timeout to allow checking running flag
        client_socket.settimeout(1.0)

        print(f"New connection from {client_address[0]}:{client_address[1]}")

        try:
            while self.running:
                try:
                    # Receive and parse FGR message
                    msg = receive_message(client_socket, timeout=1.0)

                    if msg is None:
                        # Timeout or connection issue, continue loop to check running flag
                        continue

                    with self.lock:
                        self.stats['bytes_received'] += len(msg.pack())

                        # Check if this is a log message
                        if msg.message_type == FGRMsgType.FGR_MSG_TYPE_LOG:
                            self.stats['log_messages'] += 1

                            # Extract log message and level
                            log_text = msg.get_log_message()
                            # In the header, error_or_state contains the log level for LOG messages
                            log_level = msg.error_or_state

                            # Write to journal with IP prepended
                            self._write_to_journal(log_text, log_level, device_info)

                except socket.timeout:
                    # Expected timeout, just continue to check running flag
                    continue
                except ConnectionResetError:
                    print(f"Connection reset by {client_address[0]}:{client_address[1]}")
                    break
                except BrokenPipeError:
                    print(f"Broken pipe from {client_address[0]}:{client_address[1]}")
                    break
                except Exception as e:
                    with self.lock:
                        self.stats['errors'] += 1
                    print(f"Error handling client {client_address[0]}:{client_address[1]}: {e}")
                    # Don't break on transient errors, continue trying
                    time.sleep(0.1)
                    continue

        finally:
            try:
                client_socket.close()
            except:
                pass

            with self.lock:
                # Remove thread from tracking
                if client_socket in self.client_threads:
                    del self.client_threads[client_socket]

            print(f"Connection closed from {client_address[0]}:{client_address[1]}")

    def start(self) -> None:
        """Start the log server"""
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
            print("Press Ctrl+C to stop")
            print()

            # Main accept loop
            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()

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
        """Stop the log server and clean up"""
        print("\nShutting down...")

        # Wait for all client threads to finish
        print("Waiting for client threads to finish...")
        with self.lock:
            threads = list(self.client_threads.values())

        for thread in threads:
            try:
                thread.join(timeout=2.0)
            except:
                pass

        # Close server socket
        if self.server_socket:
            try:
                self.server_socket.close()
            except:
                pass

        # Print statistics
        print("\n=== Server Statistics ===")
        print(f"Total connections: {self.stats['connections']}")
        print(f"Log messages received: {self.stats['log_messages']}")
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

    args = parser.parse_args()

    # Validate port range
    if args.port < 1 or args.port > 65535:
        print(f"Error: Invalid port number {args.port}")
        sys.exit(1)

    # Create and start server
    server = FGRLogServer(
        bind_address=args.bind_address,
        port=args.port
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