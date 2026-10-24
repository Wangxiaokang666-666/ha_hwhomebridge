"""创建完整、全新的隔离 HA 夹具；从不复制已配对运行目录。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil

from build_manifest import inspect_elf
from ha_configflow_fixture import prepare

ROOT = Path(__file__).resolve().parent
COMPONENT = ROOT.parents[1] / 'custom_components/hwhomebridge'


def create(destination, binary, arch, real_legacy=False, assets=None):
    if destination.exists():
        raise ValueError('fresh test destination required')
    if real_legacy and arch != 'x86_64':
        raise ValueError('real legacy M2 test requires x86_64 Linux')
    inspect_elf(binary, arch)
    os.umask(0o077)
    destination.mkdir(parents=True, mode=0o700)
    config = destination / 'config'
    target = config / 'custom_components/hwhomebridge'
    # 只选择源代码及公开定义目录，不能递归复制整个集成或 experiments。
    sources = list(COMPONENT.glob('*.py')) + list(COMPONENT.glob('*.json'))
    sources += list((COMPONENT / 'openharmony').glob('*.py'))
    for folder in ('config', 'openharmony/config', 'translations'):
        sources += list((COMPONENT / folder).rglob('*.json'))
    for path in sources:
        if '.local.' in path.name:
            continue
        copied = target / path.relative_to(COMPONENT)
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, copied)
    if real_legacy:
        for name in ('config/hilink.cfg', 'config/hilink_bak.cfg', 'lib/amd64/libhilink_bridge.so'):
            copied = target / 'hilink_bridge' / name
            copied.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(COMPONENT / 'hilink_bridge' / name, copied)
    prepare(config, ROOT, binary, assets)
    tests = destination / 'tests'
    tests.mkdir()
    for name in ('ha_coexistence_test.py', 'ha_real_sdk_test.py', 'ha_child_smoke.py', 'virtual_child_smoke.py'):
        shutil.copyfile(ROOT / name, tests / name)
    executables = ('coap_alignment_test', 'ha_child_state_test', 'virtual_child_state_test', 'speke_vectors')
    for name in executables:
        inspect_elf(binary.parent / name, arch)
        shutil.copyfile(binary.parent / name, tests / name)
    # 旧库从 eth0 取得 MAC；重启同一夹具须复用这份全新本地 MAC。
    mac = '02:' + ':'.join(f'{n:02x}' for n in secrets.token_bytes(5))
    (destination / 'test-mac.private').write_text(mac)
    for path in destination.rglob('*'):
        path.chmod(0o700 if path.is_dir() or path.name in (*executables, 'bridge_poc') else 0o600)
    summary = {'new_unpaired_fixture': True, 'architecture': arch, 'real_legacy_sdk': real_legacy}
    if real_legacy:
        library = target / 'hilink_bridge/lib/amd64/libhilink_bridge.so'
        summary['legacy_sdk_sha256'] = hashlib.sha256(library.read_bytes()).hexdigest()
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--arch', choices=('aarch64', 'x86_64'), required=True)
    parser.add_argument('--assets', type=Path)
    parser.add_argument('--real-legacy', action='store_true')
    args = parser.parse_args()
    print(json.dumps(create(args.destination, args.binary, args.arch, args.real_legacy, args.assets)))
