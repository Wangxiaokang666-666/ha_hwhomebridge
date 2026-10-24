import asyncio
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from framework_imports import load

module = load('diagnostics')


class DiagnosticsTest(unittest.TestCase):
    def run_diagnostic(self, current):
        hass = SimpleNamespace(data={'hwhomebridge': {'original': current}})
        entry = SimpleNamespace(entry_id='original', data={}, options={})
        return asyncio.run(module.async_get_config_entry_diagnostics(hass, entry))

    def manager(self):
        device = SimpleNamespace(ha_device_id='source', product=SimpleNamespace(pid='002'),
            sn='PRIVATE-CHILD-SN', service_entries={'switch': SimpleNamespace(entity_id='light.main')})
        return SimpleNamespace(get_all_devices=lambda: [device])

    def test_diagnostic_does_not_load_missing_legacy_module(self):
        with patch.dict(sys.modules, {module.__package__ + '.hwbridge': None}):
            result = self.run_diagnostic({'backend': 'legacy', 'runtime': None})
        self.assertFalse(result['runtime_bindings_available'])
        self.assertEqual(result['devices'], [])

    def test_current_resident_binding_and_callback_are_read_without_secrets(self):
        resident = SimpleNamespace(service_router=SimpleNamespace(sn_manager=self.manager()),
                                   bridge_work=True, lib='PRIVATE-KEY', ac='PRIVATE-AC')
        with patch.dict(sys.modules, {module.__package__ + '.hwbridge': resident}):
            result = self.run_diagnostic({'backend': 'legacy', 'runtime': None})
        self.assertTrue(result['runtime_bindings_available'])
        self.assertTrue(result['legacy_cloud_online_callback'])
        self.assertEqual(result['devices'][0]['services'], {'switch': 'light.main'})
        self.assertNotIn('PRIVATE', str(result))

    def test_managed_router_uses_its_own_manager(self):
        runtime = SimpleNamespace(router=SimpleNamespace(manager=self.manager()))
        result = self.run_diagnostic({'backend': 'openharmony', 'runtime': runtime})
        self.assertEqual(result['binding_source'], 'managed_router')
        self.assertTrue(result['runtime_bindings_available'])
        self.assertIsNone(result['legacy_cloud_online_callback'])

    def test_disabled_entry_does_not_reuse_resident_legacy_bindings(self):
        result = self.run_diagnostic({'backend': 'disabled', 'runtime': None})
        self.assertFalse(result['runtime_bindings_available'])
