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

INTERFACE="wlan0"

# Announce startup cleanly to syslog
logger -t clear-node-ghosts "Daemon started cleanly. Monitoring $INTERFACE state tables..."

while true; do
    # Scrape the kernel's wireless tables directly
    iw dev "$INTERFACE" station dump 2>/dev/null | awk -v iface="$INTERFACE" '
        /^Station/ {
            mac = $2
            assoc = "unknown"
        }
        /associated:/ {
            assoc = $2

            if (assoc == "no") {
                # 1. Build a custom log action message
                log_msg = "logger -t clear-node-ghosts \"🔥 Evicted zombie client [" mac "] - Found stuck in associated:no state.\""
                system(log_msg)

                # 2. Execute the actual low-level driver purge
                cmd = "iw dev " iface " station del " mac
                system(cmd)
            }
        }
    '
    # Check the state tables every 2 seconds
    sleep 2
done