"""真实 HA 原生表单验收；必须以 --network none 运行，仅操作 oh_single 虚拟源。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import InvalidData

from gateway_ha_acceptance import BASE, CONFIG, make_hass, child_processes, save


def frozen():
    paths = [BASE / name for name in ('gateway.local.json', 'bindings.json', 'instances.json')]
    paths += list((BASE / 'runtime/sdk-state').rglob('*'))
    paths += list((BASE / 'runtime').glob('ha-child-*.json'))
    return {str(path.relative_to(BASE)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


async def flow_start(hass, entry, action):
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    return await hass.config_entries.options.async_configure(flow['flow_id'], {'action': action})


async def advance(hass, flow, values, step):
    result = await hass.config_entries.options.async_configure(flow['flow_id'], values)
    assert result.get('step_id') == step and not result.get('errors'), (step, result)
    return result


async def exercise(phase):
    # 没有默认路由，避免清理过的实验身份重新上线；不启动 SDK。
    assert not any(line.split()[1] == '00000000' for line in Path('/proc/net/route').read_text().splitlines()[1:])
    before = frozen()
    hass, entry = await make_hass()
    from custom_components.hwhomebridge.gateway_management import GatewayManagement, OPTION, prepare_runtime
    from custom_components.hwhomebridge.availability import SourceUnavailable
    try:
        assert entry.state == ConfigEntryState.LOADED and entry.options['backend'] == 'disabled'
        assert not child_processes()
        manager = await GatewayManagement.create(hass, entry)
        original = manager.initial()
        identity = (entry.entry_id, entry.unique_id)
        if phase == 'restart':
            expected = json.loads((CONFIG / 'management-result.private.json').read_text())
            assert list(identity) == expected['entry_identity']
            document = manager.document(entry.options[OPTION])
            router, _ = await manager.preview(document, entry.entry_id)
            assert {row['key']: row['sn'] for row in router.catalog} == expected['child_sns']
            assert not document['enabled']
            for row in router.catalog:
                try:
                    router.snapshot(row['key'])
                except SourceUnavailable:
                    pass
                else:
                    raise AssertionError('disabled child is readable')
            active = BASE / 'runtime/managed'
            native = subprocess.run([str(BASE / 'bridge_poc'), str(active), '--live', str(BASE / 'gateway.local.json')],
                env={**os.environ, 'POC_INTERFACE': 'no-such-nic'}, capture_output=True, text=True, timeout=20)
            assert native.returncode == 2 and 'POC_INTERFACE' in native.stderr, native.stderr
            assert '"event":"sdk_start"' not in native.stdout
            result = {'cold_restart_same_entry_options_and_child_sns': True, 'no_sdk_process': True,
                      'native_shared_KV_guard_passed_before_network_refusal': True}
        else:
            source = original['devices'][0]
            flow = await flow_start(hass, entry, 'role_binding')
            flow = await advance(hass, flow, {'device': source['id']}, 'managed_layout')
            flow = await advance(hass, flow, {'layout': 'switches'}, 'managed_cards')
            cards = ['fan-controls-0', 'fan-controls-1', 'light', 'aux', 'timer-controls']
            flow = await advance(hass, flow, {'cards': cards}, 'managed_roles')
            # 测试 HA 实际可序列化的 EntitySelector Schema，而非自制表单。
            from homeassistant.helpers import selector
            for field in flow['data_schema'].schema.values():
                assert isinstance(field, selector.EntitySelector)
                field.serialize()
            required = {str(key) for key in flow['data_schema'].schema}
            roles = {key: source['roles'][key] for key in required}
            bad = {**roles, 'fan': 'fan.oh_single_foreign'}
            try:
                await hass.config_entries.options.async_configure(flow['flow_id'], bad)
            except InvalidData:
                pass
            else:
                raise AssertionError('entity selector accepted a foreign entity')
            assert OPTION not in entry.options
            flow = await advance(hass, flow, roles, 'managed_confirm')
            assert 'card_count' in flow['description_placeholders']
            result = await hass.config_entries.options.async_configure(flow['flow_id'], {'confirm': True})
            assert result['type'] == 'create_entry'
            await hass.async_block_till_done()
            assert entry.state == ConfigEntryState.LOADED and entry.options['backend'] == 'disabled'
            document = manager.document(entry.options[OPTION])
            router, notes = await manager.preview(document, entry.entry_id)
            key = source.get('key_prefix', '') + 'fan-controls-0'
            await hass.async_add_executor_job(router.write, key, 'switch2', {'on': 1})
            assert hass.states.get(roles['fan']).attributes['percentage'] == 37
            assert (await hass.async_add_executor_job(router.snapshot, key))['switch2']['on'] == 1
            # 新增一个仅存在于此无网络容器的标准灯，走完整设备选择和角色绑定。
            from homeassistant.helpers import device_registry as dr, entity_registry as er
            light = 'light.oh_single_managed_added'
            fixture = hass.data['hwhomebridge_virtual_fixture']
            fixture['states'][light] = {'state': 'off', 'attributes': {
                'brightness': 128, 'color_temp_kelvin': 4000,
                'min_color_temp_kelvin': 2700, 'max_color_temp_kelvin': 6500,
                'supported_color_modes': ['color_temp']}}
            device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                identifiers={('hwhomebridge_fixture', 'management-added-light')},
                name='Management virtual light', model='virtual-standard-light')
            er.async_get(hass).async_get_or_create('light', 'hwhomebridge_fixture', light,
                config_entry=entry, device_id=device.id, suggested_object_id=light.split('.')[1])
            fixture['sync']()
            flow = await flow_start(hass, entry, 'device_management')
            flow = await advance(hass, flow, {'devices': original['enabled'] + [device.id]}, 'managed_roles')
            flow = await advance(hass, flow, {'main': light}, 'managed_confirm')
            result = await hass.config_entries.options.async_configure(flow['flow_id'], {'confirm': True})
            assert result['type'] == 'create_entry'
            await hass.async_block_till_done()
            router, _ = await manager.preview(entry.options[OPTION], entry.entry_id)
            light_key = 'device-' + hashlib.sha256(device.id.encode()).hexdigest()[:16]
            await hass.async_add_executor_job(router.write, light_key, 'switch', {'on': 1})
            assert hass.states.get(light).state == 'on'
            assert (await hass.async_add_executor_job(router.snapshot, light_key))['switch']['on'] == 1
            # 两个并行页面只允许第一个保存，拒绝用旧表单覆盖更新。
            stale = await flow_start(hass, entry, 'device_management')
            active = await flow_start(hass, entry, 'device_management')
            active = await advance(hass, active, {'devices': []}, 'managed_confirm')
            result = await hass.config_entries.options.async_configure(active['flow_id'], {'confirm': True})
            assert result['type'] == 'create_entry'
            await hass.async_block_till_done()
            stale = await hass.config_entries.options.async_configure(stale['flow_id'], {'devices': original['enabled']})
            assert stale['errors']['base'] == 'invalid_managed_plan'
            router, _ = await manager.preview(entry.options[OPTION], entry.entry_id)
            assert light_key not in {row['key'] for row in router.catalog}, 'unpaired disabled child would be registered'
            calls = (CONFIG / 'fixture-service-calls.private.jsonl').read_bytes()
            try:
                await hass.async_add_executor_job(router.write, key, 'switch1', {'on': 1})
            except SourceUnavailable:
                pass
            else:
                raise AssertionError('disabled card accepted control')
            assert calls == (CONFIG / 'fixture-service-calls.private.jsonl').read_bytes()
            active_path = await hass.async_add_executor_job(prepare_runtime, manager.spec, router.catalog)
            assert (active_path / 'sdk-state').resolve() == (BASE / 'runtime/sdk-state').resolve()
            result = {'native_HA_forms': True, 'foreign_role_rejected': True,
                      'explicit_layout_and_cards_saved': True, 'real_HA_service_and_readback': True,
                      'new_standard_light_same_parent': True,
                      'concurrent_stale_form_rejected': True, 'disabled_zero_writes': True,
                      'shared_original_KV': True, 'entry_identity': list(identity),
                      'child_sns': {row['key']: row['sn'] for row in router.catalog},
                      'template_limits_shown': bool(notes)}
            save(CONFIG / 'management-result.private.json', result)
        assert len(hass.config_entries.async_entries('hwhomebridge')) == 1
        assert (entry.entry_id, entry.unique_id) == identity
        assert not child_processes() and frozen() == before
        result.update(one_entry_and_parent=True, frozen_identity_unchanged=True, network_isolated=True)
        save(CONFIG / ('management-' + phase + '.private.json'), result)
        print('MANAGEMENT_ACCEPTANCE', phase, 'PASS', flush=True)
    finally:
        await hass.async_stop()


if __name__ == '__main__':
    asyncio.run(exercise(sys.argv[1]))
