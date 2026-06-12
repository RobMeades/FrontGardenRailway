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
LibLogger - Shared logging module for FGR system.

Provides a single source of truth for writing logs to both SQLite database
and systemd journal, with log_id as the universal identifier.

MODES:
- Database mode (--db-path provided): Logs go to both database and journal.
  log_ids are reserved from database sequence for correlation.

- Journal-only mode (no --db-path): Logs go ONLY to journal.
  No log_id coordination needed - simply omit the field.

CRITICAL: In database mode, log_id is ALWAYS a sequential integer from the
database. NO FALLBACK IDS. If a log_id cannot be obtained, an exception is raised.

MULTI-WRITER COMPATIBILITY:
- Uses BEGIN DEFERRED (not IMMEDIATE) for cooperative locking
- WAL mode enabled for concurrent reads
- Short transactions (50 logs or 1 second)
- Retries on lock conflicts
"""

import sqlite3
import threading
import time
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

# Import systemd journal with fallback
try:
    import systemd.journal
    HAS_SYSTEMD = True
except ImportError:
    HAS_SYSTEMD = False
    print("[LibLogger] Warning: python-systemd not installed, journal logging disabled")


class LibLogger:
    """
    Shared logger for database and journal.

    In database mode: Uses log_id (from database sequence) as universal identifier.
    In journal-only mode: Simply writes to journal without IDs.
    """

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

            # Track refill threads for clean shutdown
            self._refill_threads = []
            self._refill_threads_lock = threading.Lock()

            # Serialize ID reservations (SQLite doesn't allow concurrent writes)
            self._reserve_lock = threading.Lock()

            # Database mode only attributes (only used if db_path is set)
            self.log_id_buffer = []
            self.log_id_lock = threading.Lock()
            self.batch_size = 100
            self.refill_threshold = 50
            self._refill_in_progress = False

            # Thread-local storage for database connections
            self._thread_local = threading.local()

    def init(self, db_path: Optional[Path] = None) -> None:
        """
        Initialize the logger.

        Args:
            db_path: If provided, enables database mode with log_id correlation.
                     If None, runs in journal-only mode (no database writes, no IDs).
        """
        if self._initialized:
            return

        if db_path:
            # Database mode
            self.db_path = Path(db_path)
            print(f"[LibLogger] Database mode enabled: {self.db_path}")
            self._init_tables()
            self._start_writer_thread()
            self._refill_log_id_buffer_sync()  # Runs in main thread, must succeed
            print(f"[LibLogger] Database mode ready with {len(self.log_id_buffer)} reserved IDs")
        else:
            # Journal-only mode
            self.db_path = None
            print("[LibLogger] Journal-only mode (no database, no log_id coordination)")

        self._initialized = True
        print("[LibLogger] Initialization complete")

    def _get_connection(self):
        """
        Get a database connection for the current thread.
        Creates one per thread and reuses it.
        """
        if not hasattr(self._thread_local, 'conn') or self._thread_local.conn is None:
            self._thread_local.conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
            self._thread_local.conn.execute("PRAGMA journal_mode=WAL")
            self._thread_local.conn.execute("PRAGMA synchronous=NORMAL")
            # Cooperative timeout for multi-writer scenarios
            self._thread_local.conn.execute("PRAGMA busy_timeout=10000")
        return self._thread_local.conn

    def _close_connection(self):
        """Close the database connection for the current thread."""
        if hasattr(self._thread_local, 'conn') and self._thread_local.conn is not None:
            try:
                self._thread_local.conn.close()
            except Exception:
                pass
            finally:
                self._thread_local.conn = None

    def _refill_log_id_buffer_sync(self):
        """
        Synchronously reserve a batch of log_ids from the database sequence.
        Uses a global lock to serialize reservations across threads.

        Raises RuntimeError if IDs cannot be reserved.
        """
        if self.db_path is None:
            raise RuntimeError("Cannot refill log_ids in journal-only mode")

        # Serialize all ID reservations - only one thread at a time
        with self._reserve_lock:
            # Double-check if buffer was refilled while we were waiting for the lock
            with self.log_id_lock:
                if len(self.log_id_buffer) > self.refill_threshold:
                    print(f"[LibLogger] Buffer already refilled by another thread, skipping")
                    return

            conn = self._get_connection()
            cursor = conn.cursor()

            # Retry with exponential backoff - but NEVER fall back to fake IDs
            for attempt in range(10):  # 10 attempts, ~51 seconds total
                try:
                    cursor.execute("""
                        UPDATE global_sequence
                        SET next_seq = next_seq + ?
                        WHERE id = 1
                        RETURNING next_seq - ? + 1, next_seq
                    """, (self.batch_size, self.batch_size))

                    result = cursor.fetchone()
                    if result:
                        start, end = result
                        new_ids = list(range(start, end + 1))

                        with self.log_id_lock:
                            self.log_id_buffer.extend(new_ids)
                            buffer_len = len(self.log_id_buffer)

                        print(f"[LibLogger] Reserved log_ids {start}-{end} (buffer now: {buffer_len})")
                        return
                    else:
                        # Should never happen - insert initial row and retry
                        cursor.execute("INSERT INTO global_sequence (id, next_seq) VALUES (1, 1)")
                        continue

                except sqlite3.OperationalError as e:
                    if "locked" in str(e) and attempt < 9:
                        wait_time = 0.05 * (2 ** attempt)  # 50ms, 100ms, 200ms, 400ms, 800ms, 1.6s, 3.2s, 6.4s, 12.8s
                        print(f"[LibLogger] Database locked, retrying in {wait_time:.3f}s (attempt {attempt+1}/10)")
                        time.sleep(wait_time)
                        continue
                    # Failed to get lock after all retries
                    raise RuntimeError(f"Failed to reserve log_ids after {attempt+1} attempts: {e}")
                except Exception as e:
                    raise RuntimeError(f"Failed to reserve log_ids: {e}")

            # Should never get here
            raise RuntimeError("Failed to reserve log_ids after maximum retries")

    def _refill_log_id_buffer_sync_wrapper(self):
        """Wrapper that ensures cleanup of thread tracking and connection."""
        try:
            self._refill_log_id_buffer_sync()
        except Exception as e:
            print(f"[LibLogger] Refill thread failed: {e}")
            # Re-raise to make thread crash visible
            raise
        finally:
            # Remove from tracking
            with self._refill_threads_lock:
                current = threading.current_thread()
                if current in self._refill_threads:
                    self._refill_threads.remove(current)
            # Clear refill flag
            with self.log_id_lock:
                self._refill_in_progress = False
            # Close this thread's database connection
            self._close_connection()

    def _ensure_log_id_buffer(self):
        """
        Ensure buffer has IDs available.
        Raises RuntimeError if IDs cannot be obtained.
        """
        if self.db_path is None:
            raise RuntimeError("Cannot get log_id in journal-only mode")

        # Fast path - if we have enough, return immediately
        with self.log_id_lock:
            if len(self.log_id_buffer) > self.refill_threshold:
                return

        # Need more IDs. Try to start a background refill if not already in progress
        with self.log_id_lock:
            if self._refill_in_progress:
                # Another thread is already refilling, wait for it
                pass
            elif len(self.log_id_buffer) > 0:
                # Buffer is low but not empty - start async refill
                self._refill_in_progress = True
                refill_thread = threading.Thread(
                    target=self._refill_log_id_buffer_sync_wrapper,
                    daemon=False,
                    name="LibLogger-Refill"
                )
                with self._refill_threads_lock:
                    self._refill_threads.append(refill_thread)
                refill_thread.start()
                return
            else:
                # Buffer is EMPTY - need synchronous refill
                pass

        # Buffer is empty - do synchronous refill (this blocks)
        with self.log_id_lock:
            # Double-check buffer after potential lock wait
            if len(self.log_id_buffer) > 0:
                return
            # Double-check refill flag
            if self._refill_in_progress:
                # Wait for async refill to complete
                time.sleep(0.01)
                if len(self.log_id_buffer) > 0:
                    return
            self._refill_in_progress = True

        try:
            print(f"[LibLogger] Buffer empty, performing synchronous refill")
            self._refill_log_id_buffer_sync()
        finally:
            with self.log_id_lock:
                self._refill_in_progress = False

        # After refill, verify we have IDs
        with self.log_id_lock:
            if not self.log_id_buffer:
                raise RuntimeError("Failed to refill log_id buffer - database sequence unavailable")

    def _get_next_log_id(self) -> int:
        """
        Get next log_id. ALWAYS returns a valid sequential ID.
        Raises RuntimeError if no IDs available.
        """
        if self.db_path is None:
            raise RuntimeError("Cannot get log_id in journal-only mode")

        self._ensure_log_id_buffer()

        with self.log_id_lock:
            if self.log_id_buffer:
                return self.log_id_buffer.pop(0)
            else:
                raise RuntimeError("No log_ids available - database sequence is broken")

    def _start_writer_thread(self):
        """Start the single database writer thread (database mode only)."""
        if self.db_path is None:
            return

        self.write_queue = queue.Queue(maxsize=50000)
        self._stop_writer.clear()

        def writer_loop():
            """Writer thread with cooperative locking for multi-writer scenarios."""
            try:
                conn = self._get_connection()
                # Cooperative timeout for multi-writer scenarios
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute("PRAGMA wal_autocheckpoint=500")
                cursor = conn.cursor()

                print("[LibLogger] Writer thread started")
                batch = []
                batch_count = 0
                last_flush_time = time.time()

                # Settings for your traffic pattern
                MAX_BATCH_SIZE = 50      # Flush when we reach this many
                FLUSH_INTERVAL = 1.0     # Flush every 1 second

                while not self._stop_writer.is_set():
                    try:
                        query, params = self.write_queue.get(timeout=0.1)

                        if query is None and params is None:
                            print("[LibLogger] Writer thread received shutdown signal")
                            break

                        batch.append((query, params))
                        batch_count += 1
                        now = time.time()

                        # Flush if: batch full OR (any logs AND it's been FLUSH_INTERVAL seconds)
                        if (batch_count >= MAX_BATCH_SIZE or
                            (batch_count > 0 and (now - last_flush_time) >= FLUSH_INTERVAL)):
                            # Small delay before retry if we were locked before
                            retry_delay = 0
                            max_retries = 3
                            for retry in range(max_retries):
                                try:
                                    if retry > 0:
                                        time.sleep(retry_delay)
                                        retry_delay = 0.05 * (2 ** retry)

                                    # Use BEGIN DEFERRED for cooperative locking
                                    conn.execute("BEGIN")
                                    for q, p in batch:
                                        cursor.execute(q, p)
                                    conn.commit()
                                    last_flush_time = now
                                    if batch_count >= 10:
                                        print(f"[LibLogger] Committed batch of {len(batch)} logs")
                                    break  # Success
                                except sqlite3.OperationalError as e:
                                    conn.rollback()
                                    if "locked" in str(e) and retry < max_retries - 1:
                                        print(f"[LibLogger] Database busy, retrying batch of {len(batch)} logs (attempt {retry+1}/{max_retries})")
                                        continue
                                    else:
                                        print(f"[LibLogger] Batch write error after {retry+1} attempts: {e}")
                                        # Put batch back in queue for later
                                        for q, p in reversed(batch):
                                            try:
                                                self.write_queue.put_nowait((q, p))
                                            except queue.Full:
                                                pass
                                        break
                                except Exception as e:
                                    conn.rollback()
                                    print(f"[LibLogger] Batch write error: {e}")
                                    break
                            batch = []
                            batch_count = 0

                    except queue.Empty:
                        # No new logs - flush anything pending immediately
                        if batch_count > 0:
                            try:
                                conn.execute("BEGIN")
                                for q, p in batch:
                                    cursor.execute(q, p)
                                conn.commit()
                                # Don't log tiny flushes
                            except sqlite3.OperationalError as e:
                                conn.rollback()
                                if "locked" in str(e):
                                    # Put batch back for later
                                    for q, p in reversed(batch):
                                        try:
                                            self.write_queue.put_nowait((q, p))
                                        except queue.Full:
                                            pass
                                else:
                                    print(f"[LibLogger] Idle flush error: {e}")
                            except Exception as e:
                                conn.rollback()
                                print(f"[LibLogger] Idle flush error: {e}")
                            finally:
                                batch = []
                                batch_count = 0
                        continue
                    except Exception as e:
                        print(f"[LibLogger] Writer thread error: {e}")
                        time.sleep(0.1)

                # Final flush on shutdown
                if batch:
                    try:
                        conn.execute("BEGIN")
                        for q, p in batch:
                            cursor.execute(q, p)
                        conn.commit()
                        print(f"[LibLogger] Final flush: {len(batch)} logs")
                    except Exception as e:
                        conn.rollback()
                        print(f"[LibLogger] Final flush error: {e}")

            except Exception as e:
                print(f"[LibLogger] Writer thread fatal error: {e}")
            finally:
                # Close this thread's connection
                self._close_connection()
                print("[LibLogger] Writer thread stopped")

        self.writer_thread = threading.Thread(target=writer_loop, daemon=False, name="LibLogger-Writer")
        self.writer_thread.start()

    def _init_tables(self) -> None:
        """Create necessary tables (database mode only)."""
        if self.db_path is None:
            return

        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS logs (
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

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_node_ip ON logs(node_ip)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_epoch ON logs(epoch_time)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_tag ON logs(log_tag)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_epoch_real ON logs(epoch_time_real)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_logs_message_type ON logs(message_type)")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_logs_log_id ON logs(log_id)")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS global_sequence (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    next_seq INTEGER NOT NULL DEFAULT 1
                )
            """)
            cursor.execute("INSERT OR IGNORE INTO global_sequence (id, next_seq) VALUES (1, 1)")

            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS logs_fts USING fts5(message)
            """)

            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS logs_ai AFTER INSERT ON logs BEGIN
                    INSERT INTO logs_fts(rowid, message) VALUES (new.rowid, new.message);
                END
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS logs_ad AFTER DELETE ON logs BEGIN
                    INSERT INTO logs_fts(logs_fts, rowid, message) VALUES ('delete', old.rowid, old.message);
                END
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS logs_au AFTER UPDATE ON logs BEGIN
                    INSERT INTO logs_fts(logs_fts, rowid, message) VALUES ('delete', old.rowid, old.message);
                    INSERT INTO logs_fts(rowid, message) VALUES (new.rowid, new.message);
                END
            """)

            conn.commit()
            print("[LibLogger] Tables ready")
        except Exception as e:
            print(f"[LibLogger] Failed to initialize tables: {e}")
            self.db_path = None
            raise
        # Don't close connection - keep it for this thread (main thread)

    def log(self,
            source: str,
            node_ip: str,
            message: str,
            log_level: int = 1,
            log_tag: str = None,
            message_type: str = "LOG") -> int:
        """
        Write a log entry.

        Returns:
            log_id (always int in database mode)

        Raises:
            RuntimeError: If in database mode and log_id cannot be obtained
        """
        if not self._initialized:
            raise RuntimeError("LibLogger not initialized. Call init() first.")

        # Get log_id - will raise RuntimeError if unavailable
        log_id = self._get_next_log_id() if self.db_path is not None else None

        # Database write (only if in database mode)
        if self.db_path is not None and self.write_queue:
            timestamp = time.time()
            query = """
                INSERT INTO logs (
                    log_id, node_ip, timestamp_utc, epoch_time, log_level, log_tag,
                    message_type, message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            params = (
                log_id,
                node_ip,
                datetime.fromtimestamp(timestamp).isoformat() + 'Z',
                timestamp,
                log_level,
                log_tag,
                message_type,
                message
            )

            try:
                self.write_queue.put_nowait((query, params))
            except queue.Full:
                # Queue full - this is a critical error
                raise RuntimeError(f"Log queue full (50000), cannot accept log_id {log_id}")

        # Journal write (always, but only if we have a log_id)
        if HAS_SYSTEMD:
            extra_fields = {
                'SYSLOG_IDENTIFIER': 'fgr-log-server',
                'PRIORITY': log_level,
                'FGR_SOURCE': source,
                'FGR_NODE_IP': node_ip,
                'FGR_MESSAGE_TYPE': message_type,
            }

            if log_id is not None:
                extra_fields['FGR_LOG_ID'] = str(log_id)

            if log_tag:
                extra_fields['FGR_LOG_TAG'] = log_tag

            systemd.journal.send(message, **extra_fields)
        else:
            # Fallback to console
            id_str = f"[{log_id}]" if log_id is not None else "[NO_ID]"
            print(f"[{source}] {id_str} {message}")

        return log_id

    def admin_log(self, message: str, log_level: int = 6) -> None:
        """
        Admin-only log - goes to journal only, never database.
        """
        if HAS_SYSTEMD:
            extra_fields = {
                'SYSLOG_IDENTIFIER': 'fgr-log-server',
                'PRIORITY': log_level,
                'FGR_SOURCE': 'ADMIN',
            }
            systemd.journal.send(message, **extra_fields)
        else:
            print(f"[ADMIN] {message}")

    def is_db_available(self) -> bool:
        """Return True if in database mode and ready for queries."""
        return self.db_path is not None and self.write_queue is not None

    def get_logs_by_log_id(self, target_log_id: int,
                           before: int = 100, after: int = 100) -> List[Dict[str, Any]]:
        """
        Retrieve logs around a specific log_id (database mode only).
        Returns empty list if in journal-only mode.
        """
        if self.db_path is None:
            print("[LibLogger] Warning: get_logs_by_log_id called in journal-only mode")
            return []

        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            start_id = max(1, target_log_id - before)
            end_id = target_log_id + after

            cursor.execute("""
                SELECT log_id, epoch_time, message, node_ip, log_level, log_tag, message_type
                FROM logs
                WHERE log_id BETWEEN ? AND ?
                ORDER BY log_id ASC
            """, (start_id, end_id))

            return [dict(row) for row in cursor.fetchall()]
        finally:
            pass

    def get_logs_by_timestamp(self, timestamp: float,
                              before: int = 100, after: int = 100) -> List[Dict[str, Any]]:
        """
        Retrieve logs around a specific timestamp (database mode only).
        Returns empty list if in journal-only mode.
        """
        if self.db_path is None:
            print("[LibLogger] Warning: get_logs_by_timestamp called in journal-only mode")
            return []

        conn = self._get_connection()
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT log_id
                FROM logs
                WHERE epoch_time <= ?
                ORDER BY epoch_time DESC
                LIMIT 1
            """, (timestamp,))

            row = cursor.fetchone()
            if not row:
                return []

            closest_id = row['log_id']
            return self.get_logs_by_log_id(closest_id, before, after)
        finally:
            pass

    def execute_sql(self, query: str, params: tuple = ()) -> Optional[List[Dict]]:
        """
        Execute a read-only SQL query (database mode only).
        Returns None in journal-only mode.
        """
        if self.db_path is None:
            return None

        conn = self._get_connection()
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
            pass

    def shutdown(self, timeout: float = 10.0) -> None:
        """
        Gracefully shut down (database mode only).
        """
        if self.db_path is None:
            print("[LibLogger] Journal-only mode, nothing to shut down")
            return

        print("[LibLogger] Shutting down...")
        start_time = time.time()

        # 1. Stop accepting new work
        self._stop_writer.set()

        # 2. Wait for pending refill threads to complete
        with self._refill_threads_lock:
            active_refills = list(self._refill_threads)

        if active_refills:
            remaining_timeout = timeout - (time.time() - start_time)
            if remaining_timeout > 0:
                print(f"[LibLogger] Waiting for {len(active_refills)} refill threads...")
                for thread in active_refills:
                    thread.join(timeout=remaining_timeout / len(active_refills))

        # 3. Shutdown writer thread
        if self.writer_thread and self.writer_thread.is_alive():
            pending = self.write_queue.qsize() if self.write_queue else 0
            print(f"[LibLogger] Writer thread: {pending} pending writes")

            if self.write_queue:
                try:
                    self.write_queue.put_nowait((None, None))
                except queue.Full:
                    pass

            remaining_timeout = timeout - (time.time() - start_time)
            if remaining_timeout > 0:
                self.writer_thread.join(timeout=remaining_timeout)

            if self.writer_thread.is_alive():
                print("[LibLogger] WARNING: Writer thread did not stop within timeout")
            else:
                print("[LibLogger] Writer thread stopped cleanly")

        # 4. Close main thread's connection
        self._close_connection()

        print("[LibLogger] Shutdown complete")