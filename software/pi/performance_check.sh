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

# Quick status check for FGR system with CSV logging
#
# Usage: ./performance_check.sh [--csv] [--plot] [--watch]
#
# Or:
#
# watch -n 10 ./performance_check.sh --watch
#
# Or:
#
# crontab -e
#
# ...and add a line:
#
# * * * * * /home/rob/performance_check.sh --csv > /dev/null 2>&1
#
# ...to save a plot every minute

# Output directory (survives reboot)
OUTPUT_DIR="/mnt/ssd/monitoring"
mkdir -p "$OUTPUT_DIR"

CSV_FILE="$OUTPUT_DIR/lag_stats.csv"
LAG_FILE="$OUTPUT_DIR/lag_stats.txt"
STATUS_FILE="$OUTPUT_DIR/fgr_status.json"
DIAG_LOG="$OUTPUT_DIR/diagnostics.log"

# --- Parse arguments ---
LOG_CSV=0
DO_PLOT=0
WATCH_MODE=0
for arg in "$@"; do
    case $arg in
        --csv) LOG_CSV=1 ;;
        --plot) DO_PLOT=1 ;;
        --watch) WATCH_MODE=1 ;;
    esac
done

# --- Function: Get freelist warning level ---
get_freelist_warning() {
    local freelist=$1
    if [ -z "$freelist" ] || [ "$freelist" -eq -1 ] 2>/dev/null; then
        echo "UNKNOWN"
        return
    fi

    if [ "$freelist" -gt 50000 ] 2>/dev/null; then
        echo "🔴 HIGH (VACUUM recommended)"
    elif [ "$freelist" -gt 20000 ] 2>/dev/null; then
        echo "🟡 MEDIUM (consider VACUUM)"
    elif [ "$freelist" -gt 5000 ] 2>/dev/null; then
        echo "🟢 MODERATE (monitor)"
    else
        echo "✅ LOW (healthy)"
    fi
}

# --- Function: Get metrics as CSV-ready variables ---
get_metrics() {
    # Get lag
    J=$(journalctl --identifier=fgr-log-server -n 1 --output=short-iso 2>/dev/null | awk '{print $1}' | head -1)
    P=$(journalctl --identifier=python -n 1 --output=short-iso 2>/dev/null | awk '{print $1}' | head -1)
    LAG="N/A"
    if [ -n "$J" ] && [ -n "$P" ]; then
        LAG_VAL=$(($(date -d "$P" +%s) - $(date -d "$J" +%s)))
        if [ $LAG_VAL -lt 0 ]; then LAG_VAL=0; fi
        LAG="$LAG_VAL"
    fi

    RATE=$(journalctl --since "1 minute ago" 2>/dev/null | wc -l)
    FREELIST=$(sqlite3 /mnt/ssd/logs.db "PRAGMA freelist_count;" 2>/dev/null)
    if [ -z "$FREELIST" ]; then
        FREELIST="-1"
    fi
    UTIL=$(iostat -x 1 2 2>/dev/null | grep sda | tail -1 | awk '{print $NF}')
    LOAD=$(uptime | awk -F'load average:' '{print $2}' | sed 's/^ //')
    MEM=$(free -m | grep Mem | awk '{print $3 "/" $2}')
    JOURNAL_SIZE=$(du -sh /var/log/journal/ 2>/dev/null | awk '{print $1}')
    DB_SIZE=$(du -h /mnt/ssd/logs.db 2>/dev/null | awk '{print $1}')

    # Extract load values
    LOAD_1=$(echo "$LOAD" | awk -F', ' '{print $1}')
    LOAD_5=$(echo "$LOAD" | awk -F', ' '{print $2}')
    LOAD_15=$(echo "$LOAD" | awk -F', ' '{print $3}')

    # Extract memory
    MEM_USED=$(echo "$MEM" | cut -d'/' -f1)
    MEM_TOTAL=$(echo "$MEM" | cut -d'/' -f2 | awk '{print $1}')

    # Convert sizes to MB
    JOURNAL_MB=$(echo "$JOURNAL_SIZE" | sed 's/M//g' | sed 's/G/*1024/g' | bc 2>/dev/null || echo "0")
    DB_MB=$(echo "$DB_SIZE" | sed 's/M//g' | sed 's/G/*1024/g' | bc 2>/dev/null || echo "0")

    # Get freelist warning
    FREELIST_WARNING=$(get_freelist_warning "$FREELIST")

    # Return as CSV-ready variables (using a delimiter)
    echo "${LAG}|${RATE}|${FREELIST}|${UTIL}|${LOAD_1}|${LOAD_5}|${LOAD_15}|${MEM_USED}|${MEM_TOTAL}|${JOURNAL_MB}|${DB_MB}|${FREELIST_WARNING}"
}

# --- Main ---
TIMESTAMP=$(date +%Y-%m-%d_%H:%M:%S)

# Get metrics (returns pipe-separated values)
METRICS=$(get_metrics)

# Parse the metrics
LAG=$(echo "$METRICS" | cut -d'|' -f1)
RATE=$(echo "$METRICS" | cut -d'|' -f2)
FREELIST=$(echo "$METRICS" | cut -d'|' -f3)
UTIL=$(echo "$METRICS" | cut -d'|' -f4)
LOAD_1=$(echo "$METRICS" | cut -d'|' -f5)
LOAD_5=$(echo "$METRICS" | cut -d'|' -f6)
LOAD_15=$(echo "$METRICS" | cut -d'|' -f7)
MEM_USED=$(echo "$METRICS" | cut -d'|' -f8)
MEM_TOTAL=$(echo "$METRICS" | cut -d'|' -f9)
JOURNAL_MB=$(echo "$METRICS" | cut -d'|' -f10)
DB_MB=$(echo "$METRICS" | cut -d'|' -f11)
FREELIST_WARNING=$(echo "$METRICS" | cut -d'|' -f12-)

# --- Watch mode (compact output for watch) ---
if [ $WATCH_MODE -eq 1 ]; then
    TS=$(date +%H:%M:%S)
    # Get metrics fresh (watch mode needs all variables)
    METRICS=$(get_metrics)
    LAG=$(echo "$METRICS" | cut -d'|' -f1)
    RATE=$(echo "$METRICS" | cut -d'|' -f2)
    FREELIST=$(echo "$METRICS" | cut -d'|' -f3)
    UTIL=$(echo "$METRICS" | cut -d'|' -f4)
    LOAD_1=$(echo "$METRICS" | cut -d'|' -f5)
    LOAD_5=$(echo "$METRICS" | cut -d'|' -f6)
    LOAD_15=$(echo "$METRICS" | cut -d'|' -f7)
    MEM_USED=$(echo "$METRICS" | cut -d'|' -f8)
    MEM_TOTAL=$(echo "$METRICS" | cut -d'|' -f9)
    FREELIST_WARNING=$(echo "$METRICS" | cut -d'|' -f12-)

    # Format load and memory
    LOAD="${LOAD_1}, ${LOAD_5}, ${LOAD_15}"
    MEM="${MEM_USED}/${MEM_TOTAL}"

    # Update min/max lag
    if [ "$LAG" != "N/A" ] && [ "$LAG" -ge 0 ] 2>/dev/null; then
        if [ ! -f "$LAG_FILE" ]; then
            echo "$LAG" > "$LAG_FILE"
            MIN=$LAG
            MAX=$LAG
        else
            MIN=$(sort -n "$LAG_FILE" 2>/dev/null | head -1)
            MAX=$(sort -n "$LAG_FILE" 2>/dev/null | tail -1)
            if [ -z "$MIN" ]; then MIN=$LAG; fi
            if [ -z "$MAX" ]; then MAX=$LAG; fi
            if [ $LAG -lt $MIN ] 2>/dev/null; then MIN=$LAG; fi
            if [ $LAG -gt $MAX ] 2>/dev/null; then MAX=$LAG; fi
            echo "$LAG" >> "$LAG_FILE"
        fi
    else
        MIN="N/A"
        MAX="N/A"
    fi

    # Print with min/max
    echo "$TS | Lag: ${LAG}s (min: ${MIN}s, max: ${MAX}s) | Logs/min: ${RATE} | Freelist: ${FREELIST} ${FREELIST_WARNING} | USB: ${UTIL}% | Load: ${LOAD} | Mem: ${MEM}MB"
    exit 0
fi

# --- Print human-readable output ---
echo "========================================="
echo "FGR System Status"
echo "========================================="
echo "Time: $(date)"
echo ""
echo "Lag: ${LAG}s"
echo "Logs/min: ${RATE}"
echo "Freelist: ${FREELIST} ${FREELIST_WARNING}"
echo "USB util: ${UTIL}%"
echo "Load: ${LOAD_1}, ${LOAD_5}, ${LOAD_15}"
echo "Memory: ${MEM_USED}/${MEM_TOTAL} MB used"
echo "Journal: $(du -sh /var/log/journal/ 2>/dev/null | awk '{print $1}')"
echo "DB: $(du -h /mnt/ssd/logs.db 2>/dev/null | awk '{print $1}')"

# --- CSV Logging ---
if [ $LOG_CSV -eq 1 ]; then
    # Create CSV header if file doesn't exist
    if [ ! -f "$CSV_FILE" ]; then
        echo "timestamp,lag_s,logs_per_min,freelist,usb_util_pct,load_1,load_5,load_15,mem_used_mb,mem_total_mb,journal_mb,db_mb,freelist_warning" > "$CSV_FILE"
    fi

    # Clean freelist warning of commas for CSV
    FREELIST_WARNING_CLEAN=$(echo "$FREELIST_WARNING" | tr ',' ';')

    # Append to CSV
    echo "$TIMESTAMP,$LAG,$RATE,$FREELIST,$UTIL,$LOAD_1,$LOAD_5,$LOAD_15,$MEM_USED,$MEM_TOTAL,$JOURNAL_MB,$DB_MB,\"$FREELIST_WARNING_CLEAN\"" >> "$CSV_FILE"
    echo "[CSV] Appended to $CSV_FILE"
fi

# --- Write JSON status file for GUI ---
cat > "$STATUS_FILE" <<EOF
{
    "timestamp": "$TIMESTAMP",
    "lag": $LAG,
    "logs_per_min": $RATE,
    "freelist": $FREELIST,
    "freelist_warning": "$FREELIST_WARNING",
    "usb_util": $UTIL,
    "load_1": $LOAD_1,
    "load_5": $LOAD_5,
    "load_15": $LOAD_15,
    "mem_used": $MEM_USED,
    "mem_total": $MEM_TOTAL,
    "journal_mb": $JOURNAL_MB,
    "db_mb": $DB_MB
}
EOF

# --- Track min/max lag ---
if [ "$LAG" != "N/A" ] && [ "$LAG" -ge 0 ] 2>/dev/null; then
    if [ ! -f "$LAG_FILE" ]; then
        echo "$LAG" > "$LAG_FILE"
        echo "$LAG" >> "$LAG_FILE"
        MIN=$LAG
        MAX=$LAG
    else
        MIN=$(sort -n "$LAG_FILE" | head -1)
        MAX=$(sort -n "$LAG_FILE" | tail -1)
        if [ $LAG -lt $MIN ]; then MIN=$LAG; fi
        if [ $LAG -gt $MAX ]; then MAX=$LAG; fi
        echo "$LAG" >> "$LAG_FILE"
    fi
    echo "Min lag: ${MIN}s, Max lag: ${MAX}s"
fi

# --- ASCII Plot (if --plot) ---
if [ $DO_PLOT -eq 1 ]; then
    if [ -f "$CSV_FILE" ]; then
        echo ""
        echo "=== Lag History (last 30 samples) ==="
        tail -n 31 "$CSV_FILE" | grep -v "^timestamp" | awk -F, '{print $2}' | \
        while read -r val; do
            if [ -n "$val" ] && [[ "$val" =~ ^[0-9]+$ ]]; then
                if [ "$val" -lt 60 ]; then
                    bar=$(printf "%${val}s" | tr ' ' '#')
                    printf "%4ds %s\n" "$val" "$bar"
                else
                    bar=$(printf "%60s" | tr ' ' '#')
                    printf "%4ds %s>\n" "$val" "$bar"
                fi
            fi
        done
        echo ""
    fi
fi

# --- Freelist warning if high ---
if [ -n "$FREELIST" ] && [ "$FREELIST" -gt 50000 ] 2>/dev/null; then
    echo ""
    echo "⚠️  WARNING: Freelist is $FREELIST - VACUUM recommended!"
    echo "   Schedule maintenance: sudo systemctl stop log_server; sudo sqlite3 /mnt/ssd/logs.db 'VACUUM;'; sudo systemctl start log_server"
fi

# --- DIAGNOSTICS (low impact, written every run) ---
{
echo "=== $(date -Iseconds) ==="

echo "--- LOADAVG ---"
cat /proc/loadavg 2>&1

echo "--- TOP PROCESSES BY CPU ---"
ps -eo pid,ppid,%cpu,%mem,state,args --sort=-%cpu --no-headers 2>/dev/null | head -15

echo "--- D-STATE PROCESSES ---"
ps -eo pid,state,wchan,cmd --no-headers 2>/dev/null | grep " D " | grep -v grep || echo "None"

echo "--- LOG SERVER PROCESS ---"
pgrep -f "log_server.py" -l 2>&1 || echo "Not found"
for pid in $(pgrep -f "log_server.py" 2>/dev/null); do
    ps -p "$pid" -o pid,state,wchan,cmd --no-headers 2>&1
done

echo "--- PYTHON CONTROLLER ---"
pgrep -f "web_controller.py" -l 2>&1 || echo "Not found"
for pid in $(pgrep -f "web_controller.py" 2>/dev/null); do
    ps -p "$pid" -o pid,state,wchan,cmd --no-headers 2>&1
done

echo "--- KERNEL (USB/IO/ERRORS) ---"
dmesg 2>/dev/null | grep -i "usb\|sd[a-z]\|i/o\|error\|reset" | tail -5 || echo "None"

echo "--- MOUNT STATUS ---"
mount | grep /mnt/ssd 2>&1

echo "--- SOCKETS (port 5001) ---"
ss -tpn 2>/dev/null | grep 5001 || echo "None"

echo "--- WIFI (AP mode) ---"
ip addr show wlan0 2>/dev/null | grep "state"
ip addr show wlan0 2>/dev/null | grep "inet "
/usr/sbin/iw dev wlan0 info 2>/dev/null | grep -E "channel|frequency" || echo "No info"
echo "Connected stations:"
STATIONS=$(/usr/sbin/iw dev wlan0 station dump 2>/dev/null | grep "Station")
if [ -n "$STATIONS" ]; then
    echo "$STATIONS"
else
    echo "None"
fi

echo "--- SYSTEMD ---"
systemctl is-system-running 2>&1
systemctl --failed --no-legend 2>&1 || echo "No failed units"

echo "========================================="
} >> "$DIAG_LOG" 2>&1