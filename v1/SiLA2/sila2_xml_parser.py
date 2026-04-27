"""
Parse SiLA2 .sila.xml feature definition files.
No proto dependencies — returns plain Python dicts.
"""

from pathlib import Path
from xml.etree import ElementTree as ET
import logging

_NS = {'s': 'http://www.sila-standard.org'}
_log = logging.getLogger(__name__)


def _text(elem, tag: str) -> str:
    node = elem.find(tag, _NS)
    return (node.text or '').strip() if node is not None else ''


def _parse_param(elem) -> dict:
    dt = elem.find('.//{http://www.sila-standard.org}Basic')
    constraints = [
        v.text or ''
        for v in elem.findall('.//{http://www.sila-standard.org}Value')
    ]
    return {
        'identifier': _text(elem, 's:Identifier'),
        'display_name': _text(elem, 's:DisplayName'),
        'description': _text(elem, 's:Description'),
        'data_type': dt.text if dt is not None else 'String',
        'required': True,
        'constraints': constraints,
    }


def parse_sila_xml(xml_path: str) -> dict:
    """
    Parse a .sila.xml file.

    Returns a dict with keys:
        identifier, display_name, description, version, category,
        commands (list of dicts), properties (list of dicts)
    """
    path = Path(xml_path)
    if not path.exists():
        raise FileNotFoundError(f"SiLA XML not found: {xml_path}")

    root = ET.parse(str(path)).getroot()

    commands = []
    for cmd in root.findall('s:Command', _NS):
        obs = cmd.find('s:Observable', _NS)
        commands.append({
            'identifier': _text(cmd, 's:Identifier'),
            'display_name': _text(cmd, 's:DisplayName'),
            'description': _text(cmd, 's:Description'),
            'observable': (obs.text or '').strip().lower() == 'yes' if obs is not None else False,
            'parameters': [_parse_param(p) for p in cmd.findall('s:Parameter', _NS)],
            'responses': [_parse_param(r) for r in cmd.findall('s:Response', _NS)],
        })

    properties = []
    for prop in root.findall('s:Property', _NS):
        obs = prop.find('s:Observable', _NS)
        dt = prop.find('.//{http://www.sila-standard.org}Basic')
        properties.append({
            'identifier': _text(prop, 's:Identifier'),
            'display_name': _text(prop, 's:DisplayName'),
            'description': _text(prop, 's:Description'),
            'data_type': dt.text if dt is not None else 'String',
            'observable': (obs.text or '').strip().lower() == 'yes' if obs is not None else False,
        })

    return {
        'identifier': _text(root, 's:Identifier'),
        'display_name': _text(root, 's:DisplayName'),
        'description': _text(root, 's:Description'),
        'version': root.get('FeatureVersion', '1.0'),
        'category': root.get('Category', ''),
        'commands': commands,
        'properties': properties,
    }


def features_from_xml_dir(features_dir: str) -> list:
    """Parse all *.sila.xml files in a directory. Returns list of feature dicts."""
    result = []
    for p in sorted(Path(features_dir).glob('*.sila.xml')):
        try:
            result.append(parse_sila_xml(str(p)))
        except Exception as e:
            _log.warning(f"Failed to parse {p.name}: {e}")
    return result


def build_proto_features(xml_features: list, pb2) -> list:
    """
    Convert list of feature dicts (from parse_sila_xml) into proto Feature messages.

    Args:
        xml_features: list of dicts from features_from_xml_dir / parse_sila_xml
        pb2: the SiLA2Common_pb2 module (passed to avoid import dependency)

    Returns:
        list of pb2.Feature instances
    """
    proto_features = []
    for f in xml_features:
        commands = []
        for c in f.get('commands', []):
            params = [
                pb2.Parameter(
                    identifier=p['identifier'],
                    display_name=p['display_name'],
                    description=p['description'],
                    data_type=p.get('data_type', 'String'),
                    required=p.get('required', True),
                    constraints=p.get('constraints', []),
                )
                for p in c.get('parameters', [])
            ]
            commands.append(pb2.Command(
                identifier=c['identifier'],
                display_name=c['display_name'],
                description=c['description'],
                observable=c.get('observable', False),
                parameters=params,
            ))

        properties = []
        for p in f.get('properties', []):
            properties.append(pb2.Property(
                identifier=p['identifier'],
                display_name=p['display_name'],
                description=p['description'],
                data_type=p.get('data_type', 'String'),
                observable=p.get('observable', False),
            ))

        proto_features.append(pb2.Feature(
            identifier=f['identifier'],
            display_name=f['display_name'],
            description=f['description'],
            version=f.get('version', '1.0'),
            category=f.get('category', ''),
            commands=commands,
            properties=properties,
        ))
    return proto_features
