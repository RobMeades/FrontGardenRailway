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
"""

# NodeHandler and FGR protocol are injected by the controller

class LevelGaugeHandler(NodeHandler):
    """
    A level gauge node handler that reports a level measurement.
    """
    
    def on_connected(self):
        """Called when the node first connects to the controller"""
        self.logger.info(f"Level gauge node {self.node.name} is online")
    
    def on_disconnected(self):
        """Called when the node disconnects"""
        self.logger.info(f"Level gauge {self.node.name} went offline")
    
    def on_needs_cfg(self, msg: fgr.FGRMsg):
        """Called when node reports it needs configuration"""
        self.logger.info(f"Node {self.node.name} needs configuration, sending empty CFG response")
        # Send back the same message (FGR_IND_RSP_NEEDS_CFG) as a response with the same reference
        self.send_response(msg.subtype, msg.reference, b"")
    
    def on_start(self, msg: fgr.FGRMsg):
        """Called when node reports it has started"""
        self.logger.info(f"Node {self.node.name} has started")
    
    def on_stop(self, msg: fgr.FGRMsg):
        """Called when node reports it has stopped"""
        self.logger.info(f"Node {self.node.name} has stopped")
    
    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle device-specific indications.
        Return True if handled, False otherwise.
        """
        ind_type = msg.subtype
        
        # Check if this is a device-specific indication (> FGR_IND_RSP_LAST)
        if ind_type > fgr.FGRIndRsp.FGR_IND_RSP_LAST:
            self.logger.info(f"Node {self.node.name} sent device indication: 0x{ind_type:03X}, value={msg.error_or_state}")
            return True
        
        # Not handled - let base class handle standard indications
        return super().on_indication(msg)
    
    def on_confirmation(self, msg: fgr.FGRMsg) -> bool:
        """Handle confirmation messages"""
        self.logger.info(f"Node {self.node.name} confirmed: type=0x{msg.subtype:03X}, error={msg.error_or_state}")
        return True
    
    def on_response(self, msg: fgr.FGRMsg) -> bool:
        """Handle response messages (responses to indications we sent)"""
        self.logger.info(f"Node {self.node.name} responded: type=0x{msg.subtype:03X}")
        return True
