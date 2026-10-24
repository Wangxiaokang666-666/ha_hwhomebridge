"""旧 SDK 测试身份的离线转换；只接受已验证版本，不启动网络或修改源文件。"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re

from single_engine_fixture import load, private_root

_store = load('identity_store')
SDK_SHA256 = _store.SDK_SHA256
digest, private_read, decode = _store.digest, _store.private_read, _store.decode
hkdf, build_store = _store.hkdf, _store.build_store


def prepare(root, output, mac, port, sdk, *, include_new_children=False):
    private_root(root)
    with (root / 'engine-owner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return prepare_locked(root, output, mac, port, sdk, include_new_children=include_new_children)


def prepare_locked(root, output, mac, port, sdk, *, include_new_children=False):
    for directory in (root / 'legacy', root / 'runtime'):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError('owned test input directories required')
    if digest(sdk.read_bytes()) != SDK_SHA256:
        raise ValueError('legacy SDK version has not been verified')
    gateway = json.loads(private_read(root / 'gateway.local.json'))
    ac = private_read(root / 'legacy/device_ac')
    if not gateway['sn'].startswith('OHPOC-') or private_read(root / 'legacy/device_sn') != gateway['sn'].encode() + b'\0':
        raise ValueError('independent test identity required')
    if ac.hex() != gateway['ac_key_hex']:
        raise ValueError('test AC mismatch')
    sources = {name: private_read(root / 'legacy' / name) for name in ('config.cfg', 'bridge.cfg')}
    identity = decode(sources['config.cfg'], sources['bridge.cfg'], ac, mac)
    catalog = json.loads(private_read(root / 'runtime/ha-children.local.json'))
    from legacy_category_fixture import CLOUD_PIDS
    new_catalog = [row for row in catalog if row.get('pid') not in CLOUD_PIDS.values()] if include_new_children else []
    if len(catalog) > 64 or any(not re.fullmatch(r'[a-z0-9-]{1,31}', row['key']) for row in catalog):
        raise ValueError('bounded safe catalog required')
    if (len({row['key'] for row in catalog}) != len(catalog)
            or len({row['sn'] for row in catalog}) != len(catalog)):
        raise ValueError('old and new children must have unique keys and SNs')
    catalog = [row for row in catalog if row.get('pid') in CLOUD_PIDS.values()]
    records = {row['sn']: row for row in identity['children']}
    if (len(catalog) != len(records) or {row['sn'] for row in catalog} != set(records)
            or len({row['key'] for row in catalog}) != len(catalog)):
        raise ValueError('catalog must cover every old child exactly once')
    for row in catalog:
        if (not re.fullmatch(r'[a-z0-9-]{1,31}', row['key'])
                or CLOUD_PIDS.get(row.get('framework_pid')) != row['pid']
                or row.get('sdk_index') != int(row['framework_pid']) - 1):
            raise ValueError('verified old product catalog required')
    # 创建新目录，绝不覆盖旧 KV 或已有 OpenHarmony 配对存储。
    output.mkdir(mode=0o700)
    state = output / 'sdk-state'
    state.mkdir(mode=0o700)
    for name, data in build_store(identity, gateway, port).items():
        (state / name).write_bytes(data)
        (state / name).chmod(0o600)
    imported_catalog, child_receipts = [], []
    for child in catalog:
        record = records[child['sn']]
        imported_catalog.append({**child, 'existing_cloud_only': True})
        child_state = {'version': 1, 'parent_sn': gateway['sn'], 'sn': child['sn'],
                       'udid': digest(('OpenHarmonyHA' + child['key'] + child['sn']).encode()),
                       'devId': record['dev_id'], 'on': 0}
        path = output / ('ha-child-' + child['key'] + '.json')
        path.write_text(json.dumps(child_state, indent=2))
        path.chmod(0o600)
        child_receipts.append({'key': child['key'], 'id_sha256': digest(record['dev_id'].encode()),
                               'sn_sha256': digest(child['sn'].encode())})
    imported_catalog.extend(new_catalog)
    gateway = {**gateway, 'enable_virtual_child': False}
    for name, value in [('ha-children.local.json', imported_catalog), ('gateway.local.json', gateway)]:
        (output / name).write_text(json.dumps(value, indent=2))
        (output / name).chmod(0o600)
    receipt = {'version': 2, 'children': child_receipts, 'new_child_keys': [row['key'] for row in new_catalog],
               'source_catalog_sha256': digest(private_read(root / 'runtime/ha-children.local.json')),
               'target_catalog_sha256': digest(private_read(output / 'ha-children.local.json')), 'offline_only': True, 'live_takeover_verified': False,
               'source_mac_required': True, 'cloud_port': port, 'existing_child_metadata_sync': False,
               'legacy_sdk_sha256': SDK_SHA256,
               'source_sha256': {name: digest(data) for name, data in sources.items()},
               'root_id_sha256': digest(identity['dev_id'].encode()),
               'child_id_sha256': digest(identity['child_id'].encode()),
               'child_sn_sha256': digest(identity['child_sn'].encode()),
               'account_field_sha256': {name: digest(identity[name] if isinstance(identity[name], bytes)
                   else identity[name].encode()) for name in ('secret', 'psk', 'uid_hash', 'auth_id', 'auth_code')},
               'target_sha256': {name: digest((state / name).read_bytes()) for name in ('dev_data', 'conn_data')}}
    (output / 'import-receipt.json').write_text(json.dumps(receipt, indent=2))
    (output / 'import-receipt.json').chmod(0o600)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--legacy-mac', required=True)
    parser.add_argument('--cloud-port', type=int, required=True)
    parser.add_argument('--legacy-sdk', type=Path, required=True)
    parser.add_argument('--include-new-children', action='store_true', help='also register the explicitly configured virtual fan supplements under the same parent')
    args = parser.parse_args()
    os.umask(0o077)
    prepare(args.root, args.output, args.legacy_mac, args.cloud_port, args.legacy_sdk, include_new_children=args.include_new_children)
    print(json.dumps({'offline_conversion': True, 'source_unchanged': True, 'live_takeover_verified': False}))
