"""升级工具的文件安全/还原回归；真实 Podman 与 HA 验收另见 M3 记录。"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import ha_upgrade as upgrade


class UpgradeFilesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config = self.root / 'config'
        self.component = self.config / upgrade.COMPONENT
        self.component.mkdir(parents=True)
        (self.component / '__init__.py').write_text('old code\n')
        for folder in ('config/adapters/xiaomi', 'hilink_bridge/lib/amd64', 'profile'):
            (self.component / folder).mkdir(parents=True)
            (self.component / folder / 'keep').write_bytes(b'target custom configuration')
        self.entries = self.config / '.storage/core.config_entries'
        self.entries.parent.mkdir()
        upgrade.write_json(self.entries, {'data': {'entries': [
            {'entry_id': 'bridge', 'domain': 'hwhomebridge', 'data': {}},
            {'entry_id': 'other', 'domain': 'other', 'data': {'kept': 1}}]}})
        self.runtime = self.config / '.storage/hwhomebridge/config/device_ac'
        self.runtime.parent.mkdir(parents=True)
        self.runtime.write_bytes(b'private runtime canary')
        self.saved = self.root / 'backup'
        self.guard = patch.object(upgrade, 'stopped_container', return_value={'Id': 'isolated'})
        self.guard.start()
        self.addCleanup(self.guard.stop)
        upgrade.backup(self.config, self.saved, 'test')

    def install(self):
        payload = {'__init__.py': b'new code\n', 'openharmony/new.py': b'new backend\n'}
        with patch.object(upgrade, 'preflight'), patch.object(upgrade, 'load_package', return_value=({'version': upgrade.VERSION}, payload)):
            return upgrade.install(self.config, self.root / 'package', 'sha', self.saved, 'test', Path('probe'))

    def test_preserve_install_restore(self):
        before = upgrade.regular_tree(self.component)
        entry_bytes = self.entries.read_bytes()
        self.assertTrue(self.install()['passed'])
        self.assertEqual((self.component / 'config/adapters/xiaomi/keep').read_bytes(), b'target custom configuration')
        self.assertEqual(self.runtime.read_bytes(), b'private runtime canary')
        self.assertEqual(self.entries.read_bytes(), entry_bytes)
        self.assertTrue(upgrade.restore(self.config, self.saved, 'test')['component_exactly_restored'])
        self.assertEqual(upgrade.regular_tree(self.component), before)
        self.assertEqual(self.entries.read_bytes(), entry_bytes)

    def test_selective_entry_restore_keeps_other_updates(self):
        self.install()
        current = upgrade.read_json(self.entries)
        current['data']['entries'][0]['data'] = {'backend_type': 'legacy'}
        current['data']['entries'][1]['data'] = {'kept': 2}
        current['data']['entries'].append({'entry_id': 'added', 'domain': 'other', 'data': {}})
        upgrade.write_json(self.entries, current)
        with self.assertRaisesRegex(ValueError, 'restore-entries'):
            upgrade.restore(self.config, self.saved, 'test')
        self.assertEqual((self.component / '__init__.py').read_text(), 'new code\n')
        upgrade.restore(self.config, self.saved, 'test', True)
        restored = upgrade.read_json(self.entries)['data']['entries']
        self.assertEqual(next(e for e in restored if e['entry_id'] == 'other')['data'], {'kept': 2})
        self.assertTrue(any(e['entry_id'] == 'added' for e in restored))
        self.assertEqual(next(e for e in restored if e['entry_id'] == 'bridge')['data'], {})

    def test_stale_backup_refused(self):
        (self.component / '__init__.py').write_text('changed after backup')
        with self.assertRaisesRegex(ValueError, 'fresh unchanged backup'):
            self.install()

    def test_post_install_changes_refuse_restore(self):
        self.install()
        (self.component / 'config/adapters/xiaomi/keep').write_text('later user edit')
        with self.assertRaisesRegex(ValueError, 'changed after install'):
            upgrade.restore(self.config, self.saved, 'test')

    def test_generated_bytecode_does_not_block_restore(self):
        self.install()
        cache = self.component / '__pycache__'
        cache.mkdir()
        (cache / 'test.pyc').write_bytes(b'generated cache')
        self.assertTrue(upgrade.restore(self.config, self.saved, 'test')['component_exactly_restored'])

    def test_corrupt_backup_refused(self):
        (self.saved / 'core.config_entries').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            self.install()

    def test_symlink_refused(self):
        (self.component / 'link').symlink_to(self.runtime)
        with self.assertRaisesRegex(ValueError, 'regular'):
            upgrade.target_paths(self.config)

    def test_backup_inside_config_refused(self):
        with self.assertRaisesRegex(ValueError, 'outside'):
            upgrade.backup(self.config, self.config / 'backup', 'test')

    def test_switch_failure_recovers_old_directory(self):
        staging = self.root / 'missing'
        before = upgrade.regular_tree(self.component)
        with self.assertRaises(OSError):
            upgrade.replace_directory(self.component, staging)
        self.assertEqual(upgrade.regular_tree(self.component), before)

    def test_incomplete_switch_refused(self):
        previous = self.component.with_name('.hwhomebridge-previous')
        previous.mkdir()
        with self.assertRaisesRegex(ValueError, 'unfinished'):
            self.install()

    def test_entry_conflict_checked_before_switch(self):
        self.install()
        current = upgrade.read_json(self.entries)
        current['data']['entries'] = [{'entry_id': 'bridge', 'domain': 'other'}]
        upgrade.write_json(self.entries, current)
        with self.assertRaisesRegex(ValueError, 'ID conflict'):
            upgrade.restore(self.config, self.saved, 'test', True)
        self.assertEqual((self.component / '__init__.py').read_text(), 'new code\n')

    def test_package_hash_refused(self):
        path = self.root / 'bad.zip'
        path.write_bytes(b'not a package')
        with self.assertRaisesRegex(ValueError, 'SHA256'):
            upgrade.load_package(path, '0' * 64)

    def test_no_preserved_or_private_payload(self):
        for name in ('../escape.py', '/escape.py', 'config/product_registry.json',
                     'hilink_bridge/config/hilink.cfg', 'profile/001.json', 'openharmony/gateway.local.json',
                     '__pycache__/cache.py', 'openharmony/../../escape.py', 'openharmony/./alias.py',
                     'openharmony//alias.py'):
            self.assertFalse(upgrade.payload_allowed(name), name)

    def test_internal_package_checksum_refused(self):
        path = self.root / 'bad-member.zip'
        manifest = {'format': 1, 'kind': 'hwhomebridge-preserving-upgrade', 'version': upgrade.VERSION,
                    'tested_ha': upgrade.TESTED_HA, 'architectures': ['aarch64', 'x86_64'],
                    'files': {'__init__.py': '0' * 64}}
        with zipfile.ZipFile(path, 'w') as stream:
            stream.writestr('release.json', json.dumps(manifest))
            stream.writestr('component/__init__.py', 'tampered')
        with self.assertRaisesRegex(ValueError, 'member checksum'):
            upgrade.load_package(path, upgrade.digest(path.read_bytes()))

    def test_archive_traversal_refused_even_with_matching_sha(self):
        path = self.root / 'traversal.zip'
        manifest = {'format': 1, 'kind': 'hwhomebridge-preserving-upgrade', 'version': upgrade.VERSION,
                    'tested_ha': upgrade.TESTED_HA, 'architectures': ['aarch64', 'x86_64'],
                    'files': {'../outside.py': upgrade.digest(b'bad')}}
        with zipfile.ZipFile(path, 'w') as stream:
            stream.writestr('release.json', json.dumps(manifest))
            stream.writestr('component/../outside.py', b'bad')
        with self.assertRaisesRegex(ValueError, 'scope violation'):
            upgrade.load_package(path, upgrade.digest(path.read_bytes()))

    def test_uid_mismatch_refused(self):
        with patch.object(upgrade.os, 'getuid', return_value=-1):
            with self.assertRaisesRegex(ValueError, 'owned regular'):
                upgrade.target_paths(self.config)

    def test_insufficient_disk_refused(self):
        for name in ('hilink_bridge/lib/amd64/libhilink_bridge.so', 'config/product_registry.json',
                     'hilink_bridge/config/hilink.cfg', 'hilink_bridge/config/hilink_bak.cfg'):
            path = self.component / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'test')
        with patch.object(upgrade, 'load_package', return_value=({'architectures': ['x86_64']}, {'code.py': b'new'})), \
             patch.object(upgrade.platform, 'system', return_value='Linux'), \
             patch.object(upgrade.platform, 'machine', return_value='x86_64'), \
             patch.object(upgrade.shutil, 'disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0)):
            with self.assertRaisesRegex(ValueError, 'disk space'):
                upgrade.preflight(self.config, Path('package'), 'sha', 'test', Path('probe'))


class LocalEngineTest(unittest.TestCase):
    def test_probe_marker_separates_container_launcher_output(self):
        value = {'ha_version': '2026.10.1', 'runtime_uid': 0}
        raw = 'docker-default\nHA_RUNTIME_PREFLIGHT ' + json.dumps(value) + '\n'
        self.assertEqual(upgrade.runtime_probe_result(raw), value)
        self.assertEqual(upgrade.runtime_probe_result(json.dumps(value)), value)

    def test_duplicate_probe_results_refused(self):
        with self.assertRaises(ValueError):
            upgrade.runtime_probe_result('HA_RUNTIME_PREFLIGHT {}\nHA_RUNTIME_PREFLIGHT {}')

    def test_unmarked_noise_and_missing_or_nonobject_probe_refused(self):
        for raw in ('', 'docker-default\n{}', 'HA_RUNTIME_PREFLIGHT []'):
            with self.assertRaises(ValueError):
                upgrade.runtime_probe_result(raw)

    def test_explicit_Docker_socket_ignores_remote_context(self):
        with patch.object(upgrade, 'CONTAINER_ENGINE', 'docker'), \
             patch.dict(upgrade.os.environ, {'DOCKER_CONTEXT': 'remote', 'DOCKER_HOST': 'tcp://remote:2375'}), \
             patch.object(upgrade.subprocess, 'run') as run:
            run.return_value.returncode = 0
            upgrade.podman('inspect', 'fixture')
            self.assertEqual(run.call_args.args[0][:3], ['docker', '--host', 'unix:///var/run/docker.sock'])
            self.assertNotIn('DOCKER_CONTEXT', run.call_args.kwargs['env'])
            self.assertNotIn('DOCKER_HOST', run.call_args.kwargs['env'])

    def test_Podman_remains_local(self):
        with patch.object(upgrade, 'CONTAINER_ENGINE', 'podman'), \
             patch.object(upgrade.subprocess, 'run') as run:
            run.return_value.returncode = 0
            upgrade.podman('ps', '-q')
            self.assertEqual(run.call_args.args[0], ['podman', '--remote=false', 'ps', '-q'])

    def test_unknown_engine_rejected_before_process_execution(self):
        with patch.object(upgrade, 'CONTAINER_ENGINE', 'remote'), \
             patch.object(upgrade.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                upgrade.podman('inspect', 'fixture')
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
