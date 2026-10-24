"""离线观察证据不能掩盖变更、换主、陈旧绑定或变成接管许可。"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import ha_maintenance_review as review


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folders = [self.root / name for name in ('before', 'after', 'host-before', 'host-after')]
        for path in self.folders:
            path.mkdir()
        for path in self.folders[:2]:
            self.write(path / 'online-copy.private.json', {'files': {'config/product_registry.json': 'hash', 'hwbridge.py': 'code'}})
            self.write(path / 'hwhomebridge.entries.private.json', {'entries': [{'entry_id': 'private-entry'}]})
        host = {key: 'same-private-value' for key in review.HOST_FIELDS}
        host.update(host_channel_verified=True, host_root_systemd=True)
        for path in self.folders[2:]:
            self.write(path / 'host-preflight.private.json', host)
        self.target = {'source_id': 'private-source', 'sn': 'private-sn', 'pid': '002',
                       'entities': {'switch': 'light.private_main'},
                       'ambiguous_choices': {'switch': ['light.private_main', 'light.private_ambient']}}
        self.preflight = {'kind': 'hwhomebridge-entity-preflight', 'inventory_complete': True,
                          'two_pass_registry_stable': True, 'legacy_bindings': [self.target],
                          'config_sha256': {'config/product_registry.json': 'hash'},
                          'legacy_inventory': [{}], 'proposed_roles': [{'roles': {'main': 'light.private_main'}}],
                          'compositions': [{}], 'issues': [{'code': 'legacy_mapping_order_ambiguous'}]}
        self.binding = {'kind': 'hwhomebridge-existing-log-binding-audit',
                        'source_id': self.target['source_id'], 'sn': self.target['sn'],
                        'observed_order_replay': self.target['entities'],
                        'ambiguous_choices': deepcopy(self.target['ambiguous_choices']),
                        'historical_routes': [], 'original_runtime_bindings_confirmed': False}

    def write(self, path, value):
        path.write_text(json.dumps(value))

    def assess(self):
        return review.review(*self.folders, self.preflight, self.binding)

    def test_consistent_observations_preserve_ambiguity_and_redact_identifiers(self):
        result = self.assess()
        self.assertTrue(result['observations_consistent'])
        self.assertEqual(result['target_ambiguous_services'], ['switch'])
        self.assertNotIn('private', json.dumps(result))
        for key in ('original_runtime_bindings_confirmed', 'cold_backup', 'migration_allowed', 'production_install_allowed'):
            self.assertFalse(result[key])

    def test_component_change_not_hidden_by_equal_file_count(self):
        self.write(self.folders[1] / 'online-copy.private.json', {'files': {'config/product_registry.json': 'hash', 'hwbridge.py': 'changed'}})
        self.assertFalse(self.assess()['observations_consistent'])

    def test_entry_change_detected(self):
        self.write(self.folders[1] / 'hwhomebridge.entries.private.json', {'entries': []})
        self.assertFalse(self.assess()['owned_entries_unchanged'])

    def test_same_container_with_new_start_time_detected(self):
        path = self.folders[3] / 'host-preflight.private.json'
        host = review.load(path)
        host['core_started_at'] = 'new-start'
        self.write(path, host)
        self.assertFalse(self.assess()['same_running_Core_and_host'])

    def test_stale_adapter_observation_detected(self):
        self.preflight['config_sha256']['config/product_registry.json'] = 'stale'
        self.assertFalse(self.assess()['adapter_copy_matches_entity_preflight'])

    def test_incomplete_or_mismatched_binding_refused(self):
        self.binding['sn'] = 'other-private-sn'
        with self.assertRaises(ValueError):
            self.assess()
        self.binding['sn'] = self.target['sn']
        self.preflight['two_pass_registry_stable'] = False
        with self.assertRaises(ValueError):
            self.assess()

    def test_historical_route_does_not_confirm_current_mapping(self):
        self.binding['historical_routes'] = [{'entity': 'light.private_main'}]
        self.assertEqual(self.assess()['historical_route_count'], 1)
        self.assertFalse(self.assess()['original_runtime_bindings_confirmed'])
        self.binding['original_runtime_bindings_confirmed'] = True
        with self.assertRaises(ValueError):
            self.assess()


if __name__ == '__main__':
    unittest.main()
