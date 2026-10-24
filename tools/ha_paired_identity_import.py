"""从验真的冷备离线导入原身份及冻结旧子设备；不启动引擎，不写原HA。

输出仅供原生无网络读取器验真，不是可直接启用的HA部署目录。
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re

from ha_binding_freeze import CLOUD_PIDS, read_private
from ha_cold_export import absolute, verify, COMPONENT, RUNTIME
from ha_entity_preflight import core_module, digest
from ha_production_readonly import private_write


def import_identity(cold, frozen, profiles, endpoint_proof, output):
    cold, frozen, profiles, output = map(absolute, (cold, frozen, profiles, output))
    verify(cold)
    manifest = read_private(cold / 'cold-export.json')
    original = Path(manifest['source_config'])
    if (output.exists() or not output.parent.is_dir()
            or any(output.is_relative_to(p) or p.is_relative_to(output) for p in (cold, frozen, profiles, original))):
        raise ValueError('new import directory outside original HA and all inputs required')
    store = core_module('identity_store')
    source = cold / 'payload' / RUNTIME / 'config'
    sdk = cold / 'payload' / COMPONENT / 'hilink_bridge/lib/amd64/libhilink_bridge.so'
    if store.digest(sdk.read_bytes()) != store.SDK_SHA256:
        raise ValueError('legacy SDK version has not passed identity import validation')
    sn_raw, ac = store.private_read(source / 'device_sn'), store.private_read(source / 'device_ac')
    sn = store.legacy_gateway_sn(sn_raw)
    identity = store.decode(store.private_read(source / 'config.cfg'), store.private_read(source / 'bridge.cfg'),
                            ac, manifest['source_mac'])
    freeze_proof = read_private(frozen / 'binding-freeze.json')
    bindings = read_private(frozen / 'bindings.json')
    if (freeze_proof.get('kind') != 'hwhomebridge-offline-binding-freeze'
            or freeze_proof.get('version') != 1 or bindings.get('version') != 1
            or freeze_proof['cold_manifest_sha256'] != store.digest((cold / 'cold-export.json').read_bytes())
            or freeze_proof['entry_id'] != manifest['entry']['entry_id']
            or freeze_proof['bindings_sha256'] != digest(bindings)
            or freeze_proof.get('resident_entity_bindings_confirmed') is not True):
        raise ValueError('verified resident binding freeze for this cold snapshot required')
    records = {row['sn']: row['dev_id'] for row in identity['children']}
    rows = bindings['devices']
    if (not 1 <= len(rows) <= 64 or len(rows) != len(records) or {row['sn'] for row in rows} != set(records)
            or len({row['key'] for row in rows}) != len(rows)):
        raise ValueError('every old child must be preserved exactly once')
    endpoint = read_private(endpoint_proof)
    matches = endpoint['matched_owned_connections']
    ports = {row['port'] for row in matches}
    if (endpoint.get('version') != 1 or endpoint.get('read_only') is not True
            or endpoint['root_id_sha256'] != store.digest(identity['dev_id'].encode())
            or endpoint['cloud_host_sha256'] != store.digest(identity['url'].encode())
            or not matches or len(ports) != 1
            or any(type(row['port']) is not int or not 1 <= row['port'] <= 65535
                   or row['peer'] not in endpoint['resolved_addresses'] or not str(row['inode']).isdigit() for row in matches)):
        raise ValueError('explicit owned connection to the decoded cloud endpoint required')
    port = next(iter(ports))
    # 此SHA固定旧库的GetDevInfo常量已静态核对；不是从实验网关复制身份。
    gateway = {'sn': sn, 'prodId': 'ZG37', 'model': 'DL-01W', 'devTypeId': 'A17',
        'devTypeName': 'Smart Bridge', 'manuId': '002', 'manuName': 'DALEN', 'devName': 'DL-01W', 'protType': 1,
        'ac_key_hex': ac.hex(), 'enable_virtual_child': False, 'enable_ha_children': True,
        'preserve_bound_identity': True, 'expected_cloud_id_sha256': store.digest(identity['dev_id'].encode()),
        'pin_expires_at': 1}
    registry = core_module('product_registry').ProductRegistry()
    config = cold / 'payload' / COMPONENT / 'config'
    if not asyncio.run(registry.load(str(config / 'product_registry.json'), str(config / 'adapters'))):
        raise ValueError('verified original product configuration required')
    catalog, children, profile_proofs = [], {}, {}
    for row in rows:
        if not isinstance(row['key'], str) or not re.fullmatch(r'[a-z0-9-]{1,31}', row['key']):
            raise ValueError('bounded safe child key required')
        if CLOUD_PIDS.get(row['pid']) != row['cloud_pid']:
            raise ValueError('old framework/cloud PID mismatch')
        if row['sn'] != core_module('virtual_device').SNManager.generate_sn(row['source_id']):
            raise ValueError('original deterministic child SN mismatch')
        product = registry.get_product(row['pid'])
        adapter = None
        if row.get('adapter'):
            choices = [a for a in registry.get_all_adapters() if a.pid == row['pid'] and a.services == row['adapter']]
            if len(choices) != 1:
                raise ValueError('original vendor adapter changed')
            adapter = choices[0]
        product = registry.merge(product, adapter)
        if core_module('product_registry').product_fingerprint(product) != row['product_sha256']:
            raise ValueError('frozen product mapping changed')
        device = core_module('virtual_device').VirtualDevice(row['sn'], product, row['source_id'])
        for sid, entity in row['entities'].items():
            mapping = product.services[sid].ha_mapping
            device.add_service_entry(sid, core_module('virtual_device').ServiceEntry(entity, mapping.domain, sid))
        profile_path = profiles / (row['cloud_pid'] + '-profile.json')
        if profile_path.is_symlink() or not profile_path.is_file() or profile_path.stat().st_size > 131072:
            raise ValueError('bounded regular profile required')
        raw_profile = profile_path.read_bytes()
        profile = json.loads(raw_profile)
        if profile['prodId'] != row['cloud_pid'] or profile['protocolType'] != 'WiFi':
            raise ValueError('expected old Huawei product profile required')
        bridge = core_module('profile_bridge').ProfileBridge(None, device, profile)
        catalog.append({'key': row['key'], 'framework_pid': row['pid'], 'pid': row['cloud_pid'],
            'sdk_index': int(row['pid']) - 1, 'sn': row['sn'], 'existing_cloud_only': True,
            'devType': profile['deviceTypeId'], 'manu': profile['manufacturerId'],
            'model': profile['deviceModel'], 'protType': 1, 'services': bridge.services})
        children['ha-child-' + row['key'] + '.json'] = {'version': 1, 'parent_sn': sn, 'sn': row['sn'],
            'udid': store.digest(('OpenHarmonyHA' + row['key'] + row['sn']).encode()),
            'devId': records[row['sn']], 'on': 0}
        profile_proofs[row['cloud_pid']] = store.digest(raw_profile)
    stores = store.build_store(identity, gateway, port)
    receipt = {'version': 2, 'children': [{'key': row['key'], 'sn_sha256': store.digest(row['sn'].encode()),
        'id_sha256': store.digest(records[row['sn']].encode())} for row in rows], 'new_child_keys': [],
        'legacy_sdk_sha256': store.SDK_SHA256, 'source_sn_sha256': store.digest(sn_raw),
        'canonical_sn_sha256': store.digest(sn.encode()), 'cloud_port': port,
        'root_id_sha256': store.digest(identity['dev_id'].encode()),
        'account_field_sha256': {key: store.digest(identity[key] if isinstance(identity[key], bytes) else identity[key].encode())
            for key in ('secret', 'psk', 'uid_hash', 'auth_id', 'auth_code')},
        'target_catalog_sha256': store.digest(json.dumps(catalog, ensure_ascii=False, indent=2).encode()),
        'target_sha256': {name: store.digest(data) for name, data in stores.items()},
        'cold_manifest_sha256': freeze_proof['cold_manifest_sha256'], 'binding_freeze_sha256': digest(freeze_proof),
        'bindings_sha256': digest(bindings), 'profile_sha256': profile_proofs,
        'offline_only': True, 'live_takeover_verified': False, 'existing_child_metadata_sync': False,
        'unavailable_entities_retained': freeze_proof['unavailable_entities_retained'], 'adoption_ready': False}
    output.mkdir(mode=0o700)
    for name, data in stores.items():
        private_write(output / 'sdk-state' / name, data)
    for name, value in {**children, 'gateway.local.json': gateway, 'ha-children.local.json': catalog,
                        'import-receipt.json': receipt}.items():
        private_write(output / name, json.dumps(value, ensure_ascii=False, indent=2).encode())
    return receipt


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cold', 'frozen', 'profiles', 'endpoint-proof', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        result = import_identity(args.cold, args.frozen, args.profiles, args.endpoint_proof, args.output)
        print(json.dumps({'offline_identity_imported': True, 'old_children': len(result['children']),
                          'original_HA_written': False, 'SDK_started': False, 'adoption_ready': False}))
    except Exception:
        print(json.dumps({'passed': False, 'error': 'offline identity import failed; original HA was not changed'}))
        raise SystemExit(1) from None
