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

DESIGN:
- Writer thread owns the only long-lived write connection
- Client threads NEVER touch the database
- log_ids come from a shared buffer (deque)
- Writer thread refills buffer before it gets low
- No per-thread connections, no connection leaks

INTEGRATION:
- Provides a logging.Handler subclass to capture all standard Python logging
- Use attach_to_root_logger() to automatically capture all logs
- Use admin_log() for logs that should NOT go to the database
"""

import os
import resource
import sqlite3
import threading
import time
import queue
import collections
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import systemd.journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("[LibLogger] Warning: python-systemd not installed")


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

        # Don't try to log during shutdown
        if hasattr(self.liblogger, '_stop_writer') and self.liblogger._stop_writer.is_set():
            return

        # Map Python logging levels to LibLogger levels
        # LibLogger: 0=DEBUG, 1=INFO, 2=WARN, 3=ERROR
        level_map = {
            logging.DEBUG: 0,
            logging.INFO: 1,
            logging.WARNING: 2,
            logging.ERROR: 3,
            logging.CRITICAL: 3
        }

        log_level = level_map.get(record.levelno, 1)

        # Format the message
        msg = self.format(record)

        # Send to LibLogger
        try:
            self.liblogger.log(
                source=self.source,
                node_ip=self.node_ip,
                message=msg,
                log_level=log_level,
                log_tag=record.name,  # Logger name becomes the tag
                message_type='LOG'
            )
        except Exception as e:
            # Fallback to avoid recursion
            print(f"LibLoggerHandler failed: {e}")

    def close(self):
        """Close the handler - prevents further logging"""
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
            self.db_path = None
            self.write_queue = None
            self.writer_thread = None
            self._stop_writer = threading.Event()
            self._reserve_lock = threading.Lock()
            self._attached_handlers = []  # Track attached handlers for cleanup
            self._db_conn = None
            self._db_conn_lock = threading.Lock()
            self.RETENTION_DAYS = None  # Default to None (no trimming)
            self.ENABLE_TRIM = False

            # Shared buffer for log_ids (deque is thread-safe for append/popleft)
            self.log_id_buffer = collections.deque()
            self.buffer_lock = threading.Lock()

            # Buffer settings - large enough to handle bursts
            self.BUFFER_RESERVE_SIZE = 2000
            self.BUFFER_REFILL_THRESHOLD = 1000

    def _get_temp_connection(self):
        """Create a temporary database connection WITH auto-commit"""
        conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=OFF")
        return conn

    def _get_connection(self):
        """Get the shared database connection (for writer thread only)"""
        with self._db_conn_lock:
            if self._db_conn is None:
                self._db_conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
                self._db_conn.execute("PRAGMA journal_mode=WAL")
                self._db_conn.execute("PRAGMA synchronous=OFF")
                self._db_conn.execute("PRAGMA busy_timeout=10000")
                self._db_conn.execute("PRAGMA cache_size=50000") # About 200 Mbytes of RAM to save on USB bandwidth
                result = self._db_conn.execute("PRAGMA cache_size;").fetchone()
                print(f"[LibLogger] Cache size set to: {result[0]}")

            return self._db_conn

    def _close_connection(self):
        """Close the shared database connection"""
        with self._db_conn_lock:
            if self._db_conn is not None:
                try:
                    self._db_conn.close()
                except Exception:
                    pass
                finally:
                    self._db_conn = None

    def _set_low_priority(self):
        """Set low CPU and I/O priority for the current process.
        Returns a dict with original settings for restoration.
        """
        original = {}

        try:
            # Get original nice value
            original['nice'] = os.nice(0)
            # Set CPU priority to lowest (nice=19)
            os.nice(19 - original['nice'])
            print(f"[LibLogger] CPU priority set from {original['nice']} to 19")
        except Exception as e:
            print(f"[LibLogger] Warning: Could not set CPU priority: {e}")
            original['nice'] = None

        try:
            # I/O priority is more complex - we can't easily read it back
            # So we'll store that we changed it
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

        # I/O priority restoration is tricky - we'd need to know what it was
        # If we changed it, we could try to set it back to 0 (default)
        if original.get('ioprio_changed', False):
            try:
                import ctypes
                from ctypes import c_int

                IOPRIO_WHO_PROCESS = 1
                IOPRIO_CLASS_BE = 2  # Best-effort (default)
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

    def _init_tables(self) -> None:
        """Create tables if needed - uses temporary connection."""
        if self.db_path is None:
            return

        conn = self._get_temp_connection()
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='logs'")
            if cursor.fetchone():
                # Tables exist - check and create missing triggers
                print("[LibLogger] Database schema already exists")

                # Create triggers if they don't exist
                for trigger_sql in LOGS_TRIGGERS:
                    try:
                        cursor.execute(trigger_sql)
                    except sqlite3.OperationalError as e:
                        if "already exists" not in str(e):
                            raise e
                print("[LibLogger] Triggers ensured (insert, delete, update)")

                # Fast consistency check with timing
                print("[LibLogger] Running fast consistency check; should take only a few seconds unless there is serious corruption.")
                start_time = time.time()
                result = self.consistency_check(repair=False, fast=True)
                elapsed = time.time() - start_time
                print(f"[LibLogger] Consistency (fast): logs={result['log_count']}, fts={result['fts_count']} (took {elapsed:.2f}s)")

                if not result['consistent']:
                    print(f"[LibLogger] ⚠️  Inconsistent counts detected!")
                    print(f"[LibLogger]    logs={result['log_count']}, fts={result['fts_count']}")
                    print(f"[LibLogger]    Run consistency_check(repair=True, fast=False) to fix")
                    print(f"[LibLogger]    ...and, if that doesn't fix it, start rebuild_database(rebuild_fts=True, vacuum=True) and make some coffee")

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
            print("[LibLogger] Database schema created with contentless FTS and triggers (insert, delete, update)")

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
        """Get next log_id from buffer - called by ANY thread."""
        if self.db_path is None:
            raise RuntimeError("[LibLogger] Cannot get log_id in journal-only mode")

        # Try to pop from buffer
        with self.buffer_lock:
            if self.log_id_buffer:
                return self.log_id_buffer.popleft()

        # Buffer empty - wait briefly (writer thread should refill before this happens)
        timeout = 0.5
        start = time.time()
        while time.time() - start < timeout:
            with self.buffer_lock:
                if self.log_id_buffer:
                    return self.log_id_buffer.popleft()
            time.sleep(0.001)

        raise RuntimeError("[LibLogger] No log_ids available - buffer underrun")

    def _start_writer_thread(self):
        """Start the writer thread - owns the ONLY database connection."""
        self.write_queue = queue.Queue(maxsize=50000)
        self._stop_writer.clear()

        def writer_loop():
            # Writer thread creates its own connection
            conn = self._get_connection()
            cursor = conn.cursor()

            print("[LibLogger] Writer thread started")
            batch = []
            batch_count = 0
            last_flush_time = time.time()
            last_buffer_check = time.time()
            last_trim_time = time.time()
            last_stats_log = time.time()  # For periodic cache stats

            MAX_BATCH_SIZE = 5000
            FLUSH_INTERVAL = 10.0
            BUFFER_CHECK_INTERVAL = 1.0
            TRIM_INTERVAL = 86400  # 24 hours
            STATS_LOG_INTERVAL = 300  # 5 minutes

            while not self._stop_writer.is_set():
                now = time.time()

                # --- LOG CACHE STATS EVERY 5 MINUTES ---
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

                # Check buffer and refill if low
                if now - last_buffer_check >= BUFFER_CHECK_INTERVAL:
                    with self.buffer_lock:
                        buffer_size = len(self.log_id_buffer)
                    if buffer_size <= self.BUFFER_REFILL_THRESHOLD:
                        try:
                            self._refill_buffer(conn)
                        except Exception as e:
                            print(f"[LibLogger] Buffer refill failed: {e}")
                    last_buffer_check = now

                # Trim old logs once per day - only if enabled and retention set
                if self.ENABLE_TRIM and self.RETENTION_DAYS is not None and (now - last_trim_time >= TRIM_INTERVAL):
                    try:
                        deleted = self.trim_old_logs(days=self.RETENTION_DAYS)
                        if deleted > 0:
                            print(f"[LibLogger] Daily trim: removed {deleted} old logs")
                        last_trim_time = now
                    except Exception as e:
                        print(f"[LibLogger] ERROR: Daily trim failed: {e}")
                        # Continue running - don't stop the writer thread

                try:
                    query, params = self.write_queue.get(timeout=0.1)
                    if query is None and params is None:
                        break

                    batch.append((query, params))
                    batch_count += 1

                    if batch_count >= MAX_BATCH_SIZE or (batch_count > 0 and now - last_flush_time >= FLUSH_INTERVAL):
                        try:
                            # Autocommit mode - just execute, no BEGIN/COMMIT needed
                            for q, p in batch:
                                cursor.execute(q, p)
                            last_flush_time = now
                            if batch_count >= 20:
                                print(f"[LibLogger] Committed batch of {len(batch)} logs")
                        except Exception as e:
                            print(f"[LibLogger] Batch write error: {e}")
                            # Put batch back for retry
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

            # Flush remaining on shutdown
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

    def init(self, db_path: Optional[Path] = None, retention_days: int = None, enable_trim: bool = False) -> None:
        if self._initialized:
            return

        self.RETENTION_DAYS = retention_days
        self.ENABLE_TRIM = enable_trim

        if db_path:
            self.db_path = Path(db_path)
            print(f"[LibLogger] Database mode enabled: {self.db_path}")
            if retention_days is not None:
                print(f"[LibLogger] Retention: {retention_days} days")
            else:
                print("[LibLogger] Retention: unlimited (no trimming)")
            self._init_tables()
            self._start_writer_thread()

            # Aggressively pre-fill buffer for burst handling
            conn = self._get_temp_connection()
            try:
                for _ in range(3):  # 3 refills = 6000 IDs
                    self._refill_buffer(conn)
            finally:
                conn.close()

            with self.buffer_lock:
                print(f"[LibLogger] Database mode ready with {len(self.log_id_buffer)} reserved IDs")
        else:
            self.db_path = None
            print("[LibLogger] Journal-only mode")

        self._initialized = True
        print("[LibLogger] Initialization complete")

    def attach_to_root_logger(self, node_ip: str = "0.0.0.0",
                              source: str = "CTRL",
                              level: int = logging.DEBUG,
                              format_str: str = '%(message)s') -> None:
        """
        Attach a LibLogger handler to the root logger.
        This will capture ALL logs from the entire application.

        Args:
            node_ip: Default node_ip for logs (use '0.0.0.0' for controller)
            source: Default source identifier (e.g., 'CTRL', 'WEB')
            level: Minimum log level to capture
            format_str: Format string for log messages
        """
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
        """
        Attach a LibLogger handler to a specific logger.

        Args:
            logger: The logger to attach to
            node_ip: Default node_ip for logs
            source: Default source identifier
            level: Minimum log level to capture
            format_str: Format string for log messages
        """
        if not self._initialized:
            raise RuntimeError("[LibLogger] LibLogger not initialized. Call init() first.")

        handler = LibLoggerHandler(self, node_ip=node_ip, source=source)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(format_str))

        logger.addHandler(handler)
        self._attached_handlers.append(handler)

        print(f"[LibLogger] Attached to logger '{logger.name}' (source={source}, node_ip={node_ip})")

    def detach_all(self) -> None:
        """Remove all attached LibLogger handlers"""
        for handler in self._attached_handlers:
            # Close the handler first to prevent further logging
            handler.close()
            # Remove from root logger
            logging.root.removeHandler(handler)

        self._attached_handlers.clear()
        print("[LibLogger] Detached all handlers")

    def trim_old_logs(self, days: int = 7, batch_size: int = 1000) -> int:
        """
        Delete logs older than specified days from ALL tables.
        Now properly cleans FTS and metrics tables.
        Returns the number of rows deleted from the logs table.
        """
        if self.db_path is None:
            return 0

        try:
            conn = self._get_temp_connection()
        except Exception as e:
            print(f"[LibLogger] ERROR: Could not connect to database for trim: {e}")
            return 0

        total_deleted = 0
        total_fts_deleted = 0
        total_metrics_deleted = 0

        try:
            cursor = conn.cursor()
            cutoff = time.time() - (days * 86400)

            from datetime import datetime
            print(f"[LibLogger] Trimming logs older than {days} days (cutoff: {datetime.fromtimestamp(cutoff).isoformat()})")

            # Step 1: Get rowids to delete (for FTS cleanup)
            cursor.execute(
                "SELECT rowid FROM logs WHERE epoch_time < ? ORDER BY rowid LIMIT ?",
                (cutoff, batch_size)
            )
            rowids = [row[0] for row in cursor.fetchall()]

            if not rowids:
                print("[LibLogger] No logs to trim")
                return 0

            # Step 2: Delete from FTS first (foreign key relationship)
            placeholders = ','.join(['?' for _ in rowids])
            cursor.execute(f"DELETE FROM logs_fts WHERE rowid IN ({placeholders})", rowids)
            total_fts_deleted = cursor.rowcount
            print(f"[LibLogger] Deleted {total_fts_deleted} rows from FTS")

            # Step 3: Delete from logs
            cursor.execute(
                "DELETE FROM logs WHERE epoch_time < ? LIMIT ?",
                (cutoff, batch_size)
            )
            total_deleted = cursor.rowcount
            print(f"[LibLogger] Deleted {total_deleted} rows from logs")

            # Step 4: Clean metrics_history (if it exists)
            try:
                cursor.execute("DELETE FROM metrics_history WHERE epoch_time < ?", (cutoff,))
                total_metrics_deleted = cursor.rowcount
                if total_metrics_deleted > 0:
                    print(f"[LibLogger] Deleted {total_metrics_deleted} rows from metrics_history")
            except sqlite3.OperationalError:
                # metrics_history might not exist yet
                pass

            conn.commit()

            print(f"[LibLogger] Trim complete: logs={total_deleted}, fts={total_fts_deleted}, metrics={total_metrics_deleted}")

            return total_deleted

        except Exception as e:
            print(f"[LibLogger] ERROR: Trim operation failed: {e}")
            return total_deleted
        finally:
            try:
                conn.close()
            except Exception as e:
                print(f"[LibLogger] ERROR: Could not close database connection: {e}")

    def consistency_check(self, repair: bool = False, fast: bool = True) -> dict:
        """
        Check consistency between logs and FTS tables.

        Args:
            repair: If True, delete orphaned FTS entries to restore consistency.
            fast: If True, use fast heuristics (max rowid check). If False, do full scan.

        Returns:
            dict with counts and any discrepancies:
            {
                'log_count': int,
                'fts_count': int,
                'orphaned_fts': int,        # FTS rows with no matching log (slow mode only)
                'missing_from_fts': int,    # Log rows with no matching FTS (slow mode only)
                'consistent': bool,
                'repaired': bool,
                'message': str,
                'mode': 'fast' or 'slow' or 'fast_fallback'
            }
        """
        if self.db_path is None:
            return {'error': 'Database not available'}

        conn = self._get_temp_connection()
        try:
            cursor = conn.cursor()

            # Always get logs count (fast)
            cursor.execute("SELECT COUNT(*) FROM logs;")
            log_count = cursor.fetchone()[0]

            # Fast mode: use rowid heuristics instead of full COUNT(*)
            if fast:
                # Get the max rowid from logs and FTS
                cursor.execute("SELECT MAX(rowid) FROM logs;")
                max_log_rowid = cursor.fetchone()[0]
                if max_log_rowid is None:
                    max_log_rowid = 0

                cursor.execute("SELECT MAX(rowid) FROM logs_fts;")
                max_fts_rowid = cursor.fetchone()[0]
                if max_fts_rowid is None:
                    max_fts_rowid = 0

                # If the max rowids match, check that the last row exists in FTS
                if max_log_rowid == max_fts_rowid and max_log_rowid > 0:
                    cursor.execute("SELECT 1 FROM logs_fts WHERE rowid = ?", (max_log_rowid,))
                    exists = cursor.fetchone() is not None
                    if exists:
                        return {
                            'log_count': log_count,
                            'fts_count': log_count,  # Assume they match
                            'orphaned_fts': 0,
                            'missing_from_fts': 0,
                            'consistent': True,
                            'repaired': False,
                            'mode': 'fast',
                            'message': 'OK (max rowid match)'
                        }

                # If max rowids don't match, check recent rows
                # Count recent rows (last 10000) in logs and FTS
                min_rowid = max(max_log_rowid - 10000, 1)

                # Count logs in recent range
                cursor.execute("""
                    SELECT COUNT(*) FROM logs
                    WHERE rowid >= ? AND rowid <= ?
                """, (min_rowid, max_log_rowid))
                recent_log_count = cursor.fetchone()[0]

                # Count FTS in recent range
                cursor.execute("""
                    SELECT COUNT(*) FROM logs_fts
                    WHERE rowid >= ? AND rowid <= ?
                """, (min_rowid, max_log_rowid))
                recent_fts_count = cursor.fetchone()[0]

                # If recent rows match (or are very close), assume consistent
                if recent_log_count > 0 and recent_fts_count >= recent_log_count * 0.95:
                    return {
                        'log_count': log_count,
                        'fts_count': log_count,  # Assume they match
                        'orphaned_fts': -1,      # Unknown in fast mode
                        'missing_from_fts': -1,  # Unknown in fast mode
                        'consistent': True,
                        'repaired': False,
                        'mode': 'fast',
                        'message': f'OK (recent rows verified: {recent_fts_count}/{recent_log_count})'
                    }

                # If we get here, we're not sure - fall back to slow count
                # But print a warning and only do this once
                print("[LibLogger] Fast heuristic failed - max rowids differ")
                print(f"[LibLogger]   max_log_rowid={max_log_rowid}, max_fts_rowid={max_fts_rowid}")
                print(f"[LibLogger]   recent_log_count={recent_log_count}, recent_fts_count={recent_fts_count}")
                print("[LibLogger] Falling back to slow COUNT(*) for FTS (this may take a moment)...")

                # Slow count fallback
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

            # Slow mode: full scan (warning)
            print("[LibLogger] ⚠️  FULL CONSISTENCY SCAN STARTED - this may take several minutes")
            print(f"[LibLogger] Scanning FTS rows and log rows...")

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

            # Repair if requested and needed
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

                # Re-check after repair
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
            print(f"[LibLogger] ✅ Full consistency scan complete ({elapsed:.1f}s)")

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
        """
        Complete database rebuild - fixes corrupted indexes, rebuilds FTS, compacts.

        Args:
            rebuild_fts: If True, drop and recreate FTS table
            vacuum: If True, run VACUUM after repair

        Returns:
            dict with rebuild results
        """
        if self.db_path is None:
            return {'error': 'Database not available'}

        print("[LibLogger] ===================================================")
        print("[LibLogger] STARTING COMPLETE DATABASE REBUILD: BREW COFFEE NOW")
        print("[LibLogger] ===================================================")

        # Set low priority and store original: witihout this you risk watchdogs
        # going off during intense processing
        original_priority = self._set_low_priority()

        # Create temp directory if it doesn't exist
        import os
        temp_dir = '/mnt/ssd/tmp'
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

            # Set temp directory for this connection
            try:
                conn.execute(f"PRAGMA temp_store_directory = '{temp_dir}'")
                print(f"[LibLogger] Temp directory set to: {temp_dir}")
            except Exception as e:
                print(f"[LibLogger] Warning: Could not set temp directory: {e}")

            # Step 1: Get initial counts
            cursor.execute("SELECT COUNT(*) FROM logs;")
            results['log_count_before'] = cursor.fetchone()[0]
            print(f"[LibLogger] Initial logs count: {results['log_count_before']}")

            # Step 2: Drop corrupted indexes
            print("[LibLogger] Dropping corrupted indexes...")
            for idx_name in LOGS_INDEX_NAMES:
                try:
                    cursor.execute(f"DROP INDEX IF EXISTS {idx_name}")
                except Exception as e:
                    print(f"[LibLogger]   Warning dropping {idx_name}: {e}")
            conn.commit()
            print("[LibLogger] ✓ Indexes dropped")

            # Step 3: Recreate indexes
            print("[LibLogger] Recreating indexes...")
            for index_sql in LOGS_INDEXES:
                cursor.execute(index_sql)
                conn.commit()
                # Let the system breathe between indexes
                time.sleep(0.1)
            conn.commit()
            print("[LibLogger] ✓ Indexes recreated")

            # Step 4: Rebuild FTS if requested
            if rebuild_fts:
                print("[LibLogger] Rebuilding FTS table...")

                # Drop old FTS and triggers
                for trigger_name in LOGS_TRIGGER_NAMES:
                    try:
                        cursor.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
                    except Exception as e:
                        print(f"[LibLogger]   Warning dropping trigger {trigger_name}: {e}")

                cursor.execute("DROP TABLE IF EXISTS logs_fts")
                print("[LibLogger]   Dropped old FTS table")

                cursor.execute(FTS_TABLE_SQL)
                print("[LibLogger]   Created contentless FTS table")

                # Populate from logs in batches - handle rowid gaps properly
                print("[LibLogger]   Populating FTS from logs (this may take a moment)...")

                # Get all rowids that actually exist
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
                        # Fall back to individual inserts for this batch
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

                    # Progress every 50 batches
                    if ((i // batch_size) % 50) == 0 and i > 0:
                        percent = int(total_inserted * 100 / total_to_insert)
                        print(f"[LibLogger]     Progress: {percent}% ({total_inserted}/{total_to_insert})")

                print(f"[LibLogger]   Populated {total_inserted} rows")
                if skipped_rows:
                    print(f"[LibLogger]   Skipped {len(skipped_rows)} problematic rows")
                    if len(skipped_rows) <= 20:
                        print(f"[LibLogger]   Skipped row IDs: {skipped_rows}")
                    else:
                        print(f"[LibLogger]   First 10 skipped: {skipped_rows[:10]}...")

                # Verify FTS count
                cursor.execute("SELECT COUNT(*) FROM logs_fts;")
                results['fts_count_after'] = cursor.fetchone()[0]
                print(f"[LibLogger]   FTS count: {results['fts_count_after']}")

                # Recreate triggers
                print("[LibLogger]   Recreating triggers...")
                for trigger_sql in LOGS_TRIGGERS:
                    cursor.execute(trigger_sql)
                conn.commit()
                print("[LibLogger]   ✓ Triggers recreated")

            # Step 5: VACUUM if requested
            if vacuum:
                print("[LibLogger] Compacting database (VACUUM)...")
                print("[LibLogger] ⚠️  This will take longer than all of the other steps put together")
                try:
                    cursor.execute("VACUUM;")
                    print("[LibLogger] ✓ VACUUM complete")
                except sqlite3.OperationalError as e:
                    print(f"[LibLogger] ⚠️  VACUUM failed: {e}")
                    print("[LibLogger]    This may be due to temp directory permissions.")
                    print("[LibLogger]    You can manually run: sudo SQLITE_TMPDIR=/mnt/ssd/tmp sqlite3 /mnt/ssd/logs.db 'VACUUM;'")
                    # Don't fail the whole repair - VACUUM is optional

            # Step 6: Final verification
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

            print("[LibLogger] =========================================")
            print("[LibLogger] REBUILD COMPLETE")
            print(f"[LibLogger]   Logs count: {results['log_count_after']}")
            if rebuild_fts:
                print(f"[LibLogger]   FTS count:  {results['fts_count_after']}")
            print(f"[LibLogger]   Freelist:  {results['freelist']}")
            print(f"[LibLogger]   Quick check: {results['quick_check']}")
            print(f"[LibLogger]   Consistent: {results['consistent']}")
            print("[LibLogger] =========================================")

            return results

        except Exception as e:
            print(f"[LibLogger] ERROR during rebuild: {e}")
            import traceback
            traceback.print_exc()
            return {'error': str(e)}
        finally:
            # Always restore priority, even if an exception occurs
            self._restore_priority(original_priority)
            conn.close()

    def log(self, source: str, node_ip: str, message: str,
            log_level: int = 1, log_tag: str = None,
            message_type: str = "LOG") -> int:
        """Write a log entry - called by ANY thread."""
        if not self._initialized:
            raise RuntimeError("[LibLogger] LibLogger not initialized")

        # Don't accept new logs during shutdown
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
        if HAS_SYSTEMD:
            systemd.journal.send(message, SYSLOG_IDENTIFIER='fgr-log-server',
                                 PRIORITY=log_level, FGR_SOURCE='ADMIN')

    def is_db_available(self) -> bool:
        return self.db_path is not None and self.write_queue is not None

    def get_logs_by_log_id(self, target_log_id: int, before: int = 100, after: int = 100) -> List[Dict]:
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

    def execute_sql(self, query: str, params: tuple = ()) -> Optional[List[Dict]]:
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

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        Gracefully shut down LibLogger and all attached handlers.
        """
        if self.db_path is None:
            print("[LibLogger] Journal-only mode, nothing to shut down")
            return

        print("[LibLogger] Shutting down...")

        # 1. Detach all handlers FIRST to prevent new logs from entering
        self.detach_all()

        # 2. Stop accepting new work
        self._stop_writer.set()

        # 3. Send sentinel to writer thread
        if self.write_queue:
            try:
                self.write_queue.put_nowait((None, None))
            except queue.Full:
                pass

        # 4. Wait for writer thread to finish processing queued logs
        if self.writer_thread and self.writer_thread.is_alive():
            pending = self.write_queue.qsize() if self.write_queue else 0
            if pending > 0:
                print(f"[LibLogger] Waiting for {pending} queued logs...")

            self.writer_thread.join(timeout=timeout)

            if self.writer_thread.is_alive():
                print("[LibLogger] WARNING: Writer thread did not stop within timeout")
            else:
                print("[LibLogger] Writer thread stopped cleanly")

        # 5. Close database connection
        self._close_connection()

        print("[LibLogger] Shutdown complete")