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
Test node handler.

This handler inherits from NodeHandler (injected by controller).
All notification logic is handled by the WebController.
"""

# NodeHandler and FGR protocol are injected by the controller

class TestHandler(NodeHandler):
    """
    A test node handler for prototyping new features.
    """

    def on_connected(self):
        """Called when the node first connects"""
        self.logger.info(f"Test node {self.node.name} is online")

    def on_disconnected(self):
        """Called when the node disconnects"""
        self.logger.info(f"Test node {self.node.name} went offline")

    def on_needs_cfg(self, msg: fgr.FGRMsg):
        """Called when node needs configuration - send custom config data"""
        self.logger.info(f"Node {self.node.name} needs configuration, sending test config")

        # Build custom configuration data for test node
        # This could be anything specific to your test node
        config_data = b'\x01'  # Example: set test mode to 1

        # Send response with custom config
        self.send_response(msg.subtype, msg.reference, config_data)

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """Handle device-specific indications"""
        ind_type = msg.subtype

        # Let base class handle standard protocol (NEEDS_CFG, START, STOP)
        # This updates node state and triggers on_needs_cfg above
        super().on_indication(msg)

        # Handle device-specific test data (from contents)
        if ind_type > fgr.FGRIndRsp.FGR_IND_RSP_LAST:
            if len(msg.contents) > 0:
                value = msg.contents[0]
                self.logger.info(f"Test node {self.node.name} sent test value: {value}")

                if hasattr(self.controller, 'update_node_measurement'):
                    self.controller.update_node_measurement(self.node.name, {
                        'value': value,
                        'type': 'test'
                    })

        return True