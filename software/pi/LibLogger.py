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
LibLogger - Shared logging module for FGR system.

Two modes of operation:
1. SERVER mode - Owns the database, writer thread, trimming (used by log_server)
2. CLIENT mode - Forwards logs to the log server (used by controller)

In client mode, logs are sent via FGR_MSG_TYPE_LOG messages to the log server.
If the server is unavailable, logs fall back to the journal.
"""

import os
import resource
import sqlite3
import threading
import time
import queue
import collections
import logging
import socket
import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import systemd.journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("[LibLogger] Warning: python-systemd not installed")

# Add protocol directory for FGR imports
script_dir = Path(__file__).parent.absolute()
protocol_dir = script_dir.parent / "protocol"
if str(protocol_dir) not in __import__('sys').path:
    __import__('sys').path.insert(0, str(protocol_dir))

try:
    import fgr_protocol as fgr
except ImportError:
    fgr = None
    print("[LibLogger] Warning: fgr_protocol not available - client mode will fall back to journal")


# ============================================================
# SQL CONSTANTS - Single source of truth for schema definitions
# ============================================================

# Index definitions
LOGS_INDEXES = [
    "CREATE INDEX idx_logs_node_ip ON logs(node_ip)",
    "CREATE INDEX idx_logs_epoch ON logs(epoch_time)",
    "CREATE INDEX idx_logs_tag ON logs(log_tag)",
    "CREATE INDEX idx_logs_epoch_real ON logs(epoch_time_real)",
    "CREATE INDEX idx_logs_message_type ON logs(message_type)",
    "CREATE INDEX idx_logs_log_id ON logs(log_id)",
    "CREATE UNIQUE INDEX idx_unique_log_id ON logs(log_id) WHERE log_id > 0",
    "CREATE INDEX idx_logs_node_ip_epoch ON logs(node_ip, epoch_time DESC)"
]

LOGS_INDEX_NAMES = [
    'idx_logs_node_ip',
    'idx_logs_epoch',
    'idx_logs_tag',
    'idx_logs_epoch_real',
    'idx_logs_message_type',
    'idx_logs_log_id',
    'idx_unique_log_id',
    'idx_logs_node_ip_epoch'
]

# Trigger definitions (contentless FTS)
LOGS_TRIGGERS = [
    """
    CREATE TRIGGER logs_ai AFTER INSERT ON logs
    BEGIN
        INSERT INTO logs_fts(rowid, message) VALUES (new.rowid, new.message);
    END
    """,
    """
    CREATE TRIGGER logs_ad AFTER DELETE ON logs
    BEGIN
        INSERT INTO logs_fts(logs_fts, rowid, message) VALUES ('delete', old.rowid, old.message);
    END
    """,
    """
    CREATE TRIGGER logs_au AFTER UPDATE ON logs
    BEGIN
        INSERT INTO logs_fts(logs_fts, rowid, message) VALUES ('delete', old.rowid, old.message);
        INSERT INTO logs_fts(rowid, message) VALUES (new.rowid, new.message);
    END
    """
]

LOGS_TRIGGER_NAMES = ['logs_ai', 'logs_ad', 'logs_au']

# FTS table definition
FTS_TABLE_SQL = "CREATE VIRTUAL TABLE logs_fts USING fts5(message, content='')"

# Main logs table definition
LOGS_TABLE_SQL = """
    CREATE TABLE logs (
        rowid INTEGER PRIMARY KEY,
        log_id INTEGER NOT NULL UNIQUE,
        node_ip TEXT NOT NULL,
        timestamp_utc TEXT NOT NULL,
        epoch_time REAL NOT NULL,
        log_level INTEGER NOT NULL,
        log_tag TEXT,
        message_type TEXT NOT NULL,
        message TEXT NOT NULL,
        extracted_json TEXT,
        epoch_time_real REAL
    )
"""

# Global sequence table definition
GLOBAL_SEQUENCE_SQL = """
    CREATE TABLE global_sequence (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        next_seq INTEGER NOT NULL DEFAULT 1
    )
"""

GLOBAL_SEQUENCE_INIT_SQL = "INSERT INTO global_sequence (id, next_seq) VALUES (1, 1)"

# Not too large a batch size to avoid being bitten by the watchdog
FTS_REBUILD_BATCH_SIZE = 500


class LibLoggerHandler(logging.Handler):
    """
    Custom logging handler that sends all log records through LibLogger.
    Attach to root logger to capture everything automatically.
    """

    def __init__(self, liblogger: 'LibLogger', node_ip: str = "0.0.0.0",
                 source: str = "CTRL"):
        super().__init__()
        self.liblogger = liblogger
        self.node_ip = node_ip
        self.source = source
        self._closed = False

    def emit(self, record: logging.LogRecord):
        """Send log record to LibLogger"""
        if self._closed:
            return

        if hasattr(self.liblogger, '_stop_writer') and self.liblogger._stop_writer.is_set():
            return

        # Map Python logging levels to LibLogger levels
        level_map = {
            logging.DEBUG: 0,
            logging.INFO: 1,
            logging.WARNING: 2,
            logging.ERROR: 3,
            logging.CRITICAL: 3
        }

        log_level = level_map.get(record.levelno, 1)
        msg = self.format(record)

        try:
            self.liblogger.log(
                source=self.source,
                node_ip=self.node_ip,
                message=msg,
                log_level=log_level,
                log_tag=record.name,
                message_type='LOG'
            )
        except Exception as e:
            print(f"LibLoggerHandler failed: {e}")

    def close(self):
        self._closed = True
        super().close()


class LibLogger:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = False
            self.mode = None  # 'server' or 'client'

            # Server mode attributes
            self.db_path = None
            self.write_queue = None
            self.writer_thread = None
            self._stop_writer = threading.Event()
            self._reserve_lock = threading.Lock()
            self._attached_handlers = []
            self._db_conn = None
            self._db_conn_lock = threading.Lock()
            self.RETENTION_DAYS = None
            self.ENABLE_TRIM = False
            self.log_id_buffer = collections.deque()
            self.buffer_lock = threading.Lock()
            self.BUFFER_RESERVE_SIZE = 2000
            self.BUFFER_REFILL_THRESHOLD = 1000

            # Client mode attributes
            self.server_host = None
            self.server_port = None
            self.fallback_to_journal = True
            self.forward_sock = None
            self.forward_lock = threading.Lock()
            self._reconnect_attempt = 0
            self._max_reconnect_attempts = 10

    # ============================================================
    # INITIALIZATION - Two Modes
    # ============================================================

    def init(self, mode: str = 'server', **kwargs):
        """
        Initialize LibLogger in either server or client mode.

        Server mode (used by log_server):
            mode='server', db_path=Path, retention_days=int, enable_trim=bool

        Client mode (used by controller):
            mode='client', server_host=str, server_port=int, fallback_to_journal=bool
        """
        if self._initialized:
            return

        if mode == 'server':
            self._init_server(**kwargs)
        elif mode == 'client':
            self._init_client(**kwargs)
        else:
            raise ValueError(f"Unknown mode: {mode} (must be 'server' or 'client')")

        self._initialized = True
        print(f"[LibLogger] Initialization complete (mode={mode})")

    def _init_server(self, db_path: Path, retention_days: int = None,
                     enable_trim: bool = False):
        """Initialize as server - owns the database."""
        self.mode = 'server'
        self.db_path = Path(db_path)
        self.RETENTION_DAYS = retention_days
        self.ENABLE_TRIM = enable_trim

        print(f"[LibLogger] Server mode: database at {self.db_path}")

        self._init_tables()
        self._start_writer_thread()

        # Aggressively pre-fill buffer for burst handling
        conn = self._get_temp_connection()
        try:
            for _ in range(3):
                self._refill_buffer(conn)
        finally:
            conn.close()

        with self.buffer_lock:
            print(f"[LibLogger] Database mode ready with {len(self.log_id_buffer)} reserved IDs")

    def _init_client(self, server_host: str = '127.0.0.1',
                     server_port: int = 5001,
                     fallback_to_journal: bool = True):
        """Initialize as client - forwards logs to server."""
        self.mode = 'client'
        self.server_host = server_host
        self.server_port = server_port
        self.fallback_to_journal = fallback_to_journal

        print(f"[LibLogger] Client mode: forwarding to {server_host}:{server_port}")

        # Try to connect immediately
        self._connect_forward()

    # ============================================================
    # CLIENT MODE - Forwarding Methods
    # ============================================================

    def _connect_forward(self) -> bool:
        """Connect to the log server for forwarding."""
        with self.forward_lock:
            # If we already have a socket, try to use it
            if self.forward_sock is not None:
                try:
                    self.forward_sock.getpeername()
                    return True
                except Exception:
                    try:
                        self.forward_sock.close()
                    except Exception:
                        pass
                    self.forward_sock = None

            # Try to connect
            try:
                self.forward_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.forward_sock.settimeout(5.0)
                self.forward_sock.connect((self.server_host, self.server_port))
                self.forward_sock.settimeout(None)  # Blocking mode for reading
                self._reconnect_attempt = 0
                return True
            except Exception:
                self.forward_sock = None
                self._reconnect_attempt += 1
                return False

    def _ensure_forward_connected(self) -> bool:
        """
        Ensure we have a connection to the log server.

        Returns:
            bool: True if connected, False otherwise
        """
        if self.forward_sock is not None:
            try:
                # Check if the socket is still valid
                self.forward_sock.getpeername()
                return True
            except Exception:
                with self.forward_lock:
                    try:
                        self.forward_sock.close()
                    except Exception:
                        pass
                    self.forward_sock = None

        return self._connect_forward()

    def _journal_fallback(self, source: str, node_ip: str, message: str,
                          log_level: int, log_tag: str = None,
                          message_type: str = "LOG"):
        """Fallback to journal when server is unavailable."""
        if not self.fallback_to_journal or not HAS_SYSTEMD:
            return

        try:
            extra = {
                'SYSLOG_IDENTIFIER': 'fgr-log-server',
                'PRIORITY': log_level,
                'FGR_SOURCE': source,
                'FGR_NODE_IP': node_ip,
                'FGR_MESSAGE_TYPE': message_type,
            }
            if log_tag:
                extra['FGR_LOG_TAG'] = log_tag
            systemd.journal.send(message, **extra)
        except Exception:
            pass

    def _forward_log(self, source: str, node_ip: str, message: str,
                    log_level: int, log_tag: str = None,
                    message_type: str = "LOG") -> int:
        """
        Forward a log message to the log server.

        Returns:
            int: 0 on success, -1 on failure (fallback to journal)
        """
        if fgr is None:
            self._journal_fallback(source, node_ip, message, log_level, log_tag, message_type)
            return -1

        # Ensure message is a string before encoding
        if isinstance(message, bytes):
            try:
                message_str = message.decode('utf-8', errors='replace')
            except Exception:
                message_str = repr(message)
        elif isinstance(message, str):
            message_str = message
        else:
            message_str = str(message) if message is not None else ""

        # Safety: ensure it's definitely a string
        if not isinstance(message_str, str):
            message_str = str(message_str)

        # Check connection - this may call _connect_forward()
        if not self._ensure_forward_connected():
            self._journal_fallback(source, node_ip, message_str, log_level, log_tag, message_type)
            return -1

        try:
            # Create and send the log message
            msg = fgr.FGRMsg.create_log(log_level, message_str)
            success, error = fgr.send_message_with_error(self.forward_sock, msg)

            if success:
                return 0

            # send_message_with_error returned False - close and retry once
            with self.forward_lock:
                try:
                    self.forward_sock.close()
                except Exception:
                    pass
                self.forward_sock = None

            if self._connect_forward():
                try:
                    msg = fgr.FGRMsg.create_log(log_level, message_str)
                    success, error = fgr.send_message_with_error(self.forward_sock, msg)
                    if success:
                        return 0
                except Exception:
                    pass

            # Failed - fall back to journal
            self._journal_fallback(source, node_ip, message_str, log_level, log_tag, message_type)
            return -1

        except Exception as e:
            # Unexpected exception - clean up the socket and re-raise
            with self.forward_lock:
                try:
                    self.forward_sock.close()
                except Exception:
                    pass
                self.forward_sock = None

            # Re-raise the exception as this is outside normal operation
            raise

    # ============================================================
    # SERVER MODE - Database Methods
    # ============================================================

    def _get_temp_connection(self):
        """Create a temporary database connection."""
        self._ensure_server_mode()
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    def _get_connection(self):
        """Get the shared database connection (for writer thread only)."""
        with self._db_conn_lock:
            if self._db_conn is None:
                self._db_conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
                self._db_conn.execute("PRAGMA journal_mode=WAL")
                self._db_conn.execute("PRAGMA synchronous=OFF")
                self._db_conn.execute("PRAGMA busy_timeout=10000")
                self._db_conn.execute("PRAGMA cache_size=50000")
                result = self._db_conn.execute("PRAGMA cache_size;").fetchone()
                print(f"[LibLogger] Cache size set to: {result[0]}")
            return self._db_conn

    def _close_connection(self):
        """Close the shared database connection."""
        with self._db_conn_lock:
            if self._db_conn is not None:
                try:
                    self._db_conn.close()
                except Exception:
                    pass
                finally:
                    self._db_conn = None

    def _set_low_priority(self):
        """Set low CPU and I/O priority."""
        original = {}
        try:
            original['nice'] = os.nice(0)
            os.nice(19 - original['nice'])
            print(f"[LibLogger] CPU priority set from {original['nice']} to 19")
        except Exception as e:
            print(f"[LibLogger] Warning: Could not set CPU priority: {e}")
            original['nice'] = None

        try:
            import ctypes
            from ctypes import c_int

            IOPRIO_WHO_PROCESS = 1
            IOPRIO_CLASS_IDLE = 3
            IOPRIO_CLASS_SHIFT = 13

            def ioprio_set(which, who, ioprio):
                libc = ctypes.CDLL('libc.so.6')
                return libc.ioprio_set(c_int(which), c_int(who), c_int(ioprio))

            ioprio = IOPRIO_CLASS_IDLE << IOPRIO_CLASS_SHIFT
            result = ioprio_set(IOPRIO_WHO_PROCESS, os.getpid(), ioprio)
            if result == 0:
                print("[LibLogger] I/O priority set to idle (class 3)")
                original['ioprio_changed'] = True
            else:
                print(f"[LibLogger] Warning: ioprio_set returned {result}")
                original['ioprio_changed'] = False
        except AttributeError:
            print("[LibLogger] Note: I/O priority not supported on this platform")
            original['ioprio_changed'] = False
        except Exception as e:
            print(f"[LibLogger] Warning: Could not set I/O priority: {e}")
            original['ioprio_changed'] = False

        return original

    def _restore_priority(self, original):
        """Restore original priority settings."""
        if original.get('nice') is not None:
            try:
                current = os.nice(0)
                delta = original['nice'] - current
                if delta != 0:
                    os.nice(delta)
                    print(f"[LibLogger] CPU priority restored to {original['nice']}")
            except Exception as e:
                print(f"[LibLogger] Warning: Could not restore CPU priority: {e}")

        if original.get('ioprio_changed', False):
            try:
                import ctypes
                from ctypes import c_int

                IOPRIO_WHO_PROCESS = 1
                IOPRIO_CLASS_BE = 2
                IOPRIO_CLASS_SHIFT = 13

                def ioprio_set(which, who, ioprio):
                    libc = ctypes.CDLL('libc.so.6')
                    return libc.ioprio_set(c_int(which), c_int(who), c_int(ioprio))

                ioprio = IOPRIO_CLASS_BE << IOPRIO_CLASS_SHIFT
                result = ioprio_set(IOPRIO_WHO_PROCESS, os.getpid(), ioprio)
                if result == 0:
                    print("[LibLogger] I/O priority restored to default (best-effort)")
            except Exception as e:
                print(f"[LibLogger] Warning: Could not restore I/O priority: {e}")

    def _init_tables(self):
        """Create tables if needed."""
        if self.db_path is None:
            return

        conn = self._get_temp_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='logs'")
            if cursor.fetchone():
                print("[LibLogger] Database schema already exists")

                for trigger_sql in LOGS_TRIGGERS:
                    try:
                        cursor.execute(trigger_sql)
                    except sqlite3.OperationalError as e:
                        if "already exists" not in str(e):
                            raise e
                print("[LibLogger] Triggers ensured")

                result = self.consistency_check(repair=False, fast=True)
                if not result['consistent']:
                    print(f"[LibLogger] ⚠️  Inconsistent counts detected!")
                    print(f"[LibLogger]    logs={result['log_count']}, fts={result['fts_count']}")
                return

            print("[LibLogger] Creating database schema...")
            cursor.execute(LOGS_TABLE_SQL)

            for index_sql in LOGS_INDEXES:
                cursor.execute(index_sql)

            cursor.execute(GLOBAL_SEQUENCE_SQL)
            cursor.execute(GLOBAL_SEQUENCE_INIT_SQL)

            cursor.execute(FTS_TABLE_SQL)

            for trigger_sql in LOGS_TRIGGERS:
                cursor.execute(trigger_sql)

            conn.commit()
            print("[LibLogger] Database schema created")

        finally:
            conn.close()

    def _refill_buffer(self, conn):
        """Refill the log_id buffer using the provided connection."""
        with self._reserve_lock:
            with self.buffer_lock:
                if len(self.log_id_buffer) > self.BUFFER_REFILL_THRESHOLD:
                    return

            cursor = conn.cursor()
            for attempt in range(3):
                try:
                    cursor.execute("""
                        UPDATE global_sequence
                        SET next_seq = next_seq + ?
                        WHERE id = 1
                        RETURNING next_seq - ? + 1, next_seq
                    """, (self.BUFFER_RESERVE_SIZE, self.BUFFER_RESERVE_SIZE))

                    result = cursor.fetchone()
                    if result:
                        start, end = result
                        new_ids = collections.deque(range(start, end + 1))
                        with self.buffer_lock:
                            self.log_id_buffer.extend(new_ids)
                        print(f"[LibLogger] Refilled buffer: {start}-{end}")
                        return
                    else:
                        raise RuntimeError("[LibLogger] global_sequence row missing")
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 2:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                    raise

    def _get_next_log_id(self) -> int:
        """Get next log_id from buffer."""
        if self.db_path is None:
            raise RuntimeError("[LibLogger] Cannot get log_id in journal-only mode")

        with self.buffer_lock:
            if self.log_id_buffer:
                return self.log_id_buffer.popleft()

        timeout = 0.5
        start = time.time()
        while time.time() - start < timeout:
            with self.buffer_lock:
                if self.log_id_buffer:
                    return self.log_id_buffer.popleft()
            time.sleep(0.001)

        raise RuntimeError("[LibLogger] No log_ids available - buffer underrun")

    def _start_writer_thread(self):
        """Start the writer thread."""
        self.write_queue = queue.Queue(maxsize=50000)
        self._stop_writer.clear()

        def writer_loop():
            conn = self._get_connection()
            cursor = conn.cursor()

            print("[LibLogger] Writer thread started")
            batch = []
            batch_count = 0
            last_flush_time = time.time()
            last_buffer_check = time.time()
            last_trim_time = time.time()
            last_stats_log = time.time()

            MAX_BATCH_SIZE = 5000
            FLUSH_INTERVAL = 10.0
            BUFFER_CHECK_INTERVAL = 1.0
            TRIM_INTERVAL = 86400
            STATS_LOG_INTERVAL = 300

            while not self._stop_writer.is_set():
                now = time.time()

                if now - last_stats_log >= STATS_LOG_INTERVAL:
                    try:
                        size = conn.execute("PRAGMA cache_size;").fetchone()[0]
                        try:
                            hit_ratio = conn.execute("PRAGMA cache_hit_ratio;").fetchone()[0]
                            hit_str = f", hit_ratio={hit_ratio}%"
                        except:
                            hit_str = ""
                        try:
                            page_count = conn.execute("PRAGMA page_count;").fetchone()[0]
                            page_str = f", pages={page_count}"
                        except:
                            page_str = ""
                        try:
                            freelist = conn.execute("PRAGMA freelist_count;").fetchone()[0]
                            freelist_str = f", freelist={freelist}"
                        except:
                            freelist_str = ""

                        self.log_admin(f"SQLite cache: size={size}{hit_str}{page_str}{freelist_str}")
                    except Exception as e:
                        print(f"[LibLogger] Cache stats error: {e}")
                    last_stats_log = now

                if now - last_buffer_check >= BUFFER_CHECK_INTERVAL:
                    with self.buffer_lock:
                        buffer_size = len(self.log_id_buffer)
                    if buffer_size <= self.BUFFER_REFILL_THRESHOLD:
                        try:
                            self._refill_buffer(conn)
                        except Exception as e:
                            print(f"[LibLogger] Buffer refill failed: {e}")
                    last_buffer_check = now

                if self.ENABLE_TRIM and self.RETENTION_DAYS is not None and (now - last_trim_time >= TRIM_INTERVAL):
                    try:
                        deleted = self.trim_old_logs(days=self.RETENTION_DAYS)
                        if deleted > 0:
                            print(f"[LibLogger] Daily trim: removed {deleted} old logs")
                        last_trim_time = now
                    except Exception as e:
                        print(f"[LibLogger] ERROR: Daily trim failed: {e}")

                try:
                    query, params = self.write_queue.get(timeout=0.1)
                    if query is None and params is None:
                        break

                    batch.append((query, params))
                    batch_count += 1

                    if batch_count >= MAX_BATCH_SIZE or (batch_count > 0 and now - last_flush_time >= FLUSH_INTERVAL):
                        try:
                            for q, p in batch:
                                cursor.execute(q, p)
                            last_flush_time = now
                            if batch_count >= 20:
                                print(f"[LibLogger] Committed batch of {len(batch)} logs")
                        except Exception as e:
                            print(f"[LibLogger] Batch write error: {e}")
                            for q, p in reversed(batch):
                                try:
                                    self.write_queue.put_nowait((q, p))
                                except queue.Full:
                                    pass
                        finally:
                            batch = []
                            batch_count = 0

                except queue.Empty:
                    if batch_count > 0:
                        try:
                            for q, p in batch:
                                cursor.execute(q, p)
                        except Exception as e:
                            print(f"[LibLogger] Idle flush error: {e}")
                        finally:
                            batch = []
                            batch_count = 0
                        continue
                    continue
                except Exception as e:
                    print(f"[LibLogger] Writer thread error: {e}")
                    time.sleep(0.1)

            if batch:
                try:
                    for q, p in batch:
                        cursor.execute(q, p)
                    print(f"[LibLogger] Final flush: {len(batch)} logs")
                except Exception as e:
                    print(f"[LibLogger] Final flush error: {e}")

            print("[LibLogger] Writer thread stopped")

        self.writer_thread = threading.Thread(target=writer_loop, daemon=False, name="LibLogger-Writer")
        self.writer_thread.start()

    # ============================================================
    # PUBLIC METHODS
    # ============================================================

    def log(self, source: str, node_ip: str, message: str,
            log_level: int = 1, log_tag: str = None,
            message_type: str = "LOG") -> int:
        """Write a log entry - called by ANY thread."""
        if not self._initialized:
            raise RuntimeError("[LibLogger] LibLogger not initialized")

        if self.mode == 'client':
            return self._forward_log(source, node_ip, message, log_level, log_tag, message_type)

        # Server mode
        if self._stop_writer.is_set():
            return -1

        log_id = self._get_next_log_id() if self.db_path is not None else None

        if self.db_path is not None and self.write_queue:
            timestamp = time.time()
            query = """
                INSERT INTO logs (
                    log_id, node_ip, timestamp_utc, epoch_time, log_level,
                    log_tag, message_type, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (log_id, node_ip, datetime.fromtimestamp(timestamp).isoformat() + 'Z',
                      timestamp, log_level, log_tag, message_type, message)
            try:
                self.write_queue.put_nowait((query, params))
            except queue.Full:
                raise RuntimeError(f"[LibLogger] Queue full, log_id {log_id} lost")

        if HAS_SYSTEMD:
            extra = {
                'SYSLOG_IDENTIFIER': 'fgr-log-server',
                'PRIORITY': log_level,
                'FGR_SOURCE': source,
                'FGR_NODE_IP': node_ip,
                'FGR_MESSAGE_TYPE': message_type,
            }
            if log_id is not None:
                extra['FGR_LOG_ID'] = str(log_id)
            if log_tag:
                extra['FGR_LOG_TAG'] = log_tag
            systemd.journal.send(message, **extra)

        return log_id if log_id is not None else 0

    def log_admin(self, message: str, log_level: int = 6) -> None:
        """Admin log - journal only."""
        if self.mode == 'client':
            # In client mode, log_admin goes to journal directly
            if HAS_SYSTEMD:
                systemd.journal.send(message, SYSLOG_IDENTIFIER='fgr-log-server',
                                     PRIORITY=log_level, FGR_SOURCE='ADMIN')
            return

        if HAS_SYSTEMD:
            systemd.journal.send(message, SYSLOG_IDENTIFIER='fgr-log-server',
                                 PRIORITY=log_level, FGR_SOURCE='ADMIN')

    def attach_to_root_logger(self, node_ip: str = "0.0.0.0",
                              source: str = "CTRL",
                              level: int = logging.DEBUG,
                              format_str: str = '%(message)s') -> None:
        """Attach a LibLogger handler to the root logger."""
        if not self._initialized:
            raise RuntimeError("[LibLogger] LibLogger not initialized. Call init() first.")

        handler = LibLoggerHandler(self, node_ip=node_ip, source=source)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(format_str))

        logging.root.addHandler(handler)
        self._attached_handlers.append(handler)

        print(f"[LibLogger] Attached to root logger (source={source}, node_ip={node_ip})")

    def attach_to_logger(self, logger: logging.Logger, node_ip: str = "0.0.0.0",
                         source: str = "CTRL", level: int = logging.DEBUG,
                         format_str: str = '%(message)s') -> None:
        """Attach a LibLogger handler to a specific logger."""
        if not self._initialized:
            raise RuntimeError("[LibLogger] LibLogger not initialized. Call init() first.")

        handler = LibLoggerHandler(self, node_ip=node_ip, source=source)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(format_str))

        logger.addHandler(handler)
        self._attached_handlers.append(handler)

        print(f"[LibLogger] Attached to logger '{logger.name}' (source={source}, node_ip={node_ip})")

    def detach_all(self) -> None:
        """Remove all attached LibLogger handlers."""
        for handler in self._attached_handlers:
            handler.close()
            logging.root.removeHandler(handler)

        self._attached_handlers.clear()
        print("[LibLogger] Detached all handlers")

    # ============================================================
    # SERVER MODE ONLY - Database Maintenance Methods
    # ============================================================

    def _ensure_server_mode(self):
        """Raise an error if not in server mode."""
        if self.mode != 'server':
            raise RuntimeError(f"Method not supported in {self.mode} mode (server mode required)")

    def trim_old_logs(self, days: int = 7, batch_size: int = 1000) -> int:
        """Delete logs older than specified days."""
        self._ensure_server_mode()

        if self.db_path is None:
            return 0

        try:
            conn = self._get_temp_connection()
        except Exception as e:
            print(f"[LibLogger] ERROR: Could not connect to database for trim: {e}")
            return 0

        total_deleted = 0

        try:
            cursor = conn.cursor()
            cutoff = time.time() - (days * 86400)

            from datetime import datetime
            print(f"[LibLogger] Trimming logs older than {days} days (cutoff: {datetime.fromtimestamp(cutoff).isoformat()})")

            cursor.execute(
                "SELECT rowid FROM logs WHERE epoch_time < ? ORDER BY rowid LIMIT ?",
                (cutoff, batch_size)
            )
            rowids = [row[0] for row in cursor.fetchall()]

            if not rowids:
                print("[LibLogger] No logs to trim")
                return 0

            # Delete from FTS first
            placeholders = ','.join(['?' for _ in rowids])
            cursor.execute(f"DELETE FROM logs_fts WHERE rowid IN ({placeholders})", rowids)
            total_fts_deleted = cursor.rowcount

            # Delete from logs
            cursor.execute(
                "DELETE FROM logs WHERE epoch_time < ? LIMIT ?",
                (cutoff, batch_size)
            )
            total_deleted = cursor.rowcount

            # Clean metrics_history if it exists
            try:
                cursor.execute("DELETE FROM metrics_history WHERE epoch_time < ?", (cutoff,))
                total_metrics_deleted = cursor.rowcount
                if total_metrics_deleted > 0:
                    print(f"[LibLogger] Deleted {total_metrics_deleted} rows from metrics_history")
            except sqlite3.OperationalError:
                pass

            conn.commit()
            print(f"[LibLogger] Trim complete: logs={total_deleted}, fts={total_fts_deleted}")

            return total_deleted

        except Exception as e:
            print(f"[LibLogger] ERROR: Trim operation failed: {e}")
            return total_deleted
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def consistency_check(self, repair: bool = False, fast: bool = True) -> dict:
        """Check consistency between logs and FTS tables."""
        self._ensure_server_mode()

        if self.db_path is None:
            return {'error': 'Database not available'}

        conn = self._get_temp_connection()
        try:
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM logs;")
            log_count = cursor.fetchone()[0]

            if fast:
                cursor.execute("SELECT MAX(rowid) FROM logs;")
                max_log_rowid = cursor.fetchone()[0] or 0

                cursor.execute("SELECT MAX(rowid) FROM logs_fts;")
                max_fts_rowid = cursor.fetchone()[0] or 0

                if max_log_rowid == max_fts_rowid and max_log_rowid > 0:
                    cursor.execute("SELECT 1 FROM logs_fts WHERE rowid = ?", (max_log_rowid,))
                    exists = cursor.fetchone() is not None
                    if exists:
                        return {
                            'log_count': log_count,
                            'fts_count': log_count,
                            'orphaned_fts': 0,
                            'missing_from_fts': 0,
                            'consistent': True,
                            'repaired': False,
                            'mode': 'fast',
                            'message': 'OK (max rowid match)'
                        }

                min_rowid = max(max_log_rowid - 10000, 1)
                cursor.execute("SELECT COUNT(*) FROM logs WHERE rowid >= ? AND rowid <= ?", (min_rowid, max_log_rowid))
                recent_log_count = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM logs_fts WHERE rowid >= ? AND rowid <= ?", (min_rowid, max_log_rowid))
                recent_fts_count = cursor.fetchone()[0]

                if recent_log_count > 0 and recent_fts_count >= recent_log_count * 0.95:
                    return {
                        'log_count': log_count,
                        'fts_count': log_count,
                        'orphaned_fts': -1,
                        'missing_from_fts': -1,
                        'consistent': True,
                        'repaired': False,
                        'mode': 'fast',
                        'message': f'OK (recent rows verified: {recent_fts_count}/{recent_log_count})'
                    }

                print("[LibLogger] Fast heuristic failed - falling back to slow count...")
                cursor.execute("SELECT COUNT(*) FROM logs_fts;")
                fts_count = cursor.fetchone()[0]
                consistent = (log_count == fts_count)

                return {
                    'log_count': log_count,
                    'fts_count': fts_count,
                    'orphaned_fts': -1,
                    'missing_from_fts': -1,
                    'consistent': consistent,
                    'repaired': False,
                    'mode': 'fast_fallback',
                    'message': 'OK' if consistent else f'INCONSISTENT: logs={log_count}, fts={fts_count}'
                }

            # Slow mode
            print("[LibLogger] Full consistency scan started...")
            start_time = time.time()

            cursor.execute("SELECT COUNT(*) FROM logs_fts;")
            fts_count = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*) FROM logs_fts f
                LEFT JOIN logs l ON f.rowid = l.rowid
                WHERE l.rowid IS NULL
            """)
            orphaned = cursor.fetchone()[0]

            cursor.execute("""
                SELECT COUNT(*) FROM logs l
                LEFT JOIN logs_fts f ON l.rowid = f.rowid
                WHERE f.rowid IS NULL
            """)
            missing_from_fts = cursor.fetchone()[0]

            consistent = (log_count == fts_count and orphaned == 0 and missing_from_fts == 0)
            repaired = False

            if repair and not consistent:
                if orphaned > 0:
                    print(f"[LibLogger] Repairing {orphaned} orphaned FTS entries...")
                    cursor.execute("""
                        DELETE FROM logs_fts
                        WHERE rowid NOT IN (SELECT rowid FROM logs)
                    """)
                    deleted = cursor.rowcount
                    conn.commit()
                    repaired = True
                    print(f"[LibLogger] Deleted {deleted} orphaned FTS entries")

                cursor.execute("SELECT COUNT(*) FROM logs_fts;")
                fts_count = cursor.fetchone()[0]

                cursor.execute("""
                    SELECT COUNT(*) FROM logs_fts f
                    LEFT JOIN logs l ON f.rowid = l.rowid
                    WHERE l.rowid IS NULL
                """)
                orphaned = cursor.fetchone()[0]

                consistent = (log_count == fts_count and orphaned == 0 and missing_from_fts == 0)

            elapsed = time.time() - start_time
            print(f"[LibLogger] Full consistency scan complete ({elapsed:.1f}s)")

            return {
                'log_count': log_count,
                'fts_count': fts_count,
                'orphaned_fts': orphaned,
                'missing_from_fts': missing_from_fts,
                'consistent': consistent,
                'repaired': repaired,
                'mode': 'slow',
                'message': 'OK' if consistent else 'INCONSISTENT'
            }
        finally:
            conn.close()

    def rebuild_database(self, rebuild_fts: bool = True, vacuum: bool = True) -> dict:
        """Complete database rebuild."""
        self._ensure_server_mode()

        if self.db_path is None:
            return {'error': 'Database not available'}

        print("[LibLogger] STARTING COMPLETE DATABASE REBUILD...")

        original_priority = self._set_low_priority()

        temp_dir = '/mnt/fgr_data/tmp'
        if not os.path.exists(temp_dir):
            try:
                os.makedirs(temp_dir, mode=0o1777, exist_ok=True)
                print(f"[LibLogger] Created temp directory: {temp_dir}")
            except Exception as e:
                print(f"[LibLogger] Warning: Could not create temp directory {temp_dir}: {e}")

        conn = self._get_temp_connection()
        try:
            cursor = conn.cursor()
            results = {}

            try:
                conn.execute(f"PRAGMA temp_store_directory = '{temp_dir}'")
                print(f"[LibLogger] Temp directory set to: {temp_dir}")
            except Exception as e:
                print(f"[LibLogger] Warning: Could not set temp directory: {e}")

            cursor.execute("SELECT COUNT(*) FROM logs;")
            results['log_count_before'] = cursor.fetchone()[0]
            print(f"[LibLogger] Initial logs count: {results['log_count_before']}")

            print("[LibLogger] Dropping corrupted indexes...")
            for idx_name in LOGS_INDEX_NAMES:
                try:
                    cursor.execute(f"DROP INDEX IF EXISTS {idx_name}")
                except Exception as e:
                    print(f"[LibLogger]   Warning dropping {idx_name}: {e}")
            conn.commit()
            print("[LibLogger] ✓ Indexes dropped")

            print("[LibLogger] Recreating indexes...")
            for index_sql in LOGS_INDEXES:
                cursor.execute(index_sql)
                conn.commit()
                time.sleep(0.1)
            conn.commit()
            print("[LibLogger] ✓ Indexes recreated")

            if rebuild_fts:
                print("[LibLogger] Rebuilding FTS table...")

                for trigger_name in LOGS_TRIGGER_NAMES:
                    try:
                        cursor.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
                    except Exception as e:
                        print(f"[LibLogger]   Warning dropping trigger {trigger_name}: {e}")

                cursor.execute("DROP TABLE IF EXISTS logs_fts")
                print("[LibLogger]   Dropped old FTS table")

                cursor.execute(FTS_TABLE_SQL)
                print("[LibLogger]   Created contentless FTS table")

                print("[LibLogger]   Populating FTS from logs...")
                cursor.execute("SELECT rowid FROM logs ORDER BY rowid")
                all_rowids = [row[0] for row in cursor.fetchall()]
                total_to_insert = len(all_rowids)
                print(f"[LibLogger]   Total rows to insert: {total_to_insert}")

                batch_size = FTS_REBUILD_BATCH_SIZE
                total_inserted = 0
                skipped_rows = []

                for i in range(0, total_to_insert, batch_size):
                    batch = all_rowids[i:i + batch_size]
                    placeholders = ','.join(['?'] * len(batch))

                    try:
                        cursor.execute(f"""
                            INSERT INTO logs_fts(rowid, message)
                            SELECT rowid, message FROM logs
                            WHERE rowid IN ({placeholders})
                        """, batch)
                        inserted = cursor.rowcount
                        total_inserted += inserted
                        conn.commit()
                    except Exception as e:
                        print(f"[LibLogger]     Batch {i+1} failed: {e}")
                        for rowid in batch:
                            try:
                                cursor.execute(
                                    "INSERT INTO logs_fts(rowid, message) SELECT rowid, message FROM logs WHERE rowid = ?",
                                    (rowid,)
                                )
                                if cursor.rowcount > 0:
                                    total_inserted += 1
                            except Exception as e2:
                                print(f"[LibLogger]       Failed row {rowid}: {e2}")
                                skipped_rows.append(rowid)
                        conn.commit()

                    if ((i // batch_size) % 50) == 0 and i > 0:
                        percent = int(total_inserted * 100 / total_to_insert)
                        print(f"[LibLogger]     Progress: {percent}% ({total_inserted}/{total_to_insert})")

                print(f"[LibLogger]   Populated {total_inserted} rows")
                if skipped_rows:
                    print(f"[LibLogger]   Skipped {len(skipped_rows)} problematic rows")

                cursor.execute("SELECT COUNT(*) FROM logs_fts;")
                results['fts_count_after'] = cursor.fetchone()[0]
                print(f"[LibLogger]   FTS count: {results['fts_count_after']}")

                print("[LibLogger]   Recreating triggers...")
                for trigger_sql in LOGS_TRIGGERS:
                    cursor.execute(trigger_sql)
                conn.commit()
                print("[LibLogger]   ✓ Triggers recreated")

            if vacuum:
                print("[LibLogger] Compacting database (VACUUM)...")
                try:
                    cursor.execute("VACUUM;")
                    print("[LibLogger] ✓ VACUUM complete")
                except sqlite3.OperationalError as e:
                    print(f"[LibLogger] ⚠️  VACUUM failed: {e}")

            print("[LibLogger] Running final verification...")
            cursor.execute("SELECT COUNT(*) FROM logs;")
            results['log_count_after'] = cursor.fetchone()[0]

            if rebuild_fts:
                cursor.execute("SELECT COUNT(*) FROM logs_fts;")
                results['fts_count_after'] = cursor.fetchone()[0]
            else:
                results['fts_count_after'] = None

            cursor.execute("PRAGMA freelist_count;")
            results['freelist'] = cursor.fetchone()[0]

            cursor.execute("PRAGMA quick_check;")
            quick_check = cursor.fetchone()
            results['quick_check'] = quick_check[0] if quick_check else None

            results['consistent'] = (results['log_count_before'] == results['log_count_after'])
            if rebuild_fts:
                results['consistent'] = results['consistent'] and (results['log_count_after'] == results['fts_count_after'])

            print("[LibLogger] REBUILD COMPLETE")
            print(f"[LibLogger]   Logs count: {results['log_count_after']}")
            if rebuild_fts:
                print(f"[LibLogger]   FTS count:  {results['fts_count_after']}")
            print(f"[LibLogger]   Freelist:  {results['freelist']}")
            print(f"[LibLogger]   Consistent: {results['consistent']}")

            return results

        except Exception as e:
            print(f"[LibLogger] ERROR during rebuild: {e}")
            import traceback
            traceback.print_exc()
            return {'error': str(e)}
        finally:
            self._restore_priority(original_priority)
            conn.close()

    def execute_sql(self, query: str, params: tuple = ()) -> Optional[List[Dict]]:
        """Execute SQL query and return results."""
        self._ensure_server_mode()

        if self.db_path is None:
            return None

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            if query.strip().upper().startswith('SELECT'):
                return [dict(row) for row in cursor.fetchall()]
            conn.commit()
            return None
        except Exception as e:
            print(f"[LibLogger] SQL error: {e}")
            return None
        finally:
            conn.close()

    def get_logs_by_log_id(self, target_log_id: int, before: int = 100, after: int = 100) -> List[Dict]:
        """Get logs around a specific log_id."""
        self._ensure_server_mode()

        if self.db_path is None:
            return []

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            start_id = max(1, target_log_id - before)
            end_id = target_log_id + after
            cursor.execute("""
                SELECT log_id, epoch_time, message, node_ip, log_level, log_tag, message_type
                FROM logs WHERE log_id BETWEEN ? AND ? ORDER BY log_id ASC
            """, (start_id, end_id))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_logs_by_timestamp(self, timestamp: float, before: int = 100, after: int = 100) -> List[Dict]:
        """Get logs around a specific timestamp."""
        self._ensure_server_mode()

        if self.db_path is None:
            return []

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT log_id FROM logs WHERE epoch_time <= ? ORDER BY epoch_time DESC LIMIT 1", (timestamp,))
            row = cursor.fetchone()
            if not row:
                return []
            return self.get_logs_by_log_id(row['log_id'], before, after)
        finally:
            conn.close()

    def is_db_available(self) -> bool:
        """Check if database is available."""
        if self.mode == 'client':
            return False
        return self.db_path is not None and self.write_queue is not None

    # ============================================================
    # SHUTDOWN
    # ============================================================

    def shutdown(self, timeout: float = 5.0) -> None:
        """Gracefully shut down LibLogger."""
        if self.mode == 'client':
            # Close the forwarding socket
            with self.forward_lock:
                if self.forward_sock is not None:
                    try:
                        self.forward_sock.close()
                    except Exception:
                        pass
                    self.forward_sock = None
            print("[LibLogger] Client mode shutdown complete")
            return

        # Server mode shutdown
        if self.db_path is None:
            print("[LibLogger] Journal-only mode, nothing to shut down")
            return

        print("[LibLogger] Shutting down...")

        self.detach_all()
        self._stop_writer.set()

        if self.write_queue:
            try:
                self.write_queue.put_nowait((None, None))
            except queue.Full:
                pass

        if self.writer_thread and self.writer_thread.is_alive():
            pending = self.write_queue.qsize() if self.write_queue else 0
            if pending > 0:
                print(f"[LibLogger] Waiting for {pending} queued logs...")
            self.writer_thread.join(timeout=timeout)
            if self.writer_thread.is_alive():
                print("[LibLogger] WARNING: Writer thread did not stop within timeout")
            else:
                print("[LibLogger] Writer thread stopped cleanly")

        self._close_connection()
        print("[LibLogger] Shutdown complete")