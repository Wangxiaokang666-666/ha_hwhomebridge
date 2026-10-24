"""虚构网关的真实文件安装/还原；全程要求测试 HA 容器停止。"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
from unittest.mock import patch


def run(base, baseline, baseline_sha, candidate, candidate_sha, container):
    config = base / 'config'
    assert json.loads((config / 'fixture-scope.json').read_text()) == {
        'purpose': 'existing-gateway-ha-lifecycle-virtual-only', 'production_access': False}
    spec = importlib.util.spec_from_file_location('upgrade', base / 'package/ha_upgrade.py')
    upgrade = importlib.util.module_from_spec(spec); spec.loader.exec_module(upgrade)
    upgrade.stopped_container(config, container)
    component = config / upgrade.COMPONENT
    assert json.loads((config / '.storage/hwhomebridge/gateways/current/gateway.local.json').read_text())['sn'].startswith('OHPOC-')
    entries = config / '.storage/core.config_entries'
    entry_bytes = entries.read_bytes()
    paired = config / '.storage/hwhomebridge/gateways/current'
    protected = {str(p.relative_to(component)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in component.rglob('*') if p.is_file() and upgrade.preserved(str(p.relative_to(component)))}
    identity = {str(p.relative_to(paired)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in paired.rglob('*') if p.is_file()}
    # 仅构造停止的测试组件基线；读包仍检查完整摘要、架构、范围和每个文件。
    with patch.object(upgrade, 'VERSION', '1.1.0b6'):
        _, payload = upgrade.load_package(baseline, baseline_sha)
    for cache in component.rglob('__pycache__'):
        shutil.rmtree(cache)
    for path in component.rglob('*'):
        if path.is_file():
            name = str(path.relative_to(component))
            if not upgrade.preserved(name) and name not in payload:
                path.unlink()
    for name, data in payload.items():
        path = component / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data); path.chmod(0o700 if name.endswith(('/bridge_poc', '/identity_state_probe')) else 0o600)
    assert json.loads((component / 'manifest.json').read_text())['version'] == '1.1.0b6'
    original = upgrade.regular_tree(component)
    probe = base / 'package/ha_runtime_preflight.py'
    backup = base / 'backup-b6-install-check'
    upgrade.backup(config, backup, container)
    upgrade.install(config, candidate, candidate_sha, backup, container, probe)
    _, current = upgrade.load_package(candidate, candidate_sha)
    assert all((component / name).read_bytes() == data for name, data in current.items())
    assert entries.read_bytes() == entry_bytes
    restored = upgrade.restore(config, backup, container)
    assert restored['component_exactly_restored'] and upgrade.regular_tree(component) == original
    second = base / 'backup-b6-final-install'
    upgrade.backup(config, second, container)
    upgrade.install(config, candidate, candidate_sha, second, container, probe)
    assert all((component / name).read_bytes() == data for name, data in current.items())
    assert entries.read_bytes() == entry_bytes
    assert protected == {str(p.relative_to(component)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in component.rglob('*') if p.is_file() and upgrade.preserved(str(p.relative_to(component)))}
    assert identity == {str(p.relative_to(paired)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paired.rglob('*') if p.is_file()}
    upgrade.stopped_container(config, container)
    result = {'passed': True, 'baseline': '1.1.0b6', 'candidate': upgrade.VERSION,
        'install_restore_reinstall': True, 'all_package_files_verified': len(current),
        'entry_bytes_unchanged': True, 'paired_directory_unchanged': True,
        'original_configuration_library_profiles_unchanged': True, 'HA_started_by_installer': False}
    upgrade.write_json(base / 'availability-install-result.json', result)
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('base', type=Path)
    for name in ('baseline', 'candidate'):
        parser.add_argument('--' + name, type=Path, required=True)
        parser.add_argument('--' + name + '-sha', required=True)
    parser.add_argument('--container', required=True)
    args = parser.parse_args(); os.umask(0o077)
    run(args.base, args.baseline, args.baseline_sha, args.candidate, args.candidate_sha, args.container)
