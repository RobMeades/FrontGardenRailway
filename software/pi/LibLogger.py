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
- Writer thread owns the ONLY database connection
- Client threads NEVER touch the database
- log_ids come from a shared buffer (deque)
- Writer thread refills buffer before it gets low
- No per-thread connections, no connection leaks
"""

import sqlite3
import threading
import time
import queue
import collections
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

try:
    import systemd.journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("[LibLogger] Warning: python-systemd not installed")


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

            # Shared buffer for log_ids (deque is thread-safe for append/popleft)
            self.log_id_buffer = collections.deque()
            self.buffer_lock = threading.Lock()

            # Buffer settings - large enough to handle bursts
            self.BUFFER_RESERVE_SIZE = 2000
            self.BUFFER_REFILL_THRESHOLD = 1000

    def init(self, db_path: Optional[Path] = None) -> None:
        if self._initialized:
            return

        if db_path:
            self.db_path = Path(db_path)
            print(f"[LibLogger] Database mode enabled: {self.db_path}")
            self._init_tables()
            self._start_writer_thread()

            # Aggressively pre-fill buffer for burst handling
            conn = sqlite3.connect(str(self.db_path), timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
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

    def _init_tables(self) -> None:
        """Create tables if needed - uses temporary connection."""
        if self.db_path is None:
            return

        # Temporary connection - closed after use
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        cursor = conn.cursor()

        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='logs'")
            if cursor.fetchone():
                # Tables exist - ensure sequence has initial row
                cursor.execute("INSERT OR IGNORE INTO global_sequence (id, next_seq) VALUES (1, 1)")
                conn.commit()
                print("[LibLogger] Database schema already exists")
                return

            print("[LibLogger] Creating database schema...")

            cursor.execute("""
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
            """)

            cursor.execute("CREATE INDEX idx_logs_node_ip ON logs(node_ip)")
            cursor.execute("CREATE INDEX idx_logs_epoch ON logs(epoch_time)")
            cursor.execute("CREATE INDEX idx_logs_log_id ON logs(log_id)")

            cursor.execute("""
                CREATE TABLE global_sequence (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_seq INTEGER NOT NULL DEFAULT 1
                )
            """)
            cursor.execute("INSERT INTO global_sequence (id, next_seq) VALUES (1, 1)")

            cursor.execute("CREATE VIRTUAL TABLE logs_fts USING fts5(message)")

            cursor.execute("""
                CREATE TRIGGER logs_ai AFTER INSERT ON logs BEGIN
                    INSERT INTO logs_fts(rowid, message) VALUES (new.rowid, new.message);
                END
            """)

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
                        print(f"[LibLogger] Refilled buffer: {start}-{end} (now {len(self.log_id_buffer)} IDs)")
                        return
                    else:
                        cursor.execute("INSERT INTO global_sequence (id, next_seq) VALUES (1, 1)")
                        continue
                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 2:
                        time.sleep(0.05 * (2 ** attempt))
                        continue
                    raise

    def _get_next_log_id(self) -> int:
        """Get next log_id from buffer - called by ANY thread."""
        if self.db_path is None:
            raise RuntimeError("Cannot get log_id in journal-only mode")

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

        raise RuntimeError("No log_ids available - buffer underrun")

    def _start_writer_thread(self):
        """Start the writer thread - owns the ONLY database connection."""
        self.write_queue = queue.Queue(maxsize=50000)
        self._stop_writer.clear()

        def writer_loop():
            # Use autocommit mode to avoid nested transaction errors
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=10000")
            cursor = conn.cursor()

            print("[LibLogger] Writer thread started")
            batch = []
            batch_count = 0
            last_flush_time = time.time()
            last_buffer_check = time.time()

            MAX_BATCH_SIZE = 100
            FLUSH_INTERVAL = 1.0
            BUFFER_CHECK_INTERVAL = 1.0

            while not self._stop_writer.is_set():
                now = time.time()

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

            conn.close()
            print("[LibLogger] Writer thread stopped")

        self.writer_thread = threading.Thread(target=writer_loop, daemon=False, name="LibLogger-Writer")
        self.writer_thread.start()

    def log(self, source: str, node_ip: str, message: str,
            log_level: int = 1, log_tag: str = None,
            message_type: str = "LOG") -> int:
        """Write a log entry - called by ANY thread."""
        if not self._initialized:
            raise RuntimeError("LibLogger not initialized")

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
                raise RuntimeError(f"Queue full, log_id {log_id} lost")

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

        return log_id

    def admin_log(self, message: str, log_level: int = 6) -> None:
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
        if self.db_path is None:
            return

        print("[LibLogger] Shutting down...")
        self._stop_writer.set()

        if self.write_queue:
            try:
                self.write_queue.put_nowait((None, None))
            except queue.Full:
                pass

        if self.writer_thread and self.writer_thread.is_alive():
            self.writer_thread.join(timeout=2.0)
            if self.writer_thread.is_alive():
                print("[LibLogger] WARNING: Writer thread did not stop")
            else:
                print("[LibLogger] Writer thread stopped cleanly")

        print("[LibLogger] Shutdown complete")