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

# Written by DeepSeek :-)

"""
Web Interface for FGR Controller

Provides a web-based dashboard for monitoring and controlling nodes.
Subclasses the main Controller class and reads logs from systemd journal.

Controller socket binds to specified IP (for node communication).
Web server binds to all interfaces (for admin access).

Usage:
    python web_controller.py [--ip LISTEN_IP] [--port PORT] [--cfg CFG_FILE]
                             [--http-port HTTP_PORT] [--log-level LEVEL]
"""

import time
import os
import sys
import argparse
print("Importing asyncio: may take some time...", flush=True)
import asyncio
import json
import threading
import signal
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from collections import deque
print("Importing aiohttp: may take some time...", flush=True)
from aiohttp import web

# Import the controller
from controller import Controller, ConnectionState, NodeHandler, Node
import fgr_protocol as fgr

# Try to import systemd journal support
try:
    from systemd import journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("Warning: python-systemd not installed, journal reading disabled")
    print("Install with: pip install systemd-python")

# States that indicate a node is working/operational
WORKING_FGR_STATES = {
    fgr.FGRState.FGR_STATE_STARTED,   # 2 - Running normally
    fgr.FGRState.FGR_STATE_STOPPED,   # 3 - Stopped but operational/configured
    fgr.FGRState.FGR_STATE_BUSY,      # 4 - Busy but operational
}

# Default ports
HTTP_PORT_DEFAULT = 8080
CONTROLLER_PORT_DEFAULT = 5000
CONTROLLER_IP_DEFAULT = "10.10.3.1"

# Max log entries to keep
MAX_LOG_ENTRIES = 500

# Journal identifier for log_server.py
JOURNAL_IDENTIFIER = 'fgr-log-server'

# Node grid layout configuration file
NODE_GRID_CONFIG = Path(__file__).parent / "node_grid_layout.json"

# Nodes per page (2 rows x 4 columns)
NODES_PER_PAGE = 8

# Log level names
LOG_LEVEL_NAMES = ['DEBUG', 'INFO', 'WARN', 'ERROR']


def format_fgr_state(state):
    """Format fgr_state_t value for display"""
    if not state:
        return "UNKNOWN"
    name = state.name if hasattr(state, 'name') else str(state)
    # Remove FGR_STATE_ prefix
    if name.startswith('FGR_STATE_'):
        name = name[10:]
    # Replace underscores with spaces
    return name.replace('_', ' ')


def format_fgr_error(error):
    """Format fgr_error_t value for display"""
    if not error:
        return "NONE"
    name = error.name if hasattr(error, 'name') else str(error)
    # Remove FGR_ERROR_ prefix
    if name.startswith('FGR_ERROR_'):
        name = name[10:]
    # Replace underscores with spaces
    return name.replace('_', ' ')


def format_fgr_message(msg_type, is_response=True):
    """Format FGR message type for display"""
    if not msg_type:
        return "UNKNOWN"
    name = msg_type.name if hasattr(msg_type, 'name') else str(msg_type)
    # Remove FGR_ prefix
    if name.startswith('FGR_'):
        name = name[4:]
    # Replace REQ_CNF with CNF or IND_RSP with IND
    if 'REQ_CNF' in name:
        name = name.replace('REQ_CNF', 'CNF')
    elif 'IND_RSP' in name:
        name = name.replace('IND_RSP', 'IND')
    # Replace underscores with spaces
    return name.replace('_', ' ')


def format_connection_duration(node: Node) -> str:
    """Format connection duration with datetime"""
    if node.sock and hasattr(node, 'connection_time') and node.connection_time:
        duration = time.time() - node.connection_time
        days = int(duration // 86400)
        hours = int((duration % 86400) // 3600)
        minutes = int((duration % 3600) // 60)

        # Format the "since" part
        dt = datetime.fromtimestamp(node.connection_time)
        since_str = dt.strftime("%H:%M:%S %d-%m-%Y")  # Changed format

        if days > 0:
            return f"{days}d {hours}h (since {since_str})"
        elif hours > 0:
            return f"{hours}h {minutes}m (since {since_str})"
        elif minutes > 0:
            return f"{minutes}m (since {since_str})"
        else:
            return f"<1m (since {since_str})"
    elif not node.sock and hasattr(node, 'last_seen') and node.last_seen:
        duration = time.time() - node.last_seen
        dt = datetime.fromtimestamp(node.last_seen)
        since_str = dt.strftime("%H:%M:%S %d-%m-%Y")  # Changed format
        if duration < 3600:
            return f"disconnected {int(duration // 60)}m ago (since {since_str})"
        else:
            return f"disconnected {int(duration // 3600)}h ago (since {since_str})"
    return ""


class WebController(Controller):
    """Web-enabled FGR Controller with journal log reading"""

    def __init__(self, listen_ip: str = CONTROLLER_IP_DEFAULT,
                 port: int = CONTROLLER_PORT_DEFAULT,
                 nodes_dir: str = None, cfg_file: str = None,
                 http_port: int = HTTP_PORT_DEFAULT):
        super().__init__(listen_ip, port, nodes_dir, cfg_file)

        self.http_port = http_port
        self.web_app = None
        self.web_runner = None
        self.web_running = False

        # Log storage for web interface - store as list with version tracking
        self.log_entries: List[Tuple[int, str]] = []  # (version, message)
        self.max_log_entries = MAX_LOG_ENTRIES
        self._log_counter = 0

        # SSE clients
        self.sse_clients = set()

        # Store node-specific data for web display
        self.node_custom_data: Dict[str, Dict[str, Any]] = {}

        # Store recent message notifications
        self.node_notifications: Dict[str, Dict[str, Any]] = {}

        # Store custom card HTML for each node
        self.node_card_html: Dict[str, str] = {}

        # Journal reader thread
        self.journal_running = False
        self.journal_thread = None

        # Node grid layout
        self.node_grid_layout = self._load_node_grid_layout()

        # Start journal reader if available
        if HAS_SYSTEMD:
            self._start_journal_reader()
        else:
            self._log_message("Journal reading disabled - node logs will not appear")

        # Override the logger to capture controller logs
        self._setup_log_capture()

        # Record start time
        self._start_time = time.time()

    def _load_node_grid_layout(self) -> Dict[str, Any]:
        """Load node grid layout from config file"""
        if NODE_GRID_CONFIG.exists():
            try:
                with open(NODE_GRID_CONFIG, 'r') as f:
                    return json.load(f)
            except Exception as e:
                self._log_message(f"Error loading node grid layout: {e}")
        return {'order': [], 'pages': {}, 'columns': 4, 'rows': 2}

    def _save_node_grid_layout(self):
        """Save node grid layout to config file"""
        try:
            with open(NODE_GRID_CONFIG, 'w') as f:
                json.dump(self.node_grid_layout, f, indent=2)
        except Exception as e:
            self._log_message(f"Error saving node grid layout: {e}")

    def _add_log(self, prefix: str, message: str):
        """Add a log message to the buffer with automatic trimming"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        version = self._log_counter
        self._log_counter += 1
        self.log_entries.append((version, f"[{timestamp}] {prefix} {message}"))

        # Trim if needed
        while len(self.log_entries) > self.max_log_entries:
            self.log_entries.pop(0)

    def _setup_log_capture(self):
        """Capture controller logs for web interface display"""
        class WebLogHandler(logging.Handler):
            def __init__(self, callback):
                super().__init__()
                self.callback = callback

            def emit(self, record):
                msg = self.format(record)
                self.callback(msg)

        handler = WebLogHandler(self._capture_controller_log)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logging.getLogger().addHandler(handler)

    def _capture_controller_log(self, message: str):
        """Capture a controller log message for web display"""
        # Extract just the message part
        if ' - ' in message:
            parts = message.split(' - ', 2)
            msg_text = parts[2] if len(parts) >= 3 else message
        else:
            msg_text = message
        self._add_log("[CTRL]", msg_text)

    def _add_node_log(self, message: str):
        """Add a node log message to the buffer"""
        self._add_log("[NODE]", message)

    def _log_message(self, message: str):
        """Add a message to the log buffer"""
        self._add_log("[CTRL]", message)

    def _start_journal_reader(self):
        """Start background thread to read logs from journal"""
        self.journal_running = True
        self.journal_thread = threading.Thread(target=self._journal_reader_loop, daemon=True)
        self.journal_thread.start()
        self._log_message(f"Journal reader started, monitoring '{JOURNAL_IDENTIFIER}'")

    def _stop_journal_reader(self):
        """Stop the journal reader thread"""
        self.journal_running = False
        if self.journal_thread:
            self.journal_thread.join(timeout=2)

    def _journal_reader_loop(self):
        """Background thread to read logs from systemd journal"""
        try:
            # Open journal reader
            j = journal.Reader()
            j.add_match(SYSLOG_IDENTIFIER=JOURNAL_IDENTIFIER)

            # Get the cursor of the last entry
            j.seek_tail()
            j.get_previous(1)
            last_cursor = None
            for entry in j:
                last_cursor = entry.get('__CURSOR', None)
                break

            # Now seek to tail and read backwards to get recent logs
            j.seek_tail()
            j.get_previous(100)

            existing_logs = []
            for entry in j:
                message = entry.get('MESSAGE', '')
                if message:
                    message = message.rstrip()
                    existing_logs.append(message)

            # Add existing logs (newest first, reverse to oldest first)
            for msg in reversed(existing_logs):
                self._add_node_log(msg)

            if existing_logs:
                self._log_message(f"Loaded {len(existing_logs)} existing node logs")

            # Now follow new entries using the cursor
            if last_cursor:
                j.seek_cursor(last_cursor)
                j.get_next(1)

            while self.journal_running:
                # Wait for new entries (1 second timeout)
                ret = j.wait(1000000)

                if ret > 0:  # New entries available
                    for entry in j:
                        message = entry.get('MESSAGE', '')
                        if message:
                            message = message.rstrip()
                            self._add_node_log(message)

        except Exception as e:
            self._log_message(f"Journal reader error: {e}")

    def update_node_custom_data(self, node_name: str, custom_data: Dict[str, Any]):
        """Store custom data from a node for web display and update card HTML"""
        self.node_custom_data[node_name] = {
            **custom_data,
            'last_update': datetime.now().isoformat()
        }

        # Update custom card HTML from node handler
        node = self.nodes.get(node_name)
        if node and node.handler:
            try:
                node_data = {
                    'name': node_name,
                    'type': node.node_type,
                    'ip': node.ip,
                    'state': node.state.name if hasattr(node.state, 'name') else str(node.state),
                    'connected': node.sock is not None,
                    'custom_data': custom_data,
                    'message_count': node.message_count,
                    'heartbeat_count': node.heartbeat_count
                }
                self.node_card_html[node_name] = node.handler.get_card_html(node_name, node_data)
            except Exception as e:
                self._log_message(f"Error getting card HTML for {node_name}: {e}")

    def set_node_notification(self, node_name: str, message: str, is_sent: bool = True, is_success: bool = True):
        """Set a notification for a node (sent/received message)

        Args:
            node_name: Name of the node
            message: Notification message text
            is_sent: True for sent messages, False for received
            is_success: True for success, False for failure
        """
        # Add emoji indicators - play/reverse for sent/received, X for failure
        if not is_success:
            prefix = "❌"
        elif is_sent:
            prefix = "▶️"  # Play arrow for sent (command going out)
        else:
            prefix = "◀️"  # Reverse arrow for received (response coming in)

        self.node_notifications[node_name] = {
            'message': f"{prefix} {message}",
            'is_sent': is_sent,
            'is_success': is_success,
            'timestamp': time.time()
        }

    def _dispatch_message(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Override to capture messages for toasts before dispatching to node handler"""
        msg_type = msg.message_type

        # Show toasts for relevant messages (independent of node handler)
        if msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_IND:
            ind_type = msg.subtype
            # Convert integer to enum member for proper formatting
            try:
                ind_enum = fgr.FGRIndRsp(ind_type)
                msg_name = format_fgr_message(ind_enum, is_response=False)
            except ValueError:
                # Device-specific indication (not in standard enum)
                msg_name = f"0x{ind_type:03X}"
            # msg_name already includes "IND" prefix from format_fgr_message
            notification_msg = msg_name
            self.set_node_notification(node.name, notification_msg, False, True)

        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_CNF:
            req_type = msg.subtype
            error = msg.error_or_state
            # Convert integer to enum member for proper formatting
            try:
                req_enum = fgr.FGRReqCnf(req_type)
                msg_name = format_fgr_message(req_enum, is_response=True)
            except ValueError:
                msg_name = f"0x{req_type:03X}"
            if error == fgr.FGRError.FGR_ERROR_NONE:
                notification_msg = msg_name
                self.set_node_notification(node.name, notification_msg, False, True)
            else:
                error_name = format_fgr_error(error)
                notification_msg = f"{msg_name} failed: {error_name}"
                self.set_node_notification(node.name, notification_msg, False, False)

        # Call the parent's dispatch to let the node handler do its job
        super()._dispatch_message(node, msg)

    def _get_system_status(self) -> Dict[str, Any]:
        """Get current system status for API"""
        nodes_status = []

        # Get ordered node list based on layout (full order, not paginated)
        layout_order = self.node_grid_layout.get('order', [])
        pages = self.node_grid_layout.get('pages', {})
        remaining_nodes = list(self.nodes.keys())

        # Count essential nodes that are in a working state
        working_nodes = len([n for n in self.nodes.values()
                                if n.sock and n.essential and
                                n.fgr_state in WORKING_FGR_STATES])

        # Build full ordered list
        ordered_nodes = []

        # Add nodes in layout order first
        for node_name in layout_order:
            if node_name in self.nodes:
                ordered_nodes.append(node_name)
                if node_name in remaining_nodes:
                    remaining_nodes.remove(node_name)

        # Add remaining nodes at the end
        ordered_nodes.extend(remaining_nodes)

        for name in ordered_nodes:
            node = self.nodes.get(name)
            if not node:
                continue

            custom_data = self.node_custom_data.get(name, {})
            notification = self.node_notifications.get(name)
            custom_html = self.node_card_html.get(name, '')

            # Calculate connection duration with datetime
            connection_duration = format_connection_duration(node)

            # Determine display state with formatted name
            if node.sock:
                display_state = node.state.name if isinstance(node.state, ConnectionState) else str(node.state)
                # Handle local vs FGR states
                if display_state.startswith('FGR_'):
                    display_state = display_state[4:].replace('_', ' ').lower()
                else:
                    display_state = display_state.lower()
            else:
                display_state = "disconnected"

            # Format status text
            if node.log_on is None or node.log_level is None:
                log_status = ""
            else:
                log_status = f"{'ON' if node.log_on else 'OFF'}/{LOG_LEVEL_NAMES[node.log_level] if node.log_level < 4 else '?'}"

            if node.led_on is None or node.led_breathe_on is None:
                led_status = ""
            else:
                led_status = f"{'ON' if node.led_on else 'OFF'} / {'ON' if node.led_breathe_on else 'OFF'}"

            nodes_status.append({
                'name': name,
                'ip': node.ip,
                'type': node.node_type,
                'essential': node.essential,
                'state': display_state,
                'connected': node.sock is not None,
                'connection_duration': connection_duration,
                'message_count': node.message_count,
                'heartbeat_count': node.heartbeat_count,
                'custom_data': custom_data,
                'notification': notification,
                'custom_html': custom_html,
                'log_on': node.log_on,
                'log_level': node.log_level,
                'log_status': log_status,
                'led_on': node.led_on,
                'led_breathe_on': node.led_breathe_on,
                'rssi': node.rssi if node.rssi is not None else '?'
            })

        return {
            'nodes': nodes_status,
            'total_nodes': len(self.nodes),
            'connected_nodes': len([n for n in self.nodes.values() if n.sock]),
            'essential_nodes': len([n for n in self.nodes.values() if n.essential]),
            'connected_essential_nodes': len([n for n in self.nodes.values() if n.sock and n.essential]),
            'working_essential_nodes': working_nodes,
            'initialised_nodes': len([n for n in self.nodes.values() if n.sock and n.state not in [ConnectionState.DISCONNECTED, ConnectionState.CONNECTED]]),
            'server_uptime': time.time() - self._start_time,
            'journal_enabled': HAS_SYSTEMD,
            'grid_columns': self.node_grid_layout.get('columns', 4),
            'grid_rows': self.node_grid_layout.get('rows', 2),
            'nodes_per_page': NODES_PER_PAGE,
            'log_level_names': LOG_LEVEL_NAMES
        }

    async def _broadcast_status(self):
        """Broadcast status updates to SSE clients"""
        last_status = None
        # Clear old notifications periodically
        last_cleanup = time.time()
        while self.web_running:
            # Clean up old notifications (older than 3 seconds)
            now = time.time()
            if now - last_cleanup > 1:
                expired = []
                for name, notif in self.node_notifications.items():
                    if now - notif['timestamp'] > 3:
                        expired.append(name)
                for name in expired:
                    del self.node_notifications[name]
                last_cleanup = now

            current_status = self._get_system_status()
            if current_status != last_status:
                last_status = current_status
                for client in list(self.sse_clients):
                    try:
                        await client.write(f"data: {json.dumps(current_status)}\n\n".encode())
                    except Exception:
                        self.sse_clients.discard(client)
            await asyncio.sleep(0.5)

    async def handle_index(self, request):
        """Serve the main HTML page"""
        return web.Response(text=self._get_html_template(), content_type='text/html')

    async def handle_api_status(self, request):
        """Return current system status as JSON"""
        return web.json_response(self._get_system_status())

    async def handle_api_status_stream(self, request):
        """SSE stream for status updates"""
        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            }
        )
        await response.prepare(request)
        self.sse_clients.add(response)

        try:
            while self.web_running:
                status = self._get_system_status()
                try:
                    await response.write(f"data: {json.dumps(status)}\n\n".encode())
                    await asyncio.sleep(2)
                except (ConnectionResetError, BrokenPipeError, RuntimeError):
                    # Client disconnected
                    break
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            # Client disconnected - normal
            pass
        except Exception as e:
            self._log_message(f"Status stream error: {e}")
        finally:
            self.sse_clients.discard(response)

        return response

    async def handle_api_logs(self, request):
        """Return recent logs"""
        return web.json_response({'logs': [msg for _, msg in self.log_entries]})

    async def handle_api_logs_clear(self, request):
        """Clear the log buffer"""
        self.log_entries.clear()
        self._log_counter = 0
        return web.json_response({'status': 'ok'})

    async def handle_api_logs_stream(self, request):
        """SSE stream for log updates - tracks per-client version"""
        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            }
        )
        await response.prepare(request)

        # Track the last version sent to this specific client
        last_version = -1

        # Send a reset signal
        try:
            await response.write(f"event: reset\ndata: reset\n\n".encode())
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            # Client disconnected before we even started
            return response

        try:
            while self.web_running:
                # Only send if we have new logs since last_version
                if self.log_entries and self.log_entries[-1][0] > last_version:
                    # Find all new logs
                    new_logs = []
                    for version, msg in self.log_entries:
                        if version > last_version:
                            new_logs.append(msg)
                            last_version = version

                    if new_logs:
                        try:
                            await response.write(f"data: {json.dumps(new_logs)}\n\n".encode())
                        except (ConnectionResetError, BrokenPipeError, RuntimeError):
                            # Client disconnected - exit cleanly
                            break

                await asyncio.sleep(0.5)
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            # Client disconnected - normal, don't log
            pass
        except Exception as e:
            self._log_message(f"Log stream error: {e}")

        return response

    async def handle_api_command(self, request):
        """Handle command requests to nodes"""
        data = await request.json()
        node_name = data.get('node')
        command = data.get('command')
        params = data.get('params', {})

        result = {'status': 'ok', 'message': ''}
        notification_msg = None

        try:
            if command == 'query_state':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    state_value = self.ping_node(node_name)
                    if state_value is not None:
                        try:
                            state_enum = fgr.FGRState(state_value)
                            state_name = format_fgr_state(state_enum)
                        except ValueError:
                            state_name = f"unknown ({state_value})"
                        result['message'] = f"Node {node_name} state: {state_name}"
                        self.set_node_notification(node_name, f"PONG: {state_name}", False, True)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"Failed to query {node_name} state"
                        self.set_node_notification(node_name, "PING failed", True, False)

            elif command == 'start':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                elif self.start_node(node_name):
                    result['message'] = f"Node {node_name} started"
                    self.set_node_notification(node_name, "START", True, True)
                else:
                    result['status'] = 'error'
                    result['message'] = f"Failed to start {node_name}"
                    self.set_node_notification(node_name, "START failed", True, False)

            elif command == 'stop':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                elif self.stop_node(node_name):
                    result['message'] = f"Node {node_name} stopped"
                    self.set_node_notification(node_name, "STOP", True, True)
                else:
                    result['status'] = 'error'
                    result['message'] = f"Failed to stop {node_name}"
                    self.set_node_notification(node_name, "STOP failed", True, False)

            elif command == 'reboot':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                elif self.reboot_node(node_name):
                    result['message'] = f"Node {node_name} rebooting"
                    self.set_node_notification(node_name, "REBOOT", True, True)
                else:
                    result['status'] = 'error'
                    result['message'] = f"Failed to reboot {node_name}"
                    self.set_node_notification(node_name, "REBOOT failed", True, False)

            elif command == 'log_start':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.start_logging():
                            result['message'] = f"Logging started on {node_name}"
                            self.set_node_notification(node_name, "LOG START", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to start logging on {node_name}"
                            self.set_node_notification(node_name, "LOG START failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            elif command == 'log_stop':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.stop_logging():
                            result['message'] = f"Logging stopped on {node_name}"
                            self.set_node_notification(node_name, "LOG STOP", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to stop logging on {node_name}"
                            self.set_node_notification(node_name, "LOG STOP failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            elif command == 'log_level':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    level = params.get('level', 1)
                    if level < 0 or level > 3:
                        level = 1
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.set_log_level(level):
                            result['message'] = f"Log level set to {LOG_LEVEL_NAMES[level]} on {node_name}"
                            self.set_node_notification(node_name, f"LOG LEVEL {LOG_LEVEL_NAMES[level]}", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to set log level on {node_name}"
                            self.set_node_notification(node_name, "LOG LEVEL failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            elif command == 'led_on':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.led_on():
                            result['message'] = f"LED enabled on {node_name}"
                            self.set_node_notification(node_name, "LED ON", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to enable LED on {node_name}"
                            self.set_node_notification(node_name, "LED ON failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            elif command == 'led_off':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.led_off():
                            result['message'] = f"LED disabled on {node_name}"
                            self.set_node_notification(node_name, "LED OFF", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to disable LED on {node_name}"
                            self.set_node_notification(node_name, "LED OFF failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            elif command == 'led_breathe_on':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.led_breathe_on():
                            result['message'] = f"LED breathe enabled on {node_name}"
                            self.set_node_notification(node_name, "LED BREATHE ON", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to enable LED breathe on {node_name}"
                            self.set_node_notification(node_name, "LED BREATHE ON failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            elif command == 'led_breathe_off':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    node = self.nodes.get(node_name)
                    if node and node.handler:
                        if node.handler.led_breathe_off():
                            result['message'] = f"LED breathe disabled on {node_name}"
                            self.set_node_notification(node_name, "LED BREATHE OFF", True, True)
                        else:
                            result['status'] = 'error'
                            result['message'] = f"Failed to disable LED breathe on {node_name}"
                            self.set_node_notification(node_name, "LED BREATHE OFF failed", True, False)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"No handler for {node_name}"
                        self.set_node_notification(node_name, "No handler", True, False)

            else:
                result['status'] = 'error'
                result['message'] = f"Unknown command: {command}"

        except Exception as e:
            result['status'] = 'error'
            result['message'] = str(e)
            self.set_node_notification(node_name, f"Command failed: {str(e)}", True, False)

        return web.json_response(result)

    async def handle_api_layout(self, request):
        """Save node grid layout"""
        data = await request.json()
        order = data.get('order', [])
        self.node_grid_layout['order'] = order
        self._save_node_grid_layout()
        return web.json_response({'status': 'ok'})

    async def handle_api_reorder(self, request):
        """Handle node reordering across pages"""
        data = await request.json()
        source_node = data.get('source')
        target_node = data.get('target')

        if source_node and target_node and source_node != target_node:
            order = self.node_grid_layout.get('order', [])

            if source_node in order and target_node in order:
                source_idx = order.index(source_node)
                target_idx = order.index(target_node)
                order.pop(source_idx)
                order.insert(target_idx, source_node)
                self.node_grid_layout['order'] = order
                self._save_node_grid_layout()
                return web.json_response({'status': 'ok', 'order': order})

        return web.json_response({'status': 'error', 'message': 'Invalid reorder operation'})

    async def handle_api_node_data(self, request):
        """Get dynamic data for a specific node (for updating expanded view)"""
        data = await request.json()
        node_name = data.get('node')

        node = self.nodes.get(node_name)
        if not node:
            return web.json_response({'status': 'error', 'message': 'Node not found'})

        # Calculate connection duration
        connection_duration = format_connection_duration(node)

        node_data = {
            'name': node_name,
            'type': node.node_type,
            'ip': node.ip,
            'state': node.state.name if hasattr(node.state, 'name') else str(node.state),
            'connected': node.sock is not None,
            'custom_data': self.node_custom_data.get(node_name, {}),
            'message_count': node.message_count,
            'heartbeat_count': node.heartbeat_count,
            'connection_duration': connection_duration,
            'log_on': node.log_on,
            'log_level': node.log_level,
            'led_on': node.led_on,
            'led_breathe_on': node.led_breathe_on,
            'rssi': node.rssi if node.rssi is not None else '?'
        }

        return web.json_response({'status': 'ok', 'data': node_data})

    async def handle_api_node_html(self, request):
        """Get expanded HTML for a specific node"""
        data = await request.json()
        node_name = data.get('node')

        node = self.nodes.get(node_name)
        if not node:
            return web.json_response({'status': 'error', 'message': 'Node not found'})

        # Get node-specific content from handler (this goes in the middle)
        node_specific_html = ""
        if node.handler and hasattr(node.handler, 'get_expanded_html'):
            try:
                node_data = {
                    'name': node_name,
                    'custom_data': self.node_custom_data.get(node_name, {}),
                }
                node_specific_html = node.handler.get_expanded_html(node_name, node_data)
            except Exception as e:
                self._log_message(f"Error getting expanded HTML from handler: {e}")
                node_specific_html = '<div class="expanded-section"><h4>Error</h4><pre>Failed to load node data</pre></div>'
        else:
            node_specific_html = f'''
                <div class="expanded-section">
                    <h4>Node Data</h4>
                    <pre>{json.dumps(self.node_custom_data.get(node_name, {}), indent=2)}</pre>
                </div>
            '''

        # Determine online state for display
        is_online = node.sock is not None
        state_text = node.state.name if hasattr(node.state, 'name') else str(node.state)
        if state_text.startswith('FGR_'):
            state_text = state_text[4:].replace('_', ' ').lower()
        else:
            state_text = state_text.lower()

        # Handle None values safely for display
        log_level_names = ['DEBUG', 'INFO', 'WARN', 'ERROR']

        if node.log_on is None:
            log_on_str = "?"
            log_level_str = "?"
        else:
            log_on_str = "ON" if node.log_on else "OFF"
            log_level_str = log_level_names[node.log_level] if node.log_level is not None and node.log_level < 4 else "?"

        log_status_text = f"{log_on_str}/{log_level_str}"

        # LED status with question marks for unknown
        if node.led_on is None:
            led_status_text = "?"
        else:
            led_status_text = "ON" if node.led_on else "OFF"

        if node.led_breathe_on is None:
            breathe_status_text = "?"
        else:
            breathe_status_text = "ON" if node.led_breathe_on else "OFF"

        # Combined debug status text
        debug_status_text = f"LED: {led_status_text} / Breathe: {breathe_status_text}"

        # Format connection duration for display
        connection_duration = format_connection_duration(node)

        # Build the complete expanded footer with all rows
        expanded_footer_html = f'''
            <div class="expanded-footer">
                <!-- Row 1: Status line (state + notification) -->
                <div class="node-status-line">
                    <div class="node-state {'online' if is_online else 'offline'}">
                        <span class="state-text">{'● online' if is_online else '○ offline'} - {state_text}</span>
                    </div>
                    <div class="notification-placeholder"></div>
                </div>

                <!-- Row 2: Metrics line (connection duration + RSSI + message/heartbeat) -->
                <div class="node-metrics">
                    <span class="connection-duration">{connection_duration if connection_duration else ''}</span>
                    <div class="rssi-display" style="display: inline-flex; align-items: center; gap: 3px;">
                        <span class="rssi-icon">📶</span>
                            <span class="rssi-value" data-dynamic="rssi">{node.rssi if node.rssi is not None else "?"} dBm</span>
                    </div>
                    <span>📨 {node.message_count} 💓 {node.heartbeat_count}</span>
                </div>

                <!-- Row 3: Node actions (Ping, Start, Stop, Reboot) -->
                <div class="node-actions">
                    <button class="btn-query" onclick="sendCommand('{node_name}', 'query_state')">Ping</button>
                    <button class="btn-start" onclick="sendCommand('{node_name}', 'start')">Start</button>
                    <button class="btn-stop" onclick="sendCommand('{node_name}', 'stop')">Stop</button>
                    <button class="btn-reboot" onclick="sendCommand('{node_name}', 'reboot')">Reboot</button>
                </div>

                <!-- Row 4: Two-column layout (replaces original logging row) -->
                <div style="display: flex; gap: 10px; margin-top: 3px;">
                    <!-- Left column: Log Controls -->
                    <div style="flex: 1;">
                        <div class="expanded-footer-row-log">
                            <button class="btn-log-start" onclick="sendCommand('{node_name}', 'log_start')">Log On</button>
                            <button class="btn-log-stop" onclick="sendCommand('{node_name}', 'log_stop')">Log Off</button>
                            <select class="expanded-log-level-select" data-node-name="{node_name}">
                                <option value="0">DEBUG</option>
                                <option value="1" selected>INFO</option>
                                <option value="2">WARN</option>
                                <option value="3">ERROR</option>
                            </select>
                            <button class="btn-apply-level" onclick="sendCommand('{node_name}', 'log_level', {{level: parseInt(this.previousElementSibling.value)}})">Apply</button>
                            <div class="expanded-footer-status" data-dynamic="log_status">{log_status_text}</div>
                        </div>
                    </div>

                    <!-- Right column: Debug Controls -->
                    <div style="flex: 1;">
                        <div class="expanded-footer-row-debug">
                            <button class="btn-led-on" onclick="sendCommand('{node_name}', 'led_on')">LED On</button>
                            <button class="btn-led-off" onclick="sendCommand('{node_name}', 'led_off')">LED Off</button>
                            <button class="btn-breathe-on" onclick="sendCommand('{node_name}', 'led_breathe_on')">Breathe On</button>
                            <button class="btn-breathe-off" onclick="sendCommand('{node_name}', 'led_breathe_off')">Breathe Off</button>
                            <div class="expanded-footer-status" data-dynamic="debug_status">{debug_status_text}</div>
                        </div>
                    </div>
                </div>
            </div>
        '''

        # Build complete expanded view - node content in middle, footer at bottom
        complete_html = f'''
            <div class="expanded-node">
                <div class="expanded-header">
                    <h3>{node_name} <span class="expanded-nav-hint">← → use arrow keys or page buttons</span></h3>
                    <button class="collapse-btn">✕ Collapse</button>
                </div>
                <div class="expanded-content">
                    {node_specific_html}
                </div>
                {expanded_footer_html}
            </div>
        '''

        return web.json_response({'status': 'ok', 'html': complete_html})

    def start_web(self):
        """Start the web server"""
        self.web_app = web.Application()
        self.web_app.router.add_get('/', self.handle_index)
        self.web_app.router.add_get('/api/status', self.handle_api_status)
        self.web_app.router.add_get('/api/status/stream', self.handle_api_status_stream)
        self.web_app.router.add_get('/api/logs', self.handle_api_logs)
        self.web_app.router.add_get('/api/logs/stream', self.handle_api_logs_stream)
        self.web_app.router.add_post('/api/logs/clear', self.handle_api_logs_clear)
        self.web_app.router.add_post('/api/command', self.handle_api_command)
        self.web_app.router.add_post('/api/layout', self.handle_api_layout)
        self.web_app.router.add_post('/api/reorder', self.handle_api_reorder)
        self.web_app.router.add_post('/api/node/data', self.handle_api_node_data)
        self.web_app.router.add_post('/api/node/html', self.handle_api_node_html)

        # Event to signal the web thread to stop
        self.web_stop_event = threading.Event()

        async def start_server():
            self.web_runner = web.AppRunner(self.web_app)
            await self.web_runner.setup()
            site = web.TCPSite(self.web_runner, '0.0.0.0', self.http_port)
            await site.start()
            self.web_running = True
            print(f"Web interface running at http://0.0.0.0:{self.http_port}")
            # Wait for stop signal
            while self.web_running and not self.web_stop_event.is_set():
                await asyncio.sleep(0.5)
            # Cleanup in the same loop
            await self.web_runner.cleanup()
            print("Web server stopped")

        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(start_server())
            loop.close()

        self.web_thread = threading.Thread(target=run_loop, daemon=True)
        self.web_thread.start()

    def stop_web(self):
        """Stop the web server"""
        self.web_running = False
        # Signal the web thread to stop
        if hasattr(self, 'web_stop_event'):
            self.web_stop_event.set()
        # Wait for the web thread to finish (with timeout)
        if hasattr(self, 'web_thread') and self.web_thread.is_alive():
            self.web_thread.join(timeout=3.0)

    def start(self) -> bool:
        """Start the controller and web server"""
        # Make sure connection_time is tracked
        if not hasattr(Node, 'connection_time'):
            Node.connection_time = 0.0
        if not hasattr(Node, 'last_seen'):
            Node.last_seen = 0.0

        if not super().start():
            return False
        self.start_web()
        return True

    def stop(self) -> None:
        """Stop the controller and web server"""
        self._stop_journal_reader()
        self.stop_web()
        super().stop()

    def _get_html_template(self) -> str:
        """Return the HTML template"""
        return '''<!DOCTYPE html>
<html>
<head>
    <title>FGR Controller</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        :root {
            --card-height: 260px;      /* Slightly taller to accommodate RSSI */
            --grid-gap: 12px;          /* Gap between cards */
            --grid-rows: 2;            /* Number of rows per page */
            --grid-columns: 4;         /* Number of columns per page */
        }

        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1600px;
            margin: 0 auto;
            padding: 4px 12px 12px 12px;
            background: #f5f5f5;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
        }
        h1 {
            margin: 0;
            font-size: 20px;
            color: #333;
            line-height: 1.2;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            flex-wrap: wrap;
            gap: 6px;
            flex-shrink: 0;
        }
        .title-section h1 { margin: 0; }
        .title-section .subtitle { margin: 0; font-size: 11px; color: #666; }

        .status-banner {
            background: #fff3e0;
            border-left: 4px solid #ffc107;
            padding: 2px 8px;
            border-radius: 6px;
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 11px;
        }
        .status-banner.ready { background: #e8f5e9; border-left-color: #2e7d32; }
        .status-banner.waiting { background: #fff3e0; border-left-color: #ffc107; }
        .status-icon { font-size: 14px; }
        .status-text { flex: 1; white-space: nowrap; }
        .status-details { font-size: 9px; color: #666; margin-top: 1px; }

        /* Main grid wrapper with margin buttons */
        .grid-wrapper {
            position: relative;
            margin: 0 50px 10px 50px;
            flex-shrink: 0;
            display: flex;
            flex-direction: column;
        }

        /* Grid container - uses CSS variable for height calculation */
        .grid-container {
            position: relative;
            margin-bottom: 2px;
            margin-top: 0;
            height: calc(var(--card-height) * var(--grid-rows) + var(--grid-gap));
            overflow-y: visible;
            min-height: auto;
        }

        /* Make sure the page has enough scroll space */
        html {
            overflow-y: auto;
        }

        .node-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(280px, 1fr));
            gap: var(--grid-gap);
            overflow-x: auto;
            height: 100%;
            align-content: start;
            transition: all 0.3s ease;
            margin-top: 0;
        }

        /* Expanded card mode - single card takes full width */
        .node-grid.expanded {
            display: block;
            position: relative;
        }

        .node-grid.expanded .node-card:not(.expanded-card) {
            visibility: hidden;
            position: absolute;
        }

        .node-grid.expanded .node-card.expanded-card {
            display: flex;
            width: 100%;
            height: calc(var(--card-height) * var(--grid-rows) + var(--grid-gap) - 10px);
            min-height: auto;
            max-height: none;
            overflow-y: auto;
            position: relative;
            z-index: 10;
        }

        /* When card is expanded, hide the original footer */
        .node-card.expanded-card .node-footer {
            display: none;
        }

        /* Expanded footer - shown only when expanded */
        .node-card.expanded-card .expanded-footer {
            display: block;
            margin-top: auto;
            flex-shrink: 0;
        }

        .node-grid.has-more {
            grid-template-columns: repeat(auto-fill, minmax(280px, 320px));
        }

        .node-card {
            background: white;
            border-radius: 8px;
            padding: 10px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            transition: transform 0.1s, box-shadow 0.2s;
            -webkit-user-select: none;
            -moz-user-select: none;
            -ms-user-select: none;
            user-select: none;
            height: var(--card-height);
            display: flex;
            flex-direction: column;
            overflow: hidden;
            cursor: pointer;
        }

        .node-card.expanded-card {
            cursor: default;
            height: calc(var(--card-height) * var(--grid-rows) + var(--grid-gap) - 10px);
            min-height: auto;
            max-height: none;
            overflow-y: auto;
            position: relative;
            z-index: 10;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: flex;
            flex-direction: column;
        }

        /* Hide the expand hint on expanded cards */
        .node-card.expanded-card .expand-hint {
            display: none;
        }

        .node-card * {
            user-select: text;
        }

        /* Expand hint - appears on hover */
        .expand-hint {
            position: absolute;
            top: 8px;
            right: 8px;
            font-size: 12px;
            color: #999;
            opacity: 0;
            transition: opacity 0.2s;
            pointer-events: none;
        }

        .node-card:hover .expand-hint {
            opacity: 0.7;
        }

        /* Make drag handle not selectable */
        .drag-handle {
            user-select: none;
            cursor: grab;
        }
        .drag-handle:active {
            cursor: grabbing;
        }

        /* Essential vs Non-essential node styling */
        .node-card.non-essential {
            opacity: 0.85;
            border: 1px dashed #ccc;
        }
        .node-card.non-essential .node-name {
            color: #666;
        }
        .node-card.essential {
            border: 1px solid #e0e0e0;
            border-left: 4px solid #4caf50;
        }
        .node-card.essential .node-name {
            color: #333;
            font-weight: bold;
        }
        .node-card.online {
            border-left: 4px solid #4caf50;
        }
        .node-card.offline {
            opacity: 0.7;
        }
        .node-card.essential.online {
            border-left: 4px solid #4caf50;
        }
        .node-card.drag-over {
            border: 2px solid #4caf50;
            background: #f0fff0;
            transform: scale(1.01);
            transition: all 0.1s ease;
        }

        .drag-handle {
            color: #999;
            font-size: 14px;
            padding: 2px 4px;
            margin-right: 2px;
            display: inline-block;
            transition: color 0.2s;
        }
        .drag-handle:hover {
            color: #666;
        }

        /* Header section: node name and type */
        .node-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
            flex-shrink: 0;
        }
        .node-title {
            display: flex;
            align-items: center;
            flex: 1;
        }
        .node-name {
            font-size: 13px;
            font-weight: bold;
            color: #333;
        }
        .node-type {
            font-size: 8px;
            color: #888;
            background: #f0f0f0;
            padding: 1px 4px;
            border-radius: 20px;
            white-space: nowrap;
        }

        /* IP address inline with type */
        .node-ip {
            font-family: monospace;
            font-size: 8px;
            color: #666;
            margin-left: 4px;
            display: inline-block;
        }

        /* Fixed footer area (status, metrics, buttons) */
        .node-footer {
            margin-top: auto;
            flex-shrink: 0;
        }

        /* Status line with state and notification */
        .node-status-line {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
            font-size: 10px;
            min-height: 18px;
        }
        .node-state {
            display: inline-flex;
            align-items: center;
        }
        .node-state.online { color: #4caf50; }
        .node-state.offline { color: #f44336; }
        .state-text { text-transform: capitalize; }
        .rssi-display {
            display: inline-flex;
            align-items: center;
            gap: 3px;
            font-size: 9px;
            color: #666;
        }
        .rssi-icon { font-size: 10px; }
        .rssi-value { font-family: monospace; }
        .notification {
            font-size: 8px;
            padding: 1px 4px;
            border-radius: 12px;
            animation: fadeOut 3s forwards;
            background: #2196f3;
            color: white;
            white-space: nowrap;
            margin-left: 6px;
        }
        .notification.success { background: #4caf50; }
        .notification.failure { background: #f44336; }
        @keyframes fadeOut {
            0% { opacity: 1; }
            70% { opacity: 1; }
            100% { opacity: 0; display: none; }
        }

        /* Metrics line */
        .node-metrics {
            font-size: 8px;
            color: #888;
            margin-bottom: 6px;
            border-top: 1px solid #eee;
            padding-top: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        /* Custom area - gets maximum space, supports custom HTML */
        .node-custom-container {
            flex-grow: 1;
            margin: 4px 0;
            overflow-y: auto;
            min-height: 40px;
            -webkit-user-select: text;
            -moz-user-select: text;
            -ms-user-select: text;
            user-select: text;
        }

        /* Default custom styling (used when no custom HTML provided) */
        .node-custom {
            background: #e3f2fd;
            border-radius: 4px;
            padding: 6px;
            text-align: center;
            height: 100%;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }
        .custom-value {
            font-size: 20px;
            font-weight: bold;
            color: #1976d2;
            line-height: 1.2;
        }
        .custom-unit {
            font-size: 9px;
            color: #666;
            margin-top: 2px;
        }

        /* Button grid */
        .node-actions {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 4px;
            margin-top: 4px;
            margin-bottom: 3px;
        }
        .node-actions-group {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 4px;
            margin-top: 3px;
        }
        button, .log-level-select {
            padding: 3px 2px;
            font-size: 8px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
            width: 100%;
        }
        button:hover { opacity: 0.8; transform: translateY(-1px); }
        .btn-query { background: #2196f3; color: white; }
        .btn-start { background: #4caf50; color: white; }
        .btn-stop { background: #ff9800; color: white; }
        .btn-reboot { background: #f44336; color: white; }
        .btn-log-start { background: #009688; color: white; }
        .btn-log-stop { background: #607d8b; color: white; }
        .btn-led-on, .btn-breathe-on { background: #4caf50; color: white; }
        .btn-led-off, .btn-breathe-off { background: #f44336; color: white; }
        .log-level-select {
            background: #e0e0e0;
            cursor: pointer;
            font-size: 7px;
        }
        .log-status-text {
            font-size: 7px;
            background: #e8f5e9;
            padding: 3px 2px;
            border-radius: 3px;
            text-align: center;
            color: #2e7d32;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        /* Expanded view styles */
        .expanded-node {
            padding: 5px;
            display: flex;
            flex-direction: column;
            height: 100%;
            flex: 1;
            -webkit-user-select: text;
            -moz-user-select: text;
            -ms-user-select: text;
            user-select: text;
        }

        .expanded-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 8px;
            border-bottom: 1px solid #e0e0e0;
            flex-shrink: 0;
        }

        .expanded-header h3 {
            margin: 0;
            font-size: 16px;
            color: #333;
        }

        .collapse-btn {
            background: #f0f0f0;
            color: #666;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 4px 12px;
            cursor: pointer;
            font-size: 11px;
            transition: all 0.2s;
            width: auto;
            min-width: 60px;
            white-space: nowrap;
        }

        .collapse-btn:hover {
            background: #e0e0e0;
            color: #333;
            border-color: #ccc;
        }

        .expanded-content {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 15px;
            flex: 1;
            overflow-y: auto;
            margin-bottom: 10px;
        }

        .expanded-nav-hint {
            font-size: 10px;
            color: #999;
            margin-left: 10px;
            font-weight: normal;
        }

        .expanded-section {
            background: #f8f9fa;
            border-radius: 6px;
            padding: 10px;
            height: fit-content;
        }

        .expanded-section h4 {
            margin: 0 0 8px 0;
            font-size: 13px;
            color: #495057;
            border-bottom: 1px solid #dee2e6;
            padding-bottom: 4px;
        }

        .expanded-section p {
            margin: 6px 0;
            font-size: 11px;
        }

        .expanded-section pre {
            background: #fff;
            padding: 6px;
            border-radius: 4px;
            overflow-x: auto;
            font-size: 9px;
            margin: 0;
        }

        /* Expanded footer button rows */
        .expanded-footer-row-log {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 4px;
            margin-top: 3px;
        }

        .expanded-footer-row-debug {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 4px;
            margin-top: 3px;
        }

        .expanded-footer-row-log button,
        .expanded-footer-row-debug button,
        .expanded-footer-row-log select,
        .expanded-footer-row-debug select {
            padding: 3px 2px;
            font-size: 8px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
            width: 100%;
        }

        .expanded-footer-row-log button:hover,
        .expanded-footer-row-debug button:hover {
            opacity: 0.8;
            transform: translateY(-1px);
        }

        .expanded-footer-status {
            font-size: 7px;
            background: #e8f5e9;
            padding: 3px 2px;
            border-radius: 3px;
            text-align: center;
            color: #2e7d32;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .expanded-log-level-select {
            background: #e0e0e0;
            border: none;
            border-radius: 3px;
            padding: 3px 2px;
            font-size: 7px;
            cursor: pointer;
            text-align: center;
            width: 100%;
        }

        /* Debug panel - fixed position with margins */
        .debug-panel {
            background: white;
            border-radius: 8px;
            box-shadow: 0 -2px 10px rgba(0,0,0,0.15);
            position: fixed;
            bottom: 10px;
            left: 10px;
            right: 10px;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            transition: height 0.1s ease;
            border: 1px solid #ddd;
            min-height: 40px;
        }

        /* Resize handle at the top edge of the panel */
        .debug-panel-resize-handle {
            position: absolute;
            top: -6px;
            left: 20px;
            right: 20px;
            height: 12px;
            cursor: ns-resize;
            background: #007bff;
            border-radius: 6px 6px 0 0;
            z-index: 1001;
            opacity: 0.6;
            transition: opacity 0.2s;
        }

        .debug-panel-resize-handle:hover {
            opacity: 1;
            background: #0056b3;
        }

        .debug-panel-resize-handle:active {
            opacity: 1;
            background: #004099;
        }

        /* Collapsed state */
        .debug-panel.collapsed {
            height: 42px !important;
            min-height: 42px;
            cursor: pointer;
        }

        .debug-panel.collapsed .debug-header {
            cursor: pointer;
            border-radius: 6px;
        }

        .debug-panel.collapsed .debug-window,
        .debug-panel.collapsed .debug-buttons {
            display: none;
        }

        .debug-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: nowrap;
            gap: 12px;
            padding: 8px 15px;
            background: #f0f0f0;
            border-radius: 6px 6px 0 0;
            flex-shrink: 0;
            user-select: none;
            border-bottom: 1px solid #ddd;
        }

        .debug-header h2 {
            margin: 0;
            font-size: 13px;
            color: #333;
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
            white-space: nowrap;
        }

        /* Debug filters styling */
        .debug-filters {
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
            font-size: 10px;
            flex: 1;
        }

        /* Prevent filter inputs from triggering panel collapse/expand */
        .debug-filters input,
        .debug-filters label {
            position: relative;
            z-index: 1002;
            cursor: default;
        }

        .filter-textbox {
            cursor: text;
        }

        .filter-checkbox {
            cursor: pointer;
        }

        .filter-checkbox {
            display: flex;
            align-items: center;
            gap: 4px;
            cursor: pointer;
            white-space: nowrap;
            color: #333;
        }

        .filter-checkbox input {
            cursor: pointer;
            margin: 0;
        }

        .filter-input-group {
            display: flex;
            align-items: center;
            gap: 4px;
            background: #e9ecef;
            padding: 2px 8px;
            border-radius: 4px;
        }

        .filter-label {
            font-size: 9px;
            color: #495057;
            white-space: nowrap;
        }

        .filter-textbox {
            border: 1px solid #ced4da;
            border-radius: 3px;
            padding: 2px 4px;
            font-size: 9px;
            width: 90px;
            background: white;
        }

        .filter-textbox:focus {
            outline: none;
            border-color: #007bff;
        }

        .filter-select {
            border: 1px solid #ced4da;
            border-radius: 3px;
            padding: 2px 4px;
            font-size: 9px;
            background: white;
            cursor: pointer;
        }

        .filter-select:focus {
            outline: none;
            border-color: #007bff;
        }

        .log-highlight {
            color: #ffd966 !important;
            font-weight: bold;
        }

        .debug-buttons {
            display: flex;
            gap: 6px;
            flex-shrink: 0;
        }

        .debug-buttons button {
            padding: 4px 8px;
            font-size: 10px;
            white-space: nowrap;
        }

        /* Dock button */
        .debug-dock-btn {
            background: #6c757d;
            color: white;
            border: none;
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 10px;
            cursor: pointer;
        }

        .debug-dock-btn:hover {
            background: #5a6268;
        }

        /* Toggle button */
        .debug-toggle-btn {
            background: #007bff;
            color: white;
            border: none;
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 10px;
            cursor: pointer;
        }

        .debug-toggle-btn:hover {
            background: #0056b3;
        }

        /* Debug window log area */
        .debug-window {
            background: #1e1e1e;
            color: #d4d4d4;
            font-family: 'Courier New', 'SF Mono', 'Monaco', 'Menlo', monospace;
            font-size: 11px;
            padding: 10px;
            flex: 1;
            overflow-y: auto;
            min-height: 100px;
            line-height: 1.4;
            border-radius: 0 0 6px 6px;
        }

        .debug-window .log-ctrl {
            color: #4ec9b0;
        }

        .debug-window .log-node {
            color: #9cdcfe;
        }

        .footer {
            text-align: center;
            color: #999;
            font-size: 8px;
            margin-top: 4px;
            margin-bottom: 80px;
            flex-shrink: 0;
        }

        .badge-success { background: #4caf50; color: white; padding: 2px 5px; border-radius: 4px; font-size: 8px; }
        .badge-warning { background: #ff9800; color: white; padding: 2px 5px; border-radius: 4px; font-size: 8px; }

        /* Page indicator styling */
        .page-indicator {
            text-align: center;
            margin-top: 6px;
            font-size: 11px;
            color: #888;
        }

        /* Navigation buttons */
        .nav-btn {
            position: absolute;
            top: 50%;
            transform: translateY(-50%);
            background: #007bff;
            color: white;
            border: none;
            border-radius: 50%;
            width: 40px;
            height: 40px;
            cursor: pointer;
            transition: all 0.2s;
            display: flex;
            align-items: center;
            justify-content: center;
            box-shadow: 0 2px 6px rgba(0,0,0,0.2);
            z-index: 10;
            font-size: 18px;
            font-weight: bold;
            padding: 0;
        }

        .nav-btn span {
            display: inline-block;
            line-height: 1;
        }

        .nav-prev {
            left: -50px;
        }

        .nav-next {
            right: -50px;
        }

        .nav-btn:hover:not(:disabled) {
            background: #0056b3;
            transform: translateY(-50%) scale(1.05);
        }

        .nav-btn:disabled {
            background: #cccccc;
            cursor: not-allowed;
            opacity: 0.5;
            transform: translateY(-50%);
        }

        .scroll-hint {
            text-align: center;
            font-size: 9px;
            color: #999;
            margin-top: 2px;
            display: none;
        }
        .scroll-hint.show { display: block; }


        /* 5-column layout for logging row with Apply button */
        .node-actions-group-5col {
            display: grid;
            grid-template-columns: repeat(5, 1fr);
            gap: 4px;
            margin-top: 3px;
        }

        /* Style for Apply button */
        .btn-apply-level {
            background: #17a2b8;
            color: white;
            padding: 3px 2px;
            font-size: 7px;
            border: none;
            border-radius: 3px;
            cursor: pointer;
            transition: all 0.2s;
            text-align: center;
            width: 100%;
        }

        .btn-apply-level:hover {
            opacity: 0.8;
            transform: translateY(-1px);
        }

        @media (max-width: 1200px) {
            .node-grid { grid-template-columns: repeat(4, minmax(260px, 1fr)); }
            .grid-wrapper { margin: 0 40px; }
            .nav-prev { left: -40px; }
            .nav-next { right: -40px; }
        }
        @media (max-width: 1000px) {
            .node-grid { overflow-x: auto; grid-template-columns: repeat(4, 280px); }
            .scroll-hint.show { display: block; }
        }
        @media (max-width: 768px) {
            .grid-wrapper { margin: 0 35px; }
            .nav-prev { left: -35px; width: 32px; height: 32px; }
            .nav-next { right: -35px; width: 32px; height: 32px; }
            .nav-prev::before, .nav-next::before { font-size: 14px; }
        }

        /* Override collapsed behavior - prevent hiding content */
        .debug-panel.collapsed .debug-window {
            display: flex !important;
            visibility: visible !important;
            opacity: 1 !important;
            height: auto !important;
            min-height: 100px !important;
        }

        /* Also ensure log entries are visible */
        .debug-window > div {
            display: block !important;
            height: auto !important;
            min-height: auto !important;
        }

    </style>
</head>
<body>
    <div class="header">
        <div class="title-section">
            <h1>🚂 FGR Controller</h1>
            <div class="subtitle">Front Garden Railway - Node Monitoring & Control</div>
        </div>
        <div id="statusBanner" class="status-banner waiting">
            <div class="status-icon">⏳</div>
            <div class="status-text">
                <div>Initializing...</div>
                <div class="status-details">Waiting for nodes</div>
            </div>
        </div>
    </div>

    <div class="grid-wrapper">
        <button id="prevPageBtn" class="nav-btn nav-prev" onclick="previousPage()" disabled><span>◀</span></button>
        <button id="nextPageBtn" class="nav-btn nav-next" onclick="nextPage()" disabled><span>▶</span></button>
        <div class="grid-container">
            <div id="nodeGrid" class="node-grid">
                <div style="text-align: center; grid-column: 1/-1; padding: 40px;">Loading nodes...</div>
            </div>
            <div id="scrollHint" class="scroll-hint">← → Scroll for more nodes → ←</div>
        </div>

        <div id="pageIndicator" class="page-indicator">Page <span id="currentPageNum">1</span> / <span id="totalPagesNum">1</span></div>
    </div>

    <div class="debug-panel">
        <div class="debug-header">
            <h2>🐛 Debug Output <span id="journalBadge" class="badge-warning">loading...</span></h2>
            <div class="debug-filters">
                <label class="filter-checkbox">
                    <input type="checkbox" id="filterExcludeCtrl"> Exclude CTRL
                </label>
                <div class="filter-input-group">
                    <span class="filter-label" id="filterIncludeLabel">Include only NODEs</span>
                    <input type="text" id="filterIncludeNodes" placeholder="e.g., 2,3" class="filter-textbox">
                </div>
                <div class="filter-input-group">
                    <span class="filter-label" id="filterHighlightLabel">Highlight NODEs</span>
                    <input type="text" id="filterHighlightNodes" placeholder="e.g., 2,3" class="filter-textbox">
                </div>
                <div class="filter-input-group">
                    <span class="filter-label">Min NODE log level</span>
                    <select id="filterMinLogLevel" class="filter-select">
                        <option value="0">DEBUG</option>
                        <option value="1">INFO</option>
                        <option value="2">WARN</option>
                        <option value="3">ERROR</option>
                        <option value="4" selected>No filter</option>
                    </select>
                </div>
            </div>
            <div class="debug-buttons">
                <button onclick="selectAllLogs()" style="background:#17a2b8;color:white;">📋 Select All</button>
                <button onclick="copyLogsToClipboard(event)" style="background:#28a745;color:white;">📋 Copy</button>
                <button onclick="clearLogs()" style="background:#6c757d;color:white;">🗑️ Clear</button>
            </div>
        </div>
        <div id="debugWindow" class="debug-window">Waiting for logs...</div>
    </div>

    <div class="footer">FGR Controller - Drag ⋮⋮ to reorder nodes | Double-click card to expand | Drag blue bar above debug panel to resize | 📌 Dock returns to default size | Click header to collapse/expand</div>

    <script>
        let statusSource = null;
        let logsSource = null;
        let autoScrollEnabled = true;
        let logBuffer = [];
        let logStreamActive = true;
        let scrollTimeout = null;
        let nodeOrder = [];
        let nodesData = {};  // Store node data for reference
        let gridBuilt = false;  // Track if grid has been built
        let lastNotificationTimestamp = {};  // Track last notification timestamp per node
        let isExpanded = false;  // Track if a card is expanded
        let expandedNodeName = null;  // Track which node is expanded
        let expandedRefreshInterval = null;  // Refresh dynamic data for expanded view
        let debugPanelCollapsed = false;  // Track debug panel collapsed state

        // Pagination variables
        let currentPage = 0;
        let totalPages = 1;
        let allNodes = [];  // Store all nodes in order
        let nodesPerPage = 8;  // 2 rows x 4 columns

        // Drag auto-scroll for pagination
        let dragAutoScrollTimer = null;
        let isDragging = false;
        let dragSourceNode = null;

        // Log level names
        const logLevelNames = ['DEBUG', 'INFO', 'WARN', 'ERROR'];

        // Set CSS variables from server data if needed
        function updateCSSVariables(status) {
            if (status.grid_rows) {
                document.documentElement.style.setProperty('--grid-rows', status.grid_rows);
            }
            if (status.grid_columns) {
                document.documentElement.style.setProperty('--grid-columns', status.grid_columns);
            }
            const rows = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-rows')) || 2;
            const cols = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-columns')) || 4;
            nodesPerPage = rows * cols;
        }

        // Expand a card to full view
        async function expandCard(nodeName) {
            // Clear any existing text selection
            if (window.getSelection) {
                window.getSelection().removeAllRanges();
            } else if (document.selection) {
                document.selection.empty();
            }

            // If already expanded and trying to expand the same card, collapse it
            if (isExpanded && expandedNodeName === nodeName) {
                collapseCard();
                return;
            }

            // If already expanded but different node, just swap the content
            if (isExpanded && expandedNodeName !== nodeName) {
                // Swap to new node without changing grid layout
                const currentCard = document.querySelector('.node-card.expanded-card');
                if (currentCard) {
                    const customContainer = currentCard.querySelector('.node-custom-container');

                    // Show loading indicator
                    customContainer.innerHTML = '<div style="text-align:center; padding:20px;">Loading expanded view for ' + nodeName + '...</div>';

                    try {
                        const response = await fetch('/api/node/html', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({node: nodeName})
                        });
                        const result = await response.json();

                        if (result.status === 'ok' && result.html) {
                            customContainer.innerHTML = result.html;

                            // Add collapse button handler
                            const collapseBtn = customContainer.querySelector('.collapse-btn');
                            if (collapseBtn) {
                                collapseBtn.onclick = (e) => {
                                    e.stopPropagation();
                                    collapseCard();
                                };
                            } else {
                                // If no collapse button found, add one to the header
                                const expandedHeader = customContainer.querySelector('.expanded-header');
                                if (expandedHeader && !expandedHeader.querySelector('.collapse-btn')) {
                                    const missingCollapseBtn = document.createElement('button');
                                    missingCollapseBtn.className = 'collapse-btn';
                                    missingCollapseBtn.textContent = '✕ Collapse';
                                    missingCollapseBtn.onclick = (e) => {
                                        e.stopPropagation();
                                        collapseCard();
                                    };
                                    expandedHeader.appendChild(missingCollapseBtn);
                                }
                            }

                            // Add navigation hint if not present
                            const expandedHeader = customContainer.querySelector('.expanded-header h3');
                            if (expandedHeader && !customContainer.querySelector('.expanded-nav-hint')) {
                                const navHint = document.createElement('span');
                                navHint.className = 'expanded-nav-hint';
                                navHint.textContent = ' ← → use arrow keys or page buttons';
                                navHint.style.fontSize = '10px';
                                navHint.style.color = '#999';
                                navHint.style.fontWeight = 'normal';
                                navHint.style.marginLeft = '10px';
                                expandedHeader.appendChild(navHint);
                            }

                            // Update expanded node name
                            expandedNodeName = nodeName;
                            currentCard.setAttribute('data-node-name', nodeName);

                            // Update pagination controls for the new node position
                            updatePaginationControls();

                            // Start refreshing dynamic data for the new node
                            startExpandedRefresh(nodeName);
                        } else {
                            console.error('Invalid API response:', result);
                            customContainer.innerHTML = `
                                <div class="expanded-node">
                                    <div class="expanded-header">
                                        <h3>${nodeName}</h3>
                                        <button class="collapse-btn">✕ Collapse</button>
                                    </div>
                                    <div class="expanded-content">
                                        <div class="expanded-section">
                                            <h4>❌ Failed to Load Data</h4>
                                            <p>${result.message || 'Unknown error'}</p>
                                            <p>The node may be disconnected or unavailable.</p>
                                        </div>
                                    </div>
                                </div>
                            `;
                            const collapseBtn = customContainer.querySelector('.collapse-btn');
                            if (collapseBtn) {
                                collapseBtn.onclick = (e) => {
                                    e.stopPropagation();
                                    collapseCard();
                                };
                            }
                            expandedNodeName = nodeName;
                            currentCard.setAttribute('data-node-name', nodeName);
                            updatePaginationControls();
                        }
                    } catch (e) {
                        console.error('Error loading expanded view:', e);
                        customContainer.innerHTML = `
                            <div class="expanded-node">
                                <div class="expanded-header">
                                    <h3>${nodeName}</h3>
                                    <button class="collapse-btn">✕ Collapse</button>
                                </div>
                                <div class="expanded-content">
                                    <div class="expanded-section">
                                        <h4>❌ Network Error</h4>
                                        <p>${e.message}</p>
                                        <p>Please check your connection to the controller.</p>
                                    </div>
                                </div>
                            </div>
                        `;
                        const collapseBtn = customContainer.querySelector('.collapse-btn');
                        if (collapseBtn) {
                            collapseBtn.onclick = (e) => {
                                e.stopPropagation();
                                collapseCard();
                            };
                        }
                    }
                }
                return;
            }

            const card = document.querySelector(`.node-card[data-node-name="${nodeName}"]`);
            if (!card) {
                console.error('Card not found for node:', nodeName);
                return;
            }

            const customContainer = card.querySelector('.node-custom-container');
            const originalHtml = customContainer.innerHTML;
            card.setAttribute('data-original-html', originalHtml);

            customContainer.innerHTML = '<div style="text-align:center; padding:20px;">Loading expanded view...</div>';

            try {
                const response = await fetch('/api/node/html', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({node: nodeName})
                });
                const result = await response.json();

                if (result.status === 'ok' && result.html) {
                    customContainer.innerHTML = result.html;

                    // Add collapse button handler
                    const collapseBtn = customContainer.querySelector('.collapse-btn');
                    if (collapseBtn) {
                        collapseBtn.onclick = (e) => {
                            e.stopPropagation();
                            collapseCard();
                        };
                    } else {
                        // If no collapse button found, add one to the header
                        const expandedHeader = customContainer.querySelector('.expanded-header');
                        if (expandedHeader && !expandedHeader.querySelector('.collapse-btn')) {
                            const missingCollapseBtn = document.createElement('button');
                            missingCollapseBtn.className = 'collapse-btn';
                            missingCollapseBtn.textContent = '✕ Collapse';
                            missingCollapseBtn.onclick = (e) => {
                                e.stopPropagation();
                                collapseCard();
                            };
                            expandedHeader.appendChild(missingCollapseBtn);
                        }
                    }

                    // Add navigation hint if not present
                    const expandedHeader = customContainer.querySelector('.expanded-header h3');
                    if (expandedHeader && !customContainer.querySelector('.expanded-nav-hint')) {
                        const navHint = document.createElement('span');
                        navHint.className = 'expanded-nav-hint';
                        navHint.textContent = ' ← → use arrow keys or page buttons';
                        navHint.style.fontSize = '10px';
                        navHint.style.color = '#999';
                        navHint.style.fontWeight = 'normal';
                        navHint.style.marginLeft = '10px';
                        expandedHeader.appendChild(navHint);
                    }

                    isExpanded = true;
                    expandedNodeName = nodeName;
                    card.classList.add('expanded-card');
                    document.getElementById('nodeGrid').classList.add('expanded');

                    // Hide other cards
                    document.querySelectorAll('.node-card').forEach(c => {
                        if (c !== card) {
                            c.style.visibility = 'hidden';
                            c.style.position = 'absolute';
                        }
                    });

                    // Update pagination controls for expanded mode
                    updatePaginationControls();

                    // Start refreshing dynamic data
                    startExpandedRefresh(nodeName);
                } else {
                    customContainer.innerHTML = originalHtml;
                    console.error('Failed to load expanded view:', result.message);
                }
            } catch (e) {
                customContainer.innerHTML = originalHtml;
                console.error('Error loading expanded view:', e);
            }
        }

        // Collapse expanded card - restore to original state
        function collapseCard() {
            if (!isExpanded) return;

            // Stop the dynamic data refresh
            stopExpandedRefresh();

            const expandedCard = document.querySelector('.node-card.expanded-card');
            if (expandedCard) {
                const originalHtml = expandedCard.getAttribute('data-original-html');

                // Restore the original custom data container HTML
                const customContainer = expandedCard.querySelector('.node-custom-container');
                if (customContainer && originalHtml) {
                    customContainer.innerHTML = originalHtml;
                }

                // Remove expanded class
                expandedCard.classList.remove('expanded-card');
            }

            // Restore all cards
            document.querySelectorAll('.node-card').forEach(c => {
                c.style.visibility = '';
                c.style.position = '';
            });

            // Remove expanded class from grid
            document.getElementById('nodeGrid').classList.remove('expanded');

            isExpanded = false;
            expandedNodeName = null;

            // Restore normal pagination mode
            updatePaginationControls();

            // Refresh the grid to ensure all data is current
            renderCurrentPage();
        }

        function setupDebugWindow() {
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;
            debugWindow.addEventListener('scroll', function() {
                const isAtBottom = debugWindow.scrollHeight - debugWindow.scrollTop - debugWindow.clientHeight < 10;
                if (isAtBottom) {
                    autoScrollEnabled = true;
                } else {
                    autoScrollEnabled = false;
                    if (scrollTimeout) clearTimeout(scrollTimeout);
                    scrollTimeout = setTimeout(() => { autoScrollEnabled = true; }, 10000);
                }
            });
        }

        // Setup floating debug panel with resize, collapse, and dock
        function setupResizableDebugPanel() {
            const debugPanel = document.querySelector('.debug-panel');
            if (!debugPanel) return;

            // Get header elements
            const header = debugPanel.querySelector('.debug-header');
            const title = header.querySelector('h2');
            const buttonsDiv = header.querySelector('.debug-buttons');

            // Clear existing extra buttons from h2 (remove any that were added before)
            const existingBtns = title.querySelectorAll('button');
            existingBtns.forEach(btn => btn.remove());

            // Add dock button to h2
            const dockBtn = document.createElement('button');
            dockBtn.className = 'debug-dock-btn';
            dockBtn.textContent = '📌 Dock';
            dockBtn.onclick = function(e) {
                e.stopPropagation();
                dockDebugPanel();
            };
            title.appendChild(dockBtn);

            // Add toggle button to h2
            const toggleBtn = document.createElement('button');
            toggleBtn.className = 'debug-toggle-btn';
            toggleBtn.textContent = '▲ Collapse';
            toggleBtn.onclick = function(e) {
                e.stopPropagation();
                toggleDebugPanel();
            };
            title.appendChild(toggleBtn);

            // Store docked height
            if (!debugPanel.getAttribute('data-docked-height')) {
                debugPanel.setAttribute('data-docked-height', '250px');
            }

            // Create resize handle at the top edge
            let resizeHandle = debugPanel.querySelector('.debug-panel-resize-handle');
            if (!resizeHandle) {
                resizeHandle = document.createElement('div');
                resizeHandle.className = 'debug-panel-resize-handle';
                debugPanel.insertBefore(resizeHandle, debugPanel.firstChild);
            }

            // Set initial docked height
            if (!debugPanel.style.height || debugPanel.style.height === '40px' || debugPanel.style.height === '42px') {
                debugPanel.style.height = debugPanel.getAttribute('data-docked-height');
            }

            let isResizing = false;
            let startY = 0;
            let startHeight = 0;

            resizeHandle.addEventListener('mousedown', function(e) {
                if (debugPanelCollapsed) return;
                isResizing = true;
                startY = e.clientY;
                startHeight = debugPanel.offsetHeight;
                document.body.style.userSelect = 'none';
                document.body.style.cursor = 'ns-resize';
                e.preventDefault();
            });

            document.addEventListener('mousemove', function(e) {
                if (!isResizing) return;

                // Calculate new height (dragging UP increases height)
                const deltaY = startY - e.clientY;
                let newHeight = startHeight + deltaY;

                const minHeight = 100;
                const maxHeight = window.innerHeight * 0.93;  // 93% of window height, just below header/banner

                newHeight = Math.min(maxHeight, Math.max(minHeight, newHeight));

                debugPanel.style.height = newHeight + 'px';
            });

            document.addEventListener('mouseup', function() {
                if (isResizing) {
                    isResizing = false;
                    document.body.style.userSelect = '';
                    document.body.style.cursor = '';
                }
            });
        }

        function dockDebugPanel() {
            const debugPanel = document.querySelector('.debug-panel');
            const dockBtn = document.querySelector('.debug-dock-btn');

            if (!debugPanel) return;

            const dockedHeight = debugPanel.getAttribute('data-docked-height') || '250px';

            // If already docked, do nothing
            if (debugPanel.style.height === dockedHeight && !debugPanelCollapsed) {
                // Flash feedback to show it's already docked
                dockBtn.style.background = '#28a745';
                setTimeout(() => {
                    dockBtn.style.background = '#6c757d';
                }, 500);
                return;
            }

            // Ensure not collapsed
            if (debugPanelCollapsed) {
                toggleDebugPanel();
            }

            // Set to docked height
            debugPanel.style.height = dockedHeight;

            // Flash feedback
            dockBtn.style.background = '#28a745';
            setTimeout(() => {
                dockBtn.style.background = '#6c757d';
            }, 500);
        }

        function toggleDebugPanel() {
            const debugPanel = document.querySelector('.debug-panel');
            const toggleBtn = document.querySelector('.debug-toggle-btn');

            if (!debugPanel) return;

            debugPanelCollapsed = !debugPanelCollapsed;

            if (debugPanelCollapsed) {
                // Save current height before collapsing
                const currentHeight = debugPanel.style.height;
                if (currentHeight && currentHeight !== '40px' && currentHeight !== '42px') {
                    debugPanel.setAttribute('data-expanded-height', currentHeight);
                }
                debugPanel.classList.add('collapsed');
                if (toggleBtn) toggleBtn.textContent = '▼ Expand';
            } else {
                debugPanel.classList.remove('collapsed');
                const savedHeight = debugPanel.getAttribute('data-expanded-height');
                if (savedHeight) {
                    debugPanel.style.height = savedHeight;
                } else if (debugPanel.style.height === '40px' || debugPanel.style.height === '42px') {
                    debugPanel.style.height = debugPanel.getAttribute('data-docked-height') || '250px';
                }
                if (toggleBtn) toggleBtn.textContent = '▲ Collapse';
            }
        }

        function selectAllLogs() {
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;
            const range = document.createRange();
            range.selectNodeContents(debugWindow);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
        }

        function copyLogsToClipboard(event) {
            const debugWindow = document.getElementById('debugWindow');
            const text = debugWindow.innerText;
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(text).then(() => {
                    const btn = event.currentTarget;
                    const originalText = btn.textContent;
                    btn.textContent = '✓ Copied!';
                    setTimeout(() => { btn.textContent = originalText; }, 2000);
                }).catch(() => fallbackCopy(text));
            } else {
                fallbackCopy(text);
            }
        }

        function fallbackCopy(text) {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);
            alert('Copied to clipboard');
        }

        function clearLogs() {
            const debugWindow = document.getElementById('debugWindow');
            if (debugWindow) {
                debugWindow.innerHTML = 'Logs cleared...';
            }
            logBuffer = [];
            logElements = [];
            logTexts = [];
            fetch('/api/logs/clear', {method: 'POST'})
                .then(() => setupLogsStream())
                .catch(e => console.error('Error clearing logs:', e));
        }

        // Filter state
        let filterExcludeCtrl = false;
        let filterIncludeNodes = new Set();  // Set of node IP last octets to include (empty means all)
        let filterHighlightNodes = new Set(); // Set of node IP last octets to highlight
        let filterMinLogLevel = 4;  // Default OFF (4)
        let controllerIpPrefix = ''; // Will be set from server

        // Parse node IP last octet from log line
        function extractNodeLastOctet(logLine) {
            const bracketMatch = logLine.match(/\\[NODE\\]\\s+\\[([^\\]]+)\\]/);
            if (bracketMatch && bracketMatch[1]) {
                const ipParts = bracketMatch[1].split('.');
                if (ipParts.length === 4) {
                    return ipParts[3];
                }
            }
            return null;
        }

        // Extract log level from NODE log line
        function extractLogLevel(logLine) {
            if (!logLine.includes('[NODE]')) return null;

            const match = logLine.match(/\\[NODE\\].*?\\[[\\d.]+\\]\\s*([DIWE])\\s/);
            if (match && match[1]) {
                const levelChar = match[1];
                if (levelChar === 'D') return 0;  // DEBUG
                if (levelChar === 'I') return 1;  // INFO
                if (levelChar === 'W') return 2;  // WARN
                if (levelChar === 'E') return 3;  // ERROR
            }
            return null;
        }

        function shouldDisplayLog(logLine) {
            const isCtrl = logLine.includes('[CTRL]');
            const isNode = logLine.includes('[NODE]');

            // Exclude CTRL filter
            if (filterExcludeCtrl && isCtrl) {
                return false;
            }

            // Include only NODEs filter
            if (filterIncludeNodes.size > 0 && isNode) {
                const lastOctet = extractNodeLastOctet(logLine);
                if (lastOctet && !filterIncludeNodes.has(lastOctet)) {
                    return false;
                }
            }

            // Minimum log level filter (NODE logs only)
            if (isNode && filterMinLogLevel < 4) {  // 4 = OFF
                const logLevel = extractLogLevel(logLine);
                if (logLevel !== null && logLevel < filterMinLogLevel) {
                    return false;
                }
            }

            return true;
        }

        // Apply highlighting to a log line
        function applyHighlighting(logLine) {
            if (filterHighlightNodes.size === 0) {
                return logLine;  // Return raw text, not escaped
            }

            const lastOctet = extractNodeLastOctet(logLine);
            if (lastOctet && filterHighlightNodes.has(lastOctet)) {
                return `<span class="log-highlight">${logLine}</span>`;
            }
            return logLine;
        }

        // Store all log DOM elements for efficient filtering
        let logElements = [];  // Store references to DOM elements
        let logTexts = [];     // Store raw text for each log

        function refilterAndRenderLogs() {
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;

            // If we have no logElements yet, build them from buffer
            if (logElements.length === 0 && logBuffer.length > 0) {
                debugWindow.innerHTML = '';
                logElements = [];
                logTexts = [];
                for (const log of logBuffer) {
                    const className = log.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                    const logDiv = document.createElement('div');
                    logDiv.className = className;
                    logDiv.textContent = log;
                    debugWindow.appendChild(logDiv);
                    logElements.push(logDiv);
                    logTexts.push(log);
                }
            }

            // Apply filters to existing elements
            let visibleCount = 0;
            for (let i = 0; i < logElements.length; i++) {
                const logDiv = logElements[i];
                const log = logTexts[i];
                const shouldShow = shouldDisplayLog(log);

                if (shouldShow) {
                    logDiv.style.display = '';
                    const highlightedContent = applyHighlighting(log);
                    if (highlightedContent !== log) {
                        logDiv.innerHTML = highlightedContent;
                    } else if (logDiv.innerHTML !== log && logDiv.textContent === log) {
                        logDiv.textContent = log;
                    }
                    visibleCount++;
                } else {
                    logDiv.style.setProperty('display', 'none', 'important');
                }
            }

            // Show message if nothing visible
            const emptyMsg = document.getElementById('filter-empty-message');
            if (visibleCount === 0 && logBuffer.length > 0) {
                if (!emptyMsg) {
                    const msg = document.createElement('div');
                    msg.id = 'filter-empty-message';
                    msg.className = 'log-ctrl';
                    msg.textContent = 'No logs match current filters...';
                    debugWindow.appendChild(msg);
                } else {
                    emptyMsg.style.display = '';
                }
            } else if (emptyMsg) {
                emptyMsg.style.display = 'none';
            }
        }

        // Parse comma/space separated list of numbers into a Set
        function parseFilterInput(inputValue) {
            const result = new Set();
            if (!inputValue.trim()) return result;

            // Match one or more numbers, separated by any non-number characters
            const matches = inputValue.match(/\\d+/g);
            if (matches) {
                for (const match of matches) {
                    result.add(match);
                }
            }
            return result;
        }

        // Update filter labels with controller IP prefix
        function updateFilterLabels(controllerIpPrefix) {
            const includeLabel = document.getElementById('filterIncludeLabel');
            const highlightLabel = document.getElementById('filterHighlightLabel');

            if (includeLabel && controllerIpPrefix) {
                includeLabel.textContent = `Include only NODEs ${controllerIpPrefix}.X`;
            }
            if (highlightLabel && controllerIpPrefix) {
                highlightLabel.textContent = `Highlight NODEs ${controllerIpPrefix}.X`;
            }
        }

        function updateFiltersAndRender() {
            const excludeCheckbox = document.getElementById('filterExcludeCtrl');
            filterExcludeCtrl = excludeCheckbox ? excludeCheckbox.checked : false;

            const includeInput = document.getElementById('filterIncludeNodes');
            filterIncludeNodes = parseFilterInput(includeInput ? includeInput.value : '');

            const highlightInput = document.getElementById('filterHighlightNodes');
            filterHighlightNodes = parseFilterInput(highlightInput ? highlightInput.value : '');

            const logLevelSelect = document.getElementById('filterMinLogLevel');
            filterMinLogLevel = logLevelSelect ? parseInt(logLevelSelect.value) : 4;

            refilterAndRenderLogs();
        }

        // Set controller IP prefix for filter hints
        function setControllerIpPrefix(ip) {
            // Extract first three octets: a.b.c
            const match = ip.match(/(\\d+\\.\\d+\\.\\d+)\\.\\d+/);
            if (match) {
                controllerIpPrefix = match[1];
                // Update placeholder text for filter inputs
                const includeInput = document.getElementById('filterIncludeNodes');
                const highlightInput = document.getElementById('filterHighlightNodes');
                if (includeInput) {
                    includeInput.placeholder = `e.g., 1,2,3 (${controllerIpPrefix}.X)`;
                }
                if (highlightInput) {
                    highlightInput.placeholder = `e.g., 1,2,3 (${controllerIpPrefix}.X)`;
                }
            }
        }

        function appendLogsFiltered(newLogs) {
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;

            if (newLogs.length === 0) return;

            // Remove empty message if present
            const emptyMsg = document.getElementById('filter-empty-message');
            if (emptyMsg) emptyMsg.remove();

            // Clear "Waiting" message
            if (debugWindow.innerHTML === 'Waiting for logs...') {
                debugWindow.innerHTML = '';
                logElements = [];
                logTexts = [];
            }

            const wasAtBottom = debugWindow.scrollHeight - debugWindow.scrollTop - debugWindow.clientHeight < 10;

            // Add new logs to buffer and DOM
            for (const log of newLogs) {
                logBuffer.push(log);
                logTexts.push(log);

                const shouldShow = shouldDisplayLog(log);
                const className = log.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                const logDiv = document.createElement('div');
                logDiv.className = className;

                if (shouldShow) {
                    const highlightedContent = applyHighlighting(log);
                    if (highlightedContent !== log) {
                        logDiv.innerHTML = highlightedContent;
                    } else {
                        logDiv.textContent = log;
                    }
                    logDiv.style.display = '';
                } else {
                    logDiv.textContent = log;
                    logDiv.style.setProperty('display', 'none', 'important');
                }

                debugWindow.appendChild(logDiv);
                logElements.push(logDiv);
            }

            if (wasAtBottom && autoScrollEnabled) {
                debugWindow.scrollTop = debugWindow.scrollHeight;
            }
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Update pagination controls - handle expanded mode differently
        function updatePaginationControls() {
            const prevBtn = document.getElementById('prevPageBtn');
            const nextBtn = document.getElementById('nextPageBtn');
            const currentPageSpan = document.getElementById('currentPageNum');
            const totalPagesSpan = document.getElementById('totalPagesNum');

            if (isExpanded) {
                // In expanded mode, enable/disable based on node position in list
                if (expandedNodeName) {
                    const currentIndex = allNodes.indexOf(expandedNodeName);
                    if (prevBtn) prevBtn.disabled = (currentIndex <= 0);
                    if (nextBtn) nextBtn.disabled = (currentIndex >= allNodes.length - 1);
                }
                // Show current node position in page indicator
                if (expandedNodeName) {
                    const currentIndex = allNodes.indexOf(expandedNodeName);
                    if (currentPageSpan && totalPagesSpan) {
                        currentPageSpan.textContent = currentIndex + 1;
                        totalPagesSpan.textContent = allNodes.length;
                    }
                }
            } else {
                // Normal pagination mode
                totalPages = Math.max(1, Math.ceil(allNodes.length / nodesPerPage));

                if (currentPage >= totalPages) {
                    currentPage = totalPages - 1;
                }
                if (currentPage < 0) {
                    currentPage = 0;
                }

                if (currentPageSpan) currentPageSpan.textContent = currentPage + 1;
                if (totalPagesSpan) totalPagesSpan.textContent = totalPages;

                if (prevBtn) prevBtn.disabled = (currentPage === 0);
                if (nextBtn) nextBtn.disabled = (currentPage >= totalPages - 1);
            }
        }

       function previousPage() {
            if (isExpanded) {
                // In expanded mode, navigate to previous node in the list
                if (expandedNodeName) {
                    const currentIndex = allNodes.indexOf(expandedNodeName);
                    if (currentIndex > 0) {
                        const prevNode = allNodes[currentIndex - 1];
                        expandCard(prevNode);
                    }
                }
            } else if (currentPage > 0) {
                currentPage--;
                updatePaginationControls();
                renderCurrentPage();
            }
        }

        function nextPage() {
            if (isExpanded) {
                // In expanded mode, navigate to next node in the list
                if (expandedNodeName) {
                    const currentIndex = allNodes.indexOf(expandedNodeName);
                    if (currentIndex < allNodes.length - 1) {
                        const nextNode = allNodes[currentIndex + 1];
                        expandCard(nextNode);
                    }
                }
            } else if (currentPage < totalPages - 1) {
                currentPage++;
                updatePaginationControls();
                renderCurrentPage();
            }
        }

        function goToPage(page) {
            if (page >= 0 && page < totalPages && page !== currentPage && !isExpanded) {
                currentPage = page;
                updatePaginationControls();
                renderCurrentPage();
                return true;
            }
            return false;
        }

        function handleDragStart(e, nodeName) {
            if (isExpanded) return;
            isDragging = true;
            dragSourceNode = nodeName;
            e.dataTransfer.setData('text/plain', nodeName);
            e.dataTransfer.effectAllowed = 'move';
            const dragIcon = document.createElement('div');
            dragIcon.textContent = '⋮⋮';
            dragIcon.style.position = 'absolute';
            dragIcon.style.top = '-1000px';
            document.body.appendChild(dragIcon);
            e.dataTransfer.setDragImage(dragIcon, 0, 0);
            setTimeout(() => document.body.removeChild(dragIcon), 0);
            startDragAutoScroll(e);
        }

        function handleDragEnd(e) {
            isDragging = false;
            dragSourceNode = null;
            stopDragAutoScroll();
            document.querySelectorAll('.node-card').forEach(card => {
                card.classList.remove('drag-over');
            });
        }

        function startDragAutoScroll(e) {
            if (dragAutoScrollTimer) clearInterval(dragAutoScrollTimer);
            dragAutoScrollTimer = setInterval(() => {
                if (!isDragging || isExpanded) return;
                const mouseX = window.dragMouseX || 0;
                const windowWidth = window.innerWidth;

                const nearRightEdge = mouseX > windowWidth - 100;
                const nearLeftEdge = mouseX < 100;

                if (nearRightEdge && currentPage < totalPages - 1) {
                    nextPage();
                } else if (nearLeftEdge && currentPage > 0) {
                    previousPage();
                }
            }, 500);
        }

        function stopDragAutoScroll() {
            if (dragAutoScrollTimer) {
                clearInterval(dragAutoScrollTimer);
                dragAutoScrollTimer = null;
            }
        }

        document.addEventListener('drag', function(e) {
            window.dragMouseX = e.clientX;
            window.dragMouseY = e.clientY;
        });

        document.addEventListener('keydown', function(e) {
            if (isExpanded) {
                if (e.key === 'ArrowLeft') {
                    previousPage();
                    e.preventDefault();
                } else if (e.key === 'ArrowRight') {
                    nextPage();
                    e.preventDefault();
                } else if (e.key === 'Escape') {
                    collapseCard();
                    e.preventDefault();
                }
            }
        });

        function handleDragOver(e) {
            if (isExpanded) return;
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
        }

        function handleDragEnter(e) {
            if (isExpanded) return;
            e.preventDefault();
            const card = e.target.closest('.node-card');
            if (card) {
                card.classList.add('drag-over');
            }
        }

        function handleDragLeave(e) {
            const card = e.target.closest('.node-card');
            if (card) {
                card.classList.remove('drag-over');
            }
        }

        async function handleDrop(e, targetNodeName) {
            if (isExpanded) return;
            e.preventDefault();
            const sourceNode = e.dataTransfer.getData('text/plain');
            const targetNode = targetNodeName;

            if (sourceNode && targetNode && sourceNode !== targetNode) {
                const sourceIndex = allNodes.indexOf(sourceNode);
                const targetIndex = allNodes.indexOf(targetNode);
                if (sourceIndex !== -1 && targetIndex !== -1) {
                    allNodes.splice(sourceIndex, 1);
                    allNodes.splice(targetIndex, 0, sourceNode);
                    nodeOrder = [...allNodes];
                    await saveNodeOrder();
                    renderCurrentPage();
                }
            }

            document.querySelectorAll('.node-card').forEach(card => {
                card.classList.remove('drag-over');
            });
        }

        async function saveNodeOrder() {
            try {
                await fetch('/api/layout', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({order: nodeOrder})
                });
            } catch (e) {
                console.error('Error saving layout:', e);
            }
        }

        function renderCurrentPage() {
            if (isExpanded) return;

            const grid = document.getElementById('nodeGrid');
            const startIdx = currentPage * nodesPerPage;
            const endIdx = Math.min(startIdx + nodesPerPage, allNodes.length);
            const pageNodes = allNodes.slice(startIdx, endIdx);

            if (pageNodes.length === 0 && allNodes.length > 0) {
                currentPage = Math.max(0, totalPages - 1);
                updatePaginationControls();
                renderCurrentPage();
                return;
            }

            let html = '';
            for (const nodeName of pageNodes) {
                const nodeData = nodesData[nodeName];
                if (!nodeData) continue;

                const node = nodeData;
                const isOnline = node.connected;
                const essentialClass = node.essential ? 'essential' : 'non-essential';
                const customHtml = node.custom_html || '';
                const logStatusText = node.log_status || '?';
                const rssi = node.rssi !== undefined && node.rssi !== null ? node.rssi : '?';
                const rssiIcon = (rssi !== '?' && rssi > -50) ? '📶' : '📶';  // Keep icon simple for unknown

                html += `
                    <div class="node-card ${isOnline ? 'online' : 'offline'} ${essentialClass}"
                         data-node-name="${node.name}"
                         ondblclick="expandCard('${node.name}')"
                         ondragover="handleDragOver(event)"
                         ondragenter="handleDragEnter(event)"
                         ondragleave="handleDragLeave(event)"
                         ondrop="handleDrop(event, '${node.name}')">
                        <div class="expand-hint">⤢ Double-click to expand</div>
                        <div class="node-header">
                            <div class="node-title">
                                <span class="drag-handle"
                                      draggable="true"
                                      ondragstart="handleDragStart(event, '${node.name}')"
                                      ondragend="handleDragEnd(event)">⋮⋮</span>
                                <span class="node-name">${escapeHtml(node.name)}</span>
                            </div>
                            <div>
                                <span class="node-type">${escapeHtml(node.type || 'unknown')}</span>
                                <span class="node-ip">${escapeHtml(node.ip)}</span>
                            </div>
                        </div>

                        <div class="node-custom-container">
                            ${customHtml || '<div class="node-custom"><div class="custom-value">—</div><div class="custom-unit">No data</div></div>'}
                        </div>

                        <div class="node-footer">
                            <div class="node-status-line">
                                <div class="node-state ${isOnline ? 'online' : 'offline'}">
                                    <span class="state-text">${isOnline ? '● online' : '○ offline'} - ${escapeHtml(node.state)}</span>
                                </div>
                                <div id="notification-${node.name.replace(/[^a-zA-Z0-9]/g, '_')}" class="notification-placeholder"></div>
                            </div>
                                <div class="node-metrics">
                                    <span class="connection-duration">${node.connection_duration ? '📡 ' + node.connection_duration : ''}</span>
                                    <div class="rssi-display" style="display: inline-flex; align-items: center; gap: 3px;">
                                        <span class="rssi-icon">${rssiIcon}</span>
                                        <span class="rssi-value">${rssi} dBm</span>
                                    </div>
                                    <span>📨 <span class="message-count">${node.message_count}</span> 💓 <span class="heartbeat-count">${node.heartbeat_count}</span></span>
                                </div>
                            <div class="node-actions">
                                <button class="btn-query" onclick="sendCommand('${node.name}', 'query_state')">Ping</button>
                                <button class="btn-start" onclick="sendCommand('${node.name}', 'start')">Start</button>
                                <button class="btn-stop" onclick="sendCommand('${node.name}', 'stop')">Stop</button>
                                <button class="btn-reboot" onclick="sendCommand('${node.name}', 'reboot')">Reboot</button>
                            </div>
                            <div class="node-actions-group-5col">
                                <button class="btn-log-start" onclick="sendCommand('${node.name}', 'log_start')">Log On</button>
                                <button class="btn-log-stop" onclick="sendCommand('${node.name}', 'log_stop')">Log Off</button>
                                <select class="log-level-select" data-node-name="${node.name}">
                                    <option value="0">DEBUG</option>
                                    <option value="1">INFO</option>
                                    <option value="2">WARN</option>
                                    <option value="3">ERROR</option>
                                </select>
                                <button class="btn-apply-level" onclick="sendCommand('${node.name}', 'log_level', {level: parseInt(this.previousElementSibling.value)})">Apply</button>
                                <div class="log-status-text">${logStatusText}</div>
                            </div>
                        </div>
                    </div>
                `;
            }

            grid.innerHTML = html;

            for (const nodeName of pageNodes) {
                updateNodeCustomData(nodesData[nodeName]);
            }

            gridBuilt = true;
        }

        function updateExistingNodes(status) {
            // Update nodesData with latest info
            for (const node of status.nodes) {
                nodesData[node.name] = node;
            }

            // Find the expanded card if we're expanded
            let expandedCard = null;
            if (isExpanded) {
                expandedCard = document.querySelector('.node-card.expanded-card');
            }

            const startIdx = currentPage * nodesPerPage;
            const endIdx = Math.min(startIdx + nodesPerPage, allNodes.length);
            const visibleNodes = allNodes.slice(startIdx, endIdx);

            for (const nodeName of visibleNodes) {
                // If expanded and this is not the expanded node, skip
                if (isExpanded && expandedCard && nodeName !== expandedNodeName) {
                    continue;
                }

                const node = nodesData[nodeName];
                if (!node) {
                    continue;
                }

                // If expanded, use the expanded card; otherwise find by selector
                let card;
                if (isExpanded && expandedCard && nodeName === expandedNodeName) {
                    card = expandedCard;
                } else {
                    card = document.querySelector(`.node-card[data-node-name="${nodeName}"]`);
                }
                if (!card) {
                    continue;
                }

                const isOnline = node.connected;

                // Update classes
                if (isOnline) {
                    card.classList.add('online');
                    card.classList.remove('offline');
                } else {
                    card.classList.add('offline');
                    card.classList.remove('online');
                }
                card.classList.toggle('essential', node.essential);
                card.classList.toggle('non-essential', !node.essential);

                // Update node state text
                const stateSpan = card.querySelector('.state-text');
                if (stateSpan) {
                    stateSpan.textContent = `${isOnline ? '● online' : '○ offline'} - ${node.state}`;
                }

                // Update node-state div class
                const nodeStateDiv = card.querySelector('.node-state');
                if (nodeStateDiv) {
                    if (isOnline) {
                        nodeStateDiv.classList.add('online');
                        nodeStateDiv.classList.remove('offline');
                    } else {
                        nodeStateDiv.classList.add('offline');
                        nodeStateDiv.classList.remove('online');
                    }
                }

                // Update RSSI display
                const rssiSpan = card.querySelector('.rssi-value');
                if (rssiSpan) {
                    const rssiValue = (node.rssi === null || node.rssi === undefined) ? '?' : node.rssi;
                    rssiSpan.textContent = rssiValue + ' dBm';
                }

                // Update notification (toast)
                const notificationContainer = card.querySelector('.notification-placeholder');
                const existingNotification = notificationContainer?.querySelector('.notification');

                const isNewNotification = node.notification &&
                    (!lastNotificationTimestamp[node.name] ||
                     lastNotificationTimestamp[node.name] !== node.notification.timestamp);

                if (node.notification && isNewNotification) {
                    lastNotificationTimestamp[node.name] = node.notification.timestamp;

                    const notificationClass = node.notification.is_success === false ? 'notification failure' :
                                             (node.notification.message.includes('◀️') ? 'notification success' : 'notification');

                    if (existingNotification) existingNotification.remove();

                    const newNotification = document.createElement('div');
                    newNotification.className = notificationClass;
                    newNotification.textContent = node.notification.message;
                    notificationContainer.appendChild(newNotification);

                    setTimeout(() => {
                        if (newNotification.parentNode) newNotification.remove();
                    }, 3000);
                } else if (!node.notification && existingNotification) {
                    existingNotification.remove();
                }

                // Update metrics (connection duration, message count, heartbeat count)
                const durationSpan = card.querySelector('.connection-duration');
                if (durationSpan) {
                    durationSpan.textContent = node.connection_duration ? '📡 ' + node.connection_duration : '';
                }

                const messageCountSpan = card.querySelector('.message-count');
                if (messageCountSpan) {
                    messageCountSpan.textContent = node.message_count;
                }

                const heartbeatCountSpan = card.querySelector('.heartbeat-count');
                if (heartbeatCountSpan) {
                    heartbeatCountSpan.textContent = node.heartbeat_count;
                }

                // Update log status text
                const logStatusSpan = card.querySelector('.log-status-text');
                if (logStatusSpan && node.log_status) {
                    logStatusSpan.textContent = node.log_status;
                }

                // Update custom data (the card's center area - only if not expanded or if it's a different node)
                if (!isExpanded || (isExpanded && nodeName !== expandedNodeName)) {
                    updateNodeCustomData(node);
                }
            }
        }

        function updateNodeCustomData(node) {
            const card = document.querySelector(`.node-card[data-node-name="${node.name}"]`);
            if (!card) return;

            // Update custom HTML if provided
            const customContainer = card.querySelector('.node-custom-container');
            if (customContainer && node.custom_html && !isExpanded) {
                customContainer.innerHTML = node.custom_html;
            }
        }

        async function sendCommand(nodeName, command, params = {}) {
            try {
                const response = await fetch('/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({node: nodeName, command: command, params: params})
                });
                const result = await response.json();
                if (result.status === 'error') {
                    console.error(`[ERROR] ${result.message}`);
                }
            } catch (e) { console.error(`[ERROR] ${e.message}`); }
        }

        function updateUI(status) {
            const badge = document.getElementById('journalBadge');
            if (badge) {
                badge.textContent = status.journal_enabled ? '✓ Journal Active' : '⚠️ Journal Disabled';
                badge.className = status.journal_enabled ? 'badge-success' : 'badge-warning';
            }

            updateCSSVariables(status);

            const rows = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-rows')) || 2;
            const cols = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-columns')) || 4;
            nodesPerPage = rows * cols;

            if (status.nodes && status.nodes.length > 0) {
                if (nodeOrder.length === 0 && status.nodes.length > 0) {
                    nodeOrder = status.nodes.map(n => n.name);
                    allNodes = [...nodeOrder];
                } else {
                    const existingNodeNames = new Set(allNodes);
                    for (const node of status.nodes) {
                        if (!existingNodeNames.has(node.name)) {
                            allNodes.push(node.name);
                        }
                    }
                    nodeOrder = [...allNodes];
                }
            }

            for (const node of status.nodes) {
                nodesData[node.name] = node;
            }

            updatePaginationControls();

            const banner = document.getElementById('statusBanner');
            const total = status.total_nodes;
            const essential = status.essential_nodes;
            const connected_essential = status.connected_essential_nodes;
            const working_essential = status.working_essential_nodes;

            if (essential === 0) {
                banner.className = 'status-banner ready';
                banner.querySelector('.status-icon').textContent = '✅';
                banner.querySelector('.status-text > div:first-child').textContent = 'NO ESSENTIAL NODES CONFIGURED';
                banner.querySelector('.status-details').textContent = `${status.total_nodes} total nodes (all optional)`;
            } else if (connected_essential === essential && working_essential === essential) {
                banner.className = 'status-banner ready';
                banner.querySelector('.status-icon').textContent = '✅';
                banner.querySelector('.status-text > div:first-child').textContent = 'ALL ESSENTIAL NODES WORKING';
                banner.querySelector('.status-details').textContent = `${connected_essential}/${essential} essential connected, ${working_essential} working`;
            } else if (working_essential > 0) {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⏳';
                banner.querySelector('.status-text > div:first-child').textContent = 'ESSENTIAL NODES INITIALIZING';
                banner.querySelector('.status-details').textContent = `${connected_essential}/${essential} essential connected, ${working_essential} working`;
            } else if (connected_essential > 0) {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⏳';
                banner.querySelector('.status-text > div:first-child').textContent = 'ESSENTIAL NODES CONNECTING';
                banner.querySelector('.status-details').textContent = `${connected_essential}/${essential} essential connected, waiting for working state`;
            } else {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⚠️';
                banner.querySelector('.status-text > div:first-child').textContent = 'WAITING FOR ESSENTIAL NODES';
                banner.querySelector('.status-details').textContent = `0/${essential} essential connected`;
            }

            if (status.total_nodes > essential) {
                const optional_count = status.total_nodes - essential;
                const optional_connected = status.connected_nodes - connected_essential;
                banner.querySelector('.status-details').textContent += ` (${optional_connected}/${optional_count} optional online)`;
            }

            const grid = document.getElementById('nodeGrid');
            if (!isExpanded && (!gridBuilt || grid.children.length === 0 || grid.children[0]?.innerText === 'Loading nodes...')) {
                renderCurrentPage();
            } else {
                updateExistingNodes(status);
            }

            if (grid.scrollWidth > grid.clientWidth) {
                document.getElementById('scrollHint').classList.add('show');
            } else {
                document.getElementById('scrollHint').classList.remove('show');
            }
        }

        function startExpandedRefresh(nodeName) {
            // Clear existing interval
            if (expandedRefreshInterval) {
                clearInterval(expandedRefreshInterval);
                expandedRefreshInterval = null;
            }

            // Start new interval (update every 2 seconds)
            expandedRefreshInterval = setInterval(async () => {

                if (!isExpanded || expandedNodeName !== nodeName) {
                    // Not expanded anymore or node changed, stop refreshing
                    if (expandedRefreshInterval) {
                        clearInterval(expandedRefreshInterval);
                        expandedRefreshInterval = null;
                    }
                    return;
                }

                try {
                    const response = await fetch('/api/node/data', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({node: nodeName})
                    });
                    const result = await response.json();

                    if (result.status === 'ok' && result.data) {
                        updateExpandedDynamicData(result.data);
                    } else {
                        console.error('Refresh API error:', result);
                    }
                } catch (e) {
                    console.error('Error refreshing expanded data:', e);
                }
            }, 2000);
        }

        function updateExpandedDynamicData(nodeData) {
            const expandedCard = document.querySelector('.node-card.expanded-card');
            if (!expandedCard) return;

            const expandedFooter = expandedCard.querySelector('.expanded-footer');
            if (!expandedFooter) return;

            const customData = nodeData.custom_data || {};

            // Update log status - nodeData.log_status is at top level
            const logStatusSpan = expandedFooter.querySelector('[data-dynamic="log_status"]');
            if (logStatusSpan) {
                // Construct log status from individual fields if log_status not directly available
                let logStatusText = nodeData.log_status;
                if (!logStatusText && nodeData.log_on !== undefined && nodeData.log_level !== undefined) {
                    const logOnStr = nodeData.log_on ? 'ON' : 'OFF';
                    const levelNames = ['DEBUG', 'INFO', 'WARN', 'ERROR'];
                    const levelStr = levelNames[nodeData.log_level] || '?';
                    logStatusText = `${logOnStr}/${levelStr}`;
                }
                if (logStatusText && logStatusSpan.textContent !== logStatusText) {
                    logStatusSpan.textContent = logStatusText;
                    // Flash effect
                    const originalBg = logStatusSpan.style.backgroundColor;
                    logStatusSpan.style.backgroundColor = '#90ee90';
                    setTimeout(() => {
                        logStatusSpan.style.backgroundColor = originalBg;
                    }, 200);
                }
            }

            // Update debug status (combined LED + Breathe)
            const debugStatusSpan = expandedFooter.querySelector('[data-dynamic="debug_status"]');
            if (debugStatusSpan) {
                let ledText = '?';
                let breatheText = '?';

                if (nodeData.led_on !== undefined && nodeData.led_on !== null) {
                    ledText = nodeData.led_on ? 'ON' : 'OFF';
                }
                if (nodeData.led_breathe_on !== undefined && nodeData.led_breathe_on !== null) {
                    breatheText = nodeData.led_breathe_on ? 'ON' : 'OFF';
                }

                const newStatusText = `LED: ${ledText} / Breathe: ${breatheText}`;
                if (debugStatusSpan.textContent !== newStatusText) {
                    debugStatusSpan.textContent = newStatusText;
                    // Flash effect
                    const originalBg = debugStatusSpan.style.backgroundColor;
                    debugStatusSpan.style.backgroundColor = '#90ee90';
                    setTimeout(() => {
                        debugStatusSpan.style.backgroundColor = originalBg;
                    }, 200);
                }
            }

            // Update RSSI in metrics row
            const rssiValueSpan = expandedFooter.querySelector('.rssi-value');
            if (rssiValueSpan && nodeData.rssi !== undefined) {
                let rssiValue = nodeData.rssi;
                if (rssiValue === null || rssiValue === '?' || rssiValue === 'null' || rssiValue === 0) {
                    rssiValue = '?';
                }
                rssiValueSpan.textContent = rssiValue + ' dBm';
            }

            // Update node-specific dynamic elements in custom container
            const customContainer = expandedCard.querySelector('.node-custom-container');
            if (customContainer) {
                const dynamicElements = customContainer.querySelectorAll('[data-dynamic]');
                dynamicElements.forEach(el => {
                    const field = el.getAttribute('data-dynamic');
                    let newValue = null;

                    if (field === 'value') {
                        newValue = customData.value !== undefined ? customData.value : 'N/A';
                    } else if (field === 'last_update') {
                        newValue = customData.last_update || 'N/A';
                    } else if (field === 'water_height') {
                        newValue = customData.water_height !== undefined ? customData.water_height : 'N/A';
                    } else if (field === 'level') {
                        newValue = customData.level !== undefined ? customData.level : 'N/A';
                    } else if (field === 'percentage') {
                        newValue = customData.percentage !== undefined ? customData.percentage.toFixed(1) : 'N/A';
                    } else if (customData[field] !== undefined) {
                        newValue = customData[field];
                    }

                    if (newValue !== null && el.textContent != newValue) {
                        el.textContent = newValue;
                        const originalBg = el.style.backgroundColor;
                        el.style.backgroundColor = '#90ee90';
                        setTimeout(() => {
                            el.style.backgroundColor = originalBg;
                        }, 200);
                    }
                });
            }
        }

        function stopExpandedRefresh() {
            if (expandedRefreshInterval) {
                clearInterval(expandedRefreshInterval);
                expandedRefreshInterval = null;
            }
        }

        function setupStatusStream() {
            if (statusSource) statusSource.close();
            const source = new EventSource('/api/status/stream');
            source.onmessage = (event) => {
                try {
                    const status = JSON.parse(event.data);
                    updateUI(status);
                    // Extract controller IP prefix from first node's IP
                    if (!controllerIpPrefix && status.nodes && status.nodes.length > 0) {
                        for (const node of status.nodes) {
                            if (node.ip) {
                                const match = node.ip.match(/(\\d+\\.\\d+\\.\\d+)\\.\\d+/);
                                if (match) {
                                    controllerIpPrefix = match[1];
                                    updateFilterLabels(controllerIpPrefix);
                                    // Also update placeholder hints
                                    const includeInput = document.getElementById('filterIncludeNodes');
                                    const highlightInput = document.getElementById('filterHighlightNodes');
                                    if (includeInput) {
                                        includeInput.placeholder = `e.g., 2,3 (${controllerIpPrefix}.X)`;
                                    }
                                    if (highlightInput) {
                                        highlightInput.placeholder = `e.g., 2,3 (${controllerIpPrefix}.X)`;
                                    }
                                    break;
                                }
                            }
                        }
                    }
                } catch (e) { console.error("Error parsing status:", e); }
            };
            source.onerror = () => {
                console.log('Status stream error, reconnecting...');
                source.close();
                setTimeout(setupStatusStream, 5000);
            };
            statusSource = source;
        }

        function setupLogsStream() {
            if (logsSource) logsSource.close();
            const source = new EventSource('/api/logs/stream');

            source.onmessage = (event) => {
                try {
                    const newLogs = JSON.parse(event.data);
                    if (newLogs.length > 0) appendLogsFiltered(newLogs);
                } catch (e) { console.error("Error parsing logs:", e); }
            };

            source.addEventListener('clear', (event) => {
                const debugWindow = document.getElementById('debugWindow');
                if (debugWindow) {
                    debugWindow.innerHTML = 'Logs cleared...';
                }
                logBuffer = [];
                autoScrollEnabled = true;
            });

            source.addEventListener('reset', (event) => {
                const debugWindow = document.getElementById('debugWindow');
                if (debugWindow && debugWindow.innerHTML === '') {
                    debugWindow.innerHTML = 'Waiting for logs...';
                }
                // Reset our stored elements on stream reset
                logElements = [];
                logTexts = [];
            });

            source.onerror = () => {
                console.log('Logs stream error, reconnecting...');
                source.close();
                setTimeout(setupLogsStream, 5000);
            };
            logsSource = source;
        }

        setupDebugWindow();
        setupResizableDebugPanel();
        setupStatusStream();
        setupLogsStream();

        // Setup filter event listeners
        function setupFilterListeners() {
            const excludeCheckbox = document.getElementById('filterExcludeCtrl');
            const includeInput = document.getElementById('filterIncludeNodes');
            const highlightInput = document.getElementById('filterHighlightNodes');
            const logLevelSelect = document.getElementById('filterMinLogLevel');

            // Restrict input: only 2-9, comma, period, space, dash allowed
            function restrictFilterInput(e) {
                this.value = this.value.replace(/[^2-9,.\\s-]/g, '');
            }

            if (excludeCheckbox) {
                excludeCheckbox.addEventListener('change', updateFiltersAndRender);
            }
            if (includeInput) {
                includeInput.addEventListener('input', updateFiltersAndRender);
                includeInput.addEventListener('input', restrictFilterInput);
            }
            if (highlightInput) {
                highlightInput.addEventListener('input', updateFiltersAndRender);
                highlightInput.addEventListener('input', restrictFilterInput);
            }
            if (logLevelSelect) {
                logLevelSelect.addEventListener('change', updateFiltersAndRender);
            }
        }

        setupFilterListeners();

        </script>
</body>
</html>'''


def main():
    parser = argparse.ArgumentParser(description="FGR Controller with Web Interface")
    parser.add_argument("--ip", type=str, default=CONTROLLER_IP_DEFAULT,
                        help=f"IP address for controller (default: {CONTROLLER_IP_DEFAULT})")
    parser.add_argument("--port", type=int, default=CONTROLLER_PORT_DEFAULT,
                        help=f"Port for controller (default: {CONTROLLER_PORT_DEFAULT})")
    parser.add_argument("--http-port", type=int, default=HTTP_PORT_DEFAULT,
                        help=f"HTTP port for web (default: {HTTP_PORT_DEFAULT})")
    parser.add_argument("--cfg", type=Path, help="Path to node configuration file")
    parser.add_argument("--nodes-dir", type=Path, help="Directory containing node handlers")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level (default: INFO)")

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Disable aiohttp access logs
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    # Resolve config file
    cfg_file = args.cfg
    if not cfg_file:
        default_cfg = Path(__file__).parent / "nodes.json"
        if default_cfg.exists():
            cfg_file = default_cfg
            print(f"Using default config: {cfg_file}")

    controller = WebController(
        listen_ip=args.ip,
        port=args.port,
        nodes_dir=args.nodes_dir,
        cfg_file=cfg_file,
        http_port=args.http_port
    )

    print(f"\n{'='*60}")
    print("FGR Controller with Web Interface")
    print(f"{'='*60}")
    print(f"Controller listening on: {args.ip}:{args.port}")
    print(f"Web interface: http://0.0.0.0:{args.http_port}")
    print(f"Journal identifier: {JOURNAL_IDENTIFIER}")
    print(f"Configured nodes: {controller.get_node_names()}")
    print(f"Node grid layout config: {NODE_GRID_CONFIG}")
    print(f"Nodes per page: {NODES_PER_PAGE}")
    print(f"{'='*60}")
    print("Press Ctrl+C to stop\n")

    def signal_handler(signum, frame):
        print("\nShutting down...")
        controller.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if not controller.start():
        print("Failed to start controller")
        sys.exit(1)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        signal_handler(None, None)


if __name__ == "__main__":
    main()