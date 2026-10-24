"""M3 安装后、重启后和还原后的真实 HA/SDK 验证；仅使用 oh_flow_ 虚拟实体。"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import sys

import ha_coexistence_test as common
from ha_real_sdk_test import install_real_sdk, check_real_sdk
from homeassistant.config_entries import ConfigEntryState

sys.path.insert(0, '/tools')
from ha_runtime_preflight import activation


def activation_failures(states):
    failures = []
    cases = [(common.LAB / 'profiles/28BF.json', b'{}'),
             (common.LAB / 'runtime/ha-children.local.json', b'[]')]
    for path, invalid in cases:
        saved = path.read_bytes()
        try:
            path.write_bytes(invalid)
            try:
                activation(common.CONFIG, common.DIRECTORY, states)
            except (ValueError, KeyError):
                failures.append(path.name)
            else:
                raise AssertionError('invalid deployment accepted')
        finally:
            path.write_bytes(saved)
    binary = common.LAB / 'bridge_poc'
    binary.chmod(0o600)
    try:
        try:
            activation(common.CONFIG, common.DIRECTORY, states)
        except ValueError:
            failures.append('missing executable permission')
        else:
            raise AssertionError('non-executable accepted')
    finally:
        binary.chmod(0o700)
    settings = common.LAB / 'deployment.json'
    saved = settings.read_bytes()
    try:
        invalid = json.loads(saved)
        invalid['interface'] = 'missing0'
        settings.write_text(json.dumps(invalid))
        try:
            activation(common.CONFIG, common.DIRECTORY, states)
        except ValueError:
            failures.append('missing interface')
        else:
            raise AssertionError('missing interface accepted')
    finally:
        settings.write_bytes(saved)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
        busy.bind(('0.0.0.0', 15684))
        try:
            activation(common.CONFIG, common.DIRECTORY, states)
        except OSError:
            failures.append('occupied LAN port')
        else:
            raise AssertionError('occupied port accepted')
    assert len(failures) == 5
    return failures


async def exercise(phase):
    hass, calls, roles, legacy, sdk, vd = await common.make_hass(install_real_sdk)
    entries = hass.config_entries.async_entries('hwhomebridge')
    old = next(entry for entry in entries if entry.unique_id == 'hwhomebridge')
    assert old.state == ConfigEntryState.LOADED
    result = {'phase': phase, 'passed': False, 'legacy_unbind_calls': 0}
    try:
        if phase == 'restored':
            assert len(entries) == 1
            assert not (common.CONFIG / 'custom_components/hwhomebridge/openharmony').exists()
            for _ in range(150):
                if sdk.library.GetGatewaySN():
                    break
                await asyncio.sleep(.2)
            sdk.verify_identity()
            sdk.verify_runtime()
            assert sdk.starts == 1 and sdk.removals == 0
            result['original_component_loaded_with_same_legacy_identity'] = True
        else:
            await check_real_sdk(hass, sdk, vd, old)
            if phase == 'installed':
                states = {state.entity_id: state.state for state in hass.states.async_all()}
                check = await hass.async_add_executor_job(activation, common.CONFIG, common.DIRECTORY, states)
                assert check['passed'] and check['write_policy'] == 'fan_only'
                result['activation_preflight'] = check
                result['activation_failures_blocked'] = await hass.async_add_executor_job(activation_failures, states)
                shipped = common.CONFIG / 'custom_components/hwhomebridge/openharmony/bin/x86_64/bridge_poc'
                assert shipped.read_bytes() == (common.LAB / 'bridge_poc').read_bytes()
                result['instance_executable_matches_shipped_binary'] = True
                invalid = dict(states)
                del invalid[roles['fan']]
                try:
                    await hass.async_add_executor_job(activation, common.CONFIG, common.DIRECTORY, invalid)
                except ValueError:
                    result['missing_entity_blocks_activation'] = True
                else:
                    raise AssertionError('missing entity accepted')
                response = await common.create_entry(hass, 'openharmony')
                assert response['type'] == 'create_entry'
                new = response['result']
            else:
                new = next(entry for entry in entries if entry != old)
            assert new.state == ConfigEntryState.LOADED
            native = common.runtime(hass, new)
            assert native.process.returncode is None
            if phase == 'installed':
                result['paired_identity_not_used'] = True
                identity = common.hashes()
                (common.CONFIG / 'm3-identity.private.json').write_text(json.dumps(identity))
            else:
                assert common.hashes() == json.loads((common.CONFIG / 'm3-identity.private.json').read_text())
                result['new_gateway_identity_restored_after_HA_restart'] = True
            response = await common.post(hass, native.deployment.port, {'key': 'timer-controls', 'sid': 'fan', 'data': {'gear': 3}})
            assert response['ok'] and hass.states.get(roles['fan']).attributes['percentage'] == 50
            count = len(calls)
            response = await common.post(hass, native.deployment.port, {'key': 'light', 'sid': 'switch', 'data': {'on': 1}})
            assert not response['ok'] and len(calls) == count
            result['virtual_fan_write_readback_and_light_block'] = True
            await check_real_sdk(hass, sdk, vd, old)
        result.update(passed=True, legacy_starts=sdk.starts, legacy_unbind_calls=sdk.removals)
    finally:
        await hass.async_stop()
    assert sdk.removals == 0, 'HA stop must not unbind the legacy gateway'
    if phase != 'restored':
        assert native.process.returncode is not None
        result['C_stopped_with_HA'] = True
    result['legacy_unbind_calls'] = sdk.removals
    (common.CONFIG / f'm3-{phase}-results.json').write_text(json.dumps(result, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('installed', 'restarted', 'restored'))
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(exercise(args.phase))
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
