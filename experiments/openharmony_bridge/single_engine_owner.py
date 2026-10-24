"""独立虚拟实验的互斥 SDK 所有者；正常停止不会调用解绑接口。"""
import argparse
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import urllib.request

from single_engine_fixture import load, private_root, save


LegacyCallbacks = load('legacy_callbacks').LegacyCallbacks


def verified_import(root, directory, binary, gateway):
    """显式实验接管：源快照、原生解密结果与旧子设备ID必须同时一致。"""
    if directory.parent != root / 'offline-import' or directory.is_symlink():
        raise ValueError('import must be inside the independent evaluation directory')
    receipt = json.loads((directory / 'import-receipt.json').read_text())
    if receipt.get('version') not in (1, 2) or set(receipt['source_sha256']) != {'config.cfg', 'bridge.cfg'}:
        raise ValueError('unsupported offline import receipt')
    for path in directory.rglob('*'):
        info = path.lstat()
        if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private owned import artifacts required')
    for name, expected in receipt['source_sha256'].items():
        if name not in ('config.cfg', 'bridge.cfg') or hashlib.sha256((root / 'legacy' / name).read_bytes()).hexdigest() != expected:
            raise ValueError('legacy paired source changed after offline conversion')
    imported = json.loads((directory / 'gateway.local.json').read_text())
    if any(imported[key] != gateway[key] for key in ('sn', 'ac_key_hex', 'prodId', 'model', 'manuName')):
        raise ValueError('imported gateway identity mismatch')
    result = subprocess.run([str(binary.parent / 'identity_state_probe'), str(directory / 'sdk-state'),
                             str(directory / 'gateway.local.json')], capture_output=True, text=True, check=True, timeout=15)
    rows = [json.loads(line[15:]) for line in result.stdout.splitlines() if line.startswith('IDENTITY_PROBE ')]
    if len(rows) != 1:
        raise ValueError('exactly one native identity result required')
    identity = rows[0]
    if (not identity['bound_identity_loaded'] or identity['dev_id_sha256'] != receipt['root_id_sha256']
            or identity['fields'] != receipt['account_field_sha256']):
        raise ValueError('native reader did not confirm the imported legacy login identity')
    catalog = json.loads((directory / 'ha-children.local.json').read_text())
    if receipt['version'] == 1:
        records = [{'key': 'legacy-light', 'id_sha256': receipt['child_id_sha256'],
                    'sn_sha256': receipt['child_sn_sha256']}]
    else:
        records = receipt['children']
        for path, field in [(root / 'runtime/ha-children.local.json', 'source_catalog_sha256'),
                            (directory / 'ha-children.local.json', 'target_catalog_sha256')]:
            if hashlib.sha256(path.read_bytes()).hexdigest() != receipt[field]:
                raise ValueError('offline catalog changed')
    new_keys = set(receipt.get('new_child_keys', []))
    old_keys = {row['key'] for row in records}
    if (not 1 <= len(records) <= 64 or len(catalog) > 64 or len(catalog) != len(records) + len(new_keys)
            or len(old_keys) != len(records) or old_keys & new_keys
            or old_keys | new_keys != {row['key'] for row in catalog}):
        raise ValueError('import child inventory mismatch')
    source_catalog = {row['key']: row for row in json.loads((root / 'runtime/ha-children.local.json').read_text())}
    for row in catalog:
        if row['key'] in new_keys:
            if row != source_catalog.get(row['key']) or row.get('existing_cloud_only'):
                raise ValueError('new children must exactly match the explicit source configuration')
            continue
        if row.get('existing_cloud_only') is not True:
            raise ValueError('import must use existing cloud children only')
        child = json.loads((directory / ('ha-child-' + row['key'] + '.json')).read_text())
        record = next(value for value in records if value['key'] == row['key'])
        if row['sn'] != child['sn'] or child['parent_sn'] != gateway['sn']:
            raise ValueError('imported child parent or SN mismatch')
        for field, expected in [('devId', 'id_sha256'), ('sn', 'sn_sha256')]:
            if hashlib.sha256(child[field].encode()).hexdigest() != record[expected]:
                raise ValueError('imported child identity mismatch')
    return directory, directory / 'gateway.local.json'


def main(root, backend, binary, import_directory=None):
    private_root(root)
    lock = (root / 'engine-owner.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gateway = json.loads((root / 'gateway.local.json').read_text())
    if not gateway['sn'].startswith('OHPOC-') or len(gateway['sn']) != 16:
        raise ValueError('independent short test identity required')
    runtime, gateway_file = root / 'runtime', root / 'gateway.local.json'
    # 禁止仅复制SN后启动新引擎；显式导入必须由原生离线读取器确认。
    if backend == 'openharmony' and any(
        path.is_file() and path.stat().st_size
        for path in (root / 'legacy/config.cfg', root / 'legacy/config_bak.cfg')
    ):
        if import_directory is None:
            raise ValueError('paired legacy identity import is not verified; new engine refused before startup')
        runtime, gateway_file = verified_import(root, import_directory, binary, gateway)
    state = {'backend': backend, 'pid': os.getpid(), 'root_sn_sha256': hashlib.sha256(gateway['sn'].encode()).hexdigest(),
             'SDK_unbind_called': False, 'virtual_only': True}
    save(root / 'engine-owner.json', state)
    stop = False
    process = None
    def terminate(*_):
        nonlocal stop
        stop = True
        if process is not None:
            process.terminate()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, terminate)
    if backend == 'openharmony':
        env = {**os.environ, 'POC_INTERFACE': 'eth0', 'POC_LAN_PORT': '15684', 'POC_CLOUD_STATUS': '1'}
        process = subprocess.Popen([str(binary), str(runtime), '--live', str(gateway_file)], env=env)
        try:
            result = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        return result

    os.environ['HILINK_CONFIG_DIR'] = str(root / 'legacy') + '/'
    os.chdir(root / 'legacy')
    library = C.CDLL(str(binary))
    library.GetGatewaySN.restype = C.c_char_p
    library.HILINK_SetPreferredIface.argtypes = [C.c_char_p]
    library.HILINK_SetAutoAc.argtypes = [C.POINTER(C.c_ubyte), C.c_uint]
    library.HILINK_SetAutoAc.restype = C.c_int
    library.HilinkSyncBrgDevStatus.argtypes = [C.c_char_p, C.c_int]
    library.UpdateHAStatus.argtypes = [C.c_char_p, C.c_char_p, C.c_char_p]
    library.HILINK_SetPreferredIface(b'eth0')
    ac = (root / 'legacy/device_ac').read_bytes()
    if len(ac) != 48 or ac.hex() != gateway['ac_key_hex']:
        raise ValueError('independent test AC mismatch')
    ac_buffer = (C.c_ubyte * 48).from_buffer_copy(ac)
    if library.HILINK_SetAutoAc(ac_buffer, 48):
        raise ValueError('test SDK AC setup failed')
    catalog = json.loads((root / 'runtime/ha-children.local.json').read_text())
    catalog = [row for row in catalog if 'sdk_index' in row]
    if not catalog or len({row['sn'] for row in catalog}) != len(catalog):
        raise ValueError('unique old SDK test children required')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def bridge(key, sid=None, data=None):
        payload = {'key': key}
        if sid is not None:
            payload.update(sid=sid, data=data)
        request = urllib.request.Request('http://127.0.0.1:18089/bridge', data=json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        response = json.load(opener.open(request, timeout=10))
        if not response['ok']:
            raise ValueError('virtual backend rejected request')
        return response['state']
    for attempt in range(120):
        try:
            snapshots = {row['sn'].encode(): bridge(row['key']) for row in catalog}
            break
        except (OSError, ValueError):
            if stop or attempt == 119:
                raise RuntimeError('virtual backend did not become ready') from None
            time.sleep(.5)
    callbacks = LegacyCallbacks(catalog, gateway, state, snapshots)
    library.RegHomeAssistantPyCB(callbacks.action, callbacks.kind, callbacks.status, callbacks.notify, callbacks.get)
    library.RegPINQueryCallback(callbacks.pin)
    if library.main() != 0:
        raise RuntimeError('legacy test SDK startup failed')
    registered = False
    reported = {}
    next_poll = 0
    while not stop:
        # 主线程拥有正常Python栈；SDK回调只使用上述缓存和有界队列。
        while callbacks.pending:
            try:
                child_sn, raw = callbacks.pending.popleft()
                values = json.loads(raw)
                if not isinstance(values, dict) or not values:
                    raise ValueError('invalid legacy command')
                row = callbacks.children[child_sn]
                cache = callbacks.cache[child_sn]
                # 旧ABI没有SID；仅接受可唯一确定服务的字段组合。
                candidates = [sid for sid, fields in cache.items() if set(values) <= set(fields)]
                if len(candidates) != 1:
                    raise ValueError('ambiguous legacy service')
                callbacks.update(child_sn, bridge(row['key'], candidates[0], values))
                state['write_count'] = state.get('write_count', 0) + 1
            except Exception:
                state['write_rejected'] = state.get('write_rejected', 0) + 1
        if time.monotonic() < next_poll:
            time.sleep(.05)
            continue
        next_poll = time.monotonic() + 1
        for child_sn, row in callbacks.children.items():
            try:
                callbacks.update(child_sn, bridge(row['key']))
            except (OSError, ValueError):
                state['backend_read_failures'] = state.get('backend_read_failures', 0) + 1
        sn = library.GetGatewaySN()
        if sn and sn.decode() != gateway['sn']:
            raise ValueError('legacy SDK did not preserve the supplied test SN')
        state['native_identity_confirmed'] = bool(sn)
        state['device_status'] = library.HILINK_GetDevStatus()
        if state['device_status'] == 1 and not registered:
            saved = root / 'legacy-child-submitted.json'
            previous = json.loads(saved.read_text()) if saved.exists() else {}
            submitted = set(previous.get('children_sn_sha256', [previous.get('sn_sha256')]))
            for child_sn in callbacks.children:
                digest = hashlib.sha256(child_sn).hexdigest()
                if digest not in submitted:
                    library.HilinkSyncBrgDevStatus(child_sn, 3)
                    submitted.add(digest)
                    save(saved, {'children_sn_sha256': sorted(submitted - {None})})
                library.HilinkSyncBrgDevStatus(child_sn, 1)
            registered = True
        if state['device_status'] == 1 and registered:
            for child_sn, services in callbacks.encoded.items():
                for sid, value in services.items():
                    if reported.get((child_sn, sid)) != value:
                        library.UpdateHAStatus(child_sn, sid.encode(), value)
                        reported[child_sn, sid] = value
        state['child_registration_submitted'] = registered
        save(root / 'engine-owner.json', state)
    state['stopped_without_unbind'] = True
    save(root / 'engine-owner.json', state)
    # SDK线程仍驻留，避免Python析构释放其持有的回调；由OS回收整个独立进程。
    os._exit(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('backend', choices=('legacy', 'openharmony'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('binary', type=Path)
    parser.add_argument('--import-directory', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    raise SystemExit(main(args.directory.resolve(), args.backend, args.binary.resolve(),
                         args.import_directory.absolute() if args.import_directory else None))
