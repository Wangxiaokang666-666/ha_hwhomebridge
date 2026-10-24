"""同一网关的离线接管合同；校验身份和文件，不启动SDK。"""
import json
import os
from pathlib import Path
import platform
import re
import struct
import socket
import subprocess

from .identity_store import SDK_SHA256, decode, digest, private_read, legacy_gateway_sn
from .openharmony.deployment import private_path


class GatewayDeployment:
    def __init__(self, config_dir, relative, unique_id):
        if not isinstance(relative, str):
            raise ValueError('private managed gateway directory required')
        base = Path(config_dir).resolve() / '.storage/hwhomebridge/gateways'
        self.directory = Path(config_dir).resolve() / relative
        if (Path(relative).is_absolute() or '..' in Path(relative).parts
                or self.directory.resolve() != self.directory or not self.directory.is_relative_to(base)
                or self.directory == base):
            raise ValueError('private managed gateway directory required')
        private_path(self.directory, True)
        self.settings = self.read('deployment.json')
        if self.settings.get('version') != 1 or self.settings['entry_unique_id'] != unique_id:
            raise ValueError('existing configuration entry must match adoption manifest')
        self.gateway = self.directory / 'gateway.local.json'
        self.root = self.read('gateway.local.json')
        self.receipt = self.read('runtime/import-receipt.json')
        self.catalog = self.read('runtime/ha-children.local.json')
        self.bindings = self.read('bindings.json')
        if self.receipt.get('version') != 2 or not self.receipt['children']:
            raise ValueError('verified multi-child identity import required')
        self.identity = self.receipt['root_id_sha256']
        if self.settings['root_id_sha256'] != self.identity:
            raise ValueError('gateway identity contract mismatch')
        if self.root.get('preserve_bound_identity') is not True or self.root.get('expected_cloud_id_sha256') != self.identity:
            raise ValueError('native engine must require the existing bound identity')
        self.runtime = self.directory / 'runtime'
        self.port = self.settings['port']
        self.lan_port = self.settings['lan_port']
        if any(type(v) is not int or not 1024 <= v <= 65535 for v in (self.port, self.lan_port)):
            raise ValueError('invalid gateway ports')
        self.interface = self.settings['interface']
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', self.interface):
            raise ValueError('invalid interface')
        self.binary = self.directory / 'bridge_poc'
        self.probe = self.directory / 'identity_state_probe'
        self.legacy = self.directory / 'legacy'
        self.legacy_library = Path(__file__).parent / 'hilink_bridge/lib' / {
            'x86_64': 'amd64', 'aarch64': 'aarch64'}.get(platform.machine(), 'unsupported') / 'libhilink_bridge.so'
        ca = Path(self.root['ca_file'])
        if not ca.is_relative_to(self.directory) or ca.resolve() != ca:
            raise ValueError('owned local CA required')
        private_path(ca)
        if (self.root.get('enable_virtual_child') is not False or self.root.get('enable_ha_children') is not True
                or self.root.get('prodId') != 'ZG37'):
            raise ValueError('one existing parent, no standalone virtual root/child')
        self.verify_source()
        self.verify_catalog()
        self.verify_inputs()

    def verify_inputs(self):
        expected = self.settings['input_sha256']
        paths = {'bindings.json', 'instances.json', 'gateway.local.json'}
        paths.update(str(path.relative_to(self.directory)) for path in (self.directory / 'mapping').rglob('*.json'))
        paths.update('profiles/' + row['pid'] + '.json' for row in self.catalog)
        if set(expected) != paths:
            raise ValueError('complete frozen mappings and profiles required')
        for name in paths:
            if '..' in Path(name).parts or digest(private_read(self.directory / name)) != expected[name]:
                raise ValueError('gateway mapping/profile changed; offline preflight required')

    def read(self, name):
        path = self.directory / name
        for parent in path.parents:
            if parent == self.directory.parent:
                break
            private_path(parent, True)
        return json.loads(private_read(path))

    def verify_source(self):
        ac = private_read(self.legacy / 'device_ac')
        raw_sn = private_read(self.legacy / 'device_sn')
        sn = legacy_gateway_sn(raw_sn)
        if (ac.hex() != self.root['ac_key_hex']
                or sn != self.root['sn']):
            raise ValueError('legacy and target SN/AC differ')
        source_sn = self.receipt.get('source_sn_sha256')
        if ((raw_sn.endswith(b'\n') and (source_sn is None or self.receipt.get('legacy_sdk_sha256') != SDK_SHA256))
                or source_sn is not None and source_sn != digest(raw_sn)):
            raise ValueError('original SN bytes changed or LF import proof missing')
        identity = decode(private_read(self.legacy / 'config.cfg'), private_read(self.legacy / 'bridge.cfg'),
                          ac, self.settings['source_mac'])
        fields = {key: digest(identity[key] if isinstance(identity[key], bytes) else identity[key].encode())
                  for key in ('secret', 'psk', 'uid_hash', 'auth_id', 'auth_code')}
        if digest(identity['dev_id'].encode()) != self.identity or fields != self.receipt['account_field_sha256']:
            raise ValueError('legacy paired identity changed; new offline preflight required')
        expected = {(row['sn_sha256'], row['id_sha256']) for row in self.receipt['children']}
        actual = {(digest(row['sn'].encode()), digest(row['dev_id'].encode())) for row in identity['children']}
        if actual != expected:
            raise ValueError('old child identity inventory differs')

    def verify_catalog(self):
        if digest(private_read(self.runtime / 'ha-children.local.json')) != self.receipt['target_catalog_sha256']:
            raise ValueError('adopted catalog changed')
        keys = {row['key'] for row in self.catalog}
        if not 1 <= len(self.catalog) <= 64 or len(keys) != len(self.catalog):
            raise ValueError('unique bounded child catalog required')
        for row in self.catalog:
            if not re.fullmatch(r'[a-z0-9-]{1,31}', row['key']):
                raise ValueError('invalid child key')
        old = {row['key']: row for row in self.receipt['children']}
        if keys != set(old) | set(self.receipt.get('new_child_keys', [])):
            raise ValueError('explicit old and new child inventory required')
        for row in self.catalog:
            if row['key'] not in old:
                continue
            child = self.read('runtime/ha-child-' + row['key'] + '.json')
            if (row.get('existing_cloud_only') is not True or row['sn'] != child['sn']
                    or child['parent_sn'] != self.root['sn']
                    or digest(child['sn'].encode()) != old[row['key']]['sn_sha256']
                    or digest(child['devId'].encode()) != old[row['key']]['id_sha256']):
                raise ValueError('existing child identity mismatch')

    def verify_engine(self, engine):
        if engine not in ('legacy', 'openharmony'):
            raise ValueError('unknown managed engine')
        if self.settings.get('offline_preparation') is True:
            raise ValueError('offline production preparation cannot start an engine; exclusive handover required')
        if self.interface not in {name for _, name in socket.if_nameindex()}:
            raise ValueError('configured network interface is missing')
        self.verify_source()
        self.verify_catalog()
        self.verify_inputs()
        if engine == 'legacy':
            if digest(self.legacy_library.read_bytes()) != SDK_SHA256:
                raise ValueError('legacy SDK architecture/version has not passed adoption validation')
            mac = Path('/sys/class/net') / self.interface / 'address'
            if mac.read_text().strip().lower() != self.settings['source_mac'].lower():
                raise ValueError('legacy rollback requires the original source network interface MAC')
            return
        if self.settings.get('availability_protocol') != 1:
            raise ValueError('managed OpenHarmony directory requires availability protocol 1; rebuild offline before switching')
        for path, name in [(self.binary, 'binary_sha256'), (self.probe, 'probe_sha256')]:
            private_path(path)
            if not os.access(path, os.X_OK) or digest(path.read_bytes()) != self.settings[name]:
                raise ValueError('native executable missing or checksum mismatch')
            header = path.read_bytes()[:64]
            if (len(header) != 64 or header[:6] != b'\x7fELF\x02\x01'
                    or struct.unpack_from('<H', header, 18)[0] != {'x86_64':62, 'aarch64':183}.get(platform.machine())):
                raise ValueError('native executable architecture mismatch')
        try:
            result = subprocess.run([str(self.probe), str(self.runtime / 'sdk-state'), str(self.gateway)],
                                    capture_output=True, text=True, check=True, timeout=15)
        except (subprocess.SubprocessError, OSError) as error:
            raise ValueError('native identity probe failed') from error
        values = [json.loads(line[15:]) for line in result.stdout.splitlines() if line.startswith('IDENTITY_PROBE ')]
        if (len(values) != 1 or not values[0]['bound_identity_loaded']
                or values[0]['dev_id_sha256'] != self.identity
                or values[0]['fields'] != self.receipt['account_field_sha256']):
            raise ValueError('native reader did not confirm existing bound identity')


def load_gateway(config_dir, relative, unique_id):
    return GatewayDeployment(config_dir, relative, unique_id)
