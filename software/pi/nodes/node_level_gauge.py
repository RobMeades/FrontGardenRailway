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

This handler inherits from NodeHandler (injected by controller),
and therefore has access to all of the state variables for a node
that the Controller updates.
"""

import json
from typing import Dict, Any

# NodeHandler and FGR protocol are injected by the controller

# Reservoir depth constant (mm from top to full)
RESERVOIR_DEPTH = 1000

class LevelGaugeHandler(NodeHandler):
    """
    A level gauge node handler.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the handler."""
        super().__init__(*args, **kwargs)

        try:
            # Nothing to initialise
            self._log_init_exit()
        except Exception as e:
            self._log_init_error(e)
            raise

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

    def on_node_specific_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle node-specific indications (REQUIRED).
        Called only for message types unknown to the base class
        """
        ind_type = msg.subtype

        # Handle custom level reading
        if ind_type == 0x100:
            if len(msg.contents) >= 2:
                # Level reading in contents (e.g., 2 bytes)
                level_mm = int.from_bytes(msg.contents[:2], 'big')
                water_height = RESERVOIR_DEPTH - level_mm
                percentage = (water_height / RESERVOIR_DEPTH) * 100

                self.logger.info(f"Level gauge reading: {level_mm} mm from top -> {water_height} mm water ({percentage:.1f}%)")

                if hasattr(self.controller, 'update_node_custom_data'):  # ← Renamed method
                    self.controller.update_node_custom_data(self.node.name, {  # ← Renamed method call
                        'level': level_mm,
                        'water_height': max(0, water_height),
                        'percentage': max(0, min(100, percentage)),
                        'type': 'level_gauge'
                    })

        return True

    def get_card_html(self, node_name: str, node_data: Dict[str, Any]) -> str:
        custom_data = node_data.get('custom_data', {})
        water_height = custom_data.get('water_height', 'N/A')
        percentage = custom_data.get('percentage', 0)

        if isinstance(percentage, (int, float)):
            bar_width = min(100, max(0, percentage))
        else:
            bar_width = 0

        return f'''
            <div class="node-custom">
                <div class="custom-value" data-dynamic="water_height">{water_height} <span class="custom-unit">mm</span></div>
                <div class="custom-unit">Water Level ({percentage:.1f}%)</div>
                <div style="margin-top: 6px; background: #ddd; border-radius: 4px; height: 6px; overflow: hidden;">
                    <div style="background: #2196f3; width: {bar_width}%; height: 6px; border-radius: 4px; transition: width 0.3s ease;"></div>
                </div>
            </div>
        '''

    def get_expanded_html(self, node_name: str, node_data: Dict[str, Any]) -> str:
        """Return HTML for the node-specific section of expanded view"""
        custom_data = node_data.get('custom_data', {})
        water_height = custom_data.get('water_height', 'N/A')
        level = custom_data.get('level', 'N/A')
        percentage = custom_data.get('percentage', 0)

        # Ensure percentage is a number for the progress bar
        if isinstance(percentage, (int, float)):
            bar_width = min(100, max(0, percentage))
        else:
            bar_width = 0

        return f'''
            <div class="expanded-section">
                <h4>📊 Water Level Measurements</h4>
                <p>Water Height: <span data-dynamic="water_height">{water_height}</span> mm</p>
                <p>Distance from Top: <span data-dynamic="level">{level}</span> mm</p>
                <p>Percentage Full: <span data-dynamic="percentage">{percentage:.1f}</span>%</p>
                <div style="margin-top: 10px; background: #ddd; border-radius: 4px; height: 20px; overflow: hidden;">
                    <div style="background: #2196f3; width: {bar_width}%; height: 20px; border-radius: 4px; transition: width 0.3s ease; display: flex; align-items: center; justify-content: center; color: white; font-size: 11px; font-weight: bold;">
                        {percentage:.1f}%
                    </div>
                </div>
            </div>
            <div class="expanded-section">
                <h4>⚙️ Reservoir Information</h4>
                <p>Total Depth: 1000 mm</p>
                <p>Full Capacity: 100%</p>
                <p>Empty Level: 0 mm</p>
            </div>
        '''

# Factory function for controller to create handler
def create_handler(**kwargs):
    """Factory function - automatically finds and returns the handler class in this module."""
    import inspect, sys
    for name, obj in inspect.getmembers(sys.modules[__name__]):
        if (inspect.isclass(obj) and
            issubclass(obj, NodeHandler) and
            obj != NodeHandler):
            return obj(**kwargs)
    raise RuntimeError("No NodeHandler subclass found in module")
