"""独立Linux的真实文件升级、注入切换失败和还原；拒绝家庭HA目录。"""
import argparse
import importlib.util
import json
from pathlib import Path
import re
from unittest.mock import patch


def main(base, checksum, action, iteration, package=None, container='hwhomebridge-ha-lifecycle'):
    if iteration and not re.fullmatch('[a-z0-9-]+',iteration):
        raise ValueError('simple private iteration name required')
    suffix = '-' + iteration if iteration else ''
    config = base / 'config'
    assert json.loads((config / 'fixture-scope.json').read_text()) == {
        'purpose':'existing-gateway-ha-lifecycle-virtual-only', 'production_access':False}
    spec = importlib.util.spec_from_file_location('upgrade', base / 'tools/ha_upgrade.py')
    upgrade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(upgrade)
    package = package or base / 'tools/hwhomebridge-1.1.0b5.zip'
    probe = base / 'tools/ha_runtime_preflight.py'
    component = config / upgrade.COMPONENT
    entries = config / '.storage/core.config_entries'
    result = {'action':action}
    if action == 'install':
        before, entry_bytes = upgrade.regular_tree(component), entries.read_bytes()
        fault = base / ('backup-injected-failure'+suffix)
        upgrade.backup(config, fault, container)
        rename = Path.rename
        injected = False
        def fail_once(path, target):
            nonlocal injected
            if path.name == 'component' and path.parent.name.startswith('.hwhomebridge-stage-') and not injected:
                injected = True
                raise OSError('intentional independent filesystem-switch failure')
            return rename(path, target)
        try:
            with patch.object(Path, 'rename', fail_once):
                upgrade.install(config, package, checksum, fault, container, probe)
        except OSError:
            assert injected
        else:
            raise AssertionError('injected replacement failure did not occur')
        assert upgrade.regular_tree(component) == before and entries.read_bytes() == entry_bytes
        result['injected_install_failure_restores_original_files_and_entry'] = True
        backup = base / ('backup-b4'+suffix)
        result['backup'] = upgrade.backup(config, backup, container)
        result['preflight'] = upgrade.preflight(config, package, checksum, container, probe)
        result['install'] = upgrade.install(config, package, checksum, backup, container, probe)
        after = upgrade.regular_tree(component)
        assert all(after[name] == detail for name,detail in before.items() if upgrade.preserved(name))
        assert entries.read_bytes() == entry_bytes
        result['all_original_configs_libraries_profiles_and_entries_preserved'] = True
    else:
        result['restore'] = upgrade.restore(config, base / ('backup-b4'+suffix), container, True)
    result['passed'] = True
    upgrade.write_json(base / (action + suffix + '-result.json'), result)
    # Private preflight metadata remains on Linux; only acceptance booleans go to stdout.
    print(json.dumps({key:value for key,value in result.items() if not isinstance(value,dict)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('base',type=Path)
    parser.add_argument('checksum')
    parser.add_argument('action',choices=['install','restore'])
    parser.add_argument('--iteration',default='')
    parser.add_argument('--package',type=Path)
    parser.add_argument('--container',default='hwhomebridge-ha-lifecycle')
    args = parser.parse_args()
    main(args.base,args.checksum,args.action,args.iteration,args.package,args.container)
