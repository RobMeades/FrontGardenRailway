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

# Written by Google Gemini :-)

import sqlite3
import argparse
import sys

DATABASE_PATH="/mnt/ssd/logs.db"

def main():
    parser = argparse.ArgumentParser(
        description="Query FGR log database"
    )

    # Target device IP as a positional argument (required, no flag needed)
    parser.add_argument(
        "ip",
        type=str,
        help="Target node IP address to filter logs by (e.g., 10.10.3.2)"
    )

    # Database path defaults to your external SSD mount location
    parser.add_argument(
        "--db", "-d",
        type=str,
        default=DATABASE_PATH,
        help=f"Path to unified SQLite database file (default: {DATABASE_PATH})"
    )

    # Limit defaults to None (fetches all records matching the query)
    parser.add_argument(
        "--limit", "-l",
        type=int,
        default=None,
        help="Limit number of rows returned (default: None, returns all logs)"
    )

    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    cursor = conn.cursor()

    # Base query string
    query = """
        SELECT timestamp_utc, log_tag, message_type, message
        FROM device_logs
        WHERE node_ip = ?
        ORDER BY epoch_time DESC
    """

    # Append LIMIT clause dynamically if a limit value was requested
    params = [args.ip]
    if args.limit is not None:
        query += " LIMIT ?"
        params.append(args.limit)

    try:
        cursor.execute(query, params)
        rows = cursor.fetchall()

        if not rows:
            print(f"No log records found for node IP: {args.ip} in {args.db}")
            return

        # Header description block
        limit_desc = f"last {len(rows)}" if args.limit is not None else "all available"
        print(f"=== Displaying {limit_desc} logs for {args.ip} (Chronological order) ===")
        print(f"{'Timestamp (UTC)':<26} | {'Tag':<15} | {'Type':<7} | Message")
        print("-" * 90)

        # Reverse rows to print in ascending chronological order (oldest -> newest)
        for row in reversed(rows):
            timestamp, tag, msg_type, message = row
            tag_str = tag if tag else "None"

            # ANSI terminal syntax visual coloring for distinct profiles
            if tag in ('BACKTRACE', 'STACK_OVERFLOW'):
                # Red text output highlighting deep hardware crash state
                print(f"\033[91m{timestamp:<26} | {tag_str:<15} | {msg_type:<7} | {message}\033[0m")
            elif msg_type == 'METRIC':
                # Green text output indicating telemetry/JSON data stream profile
                print(f"\033[92m{timestamp:<26} | {tag_str:<15} | {msg_type:<7} | {message}\033[0m")
            else:
                # Standard console payload text format
                print(f"{timestamp:<26} | {tag_str:<15} | {msg_type:<7} | {message}")

    except sqlite3.OperationalError as e:
        print(f"Database Error: Could not query table. Verify database path: {args.db}\nDetail: {e}", file=sys.stderr)
    except Exception as e:
        print(f"Unexpected operational error: {e}", file=sys.stderr)
    finally:
        conn.close()

if __name__ == "__main__":
    main()