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

# All written by DeepSeek :-).

"""
Log Server for FGR ESP32 Devices

This script listens for FGR protocol log messages from ESP32 devices
and writes them to the Linux systemd journal.

The script handles:
- TCP socket connections from multiple devices
- Automatic detection and parsing of FGR log messages
- Writing logs to the system journal with appropriate priorities
- Graceful shutdown on SIGTERM/SIGINT

Usage:
    python3 fgr_log_server.py [--port PORT] [--bind-address ADDR]

Options:
    --port PORT             Port to listen on (default: 5000)
    --bind-address ADDR     Address to bind to (default: 0.0.0.0)
"""

import sys
import socket
import argparse
import signal
import struct
import time
from typing import Optional, Dict, Set
from datetime import datetime

# Import the generated FGR protocol module
try:
    from fgr_protocol import (
        FGRMsg, FGRMsgType, FGRLogLevel, receive_message, send_message
    )
except ImportError:
    print("Error: Cannot import fgr_protocol module")
    print("Please ensure fgr_protocol.py is in the Python path")
    sys.exit(1)

# Try to import systemd journal support
try:
    from systemd import journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("Warning: python-systemd not installed, falling back to console output")

# Map FGR log levels to systemd priorities
# See: https://www.freedesktop.org/software/systemd/man/latest/sd-daemon.html
LOG_LEVEL_TO_PRIORITY = {
    FGRLogLevel.FGR_LOG_LEVEL_DEBUG: journal.LOG_DEBUG if HAS_SYSTEMD else 7,
    FGRLogLevel.FGR_LOG_LEVEL_INFO: journal.LOG_INFO if HAS_SYSTEMD else 6,
    FGRLogLevel.FGR_LOG_LEVEL_WARN: journal.LOG_WARNING if HAS_SYSTEMD else 4,
    FGRLogLevel.FGR_LOG_LEVEL_ERROR: journal.LOG_ERR if HAS_SYSTEMD else 3,
}

# Default priorities for unknown log levels
DEFAULT_PRIORITY = journal.LOG_INFO if HAS_SYSTEMD else 6


class FGRLogServer:
    """FGR Protocol Log Server"""
    
    def __init__(self, bind_address: str = '0.0.0.0', port: int = 5000):
        self.bind_address = bind_address
        self.port = port
        self.server_socket: Optional[socket.socket] = None
        self.client_sockets: Set[socket.socket] = set()
        self.running = True
        self.stats = {
            'connections': 0,
            'log_messages': 0,
            'bytes_received': 0,
            'errors': 0
        }
        
        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum: int, frame) -> None:
        """Handle shutdown signals"""
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False
    
    def _get_priority_from_level(self, level: int) -> int:
        """Convert FGR log level to systemd priority"""
        return LOG_LEVEL_TO_PRIORITY.get(level, DEFAULT_PRIORITY)
    
    def _write_to_journal(self, message: str, level: int, 
                          device_info: Dict[str, str]) -> None:
        """Write a log message to the systemd journal"""
        priority = self._get_priority_from_level(level)
        
        if HAS_SYSTEMD:
            # Send to systemd journal with metadata
            with journal.JournalHandler(
                level=priority,
                identifier='fgr-log-server'
            ) as journal_handler:
                # Add extra fields to the journal entry
                extra_fields = {
                    'FGR_DEVICE_ADDR': device_info.get('addr', 'unknown'),
                    'FGR_DEVICE_PORT': device_info.get('port', 'unknown'),
                    'FGR_LOG_LEVEL': str(level),
                }
                
                # Create the log entry
                journal.send(
                    message,
                    priority=priority,
                    **extra_fields
                )
        else:
            # Fallback to console output
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            level_names = {
                FGRLogLevel.FGR_LOG_LEVEL_DEBUG: 'DEBUG',
                FGRLogLevel.FGR_LOG_LEVEL_INFO: 'INFO',
                FGRLogLevel.FGR_LOG_LEVEL_WARN: 'WARN',
                FGRLogLevel.FGR_LOG_LEVEL_ERROR: 'ERROR',
            }
            level_name = level_names.get(level, f'LEVEL_{level}')
            
            print(f"[{timestamp}] [{device_info['addr']}:{device_info['port']}] "
                  f"[{level_name}] {message}")
    
    def _handle_client(self, client_socket: socket.socket, 
                       client_address: tuple) -> None:
        """Handle a connected client"""
        device_info = {
            'addr': client_address[0],
            'port': client_address[1]
        }
        
        print(f"New connection from {client_address[0]}:{client_address[1]}")
        
        try:
            while self.running:
                # Receive and parse FGR message
                msg = receive_message(client_socket, timeout=1.0)
                
                if msg is None:
                    # Timeout or connection issue, continue loop
                    continue
                
                self.stats['bytes_received'] += len(msg.pack())
                
                # Check if this is a log message
                if msg.message_type == FGRMsgType.FGR_MSG_TYPE_LOG:
                    self.stats['log_messages'] += 1
                    
                    # Extract log message and level
                    log_text = msg.get_log_message()
                    log_level = msg.header.log_level
                    
                    # Write to journal
                    self._write_to_journal(log_text, log_level, device_info)
                    
                    # Optional: Send acknowledgment back to device
                    # (Not required by protocol, but could be implemented)
                    # ack = FGRMsg.create_rsp(0, msg.reference)
                    # send_message(client_socket, ack)
                    
                else:
                    # Non-log message received
                    print(f"Received non-log message type {msg.message_type} "
                          f"from {client_address[0]}:{client_address[1]}")
                    
        except ConnectionResetError:
            print(f"Connection reset by {client_address[0]}:{client_address[1]}")
        except BrokenPipeError:
            print(f"Broken pipe from {client_address[0]}:{client_address[1]}")
        except Exception as e:
            self.stats['errors'] += 1
            print(f"Error handling client {client_address[0]}:{client_address[1]}: {e}")
        finally:
            client_socket.close()
            self.client_sockets.discard(client_socket)
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
            print("Press Ctrl+C to stop")
            print()
            
            # Main accept loop
            while self.running:
                try:
                    client_socket, client_address = self.server_socket.accept()
                    self.stats['connections'] += 1
                    self.client_sockets.add(client_socket)
                    
                    # Handle client in the main thread (simplified)
                    # For production, you might want to use threading
                    self._handle_client(client_socket, client_address)
                    
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
        
        # Close all client connections
        for client_socket in self.client_sockets:
            try:
                client_socket.close()
            except:
                pass
        self.client_sockets.clear()
        
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
        default=5000,
        help='Port to listen on (default: 5000)'
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
