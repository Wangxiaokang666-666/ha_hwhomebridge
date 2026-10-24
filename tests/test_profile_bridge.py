"""profile投影的协议边界；虚构profile和实体，不依赖私有产品文件。"""
from types import SimpleNamespace
import unittest

from framework_imports import load
from test_fan_capabilities import RecordingDispatcher

models, devices = load('product_registry'), load('virtual_device')
ProfileBridge = load('profile_bridge').ProfileBridge


def fixture():
    router = load('service_router').ServiceRouter()
    product = models.ProductDef(pid='fixture', name='virtual', services={
        'current': models.ServiceDef('current', 'electric', char_name='current', c_service_id='electric',
            value_type='float', ha_mapping=models.HAMapping(domain='sensor', action='read_only', value_attr='state')),
        'voltage': models.ServiceDef('voltage', 'electric', char_name='voltage', c_service_id='electric',
            value_type='float', ha_mapping=models.HAMapping(domain='sensor', action='read_only', value_attr='state')),
        'heatingTarget': models.ServiceDef('heatingTarget', 'heatingTarget', char_name='heatingTarget',
            ha_mapping=models.HAMapping(domain='number', action='set_value', value_attr='state'))})
    device = devices.VirtualDevice('test-sn', product, 'fake')
    for sid, definition in product.services.items():
        device.add_service_entry(sid, devices.ServiceEntry(definition.ha_mapping.domain + '.test_' + sid.lower(),
                                                         definition.ha_mapping.domain, sid))
    states = dict(zip(device.get_all_entity_ids(), [SimpleNamespace(state=value, attributes={})
                                                   for value in ('0.17', '220.56', '50')]))
    router.sn_manager.register(device)
    router.set_hass(SimpleNamespace(states=SimpleNamespace(get=states.get)))
    router.action_dispatcher = RecordingDispatcher()
    profile = {'services': [
        {'serviceId': 'electric', 'serviceType': 'electric', 'characteristics': [
            {'characteristicName': name, 'characteristicType': 'float', 'method': 'R'}
            for name in ('current', 'voltage')]},
        {'serviceId': 'temperature', 'serviceType': 'temperature', 'characteristics': [
            {'characteristicName': 'heatingTarget', 'characteristicType': 'int', 'method': 'RW', 'min': '0', 'max': '100'}]}]}
    return router, device, profile


class ProfileBridgeTests(unittest.TestCase):
    def test_missing_or_unavailable_source_does_not_report_a_false_device_state(self):
        for value in (None, 'unknown', 'unavailable'):
            router, device, profile = fixture()
            bridge = ProfileBridge(router, device, profile)
            router._hass.states.get = lambda entity: None if value is None else SimpleNamespace(state=value, attributes={})
            with self.subTest(state=value), self.assertRaises(ValueError):
                bridge.snapshot()
            self.assertFalse(router.action_dispatcher.calls)

    def test_internal_names_project_into_shared_cloud_service_without_mutation(self):
        router, device, profile = fixture()
        bridge = ProfileBridge(router, device, profile)
        self.assertEqual(bridge.services, {'electric': 'electric', 'temperature': 'temperature'})
        self.assertEqual(bridge.snapshot(), {'electric': {'current': .17, 'voltage': 220.56},
                                           'temperature': {'heatingTarget': 50}})
        self.assertIsNone(device.product.services['heatingTarget'].c_service_id)
        bridge.write('temperature', {'heatingTarget': 70})
        self.assertEqual(router.action_dispatcher.calls[-1], ('number', 'set_value', 'number.test_heatingtarget', {'value': 70}))

    def test_read_only_unknown_wrong_sid_and_invalid_values_never_dispatch(self):
        router, device, profile = fixture()
        bridge = ProfileBridge(router, device, profile)
        for sid, payload in [('electric', {'current': 1}), ('heatingTarget', {'heatingTarget': 50}),
                ('temperature', {'current': 1}), ('temperature', {'heatingTarget': 101}),
                ('temperature', {'heatingTarget': True}), ('temperature', {'heatingTarget': float('nan')}),
                ('temperature', {'heatingTarget': 70, 'unmapped': 1})]:
            with self.subTest(sid=sid), self.assertRaises(ValueError):
                bridge.write(sid, payload)
        self.assertFalse(router.action_dispatcher.calls)

    def test_missing_or_ambiguous_profile_mapping_refuses_setup(self):
        router, device, profile = fixture()
        profile['services'].append({**profile['services'][1], 'serviceId': 'another-temperature'})
        with self.assertRaises(ValueError):
            ProfileBridge(router, device, profile)

        profile['services'] = []
        with self.assertRaises(ValueError):
            ProfileBridge(router, device, profile)

    def test_real_app_partial_timer_patch_is_normalized_without_mutating_request(self):
        request = {'action': 1, 'delay': [{'end': '20990101T120000Z', 'sid': 'switch', 'para': 'on'}]}
        normalized = ProfileBridge._validate_timer(request)
        self.assertEqual(normalized['num'], 1)
        self.assertEqual(normalized['delay'][0]['enable'], 1)
        self.assertNotIn('num', request)
        self.assertNotIn('enable', request['delay'][0])
        self.assertEqual(ProfileBridge._validate_timer({'action': 2, 'delay': [{}]})['num'], 0)
        for invalid in ({**request, 'num': True}, {'action': 1, 'delay': [{'end': '20990101T120000Z', 'enable': True}]},
                        {'action': 1, 'delay': [{'end': '20000101T120000Z'}]}, {'action': 1, 'delay': []}):
            with self.assertRaises(ValueError):
                ProfileBridge._validate_timer(invalid)
