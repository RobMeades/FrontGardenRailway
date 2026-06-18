#!/bin/bash

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

# Written by Deep Seek, tested by Rob :-)

# watch -n 10 performance_check.sh
# Quick status check for FGR system

echo "=== FGR System Performance Check ==="
echo "Time: $(date)"
echo ""

# Get timestamps and lag
J=$(journalctl --identifier=fgr-log-server -n 1 --output=short-iso 2>/dev/null | awk "{print \$1}" | head -1)
P=$(journalctl --identifier=python -n 1 --output=short-iso 2>/dev/null | awk "{print \$1}" | head -1)

if [ -n "$J" ] && [ -n "$P" ]; then
    LAG=$(($(date -d "$P" +%s) - $(date -d "$J" +%s)))
    echo "Lag: ${LAG}s"
else
    echo "Lag: N/A (no logs)"
fi

# Log rate per minute
RATE=$(journalctl --since "1 minute ago" 2>/dev/null | wc -l)
echo "Logs/min: ${RATE}"

# Database freelist count
FREELIST=$(sudo sqlite3 /mnt/ssd/logs.db "PRAGMA freelist_count;" 2>/dev/null)
echo "Freelist: ${FREELIST}"

# USB drive utilization
UTIL=$(iostat -x 1 2 2>/dev/null | grep sda | tail -1 | awk "{print \$NF}")
echo "USB util: ${UTIL}%"

# Load average
LOAD=$(uptime | awk -F'load average:' '{print $2}')
echo "Load:${LOAD}"

# Memory
MEM=$(free -m | grep Mem | awk '{print $3 "/" $2 " MB used"}')
echo "Memory: ${MEM}"

# Journal size
JOURNAL_SIZE=$(du -sh /var/log/journal/ 2>/dev/null | awk '{print $1}')
echo "Journal: ${JOURNAL_SIZE}"

# Database size
DB_SIZE=$(du -h /mnt/ssd/logs.db 2>/dev/null | awk '{print $1}')
echo "DB: ${DB_SIZE}"