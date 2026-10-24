"""只读真实实体预检；重放公共匹配和组合，不导入 hwbridge、不调用 HA 服务。"""
import argparse
import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import types

from ha_production_readonly import SupervisorReader, private_write

READ_COMMANDS = {'get_config', 'get_states', 'get_services',
                 'config/device_registry/list', 'config/entity_registry/list',
                 'config/entity_registry/get', 'config_entries/get'}
DEVICE_FIELDS = ('id', 'name', 'name_by_user', 'model', 'manufacturer', 'disabled_by', 'config_entries', 'identifiers')
ENTITY_FIELDS = ('entity_id', 'device_id', 'platform', 'unique_id', 'disabled_by', 'hidden_by',
                 'device_class', 'original_device_class', 'name', 'original_name')
ATTRIBUTES = {'friendly_name', 'device_class', 'supported_features', 'supported_color_modes', 'color_mode',
              'brightness', 'color_temp_kelvin', 'min_color_temp_kelvin', 'max_color_temp_kelvin',
              'effect', 'effect_list', 'percentage', 'percentage_step', 'direction', 'preset_mode',
              'preset_modes', 'oscillating', 'options', 'min', 'max', 'step', 'unit_of_measurement'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def core_module(name):
    # 只运行当前源码中的公共模块，绝不执行从 HA 下载的 Python。
    package = '_hwhomebridge_export_core'
    if package not in sys.modules:
        module = types.ModuleType(package)
        module.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/hwhomebridge')]
        sys.modules[package] = module
    return importlib.import_module(package + '.' + name)


def project(rows, fields, key, *, sort_rows=True):
    result = []
    for row in rows:
        value = {name: row.get(name) for name in fields}
        if 'config_entries' in fields and 'config_entries' not in row and 'config_entry_id' in row:
            # HA 3.4设备存储为单一归属；WebSocket仍提供兼容列表。
            if row.get('composite_device_id') or row.get('has_composite_identifiers'):
                raise ValueError('composite storage ownership needs an explicit projection')
            owner = row['config_entry_id']
            if owner is not None and not isinstance(owner, str):
                raise ValueError('invalid singular configuration entry owner')
            value['config_entries'] = [owner] if owner is not None else []
        result.append(value)
    for row in result:
        for name in ('config_entries', 'identifiers'):
            if name in row and row[name] is not None:
                row[name] = sorted(row[name])
    if len({row[key] for row in result}) != len(result):
        raise ValueError('duplicate registry record')
    return sorted(result, key=lambda row: row[key]) if sort_rows else result


def registry_proof(devices, entities, source_ids, bridge_ids):
    return {'devices': project([d for d in devices if d['id'] in source_ids | bridge_ids], DEVICE_FIELDS, 'id'),
            'entities': project([e for e in entities if e.get('device_id') in source_ids], ENTITY_FIELDS, 'entity_id')}


def old_inventory(snapshot):
    entries = snapshot['entries']
    if len(entries) != 1 or entries[0]['domain'] != 'hwhomebridge':
        raise ValueError('exactly one original hwhomebridge entry required')
    entry = entries[0]
    sns = core_module('virtual_device').SNManager
    devices = {row['id']: row for row in snapshot['devices']}
    bridges = {}
    for row in devices.values():
        for identity in row.get('identifiers') or []:
            if identity and identity[0] == 'hwhomebridge' and len(identity) == 3 and identity[1] == entry['entry_id']:
                sn = identity[2]
                if sn in bridges or entry['entry_id'] not in row.get('config_entries', []):
                    raise ValueError('ambiguous original bridge registry identity')
                bridges[sn] = row
    if not bridges:
        raise ValueError('original bridge child inventory missing')
    sources = {}
    bridge_ids = {row['id'] for row in bridges.values()}
    for sn, bridge in bridges.items():
        matches = [d for d in devices.values() if d['id'] not in bridge_ids
                   and sns.generate_sn(d['id']) == sn]
        if len(matches) != 1:
            raise ValueError('original SN must resolve to exactly one source device')
        sources[sn] = (matches[0], bridge)
    return entry, sources


def commands(mapping):
    action, domain = mapping.action, mapping.domain
    if action == 'turn_on_off':
        return {'select_option'} if domain == 'select' else {'turn_on', 'turn_off'}
    if action in ('set_brightness', 'set_color_temp', 'set_color', 'set_effect'):
        return {'turn_on'}
    return {'set_speed': {'set_percentage'}, 'set_direction': {'set_direction'},
            'set_fan_mode': {'set_preset_mode'}, 'set_temperature': {'set_temperature'},
            'press': {'press'}}.get(action, set())


async def assess(snapshot, component, instances=None, capability_config=None):
    entry, sources = old_inventory(snapshot)
    router = core_module('service_router').ServiceRouter()
    if not await router.load_product_registry(str(component / 'config/product_registry.json')):
        raise ValueError('source product configuration unavailable')
    states = {row['entity_id']: row for row in snapshot['states']}
    entities = {row['entity_id']: row for row in snapshot['entities']}
    source_ids = {source['id'] for source, _ in sources.values()}
    bridge_ids = {bridge['id'] for _, bridge in sources.values()}
    issues, rows = [], []

    def issue(code, **context):
        value = {'code': code, **context}
        if value not in issues:
            issues.append(value)

    def available(entity, source):
        record, state = entities.get(entity), states.get(entity)
        if not record or record.get('device_id') != source:
            issue('entity_ownership', entity=entity)
        elif record.get('disabled_by') is not None or record.get('platform') == 'huawei_smarthome':
            issue('entity_disabled_or_loop', entity=entity)
        if not state or state['state'] in ('unknown', 'unavailable'):
            issue('entity_unavailable', entity=entity)

    def candidate(source, entity_list):
        return router.register_device(source['id'], source.get('name_by_user') or source.get('name') or '',
            entity_list, model=source.get('model') or '', manufacturer=source.get('manufacturer') or '')

    for sn, (source, bridge) in sorted(sources.items()):
        if source.get('disabled_by') is not None or bridge.get('disabled_by') is not None:
            issue('device_disabled', source_id=source['id'])
        entity_list = []
        for record in snapshot['entities']:
            if record.get('device_id') != source['id'] or record.get('platform') == 'huawei_smarthome':
                continue
            state = states.get(record['entity_id'], {}).get('attributes', {})
            entity_list.append({'entity_id': record['entity_id'], 'domain': record['entity_id'].split('.')[0],
                'device_class': record.get('device_class') or record.get('original_device_class') or state.get('device_class'),
                'name': state.get('friendly_name', '')})
        device = candidate(source, entity_list)
        if device is None:
            issue('legacy_product_not_matched', source_id=source['id'])
            continue
        mapped = {sid: svc.entity_id for sid, svc in device.service_entries.items()}
        router.sn_manager.unregister(device.sn)
        reversed_device = candidate(source, list(reversed(entity_list)))
        alternatives = {sid:svc.entity_id for sid,svc in reversed_device.service_entries.items()} if reversed_device else {}
        if reversed_device is None or mapped != alternatives:
            issue('legacy_mapping_order_ambiguous', source_id=source['id'])
        if reversed_device:
            router.sn_manager.unregister(reversed_device.sn)
        device = candidate(source, entity_list)
        for sid, entity in mapped.items():
            available(entity, source['id'])
            mapping = device.product.services[sid].ha_mapping
            missing = commands(mapping) - set(snapshot['services'].get(mapping.domain, []))
            if missing:
                issue('HA_service_missing', entity=entity, service=sid, actions=sorted(missing))
        rows.append({'source_id': source['id'], 'bridge_device_id': bridge['id'], 'sn': sn,
                     'pid': device.product.pid, 'entities': mapped,
                     'ambiguous_choices':{sid:sorted({entity,alternatives[sid]}) for sid,entity in mapped.items()
                                          if sid in alternatives and entity != alternatives[sid]}})

    compositions = []
    if (instances is None) != (capability_config is None):
        raise ValueError('explicit instances and capability config must be supplied together')
    if instances is not None:
        if 'ha' in instances:
            raise ValueError('preflight instances must not contain REST credentials')
        role_issues_before = len(issues)
        for source in instances['devices']:
            source_ids.add(source['id'])
            original = next((d for d in snapshot['devices'] if d['id'] == source['id']), None)
            if not original or original.get('disabled_by') is not None:
                issue('composition_device_missing_or_disabled', source_id=source['id'])
            for entity in source['roles'].values():
                available(entity, source['id'])
        if len(issues) == role_issues_before:
            class ReadOnlyTransport(core_module('ha_transport').ScopedTransport):
                def states(self):
                    return {k: v for k, v in states.items() if k in self.allowed}
                def call(self, *_args, **_kwargs):
                    raise RuntimeError('preflight cannot control devices')
            definitions = json.loads((capability_config / 'product_registry.json').read_text())
            owned = {source: {e['entity_id'] for e in snapshot['entities'] if e.get('device_id') == source
                             and e.get('disabled_by') is None and e.get('platform') != 'huawei_smarthome'} for source in source_ids}
            try:
                # 只验证公共角色和状态，不把预检用SN当成最终原生目录或父身份。
                composed = await core_module('capability_router').CapabilityRouter.create(ReadOnlyTransport(), capability_config,
                    {**instances,'parent_sn':instances.get('parent_sn','preflight-existing-parent')},
                    owned_entities=owned, supported_pids={p['pid'] for p in definitions['products']},
                    sn_manager=router.sn_manager)
                for key, device in composed.devices.items():
                    values = composed.snapshot(key)
                    if set(values) != set(device.product.services):
                        issue('composition_state_incomplete', card=key)
                    for service in device.product.services.values():
                        mapping = service.ha_mapping
                        missing = commands(mapping) - set(snapshot['services'].get(mapping.domain, []))
                        if missing:
                            issue('HA_service_missing', card=key, actions=sorted(missing))
                    compositions.append({'key': key, 'pid': device.product.pid, 'state_projection_valid': True})
            except (ValueError, KeyError, TypeError):
                issue('composition_mapping_or_capability_invalid')
    proof = registry_proof(snapshot['devices'], snapshot['entities'], source_ids, bridge_ids)
    config_files = {str(p.relative_to(component)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((component / 'config').rglob('*.json'))}
    return {'version': 1, 'kind': 'hwhomebridge-entity-preflight', 'structure_passed': not issues,
        'inventory_complete':True,
        'migration_allowed': False, 'original_runtime_bindings_confirmed': False,
        'parent_identity_verified':False, 'runtime_control_verified':False,
        'entry': {'entry_id': entry['entry_id'], 'unique_id': entry.get('unique_id')},
        'legacy_inventory':[{'sn':sn,'source_id':source['id'],'bridge_device_id':bridge['id']}
                            for sn,(source,bridge) in sorted(sources.items())],
        'legacy_bindings': rows, 'compositions': compositions, 'issues': issues,
        'proposed_roles':[{'source_id':s['id'],'model':s.get('model'),'roles':s['roles']}
                          for s in instances['devices']] if instances else [],
        'source_ids': sorted(source_ids), 'bridge_ids': sorted(bridge_ids),
        'registry_proof': proof, 'registry_sha256': digest(proof), 'config_sha256': config_files,
        'capability_config_sha256': ({str(p.relative_to(capability_config)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(capability_config.rglob('*.json'))} if capability_config else {}),
        'instances_sha256': digest(instances) if instances else None,
        'service_calls': 0, 'native_library_loaded': False, 'cold_backup': False}


class HAReader(SupervisorReader):
    async def command(self, kind, *, entity_id=None):
        if kind not in READ_COMMANDS:
            raise ValueError('non-read-only HA command refused')
        arguments = {}
        if kind == 'config/entity_registry/get':
            if not isinstance(entity_id, str) or '.' not in entity_id or any(c.isspace() for c in entity_id):
                raise ValueError('explicit inventoried entity required')
            arguments['entity_id'] = entity_id
        elif entity_id is not None:
            raise ValueError('unexpected arguments for read-only command')
        self.counter += 1
        await self.ws.send(json.dumps({'id': self.counter, 'type': kind, **arguments}))
        result = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
        if result.get('id') != self.counter or not result.get('success'):
            raise ValueError('HA read-only command failed')
        self.read_requests += 1
        return result['result']


async def capture(reader, extra_source_ids=()):
    config = await reader.command('get_config')
    entries = await reader.command('config_entries/get')
    services = await reader.command('get_services')
    result = {'ha_version': config['version'],
        'entries': [{k:e.get(k) for k in ('entry_id', 'domain', 'state', 'disabled_by')} for e in entries if e['domain']=='hwhomebridge'],
        'devices': project(await reader.command('config/device_registry/list'), DEVICE_FIELDS, 'id'),
        'entities': project(await reader.command('config/entity_registry/list'), ENTITY_FIELDS, 'entity_id', sort_rows=False),
        'states': [{'entity_id':s['entity_id'], 'state':s['state'],
                    'attributes':{k:v for k,v in s['attributes'].items() if k in ATTRIBUTES}}
                   for s in await reader.command('get_states')],
        'services': {domain:sorted(definitions) for domain, definitions in services.items()}}
    _, sources = old_inventory(result)
    selected = {source['id'] for source, _ in sources.values()} | set(extra_source_ids)
    # 新版list是精简表示，不能把未返回的device_class当成持久化None。
    for row in result['entities']:
        if row.get('device_id') not in selected:
            continue
        detail = await reader.command('config/entity_registry/get', entity_id=row['entity_id'])
        if detail.get('entity_id') != row['entity_id'] or detail.get('device_id') != row['device_id']:
            raise ValueError('entity ownership changed during detail collection')
        for field in ('device_class', 'original_device_class'):
            if field not in detail:
                raise ValueError('full entity class metadata missing')
            row[field] = detail[field]
    return result


def public_summary(report):
    return {'structure_passed': report['structure_passed'], 'migration_allowed': False,
        'source_inventory_complete':report['inventory_complete'],
        'legacy_devices': len(report['legacy_bindings']), 'composition_cards': len(report['compositions']),
        'issue_counts': {code:sum(row['code']==code for row in report['issues']) for code in sorted({r['code'] for r in report['issues']})},
        'service_calls': 0, 'native_library_loaded': False, 'cold_backup': False}


async def collect(args):
    if args.output.exists() or args.output.resolve() != args.output.absolute():
        raise ValueError('new private output directory required')
    instances = json.loads(args.instances.read_text()) if args.instances else None
    extra_sources = {row['id'] for row in instances['devices']} if instances else set()
    async with HAReader(args.url, args.token_file.read_text().strip()) as reader:
        first, second = await capture(reader, extra_sources), await capture(reader, extra_sources)
    report = await assess(second, args.component, instances, args.capability_config)
    if registry_proof(first['devices'], first['entities'], set(report['source_ids']), set(report['bridge_ids'])) != report['registry_proof']:
        raise ValueError('target registry changed during read-only inspection')
    # 当前值允许变化，控制能力和可用性必须两遍都通过。
    previous = await assess(first, args.component, instances, args.capability_config)
    if (previous['issues'] != report['issues'] or previous['legacy_bindings'] != report['legacy_bindings']
            or first['services'] != second['services'] or first['entries'] != second['entries']):
        report['structure_passed'] = False
        report['issues'].append({'code':'online_observation_not_stable'})
    report['two_pass_registry_stable'] = first['entries'] == second['entries']
    report['HA_read_commands'] = reader.read_requests
    args.output.mkdir(mode=0o700)
    relevant = set(report['source_ids']) | set(report['bridge_ids'])
    second['devices'] = [d for d in second['devices'] if d['id'] in relevant]
    second['entities'] = [e for e in second['entities'] if e.get('device_id') in relevant]
    ids = {e['entity_id'] for e in second['entities']}
    second['states'] = [s for s in second['states'] if s['entity_id'] in ids]
    private_write(args.output / 'snapshot.private.json', json.dumps(second, ensure_ascii=False).encode())
    private_write(args.output / 'preflight.private.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
    private_write(args.output / 'summary.json', json.dumps(public_summary(report), indent=2).encode())
    return public_summary(report)


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--token-file', type=Path, required=True)
    parser.add_argument('--component', type=Path, required=True, help='已下载的源组件副本，仅读取 config JSON')
    parser.add_argument('--instances', type=Path)
    parser.add_argument('--capability-config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(collect(args)), ensure_ascii=False))
    except Exception:
        print(json.dumps({'structure_passed': False, 'error':'read-only entity preflight failed', 'service_calls':0}))
        raise SystemExit(1) from None
