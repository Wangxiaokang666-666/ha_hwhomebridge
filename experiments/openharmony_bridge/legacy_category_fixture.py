"""001–008 虚构清单；由原公共产品/厂家适配器生成，不连接家庭 HA。"""
import json
from pathlib import Path

from single_engine_fixture import COMPONENT, load, private_root, save

CLOUD_PIDS = dict(zip((f'{i:03d}' for i in range(1, 9)),
                     ('9MTL', '9MU7', '9MTJ', '9MTM', '9MTR', '9MTV', '9MTW', '9MTX')))


def product(registry, pid):
    adapter = None
    if pid == '006':
        adapter = next(row for row in registry.get_all_adapters() if row.pid == pid)
    return registry.merge(registry.get_product(pid), adapter)


def add_states(root, actor):
    path = root / 'legacy-inventory.json'
    if path.exists():
        for row in json.loads(path.read_text()):
            for entity, state in row['states'].items():
                if not entity.split('.')[1].startswith('oh_single_') or entity in actor.data:
                    raise ValueError('isolated unique legacy entities required')
                actor.data[entity] = state


def install(root, router, primary):
    bridges, catalog = {}, []
    inventory = root / 'legacy-inventory.json'
    rows = json.loads(inventory.read_text()) if inventory.exists() else []
    all_devices = [('legacy-light', primary)]
    for row in rows:
        definition = product(router.registry, row['pid'])
        device = load('virtual_device').VirtualDevice(
            load('virtual_device').SNManager.generate_sn(row['source_id']), definition, row['source_id'])
        for sid, entity in row['entities'].items():
            device.add_service_entry(sid, load('virtual_device').ServiceEntry(entity, entity.split('.')[0], sid))
        if not router.sn_manager.register(device):
            raise ValueError('duplicate legacy fixture identity')
        all_devices.append((row['key'], device))
    for key, device in all_devices:
        profile = json.loads((Path(__file__).parent / 'evidence' / (CLOUD_PIDS[device.product.pid] + '-profile.json')).read_text())
        bridge = load('profile_bridge').ProfileBridge(router, device, profile)
        bridges[key] = bridge
        catalog.append({'key': key, 'framework_pid': device.product.pid,
            'sdk_index': int(device.product.pid) - 1, 'pid': CLOUD_PIDS[device.product.pid], 'sn': device.sn,
            'devType': profile['deviceTypeId'], 'manu': profile['manufacturerId'],
            'model': profile['deviceModel'], 'protType': 6, 'services': bridge.services})
    return bridges, catalog


async def prepare(root):
    private_root(root)
    if (root / 'legacy-inventory.json').exists():
        raise ValueError('legacy virtual inventory already exists; do not replace paired SNs')
    parent = json.loads((root / 'gateway.local.json').read_text())['sn']
    registry = load('product_registry').ProductRegistry()
    await registry.load(str(COMPONENT / 'config/product_registry.json'), str(COMPONENT / 'config/adapters'))
    rows = []
    for pid in CLOUD_PIDS:
        if pid == '002':
            continue
        definition = product(registry, pid)
        states, entities = {}, {}
        for sid, service in definition.services.items():
            mapping = service.ha_mapping
            if mapping is None or not mapping.domain:
                continue
            suffix = 'lamp' if mapping.domain == 'light' else sid.lower()
            entity = mapping.domain + '.oh_single_old_' + pid + '_' + suffix
            entities[sid] = entity
            value = {'light': 'on', 'switch': 'on', 'number': '40', 'select': '快煮饭',
                     'binary_sensor': 'on', 'sensor': '20'}.get(mapping.domain, 'off')
            attrs = {'brightness': 128, 'color_temp_kelvin': 4000} if mapping.domain == 'light' else {}
            vm = mapping.value_mapping
            if vm:
                if vm.mapping_type in ('text_to_enum',):
                    value = next(iter(vm.mapping_dict))
                elif vm.mapping_type == 'enum_to_text_multi':
                    value = vm.mapping_dict['1'][0]
                    attrs['options'] = list(dict.fromkeys(item for values in vm.mapping_dict.values() for item in values))
                elif vm.mapping_type == 'delay_to_select':
                    value = vm.mapping_dict['0']
                    attrs['options'] = [v for k, v in vm.mapping_dict.items() if k.isdigit()]
                elif vm.mapping_type == 'enum_to_number':
                    value = str(vm.mapping_dict['0'])
            if service.value_type == 'float':
                value = {'electric': '12.34', 'voltage': '220.56', 'power': '36.78', 'current': '0.17'}[sid]
            if sid == 'temperature':
                value = '26'
            states[entity] = {'entity_id': entity, 'state': value, 'attributes': attrs}
        rows.append({'key': 'legacy-' + pid, 'pid': pid, 'source_id': 'single-engine-virtual-old-' + parent + '-' + pid,
                     'entities': entities, 'states': states})
    save(root / 'legacy-inventory.json', rows)
    persisted = root / 'virtual-device-state.private.json'
    if persisted.exists():
        states = json.loads(persisted.read_text())
        for row in rows:
            for entity, state in row['states'].items():
                states.setdefault(entity, state)
        save(persisted, states)


if __name__ == '__main__':
    import asyncio
    import sys
    asyncio.run(prepare(Path(sys.argv[1]).resolve()))
