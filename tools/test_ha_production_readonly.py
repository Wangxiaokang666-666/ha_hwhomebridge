"""生产只读工具的路由隔离、私密副本和失败回归；不连接真实 HA。"""
import json
from pathlib import Path
import tempfile
import unittest

import ha_production_readonly as read


class Response:
    def __init__(self, data, headers=None):
        self.data = data
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def read(self, size):
        return self.data[:size]


class ReadOnlyTest(unittest.TestCase):
    def test_dangerous_supervisor_requests_denied(self):
        for endpoint, method in (('/core/restart', 'post'), ('/addons/core_samba/start', 'post'),
                                 ('/addons/core_configurator/options', 'post'), ('/backups/new/full', 'post'),
                                 ('/host/reboot', 'get'), ('/ingress/session', 'delete'),
                                 ('/core/info', 'POST'), ('/addons/../../core/restart', 'get')):
            self.assertFalse(read.supervisor_request_allowed(endpoint, method))
        self.assertTrue(read.supervisor_request_allowed('/core/info', 'get'))
        self.assertTrue(read.supervisor_request_allowed('/ingress/session', 'post'))

    def test_file_editor_GET_actions_denied(self):
        for route in ('restart', 'reload_automations', 'check_config', 'exec_command', 'upload', 'save', 'delete'):
            self.assertFalse(read.file_request_allowed(route, str(read.COMPONENT / 'manifest.json')))

    def test_runtime_KV_downloads_denied(self):
        for name in ('/homeassistant/.storage/hwhomebridge/config/device_ac',
                     '/homeassistant/.storage/hwhomebridge/openharmony/lab/gateway.local.json',
                     str(read.COMPONENT / 'hilink_bridge/config/bridge.cfg'),
                     str(read.COMPONENT / 'device_ac'), str(read.COMPONENT / 'openharmony/runtime/identity.json'),
                     '/homeassistant/secrets.yaml', '/proc/net/udp'):
            self.assertFalse(read.file_request_allowed('download', name))
        self.assertTrue(read.file_request_allowed('listdir', str(read.RUNTIME)))

    def test_path_alias_and_traversal_denied(self):
        for name in ('/homeassistant/custom_components/hwhomebridge/../secrets.json',
                     '/homeassistant/custom_components/hwhomebridge//manifest.json',
                     '/homeassistant/custom_components/hwhomebridge/%2e%2e/secrets.json',
                     '/homeassistant/custom_components/hwhomebridge/./manifest.json'):
            self.assertFalse(read.file_request_allowed('download', name))

    def test_ingress_credential_forwarding_denied(self):
        for url in ('https://elsewhere.example/api/hassio_ingress/token', '//elsewhere.example/path',
                    '/other/path', '/api/hassio_ingress/test?redirect=elsewhere',
                    '/api/hassio_ingress/test/../../api', '/api/hassio_ingress/%2e%2e'):
            with self.assertRaises(ValueError):
                read.FileReader('http://ha.test', url, 'fake-session')

    def test_reader_only_sends_GET_without_body(self):
        reader = read.FileReader('http://ha.test', '/api/hassio_ingress/test', 'fake-session')
        sent = []
        class Opener:
            def open(self, request, **_kwargs):
                sent.append(request)
                return Response(b'{}', {'Content-Disposition': 'attachment; filename=manifest.json'})
        reader.opener = Opener()
        self.assertEqual(reader.get('download', str(read.COMPONENT / 'manifest.json')), b'{}')
        self.assertEqual(sent[0].get_method(), 'GET')
        self.assertIsNone(sent[0].data)
        with self.assertRaises(ValueError):
            reader.get('restart', str(read.COMPONENT))
        self.assertEqual(len(sent), 1)

    def test_missing_download_header_refused(self):
        reader = read.FileReader('http://ha.test', '/api/hassio_ingress/test', 'fake-session')
        class Opener:
            def open(self, *_args, **_kwargs):
                return Response(b'File not found')
        reader.opener = Opener()
        with self.assertRaises(ValueError):
            reader.get('download', str(read.ENTRIES))

    def test_owned_entries_exclude_other_credentials(self):
        raw = json.dumps({'key': 'core.config_entries', 'version': 1, 'minor_version': 5,
                          'data': {'entries': [{'domain': 'other', 'data': {'secret': 'other-secret'}},
                                               {'domain': 'hwhomebridge', 'data': {'kept': True}}]}}).encode()
        selected = read.owned_entries(raw)
        self.assertEqual(len(selected['entries']), 1)
        self.assertNotIn('other-secret', json.dumps(selected))
        self.assertNotIn('data', selected)

    def test_unknown_runtime_and_private_files_not_snapshot_candidates(self):
        for name in ('runtime/identity.json', 'hilink_bridge/config/bridge.cfg', 'device_ac',
                     'openharmony/runtime/identity.json', 'config/gateway.local.json', '../manifest.json', '.git/config'):
            self.assertFalse(read.snapshot_candidate(name))
        for name in ('manifest.json', 'hwbridge.py', 'config/adapters/xiaomi/bulb.json',
                     'hilink_bridge/config/hilink.cfg', 'hilink_bridge/lib/amd64/libhilink_bridge.so'):
            self.assertTrue(read.snapshot_candidate(name))

    def test_private_local_copy_modes_and_no_runtime_reads(self):
        files = {'manifest.json': b'{"version":"1.0.0"}', '__init__.py': b'# original'}
        rows = [{'name': name, 'type': 'file', 'size': len(data), 'modified': 0, 'created': 0} for name, data in files.items()]
        entries = json.dumps({'data': {'entries': [{'domain': 'hwhomebridge', 'data': {}}]}}).encode()
        class Reader:
            def inventory(self):
                return rows
            def get(self, route, path):
                self_outer.assertEqual(route, 'download')
                self_outer.assertNotIn('/hwhomebridge/config/', path)
                return entries if path == str(read.ENTRIES) else files[str(Path(path).relative_to(str(read.COMPONENT)))]
        self_outer = self
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            _, manifest, _ = read.inspect_files(Reader(), output, True)
            self.assertFalse(manifest['SDK_runtime_KV_included'])
            self.assertFalse(manifest['original_UID_modes_symlinks_verified'])
            self.assertEqual((output / 'component').stat().st_mode & 0o777, 0o700)
            self.assertEqual((output / 'hwhomebridge.entries.private.json').stat().st_mode & 0o777, 0o600)

    def test_unstable_online_source_refused(self):
        class Reader:
            count = 0
            def inventory(self):
                return [{'name': 'manifest.json', 'type': 'file', 'size': 2, 'modified': 0, 'created': 0}]
            def get(self, route, path):
                if path == str(read.ENTRIES):
                    return b'{"data":{"entries":[{"domain":"hwhomebridge"}]}}'
                self.count += 1
                return b'{}' if self.count == 1 else b'{"changed":1}'
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'changed during'):
                read.inspect_files(Reader(), Path(temporary), True)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_oversized_component_refused_before_download(self):
        class Reader:
            def inventory(self):
                return [{'name': 'manifest.json', 'type': 'file', 'size': read.MAX_FILE + 1}]
            def get(self, *_args):
                raise AssertionError('oversized file must not be requested')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, 'size outside'):
                read.inspect_files(Reader(), Path(temporary), True)


if __name__ == '__main__':
    unittest.main()
