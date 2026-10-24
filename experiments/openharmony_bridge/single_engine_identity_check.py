"""在一次性副本上检查配对存储；不启动 SDK、网络或执行身份转换。"""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from single_engine_fixture import private_root


def fingerprint(directory):
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in directory.rglob('*') if path.is_file()
    }


def probe_copy(binary, state, gateway, variant='original'):
    with tempfile.TemporaryDirectory(prefix='single-identity-probe-') as temporary:
        root = Path(temporary)
        clone = root / 'state'
        shutil.copytree(state, clone)
        clone.chmod(0o700)
        configuration = json.loads(gateway.read_text())
        if variant == 'changed_ac':
            original = bytes.fromhex(configuration['ac_key_hex'])
            configuration['ac_key_hex'] = bytes([original[0] ^ 1, *original[1:]]).hex()
        elif variant == 'corrupt_state':
            (clone / 'dev_data').write_bytes(b'invalid')
        elif variant == 'renamed_legacy':
            shutil.copyfile(clone / 'config.cfg', clone / 'dev_data')
        elif variant != 'original':
            raise ValueError('unknown probe variant')
        config_path = root / 'gateway.json'
        config_path.write_text(json.dumps(configuration))
        config_path.chmod(0o600)
        process = subprocess.run(
            [str(binary), str(clone), str(config_path)],
            capture_output=True, text=True, timeout=15,
        )
        if process.returncode:
            raise ValueError('offline native identity probe failed')
        rows = [line[15:] for line in process.stdout.splitlines() if line.startswith('IDENTITY_PROBE ')]
        if len(rows) != 1:
            raise ValueError('offline identity result missing')
        result = json.loads(rows[0])
        # 身份摘要只用于本进程比较；公开结果不携带原始身份或凭据。
        result.pop('dev_id_sha256')
        result.pop('fields', None)
        return result


def check_copies(binary, open_lab, legacy_lab):
    inputs = (open_lab / 'runtime/sdk-state', legacy_lab / 'legacy')
    before = [fingerprint(path) for path in inputs]
    results = {
        'positive': probe_copy(binary, inputs[0], open_lab / 'gateway.local.json'),
        'changed_ac': probe_copy(binary, inputs[0], open_lab / 'gateway.local.json', 'changed_ac'),
        'corrupt_state': probe_copy(binary, inputs[0], open_lab / 'gateway.local.json', 'corrupt_state'),
        'legacy_original_names': probe_copy(binary, inputs[1], legacy_lab / 'gateway.local.json'),
        'legacy_renamed_config': probe_copy(binary, inputs[1], legacy_lab / 'gateway.local.json', 'renamed_legacy'),
    }
    if not results['positive']['bound_identity_loaded']:
        raise ValueError('positive paired OpenHarmony control did not load')
    if any(results[name]['bound_identity_loaded'] for name in ('changed_ac', 'corrupt_state')):
        raise ValueError('negative storage authentication control unexpectedly loaded')
    if before != [fingerprint(path) for path in inputs]:
        raise ValueError('source paired storage changed during offline check')
    return {
        'checks': results, 'paired_sources_unchanged': True, 'network_started': False,
        'legacy_importer_available': False, 'live_legacy_takeover_allowed': False,
    }


def check(binary, open_lab, legacy_lab):
    with ExitStack() as stack:
        for root in (open_lab, legacy_lab):
            private_root(root)
            owner = stack.enter_context((root / 'engine-owner.lock').open('r'))
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return check_copies(binary, open_lab, legacy_lab)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('probe', type=Path)
    parser.add_argument('open_lab', type=Path)
    parser.add_argument('legacy_lab', type=Path)
    arguments = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(check(arguments.probe.resolve(), arguments.open_lab.resolve(), arguments.legacy_lab.resolve()), indent=2))
