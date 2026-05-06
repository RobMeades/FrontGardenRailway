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
Level gauge node handler.

This handler inherits from NodeHandler (injected by controller).
All notification logic is handled by the WebController.
"""

# NodeHandler and FGR protocol are injected by the controller

# Reservoir depth constant (mm from top to full)
RESERVOIR_DEPTH = 1000

class LevelGaugeHandler(NodeHandler):
    """
    A level gauge node handler.
    """

    def on_connected(self):
        self.logger.info(f"Level gauge node {self.node.name} is online")

    def on_disconnected(self):
        self.logger.info(f"Level gauge {self.node.name} went offline")

    def on_needs_cfg(self, msg: fgr.FGRMsg):
        """Send level-gauge-specific configuration"""
        self.logger.info(f"Node {self.node.name} needs configuration, sending level gauge config")

        # Example: configure reporting interval and calibration
        config_data = b'\x3C'  # 60 seconds reporting interval
        self.send_response(msg.subtype, msg.reference, config_data)

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """Handle device-specific indications"""
        ind_type = msg.subtype

        # Let base class handle state updates
        super().on_indication(msg)

        # Handle custom level reading
        if ind_type == 0x100:
            if len(msg.contents) >= 2:
                # Level reading in contents (e.g., 2 bytes)
                level_mm = int.from_bytes(msg.contents[:2], 'big')
                water_height = RESERVOIR_DEPTH - level_mm

                self.logger.info(f"Level gauge reading: {level_mm} mm from top -> {water_height} mm water")

                if hasattr(self.controller, 'update_node_measurement'):
                    self.controller.update_node_measurement(self.node.name, {
                        'level': level_mm,
                        'water_height': max(0, water_height),
                        'type': 'level_gauge'
                    })

        return True