#!/usr/bin/env python3
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

class NodeState(IntEnum):
    DISCONNECTED = 0
    CONNECTED = 1
    NEEDS_CFG = fgr.FGRState.FGR_STATE_NEEDS_CFG
    STARTED = fgr.FGRState.FGR_STATE_STARTED
    STOPPED = fgr.FGRState.FGR_STATE_STOPPED
    BUSY = fgr.FGRState.FGR_STATE_BUSY
    GENERIC_FAILED = fgr.FGRState.FGR_STATE_GENERIC_FAILED
    HARDWARE_FAILURE = fgr.FGRState.FGR_STATE_HARDWARE_FAILURE
    CONFIGURING = 100
    READY = 101
    ERROR = 102


@dataclass
class Node:
    """Represents a connected node"""
    ip: str
    name: str
    node_type: str = ""
    sock: Optional[socket.socket] = None
    state: NodeState = NodeState.DISCONNECTED
    fgr_state: int = fgr.FGRState.FGR_STATE_NOT_POPULATED
    reference_counter: int = 0
    pending_requests: Dict[int, queue.Queue] = field(default_factory=dict)
    last_heartbeat: float = 0
    cfg_data: Optional[Dict[str, Any]] = None
    handler: Optional['NodeHandler'] = None
    rx_thread: Optional[threading.Thread] = None
    custom_data: Dict[str, Any] = field(default_factory=dict)
    stop_event: threading.Event = field(default_factory=threading.Event)
    heartbeat_timeout: int = 60
    # Debug counters
    message_count: int = 0
    heartbeat_count: int = 0
    last_debug_log: float = 0


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
        """Send a request to this node and wait for confirmation"""
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
        
        parent_dir = self.nodes_dir.parent
        if str(parent_dir) not in sys.path:
            sys.path.insert(0, str(parent_dir))
        
        for py_file in sorted(self.nodes_dir.glob("node_*.py")):
            if py_file.name == "node_base.py":
                continue
            
            module_name = f"nodes.{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, NodeHandler) and obj != NodeHandler:
                        node_type = py_file.stem[5:]
                        self.node_handlers[node_type] = obj
                        self.logger.info(f"Loaded handler {obj.__name__} for node type '{node_type}'")
            except Exception as e:
                self.logger.error(f"Failed to load handler from {py_file.name}: {e}")
    
    def _load_nodes_from_cfg(self):
        """Load node definitions from configuration"""
        nodes_cfg = self.config_mgr.get_all_nodes()
        
        for name, node_cfg in nodes_cfg.items():
            ip = node_cfg.get("ip")
            if not ip:
                self.logger.error(f"Node {name} has no IP address, skipping")
                continue
            
            node_type = node_cfg.get("type", "")
            merged_cfg = self.config_mgr.get_node_config(name, node_type)
            heartbeat_timeout = merged_cfg.get("heartbeat_timeout", 60)
            
            self.add_node(name, ip, node_type, heartbeat_timeout)
    
    def _get_handler_for_node(self, node: Node) -> NodeHandler:
        """Create appropriate handler instance for a node"""
        if node.node_type and node.node_type in self.node_handlers:
            return self.node_handlers[node.node_type](node, self)
        
        for prefix, handler_class in self.node_handlers.items():
            if node.name.startswith(prefix):
                return handler_class(node, self)
        
        return NodeHandler(node, self)
    
    def add_node(self, name: str, ip: str, node_type: str = "", heartbeat_timeout: int = 60) -> None:
        """Add a node definition"""
        if name in self.nodes:
            self.logger.warning(f"Node {name} already exists")
            return
        
        node = Node(ip=ip, name=name, node_type=node_type, heartbeat_timeout=heartbeat_timeout)
        self.nodes[name] = node
        self.nodes_by_ip[ip] = node
        self.logger.info(f"Added node: {name} ({ip}) type='{node_type}', timeout={heartbeat_timeout}s")
    
    def _disconnect_node(self, node: Node) -> None:
        """Internal: disconnect a node"""
        if node.state == NodeState.DISCONNECTED:
            return
        
        self.logger.debug(f"[{node.name}] Disconnecting node (msgs_rcvd={node.message_count}, heartbeats={node.heartbeat_count})")
        node.stop_event.set()
        node.state = NodeState.DISCONNECTED
        
        if node.sock:
            try:
                node.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                node.sock.close()
            except Exception:
                pass
            node.sock = None
        
        for ref, q in node.pending_requests.items():
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        node.pending_requests.clear()
        
        if node.handler:
            node.handler.on_disconnected()
    
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
                    
                    if node.sock:
                        self._disconnect_node(node)
                    
                    node.stop_event.clear()
                    node.sock = client_sock
                    node.state = NodeState.CONNECTED
                    node.last_heartbeat = time.time()
                    node.reference_counter = 0
                    node.pending_requests.clear()
                    node.message_count = 0
                    node.heartbeat_count = 0
                    
                    node.handler = self._get_handler_for_node(node)
                    
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
        msg_type_names = {1: "REQ", 2: "CNF", 3: "IND", 4: "RSP", 5: "LOG"}
        
        while self.running and node.sock and not node.stop_event.is_set():
            try:
                msg = fgr.receive_message(node.sock, timeout=0.5)
                if msg is None:
                    continue
                
                node.message_count += 1
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
                    self.logger.error(f"Socket error from {node.name}: {e}")
                break
            except Exception as e:
                if not node.stop_event.is_set():
                    self.logger.error(f"Receive error from {node.name}: {e}")
                break
        
        if node.state != NodeState.DISCONNECTED:
            self._disconnect_node(node)
            self.logger.info(f"Node {node.name} disconnected")
    
    def _dispatch_message(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Dispatch a message to the appropriate handler"""
        msg_type = msg.message_type
        
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
            handled = False
            if node.handler:
                handled = node.handler.on_indication(msg)
            
            if not handled:
                self._handle_generic_indication(node, msg)
        
        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_RSP:
            if node.handler:
                node.handler.on_response(msg)
        
        elif msg_type == fgr.FGRMsgType.FGR_MSG_TYPE_REQ:
            self.logger.warning(f"Unexpected REQ from {node.name}")
        
        # LOG messages (type 5) are ignored - they go to a different endpoint
    
    def _handle_generic_indication(self, node: Node, msg: fgr.FGRMsg) -> None:
        """Handle indications not handled by node-specific code"""
        ind_type = msg.subtype
        
        # Debug: Show raw values for all indications
        self.logger.debug(
            f"[{node.name}] IND: type=0x{ind_type:03X} ({ind_type}), "
            f"state={msg.error_or_state}, ref={msg.reference}"
        )
        
        # Check for heartbeat - support both integer and enum
        heartbeat_value = None
        if hasattr(fgr.FGRIndRsp, 'FGR_IND_RSP_HEARTBEAT'):
            heartbeat_value = fgr.FGRIndRsp.FGR_IND_RSP_HEARTBEAT
            self.logger.debug(f"[{node.name}] Heartbeat expected value = {heartbeat_value}")
        
        if ind_type == fgr.FGRIndRsp.FGR_IND_RSP_NEEDS_CFG:
            self.logger.info(f"Node {node.name}: needs configuration")
            self.send_response_to_node(node.name, ind_type, msg.reference, b"")
        
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_START:
            self.logger.info(f"Node {node.name}: started")
        
        elif ind_type == fgr.FGRIndRsp.FGR_IND_RSP_STOP:
            self.logger.info(f"Node {node.name}: stopped")
        
        elif heartbeat_value is not None and ind_type == heartbeat_value:
            node.heartbeat_count += 1
            self.logger.info(f"♥ [{node.name}] HEARTBEAT #{node.heartbeat_count} (type=0x{ind_type:03X}, state={msg.error_or_state})")
        
        elif ind_type > fgr.FGRIndRsp.FGR_IND_RSP_LAST:
            self.logger.debug(f"Node {node.name}: device indication 0x{ind_type:03X}")
    
    def _heartbeat_loop(self) -> None:
        """Monitor node heartbeats"""
        while self.running:
            time.sleep(15)
            now = time.time()
            for node in self.nodes.values():
                if node.sock and node.state != NodeState.DISCONNECTED:
                    time_since = now - node.last_heartbeat
                    
                    if time_since > node.heartbeat_timeout:
                        self.logger.warning(
                            f"Node {node.name} heartbeat timeout "
                            f"(last: {time_since:.1f}s ago, timeout: {node.heartbeat_timeout}s, "
                            f"msgs={node.message_count}, hb={node.heartbeat_count})"
                        )
                        self._disconnect_node(node)
                    elif time_since > node.heartbeat_timeout - 15:
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
        self.logger.debug(f"[{node_name}] Sending RSP type=0x{rsp_type:03X}, ref={reference}, len={len(contents)}")
        return fgr.send_message(node.sock, msg)
    
    def cfg_node(self, node_name: str, cfg_data: bytes, timeout: float = 5.0) -> bool:
        """Configure a node"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_CFG, cfg_data, timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = NodeState.READY
            return True
        return False
    
    def start_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Start a node"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_START, b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = NodeState.STARTED
            return True
        return False
    
    def stop_node(self, node_name: str, timeout: float = 5.0) -> bool:
        """Stop a node"""
        cnf = self.send_request_to_node(node_name, fgr.FGRReqCnf.FGR_REQ_CNF_STOP, b"", timeout)
        if cnf and cnf.error_or_state == fgr.FGRError.FGR_ERROR_NONE:
            node = self.nodes.get(node_name)
            if node:
                node.state = NodeState.STOPPED
            return True
        return False
    
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
    
    # Show heartbeat constant info
    if hasattr(fgr.FGRIndRsp, 'FGR_IND_RSP_HEARTBEAT'):
        print(f"Heartbeat constant: FGR_IND_RSP_HEARTBEAT = {fgr.FGRIndRsp.FGR_IND_RSP_HEARTBEAT}")
    else:
        print("WARNING: FGR_IND_RSP_HEARTBEAT not defined in protocol!")
        print("Looking for any value 0x0004 in FGRIndRsp...")
        for name, value in fgr.FGRIndRsp.__members__.items():
            if value == 4:
                print(f"  Found {name} = {value} - using this for heartbeat")
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