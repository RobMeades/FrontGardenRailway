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

This handler inherits from NodeHandler (injected by controller),
and therefore has access to all of the state variables for a node
that the Controller updates.
"""

import json
import time
import threading
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

class TestHandler(NodeHandler):
    """
    A test node handler for prototyping new features.
    """
    def __init__(self, *args, **kwargs):
        """Initialize the test handler."""
        super().__init__(*args, **kwargs)

        try:
            self._counter = 0
            self._last_update = time.time()
            self._generator_thread = None
            self._running = False
            self._log_init_exit()
        except Exception as e:
            self._log_init_error(e)
            raise

    def on_connected(self):
        """Called when the node first connects"""
        self.logger.info(f"Test node {self.node.name} is online")
        # Start generating test data
        self._start_test_data_generation()

    def on_disconnected(self):
        """Called when the node disconnects"""
        self.logger.info(f"Test node {self.node.name} went offline")
        self._running = False
        # Wait for thread to finish
        if self._generator_thread and self._generator_thread.is_alive():
            self._generator_thread.join(timeout=1)

    def on_needs_cfg(self, msg: fgr.FGRMsg):
        """Called when node needs configuration - send custom config data"""
        self.logger.info(f"Node {self.node.name} needs configuration, sending test config")

        # Build custom configuration data for test node
        config_data = b'\x01'  # Example: set test mode to 1

        # Send response with custom config
        self.send_response(msg.subtype, msg.reference, config_data)

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Observe ALL indications (optional).
        Call parent first to handle standard protocol.
        """
        # Let parent handle standard protocol and update state
        is_standard = super().on_indication(msg)

        # OBSERVATION: Log for debugging (optional)
        ind_type = msg.subtype
        if is_standard:
            self.logger.debug(f"Standard indication: {ind_type}")
        else:
            self.logger.debug(f"Node-specific indication: {ind_type}")

        return is_standard

    def on_node_specific_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle node-specific indications (REQUIRED).
        Called only for message types unknown to the base class
        """
        if len(msg.contents) > 0:
            value = msg.contents[0]
            self.logger.info(f"Test node {self.node.name} sent test value: {value}")

            if hasattr(self.controller, 'update_node_custom_data'):
                self.controller.update_node_custom_data(self.node.name, {
                    'value': value,
                    'type': 'test',
                    'last_update': time.time()
                })
        return True  # Device-specific handled

    def _start_test_data_generation(self):
        """Start a background thread to generate test data"""
        if self._generator_thread and self._generator_thread.is_alive():
            return

        self._running = True

        def generate_data():
            while self._running and self.node and self.node.sock:
                if self.node.fgr_state == fgr.FGRState.FGR_STATE_STARTED:
                    self._counter += 1
                current_time = time.time()

                # Update custom data with incrementing counter
                if hasattr(self.controller, 'update_node_custom_data'):
                    self.controller.update_node_custom_data(self.node.name, {
                        'value': self._counter,
                        'counter': self._counter,
                        'last_update': current_time,
                        'uptime': current_time - self._last_update,
                        'type': 'test'
                    })

                self.logger.debug(f"Generated test data: counter={self._counter}")
                time.sleep(5)  # Update every 5 seconds

        self._generator_thread = threading.Thread(target=generate_data, daemon=True)
        self._generator_thread.start()

    def get_card_html(self, node_name: str, node_data: Dict[str, Any]) -> str:
        custom_data = node_data.get('custom_data', {})
        value = custom_data.get('value', 'N/A')
        counter = custom_data.get('counter', 'N/A')

        return f'''
            <div class="node-custom">
                <div class="custom-value" data-dynamic="value">{value}</div>
                <div class="custom-unit">Current Value</div>
                <div style="margin-top: 8px; font-size: 10px; color: #666;">
                    Counter: <span data-dynamic="counter">{counter}</span>
                </div>
            </div>
        '''

    def get_expanded_html(self, node_name: str, node_data: Dict[str, Any]) -> str:
        """Return HTML for the expanded view (node-specific content only)"""
        custom_data = node_data.get('custom_data', {})
        value = custom_data.get('value', 'N/A')
        counter = custom_data.get('counter', 'N/A')
        uptime = custom_data.get('uptime', 0)

        # Format uptime
        if isinstance(uptime, (int, float)):
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)
            if hours > 0:
                uptime_str = f"{hours}h {minutes}m {seconds}s"
            elif minutes > 0:
                uptime_str = f"{minutes}m {seconds}s"
            else:
                uptime_str = f"{seconds}s"
        else:
            uptime_str = "N/A"

        return f'''
            <div class="expanded-section">
                <h4>📊 Test Node Dynamic Data</h4>
                <p>Current Value: <strong><span data-dynamic="value">{value}</span></strong></p>
                <p>Incrementing Counter: <strong><span data-dynamic="counter">{counter}</span></strong></p>
                <p>Last Update: <span data-dynamic="last_update">{custom_data.get('last_update', 'N/A')}</span></p>
                <p>Data Generation Uptime: {uptime_str}</p>
            </div>
            <div class="expanded-section">
                <h4>📈 About This Demo</h4>
                <p>This test node demonstrates dynamic data updates:</p>
                <ul style="margin: 5px 0; padding-left: 20px;">
                    <li>The counter increments every 5 seconds</li>
                    <li>Both the card and expanded view update automatically</li>
                    <li>The <code>data-dynamic</code> attributes enable live updates</li>
                </ul>
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