"""真实C的SN保留边界；临时目录、无SDK启动、无云端注册。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def check(binary):
    with tempfile.TemporaryDirectory(prefix='single-native-') as directory:
        root = Path(directory)
        rows = [
            {'key': 'legacy-light', 'sn': 'HB0123456789abcd', 'pid': '9MU7', 'devType': '119', 'manu': '002', 'services': {'switch': 'switch'}},
            {'key': 'fan', 'pid': '28BF', 'devType': '029', 'manu': '3af', 'services': {'switch': 'switch', 'fan': 'fan'}},
        ]
        catalog = root / 'ha-children.local.json'
        def run(ok):
            catalog.write_text(json.dumps(rows))
            catalog.chmod(0o600)
            result = subprocess.run([str(binary), str(root), 'init'], capture_output=True, timeout=15)
            assert (result.returncode == 0) == ok, 'C identity validation differed from expectation'
        run(True)
        old_path = root / 'ha-child-legacy-light.json'
        before = hashlib.sha256(old_path.read_bytes()).hexdigest()
        assert json.loads(old_path.read_text())['sn'] == rows[0]['sn']
        expected = 'OHPOC-HA-' + hashlib.sha256(b'OHPOC-local-ha-smoke#ha-fan-v1').hexdigest()[:24]
        assert json.loads((root / 'ha-child-fan.json').read_text())['sn'] == expected
        run(True)
        assert hashlib.sha256(old_path.read_bytes()).hexdigest() == before
        rows[0]['existing_cloud_only'] = True
        run(False)  # 接管声明缺少原云端ID时，不允许退化成新绑定。
        saved = json.loads(old_path.read_text())
        saved['devId'] = 'old-cloud-child-id'
        old_path.write_text(json.dumps(saved))
        run(True)
        before = hashlib.sha256(old_path.read_bytes()).hexdigest()
        for bad in ('', 'has/slash', 'x' * 40, 123):
            rows[1]['sn'] = bad
            run(False)
        rows[1]['sn'] = rows[0]['sn']
        run(False)
        rows[1].pop('sn')
        rows[0]['sn'] = 'HBdifferent'
        run(False)
        assert hashlib.sha256(old_path.read_bytes()).hexdigest() == before
        rows = [{'key': 'capacity-' + str(i), 'pid': '9MU7', 'devType': '119', 'manu': '002',
                 'services': {'switch': 'switch'}} for i in range(64)]
        run(True)
        run(True)
        rows.append({**rows[-1], 'key': 'capacity-64'})
        run(False)
        rows.pop()
        for i in (0, 1):
            path = root / ('ha-child-capacity-' + str(i) + '.json')
            value = json.loads(path.read_text()); value['devId'] = 'duplicate-cloud-id'
            path.write_text(json.dumps(value))
        run(False)
    return {'passed': True, 'explicit_legacy_SN_preserved': True, 'default_SN_unchanged': True,
            'restart_preserves_identity': True, 'invalid_duplicate_or_replaced_SN_rejected': True,
            'imported_child_without_cloud_ID_rejected': True,
            'capacity_64_and_overflow_rejection': True, 'duplicate_cloud_ID_rejected': True,
            'SDK_started_or_cloud_registration': False}


if __name__ == '__main__':
    print(json.dumps(check(Path(sys.argv[1]).resolve())))
