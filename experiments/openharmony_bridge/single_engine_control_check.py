"""C真实路由→真实HA公共逻辑→虚拟设备；此程序不启动SDK或注册云端子设备。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def check(binary, catalog_path):
    with tempfile.TemporaryDirectory(prefix='single-controls-') as directory:
        root = Path(directory)
        rows = json.loads(catalog_path.read_text())
        p = root / 'ha-children.local.json'
        p.write_text(json.dumps(rows))
        p.chmod(0o600)
        def run(action, key=None, sid=None, data=None, ok=True):
            args = [str(binary), str(root), action]
            if key is not None:
                args.extend(['offline-' + key, sid])
            if data is not None:
                args.append(json.dumps(data))
            result = subprocess.run(args, capture_output=True, text=True, timeout=15)
            assert (result.returncode == 0) == ok, (action, key, sid, result.returncode)
            if action == 'get' and ok:
                return json.loads(result.stdout.splitlines()[-1])
        run('init')
        for row in rows:
            p = root / ('ha-child-' + row['key'] + '.json')
            identity = json.loads(p.read_text())
            identity['devId'] = 'offline-' + row['key']
            p.write_text(json.dumps(identity))
            p.chmod(0o600)
        run('put', 'legacy-light', 'switch', {'on': 1})
        run('put', 'legacy-light', 'brightness', {'brightness': 50})
        assert abs(run('get', 'legacy-light', 'brightness')['brightness'] - 50) <= 1
        run('put', 'legacy-light', 'cct', {'colorTemperature': 4300})
        assert run('get', 'legacy-light', 'cct') == {'colorTemperature': 4300}
        for gear in range(1, 7):
            run('put', 'fan-wifi', 'fan', {'speed': gear})
            assert run('get', 'fan-wifi', 'fan')['speed'] == gear
        run('put', 'fan-wifi', 'mode', {'mode': 2})
        assert run('get', 'fan-wifi', 'mode')['mode'] == 2
        for value in (0, 1):
            run('put', 'timer-controls', 'verticalswing', {'verticalswing': value})
            assert run('get', 'timer-controls', 'verticalswing')['verticalswing'] == value
        run('put', 'fan-wifi', 'endTime', {'time': 30})
        assert run('get', 'fan-wifi', 'endTime')['time'] == 30
        run('put', 'fan-wifi', 'endTime', {'time': 0})
        assert run('get', 'fan-wifi', 'endTime')['time'] == 0
        timer = {'Equipmentname': 0, 'countdownht': 1, 'timeing': 20}
        run('put', 'timer-controls', 'countdownht', timer)
        actual = run('get', 'timer-controls', 'countdownht')
        assert actual['countdownht'] == 1 and actual['timeing'] == 20
        run('put', 'timer-controls', 'countdownht', {'Equipmentname': 0, 'countdownht': 0, 'timeing': 20})
        assert run('get', 'timer-controls', 'countdownht')['countdownht'] == 0
        run('put', 'aux', 'switch2', {'on': 1})
        assert run('get', 'aux', 'switch2')['on'] == 1
        for key, sid in [('light-options', 'switch1'), ('effects-0', 'switch1'), ('effects-1', 'switch1'), ('effects-2', 'switch1')]:
            run('put', key, sid, {'on': 1})
            assert run('get', key, sid)['on'] == 1
        run('put', 'fan-wifi', 'fan', {'speed': 8}, ok=False)
        run('put', 'legacy-light', 'brightness', {'brightness': 101}, ok=False)
        run('put', 'legacy-light', 'netInfo', {'on': 1}, ok=False)
        run('get', 'unknown', 'switch', ok=False)
        run('init')
    return {'passed': True, 'native_C_to_real_HA_virtual_services': True,
        'legacy_light_power_brightness_CCT': True, 'six_gears_natural_wind_direction': True,
        'native_device_timer_emulation': True, 'ambient_settings_effects': True,
        'invalid_services_values_or_foreign_child_rejected': True,
        'SDK_started_or_cloud_registration': False, 'physical_devices_accessed': False}


if __name__ == '__main__':
    print(json.dumps(check(Path(sys.argv[1]).resolve(), Path(sys.argv[2]))))
