"""可回收的Linux SDK所有者。HA退出时由内核终止，不调用解绑接口。"""
import ctypes as C
import json
import os
from pathlib import Path
import signal
import sys
import time
import urllib.request

from availability import SourceUnavailable
from legacy_callbacks import LegacyCallbacks, LegacyPublisher


def save(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def main(mode, directory):
    expected_parent = int(os.environ['HWHB_PARENT_PID'])
    if C.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0) or os.getppid() != expected_parent:
        raise RuntimeError('Linux parent supervision required')
    settings = json.loads((directory / 'deployment.json').read_text())
    root = json.loads((directory / 'gateway.local.json').read_text())
    active = Path(os.environ.get('HWHB_ACTIVE_RUNTIME', str(directory / 'runtime')))
    if active not in (directory / 'runtime', directory / 'runtime/managed') or active.is_symlink():
        raise ValueError('owned gateway runtime required')
    environment = {**os.environ, 'POC_INTERFACE': settings['interface'], 'POC_CLOUD_STATUS': '1',
        'POC_HA_PROXY_PORT': str(settings['port']), 'POC_LAN_PORT': str(settings['lan_port'])}
    if mode == 'openharmony':
        binary = directory / 'bridge_poc'
        os.execve(binary, [str(binary), str(active), '--live', str(directory / 'gateway.local.json')], environment)
    os.environ['HILINK_CONFIG_DIR'] = str(directory / 'legacy') + '/'
    os.chdir(directory / 'legacy')
    library = C.CDLL(os.environ['HWHB_LEGACY_LIBRARY'])
    library.GetGatewaySN.restype = C.c_char_p
    library.HILINK_SetPreferredIface.argtypes = [C.c_char_p]
    library.HILINK_SetAutoAc.argtypes = [C.POINTER(C.c_ubyte), C.c_uint]
    library.HILINK_SetAutoAc.restype = C.c_int
    library.HilinkSyncBrgDevStatus.argtypes = [C.c_char_p, C.c_int]
    library.UpdateHAStatus.argtypes = [C.c_char_p, C.c_char_p, C.c_char_p]
    interface = settings['interface'].encode()
    library.HILINK_SetPreferredIface(interface)
    ac = bytes.fromhex(root['ac_key_hex'])
    ac_buffer = (C.c_ubyte * 48).from_buffer_copy(ac)
    if library.HILINK_SetAutoAc(ac_buffer, 48):
        raise ValueError('legacy AC setup failed')
    catalog = [row for row in json.loads((active / 'ha-children.local.json').read_text()) if 'sdk_index' in row]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def bridge(key, sid=None, data=None):
        payload = {'key': key}
        if sid is not None:
            payload.update(sid=sid, data=data)
        request = urllib.request.Request('http://127.0.0.1:' + str(settings['port']) + '/bridge',
            data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with opener.open(request, timeout=10) as response:
            result = json.load(response)
        if result.get('available') is False:
            raise SourceUnavailable('HA source unavailable')
        if not result['ok']:
            raise ValueError('HA rejected SDK request')
        return result['state']
    state = {'sdk_started': False, 'device_status': 0, 'native_SN_confirmed': False}
    snapshots = {}
    for row in catalog:
        try:
            snapshots[row['sn'].encode()] = bridge(row['key'])
        except SourceUnavailable:
            pass
    callbacks = LegacyCallbacks(catalog, root, state, snapshots)
    publisher = LegacyPublisher(library, callbacks)
    library.RegHomeAssistantPyCB(callbacks.action, callbacks.kind, callbacks.status, callbacks.notify, callbacks.get)
    library.RegPINQueryCallback(callbacks.pin)
    if library.main():
        raise RuntimeError('legacy SDK startup failed')
    stop = False
    def terminate(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    while not stop:
        while callbacks.pending:
            sn, raw = callbacks.pending.popleft()
            try:
                payload = json.loads(raw)
                matches = [sid for sid, value in callbacks.cache[sn].items()
                           if isinstance(payload, dict) and payload and set(payload) <= set(value)]
                if len(matches) != 1:
                    raise ValueError('legacy ABI has no unambiguous service ID')
                callbacks.update(sn, bridge(callbacks.children[sn]['key'], matches[0], payload))
                state['writes'] = state.get('writes', 0) + 1
            except (OSError, ValueError, KeyError, TypeError):
                state['rejected'] = state.get('rejected', 0) + 1
        for sn, row in callbacks.children.items():
            try:
                callbacks.update(sn, bridge(row['key']))
            except (OSError, ValueError):
                publisher.invalidate(sn)
                state['backend_read_failures'] = state.get('backend_read_failures', 0) + 1
        sn = library.GetGatewaySN()
        if sn and sn.decode() != root['sn']:
            raise ValueError('native SDK identity changed')
        state.update(sdk_started=True, native_SN_confirmed=bool(sn), device_status=library.HILINK_GetDevStatus())
        publisher.publish(state['device_status'] == 1)
        state['available_children'] = len(callbacks.encoded)
        state['unavailable_children'] = len(callbacks.children) - len(callbacks.encoded)
        save(directory / 'legacy-status.json', state)
        for _ in range(10):
            if stop:
                break
            time.sleep(.1)
    os._exit(0)


if __name__ == '__main__':
    os.umask(0o077)
    if sys.argv[1] not in ('legacy', 'openharmony'):
        raise SystemExit(2)
    main(sys.argv[1], Path(sys.argv[2]).resolve())
