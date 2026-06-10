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
import sys
import argparse
print("Importing asyncio: may take some time...", flush=True)
import asyncio
import json
import threading
import signal
import re
import logging
import sqlite3
import systemd.journal
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
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

# Metrics display configuration
METRICS_CONFIG = {
    'lrb':     {'type': 'event', 'importance_condition': 'value > 0', 'order': 1, 'display_format': 'hex'},
    'panic':   {'type': 'event', 'importance_condition': 'value > 0', 'order': 2, 'display_format': 'decimal'},
    'pwr':     {'type': 'event', 'importance_condition': 'value > 0', 'order': 3, 'display_format': 'decimal'},
    'w':       {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 4, 'display_format': 'decimal'},
    'ip':      {'type': 'event', 'importance_condition': 'special_ip', 'order': 5, 'display_format': 'decimal'},
    'dbm':     {'type': 'exclude', 'display_format': 'decimal'},
    'ota_c':   {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 7, 'display_format': 'decimal'},
    'ota_w':   {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 8, 'display_format': 'decimal'},
    'log_c':   {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 9, 'display_format': 'decimal'},
    'cnt_c':   {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 10, 'display_format': 'decimal'},
    'cnt_tx':  {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 11, 'display_format': 'decimal'},
    'cnt_rx':  {'type': 'event', 'importance_condition': 'has_fail', 'order': 12, 'display_format': 'decimal'},
    'ping_tx': {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 13, 'display_format': 'decimal'},
    'ping_rx': {'type': 'event', 'importance_condition': 'has_fail', 'order': 14, 'display_format': 'decimal'},
    'nvs_w':   {'type': 'boolean_event', 'importance_condition': 'has_fail', 'order': 15, 'display_format': 'decimal'},
    'stack':   {'type': 'stack', 'importance_condition': 'first_value < 256', 'order': 16, 'display_format': 'decimal'},
    'heap':    {'type': 'simple', 'importance_threshold': 10000, 'order': 17, 'display_format': 'decimal'}
}

# Human-readable help text for metrics (for tooltips)
METRICS_HELP = {
    'lrb': 'Local ReBoot event - the last time (since power-up) of, and a count of, the node restarting itself (reboots commanded by the controller are not counted)',
    'panic': 'Panic event - the last time (since power-up) of, and a count of, software panic(s)',
    'pwr': 'Power problem event - the last time (since power-up) of, and a count of, brown-outs or power glitches',
    'w': 'WiFi connection event - the last time (since boot) of and a count of (+) successful, (-) failed, connections',
    'ip': 'IP address acquisition events - the last time (since boot) of and a count of IP address acqusition - the count should be the same as WiFi connection successes',
    'dbm': 'WiFi signal strength in dBm',
    'ota_c': 'OTA connection events - the last time (since power-up) of and a count of successful (+) and failed (-) connections to the OTA server',
    'ota_w': 'OTA write events - the last time (since power-up) of and a count of successful (+) and failed (-) actual OTA updates (i.e. a new version of code was loaded)',
    'log_c': 'Log connection events- the last time (since boot) of and a count of  successful (+) and failed (-) log server connections',
    'cnt_c': 'Controller connection events - the last time (since boot) of and a count of successful (+) and failed (-) connections to the controller',
    'cnt_tx': 'Controller transmit events - the last time (since boot) of and a count of successful (+) and failed (-) transmits to the controller, value is total bytes transmitted since boot',
    'cnt_rx': 'Controller receive events - the last time (since boot) of and a count of receives from the controller, value is total bytes received since boot',
    'ping_tx': 'Ping transmit events - the last time (since boot) of and a count of successful (+) and failed (-) pings sent to the controller',
    'ping_rx': 'Ping receive events - the last time (since boot) of and a count of successful (+) and failed (-) pings received from the controller',
    'nvs_w': 'NVS write events - the last time (since boot) of and a count of successful (+) and failed (-) writes to non-volatile storage',
    'stack': 'The three tasks with the lowest minimum free stack values, in bytes',
    'heap': 'The minimum free heap memory in bytes'
}

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

def linkify_log_line(text):
    # This matches the full URL structure and ensures it keeps capturing
    # until a character that doesn't fit a URL (like a space or closing bracket)
    url_pattern = r'(http://[\d\.]+:[\d]+/\d+_[0-9\.]+)'

    return re.sub(url_pattern, r'<a href="\1" target="_blank">\1</a>', text)

class WebController(Controller):
    """Web-enabled FGR Controller with journal log reading"""

    def __init__(self, listen_ip: str = CONTROLLER_IP_DEFAULT,
                 port: int = CONTROLLER_PORT_DEFAULT,
                 nodes_dir: str = None, cfg_file: str = None,
                 http_port: int = HTTP_PORT_DEFAULT,
                 db_path: Path = None):
        super().__init__(listen_ip, port, nodes_dir, cfg_file)

        self.http_port = http_port

        # Determine expected systemd unit for this service
        self.script_name = Path(sys.argv[0]).stem  # Gets "web_controller" from "web_controller.py"
        self.controller_unit = f"{self.script_name}.service"

        # Log storage for web interface - store as list with version tracking
        self.log_entries: List[Tuple[int, str]] = []  # (version, message)
        self.max_log_entries = MAX_LOG_ENTRIES
        self._log_counter = 0

        self.db_path = db_path
        self.graphs_enabled = db_path is not None and db_path.exists()
        if not self.graphs_enabled:
            self._log_message(f"Graphs disabled - database not found at {db_path}")
        else:
            self._log_message(f"Graphs enabled using database: {db_path}")

        self.web_app = None
        self.web_runner = None
        self.web_running = False

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

        # Store metrics for each node
        self.node_metrics: Dict[str, Dict[str, Any]] = {}

        self.graph_cache = {}  # Simple in-memory cache
        self.graph_cache_timeout = 300  # 5 minutes

        # Initialize metrics history table for fast queries
        if self.graphs_enabled:
            self._init_metrics_history()

        # Record start time
        self._start_time = time.time()

    def _init_metrics_history(self):
        """Initialize the metrics_history table for fast queries"""
        print("_init_metrics_history: Starting...")

        conn = self._get_metrics_db_connection()
        if not conn:
            print("_init_metrics_history: No database connection")
            return

        try:
            cursor = conn.cursor()

            # Create table
            print("Creating metrics_history table if not exists...")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics_history (
                    epoch_time INTEGER NOT NULL,
                    node_ip TEXT NOT NULL,
                    rssi INTEGER,
                    heap INTEGER,
                    wifi_failures INTEGER,
                    panics INTEGER,
                    ctrl_disconnects INTEGER,
                    log_disconnects INTEGER,
                    PRIMARY KEY (epoch_time, node_ip)
                )
            """)

            # Get source count
            cursor.execute("SELECT COUNT(*) FROM device_logs WHERE message_type = 'METRIC'")
            source_count = cursor.fetchone()[0]
            print(f"Source device_logs has {source_count} metric rows")

            # Check current row count
            cursor.execute("SELECT COUNT(*) FROM metrics_history")
            current_count = cursor.fetchone()[0]
            print(f"metrics_history has {current_count} rows")

            # Backfill if missing more than 10% of data
            if current_count < source_count * 0.9:
                print(f"Starting backfill (have {current_count}, need {source_count})...")

                # Clear the table first to avoid duplicates
                cursor.execute("DELETE FROM metrics_history")
                conn.commit()

                # Insert in batches to avoid memory issues
                batch_size = 50000
                offset = 0

                while offset < source_count:
                    # Use INSERT OR REPLACE to handle duplicates, and take the latest values
                    cursor.execute(f"""
                        INSERT OR REPLACE INTO metrics_history (epoch_time, node_ip, rssi, heap, wifi_failures, panics, ctrl_disconnects, log_disconnects)
                        SELECT
                            epoch_time,
                            node_ip,
                            json_extract(substr(message, instr(message, '{{')), '$.dbm'),
                            json_extract(substr(message, instr(message, '{{')), '$.heap'),
                            json_extract(substr(message, instr(message, '{{')), '$.w.-.n'),
                            json_extract(substr(message, instr(message, '{{')), '$.panic.n'),
                            json_extract(substr(message, instr(message, '{{')), '$.cnt_c.-.n'),
                            json_extract(substr(message, instr(message, '{{')), '$.log_c.-.n')
                        FROM device_logs
                        WHERE message_type = 'METRIC'
                        GROUP BY epoch_time, node_ip
                        LIMIT {batch_size} OFFSET {offset}
                    """)
                    conn.commit()
                    offset += batch_size
                    print(f"Backfilled {offset}/{source_count} rows")

                print("Backfill complete")

            # Create indexes (only if they don't exist)
            print("Creating indexes...")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_epoch ON metrics_history(epoch_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_node_ip ON metrics_history(node_ip)")

            # Create trigger for new inserts
            print("Creating trigger...")
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS update_metrics_history
                AFTER INSERT ON device_logs
                WHEN NEW.message_type = 'METRIC'
                BEGIN
                    INSERT OR REPLACE INTO metrics_history (
                        epoch_time, node_ip, rssi, heap,
                        wifi_failures, panics, ctrl_disconnects, log_disconnects
                    ) VALUES (
                        NEW.epoch_time,
                        NEW.node_ip,
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.dbm'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.heap'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.w.-.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.panic.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.cnt_c.-.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.log_c.-.n')
                    );
                END
            """)

            # Verify final count
            cursor.execute("SELECT COUNT(*) FROM metrics_history")
            final_count = cursor.fetchone()[0]
            print(f"metrics_history final count: {final_count} rows")

            conn.commit()
            print("_init_metrics_history: Complete")

        except Exception as e:
            print(f"Error initializing metrics_history: {e}")
            import traceback
            traceback.print_exc()
        finally:
            conn.close()

    def _trim_metrics_history(self, days=30):
        """Remove metrics older than specified days"""
        conn = self._get_metrics_db_connection()
        if not conn:
            return

        try:
            cursor = conn.cursor()
            cutoff = time.time() - (days * 86400)
            cursor.execute("DELETE FROM metrics_history WHERE epoch_time < ?", (cutoff,))
            conn.commit()
            if cursor.rowcount > 0:
                self._log_message(f"Trimmed {cursor.rowcount} rows from metrics_history older than {days} days")
        except Exception as e:
            self._log_message(f"Error trimming metrics_history: {e}")
        finally:
            conn.close()

    def _start_metrics_trimmer(self):
        """Start background thread to trim old metrics daily"""
        def trimmer_loop():
            while self.web_running:
                # Trim once per day (86400 seconds)
                time.sleep(86400)
                if self.graphs_enabled:
                    self._trim_metrics_history()

        trimmer_thread = threading.Thread(target=trimmer_loop, daemon=True)
        trimmer_thread.start()
        self._log_message("Metrics trimmer started (runs daily)")

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

    def _add_log(self, prefix: str, message: str, journal_ts: float = None):
        """Add a log message to the buffer with automatic trimming"""
        if journal_ts:
            dt = datetime.fromtimestamp(journal_ts)
            timestamp = dt.strftime('%H:%M:%S')
        else:
            timestamp = datetime.now().strftime('%H:%M:%S')

        version = self._log_counter
        self._log_counter += 1
        linkified_message = linkify_log_line(message)
        formatted_message = f"[{timestamp}] {prefix} {linkified_message}"

        # Store with timestamp for streaming
        self.log_entries.append((version, formatted_message, journal_ts))

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
        """Capture a controller log message and send to journal"""
        # Extract just the message part
        if ' - ' in message:
            parts = message.split(' - ', 2)
            msg_text = parts[2] if len(parts) >= 3 else message
        else:
            msg_text = message

        # Send to journal
        self._log_message(msg_text)

    def _log_message(self, message: str):
        """Add a message to the journal"""
        systemd.journal.send(
            f"{message}",
            SYSLOG_IDENTIFIER=JOURNAL_IDENTIFIER,
            PRIORITY=6
        )

    def _start_journal_reader(self):
        """Start background thread to read logs from journal"""
        self.journal_running = True
        self.journal_thread = threading.Thread(target=self._journal_reader_loop, daemon=True)
        self.journal_thread.start()
        self._log_message(f"Journal reader started, monitoring '{JOURNAL_IDENTIFIER}'")

    def _stop_journal_reader(self):
        """Stop the journal reader thread"""
        self._log_message("Stopping journal reader...")
        self.journal_running = False

        # Give the thread time to notice the flag and exit
        if self.journal_thread and self.journal_thread.is_alive():
            self._log_message("Waiting for journal reader thread to exit...")
            self.journal_thread.join(timeout=1.0)
            if self.journal_thread.is_alive():
                self._log_message("Journal reader thread still alive (daemon will kill it)")
            else:
                self._log_message("Journal reader thread exited cleanly")

    def _journal_reader_loop(self):
        """Background thread to read logs from systemd journal"""
        try:
            # Open journal reader
            j = journal.Reader(path='/var/log/journal')
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

            backfill_count = 0
            for entry in j:
                if not self.journal_running:
                    break
                message = entry.get('MESSAGE', '')
                if message:
                    message = message.rstrip()
                    cursor = entry.get('__CURSOR', 'unknown')
                    backfill_count += 1

                    journal_ts = entry.get('__REALTIME_TIMESTAMP')
                    journal_ts_value = journal_ts.timestamp() if journal_ts else None

                    # Determine prefix based on syslog identifier
                    unit = entry.get('_SYSTEMD_UNIT', '')
                    if unit == self.controller_unit:
                        prefix = '[CTRL]'
                    else:
                        prefix = '[NODE]'
                        # Parse metrics from new logs (only for NODE logs)
                        metrics_result = self._parse_metrics_from_log(message)
                        if metrics_result:
                            node_ip, metrics_data = metrics_result
                            self._update_node_metrics(node_ip, metrics_data)
                    self._add_log(prefix, message, journal_ts_value)

            # Now follow new entries using the cursor
            if last_cursor:
                j.seek_cursor(last_cursor)
                j.get_next(1)

            # Use a short timeout to allow checking the running flag frequently
            append_count = 0
            while self.journal_running:
                # Use a very short timeout (100ms) to be responsive to stop signal
                # wait() takes microseconds: 100,000 microseconds = 0.1 seconds
                ret = j.wait(100000)  # 0.1 second timeout

                if not self.journal_running:
                    break

                if ret == journal.APPEND:  # New entries available
                    for entry in j:
                        if not self.journal_running:
                            break
                        message = entry.get('MESSAGE', '')
                        if message:
                            message = message.rstrip()
                            cursor = entry.get('__CURSOR', 'unknown')
                            append_count += 1

                            journal_ts = entry.get('__REALTIME_TIMESTAMP')
                            journal_ts_value = journal_ts.timestamp() if journal_ts else None

                            # Determine prefix based on syslog identifier
                            unit = entry.get('_SYSTEMD_UNIT', '')
                            if unit == self.controller_unit:
                                prefix = '[CTRL]'
                            else:
                                prefix = '[NODE]'
                                # Parse metrics from new logs (only for NODE logs)
                                metrics_result = self._parse_metrics_from_log(message)
                                if metrics_result:
                                    node_ip, metrics_data = metrics_result
                                    self._update_node_metrics(node_ip, metrics_data)
                            self._add_log(prefix, message, journal_ts_value)

        except Exception as e:
            self._log_message(f"Journal reader error: {e}")
        finally:
            self._log_message("Journal reader stopped")

    def _get_node_name_by_ip(self, ip: str) -> Optional[str]:
        """Get node name from IP address"""
        for name, node in self.nodes.items():
            if node.ip == ip:
                return name
        self._log_message(f"Could not find node name for IP: {ip}")  # Debug line
        self._log_message(f"Available node IPs: {[node.ip for node in self.nodes.values()]}")  # Debug line
        return None

    def _format_duration_compact(self, seconds: int) -> str:
        """Format seconds as compact duration (e.g., '4h 16m 3s')"""
        if seconds == 0:
            return "0s"
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if secs > 0 or not parts:
            parts.append(f"{secs}s")
        return ' '.join(parts)

    def _parse_metrics_from_log(self, log_line: str) -> Optional[Tuple[str, dict]]:
        """Extract metrics JSON from log line and return (node_ip, metrics_dict)"""
        # Look for "metrics:" pattern
        if 'metrics:' not in log_line:
            return None

        # Extract node IP - log line starts with [IP] after [NODE] is stripped
        node_match = re.search(r'^\[([0-9.]+)\]', log_line)
        if not node_match:
            return None
        node_ip = node_match.group(1)

        # Find the start of the JSON
        metrics_pos = log_line.find('metrics:')
        if metrics_pos == -1:
            return None

        # Get everything after 'metrics:'
        after_metrics = log_line[metrics_pos + 8:]  # Skip 'metrics:'

        # Find the first '{'
        json_start = after_metrics.find('{')
        if json_start == -1:
            return None

        # Extract the JSON string
        json_str = after_metrics[json_start:]

        # Parse JSON
        try:
            metrics_data = json.loads(json_str)
            return (node_ip, metrics_data)
        except json.JSONDecodeError as e:
            return None

    def _check_metric_importance(self, key: str, data: dict, all_metrics: dict) -> bool:
        """Check if a metric should be highlighted (bold/black)"""
        config = METRICS_CONFIG.get(key, {})
        condition = config.get('importance_condition', '')

        if condition == 'value > 0':
            # Simple event: check if value (n) > 0
            return data.get('n', 0) > 0

        elif condition == 'has_fail':
            # Check for fail events (-)
            if isinstance(data, dict):
                return '-' in data and data['-'].get('n', 0) > 0
            return False

        elif condition == 'special_ip':
            # Compare ip count with w success count
            ip_count = data.get('n', 0)
            w_data = all_metrics.get('w', {})
            if '+' in w_data:
                w_success_count = w_data['+'].get('n', 0)
                return ip_count != w_success_count
            return ip_count > 0  # If no w data, highlight if ip has any count

        elif condition == 'first_value < 256':
            # Stack: check first task's value
            if isinstance(data, list) and len(data) > 0:
                first_item = data[0]
                if isinstance(first_item, dict):
                    first_value = list(first_item.values())[0]
                    return first_value < 256
            return False

        elif key == 'heap' and config.get('type') == 'simple':
            threshold = config.get('importance_threshold', 10000)
            return data < threshold

        return False

    def _format_number(self, value: int, display_format: str) -> str:
        """Format a number according to display format (hex or decimal)"""
        if display_format == 'hex':
            return f"0x{value:X}"
        else:  # decimal (default)
            return str(value)

    def _format_simple_metric(self, key: str, value: int, is_important: bool) -> str:
        """Format a simple metric (just key: value)"""
        # Get display format from config
        config = METRICS_CONFIG.get(key, {})
        display_format = config.get('display_format', 'decimal')

        help_text = METRICS_HELP.get(key, '')
        formatted_value = self._format_number(value, display_format)

        if is_important:
            return f'<span class="metric-important" title="{help_text}">{key}: {formatted_value}</span>'
        else:
            return f'<span class="metric-normal" title="{help_text}">{key}: {formatted_value}</span>'

    def _format_event_metric(self, key: str, data: dict, is_important: bool) -> Optional[str]:
        """Format an event metric (single timestamp counter, optional value)"""
        # Skip if count is zero
        if data.get('n', 0) == 0:
            return None

        # Get display format from config
        config = METRICS_CONFIG.get(key, {})
        display_format = config.get('display_format', 'decimal')

        # Determine timestamp type (tb or tp)
        timestamp_type = 'tb' if 'tb' in data else 'tp' if 'tp' in data else None
        if timestamp_type:
            seconds = data[timestamp_type]
            duration = self._format_duration_compact(seconds)
        else:
            duration = "0s"

        count = data.get('n', 0)
        value = data.get('v', 0)

        help_text = METRICS_HELP.get(key, '')

        if value != 0:
            formatted_value = self._format_number(value, display_format)
            display = f"{duration} n {count} v {formatted_value}"
        else:
            display = f"{duration} n {count}"

        if is_important:
            return f'<span class="metric-important" title="{help_text}">{key}: {display}</span>'
        else:
            return f'<span class="metric-normal" title="{help_text}">{key}: {display}</span>'

    def _format_boolean_event_metric(self, key: str, data: dict, is_important: bool) -> Optional[str]:
        """Format a boolean event metric (+ and/or - events)"""
        # Get display format from config
        config = METRICS_CONFIG.get(key, {})
        display_format = config.get('display_format', 'decimal')

        parts = []

        # Format success events (+)
        if '+' in data:
            plus_data = data['+']
            if plus_data.get('n', 0) > 0:
                timestamp_type = 'tb' if 'tb' in plus_data else 'tp' if 'tp' in plus_data else None
                if timestamp_type:
                    seconds = plus_data[timestamp_type]
                    duration = self._format_duration_compact(seconds)
                else:
                    duration = "0s"
                count = plus_data.get('n', 0)
                value = plus_data.get('v', 0)
                if value != 0:
                    formatted_value = self._format_number(value, display_format)
                    parts.append(f"+ {duration} n {count} v {formatted_value}")
                else:
                    parts.append(f"+ {duration} n {count}")

        # Format fail events (-)
        if '-' in data:
            minus_data = data['-']
            if minus_data.get('n', 0) > 0:
                timestamp_type = 'tb' if 'tb' in minus_data else 'tp' if 'tp' in minus_data else None
                if timestamp_type:
                    seconds = minus_data[timestamp_type]
                    duration = self._format_duration_compact(seconds)
                else:
                    duration = "0s"
                count = minus_data.get('n', 0)
                value = minus_data.get('v', 0)
                if value != 0:
                    formatted_value = self._format_number(value, display_format)
                    parts.append(f"- {duration} n {count} v {formatted_value}")
                else:
                    parts.append(f"- {duration} n {count}")

        if not parts:
            return None

        help_text = METRICS_HELP.get(key, '')
        display = ' '.join(parts)

        if is_important:
            return f'<span class="metric-important" title="{help_text}">{key}: {display}</span>'
        else:
            return f'<span class="metric-normal" title="{help_text}">{key}: {display}</span>'

    def _format_stack_metric(self, key: str, data: list, is_important: bool) -> Optional[str]:
        """Format stack metric (array of task:value objects)"""
        if not isinstance(data, list) or len(data) == 0:
            return None

        task_parts = []
        for item in data:
            if isinstance(item, dict):
                for task_name, value in item.items():
                    task_parts.append(f"{task_name} {value}")

        if not task_parts:
            return None

        help_text = METRICS_HELP.get(key, '')
        display = ' '.join(task_parts)

        if is_important:
            return f'<span class="metric-important" title="{help_text}">{key}: {display}</span>'
        else:
            return f'<span class="metric-normal" title="{help_text}">{key}: {display}</span>'

    def _format_metrics_display(self, node_ip: str, metrics: dict) -> Tuple[str, dict]:
        """Format metrics for display, return (display_html, importance_map)"""
        formatted_parts_important = []  # Store important metrics first
        formatted_parts_normal = []     # Store normal metrics after
        importance_map = {}

        # Get all metrics that are not excluded and have configuration
        for key, config in sorted(METRICS_CONFIG.items(), key=lambda x: x[1].get('order', 999)):
            if config.get('type') == 'exclude':
                continue

            if key not in metrics:
                continue

            metric_data = metrics[key]

            # Check importance BEFORE formatting
            is_important = self._check_metric_importance(key, metric_data, metrics)
            importance_map[key] = is_important

            # Format based on type
            formatted = None
            if config['type'] == 'simple':
                if isinstance(metric_data, (int, float)):
                    formatted = self._format_simple_metric(key, metric_data, is_important)
            elif config['type'] == 'event':
                if isinstance(metric_data, dict):
                    formatted = self._format_event_metric(key, metric_data, is_important)
            elif config['type'] == 'boolean_event':
                if isinstance(metric_data, dict):
                    formatted = self._format_boolean_event_metric(key, metric_data, is_important)
            elif config['type'] == 'stack':
                if isinstance(metric_data, list):
                    formatted = self._format_stack_metric(key, metric_data, is_important)

            if formatted:
                if is_important:
                    formatted_parts_important.append(formatted)
                else:
                    formatted_parts_normal.append(formatted)

        # Combine: important first, then normal
        all_formatted_parts = formatted_parts_important + formatted_parts_normal

        if not all_formatted_parts:
            return '<span class="metric-normal">Waiting for metrics data...</span>', importance_map

        # Join with separators
        display_html = ' | '.join(all_formatted_parts)
        return display_html, importance_map

    def _update_node_metrics(self, node_ip: str, metrics: dict):
        """Update stored metrics for a node"""
        node_name = self._get_node_name_by_ip(node_ip)
        if node_name:
            display_html, importance = self._format_metrics_display(node_ip, metrics)
            self.node_metrics[node_name] = {
                'display': display_html,
                'importance': importance,
                'raw': metrics,
                'last_update': time.time()
            }
            # Also store for quick access by IP
            self.node_metrics[f"__ip_{node_ip}"] = self.node_metrics[node_name]

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

            # Get metrics data
            metrics_data = self.node_metrics.get(name, {})
            metrics_display = metrics_data.get('display', '<span class="metric-normal">Waiting for metrics data...</span>')

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
                'rssi': node.rssi if node.rssi is not None else '?',
                'metrics_display': metrics_display,  # Add this line
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
                    for version, msg, ts in self.log_entries:
                        if version > last_version:
                            new_logs.append({"message": msg, "timestamp": ts})
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

    async def handle_api_journal_time_range(self, request):
        """Get the earliest and latest timestamps available in the journal (UTC)"""

        if not HAS_SYSTEMD:
            return web.json_response({'error': 'Journal not available'}, status=503)

        try:
            def _get_range():
                j = journal.Reader(path='/var/log/journal')
                j.add_match(SYSLOG_IDENTIFIER=JOURNAL_IDENTIFIER)

                # Get earliest timestamp
                j.seek_head()
                earliest = None
                earliest_count = 0
                for entry in j:
                    earliest_count += 1
                    if entry.get('SYSLOG_IDENTIFIER') == JOURNAL_IDENTIFIER:
                        earliest_ts = entry.get('__REALTIME_TIMESTAMP')
                        if earliest_ts:
                            earliest = earliest_ts.timestamp()  # UTC timestamp
                        break
                    if earliest_count > 10000:
                        break

                # Get latest timestamp
                j.seek_tail()
                latest = None
                latest_count = 0
                # Read backwards until we find a match
                for entry in j:
                    latest_count += 1
                    if entry.get('SYSLOG_IDENTIFIER') == JOURNAL_IDENTIFIER:
                        latest_ts = entry.get('__REALTIME_TIMESTAMP')
                        if latest_ts:
                            latest = latest_ts.timestamp()  # UTC timestamp
                        break
                    if latest_count > 1000:
                        break

                return earliest, latest

            earliest, latest = await asyncio.to_thread(_get_range)

            return web.json_response({
                'earliest': earliest if earliest else None,
                'latest': latest if latest else None
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            return web.json_response({'error': str(e)}, status=500)

    async def handle_api_journal_query(self, request):
        """Unified journal query endpoint"""

        if not HAS_SYSTEMD:
            return web.json_response({'error': 'Journal not available'}, status=503)

        data = await request.json() if request.body_exists else {}

        timestamp = data.get('timestamp')
        before = data.get('before', 0)
        after = data.get('after', 0)

        if timestamp is None:
            return web.json_response({'status': 'ok', 'logs': []})

        def _format_log_entry(entry):
            ts = entry.get('__REALTIME_TIMESTAMP')
            if ts:
                dt = datetime.fromtimestamp(ts.timestamp())
                time_str = dt.strftime('%H:%M:%S')
            else:
                time_str = '00:00:00'

            message = entry.get('MESSAGE', '')
            linkify_message = linkify_log_line(message)
            unit = entry.get('_SYSTEMD_UNIT', '')
            if unit == self.controller_unit:
                prefix = '[CTRL]'
            else:
                prefix = '[NODE]'

            return {
                'message': f"[{time_str}] {prefix} {linkify_message}",
                'timestamp': ts.timestamp() if ts else None
            }

        def _query():
            target_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)

            j = journal.Reader(path='/var/log/journal')
            j.seek_realtime(target_dt)
            j.add_match(SYSLOG_IDENTIFIER='fgr-log-server')

            center = j.get_next()

            # Collect AFTER logs first (cursor moves forward)
            after_logs = []
            if after > 0 and center:
                entry = j.get_next()  # First after center
                while entry and len(after_logs) < after:
                    after_logs.append(_format_log_entry(entry))
                    entry = j.get_next()

            # Reposition to center for BEFORE logs
            j.seek_realtime(target_dt)
            j.add_match(SYSLOG_IDENTIFIER='fgr-log-server')
            j.get_next()  # Move to center
            entry = j.get_previous()  # Move to log before center

            before_logs = []
            if before > 0 and center:
                while entry and len(before_logs) < before:
                    before_logs.append(_format_log_entry(entry))
                    entry = j.get_previous()
            before_logs.reverse()

            # Assemble
            logs = before_logs
            target_index = -1
            if before > 0 and after > 0 and center:
                target_index = len(logs)
                logs.append(_format_log_entry(center))
            logs.extend(after_logs)

            if target_index == -1:
                if before > 0 and after == 0:
                    target_index = len(logs)
                elif before == 0 and after > 0:
                    target_index = 0

            return logs, target_index

        try:
            logs, target_index = await asyncio.to_thread(_query)
            has_more = len(logs) >= (before if before > 0 else after)
            return web.json_response({
                'status': 'ok',
                'logs': logs,
                'target_index': target_index,
                'has_more': has_more
            })
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)

    async def handle_api_command(self, request):
        """Handle command requests to nodes"""
        data = await request.json()
        node_name = data.get('node')
        command = data.get('command')
        params = data.get('params', {})

        result = {'status': 'ok', 'message': ''}

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
            'rssi': node.rssi if node.rssi is not None else '?',
            'metrics_display': self.node_metrics.get(node_name, {}).get('display', '<span class="metric-normal">Waiting for metrics data...</span>'),  # Add this
        }

        return web.json_response({'status': 'ok', 'data': node_data})

    async def handle_api_raw_minute_data(self, request):
        """Get raw minute-by-minute data for a specific hour bucket"""
        data = await request.json()
        metric = data.get('metric')
        hour_timestamp = data.get('timestamp')  # milliseconds from frontend
        node_ips = data.get('nodes', [])

        if not metric or not hour_timestamp:
            return web.json_response({'error': 'Missing metric or timestamp'}, status=400)

        # Convert to seconds and get hour range
        hour_start = hour_timestamp / 1000
        hour_end = hour_start + 3600

        conn = self._get_metrics_db_connection()
        if not conn:
            return web.json_response({'error': 'Database connection failed'}, status=503)

        try:
            cursor = conn.cursor()

            # Map metric names to database columns
            metric_column_map = {
                'ctrl_disconnects': 'ctrl_disconnects',
                'wifi_failures': 'wifi_failures',
                'panics': 'panics',
                'log_disconnects': 'log_disconnects',
                'rssi': 'rssi',
                'heap': 'heap'
            }

            column = metric_column_map.get(metric, metric)

            # Build node filter
            node_filter = ""
            params = [hour_start, hour_end]
            if node_ips and len(node_ips) > 0 and node_ips[0] != 'all':
                placeholders = ','.join(['?' for _ in node_ips])
                node_filter = f"AND node_ip IN ({placeholders})"
                params.extend(node_ips)

            # Query raw data for this hour
            query = f"""
                SELECT epoch_time, node_ip, {column}
                FROM metrics_history
                WHERE {column} IS NOT NULL
                AND epoch_time BETWEEN ? AND ?
                {node_filter}
                ORDER BY node_ip, epoch_time ASC
            """

            cursor.execute(query, params)
            rows = cursor.fetchall()

            if not rows:
                return web.json_response({'error': 'No data found for this hour'}, status=404)

            # Group by node_ip and organize data
            result = {}
            for row in rows:
                epoch_time, node_ip, value = row
                if node_ip not in result:
                    result[node_ip] = {
                        'ip': node_ip,
                        'name': self._get_node_name_by_ip(node_ip) or node_ip,
                        'data': []
                    }
                # Convert to milliseconds for frontend
                result[node_ip]['data'].append([epoch_time * 1000, value])

            # Also get the delta (new events) for this hour for context
            delta_info = {}
            for node_ip, node_data in result.items():
                values = [d[1] for d in node_data['data']]
                if len(values) >= 2:
                    # Calculate deltas between consecutive readings
                    deltas = [values[i] - values[i-1] for i in range(1, len(values)) if values[i] - values[i-1] > 0]
                    delta_info[node_ip] = {
                        'total_events': sum(deltas),
                        'first_value': values[0],
                        'last_value': values[-1]
                    }
                elif len(values) == 1:
                    delta_info[node_ip] = {
                        'total_events': values[0],  # First reading, assume all events happened? Or 0?
                        'first_value': values[0],
                        'last_value': values[0]
                    }
                else:
                    delta_info[node_ip] = {'total_events': 0}

            return web.json_response({
                'metric': metric,
                'hour_start': hour_start * 1000,
                'hour_end': hour_end * 1000,
                'data': result,
                'delta_summary': delta_info
            })

        except Exception as e:
            self._log_message(f"Error querying raw minute data: {e}")
            import traceback
            traceback.print_exc()
            return web.json_response({'error': str(e)}, status=500)
        finally:
            conn.close()

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

        # Get metrics display
        metrics_display = self.node_metrics.get(node_name, {}).get('display', '<span class="metric-normal">Waiting for metrics data...</span>')

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

    def _get_metrics_db_connection(self):
        """Get connection to metrics database"""
        if not self.graphs_enabled or not self.db_path or not self.db_path.exists():
            return None
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def handle_api_graph_data(self, request):
        """Get graph data for all metrics"""
        import time as time_module
        start_total = time_module.time()

        if not self.graphs_enabled:
            return web.json_response({'error': 'Graphs disabled'}, status=503)

        data = await request.json() if request.body_exists else {}
        end_time = data.get('end_time', time.time())
        start_time = data.get('start_time', end_time - (24 * 3600))
        node_ips = data.get('nodes', [])

        duration = end_time - start_time

        # Create cache key
        cache_key = f"{int(start_time/3600)}_{int(end_time/3600)}_{','.join(sorted(node_ips))}"

        # Check cache
        if cache_key in self.graph_cache:
            cached_time, cached_data = self.graph_cache[cache_key]
            if time.time() - cached_time < self.graph_cache_timeout:
                return web.json_response(cached_data)

        # Run each query in its own thread (not using a shared executor)
        # This creates a new thread for each query, allowing true parallelism
        results = await asyncio.gather(
            asyncio.to_thread(self._query_rssi_data, start_time, end_time, node_ips),
            asyncio.to_thread(self._query_wifi_failures, start_time, end_time, node_ips),
            asyncio.to_thread(self._query_panics, start_time, end_time, node_ips),
            asyncio.to_thread(self._query_ctrl_disconnects, start_time, end_time, node_ips),
            asyncio.to_thread(self._query_log_disconnects, start_time, end_time, node_ips),
            asyncio.to_thread(self._query_heap, start_time, end_time, node_ips),
        )

        rssi_data, wifi_failures_data, panics_data, ctrl_disconnects_data, log_disconnects_data, heap_data = results

        result = {
            'rssi': rssi_data,
            'wifi_failures': wifi_failures_data,
            'panics': panics_data,
            'ctrl_disconnects': ctrl_disconnects_data,
            'log_disconnects': log_disconnects_data,
            'heap': heap_data,
            'time_range': {'start': start_time, 'end': end_time}
        }

        # Cache the result
        self.graph_cache[cache_key] = (time.time(), result)

        return web.json_response(result)


    def _apply_downsampling(self, rows, start_time, end_time):
        """Apply downsampling to query results based on time range duration"""
        duration = end_time - start_time

        # Calculate step based on duration
        if duration > 432000:      # > 5 days
            step = 30
        elif duration > 259200:    # > 3 days
            step = 20
        elif duration > 172800:    # > 48 hours (2 days)
            step = 12
        elif duration > 86400:     # > 24 hours
            step = 6
        elif duration > 21600:     # > 6 hours
            step = 2
        else:                      # <= 6 hours
            step = 1

        if step == 1:
            return rows

        # Take every Nth row
        downsampled = []
        for i, row in enumerate(rows):
            if i % step == 0:
                downsampled.append(row)

        return downsampled

    def _query_bucketed_metric(self, metric_column, start_time, end_time, node_ips, bucket_seconds=3600):
        """
        Query a cumulative metric and return deltas (new events) per bucket.
        """
        # Convert string bucket spec to seconds if needed
        if isinstance(bucket_seconds, str):
            bucket_str = bucket_seconds.lower()
            if bucket_str.endswith('h'):
                bucket_seconds = int(bucket_str[:-1]) * 3600
            elif bucket_str.endswith('m'):
                bucket_seconds = int(bucket_str[:-1]) * 60
            elif bucket_str.endswith('s'):
                bucket_seconds = int(bucket_str[:-1])
            else:
                bucket_seconds = int(bucket_seconds)

        conn = self._get_metrics_db_connection()
        if not conn:
            return {}

        try:
            cursor = conn.cursor()

            node_filter = ""
            params = [start_time, end_time]
            if node_ips:
                placeholders = ','.join(['?' for _ in node_ips])
                node_filter = f"AND node_ip IN ({placeholders})"
                params.extend(node_ips)

            # Get raw cumulative values with ordering
            query = f"""
                SELECT
                    epoch_time,
                    node_ip,
                    {metric_column}
                FROM metrics_history
                WHERE {metric_column} IS NOT NULL
                AND epoch_time BETWEEN ? AND ?
                {node_filter}
                ORDER BY node_ip, epoch_time ASC
            """

            cursor.execute(query, params)
            rows = cursor.fetchall()

            if not rows:
                return {}

            # Calculate deltas per node
            node_last_values = {}
            delta_points = []  # (bucket_start, node_ip, delta)

            for row in rows:
                epoch_time, node_ip, cumulative_value = row

                last_value = node_last_values.get(node_ip)

                if last_value is not None:
                    # Calculate delta since last reading
                    delta = cumulative_value - last_value
                    if delta > 0:
                        # Calculate which bucket this belongs to
                        bucket_start = (epoch_time // bucket_seconds) * bucket_seconds
                        delta_points.append((bucket_start, node_ip, delta))

                node_last_values[node_ip] = cumulative_value

            if not delta_points:
                return {}

            # Aggregate deltas by bucket and node
            result = {}
            for bucket_start, node_ip, delta in delta_points:
                if node_ips and node_ip not in node_ips:
                    continue

                if node_ip not in result:
                    result[node_ip] = []

                # Check if we already have this bucket for this node
                existing = None
                for point in result[node_ip]:
                    if point[0] == bucket_start * 1000:
                        existing = point
                        break

                if existing:
                    existing[1] += delta  # Sum multiple deltas in same bucket
                else:
                    result[node_ip].append([bucket_start * 1000, delta])

            # Sort timestamps for each node
            for node_ip in result:
                result[node_ip].sort(key=lambda x: x[0])

            total_points = sum(len(v) for v in result.values())
            self._log_message(f"{metric_column} (deltas): {total_points} event points from {len(result)} nodes")
            return result

        except Exception as e:
            self._log_message(f"Error querying {metric_column}: {e}")
            import traceback
            traceback.print_exc()
            return {}
        finally:
            conn.close()

    def _query_rssi_data(self, start_time, end_time, node_ips):
        """Query RSSI values from metrics_history (fast path)"""
        conn = self._get_metrics_db_connection()
        if not conn:
            return {}

        try:
            cursor = conn.cursor()

            node_filter = ""
            params = [start_time, end_time]
            if node_ips:
                placeholders = ','.join(['?' for _ in node_ips])
                node_filter = f"AND node_ip IN ({placeholders})"
                params.extend(node_ips)

            query = f"""
                SELECT epoch_time, node_ip, rssi
                FROM metrics_history
                WHERE rssi IS NOT NULL
                AND epoch_time BETWEEN ? AND ?
                {node_filter}
                ORDER BY epoch_time ASC
            """

            cursor.execute(query, params)
            rows = cursor.fetchall()

            # Apply downsampling
            rows = self._apply_downsampling(rows, start_time, end_time)

            result = {}
            for row in rows:
                epoch_time, node_ip, rssi = row
                if rssi is not None and -100 <= rssi <= 0:
                    if node_ip not in result:
                        result[node_ip] = []
                    result[node_ip].append([epoch_time * 1000, rssi])

            total_points = sum(len(v) for v in result.values())
            return result

        except Exception as e:
            self._log_message(f"Error querying RSSI: {e}")
            return {}
        finally:
            conn.close()

    def _query_wifi_failures(self, start_time, end_time, node_ips):
        return self._query_bucketed_metric('wifi_failures', start_time, end_time, node_ips, 3600)

    def _query_panics(self, start_time, end_time, node_ips):
        return self._query_bucketed_metric('panics', start_time, end_time, node_ips, 3600)

    def _query_ctrl_disconnects(self, start_time, end_time, node_ips):
        return self._query_bucketed_metric('ctrl_disconnects', start_time, end_time, node_ips, 3600)

    def _query_log_disconnects(self, start_time, end_time, node_ips):
        return self._query_bucketed_metric('log_disconnects', start_time, end_time, node_ips, 3600)

    def _query_heap(self, start_time, end_time, node_ips):
        """Query heap free memory from metrics_history (fast path)"""
        conn = self._get_metrics_db_connection()
        if not conn:
            return {}

        try:
            cursor = conn.cursor()

            node_filter = ""
            params = [start_time, end_time]
            if node_ips:
                placeholders = ','.join(['?' for _ in node_ips])
                node_filter = f"AND node_ip IN ({placeholders})"
                params.extend(node_ips)

            query = f"""
                SELECT epoch_time, node_ip, heap
                FROM metrics_history
                WHERE heap IS NOT NULL
                AND epoch_time BETWEEN ? AND ?
                {node_filter}
                ORDER BY epoch_time ASC
            """

            cursor.execute(query, params)
            rows = cursor.fetchall()

            rows = self._apply_downsampling(rows, start_time, end_time)

            result = {}
            for row in rows:
                epoch_time, node_ip, heap = row
                if heap is not None and heap > 0:
                    if node_ip not in result:
                        result[node_ip] = []
                    result[node_ip].append([epoch_time * 1000, heap])

            total_points = sum(len(v) for v in result.values())
            return result

        except Exception as e:
            self._log_message(f"Error querying heap: {e}")
            return {}
        finally:
            conn.close()

    async def handle_api_graph_nodes(self, request):
        """Get list of nodes that have graph data available"""
        if not self.graphs_enabled:
            return web.json_response({'nodes': [], 'error': 'Graphs disabled'})

        # Just return the nodes from the controller - much faster!
        nodes = []
        for name, node in self.nodes.items():
            if node.ip:  # Only include nodes that have an IP
                nodes.append({'ip': node.ip, 'name': name})

        return web.json_response({'nodes': nodes})

    async def handle_api_graph_time_range(self, request):
        """Get available time range in the database"""
        if not self.graphs_enabled:
            return web.json_response({'min_time': None, 'max_time': None, 'error': 'Graphs disabled'})

        conn = self._get_metrics_db_connection()
        if not conn:
            return web.json_response({'min_time': None, 'max_time': None})

        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT MIN(epoch_time), MAX(epoch_time)
                FROM device_logs
                WHERE message_type = 'METRIC'
                OR message LIKE '%disconnect%'
            """)
            row = cursor.fetchone()
            return web.json_response({
                'min_time': row[0] if row[0] else None,
                'max_time': row[1] if row[1] else None
            })
        except Exception as e:
            self._log_message(f"Error querying time range: {e}")
            return web.json_response({'min_time': None, 'max_time': None})
        finally:
            conn.close()

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
        if HAS_SYSTEMD:
            self.web_app.router.add_post('/api/journal/query', self.handle_api_journal_query)
            self.web_app.router.add_get('/api/journal/range', self.handle_api_journal_time_range)
            self._log_message("Journal API endpoints enabled")
        if self.graphs_enabled:
            self.web_app.router.add_post('/api/graph/data', self.handle_api_graph_data)
            self.web_app.router.add_post('/api/graph/raw_minute_data', self.handle_api_raw_minute_data)
            self.web_app.router.add_get('/api/graph/nodes', self.handle_api_graph_nodes)
            self.web_app.router.add_get('/api/graph/time_range', self.handle_api_graph_time_range)
            self._log_message(f"Graph endpoints enabled")
        else:
            self._log_message(f"Graph endpoints disabled (use --db-path to enable)")

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

        # Start background metrics trimmer
        if self.graphs_enabled:
            self._start_metrics_trimmer()

        if not super().start():
            return False
        self.start_web()

        # Log graph status
        if self.graphs_enabled:
            print(f"Graphs enabled: {self.db_path}")
        else:
            print(f"Graphs disabled (use --db-path to enable)")

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
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 15px;
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

        /* Node metrics data row */
        .node-metrics-data {
            font-family: 'Courier New', 'SF Mono', 'Monaco', 'Menlo', monospace;
            font-size: 9px;
            line-height: 1.2;
            margin-top: 2px;
            padding-top: 2px;
            border-top: 1px solid #eee;
            overflow: hidden;
            white-space: nowrap;
            height: 24px;
            position: relative;
        }

        .node-metrics-data .ticker-content {
            display: inline-block;
            white-space: nowrap;
        }

        .node-metrics-data.scrolling .ticker-content {
            animation: ticker 30s linear infinite;
        }

        .node-metrics-data.scrolling .ticker-content:hover {
            animation-play-state: paused;
        }

        @keyframes ticker {
            /* 0% to 20% (First 6 seconds): Stationary pause */
            0%, 20% {
                transform: translateX(0);
            }
            /* 20% to 100% (Next 24 seconds): Smooth scroll */
            100% {
                transform: translateX(-100%);
            }
        }

        .metric-normal {
            color: #888;
            font-weight: normal;
        }

        .metric-important {
            color: #000;
            font-weight: bold;
        }

        /* Tooltip styling */
        [title] {
            cursor: help;
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

        /* Highligh links in log messages */
        .debug-window .log-node a {
            color: #FFFF00; /* Bright Yellow */
            text-decoration: underline;
            font-weight: bold;
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

        .log-marker {
            background: #2d5a2d;
            color: #88ffaa;
            text-align: center;
            padding: 4px;
            margin: 8px 0;
            font-size: 10px;
            border-top: 1px solid #88ffaa;
            border-bottom: 1px solid #88ffaa;
            font-family: monospace;
        }

        /* View selector buttons */
        .view-btn {
            padding: 4px 16px;
            font-size: 12px;
            border: 1px solid #ddd;
            border-radius: 20px;
            background: white;
            cursor: pointer;
            transition: all 0.2s;
            white-space: nowrap;
        }
        .view-btn:hover {
            background: #e3f2fd;
            border-color: #2196f3;
        }
        .view-btn.active {
            background: #2196f3;
            color: white;
            border-color: #2196f3;
        }

        /* Graph view container */
        .graph-view {
            background: white;
            border-radius: 8px;
            padding: 10px 15px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            margin-top: 0px;
        }

        /* Roughly matches dashboard grid */
        .graph-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(260px, 1fr));
            gap: 8px;
            margin-top: 2px;
            margin-bottom: 5px;
        }

        .graph-card {
            background: #fafafa;
            border-radius: 6px;
            padding: 4px;
            border: 1px solid #e0e0e0;
            cursor: pointer;
            height: 250px;
            display: flex;
            flex-direction: column;
        }

        .graph-title {
            font-size: 11px;
            font-weight: bold;
            margin-bottom: 2px;
            color: #333;
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-shrink: 0;
        }

        .graph-container {
            flex: 1;
            width: 100%;
            min-height: 0;
            position: relative;
            cursor: pointer;
        }

        .graph-container:active {
            cursor: grabbing;
        }

        .graph-container canvas {
            width: 100% !important;
            height: 100% !important;
            position: absolute;
            top: 0;
            left: 0;
        }
        .graph-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 4px;
            flex-wrap: wrap;
            gap: 4px;
            min-height: auto;
        }

        .graph-card.expanded {
            cursor: default;
        }

        .graph-card.expanded .graph-container {
            height: calc(80vh - 60px);
        }

        .graph-loading, .no-data-message {
            text-align: center;
            padding: 40px;
            color: #999;
        }

        .graph-tool-btn {
            background: none;
            border: 1px solid #ddd;
            border-radius: 4px;
            padding: 2px 6px;
            font-size: 12px;
            cursor: pointer;
            width: auto;
        }

        .graph-tool-btn:hover {
            background: #f0f0f0;
            border-color: #999;
        }

        .graph-expand-hint {
            opacity: 0.6;
            font-size: 9px;
        }

        .time-range-select {
            padding: 2px 6px;
            font-size: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
            background: white;
            cursor: pointer;
        }

        .time-range-select:hover {
            border-color: #2196f3;
        }

        .node-filter {
            display: flex;
            align-items: center;
            gap: 6px;
            font-size: 10px;
        }

        .node-filter select {
            padding: 4px 8px;
            border-radius: 4px;
            border: 1px solid #ddd;
            font-size: 11px;
        }

        .node-filter-select[size] {
            font-size: 10px;
            padding: 2px 4px;
            height: auto;
            max-height: 60px;  /* Limit height to ~3 rows */
        }

        .node-filter-select option {
            padding: 3px 6px;
            font-size: 10px;
        }

        @media (max-width: 1000px) {
            .graph-grid {
                grid-template-columns: repeat(2, 1fr);
            }
        }

        @media (max-width: 600px) {
            .graph-grid {
                grid-template-columns: 1fr;
            }
        }

        .minute-modal {
            position: fixed;
            z-index: 10001;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: rgba(0,0,0,0.7);
        }

        .minute-modal-content {
            background-color: #fefefe;
            margin: 5% auto;
            padding: 0;
            border-radius: 8px;
            width: 90%;
            max-width: 1000px;
            max-height: 80vh;
            display: flex;
            flex-direction: column;
            box-shadow: 0 4px 20px rgba(0,0,0,0.3);
        }

        .minute-modal-header {
            padding: 12px 20px;
            background: #2196f3;
            color: white;
            border-radius: 8px 8px 0 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .minute-modal-header h3 {
            margin: 0;
            font-size: 16px;
        }

        .minute-modal-close {
            color: white;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
            line-height: 1;
        }

        .minute-modal-close:hover {
            color: #ddd;
        }

        .minute-modal-body {
            padding: 20px;
            overflow-y: auto;
            flex: 1;
        }

        .minute-node-section {
            margin-bottom: 25px;
            border: 1px solid #e0e0e0;
            border-radius: 6px;
            overflow: hidden;
        }

        .minute-node-header {
            background: #f5f5f5;
            padding: 10px 15px;
            font-weight: bold;
            cursor: pointer;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .minute-node-header:hover {
            background: #e8e8e8;
        }

        .minute-node-name {
            font-size: 14px;
        }

        .minute-node-stats {
            font-size: 11px;
            color: #666;
        }

        .minute-table {
            width: 100%;
            border-collapse: collapse;
            font-size: 11px;
        }

        .minute-table th,
        .minute-table td {
            padding: 6px 10px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }

        .minute-table th {
            background: #fafafa;
            font-weight: bold;
            position: sticky;
            top: 0;
        }

        .minute-table tr:hover {
            background: #f5f5f5;
        }

        .minute-no-data {
            padding: 20px;
            text-align: center;
            color: #999;
        }

        .clickable-row:hover {
            background-color: #e3f2fd !important;
            cursor: pointer;
        }

        .clickable-row {
            transition: background-color 0.1s ease;
        }

        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
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
        <!-- View selector will be inserted here by JavaScript -->
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
                <div class="filter-input-group">
                    <span class="filter-label">Go to time</span>
                    <input type="datetime-local" id="gotoTimeInput" class="filter-textbox" style="width: 160px;">
                    <button id="gotoTimeBtn" class="debug-toggle-btn" style="padding: 2px 6px; font-size: 10px;">Go</button>
                </div>
            </div>
            <div class="debug-buttons">
                <button id="scrollLockBtn" style="background:#17a2b8;color:white;" title="Auto-scroll lock">🔒 Auto-scroll</button>
                <button onclick="selectAllLogs()" style="background:#17a2b8;color:white;">📋 Select All</button>
                <button onclick="copyLogsToClipboard(event)" style="background:#28a745;color:white;">📋 Copy</button>
                <button onclick="clearLogs()" style="background:#6c757d;color:white;">🗑️ Clear</button>
            </div>
        </div>
        <div id="debugWindow" class="debug-window">
            <div class="loading-indicator" style="text-align:center;padding:20px;">Loading logs...</div>
        </div>
    </div>

    <div class="footer">FGR Controller - Drag ⋮⋮ to reorder nodes | Double-click card to expand | Drag blue bar above debug panel to resize | 📌 Dock returns to default size | Click header to collapse/expand</div>

    <!-- Drill-down modal -->
    <div id="minuteModal" class="minute-modal" style="display: none;">
        <div class="minute-modal-content">
            <div class="minute-modal-header">
                <h3 id="minuteModalTitle">Minute-by-Minute Data</h3>
                <div style="display: flex; gap: 10px; align-items: center;">
                    <button id="minuteModalCopyBtn" class="graph-tool-btn" style="background: #17a2b8; color: white; padding: 4px 12px;">📋 Copy</button>
                    <button id="minuteModalCsvBtn" class="graph-tool-btn" style="background: #28a745; color: white; padding: 4px 12px;">📊 Export CSV</button>
                    <span class="minute-modal-close" style="cursor: pointer;">&times;</span>
                </div>
            </div>
            <div class="minute-modal-body" id="minuteModalBody">
                <div class="loading">Loading detailed data...</div>
            </div>
        </div>
    </div>

    <script>
        let statusSource = null;
        let logsSource = null;
        let autoScrollEnabled = true;
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

        // Graphing stuff
        let simpleCache = {};
        let currentChartData = {};
        let rawGraphData = {};
        let currentDrillDownData = null;

        // Log level names
        const logLevelNames = ['DEBUG', 'INFO', 'WARN', 'ERROR'];

        // Debug window
        let debugWindow = null;
        let logBuffer = [];             // Stores {timestamp, message} objects
        let logElements = [];           // DOM elements for each log
        let logTexts = [];              // Raw text for each log
        let logTimestamps = [];         // Timestamps for each log
        let isAtBottom = true;          // Whether we're scrolled to bottom
        let autoScrollLocked = false;   // Whether auto-scroll is locked (disabled)
        let isLoading = false;          // Whether we're currently loading more logs
        let hasMoreDown = true;         // Whether there are more logs below
        let journalRange = { earliest: null, latest: null };  // Available journal range
        let currentScrollAnchor = null;  // Timestamp to anchor scrolling after loading
        let maxNormalGapSeconds = 5; // Track the maximum normal gap between consecutive logs

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
            toggleBtn.innerHTML = '▲ Maximise';  // Upward triangle for Maximise
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

            // Set initial state: not collapsed, button shows "▲ Maximise"
            if (debugPanel && toggleBtn) {
                debugPanel.classList.remove('collapsed');
                toggleBtn.innerHTML = '▲ Maximise';
                toggleBtn.textContent = '▲ Maximise';
            }
        }

        function dockDebugPanel() {
            const debugPanel = document.querySelector('.debug-panel');
            const dockBtn = document.querySelector('.debug-dock-btn');

            if (!debugPanel) return;

            const dockedHeight = debugPanel.getAttribute('data-docked-height') || '250px';
            const toggleBtn = document.querySelector('.debug-toggle-btn');

            // Save current height before docking if needed
            const currentHeight = debugPanel.offsetHeight;
            const isCollapsed = debugPanel.classList.contains('collapsed');

            // Set to docked height
            debugPanel.style.height = dockedHeight;
            debugPanel.classList.remove('collapsed');

            // Update toggle button state (now in 'maximise' state relative to docked)
            if (toggleBtn && !isCollapsed && currentHeight !== parseInt(dockedHeight)) {
                // We were in an expanded state, now docked
                toggleBtn.innerHTML = '▼ Minimise';
                toggleBtn.textContent = '▼ Minimise';
            } else if (toggleBtn) {
                toggleBtn.innerHTML = '▼ Minimise';
                toggleBtn.textContent = '▼ Minimise';
            }

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

            // Get current state from button text
            const isMaximised = toggleBtn && toggleBtn.textContent.includes('Maximise');
            const isMinimised = toggleBtn && toggleBtn.textContent.includes('Minimise');

            if (isMaximised) {
                // Maximise: move top to maximum drag-height
                const maxHeight = window.innerHeight * 0.93;
                const currentHeight = debugPanel.offsetHeight;

                // Save current height before expanding if it's not already max
                if (currentHeight !== maxHeight) {
                    debugPanel.setAttribute('data-pre-max-height', currentHeight);
                }

                debugPanel.style.height = maxHeight + 'px';
                debugPanel.classList.remove('collapsed');

                // Update button
                if (toggleBtn) {
                    toggleBtn.innerHTML = '▼ Minimise';
                    toggleBtn.textContent = '▼ Minimise';
                }
            } else if (isMinimised) {
                // Minimise: move to minimum drag-height (like collapse)
                const minHeight = 42; // The height of the collapsed state

                // Save current height before collapsing
                const currentHeight = debugPanel.offsetHeight;
                if (currentHeight !== minHeight) {
                    debugPanel.setAttribute('data-expanded-height', currentHeight);
                }

                debugPanel.style.height = minHeight + 'px';
                debugPanel.classList.add('collapsed');

                // Update button
                if (toggleBtn) {
                    toggleBtn.innerHTML = '▲ Maximise';
                    toggleBtn.textContent = '▲ Maximise';
                }
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
            const TOKEN_PREFIX = '___PROTECTED_LINK_';
            const TOKEN_SUFFIX = '___';

            const protectedTags = [];
            let working = logLine;
            let matchCount = 0;

            // Protect ALL <a> tags (needed for links to crash dumps)
            working = working.replace(/<a\\b[^>]*?>.*?<\\/a>/gi, (match) => {
                const token = `${TOKEN_PREFIX}${matchCount++}${TOKEN_SUFFIX}`;
                protectedTags.push({ token, html: match });
                return token;
            });

            // Escape HTML characters
            working = working.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

            // Restore protected <a> tags
            protectedTags.forEach(({ token, html }) => {
                working = working.replace(token, html);
            });

            if (filterHighlightNodes.size === 0) {
                return working;
            }

            const lastOctet = extractNodeLastOctet(logLine);
            if (lastOctet && filterHighlightNodes.has(lastOctet)) {
                return `<span class="log-highlight">${working}</span>`;
            }
            return working;
        }

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
                    logDiv.innerHTML = highlightedContent;
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

        // Re-check all metrics rows on window resize
        let resizeTimeout;
        window.addEventListener('resize', function() {
            clearTimeout(resizeTimeout);
            resizeTimeout = setTimeout(() => {
                setupMetricsTicker();
            }, 250);
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
                            ${customHtml || '<div class="node-custom"><div class="custom-value">—</div><div class="custom-unit">None</div></div>'}
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
                        <div class="node-metrics-data">
                            <div class="ticker-content">{node.metrics_display}</div>
                        </div>
                    </div>
                `;
            }

            grid.innerHTML = html;

            // Setup ticker for new metrics rows
            setupMetricsTicker();

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

                // Update metrics data display - just update content
                let tickerContent = card.querySelector('.ticker-content');
                const metricsDataDiv = card.querySelector('.node-metrics-data');

                if (!tickerContent && metricsDataDiv && node.metrics_display) {
                    metricsDataDiv.innerHTML = '<div class="ticker-content">' + node.metrics_display + '</div>';
                } else if (tickerContent && node.metrics_display) {
                    if (tickerContent.innerHTML !== node.metrics_display) {
                        tickerContent.innerHTML = node.metrics_display;
                    }
                }

                // Check if scrolling needed
                if (tickerContent && metricsDataDiv) {
                    const needsScrolling = tickerContent.scrollWidth > metricsDataDiv.clientWidth;

                    if (needsScrolling) {
                        if (!metricsDataDiv.classList.contains('scrolling')) {
                            metricsDataDiv.classList.add('scrolling');
                        }
                    } else {
                        metricsDataDiv.classList.remove('scrolling');
                    }
                }

                // Update custom data (the card's center area - only if not expanded or if it's a different node)
                if (!isExpanded || (isExpanded && nodeName !== expandedNodeName)) {
                    updateNodeCustomData(node);
                }
            }

            // Re-check all metrics rows after updates
            setupMetricsTicker();
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

        function updateNodeMetricsData(nodeName, metricsDisplay) {
            const card = document.querySelector(`.node-card[data-node-name="${nodeName}"]`);
            if (!card) return;

            const metricsDataDiv = card.querySelector('.node-metrics-data');
            if (metricsDataDiv && metricsDisplay) {
                metricsDataDiv.innerHTML = metricsDisplay;
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

            // Update metrics data display in expanded view
            let tickerContent = expandedCard.querySelector('.ticker-content');
            const metricsDataDiv = expandedCard.querySelector('.node-metrics-data');

            if (!tickerContent && metricsDataDiv && nodeData.metrics_display) {
                metricsDataDiv.innerHTML = '<div class="ticker-content">' + nodeData.metrics_display + '</div>';
            } else if (tickerContent && nodeData.metrics_display) {
                if (tickerContent.innerHTML !== nodeData.metrics_display) {
                    tickerContent.innerHTML = nodeData.metrics_display;
                }
            }

            // Check if scrolling needed
            if (tickerContent && metricsDataDiv) {
                const currentDisplay = tickerContent.style.display;
                tickerContent.style.display = 'inline-block';
                const needsScrolling = tickerContent.scrollWidth > metricsDataDiv.clientWidth;
                tickerContent.style.display = currentDisplay;

                if (needsScrolling) {
                    metricsDataDiv.classList.add('scrolling');
                } else {
                    metricsDataDiv.classList.remove('scrolling');
                }
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

        function setupMetricsTicker() {
            // Always add scrolling class to all cards with metrics
            document.querySelectorAll('.node-metrics-data').forEach(container => {
                const content = container.querySelector('.ticker-content');
                if (content && content.scrollWidth > container.clientWidth) {
                    container.classList.add('scrolling');
                } else {
                    container.classList.remove('scrolling');
                }
            });
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

        // ============ DEBUG WINDOW ============

        // Initialize debug window with scroll loading
        function initDebugWindow() {
            debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) {
                console.error('debugWindow not found!');
                return;
            }

            // Set up scroll event listener for infinite scroll
            debugWindow.addEventListener('scroll', handleDebugScroll);

            // Load journal range info
            loadJournalRange();
        }

        // Load available journal time range
        async function loadJournalRange() {
            try {
                const response = await fetch('/api/journal/range');
                const data = await response.json();
                if (!data.error) {
                    journalRange.earliest = data.earliest;
                    journalRange.latest = data.latest;
                    // Set min/max for datetime picker
                    const gotoInput = document.getElementById('gotoTimeInput');
                    if (gotoInput && journalRange.earliest && journalRange.latest) {
                        gotoInput.min = new Date(journalRange.earliest * 1000).toISOString().slice(0, 16);
                        gotoInput.max = new Date(journalRange.latest * 1000).toISOString().slice(0, 16);
                    }
                }
            } catch (e) {
                console.error('Failed to load journal range:', e);
            }
        }

        // Load historical logs above current view (scrolling up)
        async function loadHistoricalLogsAbove() {
            // Get the earliest log we currently have
            const earliestLog = logBuffer.length > 0 ? logBuffer[0] : null;
            const beforeTimestamp = earliestLog ? earliestLog.timestamp - 0.001 : null;

            // If we have no logs, use latest journal time as reference
            if (!beforeTimestamp && journalRange.latest) {
                if (!journalRange.latest) {
                    console.log("journalRange.latest not ready, waiting...");
                    setTimeout(() => loadHistoricalLogsAbove(), 500);
                    return;
                }
                isLoading = true;
                try {
                    const response = await fetch('/api/journal/query', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            timestamp: journalRange.latest,
                            before: 100,
                            after: 20
                        })
                    });
                    const data = await response.json();
                    if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                        currentScrollAnchor = data.logs[data.target_index]?.timestamp;
                        addHistoricalLogsAbove(data.logs);
                    }
                } catch (e) {
                    console.error('loadHistoricalLogsAbove: error=', e);
                }
                isLoading = false;
                return;
            }

            if (!beforeTimestamp) {
                return;
            }

            // Check if we're near the top of the scroll area
            if (debugWindow.scrollTop > 20) {
                return;
            }

            isLoading = true;

            try {
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        timestamp: beforeTimestamp,
                        before: 100
                    })
                });
                const data = await response.json();

                if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                    const oldScrollHeight = debugWindow.scrollHeight;
                    const oldScrollTop = debugWindow.scrollTop;

                    addHistoricalLogsAbove(data.logs);

                    const newScrollHeight = debugWindow.scrollHeight;
                    debugWindow.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);
                }

            } catch (e) {
                console.error('loadHistoricalLogsAbove: fetch error=', e);
            }

            isLoading = false;
        }

        // Load more logs below (when scrolling down near bottom)
        async function loadMoreLogsBelow() {
            if (isLoading || !hasMoreDown || autoScrollLocked) return;

            // Check if we're near the bottom
            const distanceToBottom = debugWindow.scrollHeight - debugWindow.scrollTop - debugWindow.clientHeight;
            if (distanceToBottom > 50) return;

            // Get the latest log we currently have
            const latestLog = logBuffer.length > 0 ? logBuffer[logBuffer.length - 1] : null;
            const afterTimestamp = latestLog ? latestLog.timestamp : null;

            if (!afterTimestamp) return;

            isLoading = true;

            try {
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        timestamp: afterTimestamp,
                        after: 100
                    })
                });
                const data = await response.json();

                if (data.status === 'ok' && data.logs.length > 0) {
                    addHistoricalLogsBelow(data.logs);
                }

                hasMoreDown = data.has_more !== false;

            } catch (e) {
                console.error('Failed to load more logs:', e);
            }

            isLoading = false;
        }

        // Handle scroll events for infinite loading
        function handleDebugScroll() {
            if (!debugWindow) return;

            const distanceToBottom = debugWindow.scrollHeight - debugWindow.scrollTop - debugWindow.clientHeight;
            const atBottom = distanceToBottom < 10;

            // Update auto-scroll state
            if (!autoScrollLocked && atBottom && !isAtBottom) {
                isAtBottom = true;
            } else if (!autoScrollLocked && !atBottom) {
                isAtBottom = false;
            }

            // Check if we should load more
            if (debugWindow.scrollTop < 50) {
                loadHistoricalLogsAbove();
            } else if (distanceToBottom < 100 && !autoScrollLocked) {
                loadMoreLogsBelow();
            }
        }

        function updateMaxNormalGap(timestamp, prevTimestamp) {
            if (prevTimestamp) {
                const gap = timestamp - prevTimestamp;
                // Only update if gap is reasonable (ignore huge gaps from jumps)
                if (gap < 60 && gap > maxNormalGapSeconds) {
                    maxNormalGapSeconds = gap;
                }
            }
        }

        // Add historical logs above existing logs
        function addHistoricalLogsAbove(newLogs) {
            if (!newLogs.length) return;

            // Convert to internal format
            const newEntries = newLogs.map(log => ({
                timestamp: log.timestamp,
                message: log.message
            })).filter(log => shouldDisplayLog(log.message));

            // Create a Set of existing timestamps for quick lookup (with tolerance)
            const existingTimestamps = new Set(logBuffer.map(log => log.timestamp));

            // Only add logs that don't already exist (within 0.1 second tolerance)
            const uniqueNewEntries = newEntries.filter(log => {
                // Check if any existing log has a timestamp within 0.1 seconds
                const exists = Array.from(existingTimestamps).some(ts => Math.abs(ts - log.timestamp) < 0.1);
                return !exists;
            });

            if (uniqueNewEntries.length === 0) return;

            // Update maxNormalGapSeconds for consecutive entries within the new batch
            for (let i = 1; i < uniqueNewEntries.length; i++) {
                updateMaxNormalGap(uniqueNewEntries[i].timestamp, uniqueNewEntries[i-1].timestamp);
            }

            // Also check gap between last existing log and first new log
            if (logBuffer.length > 0 && uniqueNewEntries.length > 0) {
                const lastExisting = logBuffer[logBuffer.length - 1].timestamp;
                const firstNew = uniqueNewEntries[0].timestamp;
                updateMaxNormalGap(firstNew, lastExisting);
            }

            // Add to buffer
            logBuffer.push(...uniqueNewEntries);

            // Sort by timestamp
            logBuffer.sort((a, b) => a.timestamp - b.timestamp);

            // Rebuild display
            rebuildDebugDisplay();
        }

        // Add historical logs below existing logs
        function addHistoricalLogsBelow(newLogs) {
            if (!newLogs.length) return;

            // Convert to internal format and filter
            const newEntries = [];
            for (const log of newLogs) {
                if (shouldDisplayLog(log.message)) {
                    newEntries.push({
                        timestamp: log.timestamp,
                        message: log.message
                    });
                }
            }

            if (newEntries.length === 0) return;

            // Update maxNormalGapSeconds for consecutive entries within the new batch
            for (let i = 1; i < newEntries.length; i++) {
                updateMaxNormalGap(newEntries[i].timestamp, newEntries[i-1].timestamp);
            }

            // Also check gap between last existing log and first new log
            if (logBuffer.length > 0 && newEntries.length > 0) {
                const lastExisting = logBuffer[logBuffer.length - 1].timestamp;
                const firstNew = newEntries[0].timestamp;
                updateMaxNormalGap(firstNew, lastExisting);
            }

            // Add to buffer
            for (const entry of newEntries) {
                logBuffer.push(entry);
            }

            // Sort by timestamp
            logBuffer.sort((a, b) => a.timestamp - b.timestamp);

            // Rebuild display
            rebuildDebugDisplay();
        }

        // Add a single new log (from stream)
        function addNewLog(logData) {
            // Handle both old string format and new object format
            let message, timestamp;
            if (typeof logData === 'string') {
                message = logData;
                timestamp = Date.now() / 1000;
            } else {
                message = logData.message;
                timestamp = logData.timestamp;
            }

            // If timestamp is null/undefined (CTRL logs without journal time), try to parse from message or use current time
            if (!timestamp) {
                const timeMatch = message.match(/\\[(\\d{2}:\\d{2}:\\d{2})\\]/);
                if (timeMatch) {
                    const [h, m, s] = timeMatch[1].split(':').map(Number);
                    const now = new Date();
                    now.setHours(h, m, s, 0);
                    timestamp = now.getTime() / 1000;
                } else {
                    timestamp = Date.now() / 1000;
                }
            }

            // Check if we already have this exact message (prevent duplicates)
            // Use message + recent timestamp as duplicate detection
            const isDuplicate = logBuffer.some(log =>
                log.message === message && Math.abs(log.timestamp - timestamp) < 2
            );
            if (isDuplicate) {
                return;
            }

            // Update maxNormalGapSeconds by comparing with the last log in buffer
            if (logBuffer.length > 0) {
                const lastTimestamp = logBuffer[logBuffer.length - 1].timestamp;
                updateMaxNormalGap(timestamp, lastTimestamp);
            }

            // Add to buffer
            logBuffer.push({
                timestamp: timestamp,
                message: message
            });

            // Sort buffer by timestamp to maintain chronological order
            logBuffer.sort((a, b) => a.timestamp - b.timestamp);

            // Trim buffer if too large (keep last 2000)
            while (logBuffer.length > 2000) {
                logBuffer.shift();
            }

            // Add to DOM efficiently (append only)
            if (shouldDisplayLog(message)) {
                const shouldScroll = !autoScrollLocked && isAtBottom;
                const className = message.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                const logDiv = document.createElement('div');
                logDiv.className = className;

                const highlightedContent = applyHighlighting(message);
                logDiv.innerHTML = highlightedContent;

                debugWindow.appendChild(logDiv);
                logElements.push(logDiv);
                logTexts.push(message);
                logTimestamps.push(timestamp);

                if (shouldScroll) {
                    debugWindow.scrollTop = debugWindow.scrollHeight;
                }
            }
        }

        // Rebuild entire debug display (used after bulk adds)
        function rebuildDebugDisplay() {
            if (!debugWindow) return;

            const shouldScrollToBottom = !autoScrollLocked && isAtBottom;
            const oldScrollHeight = debugWindow.scrollHeight;
            const oldScrollTop = debugWindow.scrollTop;

            debugWindow.innerHTML = '';
            logElements = [];
            logTexts = [];
            logTimestamps = [];

            let visibleCount = 0;
            let lastDisplayedTimestamp = null;

            for (const log of logBuffer) {
                // Skip markers for gap calculation, but they still affect display
                if (log.isMarker) {
                    // Render marker
                    const markerDiv = document.createElement('div');
                    markerDiv.className = 'gap-marker';
                    markerDiv.textContent = log.message;
                    debugWindow.appendChild(markerDiv);
                    logElements.push(markerDiv);
                    logTexts.push(log.message);
                    logTimestamps.push(log.timestamp);
                    visibleCount++;
                    continue;
                }

                if (shouldDisplayLog(log.message)) {
                    // Check for gap before displaying this log
                    if (lastDisplayedTimestamp !== null) {
                        const gap = log.timestamp - lastDisplayedTimestamp;
                        const threshold = maxNormalGapSeconds * 2;
                        if (gap > threshold) {
                            // Insert a gap marker
                            const markerDiv = document.createElement('div');
                            markerDiv.className = 'gap-marker';
                            markerDiv.textContent = `~~~~~~~~~~~~~~~~~~~~ [gap of ${Math.round(gap)} seconds] ~~~~~~~~~~~~~~~~~~~~~`;
                            debugWindow.appendChild(markerDiv);
                            logElements.push(markerDiv);
                            logTexts.push(markerDiv.textContent);
                            logTimestamps.push(lastDisplayedTimestamp + (gap / 2));
                            visibleCount++;
                        }
                    }

                    // Render the log
                    const className = log.message.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                    const logDiv = document.createElement('div');
                    logDiv.className = className;

                    const highlightedContent = applyHighlighting(log.message);
                    logDiv.innerHTML = highlightedContent

                    debugWindow.appendChild(logDiv);
                    logElements.push(logDiv);
                    logTexts.push(log.message);
                    logTimestamps.push(log.timestamp);
                    visibleCount++;

                    lastDisplayedTimestamp = log.timestamp;
                }
            }

            // Restore or adjust scroll position (existing code remains the same)
            if (shouldScrollToBottom) {
                debugWindow.scrollTop = debugWindow.scrollHeight;
            } else if (currentScrollAnchor) {
                for (let i = 0; i < logTimestamps.length; i++) {
                    if (Math.abs(logTimestamps[i] - currentScrollAnchor) < 0.1) {
                        const targetElement = logElements[i];
                        if (targetElement) {
                            targetElement.scrollIntoView({ block: 'center' });
                            targetElement.style.backgroundColor = '#ffff99';
                            setTimeout(() => {
                                targetElement.style.backgroundColor = '';
                            }, 2000);
                            break;
                        }
                    }
                }
                currentScrollAnchor = null;
            } else if (oldScrollTop > 0 && oldScrollHeight > 0) {
                const ratio = oldScrollTop / oldScrollHeight;
                debugWindow.scrollTop = ratio * debugWindow.scrollHeight;
            }

            // Show empty message if needed
            if (visibleCount === 0 && logBuffer.length > 0) {
                const emptyMsg = document.createElement('div');
                emptyMsg.className = 'log-ctrl';
                emptyMsg.textContent = 'No logs match current filters...';
                debugWindow.appendChild(emptyMsg);
            }
        }

        // Scroll to a specific timestamp
        async function scrollToTimestamp(timestamp) {
            if (!timestamp) {
                console.error('scrollToTimestamp: No timestamp provided');
                return;
            }

            // Check if we already have logs around this time in buffer
            let closestIndex = -1;
            let closestDistance = Infinity;

            for (let i = 0; i < logTimestamps.length; i++) {
                const distance = Math.abs(logTimestamps[i] - timestamp);
                if (distance < closestDistance) {
                    closestDistance = distance;
                    closestIndex = i;
                }
            }

            // If we have logs within 60 seconds, just scroll to that point
            if (closestIndex !== -1 && closestDistance < 60) {
                const targetElement = logElements[closestIndex];
                if (targetElement) {
                    targetElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    targetElement.style.backgroundColor = '#4a4a4a';
                    setTimeout(() => {
                        targetElement.style.backgroundColor = '';
                    }, 3000);
                    return;
                }
            }

            // Fetch logs around this timestamp from journal
            isLoading = true;

            try {
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        timestamp: timestamp,
                        before: 100,
                        after: 100
                    })
                });
                const data = await response.json();

                if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                    // Get timestamps of new logs
                    const newLogs = data.logs;
                    const newTimestamps = new Set(newLogs.map(log => log.timestamp));

                    // Remove existing logs that overlap with new ones (by timestamp)
                    const filteredBuffer = logBuffer.filter(log => !newTimestamps.has(log.timestamp));

                    // Merge and sort by timestamp
                    logBuffer = [...filteredBuffer, ...newLogs];
                    logBuffer.sort((a, b) => a.timestamp - b.timestamp);

                    // Rebuild display with new merged buffer
                    rebuildDebugDisplay();

                    // Find and highlight the target log
                    const targetLog = newLogs[data.target_index];
                    if (targetLog) {
                        // Find the element with matching timestamp
                        for (let i = 0; i < logTimestamps.length; i++) {
                            if (Math.abs(logTimestamps[i] - targetLog.timestamp) < 0.001) {
                                const targetElement = logElements[i];
                                if (targetElement) {
                                    targetElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                                    targetElement.style.backgroundColor = '#4a4a4a';
                                    setTimeout(() => {
                                        targetElement.style.backgroundColor = '';
                                    }, 3000);
                                    break;
                                }
                            }
                        }
                    } else {
                        // Just scroll to the first returned log
                        if (logElements.length > 0) {
                            logElements[0].scrollIntoView({ block: 'center', behavior: 'smooth' });
                        }
                    }
                } else {
                    // Show notification that no logs available for this time
                    const notification = document.createElement('div');
                    notification.style.cssText = 'position:fixed; bottom:100px; right:20px; background:#f44336; color:white; padding:10px; border-radius:5px; z-index:10000;';
                    notification.textContent = `No logs available for ${new Date(timestamp * 1000).toLocaleString()}`;
                    document.body.appendChild(notification);
                    setTimeout(() => notification.remove(), 3000);
                }
            } catch (e) {
                console.error('scrollToTimestamp: Error=', e);
            } finally {
                isLoading = false;
            }
        }

        // Go to time from datetime picker
        function gotoTime() {
            const input = document.getElementById('gotoTimeInput');
            if (!input || !input.value) return;

            // The input value is in local time (YYYY-MM-DDThh:mm)
            // Parse it as local time, not UTC
            const localString = input.value;
            const [year, month, day, hour, minute] = localString.split(/[-T:]/).map(Number);

            // Create a date in local time (months are 0-indexed in JS)
            const localDate = new Date(year, month - 1, day, hour, minute, 0, 0);
            const timestamp = localDate.getTime() / 1000;

            scrollToTimestamp(timestamp);
        }

        // Toggle auto-scroll lock
        function toggleAutoScrollLock() {
            autoScrollLocked = !autoScrollLocked;
            const btn = document.getElementById('scrollLockBtn');
            if (btn) {
                if (autoScrollLocked) {
                    btn.innerHTML = '🔓 Auto-scroll';
                    btn.style.background = '#dc3545';
                } else {
                    btn.innerHTML = '🔒 Auto-scroll';
                    btn.style.background = '#17a2b8';
                    // Scroll to bottom when re-enabling
                    if (debugWindow) {
                        debugWindow.scrollTop = debugWindow.scrollHeight;
                    }
                }
            }
        }

        // Override existing appendLogsFiltered function
        function appendLogsFiltered(newLogs) {
            if (!newLogs || newLogs.length === 0) return;

            for (const log of newLogs) {
                addNewLog(log);
            }
        }

        // Override clearLogs to reset buffer properly
        function clearLogs() {
            logBuffer = [];
            logElements = [];
            logTexts = [];
            logTimestamps = [];
            if (debugWindow) {
                debugWindow.innerHTML = 'Logs cleared...';
            }
            hasMoreDown = true;
            fetch('/api/logs/clear', {method: 'POST'})
                .then(() => {
                    setTimeout(() => loadHistoricalLogsAbove(), 100);
                })
                .catch(e => console.error('Error clearing logs:', e));
        }

        // Override setupDebugWindow to also handle auto-scroll toggle
        function setupDebugWindow() {
            debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;

            // Setup auto-scroll toggle button
            const scrollLockBtn = document.getElementById('scrollLockBtn');
            if (scrollLockBtn) {
                scrollLockBtn.onclick = toggleAutoScrollLock;
            }

            // Setup goto time button
            const gotoBtn = document.getElementById('gotoTimeBtn');
            if (gotoBtn) {
                gotoBtn.onclick = gotoTime;
            }

            // Set up initial datetime picker default to now
            const gotoInput = document.getElementById('gotoTimeInput');
            if (gotoInput) {
                const now = new Date();
                now.setSeconds(0, 0);

                // Get local timezone offset in minutes
                const tzOffset = now.getTimezoneOffset();
                const localNow = new Date(now.getTime() - tzOffset * 60000);
                gotoInput.value = localNow.toISOString().slice(0, 16);
            }
        }

        // Initialize everything
        function initDebugWindowSystem() {
            setupDebugWindow();
            initDebugWindow();
        }

        // ============ GRAPH VIEW CODE ============
        let currentView = 'grid';
        let graphCharts = {};

        const GRAPH_CONFIG = {
            rssi: {
                title: 'WiFi RSSI (dBm)',
                yAxisLabel: '',
                valueFormatter: (v) => v + ' dBm'
            },
            wifi_failures: {
                title: 'WiFi Connection Failures (per hour)',
                yAxisLabel: '',
                yAxisMin: 0,
                valueFormatter: (v) => v + ' failure' + (v !== 1 ? 's' : ''),
                isEvent: true
            },
            panics: {
                title: 'Software Panics (per hour)',
                yAxisLabel: '',
                yAxisMin: 0,
                valueFormatter: (v) => v + ' panic' + (v !== 1 ? 's' : ''),
                isEvent: true
            },
            ctrl_disconnects: {
                title: 'Controller Disconnections (per hour)',
                yAxisLabel: '',
                yAxisMin: 0,
                valueFormatter: (v) => v + ' disconnect' + (v !== 1 ? 's' : ''),
                isEvent: true
            },
            log_disconnects: {
                title: 'Log Server Disconnections (per hour)',
                yAxisLabel: '',
                yAxisMin: 0,
                valueFormatter: (v) => v + ' disconnect' + (v !== 1 ? 's' : ''),
                isEvent: true
            },
            heap: {
                title: 'Min Free Heap Memory (KB)',
                yAxisLabel: '',
                yAxisMin: 0,
                valueFormatter: (v) => {
                    if (v >= 1024 * 1024) return (v / (1024 * 1024)).toFixed(1) + ' MB';
                    if (v >= 1024) return (v / 1024).toFixed(0) + ' KB';
                    return v + ' B';
                }
            }
        };

        const TIME_RANGES = [
            { label: 'Last Hour', hours: 1 },
            { label: 'Last 6 Hours', hours: 6 },
            { label: 'Last 24 Hours', hours: 24 },
            { label: 'Last 3 Days', hours: 72 },
            { label: 'Last 7 Days', hours: 168 }
        ];

        let currentTimeRange = TIME_RANGES[0];

        function toggleView(view) {
            currentView = view;
            const gridWrapper = document.querySelector('.grid-wrapper');
            const graphContainer = document.getElementById('graphViewContainer');
            const footer = document.querySelector('.footer');

            if (view === 'graphs') {
                if (gridWrapper) gridWrapper.style.display = 'none';
                if (graphContainer) {
                    graphContainer.style.display = 'block';
                    if (!window._graphsInitialized) {
                        initializeGraphComponents();
                    }
                    loadGraphs();
                }
                updateViewSelector('graphs');
                // Change footer text for graph view
                if (footer) {
                    footer.innerHTML = '📈 Graph View - Double-click any graph to expand | Select nodes with Ctrl/Cmd+Click | Use time range dropdown';
                }
            } else {
                if (gridWrapper) gridWrapper.style.display = 'block';
                if (graphContainer) graphContainer.style.display = 'none';
                updateViewSelector('grid');
                // Restore original footer text
                if (footer) {
                    footer.innerHTML = 'FGR Controller - Drag ⋮⋮ to reorder nodes | Double-click card to expand | Drag blue bar above debug panel to resize | 📌 Dock returns to default size | Click header to collapse/expand';
                }
                setTimeout(() => {
                    if (typeof setupMetricsTicker === 'function') setupMetricsTicker();
                }, 100);
            }
        }

        function initializeGraphComponents() {
            window._graphsInitialized = true;
            // Fetch nodes and create selector only when needed
            fetch('/api/graph/nodes')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        console.error('Graphs not available:', data.error);
                        return;
                    }
                    createViewSelector();  // This creates the buttons
                    setupTimeRangeSelector();
                    setupGraphEventListeners();
                    loadAvailableNodes();
                })
                .catch(e => console.error('Graphs not available:', e));

            // Load ECharts if needed
            if (typeof echarts === 'undefined') {
                const script = document.createElement('script');
                script.src = 'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js';
                document.head.appendChild(script);
            }
        }

        function updateViewSelector(activeView) {
            let selector = document.getElementById('viewSelector');
            if (!selector) return;

            const btns = selector.querySelectorAll('.view-btn');
            btns.forEach(btn => {
                btn.classList.remove('active');
                if ((btn.textContent.includes('Dashboard') && activeView === 'grid') ||
                    (btn.textContent.includes('Metrics') && activeView === 'graphs')) {
                    btn.classList.add('active');
                }
            });
        }

        function createViewSelector() {
            if (document.getElementById('viewSelector')) return;

            const header = document.querySelector('.header');
            if (!header) return;

            const selector = document.createElement('div');
            selector.id = 'viewSelector';
            selector.style.cssText = 'display: flex; gap: 8px; margin-left: auto;';
            selector.innerHTML = `
                <button class="view-btn ${currentView === 'grid' ? 'active' : ''}" onclick="toggleView('grid')">📊 Dashboard</button>
                <button class="view-btn ${currentView === 'graphs' ? 'active' : ''}" onclick="toggleView('graphs')">📈 Graphs</button>
            `;
            header.appendChild(selector);
        }

        function createGraphViewContainer() {
            let container = document.getElementById('graphViewContainer');
            if (container) return container;

            const gridWrapper = document.querySelector('.grid-wrapper');
            if (!gridWrapper) return null;

            container = document.createElement('div');
            container.id = 'graphViewContainer';
            container.style.display = 'none';
            container.className = 'graph-view';
            container.innerHTML = `
                <div class="graph-header">
                    <div class="time-range-selector">
                        <select id="timeRangeSelect" class="time-range-select">
                            <option value="1">Last Hour</option>
                            <option value="6">Last 6 Hours</option>
                            <option value="24" selected>Last 24 Hours</option>
                            <option value="72">Last 3 Days</option>
                            <option value="168">Last 7 Days</option>
                        </select>
                    </div>
                    <div class="node-filter">
                        <select id="graphNodeFilter" class="node-filter-select" multiple size="2" style="min-width: 200px;">
                            <option value="all">All Nodes</option>
                        </select>
                        <button id="refreshGraphsBtn" style="padding: 4px 8px; font-size: 10px;">🔄 Refresh</button>
                    </div>
                </div>
                <div class="graph-grid" id="graphGrid">
                    <div class="graph-loading">Loading graphs...</div>
                </div>
            `;

            gridWrapper.parentNode.insertBefore(container, gridWrapper.nextSibling);
            return container;
        }

        async function loadGraphs() {
            const now = Math.floor(Date.now() / 1000);
            const startTime = now - (currentTimeRange.hours * 3600);
            const cacheKey = `${currentTimeRange.hours}_${Math.floor(startTime / 60)}`;

            if (simpleCache[cacheKey]) {
                renderGraphs(simpleCache[cacheKey]);
                return;
            }

            const graphGrid = document.getElementById('graphGrid');
            if (graphGrid) {
                    graphGrid.innerHTML = `<div style="grid-column:1/-1;text-align:center;padding:40px;">📊 Loading data... (can take a while on a littul Pi Zero)</div>`;
            }

            try {
                const response = await fetch('/api/graph/data', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        start_time: startTime,
                        end_time: now,
                        nodes: []
                    })
                });
                const data = await response.json();
                simpleCache[cacheKey] = data;
                renderGraphs(data);
            } catch (e) {
                console.error('Error loading graphs:', e);
            }
        }

        async function loadAvailableNodes() {
            try {
                const response = await fetch('/api/graph/nodes');
                const data = await response.json();

                if (data.error) {
                    console.warn('Graph nodes API error:', data.error);
                    return;
                }

                const nodeFilter = document.getElementById('graphNodeFilter');
                if (nodeFilter && data.nodes) {
                    const currentValue = nodeFilter.value;
                    nodeFilter.innerHTML = '<option value="all">All Nodes</option>';
                    for (const node of data.nodes) {
                        const displayName = node.name || node.ip;
                        nodeFilter.innerHTML += `<option value="${node.ip}">${displayName} (${node.ip})</option>`;
                    }
                    // Set to "All Nodes" if no previous selection or if previous selection was 'all'
                    if (currentValue === 'all' || !currentValue || !Array.from(nodeFilter.options).some(opt => opt.value === currentValue)) {
                        nodeFilter.value = 'all';
                    } else {
                        nodeFilter.value = currentValue;
                    }
                }
            } catch (e) {
                console.error('Error loading nodes:', e);
            }
        }

        // Show minute-by-minute modal
        async function showMinuteDrillDown(graphKey, timestamp, nodes) {
            const modal = document.getElementById('minuteModal');
            const title = document.getElementById('minuteModalTitle');
            const body = document.getElementById('minuteModalBody');

            // Get metric display name
            const config = GRAPH_CONFIG[graphKey];
            const date = new Date(timestamp);
            const hourStr = date.toLocaleString();

            title.textContent = `${config.title} - ${hourStr} (Detailed Events)`;
            body.innerHTML = '<div class="loading">Loading detailed data...</div>';
            modal.style.display = 'block';

            try {
                const response = await fetch('/api/graph/raw_minute_data', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        metric: graphKey,
                        timestamp: timestamp,
                        nodes: nodes
                    })
                });

                const data = await response.json();

                if (data.error) {
                    body.innerHTML = `<div class="minute-no-data">Error: ${data.error}</div>`;
                    return;
                }

                // Store the data for CSV export
                currentDrillDownData = data;

                // Build HTML for each node
                let html = '';
                const nodeEntries = Object.entries(data.data || {});

                if (nodeEntries.length === 0) {
                    body.innerHTML = '<div class="minute-no-data">No data available for this hour</div>';
                    return;
                }

                for (const [nodeIp, nodeInfo] of nodeEntries) {
                    const nodeName = nodeInfo.name;
                    const points = nodeInfo.data;
                    const deltaInfo = data.delta_summary?.[nodeIp] || {};

                    // Build events list (only rows where changes occurred)
                    let eventsHtml = '';
                    let prevValue = null;
                    let eventCount = 0;

                    for (let i = 0; i < points.length; i++) {
                        const point = points[i];
                        const time = new Date(point[0]);
                        const timeStr = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                        const value = point[1];
                        let delta = '';
                        let isEvent = false;

                        if (prevValue !== null && value > prevValue) {
                            delta = `+${value - prevValue}`;
                            isEvent = true;
                            eventCount++;
                        } else if (prevValue !== null && value < prevValue) {
                            delta = `↺ ${value}`;
                            isEvent = true;
                            eventCount++;
                        }
                        prevValue = value;

                        // Only include rows where something happened
                        if (isEvent) {
                            const pointTimestamp = point[0] / 1000; // Convert to seconds for scrollToTimestamp
                            eventsHtml += `
                                <tr class="clickable-row" data-timestamp="${pointTimestamp}" style="cursor: pointer;">
                                    <td style="white-space: nowrap;">${timeStr}</td>
                                    <td>${value}</td>
                                    <td>${delta}</td>
                                </tr>
                            `;
                        }
                    }

                    // Only show node section if there were events
                    if (eventCount > 0) {
                        html += `
                            <div class="minute-node-section">
                                <div class="minute-node-header" onclick="toggleMinuteNode(this)">
                                    <span class="minute-node-name">${nodeName} (${nodeIp})</span>
                                    <span class="minute-node-stats">
                                        ${deltaInfo.total_events ? `📊 ${deltaInfo.total_events} events this hour` : ''}
                                    </span>
                                </div>
                                <div class="minute-node-body" style="display: block;">
                                    <table class="minute-table">
                                        <thead>
                                            <tr>
                                                <th>Time</th>
                                                <th>Cumulative Value</th>
                                                <th>Change</th>
                                            </tr>
                                        </thead>
                                        <tbody>
                                            ${eventsHtml}
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        `;
                    }
                }

                if (html === '') {
                    body.innerHTML = '<div class="minute-no-data">No events detected for any node this hour</div>';
                } else {
                    body.innerHTML = html;

                    // Add click handlers to all rows
                    document.querySelectorAll('.clickable-row').forEach(row => {
                        row.addEventListener('click', (e) => {
                            e.stopPropagation();
                            const timestamp = parseFloat(row.getAttribute('data-timestamp'));
                            if (timestamp) {
                                modal.style.display = 'none';
                                scrollToTimestamp(timestamp);
                            }
                        });
                    });
                }

                // Attach copy button handler
                const copyBtn = document.getElementById('minuteModalCopyBtn');
                if (copyBtn) {
                    copyBtn.onclick = null;
                    copyBtn.onclick = copyMinuteDataToClipboard;
                }

                // Attach CSV button handler
                const csvBtn = document.getElementById('minuteModalCsvBtn');
                if (csvBtn) {
                    // Remove old handler to avoid duplicates
                    csvBtn.onclick = null;
                    csvBtn.onclick = exportMinuteDataCSV;
                }

            } catch (e) {
                console.error('Error loading minute data:', e);
                body.innerHTML = `<div class="minute-no-data">Error loading data: ${e.message}</div>`;
            }
        }

        // Toggle node section in modal
        function toggleMinuteNode(header) {
            const body = header.nextElementSibling;
            if (body.style.display === 'none') {
                body.style.display = 'block';
            } else {
                body.style.display = 'none';
            }
        }

        // Close modal
        function closeMinuteModal() {
            const modal = document.getElementById('minuteModal');
            modal.style.display = 'none';
        }

        async function exportMinuteDataCSV() {
            if (!currentDrillDownData) {
                alert('No data to export');
                return;
            }

            const data = currentDrillDownData;
            const metric = data.metric;
            const hourStart = new Date(data.hour_start).toISOString();
            const hourEnd = new Date(data.hour_end).toISOString();

            // Build CSV rows as an array of strings
            const csvRows = [];

            // Header with metadata
            csvRows.push(`# Metric: ${metric}`);
            csvRows.push(`# Hour: ${hourStart} to ${hourEnd}`);
            csvRows.push('');
            csvRows.push('Node IP,Node Name,Timestamp (UTC),Cumulative Value,Change');

            // Data rows - only include rows where something changed
            for (const [nodeIp, nodeInfo] of Object.entries(data.data)) {
                const nodeName = nodeInfo.name;
                const points = nodeInfo.data;

                let prevValue = null;
                let hasAnyChanges = false;

                // First pass to check if there are any changes for this node
                for (const point of points) {
                    const value = point[1];
                    if (prevValue !== null && value !== prevValue) {
                        hasAnyChanges = true;
                        break;
                    }
                    prevValue = value;
                }

                // Only include node if it had changes
                if (hasAnyChanges) {
                    prevValue = null;
                    for (const point of points) {
                        const timestamp = new Date(point[0]).toISOString();
                        const value = point[1];

                        let change = '';
                        let includeRow = false;

                        if (prevValue !== null) {
                            if (value > prevValue) {
                                change = `+${value - prevValue}`;
                                includeRow = true;
                            } else if (value < prevValue) {
                                change = `↺ ${value}`;
                                includeRow = true;
                            }
                        } else {
                            // First reading - always include as baseline
                            change = '(first reading)';
                            includeRow = true;
                        }

                        if (includeRow) {
                            const escapedNodeIp = `"${nodeIp}"`;
                            const escapedNodeName = `"${nodeName}"`;
                            csvRows.push(`${escapedNodeIp},${escapedNodeName},${timestamp},${value},${change}`);
                        }

                        prevValue = value;
                    }

                    // Add blank line between nodes for readability
                    csvRows.push('');
                }
            }

            // Add summary section
            if (data.delta_summary) {
                csvRows.push('');
                csvRows.push('# Summary (Total events per node)');
                csvRows.push('Node IP,Node Name,Total Events');
                for (const [nodeIp, summary] of Object.entries(data.delta_summary)) {
                    if (summary.total_events > 0) {
                        const nodeInfo = data.data[nodeIp];
                        const nodeName = nodeInfo ? nodeInfo.name : nodeIp;
                        csvRows.push(`"${nodeIp}","${nodeName}",${summary.total_events || 0}`);
                    }
                }
            }

            // Join with String.fromCharCode(10) for proper line breaks
            const csvString = csvRows.join(String.fromCharCode(10));

            // Add BOM for UTF-8 and create download
            const blob = new Blob(['\uFEFF' + csvString], { type: 'text/csv;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.setAttribute('download', `${data.metric}_drilldown_${new Date(data.hour_start).toISOString().slice(0, 19).replace(/:/g, '-')}.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
        }

        async function copyMinuteDataToClipboard() {
            if (!currentDrillDownData) {
                alert('No data to copy');
                return;
            }

            const data = currentDrillDownData;
            const rows = [];

            rows.push(`Metric: ${data.metric}`);
            rows.push(`Hour: ${new Date(data.hour_start).toISOString()} to ${new Date(data.hour_end).toISOString()}`);
            rows.push('');
            rows.push('Node IP\tNode Name\tTimestamp (UTC)\tCumulative Value\tChange');

            for (const [nodeIp, nodeInfo] of Object.entries(data.data)) {
                const nodeName = nodeInfo.name;
                const points = nodeInfo.data;
                let prevValue = null;
                let hasAnyChanges = false;

                // First pass to check for changes
                for (const point of points) {
                    const value = point[1];
                    if (prevValue !== null && value !== prevValue) {
                        hasAnyChanges = true;
                        break;
                    }
                    prevValue = value;
                }

                if (hasAnyChanges) {
                    prevValue = null;
                    for (const point of points) {
                        const timestamp = new Date(point[0]).toISOString();
                        const value = point[1];
                        let change = '';
                        let includeRow = false;

                        if (prevValue !== null) {
                            if (value > prevValue) {
                                change = `+${value - prevValue}`;
                                includeRow = true;
                            } else if (value < prevValue) {
                                change = `↺ ${value}`;
                                includeRow = true;
                            }
                        } else {
                            change = '(first)';
                            includeRow = true;
                        }

                        if (includeRow) {
                            rows.push(`${nodeIp}\t${nodeName}\t${timestamp}\t${value}\t${change}`);
                        }
                        prevValue = value;
                    }
                    rows.push('');
                }
            }

            const text = rows.join(String.fromCharCode(10));
            await navigator.clipboard.writeText(text);

            const btn = document.getElementById('minuteModalCopyBtn');
            if (btn) {
                const originalText = btn.textContent;
                btn.textContent = 'Copied!';
                setTimeout(() => {
                    btn.textContent = originalText;
                }, 2000);
            }
        }

        // Set up modal close handlers
        function setupMinuteModal() {
            const modal = document.getElementById('minuteModal');
            const closeBtn = document.querySelector('.minute-modal-close');

            if (closeBtn) {
                closeBtn.onclick = function() {
                    modal.style.display = 'none';
                };
            }

            // Also close when clicking outside the modal content
            window.onclick = function(event) {
                if (event.target === modal) {
                    modal.style.display = 'none';
                }
            };

            // Add Escape key handler
            document.addEventListener('keydown', function(event) {
                if (event.key === 'Escape' && modal.style.display === 'block') {
                    modal.style.display = 'none';
                }
            });
        }

        function getNodeNameByIp(ip) {
            for (const [name, node] of Object.entries(nodesData)) {
                if (node.ip === ip) return name;
            }
            return ip;
        }

        function generateColors(count) {
            const baseColors = ['#5470c6', '#fac858', '#ee6666', '#73c0de', '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc'];
            if (count <= baseColors.length) return baseColors.slice(0, count);
            const colors = [...baseColors];
            for (let i = baseColors.length; i < count; i++) {
                const hue = (i * 137) % 360;
                colors.push(`hsl(${hue}, 70%, 55%)`);
            }
            return colors;
        }

        function exportGraphCSV(graphKey) {
            const seriesData = rawGraphData[graphKey];

            if (!seriesData || Object.keys(seriesData).length === 0) {
                console.error('No data found for', graphKey);
                return;
            }

            // Collect all unique timestamps
            const allTimestamps = new Set();
            for (const [nodeIp, points] of Object.entries(seriesData)) {
                for (const point of points) {
                    allTimestamps.add(point[0]);
                }
            }

            const timestamps = Array.from(allTimestamps).sort((a, b) => a - b);

            // Build headers
            let headers = ['Timestamp (UTC)'];
            const nodePoints = [];

            for (const [nodeIp, points] of Object.entries(seriesData)) {
                const nodeName = getNodeNameByIp(nodeIp);
                headers.push(`${nodeName} (${nodeIp})`);
                nodePoints.push(points);
            }

            // Build CSV rows as array of strings
            const csvRows = [];
            csvRows.push(headers.join(','));

            for (const timestamp of timestamps) {
                const dateStr = new Date(timestamp).toISOString();
                const row = [dateStr];

                for (let i = 0; i < nodePoints.length; i++) {
                    const points = nodePoints[i];
                    let value = '';
                    for (const point of points) {
                        if (point[0] === timestamp) {
                            value = point[1];
                            break;
                        }
                    }
                    row.push(value);
                }

                csvRows.push(row.join(','));
            }

            // Use String.fromCharCode(10) for newline - this is the fix that works
            const csvString = csvRows.join(String.fromCharCode(10));

            // Create blob and download
            const blob = new Blob([csvString], { type: 'text/csv;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.setAttribute('download', `${graphKey}_data.csv`);
            document.body.appendChild(link);
            link.click();
            document.body.removeChild(link);
            URL.revokeObjectURL(url);
        }

        function saveGraphAsImage(graphKey) {
            const chart = graphCharts[graphKey];
            if (chart) {
                const url = chart.getDataURL({
                    type: 'png',
                    pixelRatio: 2,
                    backgroundColor: '#fff'
                });
                const link = document.createElement('a');
                link.download = `${graphKey}_chart.png`;
                link.href = url;
                link.click();
            }
        }

        function resetGraphZoom(graphKey) {
            const chart = graphCharts[graphKey];
            if (chart) {
                chart.dispatchAction({
                    type: 'dataZoom',
                    start: 0,
                    end: 100
                });
            }
        }

        function setupGraphTimeClickHandler(graphKey, chart, container) {
            const zr = chart.getZr();

            // Remove any existing handler
            zr.off('click');

            zr.on('click', function(params) {
                // Get the plot area (where the actual data is drawn)
                let plotAreaBottom = null;

                try {
                    const model = chart.getModel();
                    const gridComponent = model.getComponent('grid');
                    if (gridComponent && gridComponent.coordinateSystem) {
                        const rect = gridComponent.coordinateSystem.getRect();
                        plotAreaBottom = rect.y + rect.height;
                    }
                } catch(e) {
                    // Fallback if grid detection fails
                    plotAreaBottom = chart.getHeight() * 0.7;
                }

                // Ignore clicks below the plot area (legend area)
                if (plotAreaBottom !== null && params.offsetY > plotAreaBottom) {
                    return; // Legend click - ignore
                }

                // Convert pixel coordinates to chart coordinates (time, value)
                const pointInPixel = [params.offsetX, params.offsetY];
                const pointInGrid = chart.convertFromPixel({ seriesIndex: 0 }, pointInPixel);

                if (pointInGrid && pointInGrid[0]) {
                    const timestamp = pointInGrid[0];
                    const timestampSeconds = timestamp / 1000;

                    // Jump to this time in the logs
                    scrollToTimestamp(timestampSeconds);

                    // Visual feedback - show a brief flash at click position
                    const flash = document.createElement('div');
                    flash.style.cssText = `
                        position: absolute;
                        left: ${params.offsetX - 8}px;
                        top: ${params.offsetY - 8}px;
                        width: 16px;
                        height: 16px;
                        background: #2196f3;
                        border-radius: 50%;
                        pointer-events: none;
                        z-index: 1000;
                        animation: pulse 0.3s ease-out;
                    `;
                    container.style.position = 'relative';
                    container.appendChild(flash);

                    // Collapse the expanded graph after a short delay
                    setTimeout(() => {
                        const graphCard = document.querySelector(`.graph-card[data-graph="${graphKey}"]`);
                        if (graphCard && graphCard.classList.contains('expanded')) {
                            toggleGraphExpand(graphKey);
                        }
                    }, 200);

                    setTimeout(() => flash.remove(), 300);
                }
            });

            // Add visual cursor hint
            container.style.cursor = 'crosshair';

            // Add CSS animation if not already present
            if (!document.querySelector('#pulse-style')) {
                const style = document.createElement('style');
                style.id = 'pulse-style';
                style.textContent = `
                    @keyframes pulse {
                        0% { transform: scale(0.5); opacity: 1; }
                        100% { transform: scale(6); opacity: 0; }
                    }
                `;
                document.head.appendChild(style);
            }
        }

        function renderGraphs(data) {
            const graphGrid = document.getElementById('graphGrid');
            if (!graphGrid) return;

            const graphs = ['rssi', 'wifi_failures', 'panics', 'ctrl_disconnects', 'log_disconnects', 'heap'];
            let html = '';

            // Calculate the requested time range in milliseconds
            const now = Date.now();
            const requestedStart = now - (currentTimeRange.hours * 3600 * 1000);

            for (const graphKey of graphs) {
                const config = GRAPH_CONFIG[graphKey];
                html += `
                    <div class="graph-card" data-graph="${graphKey}">
                        <div class="graph-title">
                            <span>${config.title}</span>
                            <div style="display: flex; gap: 8px; align-items: center;">
                                <button class="graph-tool-btn" onclick="exportGraphCSV('${graphKey}')" title="Export as CSV">📊 CSV</button>
                                <button class="graph-tool-btn" onclick="saveGraphAsImage('${graphKey}')" title="Save as Image">💾</button>
                                <button class="graph-tool-btn" onclick="resetGraphZoom('${graphKey}')" title="Reset Zoom">⟳</button>
                            </div>
                        </div>
                        <div id="graph-${graphKey}" class="graph-container"></div>
                    </div>
                `;
            }

            graphGrid.innerHTML = html;

            setTimeout(() => {
                const nodeFilter = document.getElementById('graphNodeFilter');
                const selectedNodes = nodeFilter ? Array.from(nodeFilter.selectedOptions).map(opt => opt.value) : [];
                const includeAllNodes = selectedNodes.includes('all') || selectedNodes.length === 0;

                for (const graphKey of graphs) {
                    const config = GRAPH_CONFIG[graphKey];
                    const seriesData = data[graphKey] || {};

                    const filteredData = {};
                    for (const [nodeIp, points] of Object.entries(seriesData)) {
                        if (includeAllNodes || selectedNodes.includes(nodeIp)) {
                            const filteredPoints = points.filter(point => {
                                const timestamp = point[0];
                                return timestamp >= requestedStart && timestamp <= now;
                            });
                            if (filteredPoints.length > 0) {
                                filteredData[nodeIp] = filteredPoints;
                            }
                        }
                    }

                    // Build sorted timestamps array for this graph (used for drill-down)
                    const timestampsSet = new Set();
                    for (const [nodeIp, points] of Object.entries(filteredData)) {
                        for (const point of points) {
                            timestampsSet.add(point[0]);
                        }
                    }
                    const graphTimestamps = Array.from(timestampsSet).sort((a, b) => a - b);

                    // Debug: log the filtered range
                    let minTs = Infinity, maxTs = -Infinity;
                    for (const [nodeIp, points] of Object.entries(filteredData)) {
                        for (const point of points) {
                            minTs = Math.min(minTs, point[0]);
                            maxTs = Math.max(maxTs, point[0]);
                        }
                    }

                    const container = document.getElementById(`graph-${graphKey}`);
                    if (!container) continue;

                    // Store the RAW data for export
                    rawGraphData[graphKey] = filteredData;
                    currentChartData[graphKey] = filteredData;

                    if (Object.keys(filteredData).length === 0) {
                        const chart = echarts.init(container);
                        chart.setOption({
                            title: {
                                show: true,
                                text: 'None',
                                left: 'center',
                                top: 'center',
                                textStyle: { color: '#999', fontSize: 12 }
                            }
                        });
                        graphCharts[graphKey] = chart;
                        continue;
                    }

                    const chart = echarts.init(container);
                    const option = buildChartOption(graphKey, filteredData, config);
                    chart.setOption(option);
                    chart.option = option;

                    // Store timestamps on the chart for drill-down (needed for category axis)
                    chart.timestamps = graphTimestamps;

                    graphCharts[graphKey] = chart;

                    if (config.isEvent) {
                        // Bar chart (event data) - show drill-down modal on click
                        chart.off('click');
                        chart.on('click', function(params) {
                            if (params.dataIndex !== undefined && chart.timestamps && chart.timestamps[params.dataIndex]) {
                                const timestamp = chart.timestamps[params.dataIndex];
                                chart.dispatchAction({
                                    type: 'showTip',
                                    seriesIndex: params.seriesIndex,
                                    dataIndex: params.dataIndex
                                });
                                setTimeout(() => {
                                    chart.dispatchAction({ type: 'hideTip' });
                                }, 800);

                                const nodeFilter = document.getElementById('graphNodeFilter');
                                const selectedNodes = nodeFilter ? Array.from(nodeFilter.selectedOptions).map(opt => opt.value) : ['all'];
                                showMinuteDrillDown(graphKey, timestamp, selectedNodes);
                            }
                        });
                    } else {
                        // Line chart (RSSI, Heap) - use the shared click handler function
                        setupGraphTimeClickHandler(graphKey, chart, container);

                        // Add CSS animation
                        if (!document.querySelector('#pulse-style')) {
                            const style = document.createElement('style');
                            style.id = 'pulse-style';
                            style.textContent = `
                                @keyframes pulse {
                                    0% { transform: scale(0.5); opacity: 1; }
                                    100% { transform: scale(6); opacity: 0; }
                                }
                            `;
                            document.head.appendChild(style);
                        }
                    }

                    // Double-click on graph card for expand
                    const graphCard = container.closest('.graph-card');
                    if (graphCard) {
                        graphCard.ondblclick = null;
                        graphCard.ondblclick = (e) => {
                            if (e.target.tagName === 'BUTTON') return;
                            e.stopPropagation();
                            toggleGraphExpand(graphKey);
                        };
                    }
                }
            }, 50);
        }

        function buildChartOption(graphKey, seriesData, config) {
            const series = [];
            const colors = generateColors(Object.keys(seriesData).length);
            let colorIndex = 0;

            // For event charts, collect all unique timestamps across all nodes
            let allTimestamps = [];
            if (config.isEvent) {
                const timestampSet = new Set();
                for (const [nodeIp, points] of Object.entries(seriesData)) {
                    for (const point of points) {
                        timestampSet.add(point[0]);
                    }
                }
                allTimestamps = Array.from(timestampSet).sort((a, b) => a - b);
            }

            const sortedNodes = Object.entries(seriesData).sort((a, b) => {
                const lastOctetA = parseInt(a[0].split('.').pop());
                const lastOctetB = parseInt(b[0].split('.').pop());
                return lastOctetA - lastOctetB;
            });

            for (const [nodeIp, points] of sortedNodes) {
                if (!points || points.length === 0) continue;

                const nodeName = getNodeNameByIp(nodeIp);
                const displayName = `${nodeName}\n(${nodeIp})`;
                const seriesColor = colors[colorIndex % colors.length];
                colorIndex++;

                if (config.isEvent) {
                    // For category axis, align points to timestamp indices
                    const alignedData = allTimestamps.map(ts => {
                        const point = points.find(p => p[0] === ts);
                        return point ? point[1] : 0;
                    });

                    // Calculate optimal bar width percentage based on number of nodes
                    // We want total width used = (barWidth% * numNodes) + (barGap% * (numNodes - 1)) to be <= 90%
                    // Solving for barWidth%: barWidth% = (90% - (barGap% * (numNodes - 1))) / numNodes
                    const numNodes = Object.keys(seriesData).length;
                    const barGapPercent = 2;  // 2% gap between bars
                    const targetUtilization = 85;  // Target percentage of category width to use (leaves 15% padding)

                    // Calculate bar width percentage
                    let barWidthPercentage = (targetUtilization - (barGapPercent * (numNodes - 1))) / numNodes;
                    // Clamp between 5% and 30%
                    barWidthPercentage = Math.min(30, Math.max(5, barWidthPercentage));
                    // Format as percentage string
                    const barWidthPercentStr = barWidthPercentage.toFixed(0) + '%';

                    series.push({
                        name: displayName,
                        type: 'bar',
                        data: alignedData,
                        barWidth: barWidthPercentStr,
                        barGap: barGapPercent + '%',
                        barCategoryGap: '15%',  // Space between categories (timestamps)
                        color: seriesColor,
                        itemStyle: { color: seriesColor, borderRadius: [2, 2, 0, 0], borderColor: 'rgba(0,0,0,0.2)', borderWidth: 0.5 },
                        label: { show: false },
                        emphasis: { focus: 'series' }
                    });

                } else {
                    // Line chart for continuous metrics (RSSI, heap)
                    series.push({
                        name: displayName,
                        type: 'line',
                        data: points,
                        smooth: true,
                        connectNulls: false,
                        showSymbol: false,
                        color: seriesColor,
                        lineStyle: { width: 2, color: seriesColor }
                    });
                }
            }

            if (config.isEvent) {
                // Category axis for event charts - UNCHANGED
                let yMax = 0;
                for (const s of series) {
                    const maxVal = Math.max(...s.data);
                    if (maxVal > yMax) yMax = maxVal;
                }
                const yPadding = yMax * 0.1;

                return {
                    tooltip: {
                        trigger: 'axis',
                        axisPointer: { type: 'shadow' },
                        formatter: function(params) {
                            if (!params || params.length === 0) return '';
                            const timestamp = allTimestamps[params[0].dataIndex];
                            const time = new Date(timestamp).toLocaleString();
                            let html = `<strong>${time}</strong><br/>`;
                            html += `<hr style="margin: 4px 0; border-color: #ddd;"/>`;
                            let total = 0;
                            for (const p of params) {
                                if (p.value > 0) {
                                    html += `<span style="color:${p.color}">●</span> ${p.seriesName}: ${config.valueFormatter(p.value)}<br/>`;
                                    total += p.value;
                                }
                            }
                            if (total > 0) {
                                html += `<hr style="margin: 4px 0; border-color: #ddd;"/>`;
                                html += `<strong>Total: ${config.valueFormatter(total)}</strong>`;
                            }
                            return html;
                        },
                        backgroundColor: 'rgba(50,50,50,0.95)',
                        borderColor: '#333',
                        borderWidth: 1,
                        textStyle: { color: '#fff', fontSize: 11 }
                    },
                    xAxis: {
                        type: 'category',
                        data: allTimestamps.map(ts => {
                            const date = new Date(ts);
                            const hours = date.getHours().toString().padStart(2, '0');
                            const minutes = date.getMinutes().toString().padStart(2, '0');
                            const day = date.getDate();
                            const month = date.getMonth() + 1;
                            const rangeHours = currentTimeRange ? currentTimeRange.hours : 24;

                            if (rangeHours >= 72) {
                                if (hours === '00' && minutes === '00') {
                                    return `${day}/${month}`;
                                }
                                return `${hours}:${minutes}`;
                            }
                            if (rangeHours > 24) {
                                if (hours === '00' && minutes === '00') {
                                    return `${day}/${month}`;
                                }
                                return `${hours}:${minutes}`;
                            }
                            if (rangeHours > 6) {
                                if (hours === '00' && minutes === '00') {
                                    return `${day}/${month}`;
                                }
                                return `${hours}:${minutes}`;
                            }
                            return `${hours}:${minutes}`;
                        }),
                        axisLabel: {
                            fontSize: 10,
                            margin: 8,
                            rotate: 0,
                            interval: function(index, value) {
                                const rangeHours = currentTimeRange ? currentTimeRange.hours : 24;
                                const timestamp = allTimestamps[index];
                                const date = new Date(timestamp);
                                const hours = date.getHours();

                                if (rangeHours >= 72) {
                                    return hours === 0;
                                }
                                if (rangeHours > 24) {
                                    return hours % 6 === 0;
                                }
                                return true;
                            }
                        },
                        axisLine: { lineStyle: { color: '#888' } },
                        axisTick: { show: true, alignWithLabel: true },
                        boundaryGap: true
                    },
                    yAxis: {
                        type: 'value',
                        name: config.yAxisLabel || '',
                        min: 0,
                        max: yMax + yPadding,
                        nameLocation: 'middle',
                        nameGap: 35,
                        axisLabel: '',
                        splitLine: { lineStyle: { type: 'dashed', color: '#e0e0e0' } }
                    },
                    series: series,
                    grid: { left: '8%', right: '5%', top: '8%', bottom: '18%', containLabel: true, backgroundColor: '#fafafa' },
                    legend: {
                        type: 'scroll',
                        orient: 'horizontal',
                        bottom: 0,
                        left: 'center',
                        textStyle: { fontSize: 7, lineHeight: 9 },
                        itemWidth: 12,
                        itemHeight: 4,
                        icon: 'roundRect',
                        backgroundColor: 'transparent',
                        itemGap: 4,
                        pageIconColor: '#666',
                        pageTextStyle: { color: '#666' },
                        formatter: function(name) {
                            if (name.length > 30) return name.substring(0, 27) + '...';
                            return name;
                        }
                    },
                    media: [
                        {
                            query: { minWidth: 800, minHeight: 500 },
                            option: {
                                xAxis: { axisLabel: { fontSize: 14 } },
                                yAxis: { axisLabel: { fontSize: 12 } },
                                legend: { textStyle: { fontSize: 12, lineHeight: 16 }, itemWidth: 20, itemHeight: 8 },
                                grid: { bottom: 30 }
                            }
                        }
                    ]
                };
            } else {
                // Time axis for line charts (rssi, heap) - MODIFIED with axisPointer
                const now = Date.now();
                const requestedStart = now - (currentTimeRange.hours * 3600 * 1000);

                let yMin = Infinity, yMax = -Infinity;
                for (const points of Object.values(seriesData)) {
                    for (const point of points) {
                        const value = point[1];
                        if (value < yMin) yMin = value;
                        if (value > yMax) yMax = value;
                    }
                }
                if (yMin !== Infinity) {
                    const range = yMax - yMin;
                    const padding = range * 0.1;
                    yMin = Math.floor(yMin - padding);
                    yMax = Math.ceil(yMax + padding);
                    if (graphKey === 'rssi') {
                        yMin = Math.max(-100, yMin);
                        yMax = Math.min(-30, yMax);
                    } else if (graphKey === 'heap') {
                        yMin = Math.max(0, yMin);
                    }
                }

                return {
                    tooltip: {
                        trigger: 'axis',
                        axisPointer: {
                            type: 'line',
                            snap: true,
                            triggerOn: 'mousemove|click',
                            label: {
                                show: true,
                                formatter: function(params) {
                                    return new Date(params.value).toLocaleTimeString();
                                }
                            }
                        },
                        formatter: function(params) {
                            if (!params || params.length === 0) return '';
                            const time = new Date(params[0].value[0]).toLocaleString();
                            let html = `<strong>${time}</strong><br/>`;
                            for (const p of params) {
                                html += `<span style="color:${p.color}">●</span> ${p.seriesName}: ${config.valueFormatter(p.value[1])}<br/>`;
                            }
                            return html;
                        },
                        backgroundColor: 'rgba(50,50,50,0.95)',
                        borderColor: '#333',
                        borderWidth: 1,
                        textStyle: { color: '#fff', fontSize: 11 }
                    },
                    xAxis: {
                        type: 'time',
                        name: '',
                        min: Number(requestedStart),
                        max: Number(now),
                        boundaryGap: false,
                        scale: false,
                        axisPointer: {
                            show: true,
                            type: 'line',
                            snap: true,
                            label: {
                                show: true,
                                formatter: function(params) {
                                    const date = new Date(params.value);
                                    return date.toLocaleTimeString();
                                }
                            }
                        },
                        axisLabel: {
                            fontSize: 8,
                            margin: 4,
                            formatter: function(value, index) {
                                const date = new Date(value);
                                const hours = date.getHours().toString().padStart(2, '0');
                                const minutes = date.getMinutes().toString().padStart(2, '0');
                                const day = date.getDate();
                                const month = date.getMonth() + 1;
                                const rangeHours = currentTimeRange ? currentTimeRange.hours : 24;
                                if (rangeHours > 24) {
                                    if (hours === '00' && minutes === '00') return `${day}/${month}`;
                                    return `${hours}:${minutes}`;
                                }
                                if (rangeHours > 6) {
                                    if (hours === '00' && minutes === '00') return `${day}/${month}`;
                                    return `${hours}:${minutes}`;
                                }
                                return `${hours}:${minutes}`;
                            }
                        },
                        axisLine: { lineStyle: { color: '#888' } },
                        splitLine: { show: false },
                        minorTick: { show: false }
                    },
                    yAxis: {
                        type: 'value',
                        name: config.yAxisLabel || '',
                        min: yMin,
                        max: yMax,
                        nameLocation: 'middle',
                        nameGap: 35,
                        axisLabel: {
                            fontSize: 10,
                            formatter: function(value) {
                                if (graphKey === 'heap') return Math.round(value / 1024);
                                return value;
                            }
                        },
                        splitLine: { lineStyle: { type: 'dashed', color: '#e0e0e0' } }
                    },
                    series: series,
                    grid: { left: '8%', right: '5%', top: '8%', bottom: '18%', containLabel: true, backgroundColor: '#fafafa' },
                    legend: {
                        type: 'scroll',
                        orient: 'horizontal',
                        bottom: 0,
                        left: 'center',
                        textStyle: { fontSize: 7, lineHeight: 9 },
                        itemWidth: 12,
                        itemHeight: 4,
                        icon: 'circle',
                        backgroundColor: 'transparent',
                        itemGap: 4,
                        pageIconColor: '#666',
                        pageTextStyle: { color: '#666' },
                        formatter: function(name) {
                            if (name.length > 30) return name.substring(0, 27) + '...';
                            return name;
                        }
                    },
                    dataZoom: [{
                        type: 'inside',
                        startValue: requestedStart,
                        endValue: now,
                        zoomOnMouseWheel: true,
                        moveOnMouseMove: true
                    }],
                    media: [
                        {
                            query: { minWidth: 800, minHeight: 500 },
                            option: {
                                xAxis: { axisLabel: { fontSize: 14 } },
                                yAxis: { axisLabel: { fontSize: 12 } },
                                legend: { textStyle: { fontSize: 12, lineHeight: 16 }, itemWidth: 20, itemHeight: 8 },
                                grid: { bottom: 30 }
                            }
                        }
                    ]
                };
            }
        }

        function toggleGraphExpand(graphKey) {
            const graphCard = document.querySelector(`.graph-card[data-graph="${graphKey}"]`);
            if (!graphCard) return;

            const isExpanded = graphCard.classList.contains('expanded');
            const container = document.getElementById(`graph-${graphKey}`);
            if (!container || !rawGraphData[graphKey]) return;

            const config = GRAPH_CONFIG[graphKey];
            const data = rawGraphData[graphKey];

            // Completely destroy the old chart
            if (graphCharts[graphKey]) {
                graphCharts[graphKey].dispose();
                delete graphCharts[graphKey];
            }

            // Clear the container
            container.innerHTML = '';

            if (isExpanded) {
                // Collapse
                graphCard.classList.remove('expanded');
                const overlay = document.getElementById('graph-overlay');
                if (overlay) overlay.remove();
                if (window._escapeHandler) {
                    document.removeEventListener('keydown', window._escapeHandler);
                    window._escapeHandler = null;
                }
                graphCard.style.position = '';
                graphCard.style.top = '';
                graphCard.style.left = '';
                graphCard.style.transform = '';
                graphCard.style.zIndex = '';
                graphCard.style.width = '';
                graphCard.style.height = '';

                // Create NEW chart with small fonts
                const option = buildChartOption(graphKey, data, config);
                if (option.xAxis) option.xAxis.axisLabel = { fontSize: 8, margin: 4 };
                if (option.yAxis) option.yAxis.axisLabel = { fontSize: 10 };
                if (option.legend) {
                    option.legend.textStyle = { fontSize: 7, lineHeight: 9 };
                    option.legend.itemWidth = 10;
                    option.legend.itemHeight = 4;
                }
                delete option.media;

                const chart = echarts.init(container);
                chart.setOption(option);
                chart.option = option;
                graphCharts[graphKey] = chart;

                // Re-attach click handler based on chart type
                if (config.isEvent) {
                    // Bar chart (event data) - show drill-down modal on click
                    chart.off('click');
                    chart.on('click', function(params) {
                        if (params.dataIndex !== undefined && chart.timestamps && chart.timestamps[params.dataIndex]) {
                            const timestamp = chart.timestamps[params.dataIndex];
                            chart.dispatchAction({
                                type: 'showTip',
                                seriesIndex: params.seriesIndex,
                                dataIndex: params.dataIndex
                            });
                            setTimeout(() => {
                                chart.dispatchAction({ type: 'hideTip' });
                            }, 800);

                            const nodeFilter = document.getElementById('graphNodeFilter');
                            const selectedNodes = nodeFilter ? Array.from(nodeFilter.selectedOptions).map(opt => opt.value) : ['all'];
                            showMinuteDrillDown(graphKey, timestamp, selectedNodes);
                        }
                    });
                } else {
                    // Line chart - use the shared click handler
                    setupGraphTimeClickHandler(graphKey, chart, container);
                }

            } else {
                // Expand
                graphCard.classList.add('expanded');

                const overlay = document.createElement('div');
                overlay.id = 'graph-overlay';
                overlay.style.cssText = `
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    bottom: 0;
                    background: rgba(0,0,0,0.5);
                    z-index: 9999;
                `;
                overlay.onclick = () => toggleGraphExpand(graphKey);
                document.body.appendChild(overlay);

                graphCard.style.position = 'fixed';
                graphCard.style.top = '50%';
                graphCard.style.left = '50%';
                graphCard.style.transform = 'translate(-50%, -50%)';
                graphCard.style.zIndex = '10000';
                graphCard.style.width = '80vw';
                graphCard.style.height = '80vh';

                const escapeHandler = (e) => {
                    if (e.key === 'Escape') {
                        toggleGraphExpand(graphKey);
                    }
                };
                document.addEventListener('keydown', escapeHandler);
                window._escapeHandler = escapeHandler;

                // Create NEW chart with large fonts
                const option = buildChartOption(graphKey, data, config);
                if (option.xAxis) option.xAxis.axisLabel = { fontSize: 14, margin: 8 };
                if (option.yAxis) option.yAxis.axisLabel = { fontSize: 12 };
                if (option.legend) {
                    option.legend.textStyle = { fontSize: 12, lineHeight: 16 };
                    option.legend.itemWidth = 20;
                    option.legend.itemHeight = 8;
                }
                delete option.media;

                const chart = echarts.init(container);
                chart.setOption(option);
                chart.option = option;
                graphCharts[graphKey] = chart;

                // Re-attach click handler based on chart type
                if (config.isEvent) {
                    // Bar chart (event data) - show drill-down modal on click
                    chart.off('click');
                    chart.on('click', function(params) {
                        if (params.dataIndex !== undefined && chart.timestamps && chart.timestamps[params.dataIndex]) {
                            const timestamp = chart.timestamps[params.dataIndex];
                            chart.dispatchAction({
                                type: 'showTip',
                                seriesIndex: params.seriesIndex,
                                dataIndex: params.dataIndex
                            });
                            setTimeout(() => {
                                chart.dispatchAction({ type: 'hideTip' });
                            }, 800);

                            const nodeFilter = document.getElementById('graphNodeFilter');
                            const selectedNodes = nodeFilter ? Array.from(nodeFilter.selectedOptions).map(opt => opt.value) : ['all'];
                            showMinuteDrillDown(graphKey, timestamp, selectedNodes);
                        }
                    });
                } else {
                    // Line chart - use the shared click handler
                    setupGraphTimeClickHandler(graphKey, chart, container);
                }
            }

            // Force a final resize
            setTimeout(() => {
                if (graphCharts[graphKey]) {
                    graphCharts[graphKey].resize();
                }
            }, 50);
        }

        function setupGraphEventListeners() {
            const refreshBtn = document.getElementById('refreshGraphsBtn');
            if (refreshBtn) {
                refreshBtn.onclick = () => {
                    // Clear the simple cache for current range to force re-fetch
                    const now = Math.floor(Date.now() / 1000);
                    const startTime = now - (currentTimeRange.hours * 3600);
                    const cacheKey = `${currentTimeRange.hours}_${Math.floor(startTime / 60)}`;
                    delete simpleCache[cacheKey];
                    loadGraphs();
                    refreshBtn.style.background = '#28a745';
                    setTimeout(() => {
                        refreshBtn.style.background = '';
                    }, 200);
                };
            }

            const nodeFilter = document.getElementById('graphNodeFilter');
            if (nodeFilter) {
                nodeFilter.onchange = () => {
                    // Find current cache key and re-render
                    const now = Math.floor(Date.now() / 1000);
                    const startTime = now - (currentTimeRange.hours * 3600);
                    const cacheKey = `${currentTimeRange.hours}_${Math.floor(startTime / 60)}`;
                    if (simpleCache[cacheKey]) {
                        renderGraphs(simpleCache[cacheKey]);
                    } else {
                        loadGraphs();
                    }
                };
            }
        }

        function setupTimeRangeSelector() {
            const selector = document.getElementById('timeRangeSelect');
            if (!selector) return;

            // Make sure currentTimeRange is initialized
            if (!currentTimeRange) {
                currentTimeRange = TIME_RANGES.find(r => r.hours === 24) || TIME_RANGES[2];
            }

            // Set the selector value to match currentTimeRange
            selector.value = currentTimeRange.hours;

            selector.onchange = () => {
                const hours = parseInt(selector.value);
                const selected = TIME_RANGES.find(r => r.hours === hours);
                if (selected) {
                    currentTimeRange = selected;
                    loadGraphs();
                }
            };
        }

        async function initGraphView() {
            createGraphViewContainer();
            createViewSelector();
            setupTimeRangeSelector();
            setupGraphEventListeners();
            setupMinuteModal();
            requestAnimationFrame(() => loadAvailableNodes());

            if (typeof echarts === 'undefined') {
                const script = document.createElement('script');
                script.src = 'https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js';
                script.onload = () => {
                    console.log('ECharts loaded');
                    loadGraphs();
                };
                document.head.appendChild(script);
            } else {
                console.log('ECharts already loaded');
                loadGraphs();
            }
        }

        function waitForHeader() {
            console.log('waitForHeader: checking for header...');
            if (document.querySelector('.header')) {
                console.log('Header found, initializing graph view');
                initGraphView();
            } else {
                console.log('Header not found, retrying in 100ms');
                setTimeout(waitForHeader, 100);
            }
        }

        // ============ INITIALIZATION ============

        setupResizableDebugPanel();
        setupStatusStream();
        setupLogsStream();
        setupFilterListeners();
        // Debug window
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initDebugWindowSystem);
        } else {
            initDebugWindowSystem();
        }
        // Graphs
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => {
                console.log('DOMContentLoaded fired');
                waitForHeader();
            });
        } else {
            console.log('DOM already loaded, starting immediately');
            waitForHeader();
        }

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
    parser.add_argument("--db-path", type=Path, default=None,
                        help="Path to SQLite database for metrics graphs (e.g., /mnt/ssd/logs.db)."
                        " If not provided, graph features will be disabled.")
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
        http_port=args.http_port,
        db_path=args.db_path
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