"""生成独立HA验收目录；只接受虚拟实验冷副本，不接触家庭HA。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil

from single_engine_fixture import COMPONENT, load, private_root, save


def prepare(config, source, native):
    private_root(source)
    if config.exists():
        raise ValueError('new independent HA config directory required')
    config.mkdir(mode=0o700)
    directory = config / '.storage/hwhomebridge/gateways/current'
    directory.mkdir(mode=0o700, parents=True)
    verified = source / 'arm-verified-runtime'
    shutil.copytree(verified, directory / 'runtime')
    shutil.copytree(source / 'legacy', directory / 'legacy')
    shutil.copytree(source / 'mapping', directory / 'mapping')
    shutil.copyfile(source / 'instances.json', directory / 'instances.json')
    shutil.copyfile(source / 'ca.pem', directory / 'ca.pem')
    shutil.copyfile(source / 'virtual-device-state.private.json', config / 'fixture-states.private.json')
    root = json.loads((verified / 'gateway.local.json').read_text())
    receipt = json.loads((verified / 'import-receipt.json').read_text())
    root.update(ca_file='/config/.storage/hwhomebridge/gateways/current/ca.pem', preserve_bound_identity=True,
                expected_cloud_id_sha256=receipt['root_id_sha256'], pin_expires_at=1)
    save(directory / 'gateway.local.json', root)
    for name in ('bridge_poc', 'identity_state_probe'):
        shutil.copyfile(native / name, directory / name)
    profiles = directory / 'profiles';profiles.mkdir(mode=0o700)
    hashes = {}
    for row in json.loads((directory / 'runtime/ha-children.local.json').read_text()):
        pid = row['pid'];path = Path(__file__).parent / 'evidence' / (pid + '-profile.json')
        shutil.copyfile(path, profiles / (pid + '.json'));hashes[pid] = hashlib.sha256(path.read_bytes()).hexdigest()
    inventory = json.loads((source / 'legacy-inventory.json').read_text())
    instance = json.loads((source / 'instances.json').read_text())
    catalog = json.loads((directory / 'runtime/ha-children.local.json').read_text())
    rows = [{'key': 'legacy-light', 'pid': '002', 'source_id': instance['devices'][0]['id'],
             'entities': {sid: instance['devices'][0]['roles']['main'] for sid in ('switch','brightness','cct')}}] + inventory
    registry = load('product_registry').ProductRegistry()
    asyncio.run(registry.load(str(COMPONENT / 'config/product_registry.json'), str(COMPONENT / 'config/adapters')))
    for row in rows:
        row.pop('states', None)
        cloud = next(item for item in catalog if item['key'] == row['key'])
        row.update(sn=cloud['sn'], cloud_pid=cloud['pid'])
        if row['pid'] == '006':
            row['adapter'] = next(item for item in registry.get_all_adapters() if item.pid == '006').services
    save(directory / 'bindings.json', {'version': 1, 'devices': rows})
    inputs = [directory / name for name in ('bindings.json', 'instances.json', 'gateway.local.json')]
    inputs.extend((directory / 'mapping').rglob('*.json'))
    inputs.extend(profiles.glob('*.json'))
    save(directory / 'deployment.json', {'version': 1, 'entry_unique_id': 'hwhomebridge',
        'input_sha256': {str(path.relative_to(directory)):hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs},
        'root_id_sha256': receipt['root_id_sha256'], 'source_mac': '52:55:55:60:3b:0e',
        'availability_protocol': 1, 'interface': 'eth0', 'port': 18093, 'lan_port': 15686, 'startup_timeout': 60, 'profiles': hashes,
        'binary_sha256': hashlib.sha256((native / 'bridge_poc').read_bytes()).hexdigest(),
        'probe_sha256': hashlib.sha256((native / 'identity_state_probe').read_bytes()).hexdigest()})
    for p in config.rglob('*'):
        p.chmod(0o700 if p.is_dir() or p.name in ('bridge_poc','identity_state_probe') else 0o600)
    save(config / 'fixture-scope.json', {'purpose': 'existing-gateway-ha-lifecycle-virtual-only', 'production_access': False})
    print(json.dumps({'prepared': True, 'existing_parent': True, 'virtual_old_children':8, 'virtual_supplements':7}))


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('config',type=Path);p.add_argument('source',type=Path);p.add_argument('native',type=Path)
    a=p.parse_args();prepare(a.config,a.source,a.native)
