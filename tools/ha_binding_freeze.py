"""从已加载旧路由的双遍诊断冻结绑定；不控制设备，不改原HA或配对资料。"""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import urllib.request

from ha_cold_export import absolute, verify, COMPONENT
from ha_entity_preflight import HAReader, capture, core_module, digest, registry_proof
from ha_production_readonly import NoRedirect, private_write

CLOUD_PIDS = {'001': '9MTL', '002': '9MU7', '003': '9MTJ', '004': '9MTM',
              '005': '9MTR', '006': '9MTV', '007': '9MTW', '008': '9MTX'}


def read_private(path):
    return json.loads(core_module('identity_store').private_read(absolute(path)))


def observed_devices(observation, entry_id):
    if (observation.get('version') != 1 or observation.get('kind') != 'hwhomebridge-resident-binding-observation'
            or observation.get('entry_id') != entry_id):
        raise ValueError('observation must identify the original entry')
    data = observation['diagnostic']
    if (data.get('selected_backend') != 'legacy' or data.get('active_backend') != 'legacy'
            or data.get('binding_source') != 'resident_legacy_router'
            or data.get('runtime_bindings_available') is not True
            or data.get('legacy_cloud_online_callback') is not True
            or type(data.get('HA_process_pid')) is not int or data['HA_process_pid'] <= 0):
        raise ValueError('loaded online resident legacy router required')
    rows = data['devices']
    if not isinstance(rows, list) or not rows or len(rows) > 64:
        raise ValueError('bounded resident device inventory required')
    devices = {row['source_device_id']: row for row in rows}
    if len(devices) != len(rows):
        raise ValueError('duplicate source in resident bindings')
    return data, devices


async def freeze(cold, first, second, output):
    cold, output = absolute(cold), absolute(output)
    verify(cold)
    if output.exists() or output.is_relative_to(cold) or cold.is_relative_to(output):
        raise ValueError('new private output outside cold export required')
    manifest = read_private(cold / 'cold-export.json')
    original = Path(manifest['source_config'])
    if output.is_relative_to(original) or original.is_relative_to(output):
        raise ValueError('binding output must stay outside the original HA configuration')
    if (manifest['identity'].get('child_inventory_matches_live') is not True
            or manifest['identity'].get('excluded_devices_present') is not False):
        raise ValueError('complete original child inventory without unresolved exclusions required')
    entry_id = manifest['entry']['entry_id']
    one, observed = observed_devices(first, entry_id)
    two, repeated = observed_devices(second, entry_id)
    elapsed = (datetime.fromisoformat(two['observed_at_utc']) - datetime.fromisoformat(one['observed_at_utc'])).total_seconds()
    if observed != repeated or one['HA_process_pid'] != two['HA_process_pid'] or not 0 <= elapsed <= 60:
        raise ValueError('two stable observations in one HA process required')
    report = manifest['entity_preflight']
    if any(observation.get('registry_sha256') != report['registry_sha256'] for observation in (first, second)):
        raise ValueError('resident observations must include the unchanged original registry proof')
    sources = {row['id']: row for row in report['registry_proof']['devices'] if row['id'] in report['source_ids']}
    inventory = {row['source_id']: row for row in report['legacy_inventory']}
    previous_bindings = {row['source_id']: row for row in report['legacy_bindings']}
    if set(observed) != set(inventory):
        raise ValueError('resident inventory differs from every original paired child')
    entity_rows = {row['entity_id']: row for row in report['registry_proof']['entities']}
    registry = core_module('product_registry').ProductRegistry()
    config = cold / 'payload' / COMPONENT / 'config'
    if not await registry.load(str(config / 'product_registry.json'), str(config / 'adapters')):
        raise ValueError('verified original product configuration required')
    matcher_module = core_module('product_matcher')
    matcher = matcher_module.ProductMatcher(registry)
    rows = []
    for source_id, actual in sorted(observed.items()):
        source = sources[source_id]
        bridge = next(row for row in report['registry_proof']['devices']
                      if row['id'] == inventory[source_id]['bridge_device_id'])
        if source.get('disabled_by') is not None or bridge.get('disabled_by') is not None:
            raise ValueError('disabled devices require an explicit preservation plan')
        records = [row for row in entity_rows.values() if row.get('device_id') == source_id
                   and row.get('platform') != 'huawei_smarthome']
        info = matcher_module.DeviceInfo(device_id=source_id, name=source.get('name_by_user') or source.get('name') or '',
            model=source.get('model') or '', manufacturer=source.get('manufacturer') or '',
            entity_domains=sorted({row['entity_id'].split('.')[0] for row in records}))
        product = matcher.match(info)
        if product is None or product.pid != actual['framework_pid'] or product.pid not in CLOUD_PIDS:
            raise ValueError('resident product differs from verified original configuration')
        mapped = actual['services']
        if (not isinstance(mapped, dict) or not mapped or set(mapped) - set(product.services)
                or set(mapped) != set(previous_bindings[source_id]['entities'])):
            raise ValueError('unknown or empty resident service mapping')
        for sid, entity in mapped.items():
            record = entity_rows.get(entity)
            mapping = product.services[sid].ha_mapping
            if (not record or record.get('device_id') != source_id or record.get('disabled_by') is not None
                    or record.get('platform') == 'huawei_smarthome' or mapping is None
                    or entity.split('.')[0] != mapping.domain):
                raise ValueError('resident entity ownership/domain is not preserved')
        base = registry.get_product(product.pid)
        row = {'key': 'old-' + inventory[source_id]['sn'].lower(), 'pid': product.pid,
            'source_id': source_id, 'sn': inventory[source_id]['sn'], 'cloud_pid': CLOUD_PIDS[product.pid],
            'entities': mapped, 'product_sha256': core_module('product_registry').product_fingerprint(product)}
        if product != registry.merge(base):
            adapters = [a.services for a in registry.get_all_adapters() if a.pid == product.pid
                        and registry.merge(base, a) == product]
            if len(adapters) != 1:
                raise ValueError('original vendor mapping cannot be uniquely reconstructed')
            row['adapter'] = adapters[0]
        rows.append(row)
    # 不可用实体原样保留；绑定确认不等于状态可用或获准接管。
    unavailable = [issue for issue in report['issues'] if issue['code'] == 'entity_unavailable']
    bindings = {'version': 1, 'devices': rows}
    proof = {'version': 1, 'kind': 'hwhomebridge-offline-binding-freeze', 'entry_id': entry_id,
        'cold_manifest_sha256': core_module('identity_store').digest((cold / 'cold-export.json').read_bytes()),
        'observations_sha256': [digest(first), digest(second)], 'original_config_sha256': report['config_sha256'],
        'devices': len(rows), 'bindings_sha256': digest(bindings), 'resident_entity_bindings_confirmed': True,
        'unavailable_entities_retained': unavailable, 'adoption_ready': False,
        'original_HA_written': False, 'HA_service_calls': 0, 'native_library_loaded': False}
    output.mkdir(mode=0o700)
    for name, value in [('bindings.json', bindings), ('binding-freeze.json', proof)]:
        private_write(output / name, json.dumps(value, ensure_ascii=False, indent=2).encode())
    return proof


async def observe(url, token_file, cold, output):
    verify(cold)
    manifest = read_private(cold / 'cold-export.json')
    entry_id = manifest['entry']['entry_id']
    if not entry_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in entry_id):
        raise ValueError('safe original entry ID required')
    output = absolute(output)
    original = Path(manifest['source_config'])
    if (output.exists() or output.is_relative_to(original) or original.is_relative_to(output)
            or output.is_relative_to(cold) or cold.is_relative_to(output)):
        raise ValueError('new private observation directory required')
    token = token_file.read_text().strip()
    observations = []
    async with HAReader(url, token) as reader:
        for _ in range(2):
            snapshot = await capture(reader)
            report = manifest['entity_preflight']
            proof = registry_proof(snapshot['devices'], snapshot['entities'], set(report['source_ids']), set(report['bridge_ids']))
            if proof != report['registry_proof']:
                raise ValueError('original entity/device registry changed since cold export')
            reader.counter += 1
            await reader.ws.send(json.dumps({'id': reader.counter, 'type': 'config_entries/get', 'domain': 'hwhomebridge'}))
            entries = json.loads(await asyncio.wait_for(reader.ws.recv(), 30))
            if (entries.get('id') != reader.counter or not entries.get('success')
                    or len(entries['result']) != 1 or entries['result'][0]['entry_id'] != entry_id
                    or entries['result'][0]['state'] != 'loaded'):
                raise ValueError('one original loaded bridge entry required')
            request = urllib.request.Request(reader.url + '/api/diagnostics/config_entry/' + entry_id,
                headers={'Authorization': 'Bearer ' + token}, method='GET')
            def get_diagnostic():
                with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                    raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError('diagnostic exceeds read limit')
                return json.loads(raw)['data']
            observation = {'version': 1, 'kind': 'hwhomebridge-resident-binding-observation', 'entry_id': entry_id,
                           'registry_sha256': digest(proof),
                           'diagnostic': await asyncio.to_thread(get_diagnostic)}
            observed_devices(observation, entry_id)
            observations.append(observation)
    output.mkdir(mode=0o700)
    for index, value in enumerate(observations, 1):
        private_write(output / f'observation-{index}.private.json', json.dumps(value, ensure_ascii=False, indent=2).encode())
    return observations


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('observe', 'freeze'))
    parser.add_argument('--cold', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url')
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--first', type=Path)
    parser.add_argument('--second', type=Path)
    args = parser.parse_args()
    try:
        if args.action == 'observe':
            if not args.url or not args.token_file:
                parser.error('observe requires HA URL and local token file')
            asyncio.run(observe(args.url, args.token_file, args.cold, args.output))
            print(json.dumps({'read_only_observations': 2, 'original_HA_written': False}))
        else:
            if not args.first or not args.second:
                parser.error('freeze requires two saved observations')
            proof = asyncio.run(freeze(args.cold, read_private(args.first), read_private(args.second), args.output))
            print(json.dumps({'frozen_devices': proof['devices'], 'unavailable_retained': len(proof['unavailable_entities_retained']),
                              'adoption_ready': False, 'original_HA_written': False}))
    except Exception:
        print(json.dumps({'passed': False, 'error': 'binding verification failed; original HA was not changed'}))
        raise SystemExit(1) from None
