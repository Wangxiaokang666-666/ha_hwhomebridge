"""失败切换的事务边界；真实HA和原生进程另由独立Linux验收。"""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from framework_imports import load

module = load('gateway_lifecycle')
constants = load('const')


class Engine(module.GatewayLifecycle):
    def __init__(self, directory):
        self.calls = []
        self.fail = set()
        self.invalid = set()
        async def executor(function, *args):
            return function(*args)
        spec = SimpleNamespace(directory=directory, identity='one-existing-parent',
            read=lambda name: json.loads((directory / name).read_text()), verify_engine=self.verify)
        super().__init__(SimpleNamespace(async_add_executor_job=executor, data={}), spec, 'original-entry')

    def verify(self, mode):
        if mode in self.invalid:
            raise ValueError('invalid engine artifacts')

    async def _launch(self, mode):
        self.calls.append(('launch', mode))
        if mode in self.fail:
            raise RuntimeError('injected startup failure')
        self.active = mode

    async def _stop_engine(self):
        self.calls.append(('stop', self.active))


class LifecycleTests(unittest.TestCase):
    def test_interrupted_switch_recovers_last_good_engine_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Engine(Path(directory))
            runtime.state.update(active='legacy', last_good='legacy', revision='saved-operation',
                pending={'from':'legacy','to':'openharmony'})
            asyncio.run(runtime._save())
            runtime = Engine(Path(directory))
            runtime.deployment.catalog, runtime.deployment.port = [], 18093
            server = SimpleNamespace(serve_forever=lambda:None, shutdown=lambda:None, server_close=lambda:None)
            async def run():
                await runtime.start('openharmony','saved-operation')
                await runtime.stop()
            with patch.object(module.GatewayRouter,'create',new=AsyncMock(return_value=SimpleNamespace())), \
                    patch.object(module,'make_server',return_value=server):
                asyncio.run(run())
            self.assertEqual([call for call in runtime.calls if call[0]=='launch'], [('launch','legacy')])
            self.assertEqual(runtime.state['failed_backend'],'openharmony')
            self.assertIsNone(runtime.state['pending'])

    def test_failed_fallback_stays_disabled_on_cold_boot_until_explicit_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Engine(Path(directory))
            runtime.state.update(active='disabled', last_good='openharmony', failed_backend='openharmony', revision='saved-operation')
            asyncio.run(runtime._save())
            runtime = Engine(Path(directory))
            async def run():
                await runtime.start('openharmony','saved-operation')
                await runtime.stop()
            with patch.object(module.GatewayRouter,'create',new=AsyncMock(return_value=SimpleNamespace())):
                asyncio.run(run())
            self.assertEqual(runtime.active,'disabled')
            self.assertFalse(any(call[0]=='launch' for call in runtime.calls))
            self.assertEqual(runtime.state['failed_backend'],'openharmony')

    def test_parent_online_requires_valid_ack_for_online_and_offline_children(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Engine(Path(directory))
            runtime.deployment.catalog = [{'key':'old-light'}, {'key':'new-fan'}]
            runtime.log_offset = 0
            log = Path(directory) / 'openharmony.private.log'
            log.write_text('POC {"event":"cloud_online"}\nHA {"key":"old-light","phase":3,"code":0,"failures":0,"accepted":true,"available":true}\n')
            self.assertFalse(runtime._ready('openharmony'))
            with log.open('a') as stream:
                stream.write('HA {"key":"new-fan","phase":3,"code":0,"failures":1}\n')
            self.assertFalse(runtime._ready('openharmony'))
            with log.open('a') as stream:
                stream.write('HA {"key":"new-fan","phase":3,"code":0,"failures":0,"accepted":true,"available":false}\n')
            self.assertTrue(runtime._ready('openharmony'))

    def test_original_entry_with_managed_options_keeps_legacy_entry_type(self):
        entry = SimpleNamespace(data={}, options={'backend':'openharmony',
            'gateway_directory':'.storage/hwhomebridge/gateways/current'})
        self.assertEqual(constants.backend_type(entry), 'legacy')
        self.assertEqual(constants.configured_backend(entry), 'openharmony')

    def test_fresh_managed_entry_reads_directory_from_data(self):
        entry = SimpleNamespace(data={'backend':'openharmony', 'gateway_directory':'private-directory'}, options={})
        self.assertEqual(constants.backend_type(entry), 'legacy')
        self.assertEqual(constants.managed_directory(entry), 'private-directory')

    def test_success_keeps_identity_and_records_durable_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Engine(Path(directory))
            asyncio.run(runtime.switch('legacy'))
            asyncio.run(runtime.switch('openharmony', explicit=True))
            self.assertEqual(runtime.active, 'openharmony')
            self.assertEqual(runtime.state['identity'], 'one-existing-parent')
            self.assertIsNone(runtime.state['pending'])
            self.assertEqual(runtime.state['last_good'], 'openharmony')

    def test_startup_failure_rolls_back_and_retains_failure_for_next_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Engine(Path(directory))
            asyncio.run(runtime.switch('legacy'))
            runtime.fail.add('openharmony')
            asyncio.run(runtime.switch('openharmony', explicit=True))
            self.assertEqual(runtime.active, 'legacy')
            self.assertEqual(runtime.state['failed_backend'], 'openharmony')
            restored = Engine(Path(directory))
            self.assertEqual(restored.state['last_good'], 'legacy')
            self.assertEqual(restored.state['failed_backend'], 'openharmony')
            self.assertEqual(runtime.calls[-1], ('launch', 'legacy'))

    def test_preflight_failure_does_not_stop_working_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Engine(Path(directory))
            asyncio.run(runtime.switch('legacy'))
            before = list(runtime.calls)
            runtime.invalid.add('openharmony')
            with self.assertRaises(ValueError):
                asyncio.run(runtime.switch('openharmony'))
            self.assertEqual(runtime.calls, before)
            self.assertEqual(runtime.active, 'legacy')

    def test_foreign_identity_journal_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'lifecycle.json'
            path.write_text(json.dumps({'version': 1, 'identity': 'another-parent', 'active': 'legacy', 'last_good': 'legacy'}))
            with self.assertRaises(ValueError):
                Engine(Path(directory))
