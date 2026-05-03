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

        # Monkey patch the Node class to add connection_time
        if not hasattr(Node, 'connection_time'):
            Node.connection_time = 0.0

        # Log storage for web interface - store as list with version tracking
        self.log_entries: List[Tuple[int, str]] = []  # (version, message)
        self.max_log_entries = MAX_LOG_ENTRIES
        self._log_counter = 0

        # Track the last sent log version per client
        self.client_last_versions: Dict[web.StreamResponse, int] = {}

        # SSE clients
        self.sse_clients = set()

        # Store node-specific data for web display
        self.node_measurements: Dict[str, Dict[str, Any]] = {}

        # Journal reader thread
        self.journal_running = False
        self.journal_thread = None

        # Start journal reader if available
        if HAS_SYSTEMD:
            self._start_journal_reader()
        else:
            self._log_message("Journal reading disabled - node logs will not appear")

        # Override the logger to capture controller logs
        self._setup_log_capture()

        # Record start time
        self._start_time = time.time()

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

    def _get_system_status(self) -> Dict[str, Any]:
        """Get current system status for API"""
        nodes_status = []

        for name, node in self.nodes.items():
            measurement = self.node_measurements.get(name, {})

            # Calculate connection duration with days
            connection_duration = None
            if hasattr(node, 'connection_time') and node.connection_time:
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

            # Determine display state
            if node.sock:
                if node.state == NodeState.STARTED:
                    display_state = "RUNNING"
                elif node.state == NodeState.READY:
                    display_state = "READY"
                elif node.state == NodeState.NEEDS_CFG:
                    display_state = "NEEDS CONFIG"
                else:
                    display_state = node.state.name if isinstance(node.state, NodeState) else str(node.state)
            else:
                display_state = "DISCONNECTED"

            nodes_status.append({
                'name': name,
                'ip': node.ip,
                'type': node.node_type,
                'state': display_state,
                'connected': node.sock is not None,
                'connection_duration': connection_duration,
                'message_count': node.message_count,
                'heartbeat_count': node.heartbeat_count,
                'measurement': measurement
            })

        return {
            'nodes': nodes_status,
            'total_nodes': len(self.nodes),
            'connected_nodes': len([n for n in self.nodes.values() if n.sock]),
            'initialised_nodes': len([n for n in self.nodes.values() if n.sock and n.state not in [NodeState.DISCONNECTED, NodeState.CONNECTED]]),
            'server_uptime': time.time() - self._start_time,
            'journal_enabled': HAS_SYSTEMD
        }

    async def _broadcast_status(self):
        """Broadcast status updates to SSE clients"""
        last_status = None
        while self.web_running:
            current_status = self._get_system_status()
            if current_status != last_status:
                last_status = current_status
                for client in list(self.sse_clients):
                    try:
                        await client.write(f"data: {json.dumps(current_status)}\n\n".encode())
                    except Exception:
                        self.sse_clients.discard(client)
            await asyncio.sleep(1)

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
        # Return just the messages, not the versions
        return web.json_response({'logs': [msg for _, msg in self.log_entries]})

    async def handle_api_logs_stream(self, request):
        """SSE stream for log updates using version tracking"""
        response = web.StreamResponse(
            status=200,
            headers={
                'Content-Type': 'text/event-stream',
                'Cache-Control': 'no-cache',
                'Connection': 'keep-alive',
            }
        )
        await response.prepare(request)

        # Track the last version sent to this client
        last_version = -1

        try:
            # Send all existing logs first
            if self.log_entries:
                all_logs = [msg for _, msg in self.log_entries]
                await response.write(f"data: {json.dumps(all_logs)}\n\n".encode())
                last_version = self.log_entries[-1][0] if self.log_entries else -1

            while self.web_running:
                # Find any new logs since last_version
                new_logs = []
                for version, msg in self.log_entries:
                    if version > last_version:
                        new_logs.append(msg)
                        last_version = version

                if new_logs:
                    await response.write(f"data: {json.dumps(new_logs)}\n\n".encode())

                await asyncio.sleep(0.5)
        except Exception:
            pass

        return response

    async def handle_api_logs_clear(self, request):
        """Clear the log buffer"""
        self.log_entries.clear()
        self._log_counter = 0
        return web.json_response({'status': 'ok'})

    async def handle_api_command(self, request):
        """Handle command requests to nodes"""
        data = await request.json()
        node_name = data.get('node')
        command = data.get('command')

        result = {'status': 'ok', 'message': ''}

        try:
            if command == 'query_state':
                state = self.query_node_state(node_name)
                result['message'] = f"State: {state.name if state else 'unknown'}"
            elif command == 'start':
                if self.start_node(node_name):
                    result['message'] = f"Node {node_name} started"
                else:
                    result['status'] = 'error'
                    result['message'] = f"Failed to start {node_name}"
            elif command == 'stop':
                if self.stop_node(node_name):
                    result['message'] = f"Node {node_name} stopped"
                else:
                    result['status'] = 'error'
                    result['message'] = f"Failed to stop {node_name}"
            elif command == 'reboot':
                if self.reboot_node(node_name):
                    result['message'] = f"Node {node_name} rebooting"
                else:
                    result['status'] = 'error'
                    result['message'] = f"Failed to reboot {node_name}"
            else:
                result['status'] = 'error'
                result['message'] = f"Unknown command: {command}"
        except Exception as e:
            result['status'] = 'error'
            result['message'] = str(e)

        return web.json_response(result)

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
        # Patch the Node class to add connection_time if not present
        if not hasattr(Node, 'connection_time'):
            Node.connection_time = 0.0

        # Override the _accept_loop method to set connection_time
        original_accept = self._accept_loop

        def patched_accept_loop():
            # Call original accept method
            original_accept()

        # We need to monkey patch the connection handling
        # Instead, let's override the method where nodes are accepted

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
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
            background: #f5f5f5;
        }
        h1 { color: #333; margin-bottom: 5px; }
        .subtitle { color: #666; margin-top: 0; margin-bottom: 20px; }

        .status-banner {
            background: #fff3e0;
            border-left: 4px solid #ffc107;
            padding: 12px 20px;
            margin-bottom: 20px;
            border-radius: 8px;
            display: flex;
            align-items: center;
            gap: 12px;
            font-weight: 500;
        }
        .status-banner.ready { background: #e8f5e9; border-left-color: #2e7d32; }
        .status-banner.waiting { background: #fff3e0; border-left-color: #ffc107; }
        .status-icon { font-size: 24px; }
        .status-text { flex: 1; }
        .status-details { font-size: 12px; color: #666; margin-top: 4px; }

        .node-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .node-card {
            background: white;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .node-card.online { border-left: 4px solid #4caf50; }
        .node-card.offline { border-left: 4px solid #f44336; opacity: 0.7; }
        .node-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }
        .node-name { font-size: 18px; font-weight: bold; color: #333; }
        .node-type { font-size: 12px; color: #888; background: #f0f0f0; padding: 4px 8px; border-radius: 20px; }
        .node-ip { font-family: monospace; font-size: 12px; color: #666; margin-bottom: 8px; }
        .node-state { font-size: 14px; margin-bottom: 8px; }
        .node-state.online { color: #4caf50; }
        .node-state.offline { color: #f44336; }
        .node-metrics {
            font-size: 12px;
            color: #888;
            margin-bottom: 12px;
            border-top: 1px solid #eee;
            padding-top: 8px;
        }
        .node-measurement {
            background: #e3f2fd;
            border-radius: 8px;
            padding: 10px;
            margin-bottom: 12px;
            text-align: center;
        }
        .measurement-value { font-size: 28px; font-weight: bold; color: #1976d2; }
        .measurement-unit { font-size: 14px; color: #666; }
        .node-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        button {
            padding: 6px 12px;
            font-size: 12px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            transition: all 0.2s;
        }
        button:hover { opacity: 0.8; transform: translateY(-1px); }
        .btn-query { background: #2196f3; color: white; }
        .btn-start { background: #4caf50; color: white; }
        .btn-stop { background: #ff9800; color: white; }
        .btn-reboot { background: #f44336; color: white; }

        .debug-panel {
            background: white;
            border-radius: 8px;
            padding: 20px;
            margin-top: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .debug-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
            flex-wrap: wrap;
            gap: 8px;
        }
        .debug-header h2 { margin: 0; color: #555; }
        .debug-buttons { display: flex; gap: 8px; }
        .debug-window {
            background: #1e1e1e;
            color: #d4d4d4;
            font-family: 'Courier New', monospace;
            font-size: 12px;
            padding: 10px;
            border-radius: 5px;
            height: 300px;
            overflow-y: auto;
        }
        .debug-window .log-ctrl { color: #4ec9b0; }
        .debug-window .log-node { color: #9cdcfe; }
        .footer {
            text-align: center;
            color: #999;
            font-size: 12px;
            margin-top: 20px;
        }
        .badge-success { background: #4caf50; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; }
        .badge-warning { background: #ff9800; color: white; padding: 2px 6px; border-radius: 4px; font-size: 10px; }
    </style>
</head>
<body>
    <h1>🚂 FGR Controller</h1>
    <div class="subtitle">Front Garden Railway - Node Monitoring & Control</div>

    <div id="statusBanner" class="status-banner waiting">
        <div class="status-icon">⏳</div>
        <div class="status-text">
            <div>System Status: Initializing...</div>
            <div class="status-details">Waiting for nodes to connect</div>
        </div>
    </div>

    <div id="nodeGrid" class="node-grid">
        <div style="text-align: center; grid-column: 1/-1; padding: 40px;">Loading nodes...</div>
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

    <div class="footer">FGR Controller</div>

    <script>
        let statusSource = null;
        let logsSource = null;
        let autoScrollEnabled = true;
        let scrollTimeout = null;
        let logBuffer = [];

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
            fetch('/api/logs/clear', {method: 'POST'})
                .then(() => { document.getElementById('debugWindow').innerHTML = 'Logs cleared...'; logBuffer = []; })
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
                if (logBuffer.length === 0) {
                    debugWindow.innerHTML = 'Logs cleared...';
                }
                return;
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
            if (wasAtBottom) debugWindow.scrollTop = debugWindow.scrollHeight;
        }

        function updateUI(status) {
            const badge = document.getElementById('journalBadge');
            if (badge) {
                badge.textContent = status.journal_enabled ? '✓ Journal Active' : '⚠️ Journal Disabled';
                badge.className = status.journal_enabled ? 'badge-success' : 'badge-warning';
            }

            const banner = document.getElementById('statusBanner');
            const total = status.total_nodes;
            const connected = status.connected_nodes;
            const initialised = status.initialised_nodes;
            if (initialised === total && total > 0) {
                banner.className = 'status-banner ready';
                banner.querySelector('.status-icon').textContent = '✅';
                banner.querySelector('.status-text > div:first-child').textContent = 'System Status: ALL NODES READY';
                banner.querySelector('.status-details').textContent = `${connected}/${total} connected, ${initialised} initialised`;
            } else if (connected > 0) {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⏳';
                banner.querySelector('.status-text > div:first-child').textContent = 'System Status: Initializing...';
                banner.querySelector('.status-details').textContent = `${connected}/${total} connected, ${initialised} initialised`;
            } else {
                banner.className = 'status-banner waiting';
                banner.querySelector('.status-icon').textContent = '⚠️';
                banner.querySelector('.status-text > div:first-child').textContent = 'System Status: Waiting for nodes';
                banner.querySelector('.status-details').textContent = 'No nodes connected yet.';
            }

            const grid = document.getElementById('nodeGrid');
            if (status.nodes.length === 0) {
                grid.innerHTML = '<div style="text-align: center; grid-column: 1/-1; padding: 40px;">No nodes configured. Add nodes to nodes.json</div>';
                return;
            }

            let html = '';
            for (const node of status.nodes) {
                const isOnline = node.connected;
                const measurement = node.measurement || {};
                const waterHeight = measurement.water_height;
                html += `
                    <div class="node-card ${isOnline ? 'online' : 'offline'}">
                        <div class="node-header">
                            <span class="node-name">${escapeHtml(node.name)}</span>
                            <span class="node-type">${escapeHtml(node.type || 'unknown')}</span>
                        </div>
                        <div class="node-ip">${escapeHtml(node.ip)}</div>
                        <div class="node-state ${isOnline ? 'online' : 'offline'}">
                            ${isOnline ? '🟢 Online' : '🔴 Offline'} - ${node.state}
                        </div>
                        <div class="node-metrics">
                            Connected: ${node.connection_duration || 'N/A'} |
                            Messages: ${node.message_count} |
                            Heartbeats: ${node.heartbeat_count}
                        </div>`;
                if (node.type === 'level_gauge' && waterHeight !== undefined) {
                    html += `
                        <div class="node-measurement">
                            <div class="measurement-value">${waterHeight} <span class="measurement-unit">mm</span></div>
                            <div class="measurement-unit">Water Level</div>
                        </div>`;
                }
                html += `
                        <div class="node-actions">
                            <button class="btn-query" onclick="sendCommand('${node.name}', 'query_state')">❓ Query State</button>
                            <button class="btn-start" onclick="sendCommand('${node.name}', 'start')">▶️ Start</button>
                            <button class="btn-stop" onclick="sendCommand('${node.name}', 'stop')">⏹️ Stop</button>
                            <button class="btn-reboot" onclick="sendCommand('${node.name}', 'reboot')">🔄 Reboot</button>
                        </div>
                    </div>
                `;
            }
            grid.innerHTML = html;
        }

        async function sendCommand(nodeName, command) {
            try {
                const response = await fetch('/api/command', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({node: nodeName, command: command})
                });
                const result = await response.json();
                if (result.status === 'error') console.error(`[ERROR] ${result.message}`);
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
            source.onmessage = (event) => {
                try {
                    const newLogs = JSON.parse(event.data);
                    if (newLogs.length > 0) appendLogs(newLogs);
                } catch (e) { console.error("Error parsing logs:", e); }
            };
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


class LevelGaugeHandler(NodeHandler):
    """Handler for level gauge nodes"""

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        ind_type = msg.subtype
        if ind_type == 0x100:  # Level reading
            level_mm = msg.error_or_state
            self.logger.info(f"Level gauge {self.node.name} reading: {level_mm} mm from top")
            water_height = RESERVOIR_DEPTH - level_mm
            if hasattr(self.controller, 'update_node_measurement'):
                self.controller.update_node_measurement(self.node.name, {
                    'level': level_mm,
                    'water_height': max(0, water_height),
                    'type': 'level_gauge'
                })
            return True
        return super().on_indication(msg)


class TestHandler(NodeHandler):
    """Handler for test nodes"""

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        ind_type = msg.subtype
        if ind_type > fgr.FGRIndRsp.FGR_IND_RSP_LAST:
            self.logger.info(f"Test node {self.node.name} sent value: {msg.error_or_state}")
            if hasattr(self.controller, 'update_node_measurement'):
                self.controller.update_node_measurement(self.node.name, {
                    'value': msg.error_or_state,
                    'type': 'test'
                })
            return True
        return super().on_indication(msg)


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

    # Register handlers if not loaded from files
    if 'level_gauge' not in controller.node_handlers:
        controller.node_handlers['level_gauge'] = LevelGaugeHandler
    if 'test' not in controller.node_handlers:
        controller.node_handlers['test'] = TestHandler

    print(f"\n{'='*60}")
    print("FGR Controller with Web Interface")
    print(f"{'='*60}")
    print(f"Controller listening on: {args.ip}:{args.port}")
    print(f"Web interface: http://0.0.0.0:{args.http_port}")
    print(f"Journal identifier: {JOURNAL_IDENTIFIER}")
    print(f"Configured nodes: {controller.get_node_names()}")
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
