"""从实际升级包建立全新、未上线的 App 夹具；不复制旧配对或 HA 凭据。"""
import argparse
import asyncio
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from ha_configflow_fixture import prepare
from framework_loader import load_framework

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
import ha_upgrade


def create(destination, package, checksum, entry_template):
    if destination.exists():
        raise ValueError('a new private fixture directory is required')
    _, payload = ha_upgrade.load_package(package, checksum)
    os.umask(0o077)
    config = destination / 'config'
    component = config / ha_upgrade.COMPONENT
    component.mkdir(parents=True, mode=0o700)
    # 本地测试沿用原项目安装所需的公共配置/既有 SDK；它们不分发到升级包。
    tracked = subprocess.check_output(['git', '-C', str(ROOT), 'ls-files', '-z', str(ha_upgrade.COMPONENT)], text=True)
    for name in filter(None, tracked.split('\0')):
        relative = Path(name).relative_to(ha_upgrade.COMPONENT)
        if ha_upgrade.preserved(relative.as_posix()) and relative.parts[0] != 'profile':
            target = component / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
    for name, data in payload.items():
        path = component / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    native = component / 'openharmony/bin/aarch64/bridge_poc'
    prepare(config, Path(__file__).parent, native)
    directory = config / '.storage/hwhomebridge/openharmony/lab'
    root_path = directory / 'gateway.local.json'
    root = json.loads(root_path.read_text())
    root.update(devName='TEST-B9-FOURKEY', pin_expires_at=1)
    root_path.write_text(json.dumps(root))
    instance_path = directory / 'instances.json'
    instance = json.loads(instance_path.read_text())
    source = instance['devices'][0]
    source.update(id='b9-virtual-' + uuid.uuid4().hex, name='TEST-B9 virtual fan', layout='switches',
                  cards=['fan-controls-0', 'fan-controls-1'])
    source['roles'] = {role: entity.split('.')[0] + '.oh_b9_' + role for role, entity in source['roles'].items()}
    instance_path.write_text(json.dumps(instance))
    router = asyncio.run(load_framework().OpenHarmonyRouter.create(
        load_framework().transport.ScopedTransport(), component / 'openharmony/config', instance))
    catalog = router.registry.catalog(router.devices)
    (directory / 'runtime/ha-children.local.json').write_text(json.dumps(catalog))
    assert {row['key'] for row in catalog} == {'fan-controls-0', 'fan-controls-1'}
    settings_path = directory / 'deployment.json'
    settings = json.loads(settings_path.read_text())
    settings.update(lan_port=5683, write_policy='fan_only')
    settings_path.write_text(json.dumps(settings))
    storage = json.loads(entry_template.read_text())
    previous = next(entry for entry in storage['data']['entries'] if entry['domain'] == 'hwhomebridge')
    entry = copy.deepcopy(previous)
    entry.update(entry_id=uuid.uuid4().hex, unique_id='hwhomebridge:openharmony:' + hashlib.sha256(root['sn'].encode()).hexdigest(),
                 title='TEST-B9-FOURKEY', disabled_by=None,
                 data={'backend_type': 'openharmony', 'backend': 'openharmony',
                       'openharmony_directory': '.storage/hwhomebridge/openharmony/lab'},
                 options={'backend': 'disabled'}, created_at=datetime.now(timezone.utc).isoformat(),
                 modified_at=datetime.now(timezone.utc).isoformat())
    storage['data']['entries'] = [entry]
    (config / '.storage/core.config_entries').write_text(json.dumps(storage))
    (config / 'fixture-scope.json').write_text(json.dumps({
        'purpose': 'b9-four-key-app-virtual-only', 'production_access': False}))
    (config / '.HA_VERSION').write_text(ha_upgrade.TESTED_HA)
    shutil.copyfile(Path(__file__).with_name('four_key_app_acceptance.py'), config / 'four_key_app_acceptance.py')
    for path in destination.rglob('*'):
        path.chmod(0o700 if path.is_dir() or path.name in ('bridge_poc', 'identity_state_probe') else 0o600)
    return {'fresh_unpaired_identity': True, 'selected_backend': 'disabled', 'HA_child_cards': 2,
            'built_in_virtual_probe_enabled': True,
            'package_sha256': checksum, 'production_data_copied': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--entry-template', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(create(args.destination, args.package, args.sha256, args.entry_template)))
