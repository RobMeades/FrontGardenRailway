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

# Written by Google Gemini, enhanced significantly by Deep Seek :-)

import sqlite3
import argparse
import sys
import signal
import time
import subprocess
import tempfile
import os
import re
from datetime import datetime, timedelta

DATABASE_PATH = "/mnt/ssd/logs.db"

# Global flag for signal handling
exiting = False

def signal_handler(sig, frame):
    """Handle Ctrl+C gracefully."""
    global exiting
    if exiting:
        return
    exiting = True
    print("\n\nInterrupted by user. Exiting...", file=sys.stderr)
    sys.exit(1)

def debug_print(debug_enabled, *args, **kwargs):
    """Print debug messages if debug is enabled."""
    if debug_enabled:
        print("[DEBUG]", *args, **kwargs, file=sys.stderr)

def parse_date(date_str):
    """Parse a human-readable date string into a datetime object."""
    if not date_str:
        return None

    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d"
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    raise ValueError(f"Unable to parse date: '{date_str}'. Use format YYYY-MM-DD or YYYY-MM-DD HH:MM")

def extract_esp_timestamp(message):
    """Extract ESP-IDF timestamp from message if present.
    ESP-IDF format: "I (1234) tag: message" where 1234 is milliseconds since boot.
    """
    # Look for pattern like "W (9115083)" or "I (9122073)"
    match = re.search(r'[A-Z] \((\d+)\)', message)
    if match:
        return int(match.group(1))
    return None

def calibrate_node_time(rows, debug=False):
    """
    Calibrate ESP timestamps to real time.
    ESP timestamps are in milliseconds since boot.
    Returns a dict with node_ip -> boot_time (datetime object)
    """
    calibration = {}
    node_data = {}

    for row in rows:
        timestamp, tag, msg_type, message, node_ip = row
        esp_ts = extract_esp_timestamp(message)

        if esp_ts is not None:
            try:
                # Parse the server timestamp
                server_time_str = timestamp.replace('T', ' ').replace('Z', '')
                if '.' in server_time_str:
                    server_time = datetime.strptime(server_time_str, "%Y-%m-%d %H:%M:%S.%f")
                else:
                    server_time = datetime.strptime(server_time_str, "%Y-%m-%d %H:%M:%S")

                if node_ip not in node_data:
                    node_data[node_ip] = []
                node_data[node_ip].append((esp_ts, server_time))
            except ValueError:
                debug_print(debug, f"Could not parse server timestamp: {timestamp}")
                continue

    for node_ip, samples in node_data.items():
        if not samples:
            continue

        # Sort by ESP timestamp to find the earliest sample (closest to boot)
        samples.sort(key=lambda x: x[0])

        # Use the earliest sample for calibration
        esp_ts_ms, server_time = samples[0]

        # ESP timestamps are in milliseconds since boot
        esp_seconds = esp_ts_ms / 1000.0

        # Calculate boot time: server_time - esp_seconds
        boot_time = server_time - timedelta(seconds=esp_seconds)

        calibration[node_ip] = boot_time

        debug_print(debug, f"Calibrated {node_ip}: boot_time={boot_time}, esp_ts={esp_ts_ms}ms")

    return calibration

def adjust_timestamp(server_timestamp, message, calibration, node_ip):
    """Adjust the timestamp if calibration is available."""
    esp_ts = extract_esp_timestamp(message)

    if esp_ts is not None and node_ip in calibration:
        boot_time = calibration[node_ip]
        esp_seconds = esp_ts / 1000.0  # Convert milliseconds to seconds
        real_time = boot_time + timedelta(seconds=esp_seconds)
        return real_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Keep 3 decimal places

    # Fall back to server timestamp
    return server_timestamp

def ensure_index_exists(db_path, debug=False):
    """Check if the composite index exists and create it if it doesn't."""
    index_name = "idx_logs_node_ip_epoch"
    try:
        debug_print(debug, f"Checking for composite index {index_name}...")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        # Check if the index exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
        exists = cursor.fetchone() is not None

        if not exists:
            print(f"\n" + "=" * 70, file=sys.stderr)
            print(f"PERFORMANCE OPTIMIZATION: Creating composite index for faster queries", file=sys.stderr)
            print(f"=" * 70, file=sys.stderr)
            print(f"Index: {index_name}", file=sys.stderr)
            print(f"On:    logs(node_ip, epoch_time DESC)", file=sys.stderr)
            print(f"\nThis will make queries significantly faster.", file=sys.stderr)

            try:
                # Try to create the index
                print(f"\nAttempting to create index...", file=sys.stderr)
                cursor.execute(f"CREATE INDEX {index_name} ON logs(node_ip, epoch_time DESC);")
                conn.commit()
                print(f"✓ Index created successfully!", file=sys.stderr)
                print(f"=" * 70 + "\n", file=sys.stderr)
                debug_print(debug, "Index created successfully")
                conn.close()
                return True, True
            except sqlite3.OperationalError as e:
                if "readonly" in str(e).lower():
                    print(f"\n✗ Cannot create index - database is read-only.", file=sys.stderr)
                    print(f"\n" + "=" * 70, file=sys.stderr)
                    print(f"To enable fast queries, please run this command once with sudo:", file=sys.stderr)
                    print(f"=" * 70, file=sys.stderr)
                    print(f"  sudo sqlite3 {db_path} \"CREATE INDEX {index_name} ON logs(node_ip, epoch_time DESC);\"", file=sys.stderr)
                    print(f"=" * 70, file=sys.stderr)
                    print(f"\nAfter creating the index, you can run this script normally.", file=sys.stderr)
                    print(f"(This is a one-time setup - the index will persist.)\n", file=sys.stderr)
                    conn.close()
                    return False, False
                else:
                    raise

        conn.close()
        return True, True

    except Exception as e:
        print(f"WARNING: Could not ensure index exists: {e}", file=sys.stderr)
        debug_print(debug, f"Index check failed: {e}")
        return False, False

def run_sqlite_query(db_path, query, params, timeout_seconds, debug=False):
    """Run a SQLite query in a subprocess with timeout."""
    # Build the command with parameters
    cmd = ['sqlite3', db_path]

    # Add each parameter as a .param set command
    for i, param in enumerate(params):
        cmd.extend(['-cmd', f'.param set :p{i} "{param}"'])

    # Replace ? with :p0, :p1, etc.
    modified_query = query
    for i in range(len(params)):
        modified_query = modified_query.replace('?', f':p{i}', 1)

    # Add the query as the last argument
    cmd.append(modified_query)

    debug_print(debug, f"Running sqlite3 with {len(params)} parameters")

    # Start the process
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:
        # Wait for the process with timeout
        stdout, stderr = process.communicate(timeout=timeout_seconds)

        if process.returncode != 0:
            raise Exception(f"SQLite error (code {process.returncode}): {stderr}")

        # Parse output - each line is a row with pipe-separated values
        if stdout.strip():
            rows = []
            for line in stdout.strip().split('\n'):
                # Split by '|' (sqlite3 default separator)
                row = line.split('|')
                rows.append(tuple(row))
            return rows
        else:
            return []

    except subprocess.TimeoutExpired:
        # Kill the process
        process.kill()
        process.wait()
        raise TimeoutError(f"Query timed out after {timeout_seconds} seconds")

def get_severity_char(tag, msg_type):
    """Convert log tag and type to a single severity character."""
    # Map tags to severity
    if tag in ('BACKTRACE', 'STACK_OVERFLOW'):
        return 'X'  # Critical/Error
    elif tag == 'ERROR':
        return 'E'
    elif tag == 'WARN':
        return 'W'
    elif tag == 'INFO':
        return 'I'
    elif tag == 'DEBUG':
        return 'D'
    elif msg_type == 'METRIC':
        return 'M'
    else:
        return '?'

def main():
    global exiting

    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(
        description="Query FGR log database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  {sys.argv[0]} 10.10.3.2                    # Show all logs for node 10.10.3.2
  {sys.argv[0]} 10.10.3.2 -c                 # Show logs for node + controller
  {sys.argv[0]} 10.10.3.2 -n 50              # Show last 50 logs for node
  {sys.argv[0]} 10.10.3.2 -c -n 100          # Show last 100 logs for node + controller
  {sys.argv[0]} 10.10.3.2 -s "2026-06-01"    # Show logs from June 1, 2026 onwards
  {sys.argv[0]} 10.10.3.2 -s "2026-06-01 10:00" -e "2026-06-01 18:00"  # Time range
  {sys.argv[0]} 10.10.3.2 -a                 # Adjust timestamps to real time using ESP timestamps

Note: First run may require sudo to create an index for better performance.
      This is a one-time setup - subsequent runs will not need sudo.
        """
    )

    parser.add_argument(
        "ip",
        type=str,
        help="Target node IP address to filter logs by (e.g., 10.10.3.2)"
    )

    parser.add_argument(
        "--db", "-d",
        type=str,
        default=DATABASE_PATH,
        help=f"Path to unified SQLite database file (default: {DATABASE_PATH})"
    )

    parser.add_argument(
        "--start-date", "-s",
        type=str,
        default=None,
        help="Start date filter (format: YYYY-MM-DD or YYYY-MM-DD HH:MM) (default: None - no start filter)"
    )

    parser.add_argument(
        "--end-date", "-e",
        type=str,
        default=None,
        help="End date filter (format: YYYY-MM-DD or YYYY-MM-DD HH:MM) (default: None - no end filter)"
    )

    parser.add_argument(
        "--count", "-n",
        type=int,
        default=None,
        help="Limit number of rows returned (default: None - returns all matching logs)"
    )

    parser.add_argument(
        "-c",
        action="store_true",
        default=False,
        help="Include logs from the controller (0.0.0.0) as well as the specified node (default: False - node only)"
    )

    parser.add_argument(
        "-a",
        action="store_true",
        default=False,
        help="Adjust timestamps to real time using ESP-IDF timestamps (calibrates against server arrival time)"
    )

    parser.add_argument(
        "--timeout", "-t",
        type=int,
        default=30,
        help="Query timeout in seconds (default: 30 seconds)"
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        default=False,
        help="Enable debug output"
    )

    args = parser.parse_args()

    debug_print(args.debug, f"Arguments: {args}")

    # Parse date filters if provided
    start_dt = parse_date(args.start_date) if args.start_date else None
    end_dt = parse_date(args.end_date) if args.end_date else None

    debug_print(args.debug, f"Start date: {start_dt}, End date: {end_dt}")

    # Ensure the composite index exists
    index_exists, index_created = ensure_index_exists(args.db, args.debug)

    # If index creation failed and we don't have it, the query will be slow
    if not index_exists:
        print(f"Note: Running without composite index - queries may be slow.", file=sys.stderr)
        print(f"To optimize, run with sudo once to create the index.", file=sys.stderr)
        print(f"", file=sys.stderr)

    # First, do a quick check to see if we have any data for this IP
    try:
        debug_print(args.debug, "Performing quick count check...")
        quick_conn = sqlite3.connect(args.db)
        quick_conn.execute("PRAGMA cache_size = 10000")
        quick_cursor = quick_conn.cursor()

        # Quick count to see if IP exists
        if args.c:
            quick_cursor.execute("SELECT COUNT(*) FROM logs WHERE node_ip IN (?, '0.0.0.0')", (args.ip,))
        else:
            quick_cursor.execute("SELECT COUNT(*) FROM logs WHERE node_ip = ?", (args.ip,))
        count = quick_cursor.fetchone()[0]
        quick_conn.close()

        debug_print(args.debug, f"Found {count} total rows for this IP")

        if count == 0:
            ip_display = f"{args.ip} (plus controller)" if args.c else args.ip
            print(f"No log records found for {ip_display} in {args.db}")
            return

        # If count is large, warn user
        if count > 10000 and args.count is None:
            print(f"Warning: Found {count} rows for this IP. Consider using --count to limit results.", file=sys.stderr)

    except Exception as e:
        debug_print(args.debug, f"Quick check failed: {e}")

    # Build the query
    where_conditions = []
    params = []

    # IP filter
    if args.c:
        debug_print(args.debug, f"Including controller logs (0.0.0.0) for IP: {args.ip}")
        where_conditions.append("node_ip IN (?, '0.0.0.0')")
        params.append(args.ip)
    else:
        debug_print(args.debug, f"Filtering for node IP: {args.ip}")
        where_conditions.append("node_ip = ?")
        params.append(args.ip)

    # Date range filters
    if start_dt:
        start_epoch = int(start_dt.timestamp())
        where_conditions.append("epoch_time >= ?")
        params.append(start_epoch)
        debug_print(args.debug, f"Start epoch: {start_epoch} ({start_dt})")

    if end_dt:
        end_epoch = int(end_dt.timestamp())
        where_conditions.append("epoch_time <= ?")
        params.append(end_epoch)
        debug_print(args.debug, f"End epoch: {end_epoch} ({end_dt})")

    # Build the complete WHERE clause
    where_clause = " AND ".join(where_conditions)

    # Build the query - use INDEXED BY if the index exists, otherwise let SQLite decide
    if index_exists:
        query = f"""
            SELECT timestamp_utc, log_tag, message_type, message, node_ip
            FROM logs INDEXED BY idx_logs_node_ip_epoch
            WHERE {where_clause}
            ORDER BY epoch_time DESC
        """
        debug_print(args.debug, "Using composite index idx_logs_node_ip_epoch")
    else:
        query = f"""
            SELECT timestamp_utc, log_tag, message_type, message, node_ip
            FROM logs
            WHERE {where_clause}
            ORDER BY epoch_time DESC
        """
        debug_print(args.debug, "Composite index not available - using default query plan")

    # Append LIMIT clause
    if args.count is not None:
        query += " LIMIT ?"
        params.append(args.count)
        debug_print(args.debug, f"Limiting to {args.count} rows")

    # Show the query
    debug_print(args.debug, "=" * 60)
    debug_print(args.debug, "QUERY:")
    debug_print(args.debug, query)
    debug_print(args.debug, "PARAMETERS:")
    debug_print(args.debug, params)
    debug_print(args.debug, "=" * 60)

    # Execute query in subprocess with timeout
    print(f"Executing query (timeout: {args.timeout}s)... Press Ctrl+C to cancel", file=sys.stderr)
    start_time = time.time()

    # Show a progress indicator
    import threading
    stop_spinner = False

    def spinner():
        chars = "|/-\\"
        i = 0
        while not stop_spinner:
            sys.stderr.write(f"\rQuery running... {chars[i % len(chars)]} ")
            sys.stderr.flush()
            time.sleep(0.2)
            i += 1
        sys.stderr.write("\r" + " " * 30 + "\r")
        sys.stderr.flush()

    spinner_thread = threading.Thread(target=spinner)
    spinner_thread.daemon = True
    spinner_thread.start()

    try:
        rows = run_sqlite_query(args.db, query, params, args.timeout, args.debug)
        stop_spinner = True
        spinner_thread.join(timeout=0.5)

    except TimeoutError as e:
        stop_spinner = True
        spinner_thread.join(timeout=0.5)
        print(f"\nQuery timed out after {args.timeout} seconds.", file=sys.stderr)
        if not index_exists:
            print("\n" + "=" * 70, file=sys.stderr)
            print("The query is slow because the composite index is missing.", file=sys.stderr)
            print("Please create it with:", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print(f"  sudo sqlite3 {args.db} \"CREATE INDEX idx_logs_node_ip_epoch ON logs(node_ip, epoch_time DESC);\"", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print("\nThis is a one-time setup. After creating the index, queries will be fast.", file=sys.stderr)
        else:
            print("\nOptimization tips:", file=sys.stderr)
            print("  1. Use --count to limit results: -n 100", file=sys.stderr)
            print("  2. Add more specific date filters", file=sys.stderr)
            print("  3. Increase timeout: --timeout 120", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        stop_spinner = True
        spinner_thread.join(timeout=0.5)
        print(f"\nQuery error: {e}", file=sys.stderr)
        sys.exit(1)

    elapsed_time = time.time() - start_time
    debug_print(args.debug, f"Query took {elapsed_time:.2f} seconds")

    if not rows:
        ip_display = f"{args.ip} (plus controller)" if args.c else args.ip
        print(f"\nNo log records found for {ip_display} in {args.db}")
        if start_dt or end_dt:
            date_range = []
            if start_dt:
                date_range.append(f"from {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
            if end_dt:
                date_range.append(f"to {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Date filter: {' '.join(date_range)}")
        return

    # If -c is specified without -a, warn about timestamp offsets
    if args.c and not args.a:
        print("\n" + "=" * 70, file=sys.stderr)
        print("WARNING: You are viewing both node and controller logs.", file=sys.stderr)
        print("Node timestamps are server arrival times, controller timestamps are", file=sys.stderr)
        print("generation times. They may not be aligned.", file=sys.stderr)
        print("Try using the -a flag to adjust node timestamps to real time", file=sys.stderr)
        print("using ESP-IDF timestamps embedded in the log messages.", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)

    # Calibrate timestamps if -a is specified
    calibration = None
    if args.a:
        debug_print(args.debug, "Calibrating ESP timestamps to real time...")
        calibration = calibrate_node_time(rows, args.debug)
        if calibration:
            debug_print(args.debug, f"Calibrated {len(calibration)} nodes")
        else:
            print("Warning: No ESP timestamps found in messages - using server timestamps", file=sys.stderr)

    # Header - just a simple summary
    print(f"\nQuery completed in {elapsed_time:.2f} seconds, {len(rows)} entries", file=sys.stderr)
    if start_dt:
        print(f"From: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)
    if end_dt:
        print(f"To:   {end_dt.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)
    if args.a and calibration:
        print(f"Node timestamps adjusted to real time using ESP-IDF calibration", file=sys.stderr)
    print("", file=sys.stderr)

    # Determine the maximum width needed for the source field
    # This ensures all sources align nicely for column-select editing
    max_source_width = 15  # Minimum width
    sources = set()
    for row in rows:
        _, _, _, _, node_ip = row
        if args.c and node_ip == '0.0.0.0':
            source = "CONTROLLER"
        else:
            source = node_ip
        sources.add(source)

    for source in sources:
        max_source_width = max(max_source_width, len(source))

    # Add a little extra padding
    max_source_width += 1

    # Reverse rows to print in ascending chronological order (oldest -> newest)
    for row in reversed(rows):
        timestamp, tag, msg_type, message, node_ip = row

        # Determine source label
        if args.c and node_ip == '0.0.0.0':
            source = "CONTROLLER"
        else:
            source = node_ip

        # Get severity character
        severity = get_severity_char(tag, msg_type)

        # Adjust timestamp if calibration is available
        if args.a and calibration and node_ip in calibration:
            # Adjust the timestamp using ESP-IDF calibration
            display_time = adjust_timestamp(timestamp, message, calibration, node_ip)
        else:
            # Format server timestamp to be more compact
            display_time = timestamp
            if 'T' in display_time:
                display_time = display_time.replace('T', ' ').replace('Z', '')
                # Truncate microseconds to 3 digits
                if '.' in display_time:
                    parts = display_time.split('.')
                    display_time = parts[0] + '.' + parts[1][:3]

        # Right-pad the source to the maximum width for column alignment
        # This makes it easy for column-select editors
        source_padded = source.ljust(max_source_width)

        # Print in simple format: timestamp | source | severity | message
        print(f"{display_time} {source_padded} {message}")

if __name__ == "__main__":
    main()