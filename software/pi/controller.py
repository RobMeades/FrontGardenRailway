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

# All written by DeepSeek :-)

"""
Controller for Front Garden Railway network.

Manages multiple nodes, handles FGR protocol messages, dispatches to node-specific
handlers, and maintains node state.

Node handlers are loaded dynamically from the 'nodes' directory.
Each handler file should be named 'node_*.py' and contain a class that
inherits from NodeHandler (usually named with the node type in CamelCase).

Usage:
    python controller.py [--ip LISTEN_IP] [--port PORT] [--cfg CFG_FILE]
    
Examples:
    python controller.py                          # Use defaults (10.10.3.1:5000)
    python controller.py --ip 0.0.0.0            # Listen on all interfaces
    python controller.py --port 6000             # Use port 6000
    python controller.py --ip 192.168.1.100 --port 5000
"""

import argparse
import socket
import threading
import queue
import time
import logging
import importlib
import importlib.util
import inspect
import sys
import json
import yaml
from typing import Dict, Optional, Any, List, Type
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

# ============================================================================
# Setup paths for protocol import
# ============================================================================

# Get the directory where this script is located
SCRIPT_DIR = Path(__file__).parent.absolute()

# The protocol module is in ../protocol/fgr_protocol.py relative to this script
PROTOCOL_DIR = SCRIPT_DIR.parent / "protocol"

# Add protocol directory to Python path if not already there
if str(PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_DIR))

# Now import the protocol
try:
    import fgr_protocol as fgr
except ImportError as e:
    print(f"Error: Cannot import fgr_protocol from {PROTOCOL_DIR}")
    print(f"Make sure {PROTOCOL_DIR / 'fgr_protocol.py'} exists")
    raise e


# ============================================================================
# Node State Management
# ============================================================================

class NodeState(IntEnum):
    """Node operational states (extends FGR_STATE_*)"""
    DISCONNECTED = 0
    CONNECTED = 1
    NEEDS_CFG = fgr.FGRState.FGR_STATE_NEEDS_CFG
    STARTED = fgr.FGRState.FGR_STATE_STARTED
    STOPPED = fgr.FGRState.FGR_STATE_STOPPED
    BUSY = fgr.FGRState.FGR_STATE_BUSY
    GENERIC_FAILED = fgr.FGRState.FGR_STATE_GENERIC_FAILED
    HARDWARE_FAILURE = fgr.FGRState.FGR_STATE_HARDWARE_FAILURE
    CONFIGURING = 100  # Local state while sending cfg
    READY = 101        # Configured but not started
    ERROR = 102


@dataclass
class Node:
    """Represents a connected node"""
    ip: str
    name: str
    node_type: str = ""  # e.g., "level_gauge", "stand", "lift", "door"
    sock: Optional[socket.socket] = None
    state: NodeState = NodeState.DISCONNECTED
    fgr_state: int = fgr.FGRState.FGR_STATE_NOT_POPULATED
    reference_counter: int = 0
    pending_requests: Dict[int, queue.Queue] = field(default_factory=dict)
    last_heartbeat: float = 0
    cfg_data: Optional[Dict[str, Any]] = None  # Changed from config_data
    handler: Optional['NodeHandler'] = None
    rx_thread: Optional[threading.Thread] = None
    custom_data: Dict[str, Any] = field(default_factory=dict)  # For handler-specific data


# ============================================================================
# Node Handler Base Class
# ============================================================================

class NodeHandler:
    """
    Base class for node-specific handlers.
    Override this for specific node types.
    
    To create a node handler:
    1. Create a file in the 'nodes' directory named 'node_<type>.py'
    2. Define a class that inherits from NodeHandler
    3. Override the methods you need
    4. The class name should be PascalCase of the type (e.g., LevelGaugeHandler)
    """
    
    def __init__(self, node: Node, controller: 'Controller'):
        self.node = node
        self.controller = controller
        self.logger = logging.getLogger(f"Handler.{node.name}")
    
    def on_connected(self):
        """Called when node first connects"""
        self.logger.info(f"Node connected (type={self.node.node_type})")
    
    def on_disconnected(self):
        """Called when node disconnects"""
        self.logger.info(f"Node disconnected")
    
    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle an indication message (FGR_MSG_TYPE_IND).
        Return True if handled, False to pass to generic handler.
        """
        ind_type = msg.subtype
        
        if ind_type == fgr.FGRIndRsp.FGR_IND_RSP_NEEDS_CFG:
            self.logger.info(f"Node needs configuration (state={msg.error_or_state})")
            self.node.fgr_state = msg.error_or_state
            self.node.state = NodeState.NEEDS_CFG
            self.on_needs_cfg(msg)
            return True
            
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_START:
            self.logger.info(f"Node started operating (state={msg.error_or_state})")
            self.node.fgr_state = msg.error_or_state
            self.node.state = NodeState.STARTED
            self.on_start(msg)
            return True
            
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_STOP:
            self.logger.info(f"Node stopped operating (state={msg.error_or_state})")
            self.node.fgr_state = msg.error_or_state
            self.node.state = NodeState.STOPPED
            self.on_stop(msg)
            return True
        
        return False
    
    def on_needs_cfg(self, msg: fgr.FGRMsg):
        """Called when node sends FGR_IND_RSP_NEEDS_CFG"""
        # Default behavior: send empty cfg
        self.controller.send_response_to_node(self.node.name, msg.subtype, msg.reference, b"")
    
    def on_start(self, msg: fgr.FGRMsg):
        """Called when node sends FGR_IND_RSP_START"""
        pass
    
    def on_stop(self, msg: fgr.FGRMsg):
        """Called when node sends FGR_IND_RSP_STOP"""
        pass
    
    def on_confirmation(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle a confirmation message (FGR_MSG_TYPE_CNF).
        Return True if handled, False to pass to generic handler.
        """
        return False
    
    def on_log(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle a log message (FGR_MSG_TYPE_LOG).
        Return True if handled, False to pass to generic handler.
        """
        return False
    
    def on_response(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle a response message (FGR_MSG_TYPE_RSP).
        Return True if handled, False to pass to generic handler.
        """
        return False
    
    def send_request(self, req_type: int, contents: bytes = b"",
                     timeout: float = 5.0) -> Optional[fgr.FGRMsg]:
        """
        Send a request to this node and wait for confirmation.
        Returns confirmation message or None on timeout/error.
        """
        return self.controller.send_request_to_node(self.node.name, req_type, contents, timeout)
    
    def send_response(self, rsp_type: int, reference: int, contents: bytes = b"") -> bool:
        """Send a response to an indication"""
        return self.controller.send_response_to_node(self.node.name, rsp_type, reference, contents)
    
    def set_log_level(self, level: int, timeout: float = 2.0) -> bool:
        """Set the node's log level"""
        contents = bytes([level])
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_LOG_LEVEL, contents, timeout)
        return rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE
    
    def start_logging(self, timeout: float = 2.0) -> bool:
        """Tell node to start logging"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_LOG_START, b"", timeout)
        return rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE
    
    def stop_logging(self, timeout: float = 2.0) -> bool:
        """Tell node to stop logging"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_LOG_STOP, b"", timeout)
        return rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE
    
    def reboot(self, timeout: float = 2.0) -> bool:
        """Tell node to reboot"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_REBOOT, b"", timeout)
        return rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE


# ============================================================================
# Controller Class
# ============================================================================

class Controller:
    """
    Main controller for FGR network.
    Listens for incoming connections, manages nodes, dispatches messages.
    Dynamically loads node handlers from the 'nodes' directory.
    """
    
    def __init__(self, listen_ip: str = "10.10.3.1", port: int = 5000,
                 nodes_dir: str = None):
        self.listen_ip = listen_ip
        self.port = port
        self.logger = logging.getLogger("Controller")
        
        # Determine nodes directory
        if nodes_dir is None:
            # Default: look for 'nodes' directory next to this script
            self.nodes_dir = SCRIPT_DIR / "nodes"
        else:
            self.nodes_dir = Path(nodes_dir)
        
        self.nodes: Dict[str, Node] = {}  # keyed by name
        self.nodes_by_ip: Dict[str, Node] = {}  # keyed by IP
        self.node_handlers: Dict[str, Type[NodeHandler]] = {}  # node_type -> handler class
        
        self.running = False
        self.listen_sock: Optional[socket.socket] = None
        self.listen_thread: Optional[threading.Thread] = None
        self.heartbeat_thread: Optional[threading.Thread] = None
        
        self._next_global_ref = 0
        
        # Load node handlers from nodes directory
        self._load_node_handlers()
    
    def _load_node_handlers(self):
        """Dynamically load all node handlers from the nodes directory"""
        if not self.nodes_dir.exists():
            self.logger.warning(f"Nodes directory not found: {self.nodes_dir}")
            return
        
        # Add parent directory to Python path for imports
        parent_dir = self.nodes_dir.parent
        if str(parent_dir) not in sys.path:
            sys.path.insert(0, str(parent_dir))
        
        # Find all node_*.py files
        for py_file in sorted(self.nodes_dir.glob("node_*.py")):
            if py_file.name == "node_base.py":
                continue
            
            module_name = f"nodes.{py_file.stem}"
            try:
                # Import the module
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                # Find handler classes in the module
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, NodeHandler) and obj != NodeHandler:
                        # Extract node type from filename (node_XXX.py -> XXX)
                        node_type = py_file.stem[5:]  # Remove 'node_' prefix
                        self.node_handlers[node_type] = obj
                        self.logger.info(f"Loaded handler {obj.__name__} for node type '{node_type}' from {py_file.name}")
                        
            except Exception as e:
                self.logger.error(f"Failed to load handler from {py_file.name}: {e}")
        
        if not self.node_handlers:
            self.logger.warning("No node handlers loaded")
    
    def _get_handler_for_node(self, node: Node) -> NodeHandler:
        """Create appropriate handler instance for a node"""
        # Try to get handler by node_type
        if node.node_type and node.node_type in self.node_handlers:
            handler_class = self.node_handlers[node.node_type]
            return handler_class(node, self)
        
        # Try to match by name prefix (fallback for old naming)
        for prefix, handler_class in self.node_handlers.items():
            if node.name.startswith(prefix):
                return handler_class(node, self)
        
        # Default handler
        self.logger.warning(f"No specific handler found for node '{node.name}' (type='{node.node_type}'), using base handler")
        return NodeHandler(node, self)
    
    def add_node(self, name: str, ip: str, node_type: str = "") -> None:
        """Add a node definition (pre-connection)"""
        if name in self.nodes:
            self.logger.warning(f"Node {name} already exists")
            return
        
        node = Node(ip=ip, name=name, node_type=node_type)
        self.nodes[name] = node
        self.nodes_by_ip[ip] = node
        self.logger.info(f"Added node: {name} ({ip}) type='{node_type}'")
    
    def add_nodes_from_cfg(self, cfg: Dict[str, Dict]) -> None:
        """
        Add multiple nodes from a configuration dictionary.
        Expected format:
        {
            "node_name": {"ip": "10.10.3.2", "type": "level_gauge"},
            "stand_1": {"ip": "10.10.3.3", "type": "stand"},
            ...
        }
        """
        for name, node_cfg in cfg.items():
            self.add_node(
                name=name,
                ip=node_cfg.get("ip"),
                node_type=node_cfg.get("type", "")
            )
    
    def remove_node(self, name: str) -> None:
        """Remove a node definition"""
        if name in self.nodes:
            node = self.nodes[name]
            if node.sock:
                self._disconnect_node(node)
            del self.nodes_by_ip[node.ip]
            del self.nodes[name]
            self.logger.info(f"Removed node: {name}")
    
    def _disconnect_node(self, node: Node) -> None:
        """Internal: disconnect a node"""
        # Set a flag to indicate we're disconnecting
        node.state = NodeState.DISCONNECTED
        
        if node.sock:
            try:
                # Shutdown first to break any blocking reads
                node.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                node.sock.close()
            except Exception:
                pass
            node.sock = None
        
        # Cancel pending requests
        for ref, q in node.pending_requests.items():
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        node.pending_requests.clear()
        
        # Note: Don't clear rx_thread here - let it finish on its own
        # Just call handler's on_disconnected once
        if node.handler:
            node.handler.on_disconnected()
    
    def start(self) -> bool:
        """Start the controller server"""
        self.running = True
        
        # Create listening socket
        try:
            self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_sock.bind((self.listen_ip, self.port))
            self.listen_sock.listen(10)
            self.logger.info(f"Listening on {self.listen_ip}:{self.port}")
        except Exception as e:
            self.logger.error(f"Failed to bind: {e}")
            return False
        
        # Start listening thread
        self.listen_thread = threading.Thread(target=self._accept_loop, name="Listener")
        self.listen_thread.daemon = True
        self.listen_thread.start()
        
        # Start heartbeat thread
        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="Heartbeat")
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()
        
        return True
    
    def stop(self) -> None:
        """Stop the controller"""
        self.running = False
        
        # Close all node connections
        for node in self.nodes.values():
            if node.sock:
                try:
                    node.sock.close()
                except Exception:
                    pass
        
        # Close listening socket
        if self.listen_sock:
            try:
                self.listen_sock.close()
            except Exception:
                pass
        
        # Wait for threads
        if self.listen_thread:
            self.listen_thread.join(timeout=2)
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)
        
        self.logger.info("Controller stopped")
    
    def _accept_loop(self) -> None:
        """Accept incoming connections"""
        while self.running:
            try:
                self.listen_sock.settimeout(1.0)
                client_sock, addr = self.listen_sock.accept()
                self.logger.info(f"Connection from {addr[0]}:{addr[1]}")
                
                # Find node by IP
                ip = addr[0]
                if ip in self.nodes_by_ip:
                    node = self.nodes_by_ip[ip]
                    
                    # Disconnect existing if any
                    if node.sock:
                        self._disconnect_node(node)
                    
                    node.sock = client_sock
                    node.state = NodeState.CONNECTED
                    node.last_heartbeat = time.time()
                    node.reference_counter = 0
                    node.pending_requests.clear()
                    
                    # Create handler
                    node.handler = self._get_handler_for_node(node)
                    
                    # Start receive thread
                    node.rx_thread = threading.Thread(
                        target=self._receive_loop,
                        args=(node,),
                        name=f"RX-{node.name}"
                    )
                    node.rx_thread.daemon = True
                    node.rx_thread.start()
                    
                    node.handler.on_connected()
                    self.logger.info(f"Node {node.name} connected (type={node.node_type})")
                else:
                    self.logger.warning(f"Unknown node from {ip}, closing")
                    client_sock.close()
                    
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"Accept error: {e}")
    
    def _receive_loop(self, node: Node) -> None:
        """Receive messages from a node"""
        while self.running and node.sock and node.state != NodeState.DISCONNECTED:
            try:
                msg = fgr.receive_message(node.sock, timeout=0.5)
                if msg is None:
                    # Check if we've been disconnected
                    if node.state == NodeState.DISCONNECTED:
                        break
                    continue
                
                node.last_heartbeat = time.time()
                self._dispatch_message(node, msg)
                
            except socket.error as e:
                if self.running and node.state != NodeState.DISCONNECTED:
                    # Only log if we're not already disconnecting
                    if e.errno != 9:  # Bad file descriptor - expected during disconnect
                        self.logger.error(f"Socket error from {node.name}: {e}")
                break
            except Exception as e:
                if self.running and node.state != NodeState.DISCONNECTED:
                    self.logger.error(f"Receive error from {node.name}: {e}")
                break
        
        # Only call disconnect if we're not already disconnected
        if node.state != NodeState.DISCONNECTED:
            self._disconnect_node(node)
            self.logger.info(f"Node {node.name} disconnected")
    
    def _dispatch_message(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Dispatch a message to the appropriate handler"""
        msg_type = msg.message_type
        
        if msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_CNF:
            # Confirmation - check for pending request
            ref = msg.reference
            if ref in node.pending_requests:
                q = node.pending_requests.pop(ref)
                try:
                    q.put_nowait(msg)
                except queue.Full:
                    pass
            
            # Also pass to handler
            if node.handler:
                node.handler.on_confirmation(msg)
        
        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_IND:
            # Indication
            handled = False
            if node.handler:
                handled = node.handler.on_indication(msg)
            
            if not handled:
                self._handle_generic_indication(node, msg)
        
        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_LOG:
            # Log message
            handled = False
            if node.handler:
                handled = node.handler.on_log(msg)
            
            if not handled:
                self._handle_generic_log(node, msg)
        
        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_RSP:
            # Response to an indication we sent
            handled = False
            if node.handler:
                handled = node.handler.on_response(msg)
            
            if not handled:
                self.logger.debug(f"Unhandled RSP from {node.name}: type=0x{msg.subtype:03X}")
        
        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_REQ:
            # Requests from node to controller (unusual, but handle)
            self.logger.warning(f"Unexpected REQ from {node.name}: type=0x{msg.subtype:03X}")
    
    def _handle_generic_indication(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Handle indications that weren't handled by node-specific code"""
        ind_type = msg.subtype
        
        if ind_type == fgr.FGRIndRsp.FGR_IND_RSP_NEEDS_CFG:
            self.logger.info(f"Node {node.name}: needs configuration (no handler)")
            self.send_response_to_node(node.name, ind_type, msg.reference, b"")
        
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_START:
            self.logger.info(f"Node {node.name}: started")
        
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_STOP:
            self.logger.info(f"Node {node.name}: stopped")
        
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_HEARTBEAT:
            # Heartbeat - just log at debug level
            self.logger.debug(f"Node {node.name}: heartbeat received")
            # Note: node.last_heartbeat is already updated in _receive_loop
            # Optionally send a response if nodes expect one
            # self.send_response_to_node(node.name, ind_type, msg.reference, b"")
        
        elif ind_type > fgr.FGRIndRsp.FGR_IND_RSP_LAST:
            # Device-specific indication
            self.logger.debug(f"Node {node.name}: device-specific indication 0x{ind_type:03X}, value={msg.error_or_state}")
    
    def _handle_generic_log(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Handle log messages"""
        level_map = {
            fgr.FGRLogLevel.FGR_LOG_LEVEL_DEBUG: logging.DEBUG,
            fgr.FGRLogLevel.FGR_LOG_LEVEL_INFO: logging.INFO,
            fgr.FGRLogLevel.FGR_LOG_LEVEL_WARN: logging.WARNING,
            fgr.FGRLogLevel.FGR_LOG_LEVEL_ERROR: logging.ERROR,
        }
        level = level_map.get(msg.header.log_level, logging.INFO)
        log_msg = msg.get_log_message()
        self.logger.log(level, f"[{node.name}] {log_msg}")
    
    def _heartbeat_loop(self) -> None:
        """Monitor node heartbeats - nodes must send periodic HEARTBEAT indications"""
        while self.running:
            time.sleep(30)
            now = time.time()
            for node in self.nodes.values():
                if node.sock and node.state != NodeState.DISCONNECTED:
                    # Check if we've received any message (including heartbeat) recently
                    if now - node.last_heartbeat > 60:  # 60 second timeout
                        self.logger.warning(f"Node {node.name} heartbeat timeout (last: {node.last_heartbeat:.1f}s ago)")
                        self._disconnect_node(node)
                    elif now - node.last_heartbeat > 45:  # Getting close to timeout
                        self.logger.debug(f"Node {node.name} heartbeat overdue: {now - node.last_heartbeat:.1f}s")
    
    def _get_next_reference(self, node: Node) -> int:
        """Get next reference number for a node"""
        node.reference_counter = (node.reference_counter + 1) & 0xFF
        return node.reference_counter
    
    def send_request_to_node(self, node_name: str, req_type: int,
                             contents: bytes = b"",
                             timeout: float = 5.0) -> Optional[fgr.FGRMsg]:
        """
        Send a request to a specific node and wait for confirmation.
        Returns confirmation message or None on timeout/error.
        """
        node = self.nodes.get(node_name)
        if not node or not node.sock:
            self.logger.error(f"Node {node_name} not connected")
            return None
        
        reference = self._get_next_reference(node)
        msg = fgr.FGRMsg.create_req(req_type, reference, contents)
        
        # Create queue for confirmation
        response_queue = queue.Queue(maxsize=1)
        node.pending_requests[reference] = response_queue
        
        try:
            if not fgr.send_message(node.sock, msg):
                node.pending_requests.pop(reference, None)
                return None
            
            # Wait for confirmation
            try:
                cnf = response_queue.get(timeout=timeout)
                return cnf
            except queue.Empty:
                self.logger.warning(f"Timeout waiting for confirmation from {node_name} (ref={reference})")
                node.pending_requests.pop(reference, None)
                return None
                
        except Exception as e:
            self.logger.error(f"Error sending to {node_name}: {e}")
            node.pending_requests.pop(reference, None)
            return None
    
    def send_response_to_node(self, node_name: str, rsp_type: int,
                              reference: int, contents: bytes = b"") -> bool:
        """Send a response to an indication"""
        node = self.nodes.get(node_name)
        if not node or not node.sock:
            self.logger.error(f"Node {node_name} not connected")
            return False
        
        msg = fgr.FGRMsg.create_rsp(rsp_type, reference, contents)
        return fgr.send_message(node.sock, msg)
    
    def send_request_to_all(self, req_type: int, contents: bytes = b"",
                            timeout: float = 5.0) -> Dict[str, Optional[fgr.FGRMsg]]:
        """Send a request to all connected nodes"""
        results = {}
        for node_name in self.nodes:
            if self.nodes[node_name].sock:
                rsp = self.send_request_to_node(node_name, req_type, contents, timeout)
                results[node_name] = rsp
        return results
    
    def cfg_node(self, node_name: str, cfg_data: bytes,
                 timeout: float = 5.0) -> bool:
        """Send configuration to a node"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_CFG,
                                        cfg_data, timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = NodeState.READY
                node.cfg_data = {"raw": cfg_data}  # Store raw cfg
            return True
        return False
    
    def start_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Start a node's operation"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_START,
                                        b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = NodeState.STARTED
            return True
        return False
    
    def stop_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Stop a node's operation"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_STOP,
                                        b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = NodeState.STOPPED
            return True
        return False
    
    def reboot_node(self, node_name: str, timeout: float = 2.0) -> bool:
        """Reboot a node"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_REBOOT,
                                        b"", timeout)
        return cnf is not None and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE
    
    def set_node_log_level(self, node_name: str, level: int, timeout: float = 2.0) -> bool:
        """Set a node's log level"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_LOG_LEVEL,
                                        bytes([level]), timeout)
        return cnf is not None and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE
    
    def get_node(self, name: str) -> Optional[Node]:
        """Get node by name"""
        return self.nodes.get(name)
    
    def get_nodes_by_state(self, state: NodeState) -> List[Node]:
        """Get all nodes in a given state"""
        return [n for n in self.nodes.values() if n.state == state]
    
    def get_connected_nodes(self) -> List[Node]:
        """Get all connected nodes"""
        return [n for n in self.nodes.values() if n.sock is not None]
    
    def get_node_names(self) -> List[str]:
        """Get list of all node names"""
        return list(self.nodes.keys())
    
    def get_node_types(self) -> List[str]:
        """Get list of available node handler types"""
        return list(self.node_handlers.keys())
    
    def reload_handlers(self) -> None:
        """Reload node handlers from disk (useful for development)"""
        self.logger.info("Reloading node handlers...")
        # Clear existing handlers
        self.node_handlers.clear()
        # Reload
        self._load_node_handlers()
        
        # Recreate handlers for connected nodes
        for node in self.nodes.values():
            if node.sock and node.handler:
                node.handler = self._get_handler_for_node(node)
                self.logger.info(f"Updated handler for {node.name}")


# ============================================================================
# Configuration Loading
# ============================================================================

def load_node_cfg(cfg_file: Path) -> Dict[str, Dict]:
    """Load node configuration from JSON or YAML file"""
    if not cfg_file.exists():
        raise FileNotFoundError(f"Configuration file not found: {cfg_file}")
    
    with open(cfg_file, 'r') as f:
        if cfg_file.suffix in ['.yaml', '.yml']:
            return yaml.safe_load(f)
        else:
            return json.load(f)


def get_default_node_cfg() -> Dict[str, Dict]:
    """Get the default node configuration"""
    return {
        # Test node for development
        "test_1": {
            "ip": "10.10.3.2",
            "type": "test"
        },
        # Level gauge node
        "level_gauge_1": {
            "ip": "10.10.3.3",
            "type": "level_gauge"
        }
    }


# ============================================================================
# Command Line Argument Parsing
# ============================================================================

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="FGR Railway Controller",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # Use defaults (10.10.3.1:5000)
  %(prog)s --ip 0.0.0.0                # Listen on all interfaces
  %(prog)s --port 6000                 # Use port 6000
  %(prog)s --ip 192.168.1.100 --port 5000
  %(prog)s --cfg nodes.yaml            # Load nodes from config file
  %(prog)s --log-level DEBUG           # Enable debug logging
        """
    )
    
    parser.add_argument(
        "--ip",
        type=str,
        default="10.10.3.1",
        help="IP address to listen on (default: 10.10.3.1)"
    )
    
    parser.add_argument(
        "--port",
        type=int,
        default=5000,
        help="Port to listen on (default: 5000)"
    )
    
    parser.add_argument(
        "--cfg",
        type=Path,
        help="Path to node configuration file (JSON or YAML)"
    )
    
    parser.add_argument(
        "--nodes-dir",
        type=Path,
        help="Directory containing node handlers (default: ./nodes)"
    )
    
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )
    
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Write logs to file instead of console"
    )
    
    return parser.parse_args()


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point"""
    args = parse_args()
    
    # Setup logging
    log_level = getattr(logging, args.log_level)
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    if args.log_file:
        logging.basicConfig(
            level=log_level,
            format=log_format,
            filename=args.log_file,
            filemode='a'
        )
        # Also log to console
        console = logging.StreamHandler()
        console.setLevel(log_level)
        console.setFormatter(logging.Formatter(log_format))
        logging.getLogger('').addHandler(console)
    else:
        logging.basicConfig(
            level=log_level,
            format=log_format
        )
    
    logger = logging.getLogger("Main")
    
    # Create controller
    controller = Controller(
        listen_ip=args.ip,
        port=args.port,
        nodes_dir=args.nodes_dir
    )
    
    # Load node configuration
    if args.cfg:
        try:
            node_cfg = load_node_cfg(args.cfg)
            logger.info(f"Loaded node configuration from {args.cfg}")
        except Exception as e:
            logger.error(f"Failed to load configuration: {e}")
            sys.exit(1)
    else:
        node_cfg = get_default_node_cfg()
        logger.info("Using default node configuration")
    
    # Add nodes to controller
    controller.add_nodes_from_cfg(node_cfg)
    
    # Print startup information
    print("\n" + "=" * 60)
    print("FGR Railway Controller")
    print("=" * 60)
    print(f"Listening on:    {args.ip}:{args.port}")
    print(f"Log level:       {args.log_level}")
    print(f"Nodes directory: {controller.nodes_dir}")
    print(f"Protocol from:   {PROTOCOL_DIR / 'fgr_protocol.py'}")
    print("\nConfigured nodes:")
    for name, node in controller.nodes.items():
        print(f"  - {name:20} {node.ip:15} (type: {node.node_type or 'none'})")
    print("=" * 60)
    print("Press Ctrl+C to stop\n")
    
    # Start controller
    if controller.start():
        try:
            # Keep main thread alive
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            controller.stop()
            print("Controller stopped")
    else:
        logger.error("Failed to start controller")
        sys.exit(1)


if __name__ == "__main__":
    main()
