"""在线备份范围、断线不重发、私有归档校验；不连接真实 HA。"""
import asyncio
import gzip
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import ha_online_backup as backup

NAME = 'codex-m3-online-20261016T130000-1234abcd'


def tar_bytes(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name, value in files:
            item = tarfile.TarInfo(name)
            if isinstance(value, bytes):
                item.size = len(value)
                archive.addfile(item, io.BytesIO(value))
            else:
                item.type = tarfile.SYMTYPE
                item.linkname = 'elsewhere'
                archive.addfile(item)
    return stream.getvalue()


class OnlineBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)

    def archive(self, extra=(), remove_entries=False, corrupt_gzip=False):
        files = [('data/custom_components/hwhomebridge/manifest.json', b'{"version":"1.0.0"}'),
                 ('data/.storage/hwhomebridge/config/device_ac', b'fake-test-runtime')]
        if not remove_entries:
            files.append(('data/.storage/core.config_entries', b'{"data":{"entries":[]}}'))
        inner = gzip.compress(tar_bytes(files + list(extra)))
        if corrupt_gzip:
            inner = inner[:-8] + b'broken!!'
        data = tar_bytes([('backup.json', json.dumps({'name': NAME, 'homeassistant': {'version': backup.CORE},
                         'protected': False, 'addons': [], 'folders': []}).encode()),
                          ('homeassistant.tar.gz', inner)])
        path = self.root / 'backup.tar'
        path.write_bytes(data)
        return path

    def test_only_HA_online_scope_is_permitted(self):
        data = backup.body(NAME)
        self.assertTrue(backup.allowed('/backups/new/partial', 'post', data, NAME))
        for field, value in [('addons', ['core_samba']), ('folders', ['share']),
                             ('homeassistant_exclude_database', False), ('location', ['remote'])]:
            self.assertFalse(backup.allowed('/backups/new/partial', 'post', {**data, field: value}, NAME))
        for endpoint in ('/core/stop', '/core/restart', '/host/reboot', '/backups/new/full',
                         '/backups/1234abcd/restore/full', '/addons/core_samba/start'):
            self.assertFalse(backup.allowed(endpoint, 'post', data, NAME))

    def test_name_and_read_routes_are_bounded(self):
        with self.assertRaises(ValueError):
            backup.body('existing-unrelated-backup')
        for endpoint in ('/backups/../../core/info', '/core/logs', '/jobs', '/backups/1234abcd/restore/partial'):
            self.assertFalse(backup.allowed(endpoint, 'get', None, NAME))
        self.assertTrue(backup.allowed('/backups/1234abcd/info', 'get', None, NAME))

    def test_archive_verified_without_extracting_private_values(self):
        result = backup.verify_archive(self.archive(), NAME)
        self.assertTrue(result['HA_entries_present'])
        self.assertTrue(result['legacy_runtime_present'])
        self.assertEqual(result['archive_component_links'], 0)
        self.assertNotIn('fake-test-runtime', json.dumps(result))
        self.assertFalse((self.root / 'data').exists())

    def test_missing_complete_entries_refused(self):
        with self.assertRaisesRegex(ValueError, 'entries missing'):
            backup.verify_archive(self.archive(remove_entries=True), NAME)

    def test_component_links_refused(self):
        with self.assertRaisesRegex(ValueError, 'links or special'):
            backup.verify_archive(self.archive([('data/custom_components/hwhomebridge/link', None)]), NAME)

    def test_archive_traversal_and_duplicate_refused(self):
        for extra in ([('../outside', b'bad')], [('data/.storage/core.config_entries', b'duplicate')]):
            with self.assertRaises(ValueError):
                backup.verify_archive(self.archive(extra), NAME)

    def test_gzip_trailer_corruption_refused(self):
        with self.assertRaises((OSError, EOFError, tarfile.TarError)):
            backup.verify_archive(self.archive(corrupt_gzip=True), NAME)

    def test_private_journal_and_symlink_write_refusal(self):
        path = self.root / 'journal.json'
        backup.private_json(path, {'planned': True})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        target = self.root / 'external'
        target.write_text('keep')
        path.with_name(path.name + '.tmp').symlink_to(target)
        with self.assertRaises(OSError):
            backup.private_json(path, {'planned': False})
        self.assertEqual(target.read_text(), 'keep')

    def test_ambiguous_name_refused(self):
        with self.assertRaises(ValueError):
            backup.find_backup({'backups': [{'name': NAME, 'slug': slug} for slug in ('11111111', '22222222')]}, NAME)

    def test_uncertain_creation_is_never_repeated(self):
        token = self.root / 'token'
        token.write_text('fake-token')
        output = self.root / 'out'
        calls = []
        class Client:
            def __init__(self, *_args): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *_args): pass
            async def call(self, endpoint, method='get', data=None, name=''):
                calls.append((endpoint, method))
                if method == 'post':
                    journal = json.loads((output / 'journal.private.json').read_text())
                    self_outer.assertTrue(journal['request_sent'])
                    raise ConnectionError('uncertain response')
                return {'version': backup.SUPERVISOR if endpoint == '/supervisor/info' else backup.CORE}
        self_outer = self
        with patch.object(backup, 'BackupClient', Client):
            with self.assertRaises(ConnectionError):
                asyncio.run(backup.run('http://ha.test', token, output, True))
            with patch.object(backup, 'monotonic', side_effect=(0, 601)):
                with self.assertRaises(TimeoutError):
                    asyncio.run(backup.run('http://ha.test', token, output, True))
        self.assertEqual(sum(method == 'post' for _, method in calls), 1)


if __name__ == '__main__':
    unittest.main()
