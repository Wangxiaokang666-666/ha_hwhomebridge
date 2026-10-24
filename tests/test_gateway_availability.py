"""单子设备离线、缓存失效和恢复；全部为虚构实体和原生接口替身。"""
import asyncio
import ctypes as C
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import urllib.request

from framework_imports import load
from test_gateway_lifecycle import Engine
from test_profile_bridge import fixture
from test_role_timer_composition import make_router

availability = load('availability')
callbacks_module = load('legacy_callbacks')
lifecycle = load('gateway_lifecycle')
transport = load('openharmony.transport')


class GatewayAvailabilityTests(unittest.TestCase):
    def test_legacy_projection_recovers_without_recreating_the_card(self):
        router, device, profile = fixture()
        bridge = load('profile_bridge').ProfileBridge(router, device, profile)
        original = bridge.snapshot()
        entity = device.get_entity_for_service('current')
        state = router._hass.states.get(entity)
        for unavailable in ('unknown', 'unavailable'):
            state.state = unavailable
            with self.assertRaises(availability.SourceUnavailable):
                bridge.snapshot()
        state.state = '0.17'
        self.assertEqual(bridge.snapshot(), original)
        # 未声明/未映射字段依然是配置错误，不能吞成暂时离线。
        router.get_char_state = lambda *args: '{}'
        with self.assertRaises(ValueError) as error:
            bridge.snapshot()
        self.assertNotIsInstance(error.exception, availability.SourceUnavailable)

    def test_missing_composition_entity_only_affects_its_own_card(self):
        router, source, config = make_router()
        roles = config['devices'][0]['roles']
        fan_before = router.snapshot('fan')
        ambient = source.data.pop(roles['ambient'])
        with self.assertRaises(availability.SourceUnavailable):
            router.snapshot('light')
        self.assertEqual(router.snapshot('fan'), fan_before)
        source.data[roles['ambient']] = ambient
        self.assertTrue(router.snapshot('light'))
        self.assertEqual(source.calls, [])

    def test_transport_keeps_available_entities_when_another_is_missing(self):
        scoped = load('ha_transport').ScopedTransport()
        scoped.bind({'light.present', 'fan.missing'})
        self.assertEqual(scoped.filter_states([
            {'entity_id': 'light.present', 'state': 'on'},
            {'entity_id': 'light.foreign', 'state': 'off'}]),
            {'light.present': {'entity_id': 'light.present', 'state': 'on'}})

    def test_gateway_rejects_offline_writes_before_service_dispatch(self):
        router, source, config = make_router()
        gateway = load('gateway_router').GatewayRouter()
        gateway.bridges, gateway.capabilities = {}, router
        source.data[config['devices'][0]['roles']['fan']]['state'] = 'unavailable'
        with self.assertRaises(availability.SourceUnavailable):
            gateway.write('fan', 'switch', {'on': 1})
        self.assertEqual(source.calls, [])
        self.assertTrue(gateway.snapshot('light'))

    def test_loopback_protocol_distinguishes_offline_from_bad_configuration(self):
        router = SimpleNamespace(snapshot=Mock(side_effect=[
            availability.SourceUnavailable(), ValueError('bad mapping'), {'switch': {'on': 1}}]))
        server = transport.make_server(router, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def read():
                request = urllib.request.Request('http://127.0.0.1:%d/bridge' % server.server_port,
                    data=b'{"key":"virtual"}', headers={'Content-Type': 'application/json'})
                with opener.open(request, timeout=3) as response:
                    return json.load(response)
            self.assertEqual(read(), {'ok': False, 'available': False, 'error': 'SourceUnavailable'})
            self.assertEqual(read(), {'ok': False, 'error': 'ValueError'})
            self.assertEqual(read(), {'ok': True, 'available': True, 'state': {'switch': {'on': 1}}})
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_unconfirmed_write_is_not_replayed_and_diagnostics_do_not_log_payload(self):
        actual = {'switch': {'on': 0}}
        def applied_but_unconfirmed(key, sid, data):
            actual[sid] = data
            raise TimeoutError('private-payload-sentinel')
        router = SimpleNamespace(write=Mock(side_effect=applied_but_unconfirmed))
        server = transport.make_server(router, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request('http://127.0.0.1:%d/bridge' % server.server_port,
                data=b'{"key":"private-card-sentinel","sid":"switch","data":{"on":1}}',
                headers={'Content-Type': 'application/json', 'X-HWHB-Request-Id': '17'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with self.assertLogs(transport._LOGGER, level='WARNING') as captured:
                with opener.open(request, timeout=3) as response:
                    self.assertEqual(json.load(response), {'ok': False, 'error': 'TimeoutError'})
            self.assertEqual(actual['switch'], {'on': 1})
            self.assertEqual(router.write.call_count, 1)
            line = captured.records[0].getMessage()
            report = json.loads(line.split('bridge_result ', 1)[1])
            self.assertEqual((report['request_id'], report['error_type']), ('17', 'TimeoutError'))
            self.assertNotIn('sentinel', line)
            self.assertNotIn('data', report)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=3)

    def test_startup_skips_only_typed_source_outage(self):
        for error in (availability.SourceUnavailable(), ValueError('profile mismatch')):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                runtime = Engine(Path(directory))
                runtime.deployment.catalog = [{'key': 'offline'}, {'key': 'healthy'}]
                runtime.deployment.port = 0
                router = SimpleNamespace(snapshot=Mock(side_effect=[error, {'switch': {'on': 1}}]))
                server = SimpleNamespace(serve_forever=lambda: None, shutdown=lambda: None, server_close=lambda: None)
                async def run():
                    try:
                        await runtime.start('openharmony', 'fresh')
                    finally:
                        await runtime.stop()
                with patch.object(lifecycle.GatewayRouter, 'create', new=AsyncMock(return_value=router)), \
                        patch.object(lifecycle, 'make_server', return_value=server):
                    if isinstance(error, availability.SourceUnavailable):
                        asyncio.run(run())
                        self.assertIn(('launch', 'openharmony'), runtime.calls)
                        self.assertEqual(router.snapshot.call_count, 2)
                    else:
                        with self.assertRaises(ValueError):
                            asyncio.run(run())
                        self.assertFalse(any(call[0] == 'launch' for call in runtime.calls))

    def test_old_sdk_cache_is_invalidated_and_identical_recovery_is_reported(self):
        catalog = [{'sn': key, 'key': key, 'sdk_index': 1, 'services': {'switch': 'switch'}}
                   for key in ('old-light', 'other-light')]
        snapshots = {b'other-light': {'switch': {'on': 1}}}
        callbacks = callbacks_module.LegacyCallbacks(catalog, {}, {}, snapshots)
        library = Mock()
        publisher = callbacks_module.LegacyPublisher(library, callbacks)
        publisher.publish(True)
        self.assertEqual(library.HilinkSyncBrgDevStatus.call_args_list,
            [((b'old-light', 0),), ((b'other-light', 1),)])
        self.assertFalse(callbacks.get(b'old-light', b'switch'))
        callbacks.action(b'old-light', b'{"on":1}')
        self.assertFalse(callbacks.pending)
        callbacks.update(b'old-light', {'switch': {'on': 1}})
        publisher.publish(True)
        self.assertEqual(json.loads(C.string_at(callbacks.get(b'old-light', b'switch'))), {'on': 1})
        before = library.UpdateHAStatus.call_count
        publisher.invalidate(b'old-light')
        publisher.publish(True)
        self.assertFalse(callbacks.get(b'old-light', b'switch'))
        self.assertEqual(library.UpdateHAStatus.call_count, before)
        callbacks.update(b'old-light', {'switch': {'on': 1}})
        publisher.publish(True)
        self.assertEqual(library.UpdateHAStatus.call_count, before + 1)
        # 根网关重连也必须重新宣布状态和上报，不能沿用旧连接的缓存。
        publisher.publish(False)
        publisher.publish(True)
        self.assertEqual(library.UpdateHAStatus.call_count, before + 3)
        self.assertTrue(all(call.args[1] in (0, 1) for call in library.HilinkSyncBrgDevStatus.call_args_list))
