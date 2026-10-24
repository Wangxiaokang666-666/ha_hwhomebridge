"""目标 HA Python 中的只读预检；不启动 C、不调用服务、不输出身份。"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import struct
import sys


def offline(config=Path('/config')):
    from homeassistant.const import __version__
    paths = [config / 'custom_components/hwhomebridge', config / '.storage/core.config_entries']
    paths += [p for p in paths[0].rglob('*') if p.is_file()]
    return {'ha_version': __version__, 'architecture': platform.machine(), 'runtime_uid': os.getuid(),
            'python_version': platform.python_version(),
            'owned_readable_config': all(p.stat().st_uid == os.getuid() and os.access(p, os.R_OK) for p in paths)}


def activation(config, relative, states):
    """须在目标网络命名空间、目标 HA UID 下调用；states 来自当前 hass.states。"""
    sys.path.insert(0, str(config))
    from custom_components.hwhomebridge.openharmony.deployment import load_deployment, private_path
    spec = load_deployment(config, relative)
    for path in spec.directory.rglob('*'):
        private_path(path, path.is_dir())
    if spec.interface not in dict((name, index) for index, name in socket.if_nameindex()):
        raise ValueError('configured interface missing')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        address = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack('256s', spec.interface.encode()))[20:24]
        address = socket.inet_ntoa(address)
        if address.startswith('127.'):
            raise ValueError('LAN interface must have a non-loopback IPv4 address')
        probe.bind((address, spec.lan_port))
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', spec.port))
    roles = {entity for bindings in spec.router.roles.values() for entity in bindings.values()}
    if any(entity not in states or states[entity] in ('unavailable', 'unknown') for entity in roles):
        raise ValueError('bound entities missing or unavailable')
    binary = spec.binary.read_bytes()
    machine = int.from_bytes(binary[18:20], 'little')
    if binary[:6] != b'\x7fELF\x02\x01' or machine != {'x86_64': 62, 'aarch64': 183}[platform.machine()]:
        raise ValueError('instance executable architecture mismatch')
    return {'passed': True, 'profile_and_service_catalog_verified': True, 'private_paths_verified': True,
            'interface_present': True, 'ports_free': True, 'bound_entities': len(roles),
            'binary_sha256': hashlib.sha256(binary).hexdigest(), 'write_policy': spec.write_policy,
            'identity_handover_verified': False, 'C_started': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offline', action='store_true', required=True)
    parser.parse_args()
    print('HA_RUNTIME_PREFLIGHT ' + json.dumps(offline()))
