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

import socket
import select
import queue
import threading
import time
from time import sleep
from datetime import datetime
from pathlib import Path
from typing import Optional, Union, Dict, List, Tuple

# Add the protocol directory to Python path
# Get the directory where THIS script is located
import sys
from pathlib import Path
script_dir = Path(__file__).resolve().parent

# Navigate up to the common directory where protocol.h and protocol.py live
protocol_dir = script_dir.parent / 'protocol'
sys.path.insert(0, str(protocol_dir))

import protocol

class Esp32Server:
    def __init__(self):
        # Your known devices (fixed IPs)
        self.devices = {
            "10.10.3.10": {"name": "stand", "required": True, "init": protocol.Cmd.CMD_STAND_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0},
            "10.10.3.20": {"name": "lift", "required": True, "init": protocol.Cmd.CMD_LIFT_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0},
            "10.10.3.30": {"name": "plinky-plonky", "required": True, "init": protocol.Cmd.CMD_PLINKY_PLONKY_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0},
            # Doors have index values also
            "10.10.3.40": {"name": "door 0", "required": True, "init": protocol.Cmd.CMD_DOOR_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0, "index": 0},
            "10.10.3.41": {"name": "door 1", "required": True, "init": protocol.Cmd.CMD_DOOR_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0, "index": 1},
            "10.10.3.42": {"name": "door 2", "required": True, "init": protocol.Cmd.CMD_DOOR_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0, "index": 2},
            "10.10.3.43": {"name": "door 3", "required": True, "init": protocol.Cmd.CMD_DOOR_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0, "index": 3},
            "10.10.3.44": {"name": "door 4", "required": True, "init": protocol.Cmd.CMD_DOOR_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0, "index": 4},
            "10.10.3.45": {"name": "door 5", "required": True, "init": protocol.Cmd.CMD_DOOR_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0, "index": 5},
            "10.10.3.100": {"name": "test", "required": False, "init": protocol.Cmd.CMD_STAND_INIT, "connected": False, "sock": None, "initialised": False, "reference": 0},
        }

        # Single unified queue for incoming messages (RspMsg and IndMsg only)
        # Each message is (ip, msg_obj)
        self.incoming_queue = queue.Queue()

        # Socket management
        self.server_socket = None
        self.running = False

        # For select() polling
        self.socket_list = []
        self.device_sockets = {}  # Map socket -> ip

        # Partial message buffer for each socket (in case we get fragmented reads)
        self.socket_buffers = {}  # Map socket -> bytes

        # Track pending init responses
        self.pending_inits = {}  # Map socket -> (ip, reference, timestamp)

    def start(self, port=5000):
        """Start the server and wait for connections"""
        self.running = True

        # Create server socket
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('0.0.0.0', port))
        self.server_socket.listen(len(self.devices))

        # Add server socket to list for select
        self.socket_list = [self.server_socket]

        print(f"Listening for ESP32s on port {port}")
        print("Known devices:")
        for ip, info in self.devices.items():
            print(f"  - {info['name']} ({ip})")
        print()

        # Start receiver thread
        receiver_thread = threading.Thread(target=self._receiver_loop)
        receiver_thread.daemon = True
        receiver_thread.start()

        return receiver_thread

    def _parse_message(self, data: bytes) -> Optional[Union[protocol.RspMsg, protocol.IndMsg]]:
        """
        Parse incoming data based on magic byte
        Returns the appropriate message object or None if invalid
        """
        if len(data) < 1:
            return None

        magic = data[0]

        try:
            if magic == protocol.PROTOCOL_MAGIC_RSP:
                return protocol.RspMsg.unpack(data)
            elif magic == protocol.PROTOCOL_MAGIC_IND:
                return protocol.IndMsg.unpack(data)
            else:
                # Log messages and unknown magics are ignored
                return None
        except Exception as e:
            print(f"Error parsing message: {e}")
            return None

    def _handle_init_response(self, sock, ip, msg):
        """Handle response to init command"""
        if sock not in self.pending_inits:
            return False

        expected_ref, expected_cmd, timestamp = self.pending_inits[sock]  # Store expected command too

        # Check if this response matches our pending init
        if msg.reference == expected_ref and msg.cmd_or_qry == expected_cmd:
            if msg.status == protocol.Status.STATUS_OK:
                print(f"✓ {self.devices[ip]['name']} initialised successfully")
                self.devices[ip]["initialised"] = True
            else:
                print(f"✗ {self.devices[ip]['name']} initialization failed with status {protocol.Status(msg.status).name}")

            # Remove from pending
            del self.pending_inits[sock]
            return True

        return False

    def _receiver_loop(self):
        """Main loop using select() to handle all sockets"""
        while self.running:
            try:
                # Check which sockets have data
                readable, _, exceptional = select.select(self.socket_list, [], [], 1.0)

                for sock in readable:
                    if sock is self.server_socket:
                        # New connection
                        client_sock, addr = self.server_socket.accept()
                        client_ip = addr[0]

                        if client_ip in self.devices:
                            # Known device - accept
                            self.devices[client_ip]["connected"] = True
                            self.devices[client_ip]["sock"] = client_sock
                            self.devices[client_ip]["initialised"] = False  # Reset init flag
                            self.socket_list.append(client_sock)
                            self.device_sockets[client_sock] = client_ip
                            self.socket_buffers[client_sock] = b""  # Initialize buffer
                            print(f"{self.devices[client_ip]['name']} connected from {client_ip}")

                            # Send init command
                            cmd = protocol.CmdMsg(self.devices[client_ip]["init"], self.devices[client_ip]["reference"], 0, 0, 0, 0)

                            # Track this pending init with both reference and command
                            self.pending_inits[client_sock] = (self.devices[client_ip]["reference"], self.devices[client_ip]["init"], time.time())

                            # Send the command
                            protocol.send_message(client_sock, cmd)
                            self.devices[client_ip]["reference"] = self.next_reference(self.devices[client_ip]["reference"])
                            print(f"  Sent init command (ref={self.devices[client_ip]["reference"]}) to {self.devices[client_ip]['name']}")
                        else:
                            # Unknown device - reject
                            print(f"Rejected unknown device from {client_ip}")
                            client_sock.close()

                    elif sock in self.device_sockets:
                        # Data from known device
                        ip = self.device_sockets[sock]
                        try:
                            data = sock.recv(1024)
                            if data:
                                #print(f"{ip}: received {len(data)} byte(s): ", end='')
                                #for item in data:
                                #    print(f"{item:02x}", end='')
                                #print("")
                                # Add to buffer and try to parse complete messages
                                self.socket_buffers[sock] += data

                                # Try to parse messages from buffer
                                while True:
                                    if len(self.socket_buffers[sock]) < 1:
                                        break

                                    # Determine expected message size from magic byte
                                    magic = self.socket_buffers[sock][0]
                                    msg_size = None

                                    if magic == protocol.PROTOCOL_MAGIC_RSP:
                                        msg_size = protocol.RspMsg.SIZE
                                    elif magic == protocol.PROTOCOL_MAGIC_IND:
                                        msg_size = protocol.IndMsg.SIZE
                                    else:
                                        # Unknown magic - discard first byte and continue
                                        # (silently ignores log messages and invalid data)
                                        self.socket_buffers[sock] = self.socket_buffers[sock][1:]
                                        continue

                                    # Check if we have a complete message
                                    if len(self.socket_buffers[sock]) >= msg_size:
                                        # Extract and parse message
                                        msg_data = self.socket_buffers[sock][:msg_size]
                                        self.socket_buffers[sock] = self.socket_buffers[sock][msg_size:]

                                        msg = self._parse_message(msg_data)
                                        if msg:
                                            # Check if this is a response to a pending init
                                            if sock in self.pending_inits and isinstance(msg, protocol.RspMsg):
                                                if self._handle_init_response(sock, ip, msg):
                                                    # This response was consumed by init handling
                                                    continue

                                            # Not an init response, add to queue
                                            self.incoming_queue.put((ip, msg))
                                    else:
                                        # Wait for more data
                                        break
                            else:
                                # Connection closed
                                self._handle_disconnect(sock, ip)
                        except Exception as e:
                            print(f"Error reading from {ip}: {e}")
                            self._handle_disconnect(sock, ip)

                # Handle exceptional conditions
                for sock in exceptional:
                    if sock in self.device_sockets:
                        ip = self.device_sockets[sock]
                        self._handle_disconnect(sock, ip)

                # Clean up stale pending inits (timeout after 5 seconds)
                current_time = time.time()
                stale_socks = []
                for sock, (ref, cmd, timestamp) in self.pending_inits.items():  # Unpack all 3 values
                    if current_time - timestamp > 5.0:
                        ip = self.device_sockets.get(sock, "unknown")
                        print(f"Warning: Init response timeout for {ip}")
                        stale_socks.append(sock)

                for sock in stale_socks:
                    if sock in self.pending_inits:
                        del self.pending_inits[sock]

            except Exception as e:
                print(f"Error in receiver loop: {e}")
                time.sleep(1)

    def _handle_disconnect(self, sock, ip):
        """Clean up disconnected device"""
        if ip in self.devices:
            self.devices[ip]["connected"] = False
            self.devices[ip]["sock"] = None
            self.devices[ip]["initialised"] = False
            print(f"{self.devices[ip]['name']} disconnected")

        # Clean up pending init
        if sock in self.pending_inits:
            del self.pending_inits[sock]

        if sock in self.socket_list:
            self.socket_list.remove(sock)
        if sock in self.device_sockets:
            del self.device_sockets[sock]
        if sock in self.socket_buffers:
            del self.socket_buffers[sock]

        try:
            sock.close()
        except:
            pass

    def _print_waiting_status(self, final=False, elapsed=0):
        """Print the current waiting status on a single line"""
        waiting_connect = []
        waiting_init = []

        for ip, info in self.devices.items():
            if not info["connected"]:
                waiting_connect.append(f"{info['name']} [{ip}]")
            elif not info["initialised"]:
                waiting_init.append(f"{info['name']} [{ip}]")

        CLEAR_LINE = "\033[2K"
        CURSOR_START = "\033[0G"

        if waiting_connect or waiting_init:
            status_parts = []
            if waiting_connect:
                status_parts.append(f"{', '.join(waiting_connect)}")
            if waiting_init:
                status_parts.append(f"initializing {', '.join(waiting_init)}")

            status = f"⏳ [{elapsed:.0f}s] Waiting for: {'; '.join(status_parts)}"

            if final:
                print(f"{CLEAR_LINE}{CURSOR_START}{status}")
            else:
                print(f"{CLEAR_LINE}{CURSOR_START}{status}", end='', flush=True)

    def send_command(self, ip: str, command: protocol.CmdMsg) -> bool:
        """
        Send a command message to a specific device
        """
        if ip in self.devices and self.devices[ip]["connected"]:
            # Check if device is initialised (unless this IS the init command)
            if command.command not in [info["init"] for info in self.devices.values()]:
                if not self.devices[ip]["initialised"]:
                    print(f"Warning: {self.devices[ip]['name']} not yet initialised")
                    return False

            try:
                sock = self.devices[ip]["sock"]
                return protocol.send_message(sock, command)
            except Exception as e:
                print(f"Failed to send to {self.devices[ip]['name']}: {e}")
                return False
        return False

    def next_reference(self, reference) -> int:
        reference += 1
        if reference > 255:
            reference = 0
        return reference

    def send_query(self, ip: str, query: protocol.QryMsg) -> bool:
        """
        Send a query message to a specific device
        """
        if ip in self.devices and self.devices[ip]["connected"]:
            # Check if device is initialised
            if not self.devices[ip]["initialised"]:
                print(f"Warning: {self.devices[ip]['name']} not yet initialised")
                return False

            try:
                sock = self.devices[ip]["sock"]
                return protocol.send_message(sock, query)
            except Exception as e:
                print(f"Failed to send to {self.devices[ip]['name']}: {e}")
                return False
        return False

    def send_command_to_all(self, command: protocol.CmdMsg) -> Dict[str, bool]:
        """Send command to all connected devices"""
        results = {}
        for ip, info in self.devices.items():
            if info["connected"] and info["initialised"]:
                results[ip] = self.send_command(ip, command)
        return results

    def send_query_to_all(self, query: protocol.QryMsg) -> Dict[str, bool]:
        """Send query to all connected devices"""
        results = {}
        for ip, info in self.devices.items():
            if info["connected"] and info["initialised"]:
                results[ip] = self.send_query(ip, query)
        return results

    def get_device_ip_list(self, required=False, connected=False, initialised=False):
        """Return list of device IPs"""
        return [ip for ip, info in self.devices.items() if (not required or info["required"])         \
                                                           and (not connected or info["connected"])       \
                                                           and (not initialised or info["initialised"])]

    def device_count(self, required=False, connected=False, initialised=False):
        return len(self.get_device_ip_list(required, connected, initialised))

    def is_device_connected(self, ip):
        """Check if a specific device is connected"""
        return ip in self.devices and self.devices[ip]["connected"]

    def is_device_initialised(self, ip):
        """Check if a specific device is initialised"""
        return ip in self.devices and self.devices[ip]["initialised"]

    def wait_for_all_devices(self, timeout=None):
        """Block until all known devices are connected AND initialised"""
        print("Waiting for all devices to connect and initialise...")
        start_time = time.time()
        last_print_time = 0

        CLEAR_LINE = "\033[2K"
        CURSOR_START = "\033[0G"

        while self.running:
            all_connected = (self.device_count(required=True, connected=True) == self.device_count(required=True))
            all_initialised = (self.device_count(required=True, initialised=True) == self.device_count(required=True))

            if all_connected and all_initialised:
                elapsed = time.time() - start_time
                print(f"\nAll devices connected and initialised! (took {elapsed:.1f}s)")
                return True

            elapsed = time.time() - start_time
            if timeout and elapsed > timeout:
                self._print_waiting_status(True, elapsed)
                print("\nTimeout waiting for devices")
                return False

            current_time = time.time()
            if current_time - last_print_time >= 5.0:
                self._print_waiting_status(False, elapsed)
                last_print_time = current_time

            time.sleep(1)

        return False

    def stop(self):
        """Clean shutdown"""
        self.running = False

        # Close all sockets
        for sock in self.socket_list:
            try:
                sock.close()
            except:
                pass

        print("ESP32 server stopped")
