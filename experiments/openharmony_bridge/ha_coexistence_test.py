"""真实 HA Core + 真实自编 C；旧 SDK 用替身，真实旧 Python/ctypes 路由保留。

仅用于全新的隔离 /config，实体名必须以 oh_flow_ 开头。无生产 token，
无实际配对身份，不把此结果表述为闭源 SDK 共存或 App 实机验收。
"""
import argparse
import asyncio
import hashlib
import json
import logging
import platform
from pathlib import Path
import shutil
import socket
import sys

from homeassistant import bootstrap, loader
from homeassistant.config_entries import ConfigEntryState, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

sys.path.insert(0, '/config')
CONFIG = Path('/config')
DIRECTORY = '.storage/hwhomebridge/openharmony/lab'
LAB = CONFIG / DIRECTORY
LEGACY_LIGHT = 'light.oh_flow_legacy'
RESULT = CONFIG / 'coexistence-results.json'


class SDKRecorder:
    evidence = 'call recorder; actual legacy Python/ctypes routing'
    def __init__(self):
        self.starts = 0
        self.registrations = 0
        self.nullifications = 0
        self.removals = 0
        self.callbacks = None
        self.reports = []

    def RegHomeAssistantPyCB(self, *callbacks):
        self.callbacks = callbacks
        if callbacks[0] is None:
            self.nullifications += 1
        else:
            self.registrations += 1

    def UpdateHAStatus(self, sn, sid, payload):
        assert sn.value == b'LEGACY-FIXTURE-SN'
        self.reports.append(json.loads(payload.value))
        return 0

    def RegPINQueryCallback(self, callback):
        self.pin = callback

    def RegGetRoomInfoCallback(self, callback):
        self.room = callback


def hashes():
    paths = [LAB / 'gateway.local.json', LAB / 'instances.json'] + list((LAB / 'runtime').glob('ha-child-*.json'))
    return {str(p.relative_to(LAB)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def fixture(hass, calls):
    instance = json.loads((LAB / 'instances.json').read_text())
    roles = instance['devices'][0]['roles']
    assert all(entity.split('.')[1].startswith('oh_flow_') for entity in roles.values())
    for role, entity in roles.items():
        attrs = {'percentage': 87, 'direction': 'reverse'} if role == 'fan' else {}
        if role == 'main':
            attrs = {'brightness': None, 'color_temp_kelvin': None, 'effect': None}
        hass.states.async_set(entity, '0' if entity.startswith(('number.', 'sensor.')) else 'off', attrs)
    hass.states.async_set(LEGACY_LIGHT, 'off')

    async def service(call):
        entity = call.data['entity_id']
        assert entity in set(roles.values()) | {LEGACY_LIGHT}, 'non-fixture service write rejected'
        old = hass.states.get(entity)
        attrs = {**old.attributes, **{k: v for k, v in call.data.items() if k != 'entity_id'}}
        if call.service == 'set_value':
            value = str(call.data['value'])
        elif call.service in ('turn_on', 'set_percentage'):
            value = 'on'
        elif call.service == 'turn_off':
            value = 'off'
        else:
            value = old.state
        hass.states.async_set(entity, value, attrs)
        for target in ('fan', 'light'):
            if entity == roles[target + '_timer_enable']:
                remaining = hass.states.get(roles[target + '_timer_duration']).state if value == 'on' else '0'
                hass.states.async_set(roles[target + '_timer_remaining'], remaining)
        calls.append({'domain': call.domain, 'service': call.service, 'entity': entity})

    for domain, actions in {'fan': ('turn_on', 'turn_off', 'set_percentage', 'set_direction'),
                            'light': ('turn_on', 'turn_off'), 'switch': ('turn_on', 'turn_off'),
                            'number': ('set_value',)}.items():
        for action in actions:
            hass.services.async_register(domain, action, service)
    return roles


def install_legacy_recorder(hass):
    # 导入真实旧 Python 模块；仅替换加载闭源库的入口和破坏性清理入口。
    from custom_components.hwhomebridge import hwbridge as legacy
    from custom_components.hwhomebridge.product_registry import ProductDef, ServiceDef, HAMapping
    from custom_components.hwhomebridge.service_router import ServiceRouter
    from custom_components.hwhomebridge.virtual_device import VirtualDevice, ServiceEntry
    sdk = SDKRecorder()
    router = ServiceRouter()
    router.set_hass(hass)
    product = ProductDef('002', 'Legacy fixture', services={
        'switch': ServiceDef('switch', 'switch', 'on', ha_mapping=HAMapping(domain='light', action='turn_on_off', value_attr='state'))})
    vd = VirtualDevice('LEGACY-FIXTURE-SN', product, 'legacy-source-device')
    vd.add_service_entry('switch', ServiceEntry(LEGACY_LIGHT, 'light', 'switch'))
    vd.online = True
    router.set_lib(sdk)
    assert router.sn_manager.register(vd)

    async def start(_hass):
        sdk.starts += 1
        legacy.ghass = hass
        legacy.lib = sdk
        legacy.service_router = router
        legacy.bridge_work = True
        hass.bus.async_listen("state_changed", legacy.handle_state_event)

    def remove(_hass):
        sdk.removals += 1
        raise AssertionError('isolated test forbids legacy unbinding')

    legacy.start_hw_hilink_bridge = start
    legacy.stop_hw_hilink_bridge = remove
    return legacy, sdk, vd


async def make_hass(install_legacy=install_legacy_recorder):
    hass = HomeAssistant(str(CONFIG))
    loader.async_setup(hass)
    hass.config.skip_pip = True
    calls = []
    roles = fixture(hass, calls)
    legacy, sdk, vd = install_legacy(hass)
    result = await bootstrap.async_from_config_dict({'homeassistant': {
        'name': 'Isolated coexistence test', 'latitude': 0, 'longitude': 0,
        'elevation': 0, 'unit_system': 'metric', 'time_zone': 'Asia/Shanghai'}}, hass)
    assert result is hass
    for domain in ('fan', 'light', 'switch', 'number'):
        assert await async_setup_component(hass, domain, {})
    fixture(hass, calls)
    await hass.async_start()
    await hass.async_block_till_done()
    return hass, calls, roles, legacy, sdk, vd


async def create_entry(hass, kind, directory=DIRECTORY):
    flow = await hass.config_entries.flow.async_init('hwhomebridge', context={'source': SOURCE_USER})
    assert flow['step_id'] == 'user'
    flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {'backend': kind})
    if kind == 'openharmony':
        flow = await hass.config_entries.flow.async_configure(flow['flow_id'], {'openharmony_directory': directory})
    await hass.async_block_till_done()
    if flow['type'] == 'form' and flow.get('errors'):
        hass.config_entries.flow.async_abort(flow['flow_id'])
    return flow


async def options(hass, entry, mode, directory=DIRECTORY):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(flow['flow_id'], {'action': 'backend'})
    try:
        flow = await hass.config_entries.options.async_configure(flow['flow_id'], {'backend': mode})
    except InvalidData:
        hass.config_entries.options.async_abort(flow['flow_id'])
        raise
    if flow['type'] == 'form' and flow['step_id'] == 'openharmony':
        flow = await hass.config_entries.options.async_configure(flow['flow_id'], {'openharmony_directory': directory})
    await hass.async_block_till_done()
    return flow


def runtime(hass, entry):
    return hass.data['hwhomebridge'][entry.entry_id]['runtime']


async def post(hass, port, request):
    import urllib.request
    def send():
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        req = urllib.request.Request(f'http://127.0.0.1:{port}/bridge', data=json.dumps(request).encode())
        return json.load(opener.open(req, timeout=12))
    return await hass.async_add_executor_job(send)


def clone_fixture(name, *, new_identity=True, port=18092, lan=15685, policy='read_only'):
    target = LAB.parent / name
    target.mkdir(mode=0o700)
    (target / 'runtime').mkdir(mode=0o700)
    # 只复制测试模板和未配对子设备目录，不复制运行中的 SDK KV 或已生成身份文件。
    for file in ('deployment.json', 'gateway.local.json', 'instances.json', 'bridge_poc', 'ca.pem'):
        shutil.copyfile(LAB / file, target / file)
        (target / file).chmod(0o700 if file == 'bridge_poc' else 0o600)
    shutil.copytree(LAB / 'profiles', target / 'profiles')
    shutil.copyfile(LAB / 'runtime/ha-children.local.json', target / 'runtime/ha-children.local.json')
    (target / 'runtime/ha-children.local.json').chmod(0o600)
    gateway = json.loads((target / 'gateway.local.json').read_text())
    instance = json.loads((target / 'instances.json').read_text())
    if new_identity:
        import uuid
        gateway['sn'] = instance['parent_sn'] = 'OHPOC-' + uuid.uuid4().hex[:24]
    gateway['ca_file'] = str(target / 'ca.pem')
    (target / 'gateway.local.json').write_text(json.dumps(gateway))
    (target / 'instances.json').write_text(json.dumps(instance))
    settings = json.loads((target / 'deployment.json').read_text())
    settings.update(port=port, lan_port=lan, write_policy=policy)
    (target / 'deployment.json').write_text(json.dumps(settings))
    return str(target.relative_to(CONFIG))


async def check_legacy(hass, sdk, vd, entry):
    assert sdk.callbacks and sdk.callbacks[0] is not None
    assert sdk.starts == 1 and sdk.removals == 0
    device = dr.async_get(hass).async_get_device_by_identifier(('hwhomebridge', entry.entry_id, vd.sn), entry.entry_id)
    assert device is not None and device.config_entry_id == entry.entry_id
    before = len(sdk.reports)
    for on in (1, 0):
        await hass.async_add_executor_job(sdk.callbacks[0], vd.sn.encode(), json.dumps({'on': on}).encode())
        await hass.async_block_till_done()
        assert hass.states.get(LEGACY_LIGHT).state == ('on' if on else 'off')
    from custom_components.hwhomebridge.native_async import drain_native
    await hass.async_add_executor_job(drain_native)
    assert len(sdk.reports) >= before + 2 and sdk.reports[-1] == {'on': 0}, sdk.reports


async def exercise(oh_first=False, restore=False, *, install_legacy=install_legacy_recorder,
                   check_legacy=check_legacy):
    results = {'OpenHarmony_C': f'real Linux {platform.machine()} process', 'real_devices_accessed': False}
    hass, calls, roles, legacy, sdk, vd = await make_hass(install_legacy)
    results['legacy_SDK'] = sdk.evidence
    try:
        if restore:
            entries = hass.config_entries.async_entries('hwhomebridge')
            old = next(e for e in entries if e.data.get('backend_type') == 'legacy')
            new = next(e for e in entries if e.data.get('backend_type') == 'openharmony')
            assert old.state == new.state == ConfigEntryState.LOADED
            assert runtime(hass, new).process.returncode is None
            await check_legacy(hass, sdk, vd, old)
            assert hashes() == json.loads((CONFIG / 'restart-identities.json').read_text())
            results['HA_process_restart_restores_both_entries_and_identity'] = True
        else:
            created = {}
            for kind in (('openharmony', 'legacy') if oh_first else ('legacy', 'openharmony')):
                flow = await create_entry(hass, kind)
                assert flow['type'] == 'create_entry', flow
                created[kind] = flow['result']
                assert created[kind].state == ConfigEntryState.LOADED, created[kind].reason
                if oh_first and kind == 'openharmony':
                    # 原型 DOMAIN 唯一 ID 要先迁移，随后才能新增真正的旧后端。
                    prototype = created[kind]
                    hass.config_entries.async_update_entry(prototype,
                        data={'backend': 'openharmony', 'openharmony_directory': DIRECTORY}, unique_id='hwhomebridge')
                    await hass.config_entries.async_reload(prototype.entry_id)
                    await hass.async_block_till_done()
                    assert prototype.unique_id != 'hwhomebridge'
                    results['prototype_DOMAIN_identity_migrates_before_legacy_creation'] = True
            old, new = created['legacy'], created['openharmony']
            await check_legacy(hass, sdk, vd, old)
            results['both_creation_orders_supported'] = 'OH first' if oh_first else 'legacy first'
            sdk_marker = (sdk.registrations, sdk.nullifications)
            original = hashes()
            native = runtime(hass, new)
            assert native.deployment.write_policy == 'fan_only'
            results['legacy_resident_does_not_block_real_C'] = True
            from custom_components.hwhomebridge.backend_entries import openharmony_unique_id
            assert new.unique_id == openharmony_unique_id(native.deployment.gateway_sn)
            # 兼容原型用 domain 做唯一 ID 的 OH 条目；恢复后身份不变。
            hass.config_entries.async_update_entry(new, data={'backend': 'openharmony', 'openharmony_directory': DIRECTORY}, unique_id='old-prototype')
            await hass.config_entries.async_reload(new.entry_id)
            await hass.async_block_till_done()
            native = runtime(hass, new)
            assert new.unique_id == openharmony_unique_id(native.deployment.gateway_sn)
            assert hashes() == original
            results['prototype_OH_entry_migrates_without_new_cloud_identity'] = True
            # 旧条目没有 backend 字段仍能重载，并且 SDK 不重复启动。
            hass.config_entries.async_update_entry(old, data={})
            await hass.config_entries.async_reload(old.entry_id)
            await hass.async_block_till_done()
            await check_legacy(hass, sdk, vd, old)
            sdk_marker = (sdk.registrations, sdk.nullifications)
            results['old_entry_without_backend_keeps_legacy_singleton'] = True
            flow = await create_entry(hass, 'legacy')
            assert flow['type'] == 'abort'
            results['duplicate_legacy_rejected'] = True
            process = runtime(hass, new).process
            await options(hass, old, 'disabled')
            assert old.options['backend'] == 'disabled'
            assert hass.data['hwhomebridge'][old.entry_id]['backend'] == 'legacy'
            assert process.returncode is None
            await check_legacy(hass, sdk, vd, old)
            await options(hass, old, 'legacy')
            assert sdk.starts == 1
            results['legacy_disable_requires_restart_without_affecting_OH'] = True
            for entry, mode in ((old, 'openharmony'), (new, 'legacy')):
                try:
                    response = await options(hass, entry, mode)
                except InvalidData:
                    pass  # 选项 schema 已拒绝跨类型替换。
                else:
                    assert response['type'] == 'abort'
            results['entry_backend_type_cannot_replace_other_backend'] = True
            assert (await create_entry(hass, 'openharmony'))['type'] == 'form'
            copy = clone_fixture('same-identity', new_identity=False)
            assert (await create_entry(hass, 'openharmony', copy))['type'] == 'form'
            results['duplicate_directory_and_root_identity_rejected'] = True
            response = await post(hass, 18091, {'key': 'timer-controls', 'sid': 'switchFan', 'data': {'on': 1}})
            assert response['ok'] and hass.states.get(roles['fan']).state == 'on'
            response = await post(hass, 18091, {'key': 'timer-controls', 'sid': 'fan', 'data': {'gear': 3}})
            assert response['ok'] and hass.states.get(roles['fan']).attributes['percentage'] == 50
            results['native_HA_fan_service_write_and_readback'] = True
            count = len(calls)
            blocked = [('light', 'switch', {'on': 1}), ('light', 'brightness', {'brightness': 20}),
                       ('light', 'cct', {'colorTemperature': 4000}), ('aux', 'switch2', {'on': 1}),
                       ('light-options', 'switch1', {'on': 1}), ('effects-0', 'switch1', {'on': 1}),
                       ('light', 'delay', {'action': 2, 'num': 1, 'timer': [{'id': 1}]}),
                       ('timer-controls', 'countdownht', {'Equipmentname': 0, 'timeing': 2, 'countdownht': 1}),
                       ('timer-controls', 'countdownht', {'Equipmentname': 1})]
            for key, sid, data in blocked:
                assert not (await post(hass, 18091, {'key': key, 'sid': sid, 'data': data}))['ok']
            assert len(calls) == count
            # 联合定时先列出 fan 也必须在任何调用前整体拒绝。
            mapping = native.deployment.router.devices['timer-controls'].product.services['countdownht'].ha_mapping
            mapping.options['targets']['0'].reverse()
            assert not (await post(hass, 18091, {'key': 'timer-controls', 'sid': 'countdownht',
                                                'data': {'Equipmentname': 0, 'timeing': 2, 'countdownht': 1}}))['ok']
            assert len(calls) == count
            results['light_settings_and_combined_timer_rejected_before_any_write'] = True
            try:
                await hass.async_add_executor_job(native.deployment.router.transport.call, 'light', 'turn_on', roles['main'])
            except ValueError:
                pass
            else:
                raise AssertionError('native transport bypassed write policy')
            results['native_transport_also_enforces_write_policy'] = True
            assert (await post(hass, 18091, {'key': 'fan-wifi', 'sid': 'endTime', 'data': {'time': 2}}))['ok']
            assert hass.states.get(roles['fan_timer_remaining']).state == '2'
            assert hass.states.get(roles['light_timer_enable']).state == 'off'
            assert (await post(hass, 18091, {'key': 'fan-wifi', 'sid': 'endTime', 'data': {'time': 0}}))['ok']
            results['fan_only_timer_keeps_light_timer_untouched'] = True
            previous = native.process
            await options(hass, new, 'disabled')
            assert previous.returncode is not None and runtime(hass, new) is None and hashes() == original
            await check_legacy(hass, sdk, vd, old)
            await options(hass, new, 'openharmony')
            native = runtime(hass, new)
            assert native.process.returncode is None
            results['OH_disable_reenable_preserves_legacy_and_identity'] = True
            previous = native
            previous.process.kill()
            await previous.process.wait()
            for _ in range(80):
                await asyncio.sleep(.1)
                await hass.async_block_till_done()
                current = hass.data['hwhomebridge'].get(new.entry_id, {}).get('runtime')
                if current is not None and current is not previous:
                    break
            assert current is not previous and current.process.returncode is None
            await check_legacy(hass, sdk, vd, old)
            results['C_crash_recovers_only_its_own_entry'] = True
            path = LAB / 'deployment.json'
            saved = path.read_bytes()
            invalid = json.loads(saved)
            invalid['binary_sha256'] = '0' * 64
            path.write_text(json.dumps(invalid))
            await hass.config_entries.async_reload(new.entry_id)
            await hass.async_block_till_done()
            assert new.state == ConfigEntryState.SETUP_RETRY
            await check_legacy(hass, sdk, vd, old)
            await options(hass, new, 'disabled')
            assert runtime(hass, new) is None
            path.write_bytes(saved)
            await options(hass, new, 'openharmony')
            assert runtime(hass, new).process.returncode is None
            results['failed_setup_can_be_disabled_and_repaired_without_legacy_fallback'] = True
            conflict_dir = clone_fixture('port-conflict', port=18091, lan=15687)
            assert (await create_entry(hass, 'openharmony', conflict_dir))['type'] == 'form'
            conflict_dir = clone_fixture('writer-conflict', port=18094, lan=15688, policy='fan_only')
            assert (await create_entry(hass, 'openharmony', conflict_dir))['type'] == 'form'
            results['active_port_and_writable_entity_conflicts_rejected'] = True
            second_dir = clone_fixture('second')
            flow = await create_entry(hass, 'openharmony', second_dir)
            assert flow['type'] == 'create_entry', flow
            second = flow['result']
            second_process = runtime(hass, second).process
            assert not (await post(hass, 18092, {'key': 'timer-controls', 'sid': 'switchFan', 'data': {'on': 1}}))['ok']
            assert (await post(hass, 18092, {'key': 'timer-controls'}))['ok']
            results['second_read_only_OH_instance_is_independent'] = True
            primary_process = runtime(hass, new).process
            assert await hass.config_entries.async_remove(new.entry_id)
            await hass.async_block_till_done()
            assert primary_process.returncode is not None and second_process.returncode is None
            assert hashes() == original and sdk.removals == 0
            await check_legacy(hass, sdk, vd, old)
            assert (sdk.registrations, sdk.nullifications) == sdk_marker
            results['OH_remove_keeps_other_C_and_legacy_callbacks_devices'] = True
            assert await hass.config_entries.async_remove(second.entry_id)
            flow = await create_entry(hass, 'openharmony')
            assert flow['type'] == 'create_entry'
            new = flow['result']
            results['same_gateway_readded_with_same_saved_identity'] = hashes() == original
            # 外部 UDP 占用时不启动新 C，其他后端继续工作。
            busy_dir = clone_fixture('busy', port=18093, lan=15686)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
                busy.bind(('0.0.0.0', 15686))
                flow = await create_entry(hass, 'openharmony', busy_dir)
                failed = flow['result']
                assert failed.state == ConfigEntryState.SETUP_RETRY
                await check_legacy(hass, sdk, vd, old)
                assert runtime(hass, new).process.returncode is None
                await options(hass, failed, 'disabled')
                assert await hass.config_entries.async_remove(failed.entry_id)
            results['busy_LAN_port_does_not_touch_existing_backends'] = True
            assert (sdk.registrations, sdk.nullifications) == sdk_marker and sdk.removals == 0
            (CONFIG / 'restart-identities.json').write_text(json.dumps(hashes()))
        assert all(row['entity'].split('.')[1].startswith('oh_flow_') for row in calls)
        results['legacy_starts'] = sdk.starts
        results['legacy_unbind_calls'] = sdk.removals
        results['virtual_service_call_count'] = len(calls)
        results['passed'] = True
        RESULT.write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        owned = [value['runtime'] for value in hass.data.get('hwhomebridge', {}).values()
                 if isinstance(value, dict) and value.get('runtime')]
        await hass.async_stop()
        assert all(item.process.returncode is not None for item in owned)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--oh-first', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(exercise(args.oh_first, args.restore))
