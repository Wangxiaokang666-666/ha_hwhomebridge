"""只操作 oh_b9 虚拟实体；App 写入必须经实际 HA、C 和云端/局域网链路。"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import time
import uuid

from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.config_entries import ConfigEntryState

CONFIG = Path('/config')
LAB = CONFIG / '.storage/hwhomebridge/openharmony/lab'


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def native_children():
    result = []
    children = Path(f'/proc/{os.getpid()}/task/{os.getpid()}/children').read_text().split()
    for child in children:
        try:
            command = (Path('/proc') / child / 'cmdline').read_bytes()
        except FileNotFoundError:
            continue
        if b'bridge_poc' in command or b'gateway_worker.py' in command:
            result.append(int(child))
    return result


async def main():
    assert json.loads((CONFIG / 'fixture-scope.json').read_text()) == {
        'purpose': 'b9-four-key-app-virtual-only', 'production_access': False}
    root = json.loads((LAB / 'gateway.local.json').read_text())
    assert root['sn'].startswith('OHPOC-') and root['devName'] == 'TEST-B9-FOURKEY'
    roles = json.loads((LAB / 'instances.json').read_text())['devices'][0]['roles']
    assert all(entity.split('.')[1].startswith('oh_b9_') for entity in roles.values())
    states_path = CONFIG / 'virtual-states.private.json'
    if states_path.exists():
        states = json.loads(states_path.read_text())
    else:
        states = {entity: {'state': '0' if entity.startswith(('number.', 'sensor.')) else 'off',
                          'attributes': {}} for entity in roles.values()}
        states[roles['fan']]['attributes'] = {'percentage': 0, 'direction': 'reverse'}
        save(states_path, states)
    assert set(states) == set(roles.values())
    hass = HomeAssistant('/config')
    loader.async_setup(hass)
    hass.config.skip_pip = True
    audit = CONFIG / 'service-calls.private.jsonl'
    offset = audit.stat().st_size if audit.exists() else 0
    native = LAB / 'runtime/ha-sdk.private.log'
    native_offset = native.stat().st_size if native.exists() else 0
    run_id = uuid.uuid4().hex

    def sync():
        for entity, state in states.items():
            hass.states.async_set(entity, state['state'], state['attributes'])
        save(states_path, states)

    async def service(call):
        entity = call.data['entity_id']
        assert entity in states and entity.startswith('fan.oh_b9_')
        state = states[entity]
        values = {key: value for key, value in call.data.items() if key != 'entity_id'}
        if call.service == 'turn_on':
            state['state'] = 'on'
        elif call.service == 'turn_off':
            state['state'] = 'off'
        elif call.service == 'set_percentage':
            state['state'] = 'on' if values['percentage'] else 'off'
        elif call.service != 'set_direction':
            raise ValueError('service outside fan fixture')
        state['attributes'].update(values)
        with audit.open('a') as stream:
            stream.write(json.dumps({'run_id': run_id, 'time': time.time(), 'service': call.service,
                                     'entity': entity, 'values': values}) + '\n')
        sync()

    sync()
    assert await bootstrap.async_from_config_dict({'homeassistant': {
        'name': 'TEST-B9 isolated fan', 'latitude': 0, 'longitude': 0, 'elevation': 0,
        'unit_system': 'metric', 'time_zone': 'Asia/Shanghai'}}, hass) is hass
    assert await async_setup_component(hass, 'fan', {})
    for action in ('turn_on', 'turn_off', 'set_percentage', 'set_direction'):
        hass.services.async_register('fan', action, service)
    await hass.async_start()
    entries = hass.config_entries.async_entries('hwhomebridge')
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state == ConfigEntryState.LOADED
    channel = CONFIG / 'app-acceptance'
    channel.mkdir(mode=0o700, exist_ok=True)
    stopped = asyncio.Event()
    for name in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(name, stopped.set)

    async def select(mode):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        flow = await hass.config_entries.options.async_configure(flow['flow_id'], {'action': 'backend'})
        flow = await hass.config_entries.options.async_configure(flow['flow_id'], {'backend': mode})
        if mode == 'openharmony':
            flow = await hass.config_entries.options.async_configure(flow['flow_id'], {
                'openharmony_directory': '.storage/hwhomebridge/openharmony/lab'})
        assert flow['type'] == 'create_entry'
        await hass.async_block_till_done()

    def status():
        current = hass.data['hwhomebridge'].get(entry.entry_id, {})
        calls = []
        if audit.exists():
            with audit.open() as stream:
                stream.seek(offset)
                calls = [json.loads(line) for line in stream if line.strip()]
        confirmations = []
        if native.exists():
            with native.open() as stream:
                stream.seek(native_offset)
                for line in stream:
                    if line.startswith('HA '):
                        row = json.loads(line[3:])
                        if row.get('write_confirmed') is True:
                            confirmations.append({'key': row['key'], 'sid': row['sid']})
        return {'run_id': run_id, 'HA_pid': os.getpid(), 'entry_id': entry.entry_id,
                'parent_sha256': hashlib.sha256(root['sn'].encode()).hexdigest(),
                'backend': current.get('backend'),
                'native_owners': len(native_children()),
                'states': {entity: {'state': hass.states.get(entity).state,
                                   'attributes': dict(hass.states.get(entity).attributes)} for entity in states},
                'HA_calls': calls,
                'C_confirmations': confirmations}

    save(channel / 'ready.private.json', status())
    try:
        while not stopped.is_set():
            for request in sorted(channel.glob('request-*.json')):
                response = request.with_name(request.name.replace('request-', 'response-', 1))
                if response.exists():
                    continue
                try:
                    if request.stat().st_size > 1024:
                        raise ValueError('bounded commands required')
                    value = json.loads(request.read_text())
                    if value == {'operation': 'mark'}:
                        offset = audit.stat().st_size if audit.exists() else 0
                        native_offset = native.stat().st_size if native.exists() else 0
                    elif value == {'operation': 'preflight'}:
                        from custom_components.hwhomebridge.openharmony.deployment import load_deployment
                        spec = await hass.async_add_executor_job(load_deployment, '/config',
                            '.storage/hwhomebridge/openharmony/lab')
                        assert set(spec.router.devices) == {'fan-controls-0', 'fan-controls-1'}
                    elif value == {'operation': 'activate'}:
                        await select('openharmony')
                    elif value == {'operation': 'disable'}:
                        await select('disabled')
                    elif value == {'operation': 'stop'}:
                        stopped.set()
                    elif value != {'operation': 'status'}:
                        raise ValueError('unsupported fixture command')
                    save(response, {'ok': True, **status()})
                except Exception as error:
                    save(response, {'ok': False, 'error_type': type(error).__name__})
            await asyncio.sleep(.2)
    finally:
        await hass.async_stop()
        assert not native_children()
        save(channel / 'stopped.private.json', {'all_native_owners_stopped': True, 'run_id': run_id})


if __name__ == '__main__':
    os.umask(0o077)
    asyncio.run(main())
