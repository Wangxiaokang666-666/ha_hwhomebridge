"""M3 专用新身份夹具：旧版组件 + 新网关离线文件；从不读取生产配置。"""
import argparse
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import uuid

from prepare_coexistence_fixture import create, ROOT

OLD_REVISION = '8af2809'


def prepare(destination, binary, assets=None):
    create(destination, binary, 'x86_64', real_legacy=True, assets=assets)
    config = destination / 'config'
    component = config / 'custom_components/hwhomebridge'
    shutil.rmtree(component)
    archive = subprocess.check_output(['git', '-C', str(ROOT.parents[1]), 'archive', OLD_REVISION,
                                       'custom_components/hwhomebridge'])
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(config, filter='data')
    if json.loads((component / 'manifest.json').read_text())['version'] != '1.0.0':
        raise ValueError('original component baseline changed')
    (component / 'config/.m3-preserve-canary').write_text('target adapter canary')
    (component / 'profile').mkdir()
    (component / 'profile/m3-private-canary.json').write_text('{"private_test_canary":true}')
    legacy_config = component / 'hilink_bridge/config/hilink.cfg'
    legacy_config.write_text(legacy_config.read_text() + '\n')
    entry = {'entry_id': uuid.uuid4().hex, 'version': 1, 'minor_version': 1, 'domain': 'hwhomebridge',
             'title': 'M3 isolated old entry', 'source': 'user', 'data': {}, 'options': {},
             'unique_id': 'hwhomebridge', 'disabled_by': None, 'pref_disable_new_entities': False,
             'pref_disable_polling': False, 'subentries': [], 'discovery_keys': {},
             'created_at': '2026-10-16T00:00:00+00:00', 'modified_at': '2026-10-16T00:00:00+00:00'}
    (config / '.storage/core.config_entries').write_text(json.dumps({
        'version': 1, 'minor_version': 5, 'key': 'core.config_entries', 'data': {'entries': [entry]}}))
    runtime = config / '.storage/hwhomebridge/config'
    runtime.mkdir()
    (runtime / 'm3-runtime-canary').write_text('untouched runtime marker')
    (config / '.HA_VERSION').write_text('2026.10.1')
    for name in ('ha_upgrade.py', 'ha_runtime_preflight.py', 'test_ha_upgrade.py'):
        shutil.copyfile(ROOT.parents[1] / 'tools' / name, destination / 'tests' / name)
    shutil.copyfile(ROOT / 'ha_m3_install_test.py', destination / 'tests/ha_m3_install_test.py')
    for path in destination.rglob('*'):
        executable = path.name in ('bridge_poc', 'coap_alignment_test', 'ha_child_state_test',
                                   'virtual_child_state_test', 'speke_vectors')
        path.chmod(0o700 if path.is_dir() or executable else 0o600)
    return {'fresh_unpaired_fixture': True, 'old_component_revision': OLD_REVISION,
            'old_component_version': '1.0.0', 'production_identity_copied': False}


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--assets', type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.destination, args.binary, args.assets)))
