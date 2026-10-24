"""框架接入回归：配置变更、组合身份、控制隔离、HA 事件循环和真实协议边界。"""
import asyncio
import copy
from datetime import datetime, timezone, timedelta
import importlib
from itertools import count
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from framework_loader import load_framework

framework = load_framework()
configuration = importlib.import_module(framework.__name__ + '.configuration')
transports = importlib.import_module(framework.__name__ + '.transport')
models = importlib.import_module(framework.__name__.rsplit('.', 1)[0] + '.product_registry')
routers = importlib.import_module(framework.__name__.rsplit('.', 1)[0] + '.service_router')
CONFIG = Path(framework.__file__).parent / 'config'


class FakeTransport(transports.ScopedTransport):
    def __init__(self, instance):
        self.roles = instance['devices'][0]['roles']
        self.allowed = frozenset()
        self.calls = []
        self.data = {}
        for role, entity in self.roles.items():
            attributes = {}
            if role == 'fan': attributes = {'percentage': 87, 'direction': 'reverse', 'preset_mode': None}
            if role == 'main': attributes = {'brightness': None, 'color_temp_kelvin': None, 'effect': None}
            value = 'on' if role == 'default_on' else '0' if entity.startswith(('number.', 'sensor.')) else 'off'
            self.data[entity] = {'entity_id': entity, 'state': value, 'attributes': attributes,
                                 'last_updated': datetime.now(timezone.utc).isoformat()}
    def states(self): return self.data
    def call(self, domain, action, entity, **values):
        self.validate_call(domain, action, entity, values)
        self.calls.append({'domain': domain, 'action': action, 'entity': entity, 'values': values})
        state = self.data[entity]
        if action == 'set_value': state['state'] = str(values['value'])
        elif action == 'set_direction': state['attributes']['direction'] = values['direction']
        else:
            state['state'] = 'off' if action == 'turn_off' else 'on'
            for name in ('percentage', 'preset_mode', 'brightness', 'color_temp_kelvin', 'effect'):
                if name in values: state['attributes'][name] = values[name]
            if entity == self.roles['main'] and action == 'turn_on':
                for name, value in {'brightness': 255, 'color_temp_kelvin': 4000, 'effect': '空场景'}.items():
                    if state['attributes'].get(name) is None: state['attributes'][name] = value
            if entity == self.roles['main'] and action == 'turn_off':
                for name in ('brightness', 'color_temp_kelvin', 'effect'): state['attributes'][name] = None
        for target in ('light', 'fan'):
            if entity == self.roles[target + '_timer_enable']:
                remaining = self.data[self.roles[target + '_timer_remaining']]
                remaining['state'] = self.data[self.roles[target + '_timer_duration']]['state'] if action == 'turn_on' else '0'
                remaining['last_updated'] = datetime.now(timezone.utc).isoformat()


def make_router(instance=None, config=CONFIG, transport=None):
    instance = copy.deepcopy(instance or configuration.read_json(CONFIG / 'instances.example.json'))
    transport = transport or FakeTransport(instance)
    router = asyncio.run(framework.OpenHarmonyRouter.create(transport, config, instance))
    router.settle_seconds = router.fan_settle_seconds = 0
    return router, transport, instance


class FrameworkTests(unittest.TestCase):
    def setUp(self): self.router, self.client, self.instance = make_router()
    def test_reuses_project_registry_router_and_virtual_device(self):
        self.assertIsInstance(self.router, routers.ServiceRouter)
        self.assertIsInstance(self.router.registry, models.ProductRegistry)
        self.assertEqual(len(self.router.devices), 8)
        self.assertEqual(len(self.router.sn_manager.device_virtual_device_map), 8)
        self.assertEqual(len({vd.source_device_id for vd in self.router.devices.values()}), 1)
    def test_deterministic_identity_and_composition(self):
        second, _, _ = make_router()
        self.assertEqual([v.sn for v in self.router.devices.values()], [v.sn for v in second.devices.values()])
        self.assertEqual(len({v.sn for v in self.router.devices.values()}), 8)
        self.assertEqual(self.router.registry.catalog(self.router.devices)[2]['services']['switch1'], 'switch1')
    def test_six_native_gears_and_natural_wind(self):
        for gear, percentage in enumerate((25, 37, 50, 62, 75, 87), 1):
            self.assertEqual(self.router.write('timer-controls', 'fan', {'gear': gear})['fan']['gear'], gear)
            self.assertEqual(self.client.calls[-1]['values'], {'percentage': percentage})
        state = self.router.write('timer-controls', 'fanMode', {'mode': 2})
        self.assertEqual(state['fanMode']['mode'], 2)
        self.assertEqual(state['fan']['gear'], 0)
    def test_explicit_sid_routes_two_power_channels_separately(self):
        self.router.write('timer-controls', 'switchFan', {'on': 1})
        self.assertEqual(self.client.calls[-1]['entity'], self.client.roles['fan'])
        self.router.write('timer-controls', 'switch', {'on': 1})
        self.assertEqual(self.client.calls[-1]['entity'], self.client.roles['main'])
        sn = self.router.devices['timer-controls'].sn
        with self.assertRaises(ValueError): self.router.route_action(sn, {'on': 1})
        self.assertEqual(self.router.route_action(sn, {'sid': 'switchFan', 'data': {'on': 0}})['switchFan']['on'], 0)
        self.assertEqual(json.loads(self.router.get_char_state(sn, 'switchFan')), {'on': 0})
        self.assertIsNone(self.router.get_char_state('foreign', 'switchFan'))
    def test_transport_cannot_override_configured_entity(self):
        with self.assertRaises(ValueError):self.client.call('light', 'turn_on', self.client.roles['main'], entity_id='light.foreign')
        self.assertEqual(self.client.calls, [])
    def test_paired_catalog_and_profile_guards(self):
        from framework_catalog import verify_catalog, ROOT
        with tempfile.TemporaryDirectory(prefix='runtime-framework-test-', dir=ROOT) as temporary:
            runtime = Path(temporary)
            with tempfile.NamedTemporaryFile(suffix='.local.json', dir=ROOT, mode='w+') as gateway:
                root = {'prodId':'ZG37','sn':self.instance['parent_sn'],'enable_virtual_child':True,'enable_ha_children':True}
                json.dump(root, gateway);gateway.flush()
                catalog = self.router.registry.catalog(self.router.devices)
                (runtime / 'ha-children.local.json').write_text(json.dumps(catalog))
                for key, vd in self.router.devices.items():
                    (runtime / f'ha-child-{key}.json').write_text(json.dumps({'sn':vd.sn,'parent_sn':root['sn'],'devId':'owned-test'}))
                self.assertEqual(verify_catalog(self.router, runtime, Path(gateway.name)), catalog)
                catalog[0]['model'] = 'different-product'
                (runtime / 'ha-children.local.json').write_text(json.dumps(catalog))
                with self.assertRaises(ValueError):verify_catalog(self.router, runtime, Path(gateway.name))
                catalog[0]['model'] = self.router.registry.definitions[catalog[0]['pid']]['cloud']['model']
                (runtime / 'ha-children.local.json').write_text(json.dumps(catalog))
                self.router.registry.definitions['2NAL']['services']['fan']['characteristics']['gear']['values'] = [9]
                with self.assertRaises(ValueError):verify_catalog(self.router, runtime, Path(gateway.name))
    def test_fan_power_on_avoids_native_off_slot(self):
        self.client.data[self.client.roles['fan']]['attributes']['percentage'] = 12
        self.router.write('timer-controls', 'switchFan', {'on': 1})
        self.assertEqual(self.client.calls[-1]['values'], {'percentage': 25})
    def test_light_ranges_and_off_omission(self):
        self.assertNotIn('brightness', self.router.snapshot('light'))
        for value in (1, 46, 99, 100):
            self.assertLessEqual(abs(self.router.write('light', 'brightness', {'brightness': value})['brightness']['brightness'] - value), 1)
        for kelvin in (2700, 4000, 6500):
            self.assertEqual(self.router.write('light', 'cct', {'colorTemperature': kelvin})['cct']['colorTemperature'], kelvin)
    def test_all_twelve_effects_and_settings(self):
        for group in range(3):
            for channel in range(1, 5):
                key, sid = f'effects-{group}', f'switch{channel}'
                state = self.router.write(key, sid, {'on': 1})
                self.assertEqual(state[sid]['on'], 1)
                self.assertEqual(state[sid]['name'], self.client.data[self.client.roles['main']]['attributes']['effect'])
                self.assertEqual(self.router.write(key, sid, {'on': 0})[sid]['on'], 0)
        for channel in range(1, 5):
            for on in (0, 1): self.assertEqual(self.router.write('light-options', f'switch{channel}', {'on': on})[f'switch{channel}']['on'], on)
    def test_direction_and_ambient(self):
        for on in (0, 1):
            self.assertEqual(self.router.write('timer-controls', 'verticalswing', {'verticalswing': on})['verticalswing']['verticalswing'], on)
            self.assertEqual(self.router.write('aux', 'switch2', {'on': on})['switch2']['on'], on)
    def test_sflt11_physical_reverse_reports_on_for_ha_forward(self):
        # 用户实测该型号的 HA 方向与物理正反转相反；外部状态回报也必须校正。
        for ha_direction, physical_reverse in (('forward', 1), ('reverse', 0)):
            self.client.data[self.client.roles['fan']]['attributes']['direction'] = ha_direction
            rebuilt, _, _ = make_router(self.instance, transport=self.client)
            for router in (self.router, rebuilt):
                self.assertEqual(router.snapshot('aux')['switch1']['on'], physical_reverse)
                self.assertEqual(router.snapshot('timer-controls')['verticalswing']['verticalswing'], physical_reverse)
        self.assertEqual(self.client.calls, [])

    def test_sflt11_reverse_commands_agree_across_cards(self):
        self.router.set_write_policy('fan_only')
        for key, sid, field in (('aux', 'switch1', 'on'), ('timer-controls', 'verticalswing', 'verticalswing')):
            for physical_reverse, ha_direction in ((1, 'forward'), (0, 'reverse')):
                self.router.write(key, sid, {field: physical_reverse})
                self.assertEqual(self.client.calls[-1], {'domain': 'fan', 'action': 'set_direction',
                    'entity': self.client.roles['fan'], 'values': {'direction': ha_direction}})
                self.assertEqual(self.router.snapshot('aux')['switch1']['on'], physical_reverse)
                self.assertEqual(self.router.snapshot('timer-controls')['verticalswing']['verticalswing'], physical_reverse)

    def test_standard_direction_is_not_inverted_for_other_models(self):
        standard = self.router.registry.merge(self.router.registry.get_product('2NAL'))
        self.assertEqual(standard.services['verticalswing'].ha_mapping.value_mapping.mapping_dict,
                         {'0': 'forward', '1': 'reverse'})
    def test_independent_native_timers_and_reconstruction(self):
        self.router.write('timer-controls', 'countdownht', {'Equipmentname': 1, 'timeing': 30, 'countdownht': 1})
        self.router.write('timer-controls', 'countdownht', {'Equipmentname': 2, 'timeing': 60, 'countdownht': 1})
        self.router.write('timer-controls', 'countdownht', {'countdownht': 0})
        self.assertEqual(self.router.snapshot('light')['delay']['num'], 1)
        self.assertEqual(self.router.snapshot('fan-wifi')['endTime']['time'], 0)
        rebuilt, _, _ = make_router(self.instance, transport=self.client)
        state = rebuilt.snapshot('timer-controls')['countdownht']
        self.assertEqual(state['Equipmentname'], 1)
        self.assertEqual(state['remainder'], 30)
    def test_timer_array_echo_and_cancel(self):
        end = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime('%Y%m%dT%H%M%SZ')
        self.router.write('light', 'delay', {'action': 1, 'num': 1, 'timer': [{'enable': 1, 'end': end}]})
        self.assertEqual(self.router.snapshot('light')['delay']['num'], 1)
        self.router.write('light', 'delay', {'action': 2, 'num': 1, 'timer': [{'id': 1}]})
        self.assertEqual(self.router.snapshot('light')['delay']['num'], 0)
        self.assertEqual(self.router.snapshot('fan-wifi')['endTime']['time'], 0)
    def test_invalid_requests_never_call_ha(self):
        invalid = [('timer-controls', 'fan', {'gear': True}), ('timer-controls', 'fan', {'gear': 7}),
                   ('fan-wifi', 'fan', {'speed': 8}), ('light', 'cct', {'colorTemperature': 2699}),
                   ('light', 'brightness', {'brightness': 50.5}), ('timer-controls', 'fanMode', {'mode': 1}),
                   ('timer-controls', 'countdownht', {'remainder': 1}),
                   ('timer-controls', 'countdownht', {'Equipmentname': 0, 'timeing': 721, 'countdownht': 1}),
                   ('aux', 'switch', {'on': 1}), ('foreign', 'switch', {'on': 1}),
                   ('light', 'switch', {'on': 1, 'entity_id': 'light.other'}),
                   ('light', 'delay', {'action': 1, 'num': 2, 'timer': []})]
        invalid.append(('light', 'delay', {'action': 2, 'num': 1, 'timer': [{'id': 99}]}))
        for args in invalid:
            with self.assertRaises(ValueError, msg=str(args)): self.router.write(*args)
        self.assertEqual(self.client.calls, [])
    def test_unavailable_is_not_off_and_inactive_cancel_is_noop(self):
        self.router.write('timer-controls', 'countdownht', {'Equipmentname': 0, 'countdownht': 0})
        self.assertEqual(self.client.calls, [])
        self.client.data[self.client.roles['fan']]['state'] = 'unavailable'
        with self.assertRaises(ValueError): self.router.snapshot('timer-controls')
    def test_second_device_uses_configured_entities_and_distinct_identity(self):
        instance = copy.deepcopy(self.instance)
        instance['devices'][0]['id'] = 'second-physical-device'
        instance['devices'][0]['key_prefix'] = 'second-'
        instance['devices'][0]['roles'] = {k: v.replace('example_', 'second_') for k, v in instance['devices'][0]['roles'].items()}
        router, client, _ = make_router(instance)
        router.write('second-timer-controls', 'fan', {'gear': 6})
        self.assertEqual(client.calls[-1]['entity'], 'fan.second_fan')
        self.assertNotEqual(router.devices['second-light'].sn, self.router.devices['light'].sn)
        self.assertEqual(self.client.calls, [])
    def test_new_model_and_speed_mapping_need_only_json_changes(self):
        import shutil
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / 'config';shutil.copytree(CONFIG, config)
            (config / 'adapters/xiaomi/sflt11.json').unlink()
            (config / 'adapters/xiaomi/another.json').write_text(json.dumps({'pid': '2NAL', 'composition': 'xiaomi_sflt11', 'match_rules': [{'type': 'model_exact', 'model': 'example.fan.six'}]}))
            instance = copy.deepcopy(self.instance);instance['devices'][0]['model'] = 'example.fan.six'
            router, client, _ = make_router(instance, config)
            router.write('timer-controls', 'fan', {'gear': 2})
            self.assertEqual(client.calls[-1]['values'], {'percentage': 33})
    def test_late_rollback_is_not_acknowledged(self):
        original = self.client.states;reads = []
        def states():
            data = original();data[self.client.roles['fan']]['attributes']['percentage'] = 37 if len(reads) < 4 else 25
            reads.append(1);return data
        self.client.states = states;self.router.fan_settle_seconds = 2;ticks = count(0, .1)
        shared_router = framework.__name__.rsplit('.', 1)[0] + '.capability_router'
        with patch(shared_router + '.time.monotonic', side_effect=lambda: next(ticks)), patch(shared_router + '.time.sleep'):
            with self.assertRaises(TimeoutError): self.router.write('timer-controls', 'fan', {'gear': 2})
    def test_http_read_only_and_duplicate_keys(self):
        server = transports.make_server(self.router, 0, read_only=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            def post(body):
                return json.load(opener.open(urllib.request.Request(f'http://127.0.0.1:{server.server_port}/bridge', data=body, headers={'Content-Type': 'application/json'})))
            self.assertTrue(post(b'{"key":"light"}')['ok'])
            self.assertFalse(post(b'{"key":"light","sid":"switch","data":{"on":1}}')['ok'])
            self.assertFalse(post(b'{"key":"light","key":"aux"}')['ok'])
            self.assertEqual(self.client.calls, [])
        finally:server.shutdown();server.server_close();thread.join()
    def test_fan_only_policy_rejects_light_and_mixed_timer_before_calls(self):
        self.router.set_write_policy('fan_only')
        invalid = [('light', 'switch', {'on': 1}), ('aux', 'switch2', {'on': 1}),
                   ('light-options', 'switch1', {'on': 1}), ('effects-0', 'switch1', {'on': 1}),
                   ('light', 'delay', {'action': 2, 'num': 1, 'timer': [{'id': 1}]}),
                   ('timer-controls', 'countdownht', {'Equipmentname': 0, 'timeing': 2, 'countdownht': 1}),
                   ('timer-controls', 'countdownht', {'Equipmentname': 1})]
        for request in invalid:
            with self.assertRaises(ValueError):
                self.router.write(*request)
        self.assertEqual(self.client.calls, [])
        mapping = self.router.devices['timer-controls'].product.services['countdownht'].ha_mapping
        mapping.options['targets']['0'].reverse()
        with self.assertRaises(ValueError):
            self.router.write('timer-controls', 'countdownht', {'Equipmentname': 0, 'timeing': 2, 'countdownht': 1})
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.router.forms, {})

    def test_fan_only_policy_keeps_fan_timer_and_direction_writable(self):
        self.router.set_write_policy('fan_only')
        self.router.write('timer-controls', 'verticalswing', {'verticalswing': 0})
        self.router.write('timer-controls', 'countdownht', {'Equipmentname': 2, 'timeing': 2, 'countdownht': 1})
        self.assertEqual(self.router.snapshot('fan-wifi')['endTime']['time'], 2)
        self.assertEqual(self.router.snapshot('light')['delay']['num'], 0)
        self.assertEqual({call['entity'] for call in self.client.calls},
                         {self.client.roles[k] for k in ('fan', 'fan_timer_enable', 'fan_timer_duration')})

    def test_read_only_and_unknown_policy_fail_closed(self):
        self.router.set_write_policy('read_only')
        for request in [('timer-controls', 'switchFan', {'on': 1}), ('fan-wifi', 'endTime', {'time': 2})]:
            with self.assertRaises(ValueError):
                self.router.write(*request)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(len(self.router.devices), 8)
        self.assertEqual(self.router.snapshot('fan-wifi')['switch'], {'on': 0})
        with self.assertRaises(ValueError):
            self.router.set_write_policy('all')

    def test_ha_transport_requires_worker_and_dispatches_on_ha_loop(self):
        roles = self.client.roles
        class State:
            def __init__(self, data):self.data = data
            def as_dict(self):return self.data
        async def exercise():
            calls = [];hass = type('Hass', (), {})();hass.loop = asyncio.get_running_loop()
            class States:
                def async_all(inner):return [State(x) for x in self.client.data.values()]
            class Services:
                async def async_call(inner, *args, **kwargs):
                    self.assertIs(asyncio.get_running_loop(), hass.loop);calls.append((args, kwargs))
            hass.states = States();hass.services = Services();transport = transports.HomeAssistantTransport(hass);transport.bind(set(roles.values()))
            with self.assertRaises(RuntimeError):transport.states()
            states = await asyncio.to_thread(transport.states);self.assertEqual(len(states), 13)
            await asyncio.to_thread(transport.call, 'fan', 'set_percentage', roles['fan'], percentage=50)
            self.assertEqual(calls[0][1]['blocking'], True)
            with self.assertRaises(ValueError):await asyncio.to_thread(transport.call, 'fan', 'turn_off', 'fan.other')
        asyncio.run(exercise())


if __name__ == '__main__': unittest.main()
