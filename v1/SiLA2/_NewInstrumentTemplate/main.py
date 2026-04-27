#!/usr/bin/env python3
"""
New Instrument SiLA2 Server — Template
=======================================

QUICK START (3 steps):
1. Rename this folder:  _NewInstrumentTemplate → YourInstrumentSiLA2Server
2. Edit config.yaml:    change server_name, port, uuid
3. Edit features/NewInstrument.sila.xml: define your commands/parameters
4. Edit src/servicer.py: implement each command method

The system will automatically:
- Expose all .sila.xml commands via SiLA2Common.GetFeatures
- Route ExecuteCommand to the matching method in InstrumentServicer
- Register the server on mDNS for automatic discovery
- No changes needed to LabOS core code!

Usage:
    python main.py [--port PORT] [--host HOST]
"""

import asyncio
import argparse
import inspect
import logging
import os
import signal
import sys
import time
from pathlib import Path
from concurrent import futures

import grpc
import yaml

# ─── Path setup ───────────────────────────────────────────────────────────────
_HERE = Path(__file__).parent
_SILA2_DIR = _HERE.parent

# SiLA2 directory on sys.path (for shared modules)
if str(_SILA2_DIR) not in sys.path:
    sys.path.insert(0, str(_SILA2_DIR))
# Src directory on sys.path (for servicer)
if str(_HERE / "src") not in sys.path:
    sys.path.insert(0, str(_HERE / "src"))

# ─── mDNS ─────────────────────────────────────────────────────────────────────
try:
    from sila2_mdns_registry import SiLA2ServerRegistry
    MDNS_AVAILABLE = True
except ImportError:
    MDNS_AVAILABLE = False

# ─── SiLA2Common stubs ────────────────────────────────────────────────────────
try:
    import SiLA2Common_pb2 as common_pb2
    import SiLA2Common_pb2_grpc as common_grpc
    SILA2_COMMON_AVAILABLE = True
except ImportError:
    SILA2_COMMON_AVAILABLE = False

# ─── XML parser ───────────────────────────────────────────────────────────────
try:
    from sila2_xml_parser import features_from_xml_dir, build_proto_features
    XML_PARSER_AVAILABLE = True
except ImportError:
    XML_PARSER_AVAILABLE = False

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


#                         SiLA2Common ADAPTER

class SiLA2CommonAdapter(common_grpc.SiLA2ServerInfoServicer if SILA2_COMMON_AVAILABLE else object):
    """
    Generic SiLA2Common adapter.

    - GetServerInfo  → reads config.yaml + .sila.xml description
    - GetFeatures    → reads features/*.sila.xml (auto-updated when XML changes)
    - GetStatus      → delegates to servicer._status
    - ExecuteCommand → routes dynamically to InstrumentServicer.CommandName()

    HOW COMMAND ROUTING WORKS:
    For each ExecuteCommand(command="Foo", parameters={...}), this adapter calls
    self._servicer.Foo(**params). No wiring code needed — just implement the method
    in InstrumentServicer with the same name as the <Identifier> in .sila.xml.
    """

    def __init__(self, servicer, config: dict):
        self._servicer = servicer
        self._config = config
        self._start_time = time.time()
        self._features_dir = str(_HERE / "features")

    async def GetServerInfo(self, request, context):
        sila2_cfg = self._config.get('sila2', {})
        server_cfg = self._config.get('server', {})

        # Description from XML (first feature's description)
        description = sila2_cfg.get('description', 'SiLA2 instrument server')
        if XML_PARSER_AVAILABLE:
            try:
                feats = features_from_xml_dir(self._features_dir)
                if feats:
                    description = feats[0].get('description', description)
            except Exception:
                pass

        return common_pb2.ServerInfoResponse(
            server_name=sila2_cfg.get('server_name', 'New Instrument'),
            server_type=sila2_cfg.get('server_type', 'instrument'),
            vendor=sila2_cfg.get('vendor', 'BicoccaLab'),
            server_version=sila2_cfg.get('version', '1.0.0'),
            sila_version='2.0',
            description=description,
            host=server_cfg.get('host', 'localhost'),
            uptime_seconds=int(time.time() - self._start_time),
            server_uuid=sila2_cfg.get('server_uuid', ''),
            hardware_connected=getattr(self._servicer, '_connected', False),
            hardware_status=getattr(self._servicer, '_status', 'unknown'),
        )

    async def GetFeatures(self, request, context):
        if not XML_PARSER_AVAILABLE:
            return common_pb2.FeaturesResponse(features=[])
        try:
            xml_features = features_from_xml_dir(self._features_dir)
            return common_pb2.FeaturesResponse(
                features=build_proto_features(xml_features, common_pb2)
            )
        except Exception as e:
            logger.warning(f"GetFeatures failed: {e}")
            return common_pb2.FeaturesResponse(features=[])

    async def GetStatus(self, request, context):
        connected = getattr(self._servicer, '_connected', False)
        status = getattr(self._servicer, '_status', 'unknown')
        return common_pb2.StatusResponse(
            status='running',
            server_online=True,
            hardware_online=connected,
            hardware_status=status,
            details={'instrument_status': status},
        )

    async def ExecuteCommand(self, request, responseStream, context):
        """
        Route command to InstrumentServicer method by name.

        Example: ExecuteCommand(command="Initialize") → servicer.Initialize()
        Parameters are passed as keyword args (type conversion: str → int/float/bool
        based on method signature annotations).
        """
        cmd = request.command
        raw_params = dict(request.parameters)

        try:
            handler = getattr(self._servicer, cmd, None)
            if handler is None:
                raise ValueError(f"Unknown command: {cmd}. "
                                 f"Implement {cmd}() in InstrumentServicer.")

            # Convert param types using method signature hints
            kwargs = _coerce_params(handler, raw_params)

            # Dispatch: async generator (Observable), coroutine, or sync
            if inspect.isasyncgenfunction(handler):
                async for chunk in handler(**kwargs):
                    result = chunk if isinstance(chunk, dict) else {'value': str(chunk)}
                    await responseStream.write(common_pb2.ExecuteCommandResponse(
                        success=True,
                        result=result,
                        is_intermediate=True,
                        progress=int(result.get('Progress', 50)),
                    ))
                await responseStream.write(common_pb2.ExecuteCommandResponse(
                    success=True, result={}, is_intermediate=False, progress=100
                ))

            elif asyncio.iscoroutinefunction(handler):
                result = await handler(**kwargs)
                await responseStream.write(common_pb2.ExecuteCommandResponse(
                    success=True,
                    result=_to_result_dict(result),
                    is_intermediate=False,
                    progress=100,
                ))

            else:
                result = handler(**kwargs)
                await responseStream.write(common_pb2.ExecuteCommandResponse(
                    success=True,
                    result=_to_result_dict(result),
                    is_intermediate=False,
                    progress=100,
                ))

        except Exception as e:
            logger.error(f"ExecuteCommand {cmd} failed: {e}")
            await responseStream.write(common_pb2.ExecuteCommandResponse(
                success=False, error=str(e), is_intermediate=False
            ))

    async def GetProperty(self, request, context):
        prop = request.property_name
        # Try get_<PropertyName>() on servicer
        getter = getattr(self._servicer, f'get_{prop}', None)
        if getter:
            try:
                value = getter() if not asyncio.iscoroutinefunction(getter) else await getter()
                return common_pb2.PropertyResponse(
                    property_name=prop,
                    value=str(value),
                )
            except Exception as e:
                return common_pb2.PropertyResponse(property_name=prop, error=str(e))
        return common_pb2.PropertyResponse(
            property_name=prop,
            error=f"Unknown property: {prop}"
        )


#                         HELPERS

def _coerce_params(handler, raw_params: dict) -> dict:
    """Convert string params to typed values using method signature annotations."""
    try:
        sig = inspect.signature(handler)
    except (ValueError, TypeError):
        return raw_params

    kwargs = {}
    for name, param in sig.parameters.items():
        if name == 'self':
            continue
        if name not in raw_params:
            continue
        value = raw_params[name]
        ann = param.annotation
        try:
            if ann is int or ann == 'int':
                value = int(value)
            elif ann is float or ann == 'float':
                value = float(value)
            elif ann is bool or ann == 'bool':
                value = str(value).lower() in ('true', '1', 'yes')
        except (ValueError, TypeError):
            pass
        kwargs[name] = value
    return kwargs or raw_params


def _to_result_dict(result) -> dict:
    """Convert any return value to a string-keyed dict."""
    if isinstance(result, dict):
        return {k: str(v) for k, v in result.items()}
    if result is None:
        return {}
    return {'result': str(result)}


#                         CONFIG + SERVER LIFECYCLE

def load_config() -> dict:
    config_path = _HERE / "config.yaml"
    if config_path.exists():
        with open(config_path, encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    return {"server": {"host": "0.0.0.0", "port": 50061}, "logging": {"level": "INFO"}}


async def serve(host: str, port: int, config: dict):
    from servicer import InstrumentServicer

    servicer = InstrumentServicer(config)

    # gRPC server
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))

    # TODO: Uncomment and adapt once you generate your .proto stubs:
    # import YourInstrument_pb2_grpc
    # YourInstrument_pb2_grpc.add_YourInstrumentServiceServicer_to_server(servicer, server)

    # SiLA2Common — plug & play support
    adapter = None
    if SILA2_COMMON_AVAILABLE:
        adapter = SiLA2CommonAdapter(servicer, config)
        common_grpc.add_SiLA2ServerInfoServicer_to_server(adapter, server)
        logger.info("SiLA2Common enabled (Plug & Play)")
    else:
        logger.warning("SiLA2Common stubs not found — run regen_stubs.py")

    listen_addr = f'{host}:{port}'
    server.add_insecure_port(listen_addr)
    await server.start()

    # mDNS registration
    mdns_registry = None
    if MDNS_AVAILABLE and XML_PARSER_AVAILABLE:
        try:
            xml_features = features_from_xml_dir(str(_HERE / "features"))
            feature_ids = [f['identifier'] for f in xml_features] + ['SiLA2Common']
        except Exception:
            feature_ids = ['SiLA2Common']

        sila2_cfg = config.get('sila2', {})
        mdns_registry = SiLA2ServerRegistry(
            name=sila2_cfg.get('server_name', 'NewInstrument'),
            port=port,
            features=feature_ids,
            vendor=sila2_cfg.get('vendor', 'BicoccaLab'),
            version=sila2_cfg.get('version', '1.0.0'),
            server_uuid=sila2_cfg.get('server_uuid'),
        )
        await mdns_registry.register()

    sila2_name = config.get('sila2', {}).get('server_name', 'New Instrument')
    print(f"\n{'='*60}")
    print(f"  {sila2_name} — SiLA2 Server")
    print(f"{'='*60}")
    print(f"  gRPC:        {listen_addr}")
    print(f"  mDNS:        {'Registered' if mdns_registry and mdns_registry.is_registered else 'Disabled'}")
    print(f"  Plug & Play: {'Enabled' if adapter else 'Disabled'}")
    print(f"{'='*60}\n")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        if mdns_registry:
            await mdns_registry.unregister()
        await server.stop(grace=5)
        await servicer.close()
        logger.info("Server stopped")


def main():
    parser = argparse.ArgumentParser(description='New Instrument SiLA2 Server')
    parser.add_argument('--port', '-p', type=int, help='Override server port')
    parser.add_argument('--host', '-H', type=str, help='Override server host')
    args = parser.parse_args()

    config = load_config()
    host = args.host or config.get('server', {}).get('host', '0.0.0.0')
    port = args.port or config.get('server', {}).get('port', 50061)

    log_level = config.get('logging', {}).get('level', 'INFO').upper()
    logging.getLogger().setLevel(getattr(logging, log_level, logging.INFO))

    try:
        asyncio.run(serve(host, port, config))
    except KeyboardInterrupt:
        print("\nServer stopped")


if __name__ == "__main__":
    main()
