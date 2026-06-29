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
import uuid
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
print("Importing aiohttp: may take some time...", flush=True)
from aiohttp import web
from LibLogger import LibLogger

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
    'heap':    {'type': 'simple', 'importance_threshold': 10000, 'order': 17, 'display_format': 'decimal'},
    'alloc':   {'type': 'alloc', 'importance_condition': 'has_large_allocation', 'order': 18, 'display_format': 'decimal'}
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
    'heap': 'The minimum free heap memory in bytes',
    'alloc': 'The top three largest heap allocations by file/line, with bytes allocated'
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

class SearchIterator:
    """Stateful search iterator with SQLite (fast) and journal (fallback) support"""

    def __init__(self, params: dict, start_timestamp: float, db_path: Path = None):
        self.params = params
        self.current_timestamp = start_timestamp
        self.current_rowid = params.get('from_rowid', None)  # New: for two-step search
        self.direction = params['direction']
        self.wrapped = False
        self.search_string = params['search']
        self.case_sensitive = params['case_sensitive']
        self.whole_word = params['whole_word']
        self.exclude_ctrl = params['exclude_ctrl']
        self.include_nodes = set(params['include_nodes']) if params['include_nodes'] else None
        self.min_log_level = params['min_log_level']
        self.controller_unit = None
        self.cancelled = False
        self.entries_checked = 0
        self.debug = params.get('debug', False)
        self.db_path = db_path
        self.using_sqlite = False
        self.starting_timestamp = start_timestamp
        self.has_wrapped = False

        # Don't open journal immediately - we'll try SQLite first
        self.journal = None

    def _log_debug(self, msg: str):
        """Conditional debug logging to a separate file
           (can't printf from a thread-pool and don't want
           to write to the journal as it may be being searched)"""
        if self.debug:
            try:
                with open('/tmp/sqlite_search_debug.log', 'a') as f:
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                    f.write(f"[{timestamp}] {msg}\n")
                    f.flush()
            except Exception as e:
                # Last resort - try stderr but don't crash
                print(f"Debug logging failed: {e}", file=sys.stderr, flush=True)

    def cancel(self):
        """Cancel ongoing or future searches"""
        self.cancelled = True
        self._log_debug("Search cancelled")

    def _search_sqlite(self, max_entries: int) -> Optional[Tuple[float, str, bool, int, int, int]]:
        """Search SQLite using two-step FTS + rowid fetch pattern (fast)"""
        if not self.db_path or not self.db_path.exists():
            return None

        import time
        start_total = time.time()

        self._log_debug(f"=== Starting SQLite search (two-step) ===")
        self._log_debug(f"Search string: {repr(self.search_string)}")
        self._log_debug(f"Case sensitive: {self.case_sensitive}")
        self._log_debug(f"Whole word: {self.whole_word}")
        self._log_debug(f"Current rowid: {self.current_rowid}")
        self._log_debug(f"Direction: {self.direction}")
        self._log_debug(f"Max entries: {max_entries}")

        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Build FTS query
            if self.whole_word:
                fts_query = f'"{self.search_string.replace('"', '""')}"'
            else:
                fts_query = self.search_string

            # DEBUG: Time the COUNT
            t0 = time.time()
            cursor.execute("SELECT COUNT(*) FROM logs_fts WHERE logs_fts MATCH ?", (fts_query,))
            total_matches = cursor.fetchone()[0]
            t1 = time.time()
            print(f"SEARCH TIMING: COUNT took {t1-t0:.3f}s, total_matches={total_matches}", flush=True)

            if total_matches == 0:
                return None

            # Step 1: Get rowids from FTS (fast)
            if self.direction == 'next':
                if self.current_rowid is not None:
                    rowid_condition = f"AND rowid > {self.current_rowid}"
                else:
                    rowid_condition = ""
                order = 'ASC'
            else:  # prev
                if self.current_rowid is not None:
                    rowid_condition = f"AND rowid < {self.current_rowid}"
                else:
                    rowid_condition = ""
                order = 'DESC'

            t0 = time.time()
            query = f"""
                SELECT rowid FROM logs_fts
                WHERE logs_fts MATCH ?
                {rowid_condition}
                ORDER BY rowid {order}
                LIMIT ?
            """
            cursor.execute(query, (fts_query, max_entries))
            rowids = [row[0] for row in cursor.fetchall()]
            t1 = time.time()
            print(f"SEARCH TIMING: FTS rowid fetch took {t1-t0:.3f}s, got {len(rowids)} rowids", flush=True)
            self._log_debug(f"FTS returned {len(rowids)} rowids")

            # Handle wrap if no rows found
            if not rowids:
                self._log_debug("No rows found, attempting wrap")
                if self.direction == 'next':
                    cursor.execute("SELECT MIN(rowid) FROM logs")
                else:
                    cursor.execute("SELECT MAX(rowid) FROM logs")
                wrap_rowid = cursor.fetchone()[0]

                if wrap_rowid is not None:
                    t0 = time.time()
                    if self.direction == 'next':
                        cursor.execute("""
                            SELECT rowid FROM logs_fts
                            WHERE logs_fts MATCH ?
                            AND rowid >= ?
                            ORDER BY rowid ASC
                            LIMIT ?
                        """, (fts_query, wrap_rowid, max_entries))
                    else:
                        cursor.execute("""
                            SELECT rowid FROM logs_fts
                            WHERE logs_fts MATCH ?
                            AND rowid <= ?
                            ORDER BY rowid DESC
                            LIMIT ?
                        """, (fts_query, wrap_rowid, max_entries))
                    rowids = [row[0] for row in cursor.fetchall()]
                    t1 = time.time()
                    print(f"SEARCH TIMING: FTS wrap fetch took {t1-t0:.3f}s, got {len(rowids)} rowids", flush=True)
                    self.has_wrapped = True
                    self._log_debug(f"Wrap returned {len(rowids)} rowids")
                else:
                    self._log_debug("No logs in database")
                    return None

            if not rowids:
                return None

            # Step 2: Get full log data for these rowids
            t0 = time.time()
            placeholders = ','.join(['?' for _ in rowids])
            cursor.execute(f"""
                SELECT log_id, epoch_time, message, node_ip, log_level, rowid
                FROM logs
                WHERE rowid IN ({placeholders})
                ORDER BY rowid {'ASC' if self.direction == 'next' else 'DESC'}
            """, rowids)

            rows = cursor.fetchall()
            t1 = time.time()
            print(f"SEARCH TIMING: log data fetch took {t1-t0:.3f}s, got {len(rows)} rows", flush=True)
            self._log_debug(f"Retrieved {len(rows)} full log rows")

            if not rows:
                return None

            # Apply post-filtering for case sensitivity
            if self.case_sensitive and self.search_string:
                original_count = len(rows)
                rows = [row for row in rows if self.search_string in row['message']]
                self._log_debug(f"Case-sensitive filter: {original_count} -> {len(rows)} rows")

            # Apply whole word post-filtering
            if self.whole_word and rows:
                import re
                pattern = rf'\b{re.escape(self.search_string)}\b'
                flags = 0 if self.case_sensitive else re.IGNORECASE
                original_count = len(rows)
                rows = [row for row in rows if re.search(pattern, row['message'], flags)]
                self._log_debug(f"Whole word filter: {original_count} -> {len(rows)} rows")

            if not rows:
                self._log_debug("No matches after filtering")
                total_time = time.time() - start_total
                print(f"SEARCH TIMING: TOTAL search (no match) took {total_time:.3f}s", flush=True)
                return (None, None, self.has_wrapped, len(rowids), self.entries_checked, 0)

            scanned = 0
            last_timestamp = None
            last_log_id = 0

            for row in rows:
                scanned += 1
                log_id = row['log_id']
                timestamp = row['epoch_time']
                message = row['message']
                node_ip = row['node_ip']
                rowid = row['rowid']
                last_timestamp = timestamp
                last_log_id = log_id

                # Apply exclude CTRL filter
                if self.exclude_ctrl and message.startswith('[CTRL]'):
                    continue

                # Apply node filter
                if self.include_nodes:
                    last_octet = node_ip.split('.')[-1] if node_ip else None
                    if not last_octet or last_octet not in self.include_nodes:
                        continue

                # Apply log level filter
                if self.min_log_level < 4:
                    match = re.search(r'\[(?:NODE|CTRL)\].*?\[[\d.]+\]\s*([DIWE])\s', message)
                    if match:
                        level = {'D': 0, 'I': 1, 'W': 2, 'E': 3}.get(match.group(1), 4)
                        if level < self.min_log_level:
                            continue

                # Match found!
                self.current_rowid = rowid
                self.entries_checked += scanned
                total_time = time.time() - start_total
                print(f"SEARCH TIMING: TOTAL search (match found) took {total_time:.3f}s", flush=True)
                result_tuple = (timestamp, message, self.has_wrapped, scanned, self.entries_checked, log_id)
                self._log_debug(f"Returning match: log_id={log_id}, rowid={rowid}, timestamp={timestamp}")
                return result_tuple

            # No match in this chunk after all filters
            if rows:
                self.current_rowid = rows[-1]['rowid']
                self.entries_checked += scanned
                self._log_debug(f"No match in this chunk, updated rowid to {self.current_rowid}")
                total_time = time.time() - start_total
                print(f"SEARCH TIMING: TOTAL search (no match in chunk) took {total_time:.3f}s", flush=True)
                return (None, None, self.has_wrapped, scanned, self.entries_checked, last_log_id)
            else:
                total_time = time.time() - start_total
                print(f"SEARCH TIMING: TOTAL search (no rows) took {total_time:.3f}s", flush=True)
                return None

        except Exception as e:
            self._log_debug(f"SQLite search error: {e}")
            self._log_debug(traceback.format_exc())
            return None
        finally:
            if conn:
                conn.close()

    def _init_journal(self):
        """Lazy initialization of journal reader"""
        if self.journal is None:
            self.journal = journal.Reader(path='/var/log/journal')
            self.journal.add_match(SYSLOG_IDENTIFIER=JOURNAL_IDENTIFIER)
            self._seek_to_position_journal()

    def _seek_to_position_journal(self):
        """Seek journal to start timestamp"""
        dt = datetime.fromtimestamp(self.current_timestamp, tz=timezone.utc)
        self._log_debug(f"Journal seeking to timestamp {self.current_timestamp} ({dt})")
        self.journal.seek_realtime(dt)
        if self.direction == 'next':
            entry = self.journal.get_next()
            self._log_debug(f"Moved to next entry, exists: {entry is not None}")
        else:
            entry = self.journal.get_previous()
            self._log_debug(f"Moved to previous entry, exists: {entry is not None}")

    def _extract_ip_from_log(self, message: str) -> Optional[str]:
        """Extract node IP from raw journal log line"""
        if not isinstance(message, str):
            message = str(message)
        match = re.search(r'\[([0-9.]+)\]', message)
        if match:
            return match.group(1)
        return None

    def _extract_log_level(self, message: str) -> Optional[int]:
        """Extract log level (D/I/W/E) from raw journal log line"""
        if not isinstance(message, str):
            message = str(message)
        match = re.search(r'\[\d+\.\d+\.\d+\.\d+\]\s+([DIWE])\s', message)
        if match:
            level_char = match.group(1)
            return {'D': 0, 'I': 1, 'W': 2, 'E': 3}.get(level_char)
        return None

    def _should_include_journal(self, message: str, unit: str) -> bool:
        """Apply same filters as debug view to journal entries"""
        is_ctrl = unit == self.controller_unit

        if self.exclude_ctrl and is_ctrl:
            return False

        if self.include_nodes and not is_ctrl:
            node_ip = self._extract_ip_from_log(message)
            if node_ip:
                last_octet = node_ip.split('.')[-1]
                if last_octet not in self.include_nodes:
                    return False

        if self.min_log_level < 4 and not is_ctrl:
            level = self._extract_log_level(message)
            if level is not None and level < self.min_log_level:
                return False

        return True

    def _matches_search(self, message: str) -> bool:
        """Check if message matches search string"""
        if not self.search_string:
            return False

        if not isinstance(message, str):
            message = str(message)

        search = self.search_string
        target = message

        if not self.case_sensitive:
            search = search.lower()
            target = target.lower()

        if self.whole_word:
            import re
            pattern = rf'\b{re.escape(search)}\b'
            return re.search(pattern, target) is not None
        else:
            return search in target

    def _get_next_journal_entry(self):
        """Get next journal entry, handling direction and wrap"""
        if self.direction == 'next':
            entry = self.journal.get_next()
        else:
            entry = self.journal.get_previous()

        if not entry:
            self.wrapped = True
            self._log_debug(f"Wrapping at entry {self.entries_checked}")
            if self.direction == 'next':
                self.journal.seek_head()
                entry = self.journal.get_next()
            else:
                self.journal.seek_tail()
                entry = self.journal.get_previous()
            self._log_debug(f"After wrap, entry exists: {entry is not None}")

        return entry

    def _search_journal(self, max_entries: int) -> Optional[Tuple[float, str, bool, int, int, int]]:
        """Fall back to journal search (slower)"""
        self._init_journal()

        scanned_this_call = 0
        self._log_debug(f"Journal search starting, max_entries={max_entries}, direction={self.direction}")

        while not self.cancelled and scanned_this_call < max_entries:
            if self.cancelled:
                return None

            entry = self._get_next_journal_entry()
            if not entry:
                return None

            if self.cancelled:
                return None

            scanned_this_call += 1
            self.entries_checked += 1

            message_raw = entry.get('MESSAGE')
            unit = entry.get('_SYSTEMD_UNIT', '')
            ts = entry.get('__REALTIME_TIMESTAMP')
            timestamp = ts.timestamp() if ts else None

            if self.cancelled:
                return None

            if isinstance(message_raw, list):
                message = ' '.join(str(m) for m in message_raw)
            elif message_raw is None:
                message = ''
            elif not isinstance(message_raw, str):
                message = str(message_raw)
            else:
                message = message_raw

            if not message:
                continue

            if not self._should_include_journal(message, unit):
                continue

            if self._matches_search(message):
                if timestamp:
                    self.current_timestamp = timestamp
                    self._log_debug(f"Journal MATCH FOUND at entry {self.entries_checked}")
                    return (timestamp, message, self.wrapped, scanned_this_call, self.entries_checked, 0)

        return (None, None, self.wrapped, scanned_this_call, self.entries_checked, 0)

    def find_next_match(self, max_entries: int = 1000) -> Optional[Tuple[float, str, bool, int, int, int]]:
        """
        Find next match, scanning at most max_entries.
        Returns (timestamp, message, wrapped, scanned_this_call, total_scanned) or None
        """
        # Only use SQLite if db_path is provided
        if self.db_path and self.db_path.exists():
            result = self._search_sqlite(max_entries)

            if result is not None:
                return result
            # SQLite exhausted (no more rows) - search complete
            return None
        else:
            # Fall back to journal oif no database
            return self._search_journal(max_entries)

    def close(self):
        """Close journal reader if open"""
        if self.journal is not None:
            self.journal.close()

class WebController(Controller):
    """Web-enabled FGR Controller with journal log reading"""

    def __init__(self, listen_ip: str = CONTROLLER_IP_DEFAULT,
                 port: int = CONTROLLER_PORT_DEFAULT,
                 nodes_dir: str = None, cfg_file: str = None,
                 http_port: int = HTTP_PORT_DEFAULT,
                 log_server_port: int = 0, log_server_host: str = "127.0.0.1",
                 db_path: Path = None):
        super().__init__(listen_ip, port, nodes_dir, cfg_file, log_server_port, log_server_host)

        self.http_port = http_port

        # Determine expected systemd unit for this service
        self.script_name = Path(sys.argv[0]).stem  # Gets "web_controller" from "web_controller.py"
        self.controller_unit = f"{self.script_name}.service"

        # Log storage for web interface - store as list with version tracking
        self.log_entries: List[Tuple[int, str, float, str, int]] = []  # (version, message, timestamp, source, log_id)
        self.max_log_entries = MAX_LOG_ENTRIES
        self._log_counter = 0

        self.db_path = db_path
        self.graphs_enabled = db_path is not None and db_path.exists()
        if not self.graphs_enabled:
            self._log_admin(f"Graphs disabled - database not found at {db_path}")
        else:
            self._log_admin(f"Graphs enabled using database: {db_path}")

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

        self.search_sessions: Dict[str, Dict] = {}  # session_id -> {iterator, last_access}
        self._start_session_cleanup()

        # Node grid layout
        self.node_grid_layout = self._load_node_grid_layout()

        # Start journal reader if available
        if HAS_SYSTEMD:
            self._start_journal_reader()
        else:
            self._log_admin("Journal reading disabled - node logs will not appear")

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
        conn = self._get_metrics_db_connection()
        if not conn:
            return

        try:
            cursor = conn.cursor()

            # Create table with alloc column if it doesn't exist
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
                    alloc TEXT,
                    PRIMARY KEY (epoch_time, node_ip)
                )
            """)

            # Create indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_epoch ON metrics_history(epoch_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_node_ip ON metrics_history(node_ip)")

            # Create trigger
            cursor.execute("DROP TRIGGER IF EXISTS update_metrics_history")
            cursor.execute("""
                CREATE TRIGGER update_metrics_history
                AFTER INSERT ON logs
                WHEN NEW.message_type = 'METRIC'
                BEGIN
                    INSERT OR REPLACE INTO metrics_history (
                        epoch_time, node_ip, rssi, heap,
                        wifi_failures, panics, ctrl_disconnects, log_disconnects, alloc
                    ) VALUES (
                        NEW.epoch_time,
                        NEW.node_ip,
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.dbm'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.heap'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.w.-.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.panic.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.cnt_c.-.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.log_c.-.n'),
                        json_extract(substr(NEW.message, instr(NEW.message, '{')), '$.alloc')
                    );
                END
            """)

            conn.commit()

        except Exception as e:
            print(f"Error initializing metrics_history: {e}")
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
                self._log_admin(f"Trimmed {cursor.rowcount} rows from metrics_history older than {days} days")
        except Exception as e:
            self._log_admin(f"Error trimming metrics_history: {e}")
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
        self._log_admin("Metrics trimmer started (runs daily)")

    def _load_node_grid_layout(self) -> Dict[str, Any]:
        """Load node grid layout from config file"""
        if NODE_GRID_CONFIG.exists():
            try:
                with open(NODE_GRID_CONFIG, 'r') as f:
                    return json.load(f)
            except Exception as e:
                self._log_admin(f"Error loading node grid layout: {e}")
        return {'order': [], 'pages': {}, 'columns': 4, 'rows': 2}

    def _save_node_grid_layout(self):
        """Save node grid layout to config file"""
        try:
            with open(NODE_GRID_CONFIG, 'w') as f:
                json.dump(self.node_grid_layout, f, indent=2)
        except Exception as e:
            self._log_admin(f"Error saving node grid layout: {e}")

    def _format_log_for_display(self, timestamp_str: str, source: str, node_ip: str,
                                log_level: int, message: str) -> str:
        """
        Format a log message for display.
        THIS IS THE ONLY PLACE THAT SHOULD FORMAT LOGS FOR DISPLAY.
        """
        if source == 'CTRL':
            return f"[{timestamp_str}] [CTRL] {message}"
        elif source == 'ADMIN':
            return f"[{timestamp_str}] [ADMIN] {message}"
        elif source == 'NODE':
            ip_part = f" [{node_ip}]" if node_ip else ""
            # Message already contains the level letter (D/I/W/E) from LibLogger
            return f"[{timestamp_str}] [NODE]{ip_part} {message}"
        else:
            # Fallback for unknown source
            return f"[{timestamp_str}] {message}"

    def _add_log_raw(self, source: str, node_ip: str, log_level: int,
                     message: str, journal_ts: float = None, log_id: int = 0):
        # Generate timestamp string
        if journal_ts:
            dt = datetime.fromtimestamp(journal_ts)
            timestamp_str = dt.strftime('%d/%m %H:%M:%S')
        else:
            timestamp_str = datetime.now().strftime('%d/%m %H:%M:%S')
            journal_ts = time.time()  # Use current time if no journal timestamp

        # FORMAT HERE - single source of truth
        formatted_message = self._format_log_for_display(
            timestamp_str, source, node_ip, log_level, message
        )

        # Apply linkification (URL detection)
        linkified_message = linkify_log_line(formatted_message)

        # Store with components preserved for filtering
        version = self._log_counter
        self._log_counter += 1
        self.log_entries.append((version, linkified_message, journal_ts, source, log_id))

        # Trim
        while len(self.log_entries) > self.max_log_entries:
            self.log_entries.pop(0)

    def _log_message(self, message: str):
        """Add a message to the journal via shared logger"""
        self.lib_logger.log(
            source='CTRL',
            node_ip='0.0.0.0',
            message=message,
            log_level=6,  # INFO
            message_type='CONTROL'
        )

    def _log_admin(self, message: str, log_level: int = 6):
        """
        Admin-only log - not shown in debug view, only journal.
        For ephemeral messages that shouldn't clutter the UI.
        """
        self.lib_logger.log_admin(message, log_level)

    def _start_journal_reader(self):
        """Start background thread to read logs from journal"""
        self.journal_running = True
        self.journal_thread = threading.Thread(target=self._journal_reader_loop, daemon=True)
        self.journal_thread.start()
        self._log_admin(f"Journal reader started, monitoring '{JOURNAL_IDENTIFIER}'")

    def _stop_journal_reader(self):
        """Stop the journal reader thread"""
        self._log_admin("Stopping journal reader...")
        self.journal_running = False

        # Give the thread time to notice the flag and exit
        if self.journal_thread and self.journal_thread.is_alive():
            self._log_admin("Waiting for journal reader thread to exit...")
            self.journal_thread.join(timeout=1.0)
            if self.journal_thread.is_alive():
                self._log_admin("Journal reader thread still alive (daemon will kill it)")
            else:
                self._log_admin("Journal reader thread exited cleanly")

    def _journal_reader_loop(self):
        """Background thread to read logs from systemd journal"""
        try:
            # Open journal reader
            j = journal.Reader(path='/var/log/journal')
            j.add_match(SYSLOG_IDENTIFIER=JOURNAL_IDENTIFIER)

            # Helper function to process a journal entry
            def process_entry(entry):
                process_start = time.time()

                # Materialize the entry once
                entry_dict = dict(entry)

                message = entry_dict.get('MESSAGE', '')
                if not message:
                    return

                message = message.rstrip()

                journal_ts = entry_dict.get('__REALTIME_TIMESTAMP')
                journal_ts_value = journal_ts.timestamp() if journal_ts else None

                if journal_ts_value:
                    age = time.time() - journal_ts_value
                    if age > 10:
                        if not hasattr(process_entry, '_last_lag_report'):
                            process_entry._last_lag_report = 0

                        now = time.time()
                        # Only report once every 60 seconds to avoid flooding
                        # the journal and hence affecting the measurement
                        if now - process_entry._last_lag_report > 60 or age > 300:
                            self._log_admin(f"Journal reader lag: {age:.1f}s behind (log_id={entry_dict.get('FGR_LOG_ID', '?')})")
                            process_entry._last_lag_report = now

                source = entry_dict.get('FGR_SOURCE', '')
                node_ip = entry_dict.get('FGR_NODE_IP', '')
                log_level = int(entry_dict.get('FGR_LOG_LEVEL', 0))
                log_id = int(entry_dict.get('FGR_LOG_ID', 0))

                t0 = time.time()
                if source == 'NODE' and node_ip:
                    metrics_result = self._parse_metrics_from_log(message, node_ip)
                    if metrics_result:
                        node_ip, metrics_data = metrics_result
                        self._update_node_metrics(node_ip, metrics_data)
                parse_metrics_time = time.time() - t0

                t0 = time.time()
                self._add_log_raw(source, node_ip, log_level, message, journal_ts_value, log_id)
                add_log_time = time.time() - t0

                total_time = time.time() - process_start
                if total_time > 0.05:
                    self._log_admin(f"SLOW process_entry: {total_time:.3f}s (parse_metrics={parse_metrics_time:.3f}s, add_log={add_log_time:.3f}s) for log_id={log_id}")

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

            for entry in j:
                if not self.journal_running:
                    break
                process_entry(entry)

            # Now follow new entries
            if last_cursor:
                j.seek_cursor(last_cursor)
                j.get_next(1)

            while self.journal_running:
                ret = j.wait(100000)

                if not self.journal_running:
                    break

                if ret == journal.APPEND:
                    for entry in j:
                        if not self.journal_running:
                            break
                        process_entry(entry)

        except Exception as e:
            self._log_admin(f"Journal reader error: {e}")
        finally:
            self._log_admin("Journal reader stopped")

    def _get_node_name_by_ip(self, ip: str) -> Optional[str]:
        """Get node name from IP address"""
        for name, node in self.nodes.items():
            if node.ip == ip:
                return name
        self._log_admin(f"Could not find node name for IP: {ip}")
        self._log_admin(f"Available node IPs: {[node.ip for node in self.nodes.values()]}")
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

    def _parse_metrics_from_log(self, log_line: str, node_ip: str = None) -> Optional[Tuple[str, dict]]:
        """Extract metrics JSON from log line and return (node_ip, metrics_dict)"""
        # Look for "metrics:" pattern
        if 'metrics:' not in log_line:
            return None

        # Use provided node_ip if available
        if not node_ip:
            # Extract node IP from [NODE] [IP] format
            node_match = re.search(r'\[NODE\]\s+\[([0-9.]+)\]', log_line)
            if not node_match:
                return None
            node_ip = node_match.group(1)

        # Find the start of the JSON
        metrics_pos = log_line.find('metrics:')
        if metrics_pos == -1:
            return None

        # Get everything after 'metrics:'
        after_metrics = log_line[metrics_pos + 8:]

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
        except json.JSONDecodeError:
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

        elif condition == 'has_large_allocation':  # NEW
            # Alloc: highlight if any allocation > 10KB
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        for bytes_allocated in item.values():
                            if bytes_allocated > 10240:  # 10KB threshold
                                return True
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

    def _format_alloc_metric(self, key: str, data: list, is_important: bool) -> Optional[str]:
        """Format alloc metric (array of file:line -> bytes objects)"""
        if not isinstance(data, list) or len(data) == 0:
            return None

        alloc_parts = []
        for item in data:
            if isinstance(item, dict):
                for location, bytes_allocated in item.items():
                    # Format bytes with K/M suffix for readability
                    if bytes_allocated >= 1024 * 1024:
                        formatted_bytes = f"{bytes_allocated // (1024 * 1024)}MB"
                    elif bytes_allocated >= 1024:
                        formatted_bytes = f"{bytes_allocated // 1024}KB"
                    else:
                        formatted_bytes = f"{bytes_allocated}B"
                    alloc_parts.append(f"{location}: {formatted_bytes}")

        if not alloc_parts:
            return None

        help_text = METRICS_HELP.get(key, '')
        display = ' '.join(alloc_parts)

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
            elif config['type'] == 'alloc':
                if isinstance(metric_data, list):
                    formatted = self._format_alloc_metric(key, metric_data, is_important)

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
                self._log_admin(f"Error getting card HTML for {node_name}: {e}")

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

    async def _run_search(self, session_id: str):
        """Background task to run the search"""
        session = self.search_sessions.get(session_id)
        if not session:
            return

        iterator = session['iterator']

        try:
            # Run find_next_match in a thread pool to avoid blocking
            result = await asyncio.get_event_loop().run_in_executor(
                None, iterator.find_next_match
            )

            if session.get('cancelled'):
                # Search was cancelled
                pass
            elif result:
                session['result'] = result
            else:
                session['result'] = None  # No more matches

            session['running'] = False

        except Exception as e:
            self._log_admin(f"Search error for session {session_id}: {e}")
            session['running'] = False
        finally:
            # Don't delete session immediately - let cleanup thread handle it
            pass

    async def _run_search_in_thread(self, session_id: str):
        """Run the search in a thread pool - creates iterator in the same thread"""
        session = self.search_sessions.get(session_id)
        if not session:
            return

        try:
            params = session['params']
            start_timestamp = session['start_timestamp']
            max_entries_per_poll = 1000  # Scan this many entries per poll

            def search_in_thread():
                """This runs entirely in the thread pool"""
                # Create iterator INSIDE the thread pool
                iterator = SearchIterator(params, start_timestamp, db_path=self.db_path)
                iterator.controller_unit = self.controller_unit
                iterator.debug = params.get('debug', False)

                # Store iterator in session for potential cancellation
                session['iterator'] = iterator

                total_scanned = 0

                while not session.get('cancelled'):
                    # Search for next match (bounded)
                    result = iterator.find_next_match(max_entries_per_poll)

                    if result is None:
                        # End of journal/database
                        session['finished'] = True
                        session['running'] = False
                        return

                    timestamp, message, wrapped, scanned_this, total_scanned, log_id = result

                    if timestamp is not None:
                        # Match found
                        session['result'] = (timestamp, message, wrapped, scanned_this, total_scanned, log_id)
                        session['running'] = False
                        return
                    else:
                        # No match in this chunk, update progress and continue
                        session['total_scanned'] = total_scanned
                        # Small sleep to allow polling loop to catch up
                        time.sleep(0.05)

                # Cancelled
                if params.get('debug', False):
                    print(f"SEARCH DEBUG: Session {session_id[:8]}... CANCELLED after {total_scanned} entries", flush=True)
                session['running'] = False

            # Run the entire search in a single thread pool thread
            await asyncio.get_event_loop().run_in_executor(
                None, search_in_thread
            )

        except Exception as e:
            self._log_admin(f"Search error for session {session_id}: {e}")
            session['error'] = str(e)
            session['running'] = False

    def _start_session_cleanup(self):
        """Start background thread to clean up expired search sessions"""
        def cleanup_loop():
            while self.web_running:
                time.sleep(60)  # Check every minute
                now = time.time()
                expired = []
                for sid, session in self.search_sessions.items():
                    if now - session['last_access'] > 300:  # 5 minutes
                        expired.append(sid)
                for sid in expired:
                    if sid in self.search_sessions:
                        self.search_sessions[sid]['iterator'].close()
                        del self.search_sessions[sid]
                        self._log_admin(f"Cleaned up expired search session {sid}")

        cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
        cleanup_thread.start()

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
            self._log_admin(f"Status stream error: {e}")
        finally:
            self.sse_clients.discard(response)

        return response

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
            return response

        try:
            while self.web_running:
                if self.log_entries and self.log_entries[-1][0] > last_version:
                    new_logs = []
                    for entry in self.log_entries:
                        version = entry[0]
                        if version > last_version:
                            message = entry[1]
                            timestamp = entry[2]
                            source = entry[3]
                            log_id = entry[4]

                            # Skip ADMIN logs - they shouldn't appear in debug window
                            if source != 'ADMIN':
                                new_logs.append({
                                    "message": message,
                                    "timestamp": timestamp,
                                    "source": source,
                                    "log_id": log_id
                                })
                            last_version = version
                    if new_logs:
                        try:
                            await response.write(f"data: {json.dumps(new_logs)}\n\n".encode())
                        except (ConnectionResetError, BrokenPipeError, RuntimeError):
                            break
                await asyncio.sleep(0.5)
        except (ConnectionResetError, BrokenPipeError, RuntimeError):
            pass
        except Exception as e:
            self._log_admin(f"Log stream error: {e}")

        return response

    async def handle_api_journal_time_range(self, request):
        """Get the earliest and latest timestamps available in the journal (UTC)"""

        if not HAS_SYSTEMD:
            return web.json_response({'error': 'Journal not available'}, status=503)

        try:
            def _get_range():
                j = journal.Reader(path='/var/log/journal')
                j.add_match(SYSLOG_IDENTIFIER=JOURNAL_IDENTIFIER)

                # Get earliest timestamp - just the first entry
                j.seek_head()
                earliest = None
                entry = j.get_next()
                if entry:
                    earliest_ts = entry.get('__REALTIME_TIMESTAMP')
                    if earliest_ts:
                        earliest = earliest_ts.timestamp()

                # Get latest timestamp - just the last entry
                j.seek_tail()
                latest = None
                entry = j.get_previous()
                if entry:
                    latest_ts = entry.get('__REALTIME_TIMESTAMP')
                    if latest_ts:
                        latest = latest_ts.timestamp()

                j.close()
                return earliest, latest

            earliest, latest = await asyncio.to_thread(_get_range)

            return web.json_response({
                'earliest': earliest if earliest else None,
                'latest': latest if latest else None
            })

        except Exception as e:
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
        since = data.get('since')
        until = data.get('until')

        thread_id = threading.get_ident()
        print(f"[DEBUG] JOURNAL QUERY START: thread={thread_id}, timestamp={timestamp}, before={before}, after={after}, since={since}, until={until}", flush=True)
        start_time = time.time()

        if timestamp is None and since is None:
            print(f"[DEBUG] JOURNAL QUERY END: thread={thread_id}, no timestamp/since, took={time.time()-start_time:.3f}s", flush=True)
            return web.json_response({'status': 'ok', 'logs': []})

        def _format_log_entry(entry):
            start = time.time()

            ts = entry.get('__REALTIME_TIMESTAMP')
            if ts:
                dt = datetime.fromtimestamp(ts.timestamp())
                timestamp_str = dt.strftime('%d/%m %H:%M:%S')
            else:
                timestamp_str = '00:00:00'

            # Get custom fields
            log_id = entry.get('FGR_LOG_ID', 0)
            try:
                log_id = int(log_id) if log_id else 0
            except ValueError:
                log_id = 0

            source = entry.get('FGR_SOURCE', '')
            node_ip = entry.get('FGR_NODE_IP', '')
            log_level = entry.get('FGR_LOG_LEVEL', None)
            if log_level is not None:
                try:
                    log_level = int(log_level)
                except (ValueError, TypeError):
                    log_level = None
            raw_message = entry.get('MESSAGE', '')

            # USE THE SAME FORMATTING FUNCTION
            formatted_message = self._format_log_for_display(
                timestamp_str, source, node_ip, log_level, raw_message
            )

            linkified_message = linkify_log_line(formatted_message)

            elapsed = time.time() - start
            if elapsed > 0.01:
                print(f"SLOW _format_log_entry: {elapsed:.3f}s for log_id={log_id}")

            return {
                'message': linkified_message,
                'timestamp': ts.timestamp() if ts else None,
                'log_id': log_id,
                'source': source,
                'node_ip': node_ip
            }

        def _query():
            total_start = time.time()
            print(f"[DEBUG] _query START: thread={threading.get_ident()}")

            j = journal.Reader(path='/var/log/journal')
            j.add_match(SYSLOG_IDENTIFIER='fgr-log-server')

            seek_start = time.time()

            # NEW: Use since/until time range if provided
            if since is not None and until is not None:
                since_dt = datetime.fromtimestamp(since, tz=timezone.utc)
                until_dt = datetime.fromtimestamp(until, tz=timezone.utc)
                j.seek_realtime(since_dt)
                print(f"  seek_realtime (since/until): {time.time() - seek_start:.3f}s")

                read_start = time.time()
                logs = []
                format_total = 0
                entry_count = 0
                for entry in j:
                    ts = entry.get('__REALTIME_TIMESTAMP')
                    if ts and ts.timestamp() > until:
                        break
                    entry_count += 1
                    format_start = time.time()
                    logs.append(_format_log_entry(entry))
                    format_total += time.time() - format_start
                print(f"  read/format: {time.time() - read_start:.3f}s ({entry_count} entries, format={format_total:.3f}s)")

                j.close()
                print(f"_query total: {time.time() - total_start:.3f}s")
                print(f"[DEBUG] _query END: thread={threading.get_ident()}, logs={len(logs)}")
                return logs, -1, False

            # Original behavior: query by timestamp with before/after counts
            target_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            j.seek_realtime(target_dt)
            print(f"  seek_realtime (timestamp): {time.time() - seek_start:.3f}s")

            center = j.get_next()
            center_format_start = time.time()
            center_formatted = _format_log_entry(center) if center else None
            center_format_time = time.time() - center_format_start
            print(f"  center format: {center_format_time:.3f}s")

            # Collect AFTER logs first (cursor moves forward)
            after_logs = []
            after_read_start = time.time()
            after_format_total = 0
            if after > 0 and center:
                entry = j.get_next()
                entry_count = 0
                while entry and len(after_logs) < after:
                    entry_count += 1
                    format_start = time.time()
                    after_logs.append(_format_log_entry(entry))
                    after_format_total += time.time() - format_start
                    entry = j.get_next()
                print(f"  after read/format: {time.time() - after_read_start:.3f}s ({entry_count} entries, format={after_format_total:.3f}s)")

            # Reposition to center for BEFORE logs
            j.seek_realtime(target_dt)
            j.add_match(SYSLOG_IDENTIFIER='fgr-log-server')
            j.get_next()
            entry = j.get_previous()

            before_logs = []
            before_read_start = time.time()
            before_format_total = 0
            if before > 0 and center:
                entry_count = 0
                while entry and len(before_logs) < before:
                    entry_count += 1
                    format_start = time.time()
                    before_logs.append(_format_log_entry(entry))
                    before_format_total += time.time() - format_start
                    entry = j.get_previous()
                before_logs.reverse()
                print(f"  before read/format: {time.time() - before_read_start:.3f}s ({entry_count} entries, format={before_format_total:.3f}s)")

            # Assemble
            logs = before_logs
            target_index = -1
            if before > 0 and after > 0 and center:
                target_index = len(logs)
                logs.append(center_formatted)
            logs.extend(after_logs)

            if target_index == -1:
                if before > 0 and after == 0:
                    target_index = len(logs)
                elif before == 0 and after > 0:
                    target_index = 0

            # Check if there might be more logs beyond what we fetched
            has_more = len(logs) >= (before if before > 0 else after)
            j.close()
            print(f"_query total: {time.time() - total_start:.3f}s")
            print(f"[DEBUG] _query END: thread={threading.get_ident()}, logs={len(logs)}, target_index={target_index}")
            return logs, target_index, has_more

        try:
            logs, target_index, has_more = await asyncio.to_thread(_query)
            elapsed = time.time() - start_time
            print(f"[DEBUG] JOURNAL QUERY END: thread={thread_id}, logs={len(logs)}, took={elapsed:.3f}s", flush=True)
            return web.json_response({
                'status': 'ok',
                'logs': logs,
                'target_index': target_index,
                'has_more': has_more
            })
        except Exception as e:
            elapsed = time.time() - start_time
            print(f"[DEBUG] JOURNAL QUERY ERROR: thread={thread_id}, error={e}, took={elapsed:.3f}s", flush=True)
            return web.json_response({'error': str(e)}, status=500)

    async def handle_api_search(self, request):
        """Handle search requests - using Log ID as primary cursor"""

        data = await request.json() if request.body_exists else {}
        action = data.get('action')

        # Search must have database
        if not self.db_path or not self.db_path.exists():
            return web.json_response({'error': 'Search requires database. Use --db-path to enable.'}, status=503)

        if action == 'start':
            params = {
                'search': data.get('search', ''),
                'direction': data.get('direction', 'next'),
                'case_sensitive': data.get('case_sensitive', False),
                'whole_word': data.get('whole_word', False),
                'exclude_ctrl': data.get('exclude_ctrl', False),
                'include_nodes': data.get('include_nodes', []),
                'min_log_level': data.get('min_log_level', 4),
            }
            from_rowid = data.get('from_rowid')
            fetch_limit = data.get('fetch_limit', 200)

            if not params['search']:
                return web.json_response({'error': 'No search term'}, status=400)

            def do_search():
                import time
                start_total = time.time()

                conn = None
                try:
                    conn = sqlite3.connect(str(self.db_path))
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    search_term = params['search']

                    # Build FTS query
                    if params['whole_word']:
                        fts_query = f'"{search_term.replace('"', '""')}"'
                    else:
                        fts_query = search_term

                    # Initialize wrapped variable
                    wrapped = False

                    # DEBUG: Check if any matches exist (fast with LIMIT 1)
                    t0 = time.time()
                    cursor.execute("SELECT 1 FROM logs_fts WHERE logs_fts MATCH ? LIMIT 1", (fts_query,))
                    has_matches = cursor.fetchone() is not None
                    t1 = time.time()
                    print(f"SEARCH TIMING: EXISTS check took {t1-t0:.3f}s, has_matches={has_matches}", flush=True)

                    if not has_matches:
                        return {'found': False, 'finished': True, 'total_matches': 0}

                    # Determine operator and order based on direction
                    if params['direction'] == 'next':
                        operator = '>'
                        order = 'ASC'
                    else:  # prev direction
                        operator = '<'
                        order = 'DESC'

                    # Single combined query - inline log_id to rowid conversion
                    from_log_id = data.get('from_log_id')
                    t0 = time.time()

                    if from_log_id is not None:
                        # Combined query: log_id lookup + FTS search in one go
                        query = f"""
                            SELECT rowid FROM logs_fts
                            WHERE logs_fts MATCH ?
                            AND rowid {operator} (SELECT rowid FROM logs WHERE log_id = ?)
                            ORDER BY rowid {order}
                            LIMIT ?
                        """
                        cursor.execute(query, (fts_query, from_log_id, fetch_limit))
                    else:
                        # No starting log_id - get from beginning/end
                        query = f"""
                            SELECT rowid FROM logs_fts
                            WHERE logs_fts MATCH ?
                            ORDER BY rowid {order}
                            LIMIT ?
                        """
                        cursor.execute(query, (fts_query, fetch_limit))

                    rowids = [row[0] for row in cursor.fetchall()]
                    t1 = time.time()
                    print(f"SEARCH TIMING: FTS rowid fetch took {t1-t0:.3f}s, got {len(rowids)} rowids", flush=True)

                    # Handle wrap if no rows found
                    if not rowids:
                        if params['direction'] == 'next':
                            cursor.execute("SELECT MIN(rowid) FROM logs")
                        else:
                            cursor.execute("SELECT MAX(rowid) FROM logs")
                        wrap_rowid = cursor.fetchone()[0]

                        if wrap_rowid is not None:
                            t0 = time.time()
                            if params['direction'] == 'next':
                                cursor.execute("""
                                    SELECT rowid FROM logs_fts
                                    WHERE logs_fts MATCH ?
                                    AND rowid >= ?
                                    ORDER BY rowid ASC
                                    LIMIT ?
                                """, (fts_query, wrap_rowid, fetch_limit))
                            else:
                                cursor.execute("""
                                    SELECT rowid FROM logs_fts
                                    WHERE logs_fts MATCH ?
                                    AND rowid <= ?
                                    ORDER BY rowid DESC
                                    LIMIT ?
                                """, (fts_query, wrap_rowid, fetch_limit))
                            rowids = [row[0] for row in cursor.fetchall()]
                            t1 = time.time()
                            print(f"SEARCH TIMING: FTS wrap fetch took {t1-t0:.3f}s, got {len(rowids)} rowids", flush=True)
                            wrapped = True
                        else:
                            return {'found': False, 'finished': True, 'total_matches': 0}

                    if not rowids:
                        return {'found': False, 'finished': True, 'total_matches': 0}

                    # Step 2: Get full log data for these rowids
                    t0 = time.time()
                    placeholders = ','.join(['?' for _ in rowids])
                    cursor.execute(f"""
                        SELECT log_id, epoch_time, message, node_ip, log_level, rowid
                        FROM logs
                        WHERE rowid IN ({placeholders})
                        ORDER BY rowid {'ASC' if params['direction'] == 'next' else 'DESC'}
                    """, rowids)

                    rows = cursor.fetchall()
                    t1 = time.time()
                    print(f"SEARCH TIMING: log data fetch took {t1-t0:.3f}s, got {len(rows)} rows", flush=True)

                    if not rows:
                        return {'found': False, 'finished': True, 'total_matches': 0}

                    # Apply filters
                    filter_params = {
                        'search_term': search_term,
                        'exclude_ctrl': params['exclude_ctrl'],
                        'include_nodes': params['include_nodes'],
                        'min_log_level': params['min_log_level'],
                        'case_sensitive': params['case_sensitive'],
                        'whole_word': params['whole_word']
                    }

                    filtered_matches = self._apply_search_filters(rows, filter_params)

                    if not filtered_matches:
                        return {'found': False, 'finished': True, 'total_matches': 0}

                    # Create session - store both rowid and log_id
                    session_id = str(uuid.uuid4())
                    self.search_sessions[session_id] = {
                        'last_access': time.time(),
                        'matches': filtered_matches,
                        'total_matches': -1,  # Unknown, but we don't need it for navigation
                        'current_index': 0,
                        'direction': params['direction'],
                        'search_term': search_term,
                        'fts_query': fts_query,
                        'fetch_limit': fetch_limit,
                        'last_fetched_rowid': rows[-1]['rowid'] if rows else None,
                        'last_fetched_log_id': rows[-1]['log_id'] if rows else None,
                        'has_more': len(filtered_matches) == fetch_limit,
                        'exclude_ctrl': params['exclude_ctrl'],
                        'include_nodes': params['include_nodes'],
                        'min_log_level': params['min_log_level'],
                        'case_sensitive': params['case_sensitive'],
                        'whole_word': params['whole_word']
                    }

                    # Return the first match
                    first_match = filtered_matches[0]
                    total_time = time.time() - start_total
                    print(f"SEARCH TIMING: TOTAL do_search took {total_time:.3f}s", flush=True)
                    return {
                        'session_id': session_id,
                        'found': True,
                        'log_id': first_match['log_id'],
                        'epoch_time': first_match['epoch_time'],
                        'message': first_match['message'],
                        'wrapped': wrapped,
                        'total_matches': -1,  # Unknown
                        'current_index': 0,
                        'has_more': self.search_sessions[session_id]['has_more']
                    }

                except Exception as e:
                    self._log_admin(f"Search error: {e}")
                    import traceback
                    traceback.print_exc()
                    return {'error': str(e)}
                finally:
                    if conn:
                        conn.close()

            result = await asyncio.get_event_loop().run_in_executor(None, do_search)

            if 'error' in result:
                return web.json_response({'error': result['error']}, status=500)

            return web.json_response(result)

        elif action == 'poll':
            session_id = data.get('session_id')
            direction = data.get('direction')

            if not session_id or session_id not in self.search_sessions:
                return web.json_response({'error': 'Invalid or expired session'}, status=404)

            session = self.search_sessions[session_id]
            session['last_access'] = time.time()

            # Use requested direction or session direction
            search_direction = direction if direction else session['direction']

            # Calculate next index
            if search_direction == 'next':
                session['current_index'] += 1
            else:
                session['current_index'] -= 1

            # Check if we need to fetch more matches
            wrapped = False

            if session['current_index'] >= len(session['matches']):
                # Need more matches in next direction
                if session.get('has_more', False) and session.get('last_fetched_log_id'):
                    # Fetch next batch
                    more_matches = self._fetch_more_matches(session, 'next')
                    if more_matches:
                        session['matches'].extend(more_matches)
                        session['last_fetched_log_id'] = more_matches[-1]['log_id'] if more_matches else None
                        session['has_more'] = len(more_matches) == session['fetch_limit']

                        if session['current_index'] < len(session['matches']):
                            match = session['matches'][session['current_index']]
                        else:
                            # Still out of range - wrap
                            session['current_index'] = 0
                            wrapped = True
                            match = session['matches'][session['current_index']]
                    else:
                        session['current_index'] = 0
                        wrapped = True
                        match = session['matches'][session['current_index']]
                else:
                    session['current_index'] = 0
                    wrapped = True
                    match = session['matches'][session['current_index']]
            elif session['current_index'] < 0:
                # Need more matches in prev direction
                if session.get('has_more', False) and session.get('last_fetched_log_id'):
                    # Fetch previous batch
                    more_matches = self._fetch_more_matches(session, 'prev')
                    if more_matches:
                        # Insert at beginning for prev direction
                        session['matches'] = more_matches + session['matches']
                        session['last_fetched_log_id'] = more_matches[0]['log_id'] if more_matches else None
                        session['has_more'] = len(more_matches) == session['fetch_limit']
                        # Adjust index (we added more_matches at the beginning)
                        session['current_index'] += len(more_matches)
                        match = session['matches'][session['current_index']]
                    else:
                        session['current_index'] = len(session['matches']) - 1
                        wrapped = True
                        match = session['matches'][session['current_index']]
                else:
                    session['current_index'] = len(session['matches']) - 1
                    wrapped = True
                    match = session['matches'][session['current_index']]
            else:
                match = session['matches'][session['current_index']]

            return web.json_response({
                'found': True,
                'log_id': match['log_id'],
                'epoch_time': match['epoch_time'],
                'message': match['message'],
                'wrapped': wrapped,
                'current_index': session['current_index'],
                'total_matches': session['total_matches']
            })

        elif action == 'cancel':
            session_id = data.get('session_id')
            if session_id and session_id in self.search_sessions:
                del self.search_sessions[session_id]
                self._log_admin(f"Search session {session_id} cancelled")
                return web.json_response({'status': 'cancelled'})
            return web.json_response({'error': 'Session not found'}, status=404)

        else:
            return web.json_response({'error': 'Invalid action'}, status=400)

    def _apply_search_filters(self, rows, params):
        """Apply all search filters to a list of database rows."""
        search_term = params['search_term']
        filtered_matches = []

        for row in rows:
            message = row['message']
            node_ip = row['node_ip']
            log_level = row['log_level']

            # Apply exclude CTRL filter
            if params.get('exclude_ctrl', False) and message.startswith('[CTRL]'):
                continue

            # Apply node filter
            include_nodes = params.get('include_nodes', [])
            if include_nodes:
                last_octet = node_ip.split('.')[-1] if node_ip else None
                if not last_octet or last_octet not in include_nodes:
                    continue

            # Apply log level filter
            min_log_level = params.get('min_log_level', 4)
            if min_log_level < 4:
                level_match = re.search(r'\[(?:NODE|CTRL)\].*?\[[\d.]+\]\s*([DIWE])\s', message)
                if level_match:
                    level = {'D': 0, 'I': 1, 'W': 2, 'E': 3}.get(level_match.group(1), 4)
                    if level < min_log_level:
                        continue

            # Apply case sensitivity
            case_sensitive = params.get('case_sensitive', False)
            if case_sensitive:
                if search_term not in message:
                    continue
            else:
                if search_term.lower() not in message.lower():
                    continue

            # Apply whole word
            whole_word = params.get('whole_word', False)
            if whole_word:
                pattern = rf'\b{re.escape(search_term)}\b'
                flags = 0 if case_sensitive else re.IGNORECASE
                if not re.search(pattern, message, flags):
                    continue

            filtered_matches.append({
                'log_id': row['log_id'],
                'epoch_time': row['epoch_time'],
                'message': message,
                'node_ip': node_ip,
                'rowid': row['rowid']  # Add rowid here
            })

        return filtered_matches

    def _fetch_more_matches(self, session, direction):
        """Fetch the next batch of matches for a session using rowid positioning (fast)"""
        conn = None
        try:
            conn = sqlite3.connect(str(self.db_path))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Step 1: Get rowids from FTS
            if direction == 'next':
                cursor.execute("""
                    SELECT rowid FROM logs_fts
                    WHERE logs_fts MATCH ?
                    AND rowid > ?
                    ORDER BY rowid ASC
                    LIMIT ?
                """, (session['fts_query'], session['last_fetched_rowid'], session['fetch_limit']))
            else:  # prev
                cursor.execute("""
                    SELECT rowid FROM logs_fts
                    WHERE logs_fts MATCH ?
                    AND rowid < ?
                    ORDER BY rowid DESC
                    LIMIT ?
                """, (session['fts_query'], session['last_fetched_rowid'], session['fetch_limit']))

            rowids = [row[0] for row in cursor.fetchall()]

            if not rowids:
                return []

            # Step 2: Get full log data for these rowids
            placeholders = ','.join(['?' for _ in rowids])
            cursor.execute(f"""
                SELECT log_id, epoch_time, message, node_ip, log_level, rowid
                FROM logs
                WHERE rowid IN ({placeholders})
                ORDER BY rowid {'ASC' if direction == 'next' else 'DESC'}
            """, rowids)

            rows = cursor.fetchall()

            if not rows:
                return []

            filter_params = {
                'search_term': session['search_term'],
                'exclude_ctrl': session.get('exclude_ctrl', False),
                'include_nodes': session.get('include_nodes', []),
                'min_log_level': session.get('min_log_level', 4),
                'case_sensitive': session.get('case_sensitive', False),
                'whole_word': session.get('whole_word', False)
            }

            filtered_matches = self._apply_search_filters(rows, filter_params)

            if direction == 'prev':
                filtered_matches.reverse()

            # Update the session's last_fetched_rowid and last_fetched_log_id
            if filtered_matches:
                session['last_fetched_rowid'] = filtered_matches[-1]['rowid']
                session['last_fetched_log_id'] = filtered_matches[-1]['log_id']

            return filtered_matches

        except Exception as e:
            self._log_admin(f"Fetch more matches error: {e}")
            import traceback
            traceback.print_exc()
            return []
        finally:
            if conn:
                conn.close()

    async def handle_api_logs_lookup(self, request):
        """Look up a log by log_id and return its database timestamp"""
        data = await request.json() if request.body_exists else {}
        log_id = data.get('log_id')

        if not log_id:
            return web.json_response({'error': 'Missing log_id'}, status=400)

        if not self.graphs_enabled:
            return web.json_response({'error': 'Database not available'}, status=503)

        conn = self._get_metrics_db_connection()
        if not conn:
            return web.json_response({'error': 'Database connection failed'}, status=503)

        try:
            cursor = conn.cursor()
            cursor.execute("SELECT epoch_time FROM logs WHERE log_id = ?", (log_id,))
            row = cursor.fetchone()

            if row:
                return web.json_response({
                    'status': 'ok',
                    'log_id': log_id,
                    'timestamp': row[0]
                })
            else:
                return web.json_response({
                    'status': 'not_found',
                    'log_id': log_id
                }, status=404)
        except Exception as e:
            return web.json_response({'error': str(e)}, status=500)
        finally:
            conn.close()

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
        data = await request.json() if request.body_exists else {}
        metric = data.get('metric')
        hour_timestamp = data.get('timestamp')  # milliseconds from frontend
        node_ips = data.get('nodes', [])
        graph_start_time = data.get('graph_start_time')  # seconds since epoch

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

            # Build node filter for hour bucket query
            node_filter = ""
            params = [hour_start, hour_end]
            if node_ips and len(node_ips) > 0 and node_ips[0] != 'all':
                placeholders = ','.join(['?' for _ in node_ips])
                node_filter = f"AND node_ip IN ({placeholders})"
                params.extend(node_ips)

            # CRITICAL FIX: Use COALESCE to treat NULL as 0 for counter metrics
            # For RSSI and heap, keep NULL as NULL (0 is a valid signal strength)
            if metric in ['wifi_failures', 'panics', 'ctrl_disconnects', 'log_disconnects']:
                # Counter metrics: treat NULL as 0
                query = f"""
                    SELECT epoch_time, node_ip, COALESCE({column}, 0) as {column}
                    FROM metrics_history
                    WHERE epoch_time BETWEEN ? AND ?
                    {node_filter}
                    ORDER BY node_ip, epoch_time ASC
                """
            else:
                # Non-counter metrics (RSSI, heap): keep NULL as NULL
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

            # Build result dictionary
            result = {}
            for row in rows:
                epoch_time, node_ip, value = row
                if node_ip not in result:
                    result[node_ip] = {
                        'ip': node_ip,
                        'name': self._get_node_name_by_ip(node_ip) or node_ip,
                        'data': []
                    }
                result[node_ip]['data'].append([epoch_time * 1000, value])

            # For each node with data in this hour, get the baseline
            # (last known value before the hour, bounded by graph_start_time if provided)
            baseline_info = {}
            for node_ip, node_data in result.items():
                # Build baseline query with COALESCE for counter metrics
                if metric in ['wifi_failures', 'panics', 'ctrl_disconnects', 'log_disconnects']:
                    baseline_query = f"""
                        SELECT epoch_time, COALESCE({column}, 0) as {column}
                        FROM metrics_history
                        WHERE node_ip = ?
                        AND epoch_time < ?
                    """
                else:
                    baseline_query = f"""
                        SELECT epoch_time, {column}
                        FROM metrics_history
                        WHERE node_ip = ?
                        AND {column} IS NOT NULL
                        AND epoch_time < ?
                    """

                baseline_params = [node_ip, hour_start]

                if graph_start_time:
                    baseline_query += " AND epoch_time >= ?"
                    baseline_params.append(graph_start_time)

                baseline_query += " ORDER BY epoch_time DESC LIMIT 1"

                cursor.execute(baseline_query, baseline_params)
                baseline_row = cursor.fetchone()

                if baseline_row:
                    baseline_epoch, baseline_value = baseline_row
                    baseline_info[node_ip] = {
                        'value': baseline_value,
                        'timestamp': baseline_epoch * 1000
                    }
                else:
                    baseline_info[node_ip] = None

            # Calculate delta_info using baseline as starting point
            # ONLY count INCREASES - decreases (resets) are ignored
            delta_info = {}
            for node_ip, node_data in result.items():
                baseline = baseline_info.get(node_ip)
                values = node_data['data']

                if not values:
                    delta_info[node_ip] = {'total_events': 0}
                    continue

                # Start with baseline if available
                if baseline and baseline['value'] is not None:
                    prev_value = baseline['value']
                else:
                    prev_value = None

                total_events = 0
                for point in values:
                    current_value = point[1]
                    if prev_value is not None:
                        # ONLY count INCREASES as new events
                        # Decreases (resets) are ignored - they don't represent new failures
                        if current_value > prev_value:
                            delta = current_value - prev_value
                            total_events += delta
                        # else: decrease or same value - no new events, ignore
                    else:
                        # First reading with no baseline - assume all counted failures happened
                        # since the start of the window (conservative approach)
                        if current_value > 0:
                            total_events += current_value

                    prev_value = current_value

                delta_info[node_ip] = {
                    'total_events': total_events,
                    'first_value': values[0][1] if values else None,
                    'last_value': values[-1][1] if values else None,
                    'baseline_value': baseline['value'] if baseline else None,
                    'baseline_timestamp': baseline['timestamp'] if baseline else None
                }

            return web.json_response({
                'metric': metric,
                'hour_start': hour_start * 1000,
                'hour_end': hour_end * 1000,
                'data': result,
                'delta_summary': delta_info
            })

        except Exception as e:
            self._log_admin(f"Error querying raw minute data: {e}")
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
                self._log_admin(f"Error getting expanded HTML from handler: {e}")
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
                            <button class="btn-apply-level" onclick="sendCommand('{node_name}', 'log_level', {{level: parseInt(this.previousElementSibling.value, 10)}})">Apply</button>
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
        Treats NULL as zero (metric absent from message means counter was zero).
        Only counts INCREASES as new events - decreases (resets) are ignored.
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

            # CRITICAL FIX: Use COALESCE to treat NULL as 0
            # This ensures that when a metric is absent from a message (meaning counter was zero),
            # we explicitly use 0 instead of NULL, allowing delta calculations to work correctly
            query = f"""
                SELECT
                    epoch_time,
                    node_ip,
                    COALESCE({metric_column}, 0) as {metric_column}
                FROM metrics_history
                WHERE epoch_time BETWEEN ? AND ?
                {node_filter}
                ORDER BY node_ip, epoch_time ASC
            """

            cursor.execute(query, params)
            rows = cursor.fetchall()

            if not rows:
                return {}

            # Calculate deltas per node - ONLY count increases
            node_last_values = {}
            delta_points = []  # (bucket_start, node_ip, delta)

            for row in rows:
                epoch_time, node_ip, cumulative_value = row

                last_value = node_last_values.get(node_ip)

                if last_value is not None:
                    # ONLY count INCREASES as new failures/events
                    # Decreases (resets) are ignored because they don't represent new events
                    if cumulative_value > last_value:
                        delta = cumulative_value - last_value
                        if delta > 0:
                            # Calculate which bucket this belongs to
                            bucket_start = (epoch_time // bucket_seconds) * bucket_seconds
                            delta_points.append((bucket_start, node_ip, delta))
                    # else: decrease or same value - no new events, ignore
                else:
                    # First reading for this node in the query window
                    # No baseline, so we cannot calculate a reliable delta.
                    # We skip it to avoid counting pre-existing failures.
                    pass

                node_last_values[node_ip] = cumulative_value

            if not delta_points:
                return {}

            # Aggregate deltas by bucket and node
            result = {}
            for bucket_start, node_ip, delta in delta_points:
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
            self._log_admin(f"{metric_column} (deltas - increases only): {total_points} event points from {len(result)} nodes")
            return result

        except Exception as e:
            self._log_admin(f"Error querying {metric_column}: {e}")
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
            self._log_admin(f"Error querying RSSI: {e}")
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
            self._log_admin(f"Error querying heap: {e}")
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
                FROM logs
                WHERE message_type = 'METRIC'
                OR message LIKE '%disconnect%'
            """)
            row = cursor.fetchone()
            return web.json_response({
                'min_time': row[0] if row[0] else None,
                'max_time': row[1] if row[1] else None
            })
        except Exception as e:
            self._log_admin(f"Error querying time range: {e}")
            return web.json_response({'min_time': None, 'max_time': None})
        finally:
            conn.close()

    def start_web(self):
        """Start the web server"""
        self.web_app = web.Application()
        self.web_app.router.add_get('/', self.handle_index)
        self.web_app.router.add_get('/api/status', self.handle_api_status)
        self.web_app.router.add_get('/api/status/stream', self.handle_api_status_stream)
        self.web_app.router.add_get('/api/logs/stream', self.handle_api_logs_stream)
        self.web_app.router.add_post('/api/logs/clear', self.handle_api_logs_clear)
        self.web_app.router.add_post('/api/command', self.handle_api_command)
        self.web_app.router.add_post('/api/layout', self.handle_api_layout)
        self.web_app.router.add_post('/api/reorder', self.handle_api_reorder)
        self.web_app.router.add_post('/api/node/data', self.handle_api_node_data)
        self.web_app.router.add_post('/api/node/html', self.handle_api_node_html)
        self.web_app.router.add_post('/api/search', self.handle_api_search)
        self.web_app.router.add_post('/api/logs/lookup', self.handle_api_logs_lookup)
        if HAS_SYSTEMD:
            self.web_app.router.add_post('/api/journal/query', self.handle_api_journal_query)
            self.web_app.router.add_get('/api/journal/range', self.handle_api_journal_time_range)
            self._log_admin("Journal API endpoints enabled")
        if self.graphs_enabled:
            self.web_app.router.add_post('/api/graph/data', self.handle_api_graph_data)
            self.web_app.router.add_post('/api/graph/raw_minute_data', self.handle_api_raw_minute_data)
            self.web_app.router.add_get('/api/graph/nodes', self.handle_api_graph_nodes)
            self.web_app.router.add_get('/api/graph/time_range', self.handle_api_graph_time_range)
            self._log_admin(f"Graph endpoints enabled")
        else:
            self._log_admin(f"Graph endpoints disabled (use --db-path to enable)")

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
            gap: 8px;
            padding: 6px 10px;
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
            gap: 6px;
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

        .filter-checkbox {
            display: flex;
            align-items: center;
            gap: 4px;
            cursor: pointer;
            white-space: nowrap;
            color: #333;
            font-size: 9px;
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
            padding: 2px 4px;
            border-radius: 4px;
        }

        .filter-label {
            font-size: 9px;
            color: #495057;
            white-space: nowrap;
        }

        .debug-filters .filter-textbox {
            border: 1px solid #ced4da;
            border-radius: 3px;
            padding: 2px 4px;
            font-size: 9px;
            width: 90px;
            background: white;
            cursor: text;
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

        .debug-toggle-btn:disabled,
        #searchInput:disabled {
            opacity: 0.5;
            cursor: not-allowed;
            pointer-events: none;
        }
        #searchInput {
            cursor: text;
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

        .debug-window .log-ctrl,
        .debug-window .log-node {
            border-left: 3px solid transparent;
            padding-left: 6px;
            transition: border-color 0.1s ease;
        }

        /* This comes after, so it wins when specificity is equal */
        .debug-window .log-cursor {
            border-left-color: #ff9800;
            background-color: rgba(255, 152, 0, 0.15);
        }
        .debug-window .log-cursor:hover {
            background-color: rgba(255, 152, 0, 0.25);
        }

        @keyframes spin {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }

        .search-spinner {
            display: inline-block;
            animation: spin 1s linear infinite;
            margin-right: 4px;
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

        .gap-marker {
            transition: opacity 0.1s ease;
            position: relative;
        }

        @keyframes fadeIn {
            from { opacity: 0; }
            to { opacity: 1; }
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
            <h2>🐛 Log</h2>
            <div class="debug-filters">
                <label class="filter-checkbox">
                    <input type="checkbox" id="filterExcludeCtrl"> Exclude CTRL
                </label>
                <div class="filter-input-group">
                    <span class="filter-label" id="filterIncludeLabel">Include</span>
                    <input type="text" id="filterIncludeNodes" placeholder="e.g., 2,3" class="filter-textbox">
                </div>
                <div class="filter-input-group">
                    <span class="filter-label" id="filterHighlightLabel">Highlight</span>
                    <input type="text" id="filterHighlightNodes" placeholder="e.g., 2,3" class="filter-textbox">
                </div>
                <div class="filter-input-group">
                    <span class="filter-label">Min log level</span>
                    <select id="filterMinLogLevel" class="filter-select">
                        <option value="0">DEBUG</option>
                        <option value="1">INFO</option>
                        <option value="2">WARN</option>
                        <option value="3">ERROR</option>
                        <option value="4" selected>No filter</option>
                    </select>
                </div>
                <div class="filter-input-group">
                    <span class="filter-label">Go to</span>
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

        // Base64 SVG - single up arrow
        const UP_ARROW_CURSOR = "url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0Ij48cG9seWdvbiBwb2ludHM9IjEyLDIgNCwxMCAyMCwxMCIgZmlsbD0iYmxhY2siIHN0cm9rZT0id2hpdGUiIHN0cm9rZS13aWR0aD0iMiIvPjwvc3ZnPg==') 12 2, auto";
        // Base64 SVG - single down arrow
        const DOWN_ARROW_CURSOR = "url('data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0Ij48cG9seWdvbiBwb2ludHM9IjEyLDIyIDQsMTQgMjAsMTQiIGZpbGw9ImJsYWNrIiBzdHJva2U9IndoaXRlIiBzdHJva2Utd2lkdGg9IjIiLz48L3N2Zz4=') 12 22, auto";


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
        let graphAutoRefreshEnabled = true;
        let graphAutoRefreshInterval = null;
        const GRAPH_AUTO_REFRESH_DELAY = 30000; // 30 seconds


        // Log level names
        const logLevelNames = ['DEBUG', 'INFO', 'WARN', 'ERROR'];

        // Debug window
        const MAX_LOG_ENTRIES= 2000;
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
        let deadGaps = new Set();  // Stores gap keys that have no logs in journal
        let queueRunning = false;
        let queueItems = [];
        const DebugViewOperation = Object.freeze({
            ADD_LOG: 'add_log',
            REBUILD_DISPLAY: 'rebuild_display',
            CLEAR_LOGS: 'clear_logs',
            REFILTER_DISPLAY: 'refilter_display',
            ADD_HISTORICAL_ABOVE: 'add_historical_above',
            ADD_HISTORICAL_BELOW: 'add_historical_below',
            ADD_HISTORICAL_BATCH: 'add_historical_batch',
            TRIM_BUFFER: 'trim_buffer',
            SCROLL_TO_TIMESTAMP: 'scroll_to_timestamp',
            FILL_GAP: 'fill_gap'
        });

        // Debug window search
        let activeSearchSession = null;
        let searchCaseSensitive = false;
        let searchWholeWord = false;
        let isSearching = false;
        let searchLogId = null;      // Primary cursor - for database searches and buffer navigation
        let searchTimestamp = null;   // Secondary cursor - for journal seeks (display/scroll)
        let activeFetchController = null;  // For cancelling background fetches
        let lastSearchDirection = null;    // 'next' or 'prev'
        let autoScrolling = false;         // Are we auto-scrolling to a match?
        let currentSearchAborted = false;  // Flag to ignore pending search results after cancel

        // Filter state
        let filterExcludeCtrl = false;
        let filterIncludeNodes = new Set();  // Set of node IP last octets to include (empty means all)
        let filterHighlightNodes = new Set(); // Set of node IP last octets to highlight
        let filterMinLogLevel = 4;  // Default OFF (4)
        let controllerIpPrefix = ''; // Will be set from server

        // Show a temporary toast message
        let toastTimeout = null;

        function showTemporaryMessage(msg, type = 'info') {
            // Remove existing toast if present
            const existingToast = document.getElementById('temp-toast');
            if (existingToast) {
                existingToast.remove();
                if (toastTimeout) clearTimeout(toastTimeout);
            }

            const toast = document.createElement('div');
            toast.id = 'temp-toast';
            toast.textContent = msg;

            // Set color based on type
            let textColor, borderColor;
            switch (type) {
                case 'search':
                    textColor = '#ffd966';  // Warm yellow for search
                    borderColor = '#ffd966';
                    break;
                case 'error':
                    textColor = '#f44336';  // Red for errors
                    borderColor = '#f44336';
                    break;
                case 'success':
                    textColor = '#4caf50';  // Green for success
                    borderColor = '#4caf50';
                    break;
                default: // 'info'
                    textColor = '#9cdcfe';  // Light blue (like node logs)
                    borderColor = '#2196f3';
            }

            toast.style.cssText = `
                position: fixed;
                bottom: 200px;
                right: 20px;
                background: #2d2d2d;
                color: ${textColor};
                border-left: 4px solid ${borderColor};
                padding: 8px 12px;
                border-radius: 4px;
                font-family: monospace;
                font-size: 11px;
                z-index: 10001;
                opacity: 0.95;
                transition: opacity 0.3s ease;
                box-shadow: 0 2px 8px rgba(0,0,0,0.3);
            `;
            document.body.appendChild(toast);

            toastTimeout = setTimeout(() => {
                toast.style.opacity = '0';
                setTimeout(() => {
                    if (toast.parentNode) toast.remove();
                }, 300);
                toastTimeout = null;
            }, 5000);
        }

        // Set CSS variables from server data if needed
        function updateCSSVariables(status) {
            if (status.grid_rows) {
                document.documentElement.style.setProperty('--grid-rows', status.grid_rows);
            }
            if (status.grid_columns) {
                document.documentElement.style.setProperty('--grid-columns', status.grid_columns);
            }
            const rows = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-rows'), 10) || 2;
            const cols = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-columns'), 10) || 4;
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
            if (toggleBtn && !isCollapsed && currentHeight !== parseInt(dockedHeight, 10)) {
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

        function queueDebugViewOperation(type, data = null) {
            // Return a promise so callers can await if needed (optional)
            return new Promise((resolve, reject) => {
                queueItems.push({ type, data, resolve, reject });
                if (!queueRunning) {
                    runQueue();
                }
            });
        }

        async function runQueue() {
            if (queueRunning) return;
            queueRunning = true;

            while (queueItems.length > 0) {
                const item = queueItems.shift();
                const startTime = Date.now();

                try {
                    switch (item.type) {
                        case DebugViewOperation.ADD_LOG:
                            addNewLogInternal(item.data);
                            break;
                        case DebugViewOperation.REBUILD_DISPLAY:
                            rebuildDebugDisplayInternal();
                            break;
                        case DebugViewOperation.CLEAR_LOGS:
                            clearLogsInternal();
                            break;
                        case DebugViewOperation.REFILTER_DISPLAY:
                            refilterAndRenderLogsInternal();
                            break;
                        case DebugViewOperation.ADD_HISTORICAL_ABOVE:
                            addHistoricalLogsAboveInternal(item.data);
                            break;
                        case DebugViewOperation.ADD_HISTORICAL_BELOW:
                            addHistoricalLogsBelowInternal(item.data);
                            break;
                        case DebugViewOperation.ADD_HISTORICAL_BATCH:
                            addHistoricalBatchInternal(item.data);
                            break;
                        case DebugViewOperation.TRIM_BUFFER:
                            const trimmed = trimBufferInternal();
                            if (trimmed) {
                                rebuildDebugDisplayInternal();
                            }
                            break;
                        case DebugViewOperation.SCROLL_TO_TIMESTAMP:
                            await scrollToTimestampInternal(item.data);
                            break;
                        case DebugViewOperation.FILL_GAP:
                            await fillGapDirectionalInternal(item.data);
                            break;
                        default:
                            console.error('Unknown debug view operation:', item.type);
                            item.reject(new Error(`Unknown operation: ${item.type}`));
                    }
                    const duration = Date.now() - startTime;
                    item.resolve();
                } catch (e) {
                    item.reject(e);
                }
            }

            queueRunning = false;
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

        // Parse node IP last octet from log line
        function extractNodeLastOctet(logLine) {
            // Match [NODE] [10.10.3.1] format
            const bracketMatch = logLine.match(/\\[NODE\\]\\s+\\[([0-9.]+)\\]/);
            if (bracketMatch && bracketMatch[1]) {
                const ipParts = bracketMatch[1].split('.');
                if (ipParts.length === 4) {
                    return ipParts[3];
                }
            }
            return null;
        }

        function extractLogLevel(logLine) {
            if (!logLine.includes('[NODE]')) return null;

            // Match [NODE] [IP] D/I/W/E (level is the first word after the IP)
            // Format: "[timestamp] [NODE] [10.10.3.5] I (12345) message..."
            const match = logLine.match(/\\[NODE\\]\\s+\\[[\\d.]+\\]\\s+([DIWE])\\s/);
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
            // ADMIN logs should NEVER appear in debug window
            if (logLine.includes('[ADMIN]')) {
                return false;
            }

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
            if (isNode && filterMinLogLevel < 4) {
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

        function handleLogClick(logDiv, timestamp) {
            return async (e) => {
                e.stopPropagation();
                const ts = parseFloat(logDiv.getAttribute('data-timestamp'));
                const logId = parseInt(logDiv.getAttribute('data-log_id'), 10);

                if (ts) {
                    resetSearch();
                    searchTimestamp = ts;
                    searchLogId = logId;  // Also set from the log
                    updateCursorHighlight();

                    // Flash the clicked line
                    logDiv.style.backgroundColor = '#4a4a4a';
                    setTimeout(() => {
                        if (logDiv) logDiv.style.backgroundColor = '';
                    }, 500);
                }
            };
        }

        // Don't call this, call refilterAndRenderLogs()
        function refilterAndRenderLogsInternal() {
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;

            // If we have no logElements yet, build them from buffer
            if (logElements.length === 0 && logBuffer.length > 0) {
                debugWindow.innerHTML = '';
                logElements = [];
                logTexts = [];
                for (const log of logBuffer) {
                    const className = log.message.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                    const logDiv = document.createElement('div');
                    logDiv.className = className;
                    logDiv.setAttribute('data-timestamp', log.timestamp);
                    logDiv.setAttribute('data-log_id', log.log_id || '0');

                    logDiv.textContent = log.message;
                    logDiv.addEventListener('click', handleLogClick(logDiv));
                    debugWindow.appendChild(logDiv);
                    logElements.push(logDiv);
                    logTexts.push(log.message);
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

         // Wot it says
        function refilterAndRenderLogs() {
            queueDebugViewOperation(DebugViewOperation.REFILTER_DISPLAY);
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
                includeLabel.textContent = `Include ${controllerIpPrefix}.X`;
            }
            if (highlightLabel && controllerIpPrefix) {
                highlightLabel.textContent = `Highlight ${controllerIpPrefix}.X`;
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
            filterMinLogLevel = logLevelSelect ? parseInt(logLevelSelect.value, 10) : 4;

            resetSearch();
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
                                <button class="btn-apply-level" onclick="sendCommand('${node.name}', 'log_level', {level: parseInt(this.previousElementSibling.value, 10)})">Apply</button>
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
            updateCSSVariables(status);

            const rows = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-rows'), 10) || 2;
            const cols = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--grid-columns'), 10) || 4;
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

        // Initialize debug window with scroll loading
        function initDebugWindow() {
            debugWindow = document.getElementById('debugWindow');
            if (!debugWindow) return;

            debugWindow.addEventListener('scroll', () => {
                if (autoScrolling) {
                    autoScrolling = false;
                    // Clear any active cursor monitoring
                    if (window._activeCursorElement && window._activeCursorElement._keepVisibleInterval) {
                        clearInterval(window._activeCursorElement._keepVisibleInterval);
                        window._activeCursorElement = null;
                    }
                }
            });

            debugWindow.addEventListener('wheel', () => {
                if (autoScrolling) {
                    autoScrolling = false;
                    if (window._activeCursorElement && window._activeCursorElement._keepVisibleInterval) {
                        clearInterval(window._activeCursorElement._keepVisibleInterval);
                        window._activeCursorElement = null;
                    }
                }
            });

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
            console.log(`[DEBUG] loadHistoricalLogsAbove called, isSearching=${isSearching}`);

            // Skip during search fetch
            if (isSearching || isLoading) {
                console.log('[DEBUG] loadHistoricalLogsAbove: skipping due to isSearching or isLoading');
            }

            if (isSearching || isLoading) return;

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

                const controller = new AbortController();
                activeFetchController = controller;

                console.log('[DEBUG] loadHistoricalLogsAbove: fetching initial logs from latest');
                try {
                    const response = await fetch('/api/journal/query', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        signal: controller.signal,
                        body: JSON.stringify({
                            timestamp: journalRange.latest,
                            before: 100,
                            after: 20
                        })
                    });
                    const data = await response.json();
                    if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                        currentScrollAnchor = data.logs[data.target_index]?.timestamp;
                        addHistoricalLogsAbove({logs: data.logs});
                    }
                } catch (e) {
                    if (e.name === 'AbortError') {
                        console.log('loadHistoricalLogsAbove: cancelled');
                        return;
                    }
                    console.error('loadHistoricalLogsAbove: error=', e);
                } finally {
                    if (activeFetchController === controller) {
                        activeFetchController = null;
                    }
                    isLoading = false;
                }
                return;
            }

            if (!beforeTimestamp) {
                console.log('[DEBUG] loadHistoricalLogsAbove: no beforeTimestamp, returning');
                return;
            }

            // Check if we're near the top of the scroll area
            if (debugWindow.scrollTop > 20) {
                console.log('[DEBUG] loadHistoricalLogsAbove: scrollTop > 20, returning');
                return;
            }

            console.log('[DEBUG] loadHistoricalLogsAbove: fetching historical logs before', beforeTimestamp);
            isLoading = true;
            const controller = new AbortController();
            activeFetchController = controller;

            try {
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    signal: controller.signal,
                    body: JSON.stringify({
                        timestamp: beforeTimestamp,
                        before: 100
                    })
                });
                const data = await response.json();

                if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                    const oldScrollHeight = debugWindow.scrollHeight;
                    const oldScrollTop = debugWindow.scrollTop;

                    addHistoricalLogsAbove({logs: data.logs});

                    const newScrollHeight = debugWindow.scrollHeight;
                    debugWindow.scrollTop = oldScrollTop + (newScrollHeight - oldScrollHeight);
                }

            } catch (e) {
                if (e.name === 'AbortError') {
                    console.log('loadHistoricalLogsAbove: cancelled');
                    return;
                }
                console.error('loadHistoricalLogsAbove: fetch error=', e);
            } finally {
                if (activeFetchController === controller) {
                    activeFetchController = null;
                }
                isLoading = false;
            }
        }

        // Load more logs below (when scrolling down near bottom)
        async function loadMoreLogsBelow() {
            // Skip during search fetch
            if (isSearching) {
                console.log('[DEBUG] loadMoreLogsBelow: skipping due to isSearching');
            }

            if (isSearching || isLoading || !hasMoreDown || autoScrollLocked) return;

            // Check if we're near the bottom
            const distanceToBottom = debugWindow.scrollHeight - debugWindow.scrollTop - debugWindow.clientHeight;
            if (distanceToBottom > 50) {
                console.log('[DEBUG] loadMoreLogsBelow: distanceToBottom > 50, returning');
                return;
            }

            // Get the latest log we currently have
            const latestLog = logBuffer.length > 0 ? logBuffer[logBuffer.length - 1] : null;
            const afterTimestamp = latestLog ? latestLog.timestamp : null;

            if (!afterTimestamp) {
                console.log('[DEBUG] loadMoreLogsBelow: no afterTimestamp, returning');
                return;
            }

            console.log('[DEBUG] loadMoreLogsBelow: fetching logs after', afterTimestamp);
            isLoading = true;
            const controller = new AbortController();
            activeFetchController = controller;

            try {
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    signal: controller.signal,
                    body: JSON.stringify({
                        timestamp: afterTimestamp,
                        after: 100
                    })
                });
                const data = await response.json();

                if (data.status === 'ok' && data.logs.length > 0) {
                    addHistoricalLogsBelow({logs: data.logs});
                }

                hasMoreDown = data.has_more !== false;

            } catch (e) {
                if (e.name === 'AbortError') {
                    console.log('loadMoreLogsBelow: cancelled');
                    return;
                }
                console.error('Failed to load more logs:', e);
            } finally {
                if (activeFetchController === controller) {
                    activeFetchController = null;
                }
                isLoading = false;
            }
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

        async function fetchAndScrollToTimestamp(timestamp, targetLogId, direction) {
            console.log(`[DEBUG] fetchAndScrollToTimestamp START:`, {
                timestamp: timestamp,
                targetLogId: targetLogId,
                direction: direction
            });
            console.time('fetchAndScrollToTimestamp');

            if (activeFetchController) {
                console.log('[DEBUG] fetchAndScrollToTimestamp: aborting activeFetchController');
                activeFetchController.abort();
                activeFetchController = null;
            }

            return new Promise(async (resolve, reject) => {
                try {
                    // -------- Step 1: Initial query with a generous window --------
                    // Journal timestamp is ALWAYS later than database timestamp,
                    // but we want context before AND after the target.
                    // Use a large window: 1000 before, 2000 after (extra after accounts for latency)
                    console.log('[DEBUG] fetchAndScrollToTimestamp: Step 1 - fetching with generous window');
                    console.time('fetchAndScrollToTimestamp: step1_journal_query');

                    const response1 = await fetch('/api/journal/query', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            timestamp: timestamp,
                            before: 1000,   // 1000 before for context
                            after: 2000     // 2000 after to account for journal latency
                        })
                    });
                    const data1 = await response1.json();
                    console.timeEnd('fetchAndScrollToTimestamp: step1_journal_query');
                    console.log(`[DEBUG] fetchAndScrollToTimestamp: Step 1 returned ${data1.logs?.length} logs`);

                    if (data1.status !== 'ok' || !data1.logs || data1.logs.length === 0) {
                        console.log('[DEBUG] fetchAndScrollToTimestamp: no logs returned from journal');
                        console.timeEnd('fetchAndScrollToTimestamp');
                        resolve();
                        return;
                    }

                    // -------- Step 2: Try to find the target log_id in the results --------
                    let targetFound = data1.logs.some(log => log.log_id === targetLogId);

                    if (targetFound) {
                        console.log(`[DEBUG] fetchAndScrollToTimestamp: target log_id ${targetLogId} found in initial query`);
                        // Process the logs we have - this includes both before and after for context
                        const filteredNewLogs = data1.logs.filter(log => shouldDisplayLog(log.message));
                        const uniqueNewLogs = filterUniqueLogs(filteredNewLogs);

                        if (uniqueNewLogs.length > 0) {
                            await queueDebugViewOperation(DebugViewOperation.ADD_HISTORICAL_BATCH, {
                                logs: uniqueNewLogs,
                                targetLogId: targetLogId,
                                direction: direction
                            });
                        }
                        console.timeEnd('fetchAndScrollToTimestamp');
                        resolve();
                        return;
                    }

                    // -------- Step 3: Target not found - determine direction to expand --------
                    console.log(`[DEBUG] fetchAndScrollToTimestamp: target ${targetLogId} NOT found in initial window`);

                    // Check if target is before or after the fetched range using log_id comparison
                    const firstLogId = data1.logs[0]?.log_id || -Infinity;
                    const lastLogId = data1.logs[data1.logs.length - 1]?.log_id || Infinity;

                    console.log(`[DEBUG] fetchAndScrollToTimestamp: first_log_id=${firstLogId}, last_log_id=${lastLogId}`);

                    let allLogs = [...data1.logs];
                    let fetchCount = 0;
                    const MAX_FETCHES = 10; // Max 10 fetches (30,000 entries total)
                    let targetFoundInExtra = false;

                    // If target is before the first log, fetch backwards
                    if (targetLogId < firstLogId) {
                        console.log('[DEBUG] fetchAndScrollToTimestamp: target is BEFORE the fetched range, fetching backwards');
                        let firstTimestamp = data1.logs[0].timestamp;

                        while (fetchCount < MAX_FETCHES && !targetFoundInExtra) {
                            fetchCount++;
                            console.log(`[DEBUG] fetchAndScrollToTimestamp: backward fetch ${fetchCount}, first_timestamp=${firstTimestamp}`);

                            console.time(`fetchAndScrollToTimestamp: step_backward_${fetchCount}`);
                            const response = await fetch('/api/journal/query', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    timestamp: firstTimestamp,
                                    before: 2000,  // Fetch 2000 older entries
                                    after: 0
                                })
                            });
                            const data = await response.json();
                            console.timeEnd(`fetchAndScrollToTimestamp: step_backward_${fetchCount}`);
                            console.log(`[DEBUG] fetchAndScrollToTimestamp: backward fetch ${fetchCount} returned ${data.logs?.length} logs`);

                            if (data.status !== 'ok' || !data.logs || data.logs.length === 0) {
                                console.log('[DEBUG] fetchAndScrollToTimestamp: no more older logs available');
                                break;
                            }

                            // Check if target is in this batch
                            targetFoundInExtra = data.logs.some(log => log.log_id === targetLogId);

                            // Add these logs to our collection (prepend for correct order)
                            allLogs = [...data.logs, ...allLogs];

                            if (targetFoundInExtra) {
                                console.log(`[DEBUG] fetchAndScrollToTimestamp: target found in backward fetch ${fetchCount}`);
                                break;
                            }

                            // Update first timestamp for next fetch
                            firstTimestamp = data.logs[0].timestamp;
                        }
                    }
                    // If target is after the last log, fetch forward
                    else if (targetLogId > lastLogId) {
                        console.log('[DEBUG] fetchAndScrollToTimestamp: target is AFTER the fetched range, fetching forward');
                        let lastTimestamp = data1.logs[data1.logs.length - 1].timestamp;

                        while (fetchCount < MAX_FETCHES && !targetFoundInExtra) {
                            fetchCount++;
                            console.log(`[DEBUG] fetchAndScrollToTimestamp: forward fetch ${fetchCount}, last_timestamp=${lastTimestamp}`);

                            console.time(`fetchAndScrollToTimestamp: step_forward_${fetchCount}`);
                            const response = await fetch('/api/journal/query', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    timestamp: lastTimestamp,
                                    before: 0,
                                    after: 2000  // Fetch 2000 newer entries
                                })
                            });
                            const data = await response.json();
                            console.timeEnd(`fetchAndScrollToTimestamp: step_forward_${fetchCount}`);
                            console.log(`[DEBUG] fetchAndScrollToTimestamp: forward fetch ${fetchCount} returned ${data.logs?.length} logs`);

                            if (data.status !== 'ok' || !data.logs || data.logs.length === 0) {
                                console.log('[DEBUG] fetchAndScrollToTimestamp: no more newer logs available');
                                break;
                            }

                            // Check if target is in this batch
                            targetFoundInExtra = data.logs.some(log => log.log_id === targetLogId);

                            // Add these logs to our collection
                            allLogs = [...allLogs, ...data.logs];

                            if (targetFoundInExtra) {
                                console.log(`[DEBUG] fetchAndScrollToTimestamp: target found in forward fetch ${fetchCount}`);
                                break;
                            }

                            // Update last timestamp for next fetch
                            lastTimestamp = data.logs[data.logs.length - 1].timestamp;
                        }
                    } else {
                        // Target is within the fetched range but not found (maybe filtered out?)
                        console.log('[DEBUG] fetchAndScrollToTimestamp: target log_id in range but not found (maybe filtered out)');
                        console.timeEnd('fetchAndScrollToTimestamp');
                        resolve();
                        return;
                    }

                    // -------- Step 4: Process all collected logs --------
                    console.log(`[DEBUG] fetchAndScrollToTimestamp: processing ${allLogs.length} total logs`);

                    // Deduplicate by log_id (keep latest occurrence if duplicates exist)
                    const seen = new Map();
                    for (const log of allLogs) {
                        seen.set(log.log_id, log);
                    }
                    const uniqueLogs = Array.from(seen.values());
                    console.log(`[DEBUG] fetchAndScrollToTimestamp: after dedup: ${uniqueLogs.length} logs`);

                    // Sort by log_id for consistent ordering
                    uniqueLogs.sort((a, b) => a.log_id - b.log_id);

                    const filteredNewLogs = uniqueLogs.filter(log => shouldDisplayLog(log.message));
                    const uniqueNewLogs = filterUniqueLogs(filteredNewLogs);
                    console.log(`[DEBUG] fetchAndScrollToTimestamp: after filters: ${uniqueNewLogs.length} logs`);

                    if (uniqueNewLogs.length > 0) {
                        console.log('[DEBUG] fetchAndScrollToTimestamp: queueing ADD_HISTORICAL_BATCH');
                        await queueDebugViewOperation(DebugViewOperation.ADD_HISTORICAL_BATCH, {
                            logs: uniqueNewLogs,
                            targetLogId: targetLogId,
                            direction: direction
                        });
                    } else {
                        console.log('[DEBUG] fetchAndScrollToTimestamp: no new logs to add');
                        // If we didn't find the target, try one more time with a very wide window
                        if (!targetFoundInExtra) {
                            console.log('[DEBUG] fetchAndScrollToTimestamp: target still not found, trying very wide window');
                            // Use a 60-second window centered on the timestamp (30s before, 30s after)
                            // This accounts for any significant latency
                            const wideBefore = Math.max(0, timestamp - 30);
                            const wideAfter = timestamp + 30;

                            console.time('fetchAndScrollToTimestamp: step_wide_window');
                            const response = await fetch('/api/journal/query', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({
                                    since: wideBefore,
                                    until: wideAfter
                                })
                            });
                            const data = await response.json();
                            console.timeEnd('fetchAndScrollToTimestamp: step_wide_window');
                            console.log(`[DEBUG] fetchAndScrollToTimestamp: wide window returned ${data.logs?.length} logs`);

                            if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                                const wideFiltered = data.logs.filter(log => shouldDisplayLog(log.message));
                                const wideUnique = filterUniqueLogs(wideFiltered);
                                if (wideUnique.length > 0) {
                                    console.log('[DEBUG] fetchAndScrollToTimestamp: queueing wide window logs');
                                    await queueDebugViewOperation(DebugViewOperation.ADD_HISTORICAL_BATCH, {
                                        logs: wideUnique,
                                        targetLogId: targetLogId,
                                        direction: direction
                                    });
                                }
                            }
                        }
                    }

                    console.timeEnd('fetchAndScrollToTimestamp');
                    resolve();

                } catch (e) {
                    console.error('[DEBUG] fetchAndScrollToTimestamp error:', e);
                    console.timeEnd('fetchAndScrollToTimestamp');
                    reject(e);
                }
            });
        }

        // Don't call this, call addHistoricalLogsAbove()
        function addHistoricalLogsAboveInternal(data) {
            const { logs, skipRebuild = false } = data;

            if (!logs || !logs.length) return;

            // Convert to internal format
            const newEntries = logs.map(log => ({
                log_id: log.log_id || 0,
                timestamp: log.timestamp,
                message: log.message,
                source: log.source || ''
            })).filter(log => shouldDisplayLog(log.message));

            // Filter out duplicates
            const uniqueNewEntries = filterUniqueLogs(newEntries);
            if (uniqueNewEntries.length === 0) return;

            // Add to buffer
            for (const entry of uniqueNewEntries) {
                logBuffer.push(entry);
            }

            // Sort by LOG_ID
            logBuffer.sort((a, b) => a.log_id - b.log_id);

            if (!skipRebuild) {
                rebuildDebugDisplayInternal();
            }
        }

        // Add historical logs above existing logs
        function addHistoricalLogsAbove(data) {
            queueDebugViewOperation(DebugViewOperation.ADD_HISTORICAL_ABOVE, data);
        }

        // Don't call this, call addHistoricalLogsBelow
        function addHistoricalLogsBelowInternal(data) {
            const { logs, skipRebuild = false } = data;

            if (!logs || !logs.length) return;

            // Convert to internal format and filter
            const newEntries = logs.map(log => ({
                log_id: log.log_id || 0,
                timestamp: log.timestamp,
                message: log.message,
                source: log.source || ''
            })).filter(log => shouldDisplayLog(log.message));

            // Filter out duplicates
            const uniqueNewEntries = filterUniqueLogs(newEntries);
            if (uniqueNewEntries.length === 0) return;

            // Add to buffer
            for (const entry of uniqueNewEntries) {
                logBuffer.push(entry);
            }

            // Sort by LOG_ID
            logBuffer.sort((a, b) => a.log_id - b.log_id);

            if (!skipRebuild) {
                rebuildDebugDisplayInternal();
            }
        }

        // Add historical logs below existing logs
        function addHistoricalLogsBelow(data) {
            queueDebugViewOperation(DebugViewOperation.ADD_HISTORICAL_BELOW, data);
        }

        function addHistoricalBatchInternal(data) {
            const { logs, targetLogId, direction } = data;

            console.log('addHistoricalBatchInternal: START', {
                targetLogId: targetLogId,
                targetLogId_type: typeof targetLogId,
                logs_count: logs.length,
                first_log_id: logs[0]?.log_id,
                last_log_id: logs[logs.length - 1]?.log_id
            });

            if (!logs || logs.length === 0) {
                console.log('addHistoricalBatchInternal: no logs');
                return;
            }

            // Split logs into those above and below target
            const aboveLogs = [];
            const belowLogs = [];
            let foundTarget = false;

            for (const log of logs) {
                if (log.log_id === targetLogId) {
                    foundTarget = true;
                    // Add target to below logs - it will be sorted correctly
                    belowLogs.push(log);
                } else if (!foundTarget) {
                    aboveLogs.push(log);
                } else {
                    belowLogs.push(log);
                }
            }

            console.log('addHistoricalBatchInternal: split', {
                above_count: aboveLogs.length,
                below_count: belowLogs.length,
                foundTarget: foundTarget
            });

            // Add logs above (older) - skip rebuild
            if (aboveLogs.length > 0) {
                addHistoricalLogsAboveInternal({ logs: aboveLogs, skipRebuild: true });
            }

            // Add logs below (newer) - skip rebuild
            if (belowLogs.length > 0) {
                addHistoricalLogsBelowInternal({ logs: belowLogs, skipRebuild: true });
            }

            // Single rebuild
            rebuildDebugDisplayInternal();

            // Scroll to target
            const targetElement = findLogElementByLogId(targetLogId);
            console.log('addHistoricalBatchInternal: targetElement found?', {
                targetLogId: targetLogId,
                found: !!targetElement,
                element_log_id: targetElement?.getAttribute('data-log_id'),
                element_timestamp: targetElement?.getAttribute('data-timestamp')
            });

            if (targetElement) {
                targetElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                targetElement.classList.add('log-cursor');
                targetElement.style.backgroundColor = '#ff9800';
                setTimeout(() => {
                    if (targetElement) targetElement.style.backgroundColor = '';
                }, 300);

                searchLogId = targetLogId;
                const timestampAttr = targetElement.getAttribute('data-timestamp');
                if (timestampAttr) {
                    searchTimestamp = parseFloat(timestampAttr);
                }
                lastSearchDirection = direction;
                updateCursorHighlight();
                console.log('addHistoricalBatchInternal: scroll completed');
            } else {
                console.warn('addHistoricalBatchInternal: target element NOT FOUND for log_id:', targetLogId);
            }
        }

        // Don't call this, call addNewLog()
        function addNewLogInternal(logData) {
            let message, timestamp, source, log_id;
            if (typeof logData === 'string') {
                message = logData;
                timestamp = Date.now() / 1000;
                source = '';
                log_id = 0;
            } else {
                message = logData.message;
                timestamp = logData.timestamp;
                source = logData.source || '';
                log_id = logData.log_id || 0;
            }

            // Skip ADMIN logs entirely
            if (source === 'ADMIN' || message.includes('[ADMIN]')) {
                return;
            }

            // If timestamp is null/undefined, try to parse from message
            if (!timestamp) {
                const timeMatch = message.match(/^\\[(\\d{2}\\/\\d{2}\\s+\\d{2}:\\d{2}:\\d{2})\\]/);
                if (timeMatch) {
                    const timeStr = timeMatch[1];
                    const [datePart, timePart] = timeStr.split(' ');
                    const [day, month] = datePart.split('/');
                    const [hours, minutes, seconds] = timePart.split(':');
                    const now = new Date();
                    const parsedDate = new Date(now.getFullYear(), parseInt(month) - 1, parseInt(day),
                                                parseInt(hours), parseInt(minutes), parseInt(seconds));
                    timestamp = parsedDate.getTime() / 1000;
                } else {
                    timestamp = Date.now() / 1000;
                }
            }

            // Update maxNormalGapSeconds
            if (logBuffer.length > 0) {
                const lastTimestamp = logBuffer[logBuffer.length - 1].timestamp;
                updateMaxNormalGap(timestamp, lastTimestamp);
            }

            // Add to buffer
            logBuffer.push({
                timestamp: timestamp,
                message: message,
                log_id: log_id,
                source: source
            });

            // Sort periodically - only every 100 logs (logs arrive near-chronologically)
            if (logBuffer.length % 100 === 0) {
                logBuffer.sort((a, b) => a.timestamp - b.timestamp);
            }

            // Add to DOM if it passes filters
            if (shouldDisplayLog(message)) {
                const shouldScroll = !autoScrollLocked && isAtBottom;
                const className = message.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                const logDiv = document.createElement('div');
                logDiv.className = className;
                logDiv.setAttribute('data-timestamp', timestamp);
                logDiv.setAttribute('data-log_id', log_id);

                const highlightedContent = applyHighlighting(message);
                logDiv.innerHTML = highlightedContent;
                logDiv.addEventListener('click', handleLogClick(logDiv));

                debugWindow.appendChild(logDiv);
                logElements.push(logDiv);
                logTexts.push(message);
                logTimestamps.push(timestamp);

                if (shouldScroll) {
                    debugWindow.scrollTop = debugWindow.scrollHeight;
                }
            }
        }

        // Add a newly arrived log to the debug window
        function addNewLog(logData) {
            queueDebugViewOperation(DebugViewOperation.ADD_LOG, logData);
        }

        function findCenterLogId() {
            /**
            * Find the log ID at the center of the viewport.
            * Returns null if no log found.
            */
            if (!debugWindow) return null;

            const viewportCenter = debugWindow.scrollTop + debugWindow.clientHeight / 2;
            let closestElement = null;
            let closestDistance = Infinity;

            for (const el of logElements) {
                if (el.style.display !== 'none' && el.style.visibility !== 'hidden') {
                    const rect = el.getBoundingClientRect();
                    const elCenter = rect.top + rect.height / 2;
                    const distance = Math.abs(elCenter - viewportCenter);
                    if (distance < closestDistance) {
                        closestDistance = distance;
                        closestElement = el;
                    }
                }
            }

            if (closestElement) {
                const logId = closestElement.getAttribute('data-log_id');
                if (logId && logId !== 'gap' && logId !== 'null') {
                    const id = parseInt(logId, 10);
                    if (!isNaN(id)) {
                        return id;
                    }
                }
            }

            return null;
        }

        function trimBufferInternal() {
            /**
            * Trim buffer to max size while preserving the visible range.
            * Called from the queue processor.
            * Returns true if trimming occurred, false otherwise.
            */
            if (logBuffer.length <= MAX_LOG_ENTRIES) {
                return false;
            }

            if (!debugWindow) {
                // No window? Trim to prevent memory issues
                if (logBuffer.length > MAX_LOG_ENTRIES * 1.5) {
                    logBuffer.splice(0, logBuffer.length - MAX_LOG_ENTRIES);
                    return true;
                }
                return false;
            }

            // Determine the visible range with generous margins
            const viewportHeight = debugWindow.clientHeight;
            const margin = viewportHeight * 0.5; // 50% margin above and below

            const viewportTop = debugWindow.scrollTop;
            const viewportBottom = viewportTop + viewportHeight;

            const protectedTop = viewportTop - margin;
            const protectedBottom = viewportBottom + margin;

            // Find the log IDs that fall within the protected range
            let protectedLogIds = new Set();
            let earliestProtectedTimestamp = null;
            let latestProtectedTimestamp = null;

            for (const el of logElements) {
                if (el.style.display !== 'none' && el.style.visibility !== 'hidden') {
                    const rect = el.getBoundingClientRect();
                    const elTop = rect.top;
                    const elBottom = rect.bottom;

                    // Check if this element overlaps the protected range
                    if (elTop < protectedBottom && elBottom > protectedTop) {
                        const logId = el.getAttribute('data-log_id');
                        if (logId && logId !== 'gap' && logId !== 'null') {
                            const id = parseInt(logId, 10);
                            if (!isNaN(id)) {
                                protectedLogIds.add(id);

                                // Also track timestamps for additional safety
                                const ts = parseFloat(el.getAttribute('data-timestamp'));
                                if (ts && !isNaN(ts)) {
                                    if (earliestProtectedTimestamp === null || ts < earliestProtectedTimestamp) {
                                        earliestProtectedTimestamp = ts;
                                    }
                                    if (latestProtectedTimestamp === null || ts > latestProtectedTimestamp) {
                                        latestProtectedTimestamp = ts;
                                    }
                                }
                            }
                        }
                    }
                }
            }

            // If we couldn't find any protected logs, use the center log as fallback
            if (protectedLogIds.size === 0) {
                const centerLogId = findCenterLogId();
                if (centerLogId !== null) {
                    protectedLogIds.add(centerLogId);
                }
            }

            // Calculate how many entries to remove
            let entriesToRemove = logBuffer.length - MAX_LOG_ENTRIES;

            // If we have protected logs, make sure we don't trim them
            if (protectedLogIds.size > 0) {
                // Find the earliest protected log in the buffer
                let earliestProtectedIndex = -1;
                for (let i = 0; i < logBuffer.length; i++) {
                    if (protectedLogIds.has(logBuffer[i].log_id)) {
                        earliestProtectedIndex = i;
                        break;
                    }
                }

                if (earliestProtectedIndex !== -1) {
                    // Keep at least 100 entries before the earliest protected log
                    // This provides context when scrolling up
                    const minKeep = Math.max(0, earliestProtectedIndex - 100);
                    entriesToRemove = Math.min(entriesToRemove, minKeep);
                }
            }

            // If we still need to remove entries (buffer is critically large),
            // we might need to trim even protected logs, but do it conservatively
            if (entriesToRemove > 0 && protectedLogIds.size > 0) {
                // Try to find the index of the latest protected log
                let latestProtectedIndex = -1;
                for (let i = logBuffer.length - 1; i >= 0; i--) {
                    if (protectedLogIds.has(logBuffer[i].log_id)) {
                        latestProtectedIndex = i;
                        break;
                    }
                }

                // If we're still over limit and can't remove enough before the earliest protected log,
                // we can remove some from the very beginning (oldest) even if it's in the protected range
                // but only as a last resort
                if (entriesToRemove > 0 && earliestProtectedIndex !== -1) {
                    // Calculate what we can safely remove
                    const safeRemove = Math.min(entriesToRemove, earliestProtectedIndex);
                    entriesToRemove = safeRemove;

                    // If we still can't remove enough (earliestProtectedIndex is too small),
                    // we'll remove what we can and accept a small view shift
                    if (entriesToRemove <= 0 && logBuffer.length > MAX_LOG_ENTRIES * 1.5) {
                        // Critical situation - remove at least 10 entries
                        entriesToRemove = Math.min(10, logBuffer.length - MAX_LOG_ENTRIES);
                    }
                }
            }

            // Actually remove entries
            if (entriesToRemove > 0) {
                // Store the protected range for restoration
                window._protectedLogIds = protectedLogIds;
                window._protectedEarliestTimestamp = earliestProtectedTimestamp;
                window._protectedLatestTimestamp = latestProtectedTimestamp;

                logBuffer.splice(0, entriesToRemove);
                return true;
            }

            return false;
        }

        // Trim buffer (does NOT touch DOM)
        function trimBuffer() {
            queueDebugViewOperation(DebugViewOperation.TRIM_BUFFER);
        }

        // Debug window trimmer
        function startIntelligentTrimmer() {
            /**
            * Start a trimmer that preserves the visible range.
            */
            setInterval(() => {
                // Only trim if we're significantly over the limit
                if (logBuffer.length > MAX_LOG_ENTRIES * 1.05) { // 5% over threshold
                    trimBuffer();
                }
            }, 10000); // Check every 10 seconds
        }

        async function getDbTimestampByLogId(logId) {
            if (!logId || logId === 0) return null;

            try {
                const response = await fetch('/api/logs/lookup', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ log_id: logId })
                });
                const data = await response.json();
                if (data.status === 'ok' && data.timestamp) {
                    return data.timestamp;
                }
            } catch (e) {
                console.warn('getDbTimestampByLogId failed:', e);
            }
            return null;
        }

        async function getJournalTimestampByLogId(logId, approxTimestamp) {
            console.log(`getJournalTimestampByLogId: called with logId=${logId}, approxTimestamp=${approxTimestamp}`);
            if (!logId || logId === 0) return approxTimestamp;

            try {
                // Use a 60-second window around the approximate timestamp
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        since: approxTimestamp - 60,
                        until: approxTimestamp + 60
                    })
                });
                const data = await response.json();
                console.log(`getJournalTimestampByLogId: response received, logs count=${data.logs?.length}`);

                if (data.status === 'ok' && data.logs) {
                    const match = data.logs.find(log => log.log_id === logId);
                    if (match && match.timestamp) {
                        console.log(`getJournalTimestampByLogId: found match with timestamp ${match.timestamp}`);
                        return match.timestamp;
                    } else {
                        console.log(`getJournalTimestampByLogId: no match found for log_id ${logId}`);
                    }
                }
            } catch (e) {
                console.warn('getJournalTimestampByLogId failed:', e);
            }
            console.log(`getJournalTimestampByLogId: returning approxTimestamp ${approxTimestamp}`);
            return approxTimestamp;
        }

        function findLogElementByTimestamp(timestamp) {
            for (let i = 0; i < logElements.length; i++) {
                const ts = parseFloat(logElements[i].getAttribute('data-timestamp'));
                if (Math.abs(ts - timestamp) < 0.001) {
                    return logElements[i];
                }
            }
            return null;
        }

        function findLogElementByLogId(logId) {
            const targetId = parseInt(logId, 10);
            console.log('findLogElementByLogId: looking for', {
                logId: logId,
                targetId: targetId,
                logElements_length: logElements.length,
                first_5_log_ids: logElements.slice(0, 5).map(el => el.getAttribute('data-log_id'))
            });

            if (isNaN(targetId)) {
                console.log('findLogElementByLogId: targetId is NaN');
                return null;
            }

            for (let i = 0; i < logElements.length; i++) {
                const dataLogId = parseInt(logElements[i].getAttribute('data-log_id'), 10);
                if (dataLogId === targetId) {
                    console.log('findLogElementByLogId: FOUND at index', i, 'log_id=', dataLogId);
                    return logElements[i];
                }
            }

            console.log('findLogElementByLogId: NOT FOUND');
            return null;
        }

        // Shared function to filter out duplicate logs based on log_id
        function filterUniqueLogs(newEntries) {
            if (!newEntries || newEntries.length === 0) return [];

            // Create a Set of existing log_ids for O(1) lookup
            const existingLogIds = new Set(logBuffer.map(log => log.log_id));

            // Only keep logs that don't already exist (by log_id)
            return newEntries.filter(log => {
                if (log.log_id === 0) {
                    // Fallback: if no log_id, use timestamp + message (rare)
                    return !logBuffer.some(existing =>
                        existing.message === log.message &&
                        Math.abs(existing.timestamp - log.timestamp) < 0.1
                    );
                }
                return !existingLogIds.has(log.log_id);
            });
        }

        // Don't call this one, call fillGapDirectional()
        async function fillGapDirectionalInternal(data) {
            const { edgeTimestamp, direction, gapKey, olderTimestamp, newerTimestamp, gapMarkerElement } = data;
            // Store original values for later use
            const originalOlder = olderTimestamp;
            const originalNewer = newerTimestamp;
            const BATCH_SIZE = 50;

            // Note: We cannot store DOM element references across async boundaries reliably
            // So we'll need to find the gap marker by its key after the operation completes

            try {
                const requestBody = {
                    timestamp: parseFloat(edgeTimestamp),
                    before: direction === 'older' ? BATCH_SIZE : 0,
                    after: direction === 'newer' ? BATCH_SIZE : 0
                };

                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(requestBody)
                });

                const result = await response.json();

                if (result.status === 'ok' && result.logs && result.logs.length > 0) {
                    const newEntries = result.logs
                        .map(log => ({
                            timestamp: log.timestamp,
                            message: log.message,
                            log_id: log.log_id || 0
                        }))
                        .filter(log => shouldDisplayLog(log.message));

                    if (newEntries.length > 0) {
                        // Filter out duplicates
                        const uniqueNew = filterUniqueLogs(newEntries);

                        if (uniqueNew.length > 0) {
                            // Add to buffer (lock is held, so safe)
                            logBuffer.push(...uniqueNew);
                            logBuffer.sort((a, b) => a.timestamp - b.timestamp);

                            // Rebuild display (call internal directly since lock is held)
                            rebuildDebugDisplayInternal();

                            // Scroll to the gap area after rebuild
                            setTimeout(() => {
                                const markers = document.querySelectorAll('.gap-marker');
                                let bestMarker = null;
                                let smallestDistance = Infinity;

                                for (const marker of markers) {
                                    const markerOlder = parseFloat(marker.dataset.olderTimestamp);
                                    const markerNewer = parseFloat(marker.dataset.newerTimestamp);

                                    if (markerOlder >= originalOlder && markerNewer <= originalNewer) {
                                        bestMarker = marker;
                                        break;
                                    }

                                    const distance = Math.abs(markerOlder - originalOlder) + Math.abs(markerNewer - originalNewer);
                                    if (distance < smallestDistance) {
                                        smallestDistance = distance;
                                        bestMarker = marker;
                                    }
                                }

                                if (bestMarker) {
                                    bestMarker.scrollIntoView({ block: 'center', behavior: 'smooth' });
                                }
                            }, 200);

                            showTemporaryMessage(`Loaded ${uniqueNew.length} logs`);
                            return;
                        }
                    }
                }

                // No logs found or all were duplicates
                deadGaps.add(gapKey);
                // Remove the gap marker from DOM
                const markers = document.querySelectorAll('.gap-marker');
                for (const marker of markers) {
                    if (marker.dataset.gapKey === gapKey) {
                        marker.remove();
                        break;
                    }
                }
                showTemporaryMessage('No logs exist in this gap');

            } catch (e) {
                console.error('Error filling gap:', e);
                showTemporaryMessage('Failed to load logs: ' + e.message);
            }
        }

        // Fill a gap by fetching logs in a specific direction
        async function fillGapDirectional(edgeTimestamp, direction, gapMarkerElement) {
            // Extract data from the DOM element before queueing (DOM elements can't be serialized)
            const gapKey = gapMarkerElement.dataset.gapKey;
            const olderTimestamp = parseFloat(gapMarkerElement.dataset.olderTimestamp);
            const newerTimestamp = parseFloat(gapMarkerElement.dataset.newerTimestamp);

            // Visual feedback while waiting
            gapMarkerElement.style.opacity = '0.5';
            gapMarkerElement.style.cursor = 'wait';

            queueDebugViewOperation(DebugViewOperation.FILL_GAP, {
                edgeTimestamp: parseFloat(edgeTimestamp),
                direction: direction,
                gapKey: gapKey,
                olderTimestamp: olderTimestamp,
                newerTimestamp: newerTimestamp,
                gapMarkerElement: null  // Don't store DOM reference, we'll find by key later
            });
        }

        // Don't call this, call rebuildDebugDisplay()
        function rebuildDebugDisplayInternal() {
            if (!debugWindow) return;

            // Get any pending state
            const protectedLogIds = window._protectedLogIds;
            const protectedEarliestTimestamp = window._protectedEarliestTimestamp;
            const protectedLatestTimestamp = window._protectedLatestTimestamp;

            // Clear pending state
            window._protectedLogIds = null;
            window._protectedEarliestTimestamp = null;
            window._protectedLatestTimestamp = null;

            const oldScrollTop = debugWindow.scrollTop;
            const oldScrollHeight = debugWindow.scrollHeight;

            // Sort buffer by TIMESTAMP for display (timestamps are more reliable for gap detection)
            const sortedBuffer = [...logBuffer];
            sortedBuffer.sort((a, b) => a.timestamp - b.timestamp);

            const shouldScrollToBottom = !autoScrollLocked && isAtBottom;

            debugWindow.innerHTML = '';
            logElements = [];
            logTexts = [];
            logTimestamps = [];

            let visibleCount = 0;
            let lastDisplayedTimestamp = null;
            let lastDisplayedLogId = null;  // Track log ID too
            const currentGapKeys = new Set();  // Track gaps that exist in this rebuild

            // Helper to format duration nicely
            function formatDuration(seconds) {
                seconds = Math.round(seconds);
                const days = Math.floor(seconds / 86400);
                const hours = Math.floor((seconds % 86400) / 3600);
                const minutes = Math.floor((seconds % 3600) / 60);
                const secs = seconds % 60;

                if (days > 0) {
                    return `${days}d ${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
                } else if (hours > 0) {
                    return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
                } else {
                    return `${minutes}m ${secs}s`;
                }
            }

            for (const log of sortedBuffer) {
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
                    // Check for gap before displaying this log - USE TIMESTAMPS
                    if (lastDisplayedTimestamp !== null) {
                        const gap = log.timestamp - lastDisplayedTimestamp;
                        // Use a threshold based on maxNormalGapSeconds (which tracks typical spacing)
                        const threshold = Math.max(maxNormalGapSeconds * 2, 5); // At least 5 seconds
                        if (gap > threshold) {
                            const gapKey = `${lastDisplayedTimestamp}|${log.timestamp}`;
                            currentGapKeys.add(gapKey);

                            // Check if this gap is dead (no logs in journal)
                            if (!deadGaps.has(gapKey)) {
                                const formattedGap = formatDuration(gap);
                                // Create gap marker
                                const markerDiv = document.createElement('div');
                                markerDiv.className = 'gap-marker';
                                markerDiv.style.userSelect = 'none';  // Prevent text selection cursor
                                markerDiv.textContent = `~~~~~~~~~~~~~~~~~~~~ [gap of ${formattedGap}] ~~~~~~~~~~~~~~~~~~~~~`;

                                // Store metadata - BOTH timestamps AND log IDs
                                markerDiv.dataset.olderTimestamp = lastDisplayedTimestamp;
                                markerDiv.dataset.newerTimestamp = log.timestamp;
                                markerDiv.dataset.gapKey = gapKey;
                                markerDiv.dataset.gapSeconds = Math.round(gap);
                                markerDiv.dataset.olderLogId = lastDisplayedLogId;
                                markerDiv.dataset.newerLogId = log.log_id;

                                // Helper to measure text width
                                function measureTextWidth(text, element) {
                                    const tempSpan = document.createElement('span');
                                    tempSpan.style.visibility = 'hidden';
                                    tempSpan.style.position = 'absolute';
                                    tempSpan.style.whiteSpace = 'nowrap';
                                    tempSpan.style.font = window.getComputedStyle(element).font;
                                    tempSpan.textContent = text;
                                    document.body.appendChild(tempSpan);
                                    const width = tempSpan.offsetWidth;
                                    document.body.removeChild(tempSpan);
                                    return width;
                                }

                                markerDiv.addEventListener('mousemove', (e) => {
                                    const rect = markerDiv.getBoundingClientRect();
                                    const clickX = e.clientX - rect.left;

                                    // Measure actual text width
                                    const fullTextWidth = measureTextWidth(markerDiv.textContent, markerDiv);

                                    if (clickX <= fullTextWidth) {
                                        // Find center text position
                                        const centerText = `[gap of ${formattedGap}]`;
                                        const centerTextWidth = measureTextWidth(centerText, markerDiv);
                                        const fullText = markerDiv.textContent;
                                        const leftSquiggles = fullText.indexOf(centerText);
                                        const leftSquigglesText = fullText.substring(0, leftSquiggles);
                                        const leftSquigglesWidth = measureTextWidth(leftSquigglesText, markerDiv);
                                        const centerStart = leftSquigglesWidth;
                                        const centerEnd = centerStart + centerTextWidth;

                                        if (clickX < centerStart) {
                                            markerDiv.style.cursor = UP_ARROW_CURSOR;
                                            markerDiv.title = `↑ Load older logs above this gap (${formattedGap})`;
                                        } else if (clickX > centerEnd) {
                                            markerDiv.style.cursor = DOWN_ARROW_CURSOR;
                                            markerDiv.title = `↓ Load newer logs below this gap (${formattedGap})`;
                                        } else {
                                            markerDiv.style.cursor = 'default';
                                            markerDiv.title = `Gap of ${formattedGap} - click ~ to fill`;
                                        }
                                    } else {
                                        markerDiv.style.cursor = 'default';
                                        markerDiv.title = `Gap of ${formattedGap} - click on the ~ marks to fill`;
                                    }
                                });

                                markerDiv.addEventListener('click', (e) => {
                                    const rect = markerDiv.getBoundingClientRect();
                                    const clickX = e.clientX - rect.left;

                                    const fullTextWidth = measureTextWidth(markerDiv.textContent, markerDiv);

                                    if (clickX <= fullTextWidth) {
                                        const centerText = `[gap of ${formattedGap}]`;
                                        const centerTextWidth = measureTextWidth(centerText, markerDiv);
                                        const fullText = markerDiv.textContent;
                                        const leftSquiggles = fullText.indexOf(centerText);
                                        const leftSquigglesText = fullText.substring(0, leftSquiggles);
                                        const leftSquigglesWidth = measureTextWidth(leftSquigglesText, markerDiv);
                                        const centerStart = leftSquigglesWidth;
                                        const centerEnd = centerStart + centerTextWidth;

                                        if (clickX < centerStart) {
                                            // UP arrow - fill from bottom edge upward
                                            e.stopPropagation();
                                            fillGapDirectional(markerDiv.dataset.newerTimestamp, 'older', markerDiv);
                                        } else if (clickX > centerEnd) {
                                            // DOWN arrow - fill from top edge downward
                                            e.stopPropagation();
                                            fillGapDirectional(markerDiv.dataset.olderTimestamp, 'newer', markerDiv);
                                        }
                                    }
                                });

                                markerDiv.addEventListener('mouseleave', () => {
                                    markerDiv.style.cursor = 'default';
                                });

                                debugWindow.appendChild(markerDiv);
                                logElements.push(markerDiv);
                                logTexts.push(markerDiv.textContent);
                                logTimestamps.push(lastDisplayedTimestamp + (gap / 2));
                                visibleCount++;
                            }
                            // If deadGaps.has(gapKey), skip marker entirely (logs will be adjacent)
                        }
                    }

                    // Render the log
                    const className = log.message.includes('[CTRL]') ? 'log-ctrl' : 'log-node';
                    const logDiv = document.createElement('div');
                    logDiv.className = className;
                    logDiv.setAttribute('data-timestamp', log.timestamp);
                    logDiv.setAttribute('data-log_id', log.log_id || '0');
                    const highlightedContent = applyHighlighting(log.message);
                    logDiv.innerHTML = highlightedContent;
                    logDiv.addEventListener('click', handleLogClick(logDiv));

                    debugWindow.appendChild(logDiv);
                    logElements.push(logDiv);
                    logTexts.push(log.message);
                    logTimestamps.push(log.timestamp);
                    visibleCount++;

                    lastDisplayedTimestamp = log.timestamp;
                    lastDisplayedLogId = log.log_id;  // Update log ID too
                }
            }

            // Prune deadGaps - remove keys that no longer exist in current display
            for (const deadKey of deadGaps) {
                if (!currentGapKeys.has(deadKey)) {
                    deadGaps.delete(deadKey);
                }
            }

            // Restore scroll position using the protected range if available
            if (protectedLogIds && protectedLogIds.size > 0) {
                // Find the first protected log in the new DOM
                let targetElement = null;
                for (const el of logElements) {
                    const logId = el.getAttribute('data-log_id');
                    if (logId && logId !== 'gap' && logId !== 'null') {
                        const id = parseInt(logId, 10);
                        if (!isNaN(id) && protectedLogIds.has(id)) {
                            targetElement = el;
                            break;
                        }
                    }
                }

                if (targetElement) {
                    // Scroll to this element (it's the earliest protected log)
                    targetElement.scrollIntoView({ block: 'start' });
                    // Brief highlight to show where we landed
                    targetElement.style.backgroundColor = '#ffff99';
                    setTimeout(() => {
                        if (targetElement) targetElement.style.backgroundColor = '';
                    }, 500);

                    // Restore cursor if this was a cursor
                    if (searchLogId !== null && protectedLogIds.has(searchLogId)) {
                        targetElement.classList.add('log-cursor');
                    }
                } else {
                    // Fallback: try to find by timestamp range
                    if (protectedEarliestTimestamp !== null) {
                        for (const el of logElements) {
                            const ts = parseFloat(el.getAttribute('data-timestamp'));
                            if (ts && !isNaN(ts) && Math.abs(ts - protectedEarliestTimestamp) < 0.1) {
                                targetElement = el;
                                break;
                            }
                        }
                        if (targetElement) {
                            targetElement.scrollIntoView({ block: 'start' });
                            targetElement.style.backgroundColor = '#ffff99';
                            setTimeout(() => {
                                if (targetElement) targetElement.style.backgroundColor = '';
                            }, 500);
                        }
                    }

                    // If still no target, fall back to ratio-based scroll
                    if (!targetElement) {
                        const ratio = 0.5;
                        const newScrollTop = ratio * debugWindow.scrollHeight;
                        const maxScroll = debugWindow.scrollHeight - debugWindow.clientHeight;
                        if (maxScroll > 0) {
                            debugWindow.scrollTop = Math.max(0, Math.min(newScrollTop, maxScroll));
                        }
                    }
                }
            } else if (shouldScrollToBottom) {
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

        // Rebuild entire debug display (used after bulk adds)
        function rebuildDebugDisplay() {
            queueDebugViewOperation(DebugViewOperation.REBUILD_DISPLAY);
        }

        // Don't call this one, call scrollToTimestamp()
        async function scrollToTimestampInternal(data) {
            const { timestamp, targetLogId, showToast = false, setCursor = false } = data;

            console.log(`[scrollToTimestampInternal] START: timestamp=${timestamp}, targetLogId=${targetLogId}, showToast=${showToast}, setCursor=${setCursor}`);

            if (!timestamp) {
                console.error('scrollToTimestampInternal: No timestamp provided');
                return;
            }

            if (showToast) {
                showTemporaryMessage(`🔍 Jumping to ${new Date(timestamp * 1000).toLocaleTimeString()}...`, 'search');
            }

            // Helper to set cursor if requested
            function setCursorIfRequested(element) {
                if (setCursor && element) {
                    const logId = parseInt(element.getAttribute('data-log_id'), 10);
                    if (!isNaN(logId)) {
                        searchLogId = logId;
                        searchTimestamp = timestamp;
                        updateCursorHighlight();
                        element.classList.add('log-cursor');
                    }
                }
            }

            // Helper to show completion toast if requested
            function showCompletionToast(message) {
                if (showToast) {
                    showTemporaryMessage(message, 'success');
                }
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
            console.log(`[scrollToTimestampInternal] closest in buffer: distance=${closestDistance}, index=${closestIndex}`);

            // If we have logs within 60 seconds and a target log_id, check if it exists
            if (closestIndex !== -1 && closestDistance < 60 && targetLogId) {
                let exactIndex = -1;
                for (let i = 0; i < logElements.length; i++) {
                    const rid = parseInt(logElements[i].getAttribute('data-log_id'));
                    if (rid === targetLogId) {
                        exactIndex = i;
                        break;
                    }
                }

                if (exactIndex !== -1) {
                    console.log(`[scrollToTimestampInternal] EXACT MATCH FOUND in buffer at index ${exactIndex}`);
                    const exactElement = logElements[exactIndex];
                    exactElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    exactElement.style.backgroundColor = '#4a4a4a';

                    setCursorIfRequested(exactElement);

                    setTimeout(() => {
                        if (exactElement) exactElement.style.backgroundColor = '';
                        if (showToast) {
                            showCompletionToast(`📍 Jumped to ${new Date(timestamp * 1000).toLocaleTimeString()}`);
                        }
                    }, 3000);
                    await new Promise(resolve => setTimeout(resolve, 500));
                    return;
                }
            }

            // If we have a close match in buffer (within 60 seconds) and no targetLogId
            if (closestIndex !== -1 && closestDistance < 60 && !targetLogId) {
                console.log(`[scrollToTimestampInternal] no targetLogId, scrolling to closest at index ${closestIndex}`);
                const centerElement = logElements[closestIndex];
                if (centerElement) {
                    centerElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    centerElement.style.backgroundColor = '#4a4a4a';

                    setCursorIfRequested(centerElement);

                    setTimeout(() => {
                        if (centerElement) centerElement.style.backgroundColor = '';
                        if (showToast) {
                            showCompletionToast(`📍 Jumped to ${new Date(timestamp * 1000).toLocaleTimeString()}`);
                        }
                    }, 3000);
                    await new Promise(resolve => setTimeout(resolve, 500));
                }
                return;
            }

            // Need to fetch from journal
            if (showToast) {
                showTemporaryMessage(`⏳ Loading logs near ${new Date(timestamp * 1000).toLocaleTimeString()}...`, 'search');
            }
            isLoading = true;

            try {
                const response = await fetch('/api/journal/query', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        timestamp: timestamp,
                        before: 500,
                        after: 500
                    })
                });
                const data = await response.json();

                if (data.status === 'ok' && data.logs && data.logs.length > 0) {
                    const newLogs = data.logs;
                    const filteredNewLogs = newLogs.filter(log => shouldDisplayLog(log.message));
                    const uniqueNewLogs = filterUniqueLogs(filteredNewLogs);

                    logBuffer.push(...uniqueNewLogs);
                    logBuffer.sort((a, b) => a.timestamp - b.timestamp);

                    rebuildDebugDisplayInternal();

                    // Find closest log to target timestamp
                    let bestIndex = -1;
                    let bestDistance = Infinity;
                    for (let i = 0; i < logElements.length; i++) {
                        const ts = parseFloat(logElements[i].getAttribute('data-timestamp'));
                        if (ts && !isNaN(ts)) {
                            const distance = Math.abs(ts - timestamp);
                            if (distance < bestDistance) {
                                bestDistance = distance;
                                bestIndex = i;
                            }
                        }
                    }

                    if (bestIndex !== -1) {
                        const bestElement = logElements[bestIndex];
                        bestElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                        bestElement.style.backgroundColor = '#4a4a4a';

                        setCursorIfRequested(bestElement);

                        setTimeout(() => {
                            if (bestElement) bestElement.style.backgroundColor = '';
                            if (showToast) {
                                const msg = targetLogId ?
                                    `📍 Jumped to event at ${new Date(timestamp * 1000).toLocaleTimeString()}` :
                                    `📍 Jumped to ${new Date(timestamp * 1000).toLocaleTimeString()}`;
                                showCompletionToast(msg);
                            }
                        }, 3000);

                        await new Promise(resolve => setTimeout(resolve, 500));
                    }

                    // If we have a target log_id, find exact match
                    if (targetLogId) {
                        for (let i = 0; i < logElements.length; i++) {
                            const rid = parseInt(logElements[i].getAttribute('data-log_id'));
                            if (rid === targetLogId) {
                                const exactElement = logElements[i];
                                exactElement.style.backgroundColor = '#4a4a4a';
                                exactElement.scrollIntoView({ block: 'center', behavior: 'smooth' });

                                setCursorIfRequested(exactElement);

                                setTimeout(() => {
                                    if (exactElement) exactElement.style.backgroundColor = '';
                                    if (showToast) {
                                        showCompletionToast(`📍 Jumped to exact event at ${new Date(timestamp * 1000).toLocaleTimeString()}`);
                                    }
                                }, 3000);
                                break;
                            }
                        }
                    }
                } else if (showToast) {
                    showTemporaryMessage(`No logs available for ${new Date(timestamp * 1000).toLocaleTimeString()}`, 'error');
                }
            } catch (e) {
                console.error('scrollToTimestampInternal: Error=', e);
                if (showToast) {
                    showTemporaryMessage(`❌ Failed to load logs: ${e.message}`, 'error');
                }
            } finally {
                isLoading = false;
            }
        }

        // Scroll to a specific timestamp
        function scrollToTimestamp(timestamp, targetLogId = null, options = {}) {
            const { showToast = false, setCursor = false } = options;
            console.log('scrollToTimestamp called with:', { timestamp, targetLogId, options });
            queueDebugViewOperation(DebugViewOperation.SCROLL_TO_TIMESTAMP, {
                timestamp,
                targetLogId,
                showToast,
                setCursor
            });
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

            scrollToTimestamp(timestamp, null, { showToast: false, setCursor: true });
        }

        async function scrollToMatch(timestamp, targetLogId = null, direction = null) {
            console.log('scrollToMatch called with:', { timestamp, targetLogId, direction, type: typeof targetLogId });

            let targetElement = null;

            if (targetLogId) {
                console.log(`Looking for log_id=${targetLogId} in logElements...`);
                for (let i = 0; i < logElements.length; i++) {
                    const rid = parseInt(logElements[i].getAttribute('data-log_id'));
                    if (rid === targetLogId) {
                        targetElement = logElements[i];
                        console.log(`✓ Found by log_id at index ${i}`);
                        break;
                    }
                }
                if (!targetElement) {
                    console.log(`✗ No element found with log_id=${targetLogId}`);
                }
            } else {
                console.log('No targetLogId provided, falling back to timestamp matching');
            }

            if (targetElement) {
                console.log('TARGET ELEMENT FOUND:');
                const foundTimestamp = parseFloat(targetElement.getAttribute('data-timestamp'));
                const foundLogId = parseInt(targetElement.getAttribute('data-log_id'), 10);

                if (!isNaN(foundTimestamp) && !isNaN(foundLogId)) {
                    searchTimestamp = foundTimestamp;
                    searchLogId = foundLogId;
                    if (direction) {
                        lastSearchDirection = direction;
                    }
                    updateCursorHighlight();
                    console.log('  Updated cursor to timestamp:', foundTimestamp, 'log_id:', foundLogId);
                } else if (timestamp) {
                    // Fallback to provided timestamp if parsing fails
                    searchTimestamp = timestamp;
                    if (direction) {
                        lastSearchDirection = direction;
                    }
                    updateCursorHighlight();
                    console.log('  Updated cursor to timestamp (fallback):', timestamp);
                }

                targetElement.scrollIntoView({ block: 'center' });
                targetElement.classList.add('log-cursor');
                targetElement.style.backgroundColor = '#ff9800';
                setTimeout(() => {
                    if (targetElement) targetElement.style.backgroundColor = '';
                }, 300);
                await new Promise(resolve => setTimeout(resolve, 500));
                console.log('Scroll animation complete');
            } else {
                console.log(`Element not found by log_id, calling scrollToTimestamp(${timestamp}, ${targetLogId})`);
                await scrollToTimestamp(timestamp, targetLogId, { setCursor: true });
            }

            setTimeout(() => {
                autoScrolling = false;
            }, 500);
        }

        function getVisibleTimestamp(edge) {
            // edge: 'oldest' (top of viewport) or 'newest' (bottom of viewport)
            const debugWindow = document.getElementById('debugWindow');
            if (!debugWindow || logElements.length === 0) {
                return edge === 'oldest' ? logTimestamps[0] : logTimestamps[logTimestamps.length - 1];
            }

            const viewportTop = debugWindow.scrollTop;
            const viewportBottom = viewportTop + debugWindow.clientHeight;

            if (edge === 'oldest') {
                // Find first displayed element in viewport
                for (let i = 0; i < logElements.length; i++) {
                    const el = logElements[i];
                    if (el.style.display === 'none') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.top >= viewportTop && rect.top <= viewportBottom) {
                        const ts = el.getAttribute('data-timestamp');
                        if (ts && ts !== 'gap') {
                            return parseFloat(ts);
                        }
                    }
                }
                // Fallback to first timestamp in buffer
                return logTimestamps[0];
            } else {
                // Find last displayed element in viewport
                for (let i = logElements.length - 1; i >= 0; i--) {
                    const el = logElements[i];
                    if (el.style.display === 'none') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.bottom <= viewportBottom && rect.bottom >= viewportTop) {
                        const ts = el.getAttribute('data-timestamp');
                        if (ts && ts !== 'gap') {
                            return parseFloat(ts);
                        }
                    }
                }
                // Fallback to last timestamp in buffer
                return logTimestamps[logTimestamps.length - 1];
            }
        }

        async function cancelSearch() {
            if (activeSearchSession) {
                try {
                    await fetch('/api/search', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            action: 'cancel',
                            session_id: activeSearchSession
                        })
                    });
                } catch (e) {
                    console.error('Cancel failed:', e);
                }
                activeSearchSession = null;
            }
        }

        // Reset cursor when:
        function resetSearch() {
            searchTimestamp = null;
            searchLogId = null;
            lastSearchDirection = null;
            autoScrolling = false;
            activeSearchSession = null;
            updateCursorHighlight();
        }

        function getCurrentSearchFilters() {
            return {
                exclude_ctrl: document.getElementById('filterExcludeCtrl')?.checked || false,
                include_nodes: (() => {
                    const input = document.getElementById('filterIncludeNodes');
                    if (!input || !input.value) return [];
                    const matches = input.value.match(/\\d+/g);
                    return matches || [];
                })(),
                min_log_level: parseInt(document.getElementById('filterMinLogLevel')?.value || '4', 10)
            };
        }

        function matchesSearchCriteria(message, searchTerm, caseSensitive, wholeWord) {
            let search = searchTerm;
            let target = message;

            if (!caseSensitive) {
                search = search.toLowerCase();
                target = target.toLowerCase();
            }

            if (wholeWord) {
                const pattern = new RegExp(`\\b${escapeRegex(search)}\\b`);
                return pattern.test(target);
            } else {
                return target.includes(search);
            }
        }

        function escapeRegex(string) {
            return string.replace(/[\\^$*+?.()|[\\]{}]/g, '\\\\$&');
        }

        function searchLocalBuffer(searchTerm, direction) {
            console.log('searchLocalBuffer: ========== START ==========');
            console.log('searchLocalBuffer: searchTerm:', searchTerm);
            console.log('searchLocalBuffer: caseSensitive:', searchCaseSensitive);
            console.log('searchLocalBuffer: wholeWord:', searchWholeWord);
            console.log('searchLocalBuffer: direction:', direction);
            console.log('searchLocalBuffer: searchLogId:', searchLogId);
            console.log('searchLocalBuffer: logBuffer length:', logBuffer.length);

            // Build matches array from logBuffer
            const matches = [];
            let coreDumpCount = 0;
            let coreDumpRejected = 0;

            for (let i = 0; i < logBuffer.length; i++) {
                const entry = logBuffer[i];

                const isCoreDump = entry.message.includes('CORE_DUMP');
                if (isCoreDump) {
                    coreDumpCount++;
                }

                const shouldDisplay = shouldDisplayLog(entry.message);
                const matchesCriteria = matchesSearchCriteria(entry.message, searchTerm, searchCaseSensitive, searchWholeWord);

                if (shouldDisplay && matchesCriteria) {
                    matches.push({
                        index: i,
                        log_id: entry.log_id,
                        timestamp: entry.timestamp,
                        message: entry.message
                    });
                } else if (isCoreDump) {
                    coreDumpRejected++;
                }
            }

            console.log('searchLocalBuffer: total matches found:', matches.length);

            if (matches.length === 0) {
                console.log('searchLocalBuffer: no matches, returning false');
                return { found: false };
            }

            // Sort matches by log_id
            matches.sort((a, b) => a.log_id - b.log_id);

            console.log('searchLocalBuffer: first 5 matches:');
            for (let i = 0; i < Math.min(5, matches.length); i++) {
                console.log(`  [${i}] log_id=${matches[i].log_id}, ts=${matches[i].timestamp}`);
            }
            console.log('searchLocalBuffer: last 5 matches:');
            for (let i = Math.max(0, matches.length - 5); i < matches.length; i++) {
                console.log(`  [${i}] log_id=${matches[i].log_id}, ts=${matches[i].timestamp}`);
            }

            // Find the closest match in the specified direction using LOG_ID
            let bestIndex = -1;
            if (direction === 'next') {
                for (let i = 0; i < matches.length; i++) {
                    if (searchLogId === null || matches[i].log_id > searchLogId) {
                        bestIndex = i;
                        console.log(`  found next match at index ${i}, log_id=${matches[i].log_id}`);
                        break;
                    }
                }
                if (bestIndex === -1) {
                    console.log('searchLocalBuffer: no next match, need more');
                    return { found: false, needMore: true };
                }
            } else {
                for (let i = matches.length - 1; i >= 0; i--) {
                    if (searchLogId === null || matches[i].log_id < searchLogId) {
                        bestIndex = i;
                        console.log(`  found prev match at index ${i}, log_id=${matches[i].log_id}`);
                        break;
                    }
                }
                if (bestIndex === -1) {
                    console.log('searchLocalBuffer: no prev match, need more');
                    return { found: false, needMore: true };
                }
            }

            const match = matches[bestIndex];

            // IMPORTANT: Check if there's a gap marker between the current position and this match
            // If there is a gap, we need to trigger a database search instead
            if (searchLogId !== null) {
                const hasGap = checkForGapBetween(searchLogId, match.log_id);
                if (hasGap) {
                    console.log('searchLocalBuffer: gap detected between current position and match, triggering database search');
                    // Return needMore so the database search is triggered
                    return { found: false, needMore: true };
                }
            }

            console.log('searchLocalBuffer: selected match:', { log_id: match.log_id, timestamp: match.timestamp });

            const element = findLogElementByLogId(match.log_id);
            console.log('searchLocalBuffer: element found by log_id:', !!element);

            if (!element) {
                console.log('searchLocalBuffer: element not found, need more');
                return { found: false, needMore: true };
            }

            console.log('searchLocalBuffer: ========== SUCCESS ==========');
            return {
                found: true,
                element: element,
                log_id: match.log_id,
                timestamp: match.timestamp,
                wrapped: false,
                matchNumber: bestIndex + 1,
                totalMatches: matches.length
            };
        }

        // Helper function to check if there's a gap marker between two log IDs
        function checkForGapBetween(fromLogId, toLogId) {
            console.log(`checkForGapBetween: from=${fromLogId}, to=${toLogId}`);
            let foundGap = false;
            let gapCount = 0;

            // Iterate through logElements looking for gap markers
            for (let i = 0; i < logElements.length; i++) {
                const el = logElements[i];
                if (el.classList && el.classList.contains('gap-marker')) {
                    const olderLogId = parseInt(el.dataset.olderLogId, 10);
                    const newerLogId = parseInt(el.dataset.newerLogId, 10);

                    if (!isNaN(olderLogId) && !isNaN(newerLogId)) {
                        gapCount++;
                        // Check if this gap lies between fromLogId and toLogId
                        if (fromLogId < toLogId) {
                            // Searching forward: gap should be after fromLogId and before toLogId
                            if (olderLogId > fromLogId && newerLogId <= toLogId) {
                                console.log(`  -> GAP FOUND! between ${fromLogId} and ${toLogId} (${olderLogId}->${newerLogId})`);
                                foundGap = true;
                                break;
                            }
                        } else {
                            // Searching backward: gap should be before fromLogId and after toLogId
                            if (olderLogId >= toLogId && newerLogId < fromLogId) {
                                console.log(`  -> GAP FOUND! between ${fromLogId} and ${toLogId} (${olderLogId}->${newerLogId})`);
                                foundGap = true;
                                break;
                            }
                        }
                    }
                }
            }
            console.log(`checkForGapBetween: checked ${gapCount} gap markers, returning ${foundGap}`);
            return foundGap;
        }

        async function performSearch(direction, forceReset = false) {
            const searchInput = document.getElementById('searchInput');
            const caseBtn = document.getElementById('searchCaseBtn');
            const wordBtn = document.getElementById('searchWordBtn');
            const prevBtn = document.getElementById('searchPrevBtn');
            const nextBtn = document.getElementById('searchNextBtn');
            const cancelBtn = document.getElementById('searchCancelBtn');

            console.log(`[DEBUG] performSearch START: direction=${direction}, forceReset=${forceReset}, isSearching=${isSearching}`);

            const searchTerm = searchInput.value.trim();

            if (!searchTerm) {
                showTemporaryMessage('⚠️ Enter search term', 'error');
                return;
            }

            // Cancel any ongoing background journal queries
            if (activeFetchController) {
                console.log('[DEBUG] performSearch: aborting activeFetchController');
                activeFetchController.abort();
                activeFetchController = null;
            }

            // Try local search first
            if (!forceReset && !isSearching && logBuffer.length > 0) {
                const localResult = searchLocalBuffer(searchTerm, direction);
                if (localResult.found) {
                    // Update cursor state
                    searchLogId = localResult.log_id;
                    searchTimestamp = localResult.timestamp;
                    lastSearchDirection = direction;
                    updateCursorHighlight();

                    // Scroll to match
                    localResult.element.scrollIntoView({ block: 'center', behavior: 'smooth' });
                    localResult.element.classList.add('log-cursor');
                    localResult.element.style.backgroundColor = '#ff9800';
                    setTimeout(() => {
                        if (localResult.element) localResult.element.style.backgroundColor = '';
                    }, 300);

                    showTemporaryMessage(`${localResult.matchNumber}/${localResult.totalMatches}`, 'search');
                    return;
                }
                if (localResult.needMore) {
                    showTemporaryMessage('🔍 Reached end of loaded logs, searching database...', 'search');
                }
            }

            if (isSearching) {
                showTemporaryMessage('⏳ Search already in progress...', 'search');
                return;
            }

            isSearching = true;
            currentSearchAborted = false;

            // Disable inputs during search
            searchInput.disabled = true;
            caseBtn.disabled = true;
            wordBtn.disabled = true;
            prevBtn.disabled = true;
            nextBtn.disabled = true;

            // Determine starting log_id for database search
            let fromLogId = null;
            if (!forceReset && searchLogId !== null && !autoScrolling) {
                fromLogId = searchLogId;
                if (direction === 'next') {
                    fromLogId += 1;
                } else {
                    fromLogId -= 1;
                }
            }

            const progressSpan = document.getElementById('searchProgress');

            try {
                const filters = getCurrentSearchFilters();

                // Start database search using LOG_ID
                const startResponse = await fetch('/api/search', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        action: 'start',
                        search: searchTerm,
                        direction: direction,
                        from_log_id: fromLogId,        // Primary: Log ID positioning
                        case_sensitive: searchCaseSensitive,
                        whole_word: searchWholeWord,
                        exclude_ctrl: filters.exclude_ctrl,
                        include_nodes: filters.include_nodes,
                        min_log_level: filters.min_log_level
                    })
                });

                const startData = await startResponse.json();

                if (startData.error) {
                    showTemporaryMessage(`❌ ${startData.error}`, 'error');
                    return;
                }

                if (startData.status === 'too_many') {
                    showTemporaryMessage(`❌ ${startData.message}`, 'error');
                    return;
                }

                if (startData.found === false && startData.finished === true) {
                    showTemporaryMessage(`❌ No matches found for "${searchTerm}"`, 'search');
                    return;
                }

                if (startData.found === true && startData.session_id) {
                    console.log('DATABASE MATCH:');
                    console.log('  log_id:', startData.log_id);
                    console.log('  epoch_time (db):', startData.epoch_time);

                    // Check if already in buffer
                    const existingElement = findLogElementByLogId(startData.log_id);

                    if (existingElement) {
                        // Already in buffer - just scroll to it
                        console.log('  Match already in buffer, scrolling directly');
                        searchLogId = startData.log_id;
                        searchTimestamp = parseFloat(existingElement.getAttribute('data-timestamp'));
                        lastSearchDirection = direction;
                        updateCursorHighlight();

                        existingElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                        existingElement.classList.add('log-cursor');
                        existingElement.style.backgroundColor = '#ff9800';
                        setTimeout(() => {
                            if (existingElement) existingElement.style.backgroundColor = '';
                        }, 300);

                        if (startData.wrapped) {
                            const wrapMsg = direction === 'next' ? '↺ Wrapped to earliest logs' : '↺ Wrapped to latest logs';
                            showTemporaryMessage(wrapMsg, 'search');
                        } else {
                            const shortMsg = startData.message.length > 80 ? startData.message.substring(0, 77) + '...' : startData.message;
                            showTemporaryMessage(`✅ Found: ${shortMsg}`, 'search');
                        }

                        activeSearchSession = startData.session_id;
                        return;
                    }

                    // Not in buffer - need to fetch from journal using epoch_time
                    console.log('  Match not in buffer, fetching from journal');
                    const fetchTimestamp = startData.epoch_time;  // Use database epoch_time for journal seek

                    // Show progress while fetching
                    if (progressSpan) {
                        progressSpan.innerHTML = '<span class="search-spinner">⏳</span>';
                    }

                    // Fetch surrounding logs from journal
                    await fetchAndScrollToTimestamp(fetchTimestamp, startData.log_id, direction);

                    searchLogId = startData.log_id;
                    searchTimestamp = fetchTimestamp;
                    lastSearchDirection = direction;
                    updateCursorHighlight();

                    if (startData.wrapped) {
                        const wrapMsg = direction === 'next' ? '↺ Wrapped to earliest logs' : '↺ Wrapped to latest logs';
                        showTemporaryMessage(wrapMsg, 'search');
                    } else {
                        const shortMsg = startData.message.length > 80 ? startData.message.substring(0, 77) + '...' : startData.message;
                        showTemporaryMessage(`✅ Found: ${shortMsg}`, 'search');
                    }

                    if (progressSpan) {
                        progressSpan.innerHTML = '';
                    }

                    activeSearchSession = startData.session_id;
                    return;
                }

                // Polling for results (fallback for slower searches)
                const session_id = startData.session_id;
                activeSearchSession = session_id;

                let found = false;
                let finished = false;
                let pollCount = 0;
                const maxPolls = 600;  // 5 minutes max (500ms * 600 = 300 seconds)

                if (progressSpan) {
                    progressSpan.innerHTML = '<span class="search-spinner">⏳</span>';
                }

                while (!found && !finished && pollCount < maxPolls && !currentSearchAborted) {
                    await new Promise(resolve => setTimeout(resolve, 500));
                    pollCount++;

                    if (currentSearchAborted) break;

                    const pollResponse = await fetch('/api/search', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            action: 'poll',
                            session_id: session_id,
                            direction: direction
                        })
                    });

                    const pollData = await pollResponse.json();

                    if (pollData.error) {
                        console.error('Poll error:', pollData.error);
                        showTemporaryMessage(`❌ Search error: ${pollData.error}`, 'error');
                        break;
                    }

                    if (pollData.cancelled) {
                        showTemporaryMessage('Search cancelled', 'search');
                        break;
                    }

                    if (pollData.found) {
                        found = true;
                        console.log('POLL MATCH FOUND:');
                        console.log('  log_id:', pollData.log_id);
                        console.log('  epoch_time:', pollData.epoch_time);

                        // Check if already in buffer
                        const existingElement = findLogElementByLogId(pollData.log_id);

                        if (existingElement) {
                            // Already in buffer - just scroll to it
                            console.log('  Match already in buffer, scrolling directly');
                            searchLogId = pollData.log_id;
                            searchTimestamp = parseFloat(existingElement.getAttribute('data-timestamp'));
                            lastSearchDirection = direction;
                            updateCursorHighlight();

                            existingElement.scrollIntoView({ block: 'center', behavior: 'smooth' });
                            existingElement.classList.add('log-cursor');
                            existingElement.style.backgroundColor = '#ff9800';
                            setTimeout(() => {
                                if (existingElement) existingElement.style.backgroundColor = '';
                            }, 300);

                            if (pollData.wrapped) {
                                const wrapMsg = direction === 'next' ? '↺ Wrapped to earliest logs' : '↺ Wrapped to latest logs';
                                showTemporaryMessage(wrapMsg, 'search');
                            } else {
                                const shortMsg = pollData.message.length > 80 ? pollData.message.substring(0, 77) + '...' : pollData.message;
                                showTemporaryMessage(`✅ Found: ${shortMsg}`, 'search');
                            }
                            break;
                        }

                        // Not in buffer - fetch from journal
                        const fetchTimestamp = pollData.epoch_time;
                        await fetchAndScrollToTimestamp(fetchTimestamp, pollData.log_id, direction);

                        searchLogId = pollData.log_id;
                        searchTimestamp = fetchTimestamp;
                        lastSearchDirection = direction;
                        updateCursorHighlight();

                        if (pollData.wrapped) {
                            const wrapMsg = direction === 'next' ? '↺ Wrapped to earliest logs' : '↺ Wrapped to latest logs';
                            showTemporaryMessage(wrapMsg, 'search');
                        } else {
                            const shortMsg = pollData.message.length > 80 ? pollData.message.substring(0, 77) + '...' : pollData.message;
                            showTemporaryMessage(`✅ Found: ${shortMsg}`, 'search');
                        }
                        break;
                    }

                    if (pollData.finished) {
                        finished = true;
                        break;
                    }

                    // Update progress indicator
                    if (progressSpan && pollData.current_index !== undefined && pollData.total_matches) {
                        progressSpan.innerHTML = `${pollData.current_index + 1}/${pollData.total_matches}`;
                    }
                }

                if (progressSpan) {
                    progressSpan.innerHTML = '';
                }

                if (!found && !finished && !currentSearchAborted) {
                    showTemporaryMessage(`❌ Search timeout`, 'search');
                } else if (!found && finished && !currentSearchAborted) {
                    showTemporaryMessage(`❌ No more matches for "${searchTerm}"`, 'search');
                }

            } catch (e) {
                console.error('Search error:', e);
                showTemporaryMessage(`❌ Search failed: ${e.message}`, 'error');
            } finally {
                isSearching = false;
                currentSearchAborted = false;
                searchInput.disabled = false;
                caseBtn.disabled = false;
                wordBtn.disabled = false;
                prevBtn.disabled = false;
                nextBtn.disabled = false;
                cancelBtn.disabled = false;
            }
        }

        function clearSearch() {
            activeSearchSession = null;
            const searchInput = document.getElementById('searchInput');
            if (searchInput) {
                searchInput.value = '';
                searchInput.placeholder = '🔍';
            }
        }

        function updateCursorHighlight() {
            // Remove cursor class from all logs
            document.querySelectorAll('#debugWindow .log-ctrl, #debugWindow .log-node').forEach(el => {
                el.classList.remove('log-cursor');
            });

            // Find and highlight the log with matching log_id
            if (searchLogId !== null) {
                for (let i = 0; i < logElements.length; i++) {
                    const logId = parseInt(logElements[i].getAttribute('data-log_id'), 10);
                    if (logId === searchLogId) {
                        logElements[i].classList.add('log-cursor');
                        break;
                    }
                }
            }
        }

        function initSearchUI() {
            const debugHeader = document.querySelector('.debug-header');
            const debugButtons = document.querySelector('.debug-buttons');

            if (!debugHeader || !debugButtons) return;

            // Check if search group already exists
            if (document.querySelector('.search-group')) return;

            // Create search group
            const searchGroup = document.createElement('div');
            searchGroup.className = 'search-group';
            searchGroup.style.cssText = 'display: inline-flex; align-items: center; gap: 3px; margin: 0 4px;';

            searchGroup.innerHTML = `
                <input type="text" id="searchInput" placeholder="🔍"
                    style="width: 70px; font-size: 10px; padding: 2px 4px; border: 1px solid #ccc; border-radius: 3px;"
                    title="F3: next, Shift+F3: previous">
                <button id="searchCaseBtn" class="debug-toggle-btn" style="padding: 2px 4px; font-size: 9px; background: #6c757d;">Aa</button>
                <button id="searchWordBtn" class="debug-toggle-btn" style="padding: 2px 4px; font-size: 9px; background: #6c757d;">ab</button>
                <button id="searchPrevBtn" class="debug-toggle-btn" style="padding: 2px 6px; font-size: 12px;">▲</button>
                <button id="searchNextBtn" class="debug-toggle-btn" style="padding: 2px 6px; font-size: 12px;">▼</button>
                <button id="searchCancelBtn" class="debug-toggle-btn" style="padding: 2px 4px; font-size: 10px;">✕</button>
                <span id="searchProgress" style="font-size: 9px; color: #495057; width: 40px; margin-left: 2px; font-family: monospace; display: inline-block; text-align: right;"></span>
                <button id="jumpToCursorBtn" class="debug-toggle-btn" style="padding: 2px 4px; font-size: 10px;" title="Scroll to cursor (last found match)">🎯</button>
                <button id="cursorClearBtn" class="debug-toggle-btn" style="padding: 2px 4px; font-size: 10px;" title="Clear cursor">↺</button>
            `;

            // Insert before debug-buttons
            debugHeader.insertBefore(searchGroup, debugButtons);

            const searchInput = document.getElementById('searchInput');
            const caseBtn = document.getElementById('searchCaseBtn');
            const wordBtn = document.getElementById('searchWordBtn');
            const prevBtn = document.getElementById('searchPrevBtn');
            const nextBtn = document.getElementById('searchNextBtn');
            const cancelBtn = document.getElementById('searchCancelBtn');
            const jumpToCursorBtn = document.getElementById('jumpToCursorBtn');
            const cursorClearBtn = document.getElementById('cursorClearBtn');

            if (!searchInput) return;

            // Track base direction (set by up/down buttons)
            let baseDirection = 'prev';  // Default to 'prev' (up arrow)

            // Ctrl+F handler
            document.addEventListener('keydown', (e) => {
                if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
                    e.preventDefault();
                    searchInput.focus();
                    searchInput.select();
                }
                // Escape to clear search
                if (e.key === 'Escape' && document.activeElement === searchInput) {
                    cancelSearch();
                    clearSearch();
                    searchInput.blur();
                    showTemporaryMessage('Search cancelled', 'search');
                    // Re-enable buttons
                    searchInput.disabled = false;
                    cancelBtn.disabled = false;
                    wordBtn.disabled = false;
                    prevBtn.disabled = false;
                    nextBtn.disabled = false;
                }
            });

            // Expand on focus, shrink on blur
            searchInput.addEventListener('focus', () => {
                searchInput.style.width = '100px';
                searchInput.placeholder = 'Search...';
                searchInput.select();
            });

            searchInput.addEventListener('blur', () => {
                if (!searchInput.value) {
                    searchInput.style.width = '70px';
                    searchInput.placeholder = '🔍';
                }
            });

            // Enter key triggers search forward (next direction)
            searchInput.addEventListener('keypress', (e) => {
                if (e.key === 'Enter') {
                    resetSearch();  // Reset on new search
                    baseDirection = 'next';  // Enter defaults to next direction
                    performSearch('next');
                }
            });

            caseBtn.addEventListener('click', () => {
                resetSearch();
                searchCaseSensitive = !searchCaseSensitive;
                caseBtn.style.background = searchCaseSensitive ? '#28a745' : '#6c757d';
                showTemporaryMessage(`Case ${searchCaseSensitive ? 'ON' : 'OFF'}`, 'search');
            });

            wordBtn.addEventListener('click', () => {
                resetSearch();
                searchWholeWord = !searchWholeWord;
                wordBtn.style.background = searchWholeWord ? '#28a745' : '#6c757d';
                showTemporaryMessage(`Whole word ${searchWholeWord ? 'ON' : 'OFF'}`, 'search');
            });

            // Navigation buttons - update baseDirection
            prevBtn.addEventListener('click', () => {
                baseDirection = 'prev';
                performSearch('prev');
            });

            nextBtn.addEventListener('click', () => {
                baseDirection = 'next';
                performSearch('next');
            });

            // Cancel button - cancels search and returns buttons to normal
            cancelBtn.addEventListener('click', async () => {
                // Cancel the backend search session
                if (activeSearchSession) {
                    try {
                        const response = await fetch('/api/search', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({
                                action: 'cancel',
                                session_id: activeSearchSession
                            })
                        });
                    } catch (e) {
                        console.error('Cancel failed:', e);
                    }
                    activeSearchSession = null;
                }

                if (isSearching) {
                    currentSearchAborted = true;
                }

                // Reset search state BUT KEEP CURSOR
                isSearching = false;
                // Re-enable buttons
                searchInput.disabled = false;
                caseBtn.disabled = false;
                wordBtn.disabled = false;
                prevBtn.disabled = false;
                nextBtn.disabled = false;

                // Keep the search term and cursor
                searchInput.blur();

                showTemporaryMessage('Search cancelled', 'search');
            });

            // Jump to cursor button
            jumpToCursorBtn.addEventListener('click', () => {
                if (searchTimestamp !== null) {
                    scrollToTimestamp(searchTimestamp);
                    showTemporaryMessage(`Jumped to cursor at ${new Date(searchTimestamp * 1000).toLocaleTimeString()}`, 'search');
                } else {
                    showTemporaryMessage('No cursor set', 'info');
                }
            });

            // Cursor clear button
            cursorClearBtn.addEventListener('click', () => {
                searchTimestamp = null;
                lastSearchDirection = null;
                updateCursorHighlight();
                showTemporaryMessage('Cursor cleared - next search will start from viewport', 'search');
            });

            // F3 and Shift+F3 keyboard shortcuts for search navigation
            document.addEventListener('keydown', (e) => {
                const isF3 = (e.key === 'F3' || e.keyCode === 114);

                if (isF3 && searchInput && searchInput.value.trim()) {
                    e.preventDefault();
                    let direction;

                    if (e.shiftKey) {
                        // Shift+F3: opposite of base direction
                        direction = baseDirection === 'next' ? 'prev' : 'next';
                    } else {
                        // F3: same as base direction
                        direction = baseDirection;
                    }

                    performSearch(direction, false);
                }
            });
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

        function appendLogsFiltered(newLogs) {
            if (!newLogs || newLogs.length === 0) return;

            for (const log of newLogs) {
                addNewLog(log);
            }
        }

        // Don't call this, call clearLogs()
        function clearLogsInternal() {
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

        // Override clearLogs to reset buffer properly
        function clearLogs() {
            queueDebugViewOperation(DebugViewOperation.CLEAR_LOGS);
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
            initSearchUI();
            startIntelligentTrimmer();
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
                    // Start auto-refresh when switching to graph view
                    if (graphAutoRefreshEnabled) {
                        graphStartAutoRefresh();
                    }
                }
                updateViewSelector('graphs');
                // Change footer text for graph view
                if (footer) {
                    footer.innerHTML = '📈 Graph View - Double-click any graph to expand | Select nodes with Ctrl/Cmd+Click | Auto-refresh updates every 30 seconds';
                }
            } else {
                if (gridWrapper) gridWrapper.style.display = 'block';
                if (graphContainer) graphContainer.style.display = 'none';
                updateViewSelector('grid');
                // Stop auto-refresh when leaving graph view
                graphStopAutoRefresh();
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
                    graphGrid.innerHTML = `<div style="grid-column:1/-1;text-align:center;padding:40px;">📊 Loading data... (can take a while)</div>`;
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
        async function showMinuteDrillDown(graphKey, timestamp, selectedNodes, graphStartTime) {
            const modal = document.getElementById('minuteModal');
            const title = document.getElementById('minuteModalTitle');
            const body = document.getElementById('minuteModalBody');

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
                        nodes: selectedNodes,
                        graph_start_time: graphStartTime
                    })
                });

                const data = await response.json();

                if (data.error) {
                    body.innerHTML = `<div class="minute-no-data">Error: ${data.error}</div>`;
                    return;
                }

                currentDrillDownData = data;

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
                    const baseline = deltaInfo.baseline_value;
                    const baselineTimestamp = deltaInfo.baseline_timestamp;

                    let eventsHtml = '';
                    let prevValue = null;
                    let eventCount = 0;

                    if (baseline !== null && baseline !== undefined) {
                        const baselineDate = new Date(baselineTimestamp);
                        const baselineTimeStr = baselineDate.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                        eventsHtml += `
                            <tr style="background: #f0f0f0; font-style: italic;">
                                <td colspan="2"><strong>Baseline (before this hour)</strong></td>
                                <td><strong>${baseline}</strong></td>
                                <td>—</td>
                            </tr>
                            <tr><td colspan="4" style="padding: 0;"><hr style="margin: 4px 0;"></td></tr>
                        `;
                    }

                    for (let i = 0; i < points.length; i++) {
                        const point = points[i];
                        const time = new Date(point[0]);
                        const timeStr = time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
                        const value = point[1];
                        let delta = '';
                        let isEvent = false;

                        if (prevValue !== null && value > prevValue) {
                            // Normal increase - this is a new failure/event
                            delta = `+${value - prevValue}`;
                            isEvent = true;
                            eventCount++;
                        } else if (prevValue !== null && value < prevValue) {
                            // Reset detected - counter decreased
                            // This is NOT a new failure, so we don't count it as an event
                            // Optionally show a reset indicator, but don't make it clickable
                            delta = `↺ ${value} (reset)`;
                            isEvent = false;  // NOT counted as a failure event
                            // eventCount NOT incremented
                        } else if (prevValue === null && baseline !== null && baseline !== undefined) {
                            // First reading in this hour - compare to baseline
                            const diff = value - baseline;
                            if (diff > 0) {
                                // Increase from baseline - new failures occurred
                                delta = `+${diff}`;
                                isEvent = true;
                                eventCount++;
                            } else if (diff < 0) {
                                // Decrease from baseline - reset occurred
                                // This is NOT a new failure, so we don't count it as an event
                                delta = `↺ ${value} (reset)`;
                                isEvent = false;  // NOT counted as a failure event
                                // eventCount NOT incremented
                            }
                            // If diff === 0, no change, ignore completely
                        }

                        if (isEvent) {
                            const pointTimestamp = point[0] / 1000;
                            eventsHtml += `
                                <tr class="clickable-row" data-timestamp="${pointTimestamp}" style="cursor: pointer;">
                                    <td style="white-space: nowrap;">${timeStr}</td>
                                    <td>${value}</td>
                                    <td>${delta}</td>
                                </tr>
                            `;
                        } else if (delta && !isEvent) {
                            // Show reset indicators but don't make them clickable
                            eventsHtml += `
                                <tr style="opacity: 0.6; font-style: italic;">
                                    <td style="white-space: nowrap;">${timeStr}</td>
                                    <td>${value}</td>
                                    <td>${delta}</td>
                                </tr>
                            `;
                        }

                        prevValue = value;
                    }

                    // Only show the node section if there were actual events (increases)
                    if (eventCount > 0) {
                        html += `
                            <div class="minute-node-section">
                                <div class="minute-node-header" onclick="toggleMinuteNode(this)">
                                    <span class="minute-node-name">${nodeName} (${nodeIp})</span>
                                    <span class="minute-node-stats">
                                        📊 ${eventCount} events this hour
                                        ${baseline ? ` (baseline: ${baseline})` : ''}
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

                    document.querySelectorAll('.clickable-row').forEach(row => {
                        row.addEventListener('click', (e) => {
                            e.stopPropagation();
                            const timestamp = parseFloat(row.getAttribute('data-timestamp'));
                            if (timestamp) {
                                modal.style.display = 'none';
                                scrollToTimestamp(timestamp, null, { showToast: true, setCursor: true });
                            }
                        });
                    });
                }

                const copyBtn = document.getElementById('minuteModalCopyBtn');
                if (copyBtn) {
                    copyBtn.onclick = null;
                    copyBtn.onclick = copyMinuteDataToClipboard;
                }

                const csvBtn = document.getElementById('minuteModalCsvBtn');
                if (csvBtn) {
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

        function copyMinuteDataToClipboard() {
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
                const deltaInfo = data.delta_summary?.[nodeIp] || {};
                const baseline = deltaInfo.baseline_value;
                const baselineTimestamp = deltaInfo.baseline_timestamp;

                let hasAnyChanges = false;

                // Check if there's a baseline that differs from the first point
                if (baseline !== null && baseline !== undefined && points.length > 0) {
                    if (points[0][1] !== baseline) {
                        hasAnyChanges = true;
                    }
                }

                // Also check for changes between consecutive points
                if (!hasAnyChanges) {
                    let prevValue = null;
                    for (const point of points) {
                        const value = point[1];
                        if (prevValue !== null && value !== prevValue) {
                            hasAnyChanges = true;
                            break;
                        }
                        prevValue = value;
                    }
                }

                if (hasAnyChanges) {
                    // Add baseline row if it exists
                    if (baseline !== null && baseline !== undefined && baselineTimestamp) {
                        const baselineDate = new Date(baselineTimestamp);
                        const baselineIsoString = baselineDate.toISOString();
                        rows.push(`${nodeIp}\t${nodeName}\t${baselineIsoString}\t${baseline}\t(baseline)`);
                    }

                    // Add data points with deltas
                    let prevValue = baseline;
                    for (const point of points) {
                        const timestamp = new Date(point[0]).toISOString();
                        const value = point[1];
                        let change = '';
                        let includeRow = false;

                        if (prevValue !== null && prevValue !== undefined) {
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

            if (rows.length <= 4) {
                rows.push('No events detected in this hour bucket');
            }

            const text = rows.join(String.fromCharCode(10));

            // Fallback method for non-secure contexts
            const textarea = document.createElement('textarea');
            textarea.value = text;
            document.body.appendChild(textarea);
            textarea.select();
            document.execCommand('copy');
            document.body.removeChild(textarea);

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
            const cancelBtn = document.querySelector('.minute-modal-close');

            if (cancelBtn) {
                cancelBtn.onclick = function() {
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

        function setupGraphCategoryClickHandler(graphKey, chart) {
            chart.off('click');
            chart.on('click', function(params) {
                if (params.dataIndex !== undefined && this.timestamps && this.timestamps[params.dataIndex]) {
                    const timestamp = this.timestamps[params.dataIndex];

                    // Flash the bar briefly
                    this.dispatchAction({
                        type: 'showTip',
                        seriesIndex: params.seriesIndex,
                        dataIndex: params.dataIndex
                    });
                    setTimeout(() => {
                        this.dispatchAction({ type: 'hideTip' });
                    }, 800);

                    const nodeFilter = document.getElementById('graphNodeFilter');
                    const selectedNodes = nodeFilter ? Array.from(nodeFilter.selectedOptions).map(opt => opt.value) : ['all'];

                    showMinuteDrillDown(graphKey, timestamp, selectedNodes, this.graphStartTime);
                }
            });
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
                    scrollToTimestamp(timestampSeconds, null, { showToast: true, setCursor: true });

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

                    // Store timestamps and start time for drill-down (needed for category axis)
                    chart.timestamps = graphTimestamps;
                    chart.graphStartTime = requestedStart / 1000;

                    graphCharts[graphKey] = chart;

                    if (config.isEvent) {
                        // Category chart (Wifi failure, disconnects, panics) drill-down click handler
                        setupGraphCategoryClickHandler(graphKey, chart);
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
                const lastOctetA = parseInt(a[0].split('.').pop(), 10);
                const lastOctetB = parseInt(b[0].split('.').pop(), 10);
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
                chart.graphStartTime = requestedStart / 1000;

                // Re-attach click handler based on chart type
                if (config.isEvent) {
                    // Category chart (Wifi failure, disconnects, panics) drill-down click handler
                    setupGraphCategoryClickHandler(graphKey, chart);
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
                chart.graphStartTime = requestedStart / 1000;

                // Re-attach click handler based on chart type
                if (config.isEvent) {
                    // Category chart (Wifi failure, disconnects, panics) drill-down click handler
                    setupGraphCategoryClickHandler(graphKey, chart);
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

        // Modify the auto-refresh button creation in setupGraphEventListeners()
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

            // Add auto-refresh button
            const nodeFilter = document.getElementById('graphNodeFilter');
            if (nodeFilter) {
                const parent = nodeFilter.parentNode;
                if (parent) {
                    // Check if auto-refresh button already exists
                    let autoRefreshBtn = document.getElementById('graphAutoRefreshBtn');
                    if (!autoRefreshBtn) {
                        autoRefreshBtn = document.createElement('button');
                        autoRefreshBtn.id = 'graphAutoRefreshBtn';
                        // Fix 1: Add white-space: nowrap to prevent text wrapping
                        // Fix 2: Add cursor: pointer to show the "pushy finger" on hover
                        autoRefreshBtn.style.cssText = 'padding: 4px 8px; font-size: 10px; margin-left: 4px; white-space: nowrap; cursor: pointer;';
                        autoRefreshBtn.textContent = '🔄 Auto On';
                        autoRefreshBtn.title = 'Toggle auto-refresh (default: ON)';
                        parent.appendChild(autoRefreshBtn);
                    }

                    autoRefreshBtn.onclick = () => {
                        graphAutoRefreshEnabled = !graphAutoRefreshEnabled;
                        autoRefreshBtn.textContent = graphAutoRefreshEnabled ? '🔄 Auto On' : '🔄 Auto Off';
                        autoRefreshBtn.style.background = graphAutoRefreshEnabled ? '#28a745' : '#dc3545';
                        if (graphAutoRefreshEnabled) {
                            graphStartAutoRefresh();
                        } else {
                            graphStopAutoRefresh();
                        }
                    };

                    // Set initial state
                    autoRefreshBtn.style.background = '#28a745';
                }
            }

            // Node filter change handler
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

        function graphStartAutoRefresh() {
            graphStopAutoRefresh(); // Clear any existing interval
            if (graphAutoRefreshEnabled) {
                graphAutoRefreshInterval = setInterval(() => {
                    // Only refresh if graph view is visible and auto-refresh is enabled
                    const graphContainer = document.getElementById('graphViewContainer');
                    if (graphContainer && graphContainer.style.display !== 'none' && graphAutoRefreshEnabled) {
                        // Clear cache and reload
                        const now = Math.floor(Date.now() / 1000);
                        const startTime = now - (currentTimeRange.hours * 3600);
                        const cacheKey = `${currentTimeRange.hours}_${Math.floor(startTime / 60)}`;
                        delete simpleCache[cacheKey];
                        loadGraphs();
                        // Visual feedback on refresh
                        const refreshBtn = document.getElementById('refreshGraphsBtn');
                        if (refreshBtn) {
                            refreshBtn.style.background = '#28a745';
                            setTimeout(() => {
                                refreshBtn.style.background = '';
                            }, 200);
                        }
                    }
                }, GRAPH_AUTO_REFRESH_DELAY);
            }
        }

        function graphStopAutoRefresh() {
            if (graphAutoRefreshInterval) {
                clearInterval(graphAutoRefreshInterval);
                graphAutoRefreshInterval = null;
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
                const hours = parseInt(selector.value, 10);
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
                    // Start auto-refresh after graphs are loaded
                    graphStartAutoRefresh();
                };
                document.head.appendChild(script);
            } else {
                console.log('ECharts already loaded');
                loadGraphs();
                // Start auto-refresh after graphs are loaded
                graphStartAutoRefresh();
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
    parser.add_argument("--log-server-port", type=int, default=5001,
                        help="Port of the log server to forward logs to (default: 5001)")
    parser.add_argument("--log-server-host", type=str, default="127.0.0.1",
                        help="Host of the log server to forward logs to (default: 127.0.0.1)")
    parser.add_argument("--db-path", type=Path, default=None,
                        help="Path to SQLite database for metrics graphs (e.g., /mnt/fgr_data/logs.db)."
                        " If not provided, graph features will be disabled.")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(message)s'
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
        log_server_port=args.log_server_port,
        log_server_host=args.log_server_host,
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