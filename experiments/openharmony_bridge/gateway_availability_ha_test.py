"""独立 HA 和真实两种 SDK 的掉线恢复验收；仅接受已标记的虚构实体夹具。"""
import argparse
import asyncio
import json
import logging
import os

from gateway_ha_acceptance import (BASE, CONFIG, make_hass, options, post, runtime,
    save, snapshot, child_processes, wait_active)


async def exercise(phase, native_only=False):
    # 在 HA 启动前就让一个旧灯离线；不能在引擎已启动后才注入。
    assert json.loads((CONFIG / 'fixture-scope.json').read_text()) == {
        'purpose': 'existing-gateway-ha-lifecycle-virtual-only', 'production_access': False}
    rows = json.loads((BASE / 'bindings.json').read_text())['devices']
    row = next(row for row in rows if row['pid'] == '001')
    entity = row['entities']['switch']
    assert entity.startswith('light.oh_single_')
    before = snapshot()
    if phase == 'exercise':
        states = json.loads((CONFIG / 'fixture-states.private.json').read_text())
        save(CONFIG / 'availability-original-state.private.json', states[entity])
        states[entity] = {**states[entity], 'state': 'unavailable'}
        save(CONFIG / 'fixture-states.private.json', states)
    original = json.loads((CONFIG / 'availability-original-state.private.json').read_text())
    hass, entry = await make_hass()
    identity = (entry.entry_id, entry.unique_id)
    fixture = hass.data['hwhomebridge_virtual_fixture']
    result = {'phase': phase, 'passed': False, 'virtual_only': True}
    def change(available):
        fixture['states'][entity] = original if available else {**original, 'state': 'unavailable'}
        fixture['sync']()
    async def check_offline():
        response = await post(hass, {'key': row['key']})
        assert response == {'ok': False, 'available': False, 'error': 'SourceUnavailable'}
        response = await post(hass, {'key': row['key'], 'sid': 'switch', 'data': {'on': 1}})
        assert response.get('available') is False and not response['ok']
        response = await post(hass, {'key': 'legacy-light', 'sid': 'switch', 'data': {'on': 1}})
        assert response['ok'] and response['state']['switch']['on'] == 1
    async def wait_native_status(available):
        for _ in range(300):
            path = BASE / 'openharmony.private.log'
            lines = path.read_text().splitlines() if path.exists() else []
            confirmed = []
            for line in lines:
                if not line.startswith('HA '):
                    continue
                try:
                    item = json.loads(line[3:])
                except ValueError:
                    continue
                if item.get('key') == row['key'] and item.get('accepted') is True:
                    confirmed.append(item['available'])
            if confirmed and confirmed[-1] is available:
                return
            await asyncio.sleep(.2)
        raise TimeoutError('child availability not acknowledged by cloud')
    try:
        if phase == 'exercise':
            save(CONFIG / 'availability-entry.private.json', {'entry_id': entry.entry_id, 'unique_id': entry.unique_id})
            if not native_only:
                assert (await options(hass, entry, 'legacy'))['type'] == 'create_entry'
                current = await wait_active(hass, entry, 'legacy')
                await check_offline()
                status = json.loads((BASE / 'legacy-status.json').read_text())
                assert status['unavailable_children'] == 1 and status['available_children'] == 7
                result['real_legacy_starts_with_one_of_eight_children_offline'] = True
            await options(hass, entry, 'openharmony')
            current = await wait_active(hass, entry, 'openharmony')
            await check_offline(); await wait_native_status(False)
            result['real_native_cloud_acknowledges_child_offline'] = True
            for available in (True, False, True):
                change(available)
                await wait_native_status(available)
                response = await post(hass, {'key': row['key']})
                assert response['ok'] is available
            result['runtime_drop_and_identical_state_recovery'] = True
            response = await post(hass, {'key': 'fan-wifi', 'sid': 'fan', 'data': {'speed': 6}})
            assert response['ok'] and response['state']['fan']['speed'] == 6
            result['same_gateway_real_HA_fan_write_readback'] = True
            change(False); await wait_native_status(False)
            current.process.kill()
            expected_backend = 'disabled' if native_only else 'legacy'
            current = await wait_active(hass, entry, expected_backend)
            assert current.state['failed_backend'] == 'openharmony'
            await check_offline()
            result['unverified_ARM_legacy_stays_disabled' if native_only else
                'native_exit_falls_back_while_old_child_offline'] = True
            if native_only:
                assert not child_processes()
            result['real_legacy_engine_verified'] = not native_only
            await options(hass, entry, 'openharmony')
            await wait_active(hass, entry, 'openharmony')
            await wait_native_status(False)
        elif phase == 'failback':
            current = await wait_active(hass, entry, 'openharmony')
            current.process.kill()
            current = await wait_active(hass, entry, 'legacy')
            assert current.state['failed_backend'] == 'openharmony'
            await check_offline()
            result['real_legacy_failback_persisted_for_cold_HA_restart'] = True
        else:
            current = await wait_active(hass, entry, 'openharmony')
            assert fixture['states'][entity]['state'] == 'unavailable'
            await check_offline(); await wait_native_status(False)
            result['HA_restart_retains_engine_with_offline_child'] = True
            change(True); await wait_native_status(True)
            response = await post(hass, {'key': row['key'], 'sid': 'switch', 'data': {'on': 1}})
            assert response['ok'] and response['state']['switch']['on'] == 1
            result['recovered_old_light_write_readback'] = True
        previous = json.loads((CONFIG / 'availability-entry.private.json').read_text())
        assert identity == (previous['entry_id'], previous['unique_id'])
        assert len(hass.config_entries.async_entries('hwhomebridge')) == 1
        assert len(child_processes()) == 1 and snapshot() == before
        result.update(passed=True, original_entry_preserved=True, paired_identity_files_unchanged=True,
            one_native_owner=True, real_cloud_verified=True, real_App_verified=False)
    finally:
        await hass.async_stop()
    assert not child_processes()
    result['workers_stopped_with_HA'] = True
    save(CONFIG / ('availability-' + phase + '-result.json'), result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['exercise', 'restart', 'failback'])
    parser.add_argument('--native-only', action='store_true',
        help='ARM 旧库未验真时只验证拒绝回退并停用，不绕过哈希检查')
    args = parser.parse_args()
    os.umask(0o077); logging.basicConfig(level=logging.WARNING)
    asyncio.run(exercise(args.phase, args.native_only))
