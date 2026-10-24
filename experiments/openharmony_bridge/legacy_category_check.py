"""Linux真实C→HA Core虚构001–008；跨子设备隔离、共享SID和非法写入验收。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone


def check(binary, catalog_path):
    with tempfile.TemporaryDirectory(prefix='legacy-categories-') as directory:
        root = Path(directory)
        rows = json.loads(catalog_path.read_text())
        path = root / 'ha-children.local.json'
        path.write_text(json.dumps(rows)); path.chmod(0o600)
        def run(action, key=None, sid=None, data=None, ok=True):
            args = [str(binary), directory, action]
            if key:
                args += ['offline-' + key, sid]
            if data is not None:
                args += [json.dumps(data)]
            result = subprocess.run(args, capture_output=True, text=True, timeout=20)
            assert (result.returncode == 0) == ok, (action, key, sid, result.returncode)
            if action == 'get' and ok:
                return json.loads(result.stdout.splitlines()[-1])
        run('init')
        before = {}
        for row in rows:
            p = root / ('ha-child-' + row['key'] + '.json')
            state = json.loads(p.read_text()); state['devId'] = 'offline-' + row['key']
            p.write_text(json.dumps(state)); p.chmod(0o600)
            before[row['key']] = p.read_bytes()
        def write(key, sid, payload):
            run('put', key, sid, payload)
            actual = run('get', key, sid)
            assert all(name in actual and abs(actual[name] - value) <= (1 if sid == 'brightness' else 0)
                       for name, value in payload.items()), (key, sid)
        for key in ('legacy-001', 'legacy-light', 'legacy-003'):
            write(key, 'switch', {'on': 1})
            write(key, 'brightness', {'brightness': 45})
            write(key, 'cct', {'colorTemperature': 4300})
        write('legacy-001', 'switch', {'on': 0})
        assert run('get', 'legacy-light', 'switch') == {'on': 1}
        assert run('get', 'legacy-003', 'status') == {'status': 0}
        assert run('get', 'legacy-004', 'alarmBell') == {'mode': 1}
        write('legacy-005', 'mode', {'mode': 2})
        write('legacy-005', 'temperature', {'heatingTarget': 70})
        assert run('get', 'legacy-005', 'temperature') == {'current': 26, 'heatingTarget': 70}
        end = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime('%Y%m%dT%H%M%SZ')
        run('put', 'legacy-006', 'delay', {'action': 0, 'num': 1, 'delay': [
            {'enable': 1, 'end': end, 'sid': 'switch', 'para': 'on'}]})
        assert run('get', 'legacy-006', 'delay')['num'] == 1
        run('put', 'legacy-006', 'delay', {'action': 2, 'num': 0, 'delay': [{}]})
        assert run('get', 'legacy-006', 'delay')['action'] == 2
        write('legacy-007', 'cooker', {'mode': 5})
        assert run('get', 'legacy-007', 'leftTime') == {'time': 20}
        assert run('get', 'legacy-008', 'electric') == {
            'totalConsum': 12.34, 'currentVoltage1': 220.56, 'currentPower1': 36.78, 'currentElectric1': .17}
        for key, sid, payload in [('legacy-003', 'status', {'status': 1}),
                ('legacy-004', 'alarmBell', {'mode': 1}), ('legacy-005', 'temperature', {'current': 99}),
                ('legacy-005', 'temperature', {'preserveTarget': 40}), ('legacy-005', 'heatingTarget', {'heatingTarget': 50}),
                ('legacy-007', 'leftTime', {'time': 1}), ('legacy-007', 'cooker', {'action': 1}),
                ('legacy-008', 'electric', {'currentPower1': 99}), ('legacy-light', 'brightness', {'brightness': 101})]:
            run('put', key, sid, payload, ok=False)
        run('get', 'unknown', 'switch', ok=False)
        run('init')
        assert all((root / ('ha-child-' + key + '.json')).read_bytes() == data for key, data in before.items())
    return {'passed': True, 'categories': list(range(1, 9)), 'child_count': len(rows),
            'shared_temperature_and_electric_services': True, 'legacy_select_timer': True,
            'cross_child_isolation': True, 'read_only_unmapped_or_invalid_writes_rejected': True,
            'cross_process_identity_preserved': True, 'native_cloud_connection': False,
            'physical_devices_accessed': False, 'architecture': os.uname().machine}


if __name__ == '__main__':
    import sys
    print(json.dumps(check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))))
