"""保持虚构 HA 在线，记录 App 经真实SDK写入；私有文件通道只允许观测和切换。"""
import argparse
import asyncio
import json
import logging
import os
import time
import uuid

from gateway_ha_acceptance import (BASE, CONFIG, make_hass, options, runtime,
    save, snapshot, child_processes, wait_active)


async def main(expected_boot):
    assert json.loads((CONFIG / 'fixture-scope.json').read_text()) == {
        'purpose': 'existing-gateway-ha-lifecycle-virtual-only', 'production_access': False}
    assert json.loads((BASE / 'gateway.local.json').read_text())['sn'].startswith('OHPOC-')
    initial = snapshot()
    acceptance_run_id = uuid.uuid4().hex
    channel = CONFIG / 'app-acceptance'
    channel.mkdir(mode=0o700, exist_ok=True)
    # 在启动 HA/SDK 前取起点，既排除旧确认，也保留启动期间的真实写入。
    audit = CONFIG / 'fixture-service-calls.private.jsonl'
    audit_offset = audit.stat().st_size if audit.exists() else 0
    native_log = BASE / 'openharmony.private.log'
    log_offset = native_log.stat().st_size if native_log.exists() else 0
    hass, entry = await make_hass()
    fixture = hass.data['hwhomebridge_virtual_fixture']
    roles = json.loads((BASE / 'instances.json').read_text())['devices'][0]['roles']
    assert all(entity.split('.')[1].startswith('oh_single_') for entity in fixture['states'])
    observations = []
    observation_offset = 0
    def observe_writes(current):
        router = current.router
        if getattr(router, '_acceptance_observed', False):
            return
        original_write = router.write
        def observed_write(key, sid, data):
            started = time.monotonic()
            row = {'key': key, 'sid': sid, 'error_type': None}
            try:
                return original_write(key, sid, data)
            except Exception as error:
                row['error_type'] = type(error).__name__
                raise
            finally:
                row['elapsed_seconds'] = round(time.monotonic() - started, 3)
                observations.append(row)
        router.write = observed_write
        router._acceptance_observed = True
    def status():
        current = runtime(hass, entry)
        calls = []
        if audit.exists():
            with audit.open() as stream:
                stream.seek(audit_offset)
                calls = [json.loads(line) for line in stream if line.strip()]
        writes = []
        backend_results = []
        path = BASE / 'openharmony.private.log'
        if path.exists():
            with path.open() as stream:
                stream.seek(log_offset)
                for line in stream:
                    if not line.startswith('HA '):
                        continue
                    try:
                        row = json.loads(line[3:])
                    except ValueError:
                        continue
                    if row.get('write_confirmed') is True:
                        writes.append({'key': row['key'], 'sid': row['sid']})
                    if row.get('event') == 'backend_result' and row.get('operation') == 'write':
                        backend_results.append({key: row[key] for key in (
                            'request_id', 'operation', 'reason', 'error_type', 'errno', 'elapsed_ms')
                            if key in row})
        legacy = BASE / 'legacy-status.json'
        legacy_status = json.loads(legacy.read_text()) if legacy.exists() else {}
        return {'acceptance_run_id': acceptance_run_id,
            'legacy_sdk_status': legacy_status, 'time': time.time(), 'backend': current.active if current else 'disabled',
            'native_owners': len(child_processes()), 'states': {entity: {
                'state': hass.states.get(entity).state, 'attributes': dict(hass.states.get(entity).attributes)}
                for entity in fixture['states']}, 'HA_service_calls_since_mark': calls,
            'loopback_write_observations': observations[observation_offset:],
            'native_confirmed_writes_since_mark': writes,
            'native_backend_results_since_mark': backend_results,
            'paired_files_unchanged': snapshot() == initial}
    try:
        current = await wait_active(hass, entry, expected_boot)
        observe_writes(current)
        previous = json.loads((CONFIG / 'availability-entry.private.json').read_text())
        assert (entry.entry_id, entry.unique_id) == (previous['entry_id'], previous['unique_id'])
        if expected_boot == 'legacy':
            assert current.state['failed_backend'] == 'openharmony'
        save(channel / 'ready.private.json', {**status(), 'selected_engine_restored_on_cold_HA_boot': True,
            'same_configuration_entry': True})
        running = True
        while running:
            for request in sorted(channel.glob('request-*.json')):
                response = request.with_name(request.name.replace('request-', 'response-', 1))
                if response.exists():
                    continue
                try:
                    if request.stat().st_size > 4096:
                        raise ValueError('bounded test command required')
                    value = json.loads(request.read_text())
                    if value == {'operation': 'status'}:
                        pass
                    elif value == {'operation': 'mark'}:
                        audit_offset = audit.stat().st_size if audit.exists() else 0
                        observation_offset = len(observations)
                        path = BASE / 'openharmony.private.log'
                        log_offset = path.stat().st_size if path.exists() else 0
                    elif set(value) == {'operation', 'backend'} and value['operation'] == 'backend':
                        if value['backend'] not in ('legacy', 'openharmony', 'disabled'):
                            raise ValueError('explicit validated backend required')
                        await options(hass, entry, value['backend'])
                        if value['backend'] != 'disabled':
                            observe_writes(await wait_active(hass, entry, value['backend']))
                    elif value == {'operation': 'reset_virtual_device'}:
                        for entity in set(roles.values()):
                            state = fixture['states'][entity]
                            if entity.startswith(('light.', 'fan.', 'switch.')):
                                state['state'] = 'off'
                            elif entity.startswith(('number.', 'sensor.')):
                                state['state'] = '0'
                        fixture['states'][roles['main']]['attributes'].update(brightness=128, color_temp_kelvin=4000)
                        fixture['states'][roles['fan']]['attributes'].update(percentage=0, direction='reverse')
                        fixture['sync']()
                    elif value == {'operation': 'stop'}:
                        running = False
                    else:
                        raise ValueError('unsupported virtual test command')
                    save(response, {'ok': True, **status()})
                except Exception as error:
                    save(response, {'ok': False, 'error': type(error).__name__})
            await asyncio.sleep(.2)
    finally:
        await hass.async_stop()
    assert not child_processes() and snapshot() == initial
    save(channel / 'loopback-write-observations.private.json', observations)
    save(channel / 'stopped.json', {'passed': True, 'all_native_owners_stopped': True, 'paired_files_unchanged': True})


if __name__ == '__main__':
    os.umask(0o077); logging.basicConfig(level=logging.WARNING)
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-boot', choices=('legacy', 'openharmony'), required=True)
    args = parser.parse_args()
    asyncio.run(main(args.expected_boot))
