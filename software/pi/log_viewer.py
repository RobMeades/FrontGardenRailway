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
import os
import re
import csv
import io
from datetime import datetime

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

def format_timestamp(timestamp_str):
    """
    Format a timestamp string consistently.
    Handles "2026-06-17T21:23:13.741828Z" format.
    Returns "YYYY-MM-DD HH:MM:SS.mmm" (3 decimal places).
    """
    # Remove 'Z' and replace 'T' with space if present
    clean_ts = timestamp_str.replace('Z', '').replace('T', ' ')

    # Try to parse with microseconds
    try:
        if '.' in clean_ts:
            dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S.%f")
        else:
            dt = datetime.strptime(clean_ts, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]  # Keep 3 decimal places
    except ValueError:
        # If parsing fails, return as-is
        return timestamp_str

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
    # Use CSV mode with proper quoting to handle special characters in messages
    cmd = ['sqlite3', '-csv', db_path]

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

        # Parse CSV output with proper quoting
        if stdout.strip():
            rows = []
            reader = csv.reader(io.StringIO(stdout))
            for row in reader:
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
  {sys.argv[0]}                              # Show all logs from all nodes
  {sys.argv[0]} --node 10.10.3.2             # Show logs for node 10.10.3.2
  {sys.argv[0]} --node 10.10.3.2 -c          # Show logs for node + controller
  {sys.argv[0]} --node 10.10.3.2 -n 50       # Show last 50 logs for node
  {sys.argv[0]} -s "2026-06-01"              # Show all logs from June 1, 2026 onwards
  {sys.argv[0]} -s "2026-06-01 10:00" -e "2026-06-01 18:00"  # Time range

Note: First run may require sudo to create an index for better performance.
      This is a one-time setup - subsequent runs will not need sudo.

      For queries without --node (all nodes), performance may be slower.
      Use --count or date filters to limit results.
        """
    )

    parser.add_argument(
        "--node", "-i",
        type=str,
        default=None,
        help="Target node IP address to filter logs by (e.g., 10.10.3.2). If not specified, shows all nodes."
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

    # Build the query
    where_conditions = []
    params = []

    # IP filter - if node is specified, filter by it, otherwise show all
    if args.node:
        if args.c:
            debug_print(args.debug, f"Including controller logs (0.0.0.0) for IP: {args.node}")
            where_conditions.append("node_ip IN (?, '0.0.0.0')")
            params.append(args.node)
        else:
            debug_print(args.debug, f"Filtering for node IP: {args.node}")
            where_conditions.append("node_ip = ?")
            params.append(args.node)
    else:
        if args.c:
            debug_print(args.debug, "Showing all nodes including controller logs")
            # Always true, but we need to include controller logs
            where_conditions.append("(node_ip != '0.0.0.0' OR node_ip = '0.0.0.0')")
        else:
            debug_print(args.debug, "Showing all nodes (excluding controller)")
            where_conditions.append("node_ip != '0.0.0.0'")

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

    # Build the query - use INDEXED BY if we have a specific node (for the composite index)
    if index_exists and args.node:
        query = f"""
            SELECT timestamp_utc, log_tag, message_type, message, node_ip
            FROM logs INDEXED BY idx_logs_node_ip_epoch
            WHERE {where_clause}
            ORDER BY epoch_time DESC
        """
        debug_print(args.debug, "Using composite index idx_logs_node_ip_epoch")
    else:
        # For all-nodes queries, use a simpler query with the epoch index
        query = f"""
            SELECT timestamp_utc, log_tag, message_type, message, node_ip
            FROM logs
            WHERE {where_clause}
            ORDER BY epoch_time DESC
        """
        debug_print(args.debug, "Using default query plan (no node filter - may be slower)")

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

    # Warn about performance for all-nodes queries
    if not args.node and args.count is None:
        print("\n" + "=" * 70, file=sys.stderr)
        print("INFO: Querying all nodes without a --node filter or --count limit.", file=sys.stderr)
        print("This may be slow on large databases.", file=sys.stderr)
        print("To speed up:", file=sys.stderr)
        print("  - Use --node to filter by a specific IP", file=sys.stderr)
        print("  - Use --count to limit results (e.g., -n 1000)", file=sys.stderr)
        print("  - Add date filters with -s and -e", file=sys.stderr)
        print("=" * 70 + "\n", file=sys.stderr)

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
        if not args.node:
            print("\n" + "=" * 70, file=sys.stderr)
            print("The query timed out because it's scanning all nodes.", file=sys.stderr)
            print("Try:", file=sys.stderr)
            print("  1. Use --node to filter by a specific IP", file=sys.stderr)
            print("  2. Use --count to limit results: -n 1000", file=sys.stderr)
            print("  3. Add date filters: -s '2026-06-17' -e '2026-06-17 12:00'", file=sys.stderr)
            print("  4. Increase timeout: --timeout 120", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
        elif not index_exists:
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
        if args.node:
            ip_display = f"{args.node} (plus controller)" if args.c else args.node
        else:
            ip_display = "all nodes (plus controller)" if args.c else "all nodes (excluding controller)"
        print(f"\nNo log records found for {ip_display} in {args.db}")
        if start_dt or end_dt:
            date_range = []
            if start_dt:
                date_range.append(f"from {start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
            if end_dt:
                date_range.append(f"to {end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Date filter: {' '.join(date_range)}")
        return

    # Header - just a simple summary
    print(f"\nQuery completed in {elapsed_time:.2f} seconds, {len(rows)} entries", file=sys.stderr)
    if args.node:
        print(f"Node: {args.node}", file=sys.stderr)
    else:
        print(f"Node: ALL", file=sys.stderr)
    if start_dt:
        print(f"From: {start_dt.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)
    if end_dt:
        print(f"To:   {end_dt.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)
    print("", file=sys.stderr)

    # Determine the maximum width needed for the source field
    max_source_width = 15  # Minimum width
    sources = set()
    for row in rows:
        if len(row) >= 5:
            node_ip = row[4]

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
        if len(row) < 5:
            debug_print(args.debug, f"Skipping row with insufficient columns: {len(row)}")
            continue

        timestamp = row[0]
        tag = row[1]
        msg_type = row[2]
        message = row[3]
        node_ip = row[4]

        # Determine source label
        if args.c and node_ip == '0.0.0.0':
            source = "CONTROLLER"
        else:
            source = node_ip

        # Get severity character
        severity = get_severity_char(tag, msg_type)

        # Format timestamp consistently
        display_time = format_timestamp(timestamp)

        # Right-pad the source to the maximum width for column alignment
        source_padded = source.ljust(max_source_width)

        # Print in simple format: timestamp | source | severity | message
        print(f"{display_time} {source_padded} {message}")

if __name__ == "__main__":
    main()