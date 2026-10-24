import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ha_binding_freeze as freezing
import ha_cloud_endpoint as endpoint
import test_ha_cold_export as fixtures


class BindingFreezeTest(unittest.TestCase):
    # 复用合成冷备构造；不读取生产资料、不加载原生库。
    setUp = fixtures.ColdExportTest.setUp
    write = fixtures.ColdExportTest.write
    export = fixtures.ColdExportTest.export

    def observations(self):
        diagnostic = {'observed_at_utc': '2026-10-18T00:00:00+00:00', 'HA_process_pid': 100,
            'selected_backend': 'legacy', 'active_backend': 'legacy', 'binding_source': 'resident_legacy_router',
            'runtime_bindings_available': True, 'legacy_cloud_online_callback': True,
            'devices': [{'source_device_id': 'virtual-light-source', 'framework_pid': '002',
                         'services': {'switch': 'light.fixture', 'brightness': 'light.fixture', 'cct': 'light.fixture'}}]}
        one = {'version': 1, 'kind': 'hwhomebridge-resident-binding-observation', 'entry_id': 'original-entry',
               'registry_sha256': self.report['registry_sha256'],
               'diagnostic': diagnostic}
        two = copy.deepcopy(one)
        two['diagnostic']['observed_at_utc'] = '2026-10-18T00:00:01+00:00'
        return one, two

    def frozen(self, one, two):
        return asyncio.run(freezing.freeze(self.out, one, two, self.root / 'frozen'))

    def test_stable_resident_observations_preserve_complete_old_binding(self):
        self.export()
        proof = self.frozen(*self.observations())
        self.assertTrue(proof['resident_entity_bindings_confirmed'])
        self.assertFalse(proof['adoption_ready'])
        row = json.loads((self.root / 'frozen/bindings.json').read_text())['devices'][0]
        self.assertEqual(row['cloud_pid'], '9MU7')
        self.assertEqual(set(row['entities'].values()), {'light.fixture'})
        self.assertEqual(row['sn'], self.data['devices'][1]['identifiers'][0][2])

    def test_all_read_only_outputs_refuse_the_original_HA_directory_before_network(self):
        self.export()
        target = self.config / 'not-allowed'
        with self.assertRaises(ValueError):
            asyncio.run(freezing.freeze(self.out, *self.observations(), target))
        token = SimpleNamespace(read_text=Mock(side_effect=AssertionError('must reject before authentication')))
        with self.assertRaises(ValueError):
            asyncio.run(freezing.observe('http://test.invalid', token, self.out, target))
        with patch.object(endpoint.socket, 'getaddrinfo', side_effect=AssertionError('must reject before DNS')):
            with self.assertRaises(ValueError):
                endpoint.collect(self.out, target, 'test.invalid', 22222, None, None)
        self.assertFalse(target.exists())

    def test_replay_other_entry_process_or_changed_binding_is_rejected(self):
        self.export()
        changes = [('binding_source', 'replay'), ('HA_process_pid', 101), ('active_backend', 'openharmony'),
                   ('legacy_cloud_online_callback', False)]
        for field, value in changes:
            one, two = self.observations()
            two['diagnostic'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.frozen(one, two)
        one, two = self.observations()
        one['entry_id'] = two['entry_id'] = 'other-entry'
        with self.assertRaises(ValueError):
            self.frozen(one, two)
        one, two = self.observations()
        two['registry_sha256'] = 'changed-owner-or-disabled-entity'
        with self.assertRaises(ValueError):
            self.frozen(one, two)

    def test_cross_device_invented_or_missing_service_is_rejected(self):
        self.export()
        for mapping in ({'switch': 'light.somewhere_else'}, {'switch': 'light.fixture'},
                        {'invented': 'light.fixture'}):
            one, two = self.observations()
            for item in (one, two):
                item['diagnostic']['devices'][0]['services'] = mapping
            with self.assertRaises(ValueError):
                self.frozen(one, two)

    def test_missing_or_extra_original_child_is_rejected(self):
        self.export()
        one, two = self.observations()
        for item in (one, two):
            item['diagnostic']['devices'].append({'source_device_id': 'outside', 'framework_pid': '002', 'services': {}})
        with self.assertRaises(ValueError):
            self.frozen(one, two)
