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
Protocol Generator for FGR ESP32 Control Interface

This script parses the C protocol header file and generates a Python module
with all the protocol definitions, message classes, and helper functions.

All written by DeepSeek :-).

Usage:
    python3 generate_fgr_protocol.py <fgr_protocol.h> [output.py]
"""

import re
import sys
import struct
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field


@dataclass
class ProtocolDef:
    """Container for parsed protocol definitions"""
    version: Optional[int] = None
    msg_max_len: Optional[int] = None
    msg_contents_max_len: Optional[int] = None
    log_string_max_len: Optional[int] = None
    msg_types: Dict[str, int] = field(default_factory=dict)
    req_cnf_types: Dict[str, int] = field(default_factory=dict)
    ind_rsp_types: Dict[str, int] = field(default_factory=dict)
    log_levels: Dict[str, int] = field(default_factory=dict)
    error_codes: Dict[str, int] = field(default_factory=dict)
    states: Dict[str, int] = field(default_factory=dict)
    structs: Dict[str, Dict[str, Tuple[str, int, str]]] = field(default_factory=dict)


class CHeaderParser:
    """Parser for C protocol header files"""
    
    def __init__(self, header_path: str):
        self.header_path = Path(header_path)
        self.content = self.header_path.read_text()
        self.protocol = ProtocolDef()
        
    def parse(self) -> ProtocolDef:
        """Parse the C header and extract all protocol definitions"""
        self._parse_macros()
        self._parse_enums()
        self._parse_structs()
        return self.protocol
    
    def _parse_macros(self):
        """Extract #define macros"""
        # Protocol version
        version_match = re.search(r'#define\s+FGR_PROTOCOL_VERSION\s+(0x[0-9A-Fa-f]+|\d+)', self.content)
        if version_match:
            self.protocol.version = self._parse_int(version_match.group(1))
        
        # Message contents max length
        msg_contents_len_match = re.search(r'#define\s+FGR_MSG_CONTENTS_MAX_LEN\s+(\d+)', self.content)
        if msg_contents_len_match:
            self.protocol.msg_contents_max_len = int(msg_contents_len_match.group(1))
        
        # Log string max length
        log_string_len_match = re.search(r'#define\s+FGR_LOG_STRING_MAX_LEN\s+(\d+)', self.content)
        if log_string_len_match:
            self.protocol.log_string_max_len = int(log_string_len_match.group(1))
    
    def _parse_int(self, value_str: str) -> int:
        """Parse integer value that might be hex, decimal, or binary"""
        value_str = value_str.strip()
        
        if not value_str:
            return 0
        
        if value_str.startswith('0x'):
            try:
                return int(value_str, 16)
            except ValueError:
                return 0
        
        if value_str.startswith('0b'):
            try:
                return int(value_str, 2)
            except ValueError:
                return 0
        
        try:
            return int(value_str)
        except ValueError:
            return 0
    
    def _parse_enum_body(self, body: str, enum_name: str) -> Dict[str, int]:
        """Parse an enum body and return name-value pairs"""
        values = {}
        
        # Remove comments
        body = re.sub(r'//.*$', '', body, flags=re.MULTILINE)
        
        # Collect all entries
        entries = []
        for line in body.split('\n'):
            line = line.strip()
            if line:
                for entry in line.split(','):
                    entry = entry.strip()
                    if entry and not entry.startswith('//'):
                        entries.append(entry)
        
        # Parse entries
        last_value = None
        for entry in entries:
            if '=' in entry:
                name, expr = entry.split('=', 1)
                name = name.strip()
                expr = expr.strip()
                
                # Handle expressions
                if expr.startswith('0x') or expr.isdigit():
                    value = self._parse_int(expr)
                elif '+' in expr:
                    parts = expr.split('+')
                    base_name = parts[0].strip()
                    offset = self._parse_int(parts[1].strip())
                    if base_name in values:
                        value = values[base_name] + offset
                    else:
                        value = offset
                else:
                    # Reference to another enum value
                    if expr in values:
                        value = values[expr]
                    else:
                        value = last_value + 1 if last_value is not None else 0
                
                values[name] = value
                last_value = value
            else:
                name = entry
                if last_value is not None:
                    last_value += 1
                else:
                    last_value = 0
                values[name] = last_value
        
        return values
    
    def _parse_enums(self):
        """Extract all enum definitions"""
        # Find all enum blocks - both typedef enum and regular enum
        enum_patterns = [
            r'typedef\s+enum\s*{([^}]+)}\s*(\w+)_t;',
            r'enum\s+(\w+)\s*{([^}]+)};'
        ]
        
        for pattern in enum_patterns:
            for match in re.finditer(pattern, self.content, re.DOTALL):
                if len(match.groups()) == 2:
                    if 'typedef' in pattern:
                        enum_body = match.group(1)
                        enum_name = match.group(2)
                    else:
                        enum_name = match.group(1)
                        enum_body = match.group(2)
                else:
                    continue
                
                values = self._parse_enum_body(enum_body, enum_name)
                
                # Route to appropriate container
                if enum_name == 'fgr_msg_type':
                    self.protocol.msg_types = values
                elif enum_name == 'fgr_req_cnf':
                    self.protocol.req_cnf_types = values
                elif enum_name == 'fgr_ind_rsp':
                    self.protocol.ind_rsp_types = values
                elif enum_name == 'fgr_log_level':
                    self.protocol.log_levels = values
                elif enum_name == 'fgr_error':
                    self.protocol.error_codes = values
                elif enum_name == 'fgr_state':
                    self.protocol.states = values
    
    def _parse_structs(self):
        """Extract struct definitions"""
        struct_pattern = r'typedef\s+struct\s*(?:__attribute__\(\(packed\)\))?\s*{([^}]+)}\s*(\w+)_t;'
        
        for match in re.finditer(struct_pattern, self.content, re.DOTALL):
            struct_body = match.group(1)
            struct_name = match.group(2)
            
            fields = self._parse_struct_body(struct_body)
            if fields:
                self.protocol.structs[struct_name] = fields
    
    def _parse_struct_body(self, body: str) -> Dict[str, Tuple[str, int, str]]:
        """Parse struct body and return field name -> (type, size, description) mappings"""
        fields = {}
        
        # Remove comments
        body = re.sub(r'//.*$', '', body, flags=re.MULTILINE)
        
        # Type mapping for size calculation
        type_sizes = {
            'uint8_t': 1,
            'uint16_t': 2,
            'uint32_t': 4,
            'int8_t': 1,
            'int16_t': 2,
            'int32_t': 4,
            'char': 1
        }
        
        # Split into individual field declarations
        field_lines = []
        current_field = []
        
        for line in body.split('\n'):
            line = line.strip()
            if not line:
                continue
                
            if ';' in line:
                parts = line.split(';')
                for part in parts[:-1]:
                    if current_field:
                        current_field.append(part)
                        field_lines.append(' '.join(current_field))
                        current_field = []
                    else:
                        field_lines.append(part)
                last_part = parts[-1].strip()
                if last_part:
                    current_field.append(last_part)
            else:
                current_field.append(line)
        
        if current_field:
            field_lines.append(' '.join(current_field))
        
        # Parse each field line
        for field_line in field_lines:
            field_line = field_line.strip()
            if not field_line:
                continue
                
            # Parse field declaration
            parts = field_line.split()
            if len(parts) >= 2:
                type_name = parts[0]
                field_name_part = parts[1].rstrip(';')
                
                # Handle arrays
                array_match = re.search(r'(\w+)\[(\d+)\]', field_name_part)
                if array_match:
                    field_name = array_match.group(1)
                    array_size = int(array_match.group(2))
                    element_size = type_sizes.get(type_name, 1)
                    size = element_size * array_size
                    type_info = f"{type_name}[{array_size}]"
                else:
                    field_name = field_name_part
                    size = type_sizes.get(type_name, 1)
                    type_info = type_name
                
                fields[field_name] = (type_info, size, field_line)
        
        return fields


class PythonGenerator:
    """Generate Python module from parsed protocol definitions"""
    
    def __init__(self, protocol: ProtocolDef):
        self.p = protocol
        self.output = []
    
    def generate(self) -> str:
        """Generate the complete Python module"""
        self._add_header()
        self._add_imports()
        self._add_constants()
        self._add_enums()
        self._add_message_classes()
        self._add_helper_functions()
        
        return '\n'.join(self.output)
    
    def _add_header(self):
        """Add module header and docstring"""
        self.output.extend([
            '#!/usr/bin/env python3',
            '"""',
            'Auto-generated protocol definitions for FGR ESP32 control interface.',
            '',
            'This module is generated from the C protocol header file.',
            'Do not edit this file directly - edit the .h file and regenerate.',
            '"""',
            ''
        ])
    
    def _add_imports(self):
        """Add required imports"""
        self.output.extend([
            'import struct',
            'from enum import IntEnum',
            'from typing import Optional, Union, Tuple, Any, Dict',
            'import socket',
            ''
        ])
    
    def _add_constants(self):
        """Add protocol constants"""
        if self.p.version is not None:
            self.output.append(f'FGR_PROTOCOL_VERSION = {self.p.version}')
        
        if self.p.msg_contents_max_len is not None:
            self.output.append(f'FGR_MSG_CONTENTS_MAX_LEN = {self.p.msg_contents_max_len}')
        
        if self.p.log_string_max_len is not None:
            self.output.append(f'FGR_LOG_STRING_MAX_LEN = {self.p.log_string_max_len}')
        
        self.output.append('')
    
    def _add_enums(self):
        """Add enum classes"""
        enum_configs = [
            ('FGRMsgType', self.p.msg_types, 'Message types'),
            ('FGRReqCnf', self.p.req_cnf_types, 'Request/Confirmation message types'),
            ('FGRIndRsp', self.p.ind_rsp_types, 'Indication/Response message types'),
            ('FGRLogLevel', self.p.log_levels, 'Log levels'),
            ('FGRError', self.p.error_codes, 'Error codes'),
            ('FGRState', self.p.states, 'Device states')
        ]
        
        for enum_name, values, description in enum_configs:
            if values:
                self.output.extend([
                    f'class {enum_name}(IntEnum):',
                    f'    """{description}"""'
                ])
                
                for name, value in sorted(values.items(), key=lambda x: x[1]):
                    self.output.append(f'    {name} = {value}')
                
                self.output.append('')
    
    def _generate_header_union(self):
        """Generate the header union class"""
        self.output.extend([
            'class FGRMsgHeader:',
            '    """Message header union - represents all header types"""',
            '',
            '    def __init__(self):',
            '        self._header = 0',
            '',
            '    @property',
            '    def raw(self) -> int:',
            '        """Get raw header value"""',
            '        return self._header',
            '',
            '    @raw.setter',
            '    def raw(self, value: int):',
            '        """Set raw header value"""',
            '        self._header = value',
            '',
            '    # Request header accessors',
            '    @property',
            '    def req_type(self) -> int:',
            '        """Get request type (top 12 bits are type, top 4 bits are message type)"""',
            '        return self._header & 0x0FFF',
            '',
            '    @req_type.setter',
            '    def req_type(self, value: int):',
            '        msg_type = (self._header >> 12) & 0x0F',
            '        self._header = (msg_type << 12) | (value & 0x0FFF)',
            '',
            '    @property',
            '    def req_reference(self) -> int:',
            '        """Get request reference (bits 8-15 of the raw header)"""',
            '        return (self._header >> 8) & 0xFF',
            '',
            '    @req_reference.setter',
            '    def req_reference(self, value: int):',
            '        self._header = (self._header & 0xFFFF00FF) | ((value & 0xFF) << 8)',
            '',
            '    # Confirmation header accessors',
            '    @property',
            '    def cnf_type(self) -> int:',
            '        """Get confirmation type"""',
            '        return self._header & 0x0FFF',
            '',
            '    @cnf_type.setter',
            '    def cnf_type(self, value: int):',
            '        msg_type = (self._header >> 12) & 0x0F',
            '        self._header = (msg_type << 12) | (value & 0x0FFF)',
            '',
            '    @property',
            '    def cnf_reference(self) -> int:',
            '        """Get confirmation reference"""',
            '        return (self._header >> 8) & 0xFF',
            '',
            '    @cnf_reference.setter',
            '    def cnf_reference(self, value: int):',
            '        self._header = (self._header & 0xFFFF00FF) | ((value & 0xFF) << 8)',
            '',
            '    @property',
            '    def cnf_error(self) -> int:',
            '        """Get confirmation error"""',
            '        return (self._header >> 16) & 0xFF',
            '',
            '    @cnf_error.setter',
            '    def cnf_error(self, value: int):',
            '        self._header = (self._header & 0xFF00FFFF) | ((value & 0xFF) << 16)',
            '',
            '    # Indication header accessors',
            '    @property',
            '    def ind_type(self) -> int:',
            '        """Get indication type"""',
            '        return self._header & 0x0FFF',
            '',
            '    @ind_type.setter',
            '    def ind_type(self, value: int):',
            '        msg_type = (self._header >> 12) & 0x0F',
            '        self._header = (msg_type << 12) | (value & 0x0FFF)',
            '',
            '    @property',
            '    def ind_reference(self) -> int:',
            '        """Get indication reference"""',
            '        return (self._header >> 8) & 0xFF',
            '',
            '    @ind_reference.setter',
            '    def ind_reference(self, value: int):',
            '        self._header = (self._header & 0xFFFF00FF) | ((value & 0xFF) << 8)',
            '',
            '    @property',
            '    def ind_state(self) -> int:',
            '        """Get indication state"""',
            '        return (self._header >> 16) & 0xFF',
            '',
            '    @ind_state.setter',
            '    def ind_state(self, value: int):',
            '        self._header = (self._header & 0xFF00FFFF) | ((value & 0xFF) << 16)',
            '',
            '    # Response header accessors',
            '    @property',
            '    def rsp_type(self) -> int:',
            '        """Get response type"""',
            '        return self._header & 0x0FFF',
            '',
            '    @rsp_type.setter',
            '    def rsp_type(self, value: int):',
            '        msg_type = (self._header >> 12) & 0x0F',
            '        self._header = (msg_type << 12) | (value & 0x0FFF)',
            '',
            '    @property',
            '    def rsp_reference(self) -> int:',
            '        """Get response reference"""',
            '        return (self._header >> 8) & 0xFF',
            '',
            '    @rsp_reference.setter',
            '    def rsp_reference(self, value: int):',
            '        self._header = (self._header & 0xFFFF00FF) | ((value & 0xFF) << 8)',
            '',
            '    # Log header accessors',
            '    @property',
            '    def log_level(self) -> int:',
            '        """Get log level"""',
            '        return (self._header >> 8) & 0xFF',
            '',
            '    @log_level.setter',
            '    def log_level(self, value: int):',
            '        self._header = (self._header & 0xFFFF00FF) | ((value & 0xFF) << 8)',
            '',
            '    def pack(self) -> bytes:',
            '        """Pack header into bytes"""',
            '        return struct.pack("<I", self._header)',
            '',
            '    @classmethod',
            '    def unpack(cls, data: bytes) -> "FGRMsgHeader":',
            '        """Unpack bytes into header"""',
            '        header = cls()',
            '        header.raw = struct.unpack("<I", data)[0]',
            '        return header',
            '',
            '    def get_message_type(self) -> int:',
            '        """Get the message type from the top 4 bits"""',
            '        return (self._header >> 12) & 0x0F',
            '',
            '    def __repr__(self):',
            '        return f"<FGRMsgHeader raw=0x{self._header:08X}>"',
            ''
        ])
    
    def _generate_message_class(self):
        """Generate the main message class"""
        contents_max_len = self.p.msg_contents_max_len or 256
        
        self.output.extend([
            'class FGRMsg:',
            '    """Main FGR message class with variable-length body"""',
            f'    CONTENTS_MAX_LEN = {contents_max_len}',
            '',
            '    def __init__(self, msg_type: int = 0, msg_subtype: int = 0,',
            '                 reference: int = 0, error_or_state: int = 0,',
            '                 contents: bytes = b""):',
            '        """Create a new FGR message"""',
            '        self.header = FGRMsgHeader()',
            '        self.contents = contents',
            '        self._set_header_fields(msg_type, msg_subtype, reference, error_or_state)',
            '',
            '    def _set_header_fields(self, msg_type: int, msg_subtype: int,',
            '                           reference: int, error_or_state: int):',
            '        """Set header fields based on message type"""',
            '        raw_header = (msg_type << 12) | (msg_subtype & 0x0FFF)',
            '        raw_header |= (reference & 0xFF) << 8',
            '        if msg_type in [2, 3]:  # CNF or IND',
            '            raw_header |= (error_or_state & 0xFF) << 16',
            '        self.header.raw = raw_header',
            '',
            '    @classmethod',
            '    def create_req(cls, req_type: int, reference: int = 0,',
            '                   contents: bytes = b"") -> "FGRMsg":',
            '        """Create a request message"""',
            '        return cls(FGRMsgType.FGR_MSG_TYPE_REQ, req_type, reference, 0, contents)',
            '',
            '    @classmethod',
            '    def create_cnf(cls, cnf_type: int, reference: int = 0,',
            '                   error: int = 0, contents: bytes = b"") -> "FGRMsg":',
            '        """Create a confirmation message"""',
            '        return cls(FGRMsgType.FGR_MSG_TYPE_CNF, cnf_type, reference, error, contents)',
            '',
            '    @classmethod',
            '    def create_ind(cls, ind_type: int, reference: int = 0,',
            '                   state: int = 0, contents: bytes = b"") -> "FGRMsg":',
            '        """Create an indication message"""',
            '        return cls(FGRMsgType.FGR_MSG_TYPE_IND, ind_type, reference, state, contents)',
            '',
            '    @classmethod',
            '    def create_rsp(cls, rsp_type: int, reference: int = 0,',
            '                   contents: bytes = b"") -> "FGRMsg":',
            '        """Create a response message"""',
            '        return cls(FGRMsgType.FGR_MSG_TYPE_RSP, rsp_type, reference, 0, contents)',
            '',
            '    @classmethod',
            '    def create_log(cls, level: int, message: str = "") -> "FGRMsg":',
            '        """Create a log message"""',
            '        msg = cls(FGRMsgType.FGR_MSG_TYPE_LOG, 0, 0, 0, b"")',
            '        msg.header.log_level = level',
            '        # Encode message without null terminator',
            '        msg.contents = message.encode("utf-8")[:FGR_LOG_STRING_MAX_LEN]',
            '        return msg',
            '',
            '    @property',
            '    def message_type(self) -> int:',
            '        """Get the message type"""',
            '        return self.header.get_message_type()',
            '',
            '    @property',
            '    def subtype(self) -> int:',
            '        """Get the message subtype (request/indication type, etc.)"""',
            '        return self.header.raw & 0x0FFF',
            '',
            '    @property',
            '    def reference(self) -> int:',
            '        """Get the reference field"""',
            '        return (self.header.raw >> 8) & 0xFF',
            '',
            '    @property',
            '    def error_or_state(self) -> int:',
            '        """Get error (for CNF) or state (for IND) field"""',
            '        return (self.header.raw >> 16) & 0xFF',
            '',
            '    def pack(self) -> bytes:',
            '        """Pack message into bytes for transmission"""',
            '        # Pack header',
            '        header_bytes = self.header.pack()',
            '        # Pack body (length + contents)',
            '        content_length = len(self.contents)',
            '        if content_length > self.CONTENTS_MAX_LEN:',
            '            raise ValueError(f"Contents too long: {content_length} > {self.CONTENTS_MAX_LEN}")',
            '        body_bytes = struct.pack("<I", content_length) + self.contents',
            '        return header_bytes + body_bytes',
            '',
            '    @classmethod',
            '    def unpack(cls, data: bytes) -> "FGRMsg":',
            '        """Unpack bytes into a message instance"""',
            '        if len(data) < 4:  # At least header',
            '            raise ValueError(f"Message too short: {len(data)} bytes")',
            '        ',
            '        # Unpack header',
            '        header = FGRMsgHeader.unpack(data[:4])',
            '        ',
            '        # Unpack body',
            '        if len(data) < 8:',
            '            raise ValueError(f"Message too short for body length: {len(data)} bytes")',
            '        content_length = struct.unpack("<I", data[4:8])[0]',
            '        ',
            '        if content_length > 0:',
            '            if len(data) < 8 + content_length:',
            '                raise ValueError(f"Message truncated: expected {8 + content_length} bytes, got {len(data)}")',
            '            contents = data[8:8 + content_length]',
            '        else:',
            '            contents = b""',
            '        ',
            '        msg = cls()',
            '        msg.header = header',
            '        msg.contents = contents',
            '        return msg',
            '',
            '    def get_log_message(self) -> str:',
            '        """Extract log message from contents (for LOG messages)"""',
            '        if self.message_type != FGRMsgType.FGR_MSG_TYPE_LOG:',
            '            return ""',
            '        return self.contents.decode("utf-8", errors="replace")',
            '',
            '    def __repr__(self):',
            '        msg_type_names = {v: k for k, v in FGRMsgType.__members__.items()}',
            '        msg_type_name = msg_type_names.get(self.message_type, "UNKNOWN")',
            '        return f"<FGRMsg type={msg_type_name} subtype=0x{self.subtype:03X} ref={self.reference} contents_len={len(self.contents)}>"',
            ''
        ])
    
    def _add_message_classes(self):
        """Add message class definitions"""
        self._generate_header_union()
        self.output.append('')
        self._generate_message_class()
    
    def _add_helper_functions(self):
        """Add utility functions for working with the protocol"""
        self.output.extend([
            'def send_message(sock: socket.socket, msg: FGRMsg) -> bool:',
            '    """Send a protocol message over a socket"""',
            '    try:',
            '        data = msg.pack()',
            '        sock.sendall(data)',
            '        return True',
            '    except Exception as e:',
            '        print(f"Error sending message: {e}")',
            '        return False',
            '',
            'def receive_message(sock: socket.socket, timeout: Optional[float] = None) -> Optional[FGRMsg]:',
            '    """Receive and unpack a message (reads length from header)"""',
            '    original_timeout = sock.gettimeout()',
            '    try:',
            '        if timeout is not None:',
            '            sock.settimeout(timeout)',
            '        ',
            '        # Read header (4 bytes)',
            '        header_data = sock.recv(4)',
            '        if len(header_data) == 0:',
            '            return None  # Connection closed',
            '        if len(header_data) != 4:',
            '            raise ValueError(f"Incomplete header: got {len(header_data)} bytes")',
            '        ',
            '        # Read body length (4 bytes)',
            '        length_data = sock.recv(4)',
            '        if len(length_data) != 4:',
            '            raise ValueError(f"Incomplete length field: got {len(length_data)} bytes")',
            '        content_length = struct.unpack("<I", length_data)[0]',
            '        ',
            '        # Read contents',
            '        contents = b""',
            '        if content_length > 0:',
            '            contents = sock.recv(content_length)',
            '            if len(contents) != content_length:',
            '                raise ValueError(f"Incomplete contents: expected {content_length}, got {len(contents)}")',
            '        ',
            '        # Reconstruct full message',
            '        full_data = header_data + length_data + contents',
            '        return FGRMsg.unpack(full_data)',
            '    except socket.timeout:',
            '        return None',
            '    except Exception as e:',
            '        print(f"Error receiving message: {e}")',
            '        return None',
            '    finally:',
            '        sock.settimeout(original_timeout)',
            '',
            'def create_log_message(level: int, message: str) -> FGRMsg:',
            '    """Convenience function to create a log message"""',
            '    return FGRMsg.create_log(level, message)',
            '',
            'def create_config_message(device_type: int, config_data: bytes, reference: int = 0) -> FGRMsg:',
            '    """Create a configuration request message"""',
            '    return FGRMsg.create_req(FGRReqCnf.FGR_REQ_CNF_CFG, reference, config_data)',
            ''
        ])
    
    def print_usage_instructions(self):
        """Print usage instructions for the generated module"""
        contents_max_len = self.p.msg_contents_max_len or 256
        instructions = """
╔══════════════════════════════════════════════════════════════════════════════╗
║                    FGR Protocol Module Generated Successfully                ║
╚══════════════════════════════════════════════════════════════════════════════╝

The Python protocol module has been generated. Here's how to use it:

📦 IMPORTING
──────────────────────────────────────────────────────────────────────────────
    from fgr_protocol import (
        # Enums
        FGRMsgType, FGRReqCnf, FGRIndRsp, FGRLogLevel, FGRError, FGRState,
        # Message class
        FGRMsg,
        # Helper functions
        send_message, receive_message,
        create_log_message, create_config_message
    )

🎯 SENDING REQUESTS
──────────────────────────────────────────────────────────────────────────────
    # Create a configuration request
    config_msg = FGRMsg.create_req(
        req_type=FGRReqCnf.FGR_REQ_CNF_CFG,
        reference=1,
        contents=b"\\x01\\x02\\x03"  # Device-specific config data
    )
    
    # Create a start request
    start_msg = FGRMsg.create_req(
        req_type=FGRReqCnf.FGR_REQ_CNF_START,
        reference=2
    )
    
    # Send over socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('192.168.1.100', 5000))
    send_message(sock, start_msg)

📥 RECEIVING CONFIRMATIONS
──────────────────────────────────────────────────────────────────────────────
    # Wait for confirmation (5 second timeout)
    response = receive_message(sock, timeout=5.0)
    if response and response.message_type == FGRMsgType.FGR_MSG_TYPE_CNF:
        if response.error_or_state == FGRError.FGR_ERROR_NONE:
            print(f"Success! Reference: {response.reference}")
        else:
            print(f"Error: {FGRError(response.error_or_state).name}")

🔔 HANDLING INDICATIONS
──────────────────────────────────────────────────────────────────────────────
    # Receive an asynchronous indication
    ind = receive_message(sock)
    if ind and ind.message_type == FGRMsgType.FGR_MSG_TYPE_IND:
        print(f"Indication: {FGRIndRsp(ind.subtype).name}, State: {FGRState(ind.error_or_state).name}")
        if ind.contents:
            print(f"Data: {ind.contents.hex()}")

📝 SENDING LOGS
──────────────────────────────────────────────────────────────────────────────
    # Send a log message
    log_msg = FGRMsg.create_log(FGRLogLevel.FGR_LOG_LEVEL_INFO, "System initialized")
    send_message(sock, log_msg)

🔄 COMPLETE EXAMPLE
──────────────────────────────────────────────────────────────────────────────
    import socket
    from fgr_protocol import *
    
    # Connect to ESP32
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('192.168.1.100', 5000))
    
    # Create and send start request
    start_msg = FGRMsg.create_req(FGRReqCnf.FGR_REQ_CNF_START, 1)
    
    if send_message(sock, start_msg):
        response = receive_message(sock, timeout=5.0)
        if response and response.message_type == FGRMsgType.FGR_MSG_TYPE_CNF:
            if response.error_or_state == FGRError.FGR_ERROR_NONE:
                print("Device started successfully")
    
    sock.close()

📊 MESSAGE STRUCTURE
──────────────────────────────────────────────────────────────────────────────
    Header (4 bytes):
      - Bits 0-11: Message subtype (request/indication type, etc.)
      - Bits 12-15: Message type (REQ=1, CNF=2, IND=3, RSP=4, LOG=5)
      - Bits 16-23: Reference (for REQ/CNF/IND/RSP)
      - Bits 24-31: Error (for CNF) or State (for IND)
    
    Body:
      - Length (4 bytes): Length of contents field
      - Contents (variable): Message payload (max {contents_max_len} bytes)

⚠️ NOTES
──────────────────────────────────────────────────────────────────────────────
    • All multi-byte fields are little-endian (matches ESP32 and Raspberry Pi)
    • TCP provides reliable delivery - no checksums needed
    • Messages have variable length - use receive_message() which reads the length
    • Log messages are null-terminated strings (without the null in length field)
    • Device-specific messages can use the contents field for custom data

For more details, see the protocol definition in the original C header file.
"""
        # Replace the placeholder with the actual value
        instructions = instructions.replace('{contents_max_len}', str(contents_max_len))
        print(instructions)


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python3 generate_fgr_protocol.py <fgr_protocol.h> [output.py]")
        sys.exit(1)
    
    header_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else 'fgr_protocol.py'
    
    print(f"Parsing {header_path}...")
    parser = CHeaderParser(header_path)
    protocol = parser.parse()
    
    print(f"\nFound:")
    print(f"  - {len(protocol.msg_types)} message types")
    print(f"  - {len(protocol.req_cnf_types)} request/confirmation types")
    print(f"  - {len(protocol.ind_rsp_types)} indication/response types")
    print(f"  - {len(protocol.log_levels)} log levels")
    print(f"  - {len(protocol.error_codes)} error codes")
    print(f"  - {len(protocol.states)} states")
    print(f"  - {len(protocol.structs)} structs")
    
    if protocol.msg_contents_max_len:
        print(f"  - Message contents max length: {protocol.msg_contents_max_len}")
    if protocol.log_string_max_len:
        print(f"  - Log string max length: {protocol.log_string_max_len}")
    
    print(f"\nGenerating {output_path}...")
    generator = PythonGenerator(protocol)
    python_code = generator.generate()
    
    Path(output_path).write_text(python_code)
    
    # Print usage instructions
    generator.print_usage_instructions()
    
    print(f"\n✅ Successfully generated {output_path}")


if __name__ == "__main__":
    main()
