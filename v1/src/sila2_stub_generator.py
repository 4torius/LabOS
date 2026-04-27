#!/usr/bin/env python3
"""
Dynamic Stub Generator - Runtime gRPC Code Generation
======================================================

Generates Python gRPC stubs at runtime from:
- .sila.xml Feature Definition files
- .proto Protocol Buffer files

This eliminates the need for pre-compiled stubs, enabling
true plug & play device integration.

Usage:
    generator = DynamicStubGenerator()
    
    # From .sila.xml
    stubs = generator.generate_from_sila_xml("IncubatorControl.sila.xml")
    
    # From .proto
    stubs = generator.generate_from_proto("incubator.proto")
    
    # Use the generated stubs
    stub = stubs.create_stub(channel)
    response = await stub.SetTemperature(request)

Author: BicoccaLab
Date: 2026-01-30
"""

import os
import re
import sys
import tempfile
import subprocess
import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import logging

logger = logging.getLogger(__name__)


#                           DATA STRUCTURES

@dataclass
class ProtoField:
    """Protobuf field definition."""
    name: str
    type: str
    number: int
    repeated: bool = False
    optional: bool = False


@dataclass
class ProtoMessage:
    """Protobuf message definition."""
    name: str
    fields: List[ProtoField] = field(default_factory=list)


@dataclass
class ProtoMethod:
    """Protobuf service method."""
    name: str
    input_type: str
    output_type: str
    client_streaming: bool = False
    server_streaming: bool = False


@dataclass
class ProtoService:
    """Protobuf service definition."""
    name: str
    methods: List[ProtoMethod] = field(default_factory=list)


@dataclass
class GeneratedStubs:
    """Container for generated stub classes."""
    service_name: str
    module_pb2: Any  # The _pb2 module
    module_pb2_grpc: Any  # The _pb2_grpc module
    stub_class: type
    message_classes: Dict[str, type]
    
    def create_stub(self, channel) -> Any:
        """Create a stub instance from a channel."""
        return self.stub_class(channel)
    
    def get_request(self, method_name: str) -> Optional[type]:
        """Get the request message class for a method."""
        request_name = f"{method_name}Request"
        return self.message_classes.get(request_name)
    
    def get_response(self, method_name: str) -> Optional[type]:
        """Get the response message class for a method."""
        response_name = f"{method_name}Response"
        return self.message_classes.get(response_name)


#                    SILA XML TO PROTO CONVERTER

class SiLAToProtoConverter:
    """
    Converts SiLA2 Feature Definition XML to Protocol Buffers.
    
    The SiLA2 standard defines features in XML format, but gRPC
    requires .proto files. This converter bridges that gap.
    """
    
    # SiLA2 type to protobuf type mapping
    TYPE_MAP = {
        "String": "string",
        "Integer": "int64",
        "Real": "double",
        "Boolean": "bool",
        "Binary": "bytes",
        "Date": "string",
        "Time": "string",
        "Timestamp": "string",
        "Void": "google.protobuf.Empty",
        "Any": "google.protobuf.Any",
    }
    
    def __init__(self):
        self._messages: Dict[str, ProtoMessage] = {}
        self._services: List[ProtoService] = []
    
    def convert(self, sila_xml_path: Path) -> str:
        """
        Convert a .sila.xml file to .proto format.
        
        Args:
            sila_xml_path: Path to the SiLA2 Feature Definition XML
        
        Returns:
            Proto file content as string
        """
        self._messages.clear()
        self._services.clear()
        
        tree = ET.parse(sila_xml_path)
        root = tree.getroot()
        
        # Extract feature info
        feature_id = self._get_text(root, "Identifier") or "UnknownFeature"
        
        # Create service
        service = ProtoService(name=f"{feature_id}Service")
        
        # Parse commands
        field_num = 1
        for cmd_elem in root.iter():
            if cmd_elem.tag.endswith('Command') and not cmd_elem.tag.endswith('ObservableCommand'):
                cmd_name = self._get_text(cmd_elem, "Identifier")
                if cmd_name:
                    # Create request message
                    request_msg = self._parse_command_request(cmd_elem, cmd_name, field_num)
                    self._messages[request_msg.name] = request_msg
                    
                    # Create response message
                    response_msg = self._parse_command_response(cmd_elem, cmd_name, field_num)
                    self._messages[response_msg.name] = response_msg
                    
                    # Check if observable
                    observable = self._get_text(cmd_elem, "Observable") == "Yes"
                    
                    # Create method
                    method = ProtoMethod(
                        name=cmd_name,
                        input_type=request_msg.name,
                        output_type=response_msg.name,
                        server_streaming=observable
                    )
                    service.methods.append(method)
                    field_num += 10
        
        # Parse properties
        for prop_elem in root.iter():
            if prop_elem.tag.endswith('Property'):
                prop_name = self._get_text(prop_elem, "Identifier")
                if prop_name:
                    # Create response message
                    response_msg = self._parse_property(prop_elem, prop_name, field_num)
                    self._messages[response_msg.name] = response_msg
                    
                    # Create empty request
                    request_name = f"Get{prop_name}Request"
                    self._messages[request_name] = ProtoMessage(name=request_name)
                    
                    observable = self._get_text(prop_elem, "Observable") == "Yes"
                    
                    # Getter method
                    getter = ProtoMethod(
                        name=f"Get{prop_name}",
                        input_type=request_name,
                        output_type=response_msg.name
                    )
                    service.methods.append(getter)
                    
                    # Subscribe method if observable
                    if observable:
                        subscribe_request = f"Subscribe{prop_name}Request"
                        self._messages[subscribe_request] = ProtoMessage(name=subscribe_request)
                        
                        subscriber = ProtoMethod(
                            name=f"Subscribe{prop_name}",
                            input_type=subscribe_request,
                            output_type=response_msg.name,
                            server_streaming=True
                        )
                        service.methods.append(subscriber)
                    
                    field_num += 10
        
        self._services.append(service)
        
        # Generate proto content
        return self._generate_proto(feature_id)
    
    def _parse_command_request(
        self,
        cmd_elem: ET.Element,
        cmd_name: str,
        base_field_num: int
    ) -> ProtoMessage:
        """Parse command parameters into a request message."""
        msg = ProtoMessage(name=f"{cmd_name}Request")
        field_num = 1
        
        for param_elem in cmd_elem.iter():
            if param_elem.tag.endswith('Parameter'):
                param_id = self._get_text(param_elem, "Identifier")
                param_type = self._parse_data_type(param_elem)
                
                if param_id:
                    proto_type = self.TYPE_MAP.get(param_type, "string")
                    msg.fields.append(ProtoField(
                        name=self._to_snake_case(param_id),
                        type=proto_type,
                        number=field_num
                    ))
                    field_num += 1
        
        return msg
    
    def _parse_command_response(
        self,
        cmd_elem: ET.Element,
        cmd_name: str,
        base_field_num: int
    ) -> ProtoMessage:
        """Parse command response into a response message."""
        msg = ProtoMessage(name=f"{cmd_name}Response")
        field_num = 1
        
        for resp_elem in cmd_elem.iter():
            if resp_elem.tag.endswith('Response'):
                resp_id = self._get_text(resp_elem, "Identifier")
                resp_type = self._parse_data_type(resp_elem)
                
                if resp_id:
                    proto_type = self.TYPE_MAP.get(resp_type, "string")
                    msg.fields.append(ProtoField(
                        name=self._to_snake_case(resp_id),
                        type=proto_type,
                        number=field_num
                    ))
                    field_num += 1
        
        # Add default value field if no response defined
        if not msg.fields:
            msg.fields.append(ProtoField(name="value", type="string", number=1))
        
        return msg
    
    def _parse_property(
        self,
        prop_elem: ET.Element,
        prop_name: str,
        base_field_num: int
    ) -> ProtoMessage:
        """Parse property into a response message."""
        msg = ProtoMessage(name=f"Get{prop_name}Response")
        prop_type = self._parse_data_type(prop_elem)
        proto_type = self.TYPE_MAP.get(prop_type, "string")
        
        msg.fields.append(ProtoField(
            name="value",
            type=proto_type,
            number=1
        ))
        
        return msg
    
    def _parse_data_type(self, elem: ET.Element) -> str:
        """Extract data type from element."""
        for child in elem.iter():
            if child.tag.endswith('Basic') and child.text:
                return child.text.strip()
        return "String"
    
    def _get_text(self, elem: ET.Element, tag: str) -> Optional[str]:
        """Get text from child element."""
        for child in elem:
            if child.tag.endswith(tag):
                return child.text.strip() if child.text else None
        return None
    
    def _to_snake_case(self, name: str) -> str:
        """Convert PascalCase to snake_case."""
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
    
    def _generate_proto(self, feature_id: str) -> str:
        """Generate the complete .proto file content."""
        lines = [
            'syntax = "proto3";',
            '',
            f'package sila2.{feature_id.lower()};',
            '',
            f'option java_package = "org.silastandard.{feature_id.lower()}";',
            '',
        ]
        
        # Generate messages
        for msg in self._messages.values():
            lines.append(f'message {msg.name} {{')
            for f in msg.fields:
                repeated = 'repeated ' if f.repeated else ''
                lines.append(f'    {repeated}{f.type} {f.name} = {f.number};')
            lines.append('}')
            lines.append('')
        
        # Generate services
        for service in self._services:
            lines.append(f'service {service.name} {{')
            for method in service.methods:
                stream_prefix = 'stream ' if method.server_streaming else ''
                lines.append(
                    f'    rpc {method.name}({method.input_type}) '
                    f'returns ({stream_prefix}{method.output_type});'
                )
            lines.append('}')
            lines.append('')
        
        return '\n'.join(lines)


#                    DYNAMIC STUB GENERATOR

class DynamicStubGenerator:
    """
    Generates Python gRPC stubs at runtime.
    
    This enables true plug & play: when a new SiLA2 server is discovered,
    we can generate the necessary stubs on the fly.
    """
    
    def __init__(self, output_dir: Optional[Path] = None):
        """
        Initialize the generator.
        
        Args:
            output_dir: Where to store generated files (temp dir if None)
        """
        self._output_dir = output_dir or Path(tempfile.mkdtemp(prefix="sila2_stubs_"))
        self._converter = SiLAToProtoConverter()
        self._generated: Dict[str, GeneratedStubs] = {}
        
        # Ensure output dir exists
        self._output_dir.mkdir(parents=True, exist_ok=True)
        
        # Add to Python path for imports
        if str(self._output_dir) not in sys.path:
            sys.path.insert(0, str(self._output_dir))
    
    def generate_from_sila_xml(self, xml_path: Path) -> Optional[GeneratedStubs]:
        """
        Generate stubs from a SiLA2 Feature Definition XML.
        
        Args:
            xml_path: Path to the .sila.xml file
        
        Returns:
            GeneratedStubs object with the compiled modules
        """
        xml_path = Path(xml_path)
        
        # Check cache
        cache_key = str(xml_path)
        if cache_key in self._generated:
            return self._generated[cache_key]
        
        try:
            # Convert to proto
            proto_content = self._converter.convert(xml_path)
            
            # Extract service name from the file
            feature_id = xml_path.stem.replace('.sila', '')
            proto_filename = f"{feature_id}.proto"
            proto_path = self._output_dir / proto_filename
            
            # Write proto file
            with open(proto_path, 'w') as f:
                f.write(proto_content)
            
            logger.info(f"Generated proto: {proto_path}")
            
            # Compile proto to Python
            stubs = self.generate_from_proto(proto_path)
            
            if stubs:
                self._generated[cache_key] = stubs
            
            return stubs
            
        except Exception as e:
            logger.error(f"Failed to generate stubs from {xml_path}: {e}")
            return None
    
    def generate_from_proto(self, proto_path: Path) -> Optional[GeneratedStubs]:
        """
        Generate stubs from a .proto file.
        
        Args:
            proto_path: Path to the .proto file
        
        Returns:
            GeneratedStubs object with the compiled modules
        """
        proto_path = Path(proto_path)
        
        # Check cache
        cache_key = str(proto_path)
        if cache_key in self._generated:
            return self._generated[cache_key]
        
        try:
            # Run protoc to generate Python code
            result = subprocess.run(
                [
                    sys.executable, '-m', 'grpc_tools.protoc',
                    f'-I{proto_path.parent}',
                    f'--python_out={self._output_dir}',
                    f'--grpc_python_out={self._output_dir}',
                    str(proto_path)
                ],
                capture_output=True,
                text=True
            )
            
            if result.returncode != 0:
                logger.error(f"protoc failed: {result.stderr}")
                # Try alternative method
                return self._generate_inline(proto_path)
            
            # Load the generated modules
            module_name = proto_path.stem
            pb2_path = self._output_dir / f"{module_name}_pb2.py"
            grpc_path = self._output_dir / f"{module_name}_pb2_grpc.py"
            
            if not pb2_path.exists():
                logger.error(f"Generated pb2 not found: {pb2_path}")
                return None
            
            # Import modules
            pb2_module = self._import_module(f"{module_name}_pb2", pb2_path)
            grpc_module = self._import_module(f"{module_name}_pb2_grpc", grpc_path) if grpc_path.exists() else None
            
            if not pb2_module:
                return None
            
            # Find stub class and message classes
            stub_class = None
            message_classes = {}
            
            if grpc_module:
                for name in dir(grpc_module):
                    if name.endswith('Stub'):
                        stub_class = getattr(grpc_module, name)
                        break
            
            for name in dir(pb2_module):
                obj = getattr(pb2_module, name)
                if hasattr(obj, 'DESCRIPTOR') and hasattr(obj, 'SerializeToString'):
                    message_classes[name] = obj
            
            stubs = GeneratedStubs(
                service_name=module_name,
                module_pb2=pb2_module,
                module_pb2_grpc=grpc_module,
                stub_class=stub_class,
                message_classes=message_classes
            )
            
            self._generated[cache_key] = stubs
            logger.info(f"Generated stubs for {module_name}: {len(message_classes)} messages")
            
            return stubs
            
        except Exception as e:
            logger.error(f"Failed to generate stubs from {proto_path}: {e}")
            return None
    
    def _generate_inline(self, proto_path: Path) -> Optional[GeneratedStubs]:
        """
        Generate stubs inline without calling protoc.
        
        This is a fallback when grpc_tools is not available.
        Uses a simplified code generation approach.
        """
        logger.info("Using inline stub generation (grpc_tools not available)")
        
        proto_content = proto_path.read_text()
        module_name = proto_path.stem
        
        # Parse proto content
        messages, services = self._parse_proto(proto_content)
        
        # Generate pb2 code
        pb2_code = self._generate_pb2_code(module_name, messages)
        pb2_path = self._output_dir / f"{module_name}_pb2.py"
        pb2_path.write_text(pb2_code)
        
        # Generate grpc code
        grpc_code = self._generate_grpc_code(module_name, services)
        grpc_path = self._output_dir / f"{module_name}_pb2_grpc.py"
        grpc_path.write_text(grpc_code)
        
        # Import and return
        pb2_module = self._import_module(f"{module_name}_pb2", pb2_path)
        grpc_module = self._import_module(f"{module_name}_pb2_grpc", grpc_path)
        
        if not pb2_module:
            return None
        
        # Find classes
        stub_class = None
        message_classes = {}
        
        if grpc_module:
            for name in dir(grpc_module):
                if name.endswith('Stub'):
                    stub_class = getattr(grpc_module, name)
                    break
        
        for name in dir(pb2_module):
            obj = getattr(pb2_module, name)
            if isinstance(obj, type) and hasattr(obj, 'SerializeToString'):
                message_classes[name] = obj
        
        return GeneratedStubs(
            service_name=module_name,
            module_pb2=pb2_module,
            module_pb2_grpc=grpc_module,
            stub_class=stub_class,
            message_classes=message_classes
        )
    
    def _parse_proto(self, content: str) -> Tuple[List[ProtoMessage], List[ProtoService]]:
        """Parse proto file content."""
        messages = []
        services = []
        
        # Parse messages
        msg_pattern = r'message\s+(\w+)\s*\{([^}]*)\}'
        for match in re.finditer(msg_pattern, content):
            msg_name = match.group(1)
            msg_body = match.group(2)
            
            fields = []
            field_pattern = r'(\w+)\s+(\w+)\s*=\s*(\d+)'
            for field_match in re.finditer(field_pattern, msg_body):
                fields.append(ProtoField(
                    type=field_match.group(1),
                    name=field_match.group(2),
                    number=int(field_match.group(3))
                ))
            
            messages.append(ProtoMessage(name=msg_name, fields=fields))
        
        # Parse services
        svc_pattern = r'service\s+(\w+)\s*\{([^}]*)\}'
        for match in re.finditer(svc_pattern, content):
            svc_name = match.group(1)
            svc_body = match.group(2)
            
            methods = []
            method_pattern = r'rpc\s+(\w+)\s*\(\s*(\w+)\s*\)\s*returns\s*\(\s*(stream\s+)?(\w+)\s*\)'
            for method_match in re.finditer(method_pattern, svc_body):
                methods.append(ProtoMethod(
                    name=method_match.group(1),
                    input_type=method_match.group(2),
                    output_type=method_match.group(4),
                    server_streaming=method_match.group(3) is not None
                ))
            
            services.append(ProtoService(name=svc_name, methods=methods))
        
        return messages, services
    
    def _generate_pb2_code(self, module_name: str, messages: List[ProtoMessage]) -> str:
        """Generate simplified pb2 module code."""
        lines = [
            '"""Generated protocol buffer code."""',
            'from dataclasses import dataclass, field',
            'from typing import Any, Dict, List, Optional',
            'import struct',
            '',
        ]
        
        for msg in messages:
            lines.append('@dataclass')
            lines.append(f'class {msg.name}:')
            lines.append('    """Generated message class."""')
            
            if not msg.fields:
                lines.append('    pass')
            else:
                for f in msg.fields:
                    py_type = self._proto_to_python_type(f.type)
                    default = self._proto_default(f.type)
                    lines.append(f'    {f.name}: {py_type} = {default}')
            
            lines.append('')
            lines.append('    def SerializeToString(self) -> bytes:')
            lines.append('        """Serialize to protobuf bytes."""')
            lines.append('        return self._serialize()')
            lines.append('')
            lines.append('    def ParseFromString(self, data: bytes):')
            lines.append('        """Parse from protobuf bytes."""')
            lines.append('        self._parse(data)')
            lines.append('        return self')
            lines.append('')
            lines.append('    def _serialize(self) -> bytes:')
            lines.append('        result = b""')
            for i, f in enumerate(msg.fields, 1):
                lines.append(f'        result += self._encode_field({i}, self.{f.name}, "{f.type}")')
            lines.append('        return result')
            lines.append('')
            lines.append('    def _parse(self, data: bytes):')
            lines.append('        pass  # Simplified - full parsing not implemented')
            lines.append('')
            lines.append('    def _encode_field(self, num: int, value: Any, ftype: str) -> bytes:')
            lines.append('        if value is None:')
            lines.append('            return b""')
            lines.append('        tag = (num << 3)')
            lines.append('        if ftype == "string":')
            lines.append('            encoded = value.encode("utf-8") if isinstance(value, str) else str(value).encode()')
            lines.append('            return bytes([tag | 2]) + self._varint(len(encoded)) + encoded')
            lines.append('        elif ftype == "double":')
            lines.append('            return bytes([tag | 1]) + struct.pack("<d", float(value))')
            lines.append('        elif ftype in ("int64", "int32"):')
            lines.append('            return bytes([tag]) + self._varint(int(value))')
            lines.append('        elif ftype == "bool":')
            lines.append('            return bytes([tag]) + bytes([1 if value else 0])')
            lines.append('        return b""')
            lines.append('')
            lines.append('    def _varint(self, value: int) -> bytes:')
            lines.append('        result = b""')
            lines.append('        while value > 127:')
            lines.append('            result += bytes([(value & 0x7F) | 0x80])')
            lines.append('            value >>= 7')
            lines.append('        return result + bytes([value & 0x7F])')
            lines.append('')
        
        return '\n'.join(lines)
    
    def _generate_grpc_code(self, module_name: str, services: List[ProtoService]) -> str:
        """Generate simplified grpc module code."""
        lines = [
            '"""Generated gRPC stubs."""',
            'import grpc',
            f'from . import {module_name}_pb2 as pb2',
            '',
        ]
        
        for svc in services:
            # Stub class
            lines.append(f'class {svc.name}Stub:')
            lines.append('    """Client stub for the service."""')
            lines.append('')
            lines.append('    def __init__(self, channel):')
            lines.append('        self._channel = channel')
            
            for method in svc.methods:
                if method.server_streaming:
                    lines.append(f'        self.{method.name} = channel.unary_stream(')
                else:
                    lines.append(f'        self.{method.name} = channel.unary_unary(')
                lines.append(f'            "/{module_name}.{svc.name}/{method.name}",')
                lines.append(f'            request_serializer=pb2.{method.input_type}.SerializeToString,')
                lines.append(f'            response_deserializer=pb2.{method.output_type}.ParseFromString,')
                lines.append('        )')
            
            lines.append('')
            
            # Servicer class
            lines.append(f'class {svc.name}Servicer:')
            lines.append('    """Server servicer interface."""')
            for method in svc.methods:
                lines.append(f'    def {method.name}(self, request, context):')
                lines.append('        raise NotImplementedError()')
            lines.append('')
            
            # Registration function
            lines.append(f'def add_{svc.name}Servicer_to_server(servicer, server):')
            lines.append('    """Add servicer to server."""')
            lines.append('    rpc_method_handlers = {')
            for method in svc.methods:
                if method.server_streaming:
                    handler_type = 'unary_stream'
                else:
                    handler_type = 'unary_unary'
                lines.append(f'        "{method.name}": grpc.{handler_type}_rpc_method_handler(')
                lines.append(f'            servicer.{method.name},')
                lines.append(f'            request_deserializer=pb2.{method.input_type}.ParseFromString,')
                lines.append(f'            response_serializer=pb2.{method.output_type}.SerializeToString,')
                lines.append('        ),')
            lines.append('    }')
            lines.append(f'    generic_handler = grpc.method_handlers_generic_handler(')
            lines.append(f'        "{module_name}.{svc.name}", rpc_method_handlers)')
            lines.append('    server.add_generic_rpc_handlers((generic_handler,))')
            lines.append('')
        
        return '\n'.join(lines)
    
    def _proto_to_python_type(self, proto_type: str) -> str:
        """Convert proto type to Python type hint."""
        mapping = {
            'string': 'str',
            'int32': 'int',
            'int64': 'int',
            'double': 'float',
            'float': 'float',
            'bool': 'bool',
            'bytes': 'bytes',
        }
        return mapping.get(proto_type, 'Any')
    
    def _proto_default(self, proto_type: str) -> str:
        """Get Python default value for proto type."""
        mapping = {
            'string': '""',
            'int32': '0',
            'int64': '0',
            'double': '0.0',
            'float': '0.0',
            'bool': 'False',
            'bytes': 'b""',
        }
        return mapping.get(proto_type, 'None')
    
    def _import_module(self, name: str, path: Path):
        """Dynamically import a module from path."""
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                return module
        except Exception as e:
            logger.error(f"Failed to import {name} from {path}: {e}")
        return None
    
    def clear_cache(self):
        """Clear the generated stubs cache."""
        self._generated.clear()
    
    def get_output_dir(self) -> Path:
        """Get the output directory."""
        return self._output_dir


#                              DEMO / TEST

def demo():
    """Demonstrate dynamic stub generation."""
    _log = logging.getLogger(__name__)
    _log.info("DYNAMIC STUB GENERATOR DEMO")

    generator = DynamicStubGenerator()
    sila_xml_path = Path(__file__).parent.parent / "SiLA2" / "IncubatorSiLA2Server" / "features" / "IncubatorControl.sila.xml"

    if sila_xml_path.exists():
        _log.info(f"[1] Converting: {sila_xml_path}")
        converter = SiLAToProtoConverter()
        proto_content = converter.convert(sila_xml_path)
        _log.info(f"[2] Generated Proto ({len(proto_content)} chars)")

        stubs = generator.generate_from_sila_xml(sila_xml_path)
        if stubs:
            _log.info(f"[3] Service: {stubs.service_name} | Messages: {list(stubs.message_classes.keys())}")
        else:
            _log.warning("[3] Stub generation failed (OK without grpc_tools)")
    else:
        _log.warning(f"SiLA XML not found: {sila_xml_path}")

    _log.info(f"[4] Output directory: {generator.get_output_dir()}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo()
