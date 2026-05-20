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

#!/bin/bash
INTERFACE="wlan0"

# Follow NetworkManager logs in real-time
journalctl -u NetworkManager -f -n 0 | while read -r LINE; do

    # Strictly target hostapd's explicit physical authentication and association frames
    # Example format: wlan0: interface state... or wlan0: STA xx:xx... authenticated
    if echo "$LINE" | grep -E "wlan0.*(authenticated|associated|authentication)" | grep -qE "([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"; then

        # Extract the target MAC address
        TARGET_MAC=$(echo "$LINE" | grep -oE "([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}" | head -n 1)

        # If this device is forcing a new Wi-Fi auth frame but the kernel think it is already here...
        if iw dev "$INTERFACE" station get "$TARGET_MAC" 2>/dev/null | grep -q "Station"; then
            # Clean out the stale zombie session instantly so the fresh handshake goes through
            iw dev "$INTERFACE" station del "$TARGET_MAC"
        fi
    fi
done