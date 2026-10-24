"""唯一引擎评估宿主：真实 HA Core + 公共路由，只允许本机虚构实体。"""
import argparse
import asyncio
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'tests'))
from framework_imports import COMPONENT, load


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.chmod(0o600)
    temporary.replace(path)


def private_root(root):
    stat = root.lstat()
    if root.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError('private owned evaluation directory required')
    marker = json.loads((root / 'scope.json').read_text())
    if marker != {'version': 1, 'purpose': 'single-engine-virtual-only', 'production_access': False}:
        raise ValueError('virtual-only evaluation marker required')


def prepare(root):
    if root.exists():
        raise ValueError('new independent evaluation directory required')
    root.mkdir(parents=True, mode=0o700)
    save(root / 'scope.json', {'version': 1, 'purpose': 'single-engine-virtual-only', 'production_access': False})
    configuration = root / 'mapping'
    shutil.copytree(COMPONENT / 'openharmony/config', configuration)
    source = Path(__file__).parent
    profile = json.loads((source / 'evidence/9MU7-profile.json').read_text())
    services = {svc['serviceId']: svc for svc in profile['services']}
    old = next(row for row in json.loads((COMPONENT / 'config/product_registry.json').read_text())['products'] if row['pid'] == '002')
    for sid, definition in old['services'].items():
        definition['characteristics'] = {}
        for characteristic in services[sid]['characteristics']:
            schema = {'type': characteristic['characteristicType'], 'method': characteristic['method']}
            for name in ('min', 'max'):
                if name in characteristic:
                    schema[name] = int(characteristic[name])
            if 'enumList' in characteristic:
                schema['values'] = [int(item['enumVal']) for item in characteristic['enumList']]
            definition['characteristics'][characteristic['characteristicName']] = schema
    framework = json.loads((configuration / 'product_registry.json').read_text())
    framework['products'].append(old)
    # 主灯开关保留在旧002卡上，补充卡不再注册第二个同实体开关。
    next(row for row in framework['products'] if row['pid'] == '2NAL')['services'].pop('switch')
    save(configuration / 'product_registry.json', framework)
    supplement = json.loads((configuration / 'adapters/default/2NAL.json').read_text())
    supplement['services'].pop('switch')
    save(configuration / 'adapters/default/2NAL.json', supplement)
    default = json.loads((COMPONENT / 'config/adapters/default/002.json').read_text())
    for mapping in default['services'].values():
        mapping['role'] = 'main'
    save(configuration / 'adapters/default/002.json', default)
    bundles = json.loads((configuration / 'compositions.json').read_text())
    card = next(card for card in bundles['bundles']['xiaomi_sflt11'] if card['key'] == 'light')
    card.update(pid='002', primary=True)
    save(configuration / 'compositions.json', bundles)
    instance = json.loads((configuration / 'instances.example.json').read_text())
    instance.pop('ha', None)
    # 保持16字符，便于与旧SDK的device_sn文件合同做独立对照；不是生产身份。
    parent = 'OHPOC-' + secrets.token_hex(5)
    instance['parent_sn'] = parent
    for device in instance['devices']:
        device['id'] = 'single-engine-virtual-' + parent
        device['roles'] = {role: entity.split('.')[0] + '.oh_single_' + role for role, entity in device['roles'].items()}
    save(root / 'instances.json', instance)
    (root / 'runtime').mkdir(mode=0o700)
    (root / 'legacy').mkdir(mode=0o700)
    (root / 'ha').mkdir(mode=0o700)
    for name in ('hilink.cfg', 'hilink_bak.cfg'):
        shutil.copyfile(COMPONENT / 'hilink_bridge/config' / name, root / 'legacy' / name)
        (root / 'legacy' / name).chmod(0o600)
    (root / 'legacy/device_sn').write_bytes(parent.encode() + b'\0')
    (root / 'legacy/device_sn').chmod(0o600)
    (root / 'legacy/device_ac').write_bytes(secrets.token_bytes(48))
    (root / 'legacy/device_ac').chmod(0o600)
    shutil.copyfile(source / 'assets/huawei-cbg-root-ca.pem', root / 'ca.pem')
    save(root / 'gateway.local.json', {'sn': parent, 'prodId': 'ZG37', 'model': 'DL-01W',
        'devTypeId': 'A17', 'devTypeName': 'Smart Bridge', 'manuId': '002', 'manuName': 'DALEN',
        'devName': 'Single engine virtual TEST', 'protType': 1,
        'pin': str(secrets.randbelow(90000000) + 10000000), 'pin_expires_at': int(time.time()) + 86400,
        'ac_key_hex': (root / 'legacy/device_ac').read_bytes().hex(), 'ca_file': str(root / 'ca.pem'),
        'enable_virtual_child': True, 'enable_ha_children': True})
    for path in root.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    print(json.dumps({'prepared': True, 'virtual_only': True, 'source_HA_credentials': False}))


async def serve(root, port):
    from test_role_timer_composition import DeviceTransport
    from homeassistant import loader
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry, entity_registry
    private_root(root)
    lock = (root / 'backend.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    instance = json.loads((root / 'instances.json').read_text())
    source_id = instance['devices'][0]['id']
    if not source_id.startswith('single-engine-virtual-'):
        raise ValueError('virtual device identifier required')
    roles = instance['devices'][0]['roles']
    if 'ha' in instance or not all(entity.split('.')[1].startswith('oh_single_') for entity in roles.values()):
        raise ValueError('only virtual role inventory is allowed')
    hass = HomeAssistant(str(root / 'ha'))
    loader.async_setup(hass)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    actor = DeviceTransport(instance)
    from legacy_category_fixture import add_states, install
    add_states(root, actor)
    actor.bind(actor.data)
    state_file = root / 'virtual-device-state.private.json'
    if state_file.exists():
        restored = json.loads(state_file.read_text())
        if set(restored) != set(actor.data):
            raise ValueError('virtual device state inventory mismatch')
        actor.data = restored
    for role in ('fan_timer_duration', 'light_timer_duration'):
        actor.data[roles[role]]['attributes'].update(min=0, max=720, step=1)
    def sync():
        for entity, state in actor.data.items():
            hass.states.async_set(entity, state['state'], state['attributes'])
        save(state_file, actor.data)
    sync()
    async def service(call):
        if call.data['entity_id'] not in actor.allowed:
            raise ValueError('entity outside virtual fixture')
        if call.domain == 'select' and call.service == 'select_option':
            actor.data[call.data['entity_id']]['state'] = call.data['option']
        else:
            actor.call(call.domain, call.service, call.data['entity_id'], **{key: value for key, value in call.data.items() if key != 'entity_id'})
        sync()
    transport_module = load('ha_transport')
    for domain, actions in transport_module.ScopedTransport.ACTIONS.items():
        for action in actions:
            hass.services.async_register(domain, action, service)
    hass.services.async_register('select', 'select_option', service)
    previous = load('service_router').ServiceRouter()
    previous.set_hass(hass)
    if not await previous.load_product_registry(str(COMPONENT / 'config/product_registry.json')):
        raise ValueError('legacy registry unavailable')
    primary = previous.register_device(source_id, 'virtual study light',
        [{'entity_id': roles['main'], 'domain': 'light'}], model='virtual-light')
    if primary is None or primary.product.pid != '002':
        raise ValueError('legacy light product changed')
    Router = load('capability_router').CapabilityRouter
    Registry = load('openharmony.configuration').OpenHarmonyRegistry
    class EvaluationRouter(Router):
        registry_class = Registry
    router = await EvaluationRouter.create(transport_module.HomeAssistantTransport(hass), root / 'mapping', instance,
        owned_entities={source_id: set(roles.values())},
        supported_pids={item['pid'] for item in json.loads((root / 'mapping/product_registry.json').read_text())['products']},
        sn_manager=previous.sn_manager,
        sn_factory=lambda parent, source, key: load('openharmony.configuration').child_sn(parent, key))
    router.set_hass(hass)
    router.settle_seconds = router.fan_settle_seconds = 0
    assert router.preserved_cards == {'light': primary}
    if primary.sn in {device.sn for device in router.devices.values()}:
        raise ValueError('duplicate old main light')
    catalog = router.registry.catalog(router.devices)
    for row in catalog:
        row['sn'] = router.devices[row['key']].sn
    legacy_bridges, legacy_catalog = install(root, previous, primary)
    catalog.extend(legacy_catalog)
    save(root / 'runtime/ha-children.local.json', catalog)
    save(root / 'catalog-proof.json', {'one_parent': True, 'child_count': len(catalog),
        'old_framework_pid': primary.product.pid, 'old_cloud_pid': '9MU7',
        'old_sn_sha256': hashlib.sha256(primary.sn.encode()).hexdigest(),
        'old_SN_algorithm_unchanged': True, 'old_primary_object_preserved': router.preserved_cards['light'] is primary,
        'duplicate_main_light': False, 'new_root_created_by_backend': False})
    request_lock = threading.Lock()
    def snapshot(key):
        if key in legacy_bridges:
            return legacy_bridges[key].snapshot()
        return router.snapshot(key)
    def invoke(data):
        key = data['key']
        if key not in {row['key'] for row in catalog}:
            raise ValueError('unknown child')
        if 'sid' in data:
            sid, payload = data['sid'], data['data']
            if key in legacy_bridges:
                legacy_bridges[key].write(sid, payload)
                deadline = time.monotonic() + 5
                def confirmed():
                    actual = snapshot(key)[sid]
                    if sid == 'delay':
                        return actual['action'] == (2 if payload['action'] == 2 else 0)
                    # 原路由将亮度量化到255级；读取原值，不回显请求值。
                    return all(name in actual and abs(actual[name] - value) <= (1 if sid == 'brightness' else 0)
                               for name, value in payload.items())
                while not confirmed():
                    if time.monotonic() > deadline:
                        raise TimeoutError('legacy HA readback not confirmed')
                    time.sleep(.05)
            else:
                router.write(key, sid, payload)
        return snapshot(key)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            data = {}
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if self.path != '/bridge' or not 0 < size <= 16384:
                    raise ValueError('invalid local request')
                data = json.loads(self.rfile.read(size))
                with request_lock:
                    response = {'ok': True, 'state': invoke(data)}
            except (ValueError, KeyError, TypeError, TimeoutError) as error:
                save(root / 'last-rejected-request.private.json', {'request': data, 'error': str(error)})
                response = {'ok': False, 'error': 'virtual fixture rejected request'}
            encoded = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    save(root / 'backend-status.json', {'pid': os.getpid(), 'port': server.server_port, 'ready': True,
        'virtual_only': True, 'one_parent': True, 'cards': [row['key'] for row in catalog]})
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        await hass.async_stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'serve'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--port', type=int, default=18089)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'prepare':
        prepare(args.directory.resolve())
    else:
        asyncio.run(serve(args.directory.resolve(), args.port))
