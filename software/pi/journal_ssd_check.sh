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

# Written by Google Gemini, tested by Rob :-)

# Script to verify journald is writing to SSD, not RAM.
# Logs results to syslog with appropriate priorities.

TAG="journal-ssd-check"

# 1. Find journald PID and get the active system journal file via /proc
JOURNALD_PID=$(pidof systemd-journald)

if [ -z "$JOURNALD_PID" ]; then
    logger -t "$TAG" -p user.err "ERROR: systemd-journald process not found!"
    exit 1
fi

# Look for system.journal (not user-*.journal) in the open file descriptors
JOURNAL_FILE=$(ls -l /proc/$JOURNALD_PID/fd/ 2>/dev/null | grep -o "/var/log/journal/.*/system\.journal" | head -n 1)

# 2. SANITY CHECK: Confirm the file exists and is on the SSD (/dev/sda1)
if [ -z "$JOURNAL_FILE" ]; then
    logger -t "$TAG" -p user.err "ERROR: Could not identify active system journal file via /proc!"
    exit 1
fi

FILE_DEV=$(df "$JOURNAL_FILE" | tail -1 | awk '{print $1}')
if [ "$FILE_DEV" != "/dev/sda1" ]; then
    logger -t "$TAG" -p user.err "ERROR: Journal is writing to $FILE_DEV, not the SSD (/dev/sda1)!"
    exit 1
fi

# Track state using the file path for uniqueness
MTIME_TRACKER="/tmp/journal_mtime_$(echo "$JOURNAL_FILE" | tr '/' '_')"

# 3. Create a unique heartbeat
marker="HB_$(date +%s%N)"
logger -t "$TAG" -p user.debug "Writing test marker: $marker"

# 4. Force the journal to flush all pending buffers to physical media
journalctl --flush

# 5. Small sleep to ensure the OS completes the metadata update
sleep 1

# 6. Verify the entry exists in the journal
if journalctl -n 20 | grep -q "$marker"; then
    
    # 7. Check the modification time (mtime) of the journal file
    current_mtime=$(stat -c%Y "$JOURNAL_FILE")
    
    if [ -f "$MTIME_TRACKER" ]; then
        last_mtime=$(cat "$MTIME_TRACKER")
        
        # If the current mtime is newer than the last recorded mtime, writes are occurring
        if [ "$current_mtime" -gt "$last_mtime" ]; then
            logger -t "$TAG" -p user.info "SUCCESS: Heartbeat found and ($JOURNAL_FILE) was modified."
        else
            logger -t "$TAG" -p user.warning "WARNING: Heartbeat found, but ($JOURNAL_FILE) modification time ($current_mtime) did not advance, possible SSD write-lock or systemd-journald stall."
        fi
    else
        logger -t "$TAG" -p user.notice "INITIALIZED: Tracking start for $JOURNAL_FILE."
    fi
    
    # Update the tracker
    echo "$current_mtime" > "$MTIME_TRACKER"
else
    logger -t "$TAG" -p user.err "ERROR: Heartbeat marker '$marker' not found in journalctl!"
    exit 1
fi

exit 0
