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
import sys
from pathlib import Path

# NodeHandler and FGR protocol are injected by the controller
# Add protocol directory for IDE type checking
PROTOCOL_DIR = Path(__file__).parent.parent.parent / "protocol"
if str(PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_DIR))

try:
    import fgr_protocol as fgr
except ImportError:
    # Will be injected by controller at runtime
    pass

try:
    from controller import NodeHandler
except ImportError:
    # Will be injected by controller at runtime
    pass

# Reporting interval in seconds
REPORTING_INTERVAL_SECONDS = 60

# Reservoir depth constant (mm from top to full)
RESERVOIR_DEPTH_MM = 1000

class LevelGaugeHandler(NodeHandler):
    """
    A level gauge node handler.
    """

    def __init__(self, *args, **kwargs):
        """Initialize the handler."""
        super().__init__(*args, **kwargs)

        try:
            self.reporting_interval_seconds = -1
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
        self.logger.info(f"Node {self.node.name} needs configuration, sending level gauge cfg")

        # Send body with a uint32_t containing the measurement reporting interval in seconds
        contents = REPORTING_INTERVAL_SECONDS.to_bytes(4, 'big')  # network byte order
        self.send_response(msg.subtype, msg.reference, contents)

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Observe indications in order to capture the measurement
        reporting configuration from an FGR_IND_RSP_START_IND
        """
        self.logger.debug(f"LevelGaugeHandler on_indication() called")

        # Let parent handle standard protocol and update state
        is_standard = super().on_indication(msg)

        self.logger.debug(f"In LevelGaugeHandler, msg subtype=0x{msg.subtype:03X}")

        if msg.subtype == fgr.FGRIndRsp.FGR_IND_RSP_START:
            if len(msg.contents) >= 4:
                self.reporting_interval_seconds = int.from_bytes(msg.contents[:4], 'big')
                self.logger.info(f"Node {self.node.name} reporting interval: {self.reporting_interval_seconds} seconds")
            else:
                self.logger.info(f"WARNING node {self.node.name} observed FGR_IND_RSP_START_IND"
                                 f" with unknown message contents: {msg.contents}")

        # Return what super() would have returned
        return is_standard

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
                water_height = RESERVOIR_DEPTH_MM - level_mm
                percentage = (water_height / RESERVOIR_DEPTH_MM) * 100

                self.logger.info(f"Level gauge reading: {level_mm} mm from top -> {water_height} mm water ({percentage:.1f}%)")

                if hasattr(self.controller, 'update_node_custom_data'):
                    self.controller.update_node_custom_data(self.node.name, {
                        'level': level_mm,
                        'water_height': max(0, water_height),
                        'percentage': max(0, min(100, percentage)),
                        'type': 'level_gauge'
                    })
            return True

        # Unknown node-specific indication
        self.logger.warning(f"Unknown node-specific indication: 0x{ind_type:03X}")
        return False

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