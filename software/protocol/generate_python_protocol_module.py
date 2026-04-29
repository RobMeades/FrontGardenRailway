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
Protocol Generator for ESP32 Control Interface

This script parses the C protocol header file and generates a Python module
with all the protocol definitions, message classes, and helper functions.

All written by DeepSeek :-).

Usage:
    python3 generate_protocol.py <fgr_protocol.h> [output.py]
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
    magic: Dict[str, int] = field(default_factory=dict)
    log_max_len: Optional[int] = None
    states: Dict[str, int] = field(default_factory=dict)
    commands: Dict[str, int] = field(default_factory=dict)
    queries: Dict[str, int] = field(default_factory=dict)
    indications: Dict[str, int] = field(default_factory=dict)
    log_levels: Dict[str, int] = field(default_factory=dict)
    status_codes: Dict[str, int] = field(default_factory=dict)
    structs: Dict[str, Dict[str, Tuple[str, int, str]]] = field(default_factory=dict)


class CHeaderParser:
    """Parser for C protocol header files"""
    
    def __init__(self, header_path: str):
        self.header_path = Path(header_path)
        self.content = self.header_path.read_text()
        self.protocol = ProtocolDef()
        self.enum_references = {}  # Track enum references for resolution
        
    def parse(self) -> ProtocolDef:
        """Parse the C header and extract all protocol definitions"""
        self._parse_macros()
        self._parse_enums()
        self._parse_structs()
        return self.protocol
    
    def _parse_macros(self):
        """Extract #define macros"""
        # Protocol version
        version_match = re.search(r'#define\s+PROTOCOL_VERSION\s+(0x[0-9A-Fa-f]+|\d+)', self.content)
        if version_match:
            self.protocol.version = self._parse_int(version_match.group(1))
        
        # Magic bytes
        magic_pattern = r'#define\s+PROTOCOL_MAGIC_(\w+)\s+(0x[0-9A-Fa-f]+|\d+)'
        for match in re.finditer(magic_pattern, self.content):
            name = match.group(1)
            value = self._parse_int(match.group(2))
            self.protocol.magic[name] = value
        
        # Log message max length
        log_len_match = re.search(r'#define\s+LOG_MESSAGE_MAX_LEN\s+(\d+)', self.content)
        if log_len_match:
            self.protocol.log_max_len = int(log_len_match.group(1))
    
    def _parse_int(self, value_str: str) -> int:
        """Parse integer value that might be hex, decimal, or binary"""
        value_str = value_str.strip()
        
        # Handle empty string
        if not value_str:
            return 0
        
        # Handle hexadecimal
        if value_str.startswith('0x'):
            try:
                return int(value_str, 16)
            except ValueError:
                return 0
        
        # Handle binary
        if value_str.startswith('0b'):
            try:
                return int(value_str, 2)
            except ValueError:
                return 0
        
        # Handle decimal
        try:
            return int(value_str)
        except ValueError:
            return 0
    
    def _parse_enum_body(self, body: str, enum_name: str) -> Dict[str, int]:
        """Parse an enum body and return name-value pairs"""
        values = {}
        
        # Remove comments
        body = re.sub(r'//.*$', '', body, flags=re.MULTILINE)
        
        # First, collect all entries with their expressions
        entries = []
        for line in body.split('\n'):
            line = line.strip()
            if line:
                # Split multiple entries on same line by comma
                for entry in line.split(','):
                    entry = entry.strip()
                    if entry and not entry.startswith('//'):
                        entries.append(entry)
        
        # First pass: collect all explicitly defined hex/decimal values
        for entry in entries:
            if '=' in entry:
                name, expr = entry.split('=', 1)
                name = name.strip()
                expr = expr.strip()
                
                # Handle direct hex/decimal assignments
                if expr.startswith('0x') or expr.isdigit():
                    values[name] = self._parse_int(expr)
        
        # Second pass: handle all reference-based assignments
        # Keep processing until no more changes (to handle dependencies)
        changed = True
        while changed:
            changed = False
            for entry in entries:
                if '=' in entry:
                    name, expr = entry.split('=', 1)
                    name = name.strip()
                    expr = expr.strip()
                    
                    # Skip if already assigned
                    if name in values:
                        continue
                    
                    # Handle expressions like CMD_SYSTEM_BEGIN + 1
                    if '+' in expr:
                        parts = expr.split('+')
                        base = parts[0].strip()
                        offset = int(parts[1].strip())
                        
                        if base in values:
                            values[name] = values[base] + offset
                            changed = True
                        elif base in [e.split('=')[0].strip() for e in entries if '=' in e]:
                            # Base exists but not resolved yet, will try next pass
                            pass
                    
                    # Handle simple references like CMD_STAND_INIT = CMD_STAND_BEGIN
                    elif expr in values:
                        values[name] = values[expr]
                        changed = True
                    elif expr in [e.split('=')[0].strip() for e in entries if '=' in e]:
                        # Reference exists but not resolved yet
                        pass
        
        # Third pass: handle implicitly valued entries (no '=')
        # Process entries in order to get correct sequence
        last_value = None
        for entry in entries:
            if '=' in entry:
                # This is an explicit entry - update last_value
                name = entry.split('=', 1)[0].strip()
                if name in values:
                    last_value = values[name]
            else:
                # This is an implicit entry (no '=')
                name = entry
                if last_value is not None:
                    last_value += 1
                    values[name] = last_value
                else:
                    # Start at 0 if no previous value
                    values[name] = 0
                    last_value = 0
        
        return values    
    def _parse_enums(self):
        """Extract all enum definitions"""
        # Find all enum blocks
        enum_pattern = r'typedef\s+enum\s*{([^}]+)}\s*(\w+)_t;'
        
        for match in re.finditer(enum_pattern, self.content, re.DOTALL):
            enum_name = match.group(2)
            enum_body = match.group(1)
            
            values = self._parse_enum_body(enum_body, enum_name)
            
            # Route to appropriate container
            if enum_name == 'state':
                self.protocol.states = values
            elif enum_name == 'cmd':
                self.protocol.commands = values
            elif enum_name == 'qry':
                self.protocol.queries = values
            elif enum_name == 'ind':
                self.protocol.indications = values
            elif enum_name == 'log_level':
                self.protocol.log_levels = values
            elif enum_name == 'status':
                self.protocol.status_codes = values
    
    def _parse_structs(self):
        """Extract struct definitions"""
        # More flexible pattern to handle different attribute syntaxes
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
        
        # Type mapping for size calculation - include signed types
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
                
            # Check if this line contains a complete field declaration
            if ';' in line:
                # Split by semicolon, but keep the parts
                parts = line.split(';')
                for part in parts[:-1]:  # All but last part end with semicolon
                    if current_field:
                        current_field.append(part)
                        field_lines.append(' '.join(current_field))
                        current_field = []
                    else:
                        field_lines.append(part)
                # Last part might not end with semicolon
                last_part = parts[-1].strip()
                if last_part:
                    current_field.append(last_part)
            else:
                # Continuation of a field declaration
                current_field.append(line)
        
        # Handle any remaining field
        if current_field:
            field_lines.append(' '.join(current_field))
        
        # Parse each field line
        for field_line in field_lines:
            field_line = field_line.strip()
            if not field_line:
                continue
                
            # Parse field declaration
            # Format: "type name;" or "type name[size];"
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
            'Auto-generated protocol definitions for ESP32 control interface.',
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
            'from typing import Optional, Union, Tuple, Any',
            'import socket',
            ''
        ])
    
    def _add_constants(self):
        """Add protocol constants"""
        if self.p.version is not None:
            self.output.append(f'PROTOCOL_VERSION = {self.p.version}')
        
        if self.p.magic:
            self.output.append('\n# Magic bytes')
            for name, value in sorted(self.p.magic.items()):
                self.output.append(f'PROTOCOL_MAGIC_{name} = {value}')
        
        if self.p.log_max_len is not None:
            self.output.append(f'\nLOG_MESSAGE_MAX_LEN = {self.p.log_max_len}')
        
        self.output.append('')
    
    def _add_enums(self):
        """Add enum classes"""
        enum_configs = [
            ('State', self.p.states, 'States'),
            ('Cmd', self.p.commands, 'Command codes'),
            ('Qry', self.p.queries, 'Query codes'),
            ('Ind', self.p.indications, 'Indication/Event codes'),
            ('LogLevel', self.p.log_levels, 'Log levels'),
            ('Status', self.p.status_codes, 'Status codes')
        ]
        
        for enum_name, values, description in enum_configs:
            if values:
                self.output.extend([
                    f'class {enum_name}(IntEnum):',
                    f'    """{description}"""'
                ])
                
                # Sort by value for consistent output
                for name, value in sorted(values.items(), key=lambda x: x[1]):
                    self.output.append(f'    {name} = {value}')
                
                self.output.append('')
    
    def _get_struct_format(self, struct_name: str) -> Tuple[str, int]:
        """Get struct format string and size"""
        if struct_name not in self.p.structs:
            return None, 0
        
        struct_info = self.p.structs[struct_name]
        format_chars = []
        
        # Type to struct format mapping - include signed types
        type_format = {
            'uint8_t': 'B',
            'uint16_t': 'H',
            'uint32_t': 'I',
            'int8_t': 'b',
            'int16_t': 'h',
            'int32_t': 'i',
            'char': 's'
        }
        
        # Preserve original field order - don't sort
        for field, (type_name, size, _) in struct_info.items():
            if type_name.startswith('char['):
                # Handle char array
                array_size = int(type_name.split('[')[1].rstrip(']'))
                format_chars.append(f'{array_size}s')
            elif type_name == 'char':
                format_chars.append('s')
            else:
                # Look up the format character, default to 'B' for unknown types
                format_char = type_format.get(type_name, 'B')
                format_chars.append(format_char)
        
        format_str = '<' + ''.join(format_chars)  # Little-endian
        total_size = struct.calcsize(format_str)
        
        return format_str, total_size
    
    def _add_message_classes(self):
        """Add message class definitions"""
        # Map struct names (without _t suffix) to message class names and magic constants
        message_types = [
            ('cmd_msg', 'CmdMsg', 'CMD', ['command', 'reference', 'param_1', 'param_2', 'param_3', 'param_4']),
            ('qry_msg', 'QryMsg', 'QRY', ['query', 'reference',]),
            ('rsp_msg', 'RspMsg', 'RSP', ['cmd_or_qry', 'reference', 'status', 'value']),
            ('ind_msg', 'IndMsg', 'IND', ['ind', 'value']),
            ('log_msg', 'LogMsg', 'LOG', ['level', 'message'])
        ]
        
        for struct_name, class_name, magic_name, fields in message_types:
            if struct_name in self.p.structs:
                self._generate_message_class(struct_name, class_name, magic_name, fields)
    
    def _generate_message_class(self, struct_name: str, class_name: str, 
                               magic_name: str, fields: List[str]):
        """Generate a specific message class"""
        format_str, total_size = self._get_struct_format(struct_name)
        if format_str is None:
            return
        
        # Handle special case for log message which has a string
        is_log_msg = (class_name == 'LogMsg')
        
        # Class definition
        self.output.extend([
            f'class {class_name}:',
            f'    """{class_name} - packed binary message"""'
        ])
        
        # FORMAT and SIZE
        if is_log_msg:
            self.output.extend([
                f'    FORMAT = f"<BB {{LOG_MESSAGE_MAX_LEN}}s"',
                f'    SIZE = 2 + LOG_MESSAGE_MAX_LEN',
                f'    MAGIC = PROTOCOL_MAGIC_{magic_name}',
                ''
            ])
        else:
            self.output.extend([
                f'    FORMAT = "{format_str}"',
                f'    SIZE = {total_size}',
                f'    MAGIC = PROTOCOL_MAGIC_{magic_name}',
                ''
            ])
        
        # __init__ method
        if is_log_msg:
            self.output.extend([
                f'    def __init__(self, level, message=""):',
                f'        self.magic = self.MAGIC',
                f'        self.level = level',
                f'        self.message = message',
                ''
            ])
        else:
            # For non-log messages, generate params with defaults
            params = []
            for i, field in enumerate(fields):
                if i == 0:
                    params.append(f"{field}")
                else:
                    params.append(f"{field}=0")
            
            self.output.extend([
                f'    def __init__(self, {", ".join(params)}):',
                f'        self.magic = self.MAGIC'
            ])
            
            for field in fields:
                self.output.append(f'        self.{field} = {field}')
            
            self.output.append('')
        
        # pack method
        if is_log_msg:
            self.output.extend([
                '    def pack(self) -> bytes:',
                '        """Pack message into bytes for transmission"""',
                '        # Truncate message if too long',
                '        msg_bytes = self.message.encode("utf-8")[:LOG_MESSAGE_MAX_LEN-1]',
                '        # Pad with null bytes to reach full size',
                '        padded = msg_bytes + b"\\0" * (LOG_MESSAGE_MAX_LEN - len(msg_bytes))',
                '        return struct.pack(self.FORMAT,',
                '                          self.magic,',
                '                          self.level,',
                '                          padded)',
                ''
            ])
        else:
            pack_args = ['self.magic'] + [f'self.{field}' for field in fields]
            self.output.extend([
                '    def pack(self) -> bytes:',
                '        """Pack message into bytes for transmission"""',
                f'        return struct.pack(self.FORMAT,',
                f'                          {", ".join(pack_args)})',
                ''
            ])
        
        # unpack method
        if is_log_msg:
            self.output.extend([
                '    @classmethod',
                f'    def unpack(cls, data: bytes) -> "{class_name}":',
                '        """Unpack bytes into a message instance"""',
                '        if len(data) != cls.SIZE:',
                '            raise ValueError(f"Invalid message size: got {len(data)}, expected {cls.SIZE}")',
                '        magic, level, msg_bytes = struct.unpack(cls.FORMAT, data)',
                '        if magic != cls.MAGIC:',
                '            raise ValueError(f"Invalid magic byte: got {magic:#x}, expected {cls.MAGIC:#x}")',
                '        # Decode null-terminated string',
                '        message = msg_bytes.split(b"\\0", 1)[0].decode("utf-8")',
                f'        return cls(level, message)',
                ''
            ])
        else:
            self.output.extend([
                '    @classmethod',
                f'    def unpack(cls, data: bytes) -> "{class_name}":',
                '        """Unpack bytes into a message instance"""',
                '        if len(data) != cls.SIZE:',
                '            raise ValueError(f"Invalid message size: got {len(data)}, expected {cls.SIZE}")',
                '        values = struct.unpack(cls.FORMAT, data)',
                '        magic = values[0]',
                '        if magic != cls.MAGIC:',
                '            raise ValueError(f"Invalid magic byte: got {magic:#x}, expected {cls.MAGIC:#x}")',
                f'        return cls(*values[1:])',
                ''
            ])
        
        # __repr__ method - FIXED with self. references and proper formatting
        if is_log_msg:
            self.output.extend([
                '    def __repr__(self):',
                f'        return f"<{class_name} level={{self.level}}, message=\\"{{self.message}}\\">"',
                ''
            ])
        else:
            # Build repr string with self. references
            self.output.append('    def __repr__(self):')
            repr_parts = [f"<{class_name}"]
            for field in fields:
                repr_parts.append(f" {field}={{{field}}}")
            repr_parts.append(">")
            repr_line = '        return f"' + ''.join(repr_parts) + '"'
            # Replace {field} with {self.field}
            repr_line = repr_line.replace('{', '{self.')
            self.output.append(repr_line)
            self.output.append('')

    def _add_helper_functions(self):
        """Add utility functions for working with the protocol"""
        self.output.extend([
            'def send_message(sock: socket.socket, msg) -> bool:',
            '    """Send a protocol message over a socket"""',
            '    try:',
            '        data = msg.pack()',
            '        sock.sendall(data)',
            '        return True',
            '    except Exception as e:',
            '        print(f"Error sending message: {e}")',
            '        return False',
            '',
            'def receive_message(sock: socket.socket, msg_class, timeout: Optional[float] = None):',
            '    """Receive and unpack a specific message type"""',
            '    original_timeout = sock.gettimeout()',
            '    try:',
            '        if timeout is not None:',
            '            sock.settimeout(timeout)',
            '        data = sock.recv(msg_class.SIZE)',
            '        if len(data) == 0:',
            '            return None  # Connection closed',
            '        return msg_class.unpack(data)',
            '    except socket.timeout:',
            '        return None',
            '    except Exception as e:',
            '        print(f"Error receiving message: {e}")',
            '        return None',
            '    finally:',
            '        sock.settimeout(original_timeout)',
            ''
        ])
    
    def print_usage_instructions(self):
        """Print usage instructions for the generated module"""
        log_max_len = self.p.log_max_len or 256
        instructions = f"""
╔═════════════════════════════════════════════════════════�
�══════════════════╗
║                    Protocol Module Generated Successfully                   ║
╚═════════════════════════════════════════════════════════════�
�══════════════╝

The Python protocol module has been generated. Here's how to use it:

📦 IMPORTING
─────────────────────────────────────────────────────────────�
��──────────────
    from protocol import (
        # Enums
        Cmd, Qry, Ind, LogLevel, Status,
        StateStand, StateLift, StatePlinkyPlonky, StateDoor,
        # Message classes
        CmdMsg, RspMsg, IndMsg, LogMsg,
        # Helper functions
        send_message, receive_message,
        # Constants
        PROTOCOL_VERSION, LOG_MESSAGE_MAX_LEN
    )

🎯 SENDING COMMANDS
────────────────────────────────────────────────────────────────
────────────
    # Create a CMD_STEPPER_TARGET_START command with all 4 parameters
    cmd = CmdMsg(
        command=Cmd.CMD_STEPPER_TARGET_START,
        reference=1,                           # reference to be returned in the response
        param_1=State.STATE_LIFT_RISING,       # target state
        param_2=100,                           # velocity in TSTEPs
        param_3=500,                           # current in mA
        param_4=5000                           # timeout in ms
    )
    
    # Send over socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('192.168.1.100', 5000))
    send_message(sock, cmd)

📥 RECEIVING RESPONSES
──────────────────────────────────────────────────────────────�
�─────────────
    # Wait for response (5 second timeout)
    reference = 1
    response = receive_message(sock, RspMsg, timeout=5.0)
    if response:
        if response.reference == reference
            if response.status == Status.STATUS_OK:
                print(f"Success! Value: {{response.value}}")
            else:
                print(f"Error: {{Status(response.status).name}}")

🔔 HANDLING INDICATIONS
─────────────────────────────────────────────────────────────────�
��──────────
    # Receive an asynchronous indication
    ind = receive_message(sock, IndMsg)
    if ind:
        print(f"Indication: {{Ind(ind.ind).name}}, value: {{ind.value}}")

📝 SENDING LOGS
────────────────────────────────────────────────────────────�
�───────────────
    # Send a log message
    log = LogMsg(LogLevel.LOG_INFO, "System initialized")
    send_message(log_sock, log)

🔄 COMPLETE EXAMPLE
────────────────────────────────────────────────────────────�
�───────────────
    import socket
    from protocol import *
    
    # Connect to ESP32
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect(('192.168.1.100', 5000))
    
    # Send CMD_STEPPER_TARGET_START
    reference = 1;
    cmd = CmdMsg(Cmd.CMD_STEPPER_TARGET_START, reference, 
                 State.STATE_LIFT_RISING, 100, 500, 5000)
    
    if send_message(sock, cmd):
        response = receive_message(sock, RspMsg, timeout=5.0)
        if response and response.reference == reference and response.status == Status.STATUS_OK:
            print("Operation completed successfully")
    
    sock.close()

⚠️ NOTES
───────────────────────────────────────────────────────────────────────────�
��
    • All multi-byte fields are little-endian (matches ESP32 and Raspberry Pi)
    • TCP provides reliable delivery - no checksums needed
    • Message sizes are fixed - receive exactly SIZE bytes
    • CmdMsg has 4 parameters for the CMD_STEPPER_TARGET_START command
    • Other commands may use only the first parameter
    • Log messages are automatically null-padded to LOG_MESSAGE_MAX_LEN

For more details, see the protocol definition in the original C header file.
"""
        print(instructions)


def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: python3 generate_protocol.py <protocol.h> [output.py]")
        sys.exit(1)
    
    header_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else 'protocol.py'
    
    print(f"Parsing {header_path}...")
    parser = CHeaderParser(header_path)
    protocol = parser.parse()
    
    print(f"\nFound:")
    print(f"  - {len(protocol.magic)} magic bytes")
    print(f"  - {len(protocol.states)} states")
    print(f"  - {len(protocol.commands)} commands")
    print(f"  - {len(protocol.queries)} queries")
    print(f"  - {len(protocol.indications)} indications")
    print(f"  - {len(protocol.log_levels)} log levels")
    print(f"  - {len(protocol.status_codes)} status codes")
    print(f"  - {len(protocol.structs)} structs")
    
    # List the structs found
    if protocol.structs:
        print(f"\nStructs found: {list(protocol.structs.keys())}")
    else:
        print("\n⚠️  WARNING: No structs found! Message classes will not be generated.")
    
    print(f"\nGenerating {output_path}...")
    generator = PythonGenerator(protocol)
    python_code = generator.generate()
    
    Path(output_path).write_text(python_code)
    
    # Print usage instructions
    generator.print_usage_instructions()
    
    print(f"\n✅ Successfully generated {output_path}")


if __name__ == "__main__":
    main()

