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

#  Written by DeepSeek :-).

"""
Controller for Front Garden Railway network.

Manages multiple nodes, handles FGR protocol messages, dispatches to node-specific
handlers, and maintains node state.

Node handlers are loaded dynamically from the 'nodes' directory.
Each handler file should be named 'node_*.py' and contain a class that
inherits from NodeHandler.

Configuration hierarchy (each level overrides the previous):
1. Built-in defaults
2. nodes/<node_type>/cfg.json (per-node-type defaults)
3. nodes.json (global node configuration - highest priority)

Usage:
    python controller.py [--ip LISTEN_IP] [--port PORT] [--cfg CFG_FILE] [--log-level LEVEL]

Examples:
    python controller.py                                    # Use defaults
    python controller.py --ip 0.0.0.0                      # Listen on all interfaces
    python controller.py --port 6000                       # Use port 6000
    python controller.py --cfg my_nodes.json               # Load nodes from JSON file
    python controller.py --log-level DEBUG                 # Enable debug logging
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
import traceback
from typing import Dict, Optional, Any, List, Type
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

# ============================================================================
# Setup paths for protocol import
# ============================================================================

SCRIPT_DIR = Path(__file__).parent.absolute()
PROTOCOL_DIR = SCRIPT_DIR.parent / "protocol"

if str(PROTOCOL_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_DIR))

try:
    import fgr_protocol as fgr
except ImportError as e:
    print(f"Error: Cannot import fgr_protocol from {PROTOCOL_DIR}")
    print(f"Make sure {PROTOCOL_DIR / 'fgr_protocol.py'} exists")
    raise e


# ============================================================================
# Node State Management
# ============================================================================

class ConnectionState(IntEnum):
    """Node connection states - local states (200+) and FGR states (1-127)"""
    # Local states (200-255 range, separate from FGR states)
    DISCONNECTED = 200
    CONNECTED = 201
    CONFIGURING = 202
    READY = 203
    ERROR = 204

    # FGR states (mirror the protocol values)
    FGR_NEEDS_CFG = 1
    FGR_STARTED = 2
    FGR_STOPPED = 3
    FGR_BUSY = 4
    FGR_GENERIC_FAILED = 5
    FGR_HARDWARE_FAILURE = 6

    @classmethod
    def from_fgr_state(cls, fgr_state: int):
        """Convert FGR state to ConnectionState"""
        if fgr_state == 1:
            return cls.FGR_NEEDS_CFG
        elif fgr_state == 2:
            return cls.FGR_STARTED
        elif fgr_state == 3:
            return cls.FGR_STOPPED
        elif fgr_state == 4:
            return cls.FGR_BUSY
        elif fgr_state == 5:
            return cls.FGR_GENERIC_FAILED
        elif fgr_state == 6:
            return cls.FGR_HARDWARE_FAILURE
        return cls.DISCONNECTED


@dataclass
class Node:
    """Represents a connected node"""
    ip: str
    name: str
    node_type: str = ""
    essential: bool = True
    sock: Optional[socket.socket] = None
    state: ConnectionState = ConnectionState.DISCONNECTED
    fgr_state: int = fgr.FGRState.FGR_STATE_NOT_POPULATED
    reference_counter: int = 0
    pending_requests: Dict[int, queue.Queue] = field(default_factory=dict)
    last_heartbeat: float = 0
    cfg_data: Optional[Dict[str, Any]] = None
    handler: Optional['NodeHandler'] = None
    rx_thread: Optional[threading.Thread] = None
    handler_state: Dict[str, Any] = field(default_factory=dict)  # Handler-specific persistent state
    stop_event: threading.Event = field(default_factory=threading.Event)
    heartbeat_timeout: int = 60
    connection_time: float = 0
    last_seen: float = 0
    # Debug counters
    message_count: int = 0
    heartbeat_count: int = 0
    connection_id: int = 0
    # Status information from node
    log_on: Optional[bool] = None
    log_level: Optional[int] = None
    led_on: Optional[bool] = None
    led_breathe_on: Optional[bool] = None
    rssi: Optional[int] = None  # WiFi signal strength in dBm


# ============================================================================
# Node Handler Base Class
# ============================================================================

# ============================================================================
# NodeHandler Message Guide
# ============================================================================
#
# SENDING MESSAGES TO THE NODE:
# ----------------------------
#
# REQ → CNF (Controller initiates, blocking):
#     cnf = self.send_request(req_type, contents, timeout=5.0)
#     if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
#         response = cnf.contents
#
# IND → RSP (Node initiates, non-blocking):
#     # Standard IND (HEARTBEAT, NEEDS_CFG, START, STOP) - automatically handled
#     # Node-specific IND - override on_node_specific_indication():
#     def on_node_specific_indication(self, msg):
#         self.send_response(msg.subtype, msg.reference, response_data)
#         return True
#
# OBSERVING MESSAGES (optional):
# ------------------------------
#     def on_indication(self, msg):
#         is_standard = super().on_indication(msg)  # Let parent process
#         self.logger.debug(f"Observed: {msg.subtype}")
#         return is_standard
#
#     def on_confirmation(self, msg):
#         self.logger.debug(f"CNF: {msg.subtype}")
#         return super().on_confirmation(msg)
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

    def __init__(self, **kwargs):
        """Initialize the node handler."""
        self.node = kwargs.get('node')
        self.config = kwargs.get('config', {})
        self.logger = kwargs.get('logger')
        self.controller = kwargs.get('controller')

        # Try to get node name for logging
        if self.node and hasattr(self.node, 'name'):
            self._node_name_for_logging = self.node.name
        elif self.config and 'name' in self.config:
            self._node_name_for_logging = self.config['name']
        else:
            self._node_name_for_logging = "unknown"

        # Log entry
        if self.logger:
            self.logger.info(f"{self.__class__.__name__}.__init__() called for node {self._node_name_for_logging}")

    def _log_init_exit(self):
        if self.logger:
            self.logger.info(f"{self.__class__.__name__}.__init__() completed for node {self._node_name_for_logging}")

    def _log_init_error(self, error: Exception):
        if self.logger:
            self.logger.error(f"!!! {self.__class__.__name__}.__init__() failed for node {self._node_name_for_logging}: {error}")

    def on_connected(self):
        """Called when node first connects"""
        self.logger.info(f"Node connected (type={self.node.node_type})")

    def on_disconnected(self):
        """Called when node disconnects"""
        self.logger.info(f"Node disconnected")

    def on_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle standard FGR protocol indications.
        This is the MAIN handler that processes ALL standard protocol messages.
        Updates state, tracks heartbeats, calls on_needs_cfg(), etc.

        Child classes can OBSERVE by overriding this and calling super(),
        but they should NOT interfere with standard protocol handling.

        Returns True for standard protocol messages (handled),
        False for node-specific messages (needs child handling).
        """
        ind_type = msg.subtype

        # ALWAYS update the node's FGR state from the message
        self.node.fgr_state = msg.error_or_state
        self.node.state = ConnectionState.from_fgr_state(msg.error_or_state)

        # Handle standard protocol messages
        if ind_type == fgr.FGRIndRsp.FGR_IND_RSP_HEARTBEAT:
            self.node.heartbeat_count += 1
            self.node.last_heartbeat = time.time()
            # Extract RSSI from message contents (if present)
            if len(msg.contents) >= 1:
                rssi = msg.contents[0]
                if rssi > 127:
                    rssi = rssi - 256  # Convert to signed
                self.node.rssi = rssi
            else:
                # No RSSI in heartbeat - leave as None (will show "?")
                # Optionally log a debug message
                if self.logger.isEnabledFor(logging.DEBUG):
                    self.logger.debug(f"Heartbeat from {self.node.name} has no RSSI value")
            return True

        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_NEEDS_CFG:
            self.on_needs_cfg(msg)
            return True

        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_START:
            self.on_start(msg)
            return True

        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_STOP:
            self.on_stop(msg)
            return True

        # Must be a node-specific indication
        return False

    def on_node_specific_indication(self, msg: fgr.FGRMsg) -> bool:
        """
        Handle node-specific indications.
        MUST be overridden by child classes to handle their custom messages.

        This is called ONLY for indications not known to on_indication().
        Returns True if handled, False otherwise (will be logged).
        """
        self.logger.warning(f"Unhandled node-specific indication: 0x{msg.subtype:03X}")
        return False

    def on_needs_cfg(self, msg: fgr.FGRMsg):
        """Called when node sends FGR_IND_RSP_NEEDS_CFG"""
        self.controller.send_response_to_node(self.node.name, msg.subtype, msg.reference, b"")

    def on_start(self, msg: fgr.FGRMsg):
        """Called when node sends FGR_IND_RSP_START"""
        pass

    def on_stop(self, msg: fgr.FGRMsg):
        """Called when node sends FGR_IND_RSP_STOP"""
        pass

    def on_confirmation(self, msg: fgr.FGRMsg) -> bool:
        """Handle a confirmation message (FGR_MSG_TYPE_CNF)"""
        return False

    def on_response(self, msg: fgr.FGRMsg) -> bool:
        """Handle a response message (FGR_MSG_TYPE_RSP)"""
        return False

    def send_request(self, req_type: int, contents: bytes = b"",
                     timeout: float = 5.0) -> Optional[fgr.FGRMsg]:
        """
        Send REQ to node, wait for CNF response (blocking).

        Returns CNF message on success, None on timeout/error.
        Check cnf.error_or_state for success/failure.
        """
        return self.controller.send_request_to_node(self.node.name, req_type, contents, timeout)

    def send_response(self, rsp_type: int, reference: int, contents: bytes = b"") -> bool:
        """Send a response to an indication"""
        return self.controller.send_response_to_node(self.node.name, rsp_type, reference, contents)

    def set_log_level(self, level: int, timeout: float = 2.0) -> bool:
        """Set the node's log level"""
        contents = bytes([level])
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_LOG_LEVEL, contents, timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.log_level = level
            return True
        return False

    def start_logging(self, timeout: float = 2.0) -> bool:
        """Tell node to start logging"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_LOG_START, b"", timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.log_on = True
            return True
        return False

    def stop_logging(self, timeout: float = 2.0) -> bool:
        """Tell node to stop logging"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_LOG_STOP, b"", timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.log_on = False
            return True
        return False

    def led_on(self, timeout: float = 2.0) -> bool:
        """Tell node to turn debug LED on"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_DEBUG_LED_ON, b"", timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.led_on = True
            return True
        return False

    def led_off(self, timeout: float = 2.0) -> bool:
        """Tell node to turn debug LED off"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_DEBUG_LED_OFF, b"", timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.led_on = False
            return True
        return False

    def led_breathe_on(self, timeout: float = 2.0) -> bool:
        """Tell node to turn debug LED breathe on"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_DEBUG_LED_BREATHE_ON, b"", timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.led_breathe_on = True
            return True
        return False

    def led_breathe_off(self, timeout: float = 2.0) -> bool:
        """Tell node to turn debug LED breathe off"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_DEBUG_LED_BREATHE_OFF, b"", timeout)
        if rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            self.node.led_breathe_on = False
            return True
        return False

    def reboot(self, timeout: float = 2.0) -> bool:
        """Tell node to reboot"""
        rsp = self.send_request(fgr.FGRReqCnf.FGR_REQ_CNF_REBOOT, b"", timeout)
        return rsp is not None and rsp.error_or_state == fgr.FGRError.FGR_ERROR_NONE

    # ========================================================================
    # Web Interface HTML Methods
    # ========================================================================

    def get_card_html(self, node_name: str, node_data: Dict[str, Any]) -> str:
        """
        Return HTML snippet for the card's center area.

        MUST be overridden by node handlers to display custom information.
        If not overridden, returns a placeholder.
        """
        return '<div class="node-custom-placeholder">No data (handler must override get_card_html)</div>'

    def get_expanded_html(self, node_name: str, node_data: Dict[str, Any]) -> str:
        """
        Return HTML for expanded view (full grid width).

        MUST be overridden by node handlers for detailed node information.
        If not overridden, returns a placeholder.
        """
        return '''
            <div class="expanded-node">
                <div class="expanded-header">
                    <h3>No Expanded View</h3>
                    <button class="collapse-btn">✕ Collapse</button>
                </div>
                <div class="expanded-content">
                    <div class="expanded-section">
                        <p>This node handler does not provide an expanded view.</p>
                        <p>Override get_expanded_html() in the node handler to add custom content.</p>
                    </div>
                </div>
            </div>
        '''


# ============================================================================
# Configuration Manager
# ============================================================================

class ConfigManager:
    """
    Manages hierarchical node configuration.

    Priority (highest to lowest):
    1. Global node config (nodes.json)
    2. Per-node-type config (nodes/<type>/cfg.json)
    3. Built-in defaults
    """

    DEFAULTS = {
        "heartbeat_timeout": 60
    }

    def __init__(self, nodes_dir: Path, global_cfg_file: Optional[Path] = None):
        self.nodes_dir = nodes_dir
        self.global_cfg: Dict[str, Dict] = {}
        self.type_cfgs: Dict[str, Dict] = {}

        if global_cfg_file and global_cfg_file.exists():
            self._load_global_cfg(global_cfg_file)

        self._load_type_cfgs()

    def _load_global_cfg(self, cfg_file: Path):
        """Load global configuration file"""
        try:
            with open(cfg_file, 'r') as f:
                self.global_cfg = json.load(f)
            logging.getLogger("Config").info(f"Loaded global config from {cfg_file}")
        except Exception as e:
            logging.getLogger("Config").error(f"Failed to load global config: {e}")

    def _load_type_cfgs(self):
        """Load per-node-type configuration files (nodes/<type>/cfg.json)"""
        if not self.nodes_dir.exists():
            return

        for type_dir in self.nodes_dir.glob("node_*"):
            if not type_dir.is_dir():
                continue

            node_type = type_dir.name[5:]  # Remove 'node_' prefix
            cfg_file = type_dir / "cfg.json"

            if cfg_file.exists():
                try:
                    with open(cfg_file, 'r') as f:
                        self.type_cfgs[node_type] = json.load(f)
                    logging.getLogger("Config").info(f"Loaded type config for '{node_type}' from {cfg_file}")
                except Exception as e:
                    logging.getLogger("Config").error(f"Failed to load type config for '{node_type}': {e}")

    def get_node_config(self, name: str, node_type: str = "") -> Dict[str, Any]:
        """Get merged configuration for a node"""
        config = self.DEFAULTS.copy()

        if node_type and node_type in self.type_cfgs:
            config.update(self.type_cfgs[node_type])

        if name in self.global_cfg:
            config.update(self.global_cfg[name])

        return config

    def get_all_nodes(self) -> Dict[str, Dict]:
        """Get all nodes from global config"""
        return self.global_cfg.copy()


# ============================================================================
# Controller Class
# ============================================================================

class Controller:
    """
    Main controller for FGR network.
    Listens for incoming connections, manages nodes, dispatches messages.
    """

    def __init__(self, listen_ip: str = "10.10.3.1", port: int = 5000,
                 nodes_dir: str = None, cfg_file: str = None):
        self.listen_ip = listen_ip
        self.port = port
        self.logger = logging.getLogger("Controller")

        if nodes_dir is None:
            self.nodes_dir = SCRIPT_DIR / "nodes"
        else:
            self.nodes_dir = Path(nodes_dir)

        self.config_mgr = ConfigManager(self.nodes_dir, cfg_file)

        self.nodes: Dict[str, Node] = {}
        self.nodes_by_ip: Dict[str, Node] = {}
        self.node_handlers: Dict[str, Type[NodeHandler]] = {}

        self.running = False
        self.listen_sock: Optional[socket.socket] = None
        self.listen_thread: Optional[threading.Thread] = None
        self.heartbeat_thread: Optional[threading.Thread] = None

        self._load_node_handlers()
        self._load_nodes_from_cfg()

    def _hex_dump(self, data: bytes, max_bytes: int = 32) -> str:
        """Return a hex dump of data for debugging"""
        if not data:
            return "(empty)"
        hex_str = ' '.join(f'{b:02x}' for b in data[:max_bytes])
        if len(data) > max_bytes:
            hex_str += f' ... (+{len(data)-max_bytes} bytes)'
        return hex_str

    def _load_node_handlers(self):
        """Dynamically load all node handlers from the nodes directory"""
        if not self.nodes_dir.exists():
            self.logger.warning(f"Nodes directory not found: {self.nodes_dir}")
            return

        self.logger.info(f"Looking for handlers in: {self.nodes_dir}")

        parent_dir = self.nodes_dir.parent
        if str(parent_dir) not in sys.path:
            sys.path.insert(0, str(parent_dir))

        for py_file in sorted(self.nodes_dir.glob("node_*.py")):
            if py_file.name == "node_base.py":
                continue

            module_name = f"nodes.{py_file.stem}"

            try:
                # Read the file first to check for obvious syntax errors
                with open(py_file, 'r') as f:
                    source = f.read()
                    try:
                        compile(source, str(py_file), 'exec')
                    except SyntaxError as e:
                        self.logger.error(f"SYNTAX ERROR in {py_file.name}: {e}")
                        self.logger.error(f"  Line {e.lineno}: {e.text.strip() if e.text else '?'}")
                        continue  # Skip this file

                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None:
                    self.logger.error(f"Could not create spec for {py_file.name}")
                    continue

                module = importlib.util.module_from_spec(spec)

                # Inject dependencies
                module.NodeHandler = NodeHandler
                module.fgr = fgr

                # Execute with explicit error handling
                try:
                    spec.loader.exec_module(module)
                except Exception as e:
                    self.logger.error(f"Failed to exec_module for {py_file.name}: {e}")
                    import traceback
                    self.logger.error(f"Traceback:\n{traceback.format_exc()}")
                    continue

                # Find handler classes
                found_handler = False
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    try:
                        is_subclass = issubclass(obj, NodeHandler) and obj != NodeHandler
                    except TypeError:
                        continue

                    if is_subclass:
                        node_type = py_file.stem[5:]
                        self.node_handlers[node_type] = obj
                        self.logger.info(f"Loaded handler {obj.__name__} for node type '{node_type}'")
                        found_handler = True

                if not found_handler:
                    self.logger.warning(f"No NodeHandler subclass found in {py_file.name}")

            except Exception as e:
                self.logger.error(f"Failed to load handler from {py_file.name}: {e}")
                import traceback
                self.logger.error(f"Traceback:\n{traceback.format_exc()}")


    def _load_nodes_from_cfg(self):
        """Load node definitions from configuration"""
        nodes_cfg = self.config_mgr.get_all_nodes()

        for name, node_cfg in nodes_cfg.items():
            ip = node_cfg.get("ip")
            if not ip:
                self.logger.error(f"Node {name} has no IP address, skipping")
                continue

            node_type = node_cfg.get("type", "")
            essential = node_cfg.get("essential", True)
            merged_cfg = self.config_mgr.get_node_config(name, node_type)
            heartbeat_timeout = merged_cfg.get("heartbeat_timeout", 60)

            self.add_node(name, ip, node_type, heartbeat_timeout, essential)

    def _get_handler_for_node(self, node: Node) -> NodeHandler:
        """Create appropriate handler instance for a node"""
        kwargs = {
            'node': node,
            'controller': self,
            'logger': self.logger,
            'config': node.cfg_data or {}
        }

        if node.node_type and node.node_type in self.node_handlers:
            return self.node_handlers[node.node_type](**kwargs)

        for prefix, handler_class in self.node_handlers.items():
            if node.name.startswith(prefix):
                return handler_class(**kwargs)

        return NodeHandler(**kwargs)

    def add_node(self, name: str, ip: str, node_type: str = "",
                 heartbeat_timeout: int = 60, essential: bool = True) -> None:
        """Add a node definition"""
        if name in self.nodes:
            self.logger.warning(f"Node {name} already exists")
            return

        node = Node(ip=ip, name=name, node_type=node_type,
                   heartbeat_timeout=heartbeat_timeout, essential=essential)
        self.nodes[name] = node
        self.nodes_by_ip[ip] = node
        self.logger.info(f"Added node: {name} ({ip}) type='{node_type}', timeout={heartbeat_timeout}s, essential={essential}")

    def _query_node_status(self, node: Node):
        """Query log and LED status from node after connection"""
        if not node.sock:
            return

        # Query log status
        cnf = self.send_request_to_node(node.name, fgr.FGRReqCnf.FGR_REQ_CNF_LOG_STATUS, b"", timeout=3.0)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE and len(cnf.contents) >= 2:
            node.log_on = bool(cnf.contents[0])
            node.log_level = cnf.contents[1]
            self.logger.info(f"Node {node.name}: log_on={node.log_on}, log_level={node.log_level}")
        else:
            self.logger.debug(f"Node {node.name}: log status query failed/timeout")
            # Leave as None (unknown)

        # Query debug LED status
        cnf = self.send_request_to_node(node.name, fgr.FGRReqCnf.FGR_REQ_CNF_DEBUG_LED_STATUS, b"", timeout=3.0)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE and len(cnf.contents) >= 2:
            node.led_on = bool(cnf.contents[0])
            node.led_breathe_on = bool(cnf.contents[1])
            self.logger.info(f"Node {node.name}: led_on={node.led_on}, led_breathe_on={node.led_breathe_on}")
        else:
            self.logger.debug(f"Node {node.name}: LED status query failed/timeout")
            # Leave as None (unknown)

    def _disconnect_node(self, node: Node) -> None:
        """Internal: disconnect a node"""
        if node.state == ConnectionState.DISCONNECTED:
            return

        # Record last seen time before disconnecting
        node.last_seen = time.time()

        # Store reference to current socket to prevent race conditions
        current_sock = node.sock
        current_thread_name = threading.current_thread().name

        self.logger.debug(f"[{node.name}] Disconnecting node (msgs_rcvd={node.message_count}, "
                        f"heartbeats={node.heartbeat_count}, state={node.state}, "
                        f"thread={current_thread_name})")

        # Set stop event first to signal receive thread
        node.stop_event.set()

        # Close socket properly - but only if it matches the current one
        if node.sock:
            try:
                # Try to shutdown gracefully first
                node.sock.shutdown(socket.SHUT_RDWR)
            except Exception as e:
                # Ignore errors on shutdown - socket might already be closed
                self.logger.debug(f"Error during socket shutdown for {node.name}: {e}")
            try:
                node.sock.close()
            except Exception as e:
                self.logger.debug(f"Error during socket close for {node.name}: {e}")
            node.sock = None

        # Clear all pending requests
        for ref, q in node.pending_requests.items():
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        node.pending_requests.clear()

        # Update state only if we're not already disconnected
        # This prevents race conditions where multiple threads try to disconnect
        if node.state != ConnectionState.DISCONNECTED:
            old_state = node.state
            node.state = ConnectionState.DISCONNECTED

            # Reset FGR state as well
            node.fgr_state = fgr.FGRState.FGR_STATE_NOT_POPULATED

            self.logger.info(f"Node {node.name} disconnected (old_state={old_state}, last_seen={node.last_seen})")

            # Notify handler - but only if this isn't a duplicate disconnect
            if node.handler:
                try:
                    node.handler.on_disconnected()
                except Exception as e:
                    self.logger.error(f"Error in on_disconnected() for {node.name}: {e}")
        else:
            self.logger.debug(f"[{node.name}] Already disconnected, skipping duplicate disconnect")

    def start(self) -> bool:
        """Start the controller server"""
        self.running = True

        try:
            self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listen_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_sock.bind((self.listen_ip, self.port))
            self.listen_sock.listen(10)
            self.logger.info(f"Listening on {self.listen_ip}:{self.port}")
        except Exception as e:
            self.logger.error(f"Failed to bind: {e}")
            return False

        self.listen_thread = threading.Thread(target=self._accept_loop, name="Listener")
        self.listen_thread.daemon = True
        self.listen_thread.start()

        self.heartbeat_thread = threading.Thread(target=self._heartbeat_loop, name="Heartbeat")
        self.heartbeat_thread.daemon = True
        self.heartbeat_thread.start()

        return True

    def stop(self) -> None:
        """Stop the controller"""
        self.running = False

        for node in self.nodes.values():
            node.stop_event.set()

        for node in self.nodes.values():
            if node.sock:
                try:
                    node.sock.close()
                except Exception:
                    pass

        if self.listen_sock:
            try:
                self.listen_sock.close()
            except Exception:
                pass

        if self.listen_thread:
            self.listen_thread.join(timeout=2)
        for node in self.nodes.values():
            if node.rx_thread and node.rx_thread.is_alive():
                node.rx_thread.join(timeout=1)
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

                # Peek at first bytes for debugging
                if self.logger.isEnabledFor(logging.DEBUG):
                    try:
                        client_sock.settimeout(0.5)
                        peek_data = client_sock.recv(8, socket.MSG_PEEK)
                        if peek_data:
                            self.logger.debug(f"First bytes from {addr[0]}: {self._hex_dump(peek_data)}")
                    except:
                        pass
                    client_sock.settimeout(None)

                ip = addr[0]
                if ip in self.nodes_by_ip:
                    node = self.nodes_by_ip[ip]

                    # Debug: Log active threads for this node
                    node_threads = [t.name for t in threading.enumerate() if node.name in t.name and t.is_alive()]
                    if node_threads:
                        self.logger.info(f"Active threads for {node.name} before reconnect: {node_threads}")

                    self.logger.info(f"Found node {node.name} for IP {ip}, current state={node.state}")

                    # If node already has a socket, clean it up properly
                    if node.sock:
                        self.logger.info(f"Node {node.name} already has socket, cleaning up old connection")

                        # Set stop event first to signal old receive thread
                        node.stop_event.set()

                        # Wait for old receive thread to finish before proceeding
                        if node.rx_thread and node.rx_thread.is_alive():
                            self.logger.info(f"Waiting for old receive thread for {node.name} to finish...")
                            node.rx_thread.join(timeout=2.0)
                            if node.rx_thread.is_alive():
                                self.logger.warning(f"Old receive thread for {node.name} did not terminate after 2 seconds!")
                            else:
                                self.logger.info(f"Old receive thread for {node.name} terminated successfully")

                        # Close the old socket (after thread has finished)
                        old_sock = node.sock
                        node.sock = None
                        try:
                            old_sock.shutdown(socket.SHUT_RDWR)
                        except Exception as e:
                            self.logger.debug(f"Error shutting down old socket: {e}")
                        try:
                            old_sock.close()
                        except Exception as e:
                            self.logger.debug(f"Error closing old socket: {e}")

                        # Clear pending requests
                        for ref, q in node.pending_requests.items():
                            try:
                                q.put_nowait(None)
                            except queue.Full:
                                pass
                        node.pending_requests.clear()

                        # Reset reference counter to avoid confusion
                        node.reference_counter = 0

                        self.logger.info(f"Cleanup complete for {node.name}, ready for new connection")

                    # ALWAYS reset stop event for a new connection
                    # This must happen whether there was an old socket or not,
                    # because a previous disconnection may have left stop_event.set()
                    node.stop_event.clear()
                    node.stop_event = threading.Event()  # Fresh event for new connection

                    # Reset node state for reconnection
                    node.sock = client_sock
                    node.state = ConnectionState.CONNECTED
                    node.last_heartbeat = time.time()
                    node.message_count = 0
                    node.heartbeat_count = 0
                    node.connection_id += 1
                    node.connection_time = time.time()
                    self.logger.info(f"Node {node.name} connected at {node.connection_time} (connection #{node.connection_id})")

                    # Create handler if needed
                    if not node.handler:
                        node.handler = self._get_handler_for_node(node)

                    # CAPTURE the socket BEFORE creating the thread to avoid race conditions
                    captured_sock = node.sock

                    # Start new receive thread with captured socket
                    node.rx_thread = threading.Thread(
                        target=self._receive_loop,
                        args=(node, captured_sock),  # ← Pass the captured socket
                        name=f"RX-{node.name}"
                    )
                    node.rx_thread.daemon = True
                    node.rx_thread.start()

                    # Query status from node (log and LED settings)
                    self._query_node_status(node)

                    # Notify handler of successful connection
                    try:
                        node.handler.on_connected()
                        self.logger.info(f"Node {node.name} connected (type={node.node_type})")
                    except Exception as e:
                        self.logger.error(f"Error in on_connected() for {node.name}: {e}")
                else:
                    self.logger.warning(f"Unknown node from {ip}, closing connection")
                    client_sock.close()

            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    self.logger.error(f"Accept error: {e}")
                    import traceback
                    traceback.print_exc()

    def _receive_loop(self, node: Node, captured_sock: socket.socket) -> None:
        """Receive messages from a node

        Args:
            node: The node object
            captured_sock: The socket captured at thread creation time.
                        This prevents race conditions where node.sock might
                        be changed by a new connection before this thread runs.
        """
        msg_type_names = {1: "REQ", 2: "CNF", 3: "IND", 4: "RSP", 5: "LOG"}

        # Use the captured socket, not node.sock (which might change)
        my_sock = captured_sock
        my_thread_name = threading.current_thread().name

        self.logger.info(f"Starting receive loop for {node.name} (thread={my_thread_name}, socket={my_sock.fileno() if my_sock else 'None'})")

        # Sanity check - make sure the socket is still valid
        if not my_sock:
            self.logger.error(f"Receive loop for {node.name} started with no socket!")
            return

        while self.running and not node.stop_event.is_set():
            try:
                # Use my_sock, not node.sock
                msg = fgr.receive_message(my_sock, timeout=0.5)
                if msg is None:
                    continue

                node.message_count += 1

                if self.logger.isEnabledFor(logging.DEBUG):
                    msg_type_name = msg_type_names.get(msg.message_type, f"UNK({msg.message_type})")
                    self.logger.debug(
                        f"[{node.name}] RCVD #{node.message_count}: "
                        f"type={msg_type_name}, subtype=0x{msg.subtype:03X}, "
                        f"ref={msg.reference}, err/state={msg.error_or_state}, "
                        f"len={len(msg.contents)}"
                    )

                node.last_heartbeat = time.time()
                self._dispatch_message(node, msg)

            except socket.timeout:
                continue
            except socket.error as e:
                if not node.stop_event.is_set():
                    # Don't log "Connection reset by peer" as an error during normal operation
                    if hasattr(e, 'errno') and e.errno == 104:  # Connection reset by peer
                        self.logger.debug(f"Connection reset by peer from {node.name} (normal during reboot)")
                    else:
                        self.logger.error(f"Socket error from {node.name}: {e}")
                break
            except Exception as e:
                if not node.stop_event.is_set():
                    self.logger.error(f"Receive error from {node.name}: {e}")
                    import traceback
                    traceback.print_exc()
                break

        # Clean up - only if this thread still owns the connection
        # Check if node.sock is the same as our captured socket
        current_sock = node.sock
        if current_sock is None or (my_sock and current_sock.fileno() == my_sock.fileno()):
            self.logger.info(f"Receive loop ending for {node.name}, disconnecting...")
            self._disconnect_node(node)
        else:
            self.logger.info(f"Receive loop ending for {node.name} but socket was replaced (old_fd={my_sock.fileno() if my_sock else 'None'}, new_fd={current_sock.fileno() if current_sock else 'None'}), not disconnecting")

    def _dispatch_message(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Dispatch a message to the appropriate handler"""
        try:
            msg_type = msg.message_type

            self.logger.debug(f"[{node.name}] Dispatching message type {msg_type}")

            if msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_CNF:
                ref = msg.reference
                if ref in node.pending_requests:
                    q = node.pending_requests.pop(ref)
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass

                if node.handler:
                    node.handler.on_confirmation(msg)

            elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_IND:
                if node.handler:
                    # Let parent handle standard protocol (including observation)
                    is_standard = node.handler.on_indication(msg)

                    # If not standard protocol, it's node-specific - child MUST handle it
                    if not is_standard:
                        if not node.handler.on_node_specific_indication(msg):
                            # No handler for this node-specific indication
                            self.logger.warning(
                                f"[{node.name}] Unhandled node-specific indication: "
                                f"type=0x{msg.subtype:03X}, len={len(msg.contents)}"
                            )
                else:
                    # No handler, use fallback
                    self._handle_generic_indication(node, msg)

            elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_RSP:
                if node.handler:
                    node.handler.on_response(msg)

            elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_REQ:
                self.logger.warning(f"Unexpected REQ from {node.name}")

            elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_LOG:
                # LOG messages (type 5) are ignored - they go to a different endpoint
                pass
            else:
                # Unknown message type
                self.logger.warning(f"[{node.name}] Unknown message type: {msg_type}")

        except Exception as e:
            self.logger.error(f"Exception in dispatch for {node.name}: {e}")
            import traceback
            traceback.print_exc()
            # Don't close the connection on exception

    def _handle_generic_indication(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Handle indications not handled by node-specific code"""
        ind_type = msg.subtype

        # Debug: Show raw values for all indications
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(
                f"[{node.name}] IND: type=0x{ind_type:03X} ({ind_type}), "
                f"state={msg.error_or_state}, ref={msg.reference}"
            )

        if ind_type == fgr.FGRIndRsp.FGR_IND_RSP_NEEDS_CFG:
            self.logger.info(f"Node {node.name}: needs configuration")
            # Send response but DON'T change state or close connection
            self.send_response_to_node(node.name, ind_type, msg.reference, b"")
            # Update node state
            node.fgr_state = msg.error_or_state
            node.state = ConnectionState.from_fgr_state(msg.error_or_state)

        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_START:
            self.logger.info(f"Node {node.name}: started")
            node.fgr_state = msg.error_or_state
            node.state = ConnectionState.from_fgr_state(msg.error_or_state)

        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_STOP:
            self.logger.info(f"Node {node.name}: stopped")
            node.fgr_state = msg.error_or_state
            node.state = ConnectionState.from_fgr_state(msg.error_or_state)

        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_HEARTBEAT:
            node.heartbeat_count += 1
            node.last_heartbeat = time.time()
            # Extract RSSI from message contents
            if len(msg.contents) >= 1:
                rssi = msg.contents[0]
                if rssi > 127:
                    rssi = rssi - 256
                node.rssi = rssi
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"[{node.name}] HEARTBEAT #{node.heartbeat_count}, RSSI={node.rssi} dBm")

        elif ind_type > fgr.FGRIndRsp.FGR_IND_RSP_LAST:
            if self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Node {node.name}: device indication 0x{ind_type:03X}")

    def _heartbeat_loop(self) -> None:
        """Monitor node heartbeats"""
        while self.running:
            time.sleep(15)
            now = time.time()
            for node in self.nodes.values():
                if node.sock and node.state != ConnectionState.DISCONNECTED:
                    time_since = now - node.last_heartbeat

                    if time_since > node.heartbeat_timeout:
                        self.logger.warning(
                            f"Node {node.name} heartbeat timeout "
                            f"(last: {time_since:.1f}s ago, timeout: {node.heartbeat_timeout}s, "
                            f"msgs={node.message_count}, hb={node.heartbeat_count})"
                        )
                        self._disconnect_node(node)
                    elif time_since > node.heartbeat_timeout - 15 and self.logger.isEnabledFor(logging.DEBUG):
                        self.logger.debug(
                            f"Node {node.name} heartbeat due soon "
                            f"(last: {time_since:.1f}s ago, timeout: {node.heartbeat_timeout}s)"
                        )

    def _get_next_reference(self, node: Node) -> int:
        node.reference_counter = (node.reference_counter + 1) & 0xFF
        return node.reference_counter

    def send_request_to_node(self, node_name: str, req_type: int,
                             contents: bytes = b"",
                             timeout: float = 5.0) -> Optional[fgr.FGRMsg]:
        """Send a request to a node and wait for confirmation"""
        node = self.nodes.get(node_name)
        if not node or not node.sock:
            self.logger.error(f"Node {node_name} not connected")
            return None

        reference = self._get_next_reference(node)
        msg = fgr.FGRMsg.create_req(req_type, reference, contents)

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"[{node_name}] Sending REQ type=0x{req_type:03X}, ref={reference}, len={len(contents)}")

        response_queue = queue.Queue(maxsize=1)
        node.pending_requests[reference] = response_queue

        try:
            if not fgr.send_message(node.sock, msg):
                node.pending_requests.pop(reference, None)
                return None

            try:
                return response_queue.get(timeout=timeout)
            except queue.Empty:
                self.logger.warning(f"Timeout waiting for confirmation from {node_name}")
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

        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"[{node_name}] Sending RSP type=0x{rsp_type:03X}, ref={reference}, len={len(contents)}")

        return fgr.send_message(node.sock, msg)

    def cfg_node(self, node_name: str, cfg_data: bytes, timeout: float = 5.0) -> bool:
        """Configure a node"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_CFG, cfg_data, timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = ConnectionState.READY
            return True
        return False

    def start_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Start a node"""
        self.logger.info(f"Sending start request to {node_name}")
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_START, b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                # Update state immediately - node confirmed it started
                node.fgr_state = fgr.FGRState.FGR_STATE_STARTED
                node.state = ConnectionState.from_fgr_state(fgr.FGRState.FGR_STATE_STARTED)
                self.logger.info(f"Node {node_name} started (state updated)")
            return True
        else:
            self.logger.warning(f"Node {node_name} start failed or no response")
            return False

    def stop_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Stop a node"""
        self.logger.info(f"Sending stop request to {node_name}")
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_STOP, b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                # Update state immediately - node confirmed it stopped
                node.fgr_state = fgr.FGRState.FGR_STATE_STOPPED
                node.state = ConnectionState.from_fgr_state(fgr.FGRState.FGR_STATE_STOPPED)
                self.logger.info(f"Node {node_name} stopped (state updated)")
            return True
        else:
            self.logger.warning(f"Node {node_name} stop failed or no response")
            return False

    def reboot_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Reboot a node"""
        self.logger.info(f"Sending reboot request to {node_name}")
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_REBOOT, b"", timeout)
        success = cnf is not None and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE
        if success:
            # Node will disconnect and reconnect, mark as disconnected temporarily
            node = self.nodes.get(node_name)
            if node:
                node.fgr_state = fgr.FGRState.FGR_STATE_NOT_POPULATED
                node.state = ConnectionState.DISCONNECTED
                self.logger.info(f"Node {node_name} reboot confirmed, waiting for reconnection")
        else:
            self.logger.warning(f"Node {node_name} reboot failed or no response")
        return success

    def ping_node(self, node_name: str, timeout: float = 3.0) -> Optional[int]:
        """Send a PING request to a node and return its state"""
        self.logger.info(f"Sending PING to {node_name}")
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_PING, b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            if len(cnf.contents) >= 1:
                state_value = cnf.contents[0]
                # Update the node's state
                node = self.nodes.get(node_name)
                if node:
                    node.fgr_state = state_value
                    node.state = ConnectionState.from_fgr_state(state_value)
                self.logger.info(f"Node {node_name} state: {state_value}")
                return state_value

    def query_node_state(self, node_name: str, timeout: float = 3.0) -> Optional[fgr.FGRState]:
        """Query a node's current state using PING"""
        state_value = self.ping_node(node_name, timeout)
        if state_value is not None:
            try:
                return fgr.FGRState(state_value)
            except ValueError:
                self.logger.warning(f"Unknown state value {state_value} from {node_name}")
        return None

    def get_node(self, name: str) -> Optional[Node]:
        return self.nodes.get(name)

    def get_connected_nodes(self) -> List[Node]:
        return [n for n in self.nodes.values() if n.sock is not None]

    def get_node_names(self) -> List[str]:
        return list(self.nodes.keys())


# ============================================================================
# Command Line Argument Parsing
# ============================================================================

def parse_args():
    parser = argparse.ArgumentParser(description="FGR Railway Controller")

    parser.add_argument("--ip", type=str, default="10.10.3.1",
                        help="IP address to listen on (default: 10.10.3.1)")

    parser.add_argument("--port", type=int, default=5000,
                        help="Port to listen on (default: 5000)")

    parser.add_argument("--cfg", type=Path,
                        help="Path to node configuration file (JSON format)")

    parser.add_argument("--nodes-dir", type=Path,
                        help="Directory containing node handlers (default: ./nodes)")

    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level (default: INFO)")

    return parser.parse_args()


# ============================================================================
# Default Configuration
# ============================================================================

def get_default_cfg_path() -> Path:
    """Get default configuration file path"""
    default_cfg = SCRIPT_DIR / "nodes.json"
    if default_cfg.exists():
        return default_cfg
    return None


def get_default_node_cfg() -> Dict[str, Dict]:
    """Get default node configuration (used if no cfg file provided)"""
    return {
        "test_1": {
            "ip": "10.10.3.2",
            "type": "test",
            "heartbeat_timeout": 30
        },
        "level_gauge_1": {
            "ip": "10.10.3.3",
            "type": "level_gauge",
            "heartbeat_timeout": 60
        }
    }


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    args = parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    logger = logging.getLogger("Main")

    cfg_file = args.cfg
    if not cfg_file:
        cfg_file = get_default_cfg_path()

    controller = Controller(
        listen_ip=args.ip,
        port=args.port,
        nodes_dir=args.nodes_dir,
        cfg_file=cfg_file
    )

    # If no config file and no nodes loaded, add defaults
    if not cfg_file and not controller.get_node_names():
        logger.info("No configuration file found, using defaults")
        for name, node_cfg in get_default_node_cfg().items():
            controller.add_node(
                name=name,
                ip=node_cfg["ip"],
                node_type=node_cfg.get("type", ""),
                heartbeat_timeout=node_cfg.get("heartbeat_timeout", 60)
            )

    # Print startup information
    print("\n" + "=" * 60)
    print("FGR Railway Controller")
    print("=" * 60)
    print(f"Listening on:    {args.ip}:{args.port}")
    print(f"Log level:       {args.log_level}")
    print(f"Nodes directory: {controller.nodes_dir}")
    if cfg_file and cfg_file.exists():
        print(f"Config file:     {cfg_file}")
    print("\nConfigured nodes:")
    for name, node in controller.nodes.items():
        print(f"  - {name:20} {node.ip:15} (type: {node.node_type or 'none'}, timeout: {node.heartbeat_timeout}s)")
    print("=" * 60)
    print("Press Ctrl+C to stop\n")

    if controller.start():
        try:
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