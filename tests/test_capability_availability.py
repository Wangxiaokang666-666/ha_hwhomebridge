"""公共能力的离线隔离与恢复；不导入实验后端或原生 SDK。"""
import unittest

from framework_imports import load
from test_role_timer_composition import make_router

availability = load('availability')


class CapabilityAvailabilityTests(unittest.TestCase):
    def test_missing_light_does_not_disable_fan_and_recovers(self):
        router, source, config = make_router()
        roles = config['devices'][0]['roles']
        before = router.snapshot('fan')
        missing = source.data.pop(roles['ambient'])
        with self.assertRaises(availability.SourceUnavailable):
            router.snapshot('light')
        self.assertEqual(router.snapshot('fan'), before)
        source.data[roles['ambient']] = missing
        self.assertTrue(router.snapshot('light'))

    def test_offline_source_rejects_write_before_dispatch(self):
        router, source, config = make_router()
        source.data[config['devices'][0]['roles']['fan']]['state'] = 'unavailable'
        with self.assertRaises(availability.SourceUnavailable):
            router.write('fan', 'switch', {'on': 1})
        self.assertEqual(source.calls, [])

    def test_missing_native_timer_rejects_write_before_dispatch(self):
        router, source, config = make_router()
        source.data.pop(config['devices'][0]['roles']['fan_timer_enable'])
        with self.assertRaises(availability.SourceUnavailable):
            router.write('fan', 'switch', {'on': 1})
        self.assertEqual(source.calls, [])

    def test_transport_filters_foreign_states_without_requiring_every_entity(self):
        transport = load('ha_transport').ScopedTransport()
        transport.bind({'light.present', 'fan.missing'})
        present = {'entity_id': 'light.present', 'state': 'on'}
        self.assertEqual(transport.filter_states([present,
            {'entity_id': 'light.foreign', 'state': 'off'}]), {'light.present': present})
