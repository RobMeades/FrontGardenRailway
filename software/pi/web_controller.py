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

import argparse
import asyncio
import json
import threading
import time
import signal
import sys
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from collections import deque

from aiohttp import web

# Import the controller
from controller import Controller, NodeState, NodeHandler, Node
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

# Water reservoir constant (mm from top to full)
RESERVOIR_DEPTH = 1000

# Journal identifier for log_server.py
JOURNAL_IDENTIFIER = 'fgr-log-server'

# Node grid layout configuration file
NODE_GRID_CONFIG = Path(__file__).parent / "node_grid_layout.json"


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
        self.node_measurements: Dict[str, Dict[str, Any]] = {}

        # Store recent message notifications
        self.node_notifications: Dict[str, Dict[str, Any]] = {}

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
        return {'order': [], 'columns': 4, 'rows': 2}

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

    def update_node_measurement(self, node_name: str, measurement: Dict[str, Any]):
        """Store a measurement from a node for web display"""
        self.node_measurements[node_name] = {
            **measurement,
            'last_update': datetime.now().isoformat()
        }

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

        # Get ordered node list based on layout
        ordered_nodes = []
        layout_order = self.node_grid_layout.get('order', [])
        remaining_nodes = list(self.nodes.keys())

        # Count essential nodes that are in a working state
        working_nodes = len([n for n in self.nodes.values()
                                if n.sock and n.essential and
                                n.fgr_state in WORKING_FGR_STATES])

        # Add nodes in layout order first
        for node_name in layout_order:
            if node_name in self.nodes:
                ordered_nodes.append(node_name)
                remaining_nodes.remove(node_name)

        # Add remaining nodes at the end
        ordered_nodes.extend(remaining_nodes)

        for name in ordered_nodes:
            node = self.nodes.get(name)
            if not node:
                continue

            measurement = self.node_measurements.get(name, {})
            notification = self.node_notifications.get(name)

            # Calculate connection duration
            connection_duration = None
            if node.sock and hasattr(node, 'connection_time') and node.connection_time:
                duration = time.time() - node.connection_time
                days = int(duration // 86400)
                hours = int((duration % 86400) // 3600)
                minutes = int((duration % 3600) // 60)
                seconds = int(duration % 60)

                if days > 0:
                    connection_duration = f"{days}d {hours}h {minutes}m"
                elif hours > 0:
                    connection_duration = f"{hours}h {minutes}m"
                elif minutes > 0:
                    connection_duration = f"{minutes}m {seconds}s"
                else:
                    connection_duration = f"{seconds}s"
            elif not node.sock and hasattr(node, 'last_seen') and node.last_seen:
                # Show when it was last seen if disconnected
                duration = time.time() - node.last_seen
                if duration < 3600:
                    connection_duration = f"disconnected {int(duration // 60)}m ago"
                else:
                    connection_duration = f"disconnected {int(duration // 3600)}h ago"

            # Determine display state with formatted name
            if node.sock:
                if node.state == NodeState.STARTED:
                    display_state = "STARTED"
                elif node.state == NodeState.READY:
                    display_state = "READY"
                elif node.state == NodeState.NEEDS_CFG:
                    display_state = "NEEDS CONFIG"
                else:
                    display_state = node.state.name if isinstance(node.state, NodeState) else str(node.state)
                display_state = format_fgr_state(display_state).lower()
            else:
                display_state = "disconnected"

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
                'measurement': measurement,
                'notification': notification
            })

        return {
            'nodes': nodes_status,
            'total_nodes': len(self.nodes),
            'connected_nodes': len([n for n in self.nodes.values() if n.sock]),
            'essential_nodes': len([n for n in self.nodes.values() if n.essential]),
            'connected_essential_nodes': len([n for n in self.nodes.values() if n.sock and n.essential]),
            'working_essential_nodes': working_nodes,
            'initialised_nodes': len([n for n in self.nodes.values() if n.sock and n.state not in [NodeState.DISCONNECTED, NodeState.CONNECTED]]),
            'server_uptime': time.time() - self._start_time,
            'journal_enabled': HAS_SYSTEMD,
            'grid_columns': self.node_grid_layout.get('columns', 4),
            'grid_rows': self.node_grid_layout.get('rows', 2)
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
                await response.write(f"data: {json.dumps(status)}\n\n".encode())
                await asyncio.sleep(2)
        except Exception:
            pass
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
        # No need to broadcast - clients will see that no new logs are available
        # until new ones arrive, and their last_version will reset when they reconnect
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
        await response.write(f"event: reset\ndata: reset\n\n".encode())

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
                        await response.write(f"data: {json.dumps(new_logs)}\n\n".encode())

                await asyncio.sleep(0.5)
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
                    cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_LOG_START, b"", timeout=3.0)
                    if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
                        result['message'] = f"Logging started on {node_name}"
                        self.set_node_notification(node_name, "LOG START", True, True)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"Failed to start logging on {node_name}"
                        self.set_node_notification(node_name, "LOG START failed", True, False)

            elif command == 'log_stop':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_LOG_STOP, b"", timeout=3.0)
                    if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
                        result['message'] = f"Logging stopped on {node_name}"
                        self.set_node_notification(node_name, "LOG STOP", True, True)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"Failed to stop logging on {node_name}"
                        self.set_node_notification(node_name, "LOG STOP failed", True, False)

            elif command == 'log_level':
                if not self.nodes.get(node_name, {}).sock:
                    result['status'] = 'error'
                    result['message'] = f"Node {node_name} not connected"
                    self.set_node_notification(node_name, "Node not connected", True, False)
                else:
                    level = params.get('level', 1)
                    if level < 0 or level > 3:
                        level = 1
                    cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_LOG_LEVEL, bytes([level]), timeout=3.0)
                    if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
                        level_names = ['DEBUG', 'INFO', 'WARN', 'ERROR']
                        result['message'] = f"Log level set to {level_names[level]} on {node_name}"
                        self.set_node_notification(node_name, f"LOG LEVEL {level_names[level]}", True, True)
                    else:
                        result['status'] = 'error'
                        result['message'] = f"Failed to set log level on {node_name}"
                        self.set_node_notification(node_name, "LOG LEVEL failed", True, False)

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

        async def start_server():
            self.web_runner = web.AppRunner(self.web_app)
            await self.web_runner.setup()
            site = web.TCPSite(self.web_runner, '0.0.0.0', self.http_port)
            await site.start()
            self.web_running = True
            print(f"Web interface running at http://0.0.0.0:{self.http_port}")
            while self.web_running:
                await asyncio.sleep(1)

        def run_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(start_server())
            loop.run_forever()

        self.web_thread = threading.Thread(target=run_loop, daemon=True)
        self.web_thread.start()

    def stop_web(self):
        """Stop the web server"""
        self.web_running = False
        if self.web_runner:
            def cleanup():
                loop = asyncio.new_event_loop()
                loop.run_until_complete(self.web_runner.cleanup())
            threading.Thread(target=cleanup, daemon=True).start()

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
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            max-width: 1600px;
            margin: 0 auto;
            padding: 15px;
            background: #f5f5f5;
        }
        h1 {
            margin: 0;
            font-size: 24px;
            color: #333;
        }
        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 15px;
        }
        .title-section h1 { margin: 0; }
        .title-section .subtitle { margin: 0; font-size: 12px; color: #666; }

        .status-banner {
            background: #fff3e0;
            border-left: 4px solid #ffc107;
            padding: 8px 15px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 13px;
        }
        .status-banner.ready { background: #e8f5e9; border-left-color: #2e7d32; }
        .status-banner.waiting { background: #fff3e0; border-left-color: #ffc107; }
        .status-icon { font-size: 18px; }
        .status-text { flex: 1; white-space: nowrap; }
        .status-details { font-size: 10px; color: #666; margin-top: 2px; }

        /* Node grid container with horizontal scrolling */
        .grid-container {
            position: relative;
            margin-bottom: 20px;
        }
        .node-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(280px, 1fr));
            gap: 15px;
            overflow-x: auto;
            padding-bottom: 10px;
        }
        .node-grid.has-more {
            grid-template-columns: repeat(auto-fill, minmax(280px, 320px));
        }
        .node-card {
            background: white;
            border-radius: 10px;
            padding: 12px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.1);
            transition: transform 0.1s, box-shadow 0.2s;
            user-select: text;
        }
        .node-card * {
            user-select: text;
        }
        /* Make drag handle not selectable */
        .drag-handle {
            user-select: none;
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
        /* When essential and online, essential border-left takes precedence */
        .node-card.essential.online {
            border-left: 4px solid #4caf50;
        }
        /* Drag over highlight */
        .node-card.drag-over {
            border: 2px solid #4caf50;
            background: #f0fff0;
            transform: scale(1.01);
            transition: all 0.1s ease;
        }

        /* Drag handle styling */
        .drag-handle {
            cursor: grab;
            color: #999;
            font-size: 16px;
            padding: 4px 8px;
            margin-right: 4px;
            display: inline-block;
            transition: color 0.2s;
        }
        .drag-handle:hover {
            color: #666;
        }
        .drag-handle:active {
            cursor: grabbing;
        }
        .node-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .node-title {
            display: flex;
            align-items: center;
            flex: 1;
        }
        .node-name {
            font-size: 16px;
            font-weight: bold;
            color: #333;
        }
        .node-type { font-size: 10px; color: #888; background: #f0f0f0; padding: 2px 6px; border-radius: 20px; }
        .node-ip { font-family: monospace; font-size: 10px; color: #666; margin-bottom: 6px; }
        .node-state { font-size: 12px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; }
        .node-state.online { color: #4caf50; }
        .node-state.offline { color: #f44336; }
        .state-text { text-transform: capitalize; }
        .notification {
            font-size: 10px;
            padding: 2px 6px;
            border-radius: 12px;
            animation: fadeOut 3s forwards;
            background: #2196f3;
            color: white;
            white-space: nowrap;
        }
        .notification.success { background: #4caf50; }
        .notification.failure { background: #f44336; }
        @keyframes fadeOut {
            0% { opacity: 1; }
            70% { opacity: 1; }
            100% { opacity: 0; display: none; }
        }
        .node-metrics {
            font-size: 10px;
            color: #888;
            margin-bottom: 10px;
            border-top: 1px solid #eee;
            padding-top: 6px;
            display: flex;
            justify-content: space-between;
        }
        .node-measurement {
            background: #e3f2fd;
            border-radius: 6px;
            padding: 6px;
            margin-bottom: 10px;
            text-align: center;
        }
        .measurement-value { font-size: 22px; font-weight: bold; color: #1976d2; }
        .measurement-unit { font-size: 11px; color: #666; }

        /* Button grid - 2 rows of 4 equal items */
        .node-actions {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 6px;
            margin-top: 8px;
        }
        .node-actions-group {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 6px;
            margin-top: 6px;
        }
        button, .log-level-select {
            padding: 5px 4px;
            font-size: 10px;
            border: none;
            border-radius: 4px;
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
        .log-level-select {
            background: #e0e0e0;
            cursor: pointer;
            font-size: 9px;
        }

        .debug-panel {
            background: white;
            border-radius: 8px;
            padding: 15px;
            margin-top: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .debug-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .debug-header h2 { margin: 0; font-size: 16px; color: #555; }
        .debug-buttons { display: flex; gap: 6px; }
        .debug-window {
            background: #1e1e1e;
            color: #d4d4d4;
            font-family: 'Courier New', monospace;
            font-size: 11px;
            padding: 10px;
            border-radius: 5px;
            height: 200px;
            overflow-y: auto;
        }
        .debug-window .log-ctrl { color: #4ec9b0; }
        .debug-window .log-node { color: #9cdcfe; }
        .footer {
            text-align: center;
            color: #999;
            font-size: 10px;
            margin-top: 15px;
        }
        .badge-success { background: #4caf50; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; }
        .badge-warning { background: #ff9800; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; }

        .scroll-hint {
            text-align: center;
            font-size: 11px;
            color: #999;
            margin-top: 5px;
            display: none;
        }
        .scroll-hint.show { display: block; }

        @media (max-width: 1200px) {
            .node-grid { grid-template-columns: repeat(4, minmax(260px, 1fr)); }
        }
        @media (max-width: 1000px) {
            .node-grid { overflow-x: auto; grid-template-columns: repeat(4, 280px); }
            .scroll-hint.show { display: block; }
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

    <div class="grid-container">
        <div id="nodeGrid" class="node-grid">
            <div style="text-align: center; grid-column: 1/-1; padding: 40px;">Loading nodes...</div>
        </div>
        <div id="scrollHint" class="scroll-hint">← → Scroll for more nodes → ←</div>
    </div>

    <div class="debug-panel">
        <div class="debug-header">
            <h2>🐛 Debug Output <span id="journalBadge" class="badge-warning">loading...</span></h2>
            <div class="debug-buttons">
                <button onclick="selectAllLogs()" style="background:#17a2b8;color:white;">📋 Select All</button>
                <button onclick="copyLogsToClipboard(event)" style="background:#28a745;color:white;">📋 Copy</button>
                <button onclick="clearLogs()" style="background:#6c757d;color:white;">🗑️ Clear</button>
            </div>
        </div>
        <div id="debugWindow" class="debug-window">Waiting for logs...</div>
    </div>

    <div class="footer">FGR Controller - Drag ⋮⋮ to reorder nodes</div>

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

        // Log level names
        const logLevelNames = ['DEBUG', 'INFO', 'WARN', 'ERROR'];

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
            // Clear locally immediately for responsiveness
            const debugWindow = document.getElementById('debugWindow');
            if (debugWindow) {
                debugWindow.innerHTML = 'Logs cleared...';
            }
            logBuffer = [];

            // Tell the server to clear its buffer, then reconnect the stream
            fetch('/api/logs/clear', {method: 'POST'})
                .then(() => {
                    // Reconnect the stream to reset version tracking
                    setupLogsStream();
                })
                .catch(e => console.error('Error clearing logs:', e));
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        function appendLogs(newLogs) {
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;

            if (newLogs.length === 0) {
                if (logBuffer.length === 0 && debugWindow.innerHTML === 'Logs cleared...') {
                    return;
                }
                if (logBuffer.length === 0 && debugWindow.innerHTML === '') {
                    debugWindow.innerHTML = 'Waiting for logs...';
                }
                return;
            }

            // If the window shows "Logs cleared..." and we have new logs, append them
            if (debugWindow.innerHTML === 'Logs cleared...') {
                debugWindow.innerHTML = '';
            }

            // If the window is empty, don't add a waiting message, just add logs
            if (debugWindow.innerHTML === '' || debugWindow.innerHTML === 'Waiting for logs...') {
                debugWindow.innerHTML = '';
            }

            const wasAtBottom = debugWindow.scrollHeight - debugWindow.scrollTop - debugWindow.clientHeight < 10;
            newLogs.forEach(log => {
                let className = 'log-ctrl';
                if (log.includes('[NODE]')) className = 'log-node';
                if (log.includes('ERROR')) className = 'log-ctrl';
                const logDiv = document.createElement('div');
                logDiv.className = className;
                logDiv.textContent = log;
                debugWindow.appendChild(logDiv);
            });
            logBuffer = logBuffer.concat(newLogs);
            if (wasAtBottom && autoScrollEnabled) {
                debugWindow.scrollTop = debugWindow.scrollHeight;
            }
        }

        // Simple drag and drop - handle is draggable, not the whole card
        function handleDragStart(e, nodeName) {
            e.dataTransfer.setData('text/plain', nodeName);
            e.dataTransfer.effectAllowed = 'move';
            // Make the drag image transparent
            const dragIcon = document.createElement('div');
            dragIcon.textContent = '⋮⋮';
            dragIcon.style.position = 'absolute';
            dragIcon.style.top = '-1000px';
            document.body.appendChild(dragIcon);
            e.dataTransfer.setDragImage(dragIcon, 0, 0);
            setTimeout(() => document.body.removeChild(dragIcon), 0);
        }

        function handleDragEnd(e) {
            document.querySelectorAll('.node-card').forEach(card => {
                card.classList.remove('drag-over');
            });
        }

        function handleDragOver(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
        }

        function handleDragEnter(e) {
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

        function handleDrop(e, targetNodeName) {
            e.preventDefault();
            const sourceNode = e.dataTransfer.getData('text/plain');
            const targetNode = targetNodeName;

            if (sourceNode && targetNode && sourceNode !== targetNode) {
                const sourceIndex = nodeOrder.indexOf(sourceNode);
                const targetIndex = nodeOrder.indexOf(targetNode);
                if (sourceIndex !== -1 && targetIndex !== -1) {
                    nodeOrder.splice(sourceIndex, 1);
                    nodeOrder.splice(targetIndex, 0, sourceNode);
                    saveNodeOrder();
                    // Refresh the UI
                    fetch('/api/status')
                        .then(response => response.json())
                        .then(status => {
                            buildFullGrid(status);
                        })
                        .catch(e => console.error('Error refreshing UI:', e));
                }
            }

            document.querySelectorAll('.node-card').forEach(card => {
                card.classList.remove('drag-over');
            });
        }

        function saveNodeOrder() {
            fetch('/api/layout', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({order: nodeOrder})
            }).catch(e => console.error('Error saving layout:', e));
        }

        function buildFullGrid(status) {
            const grid = document.getElementById('nodeGrid');
            let html = '';
            for (const node of status.nodes) {
                const isOnline = node.connected;
                const measurement = node.measurement || {};
                const waterHeight = measurement.water_height;
                const essentialClass = node.essential ? 'essential' : 'non-essential';

                html += `
                    <div class="node-card ${isOnline ? 'online' : 'offline'} ${essentialClass}"
                         data-node-name="${node.name}"
                         ondragover="handleDragOver(event)"
                         ondragenter="handleDragEnter(event)"
                         ondragleave="handleDragLeave(event)"
                         ondrop="handleDrop(event, '${node.name}')">
                        <div class="node-header">
                            <div class="node-title">
                                <span class="drag-handle"
                                      draggable="true"
                                      ondragstart="handleDragStart(event, '${node.name}')"
                                      ondragend="handleDragEnd(event)">⋮⋮</span>
                                <span class="node-name">${escapeHtml(node.name)}</span>
                            </div>
                            <span class="node-type">${escapeHtml(node.type || 'unknown')}</span>
                        </div>
                        <div class="node-ip">${escapeHtml(node.ip)}</div>
                        <div class="node-state ${isOnline ? 'online' : 'offline'}">
                            <span class="state-text">${isOnline ? 'online' : 'offline'} - ${escapeHtml(node.state)}</span>
                        </div>
                        <div class="node-metrics">
                            <span class="connection-duration">${node.connection_duration ? '📡 ' + node.connection_duration : ''}</span>
                            <span>📨 <span class="message-count">${node.message_count}</span> 💓 <span class="heartbeat-count">${node.heartbeat_count}</span></span>
                        </div>
                        <div class="node-measurement-container"></div>
                        <div class="node-actions">
                            <button class="btn-query" onclick="sendCommand('${node.name}', 'query_state')">Ping</button>
                            <button class="btn-start" onclick="sendCommand('${node.name}', 'start')">Start</button>
                            <button class="btn-stop" onclick="sendCommand('${node.name}', 'stop')">Stop</button>
                            <button class="btn-reboot" onclick="sendCommand('${node.name}', 'reboot')">Reboot</button>
                        </div>
                        <div class="node-actions-group">
                            <button class="btn-log-start" onclick="sendCommand('${node.name}', 'log_start')">Log On</button>
                            <button class="btn-log-stop" onclick="sendCommand('${node.name}', 'log_stop')">Log Off</button>
                            <select class="log-level-select" data-node-name="${node.name}">
                                <option value="0">DEBUG</option>
                                <option value="1" selected>INFO</option>
                                <option value="2">WARN</option>
                                <option value="3">ERROR</option>
                            </select>
                            <button class="btn-query" onclick="setLogLevelFromSelect('${node.name}')">Set</button>
                        </div>
                    </div>
                `;
            }
            grid.innerHTML = html;

            // Store node data and add measurement displays
            for (const node of status.nodes) {
                nodesData[node.name] = node;
                updateNodeMeasurements(node);
            }

            // Add log level change listeners
            document.querySelectorAll('.log-level-select').forEach(select => {
                select.addEventListener('change', function(e) {
                    e.stopPropagation();
                    const nodeName = this.getAttribute('data-node-name');
                    const level = parseInt(this.value);
                    setLogLevel(nodeName, level);
                });
            });

            gridBuilt = true;
        }

        function updateExistingNodes(status) {
            for (const node of status.nodes) {
                const oldNodeData = nodesData[node.name];
                nodesData[node.name] = node;

                const card = document.querySelector(`.node-card[data-node-name="${node.name}"]`);
                if (!card) continue;

                const isOnline = node.connected;

                // Update classes - set explicitly rather than toggle
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
                    stateSpan.textContent = `${isOnline ? 'online' : 'offline'} - ${node.state}`;
                }

                // Update the node-state div class to match
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

                // Update notification - only show new notifications (based on timestamp)
                const notificationContainer = card.querySelector('.node-state');
                const existingNotification = notificationContainer?.querySelector('.notification');

                // Check if this is a new notification (different timestamp)
                const isNewNotification = node.notification &&
                    (!lastNotificationTimestamp[node.name] ||
                     lastNotificationTimestamp[node.name] !== node.notification.timestamp);

                if (node.notification && isNewNotification) {
                    // Record this notification timestamp
                    lastNotificationTimestamp[node.name] = node.notification.timestamp;

                    const notificationClass = node.notification.is_success === false ? 'notification failure' :
                                             (node.notification.message.includes('◀️') ? 'notification success' : 'notification');

                    // Remove old notification if exists
                    if (existingNotification) {
                        existingNotification.remove();
                    }

                    // Add new notification
                    const newNotification = document.createElement('span');
                    newNotification.className = notificationClass;
                    newNotification.textContent = node.notification.message;
                    notificationContainer.appendChild(newNotification);

                    // Remove after animation
                    setTimeout(() => {
                        if (newNotification.parentNode) newNotification.remove();
                    }, 3000);
                } else if (!node.notification && existingNotification) {
                    existingNotification.remove();
                }

                // Update metrics
                const durationSpan = card.querySelector('.connection-duration');
                if (durationSpan) {
                    durationSpan.textContent = node.connection_duration ? '📡 ' + node.connection_duration : '';
                }
                const messageCountSpan = card.querySelector('.message-count');
                if (messageCountSpan) messageCountSpan.textContent = node.message_count;
                const heartbeatCountSpan = card.querySelector('.heartbeat-count');
                if (heartbeatCountSpan) heartbeatCountSpan.textContent = node.heartbeat_count;

                // Update measurements
                updateNodeMeasurements(node);
            }
        }

        function updateNodeMeasurements(node) {
            const card = document.querySelector(`.node-card[data-node-name="${node.name}"]`);
            if (!card) return;

            const measurement = node.measurement || {};
            const waterHeight = measurement.water_height;
            const measurementContainer = card.querySelector('.node-measurement-container');
            if (measurementContainer) {
                if (node.type === 'level_gauge' && waterHeight !== undefined) {
                    measurementContainer.innerHTML = `
                        <div class="node-measurement">
                            <div class="measurement-value">${waterHeight} <span class="measurement-unit">mm</span></div>
                            <div class="measurement-unit">Water Level</div>
                        </div>`;
                } else if (node.type === 'test' && measurement.value !== undefined) {
                    measurementContainer.innerHTML = `
                        <div class="node-measurement">
                            <div class="measurement-value">${measurement.value} <span class="measurement-unit">value</span></div>
                            <div class="measurement-unit">Last reading</div>
                        </div>`;
                } else if (measurementContainer.innerHTML !== '') {
                    measurementContainer.innerHTML = '';
                }
            }
        }

        function setLogLevelFromSelect(nodeName) {
            const select = document.querySelector(`.log-level-select[data-node-name="${nodeName}"]`);
            if (select) {
                const level = parseInt(select.value);
                setLogLevel(nodeName, level);
            }
        }

        function updateUI(status) {
            const badge = document.getElementById('journalBadge');
            if (badge) {
                badge.textContent = status.journal_enabled ? '✓ Journal Active' : '⚠️ Journal Disabled';
                badge.className = status.journal_enabled ? 'badge-success' : 'badge-warning';
            }

            const banner = document.getElementById('statusBanner');
            const total = status.total_nodes;
            const essential = status.essential_nodes;
            const connected_essential = status.connected_essential_nodes;
            const working_essential = status.working_essential_nodes;

            // Handle case with no essential nodes
            if (essential === 0) {
                banner.className = 'status-banner ready';
                banner.querySelector('.status-icon').textContent = '✅';
                banner.querySelector('.status-text > div:first-child').textContent = 'NO ESSENTIAL NODES CONFIGURED';
                banner.querySelector('.status-details').textContent = `${status.total_nodes} total nodes (all optional)`;
            }
            // Check if all essential nodes are connected AND working
            else if (connected_essential === essential && working_essential === essential) {
                banner.className = 'status-banner ready';
                banner.querySelector('.status-icon').textContent = '✅';
                banner.querySelector('.status-text > div:first-child').textContent = 'ALL ESSENTIAL NODES WORKING';
                banner.querySelector('.status-details').textContent = `${connected_essential}/${essential} essential connected, ${working_essential} working`;
            }
            // Check if at least some essential nodes are working
            else if (working_essential > 0) {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⏳';
                banner.querySelector('.status-text > div:first-child').textContent = 'ESSENTIAL NODES INITIALIZING';
                banner.querySelector('.status-details').textContent = `${connected_essential}/${essential} essential connected, ${working_essential} working`;
            }
            // Check if essential nodes are connected but none working yet
            else if (connected_essential > 0) {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⏳';
                banner.querySelector('.status-text > div:first-child').textContent = 'ESSENTIAL NODES CONNECTING';
                banner.querySelector('.status-details').textContent = `${connected_essential}/${essential} essential connected, waiting for working state`;
            }
            else {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⚠️';
                banner.querySelector('.status-text > div:first-child').textContent = 'WAITING FOR ESSENTIAL NODES';
                banner.querySelector('.status-details').textContent = `0/${essential} essential connected`;
            }

            // Add optional nodes info if any exist
            if (status.total_nodes > essential) {
                const optional_count = status.total_nodes - essential;
                const optional_connected = status.connected_nodes - connected_essential;
                banner.querySelector('.status-details').textContent += ` (${optional_connected}/${optional_count} optional online)`;
            }

            // Update node order from server
            if (status.nodes.length > 0 && nodeOrder.length === 0) {
                nodeOrder = status.nodes.map(n => n.name);
            }

            // Check if grid exists, if not build it
            const grid = document.getElementById('nodeGrid');
            if (!gridBuilt || grid.children.length === 0) {
                buildFullGrid(status);
            } else if (grid.children[0] && grid.children[0].tagName === 'DIV' && grid.children[0].innerText === 'Loading nodes...') {
                buildFullGrid(status);
            } else {
                // Update existing nodes
                updateExistingNodes(status);
            }

            // Check if scrolling is needed
            const gridRect = grid.getBoundingClientRect();
            const scrollHint = document.getElementById('scrollHint');
            if (grid.scrollWidth > grid.clientWidth) {
                scrollHint.classList.add('show');
            } else {
                scrollHint.classList.remove('show');
            }
        }

        async function sendCommand(nodeName, command) {
            try {
                const response = await fetch('/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({node: nodeName, command: command})
                });
                const result = await response.json();
                if (result.status === 'error') {
                    console.error(`[ERROR] ${result.message}`);
                }
            } catch (e) { console.error(`[ERROR] ${e.message}`); }
        }

        async function setLogLevel(nodeName, level) {
            try {
                const response = await fetch('/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({node: nodeName, command: 'log_level', params: {level: parseInt(level)}})
                });
                const result = await response.json();
                if (result.status === 'error') {
                    console.error(`[ERROR] ${result.message}`);
                }
            } catch (e) { console.error(`[ERROR] ${e.message}`); }
        }

        function setupStatusStream() {
            if (statusSource) statusSource.close();
            const source = new EventSource('/api/status/stream');
            source.onmessage = (event) => {
                try { updateUI(JSON.parse(event.data)); }
                catch (e) { console.error("Error parsing status:", e); }
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

            // Handle normal data messages (new logs)
            source.onmessage = (event) => {
                try {
                    const newLogs = JSON.parse(event.data);
                    if (newLogs.length > 0) appendLogs(newLogs);
                } catch (e) { console.error("Error parsing logs:", e); }
            };

            // Handle clear event
            source.addEventListener('clear', (event) => {
                console.log('Logs cleared remotely');
                const debugWindow = document.getElementById('debugWindow');
                if (debugWindow) {
                    debugWindow.innerHTML = 'Logs cleared...';
                }
                logBuffer = [];
                autoScrollEnabled = true;
            });

            // Handle reset event
            source.addEventListener('reset', (event) => {
                console.log('Log stream reset');
                const debugWindow = document.getElementById('debugWindow');
                if (debugWindow && debugWindow.innerHTML === '') {
                    debugWindow.innerHTML = 'Waiting for logs...';
                }
            });

            source.onerror = () => {
                console.log('Logs stream error, reconnecting...');
                source.close();
                setTimeout(setupLogsStream, 5000);
            };
            logsSource = source;
        }

        setupDebugWindow();
        setupStatusStream();
        setupLogsStream();
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