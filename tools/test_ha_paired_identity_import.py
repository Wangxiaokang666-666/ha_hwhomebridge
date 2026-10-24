import asyncio
import json
import unittest

import ha_binding_freeze as freezing
import ha_paired_identity_import as importing
import test_ha_cold_export as fixtures
import test_ha_binding_freeze as binding_fixtures


class PairedIdentityImportTest(unittest.TestCase):
    setUp = fixtures.ColdExportTest.setUp
    write = fixtures.ColdExportTest.write
    export = fixtures.ColdExportTest.export
    observations = binding_fixtures.BindingFreezeTest.observations

    def prepare(self):
        self.raw_SN = b'SNPROBE-01234567\n'
        (self.config / fixtures.cold.RUNTIME / 'config/device_sn').write_bytes(self.raw_SN)
        self.export()
        self.frozen = self.root / 'frozen'
        asyncio.run(freezing.freeze(self.out, *self.observations(), self.frozen))
        self.profiles = self.root / 'profiles'
        self.profiles.mkdir()
        definitions = json.loads((fixtures.COMPONENT / 'config/product_registry.json').read_text())
        product = next(p for p in definitions['products'] if p['pid'] == '002')
        profile = {'prodId': '9MU7', 'protocolType': 'WiFi', 'deviceTypeId': '119',
            'manufacturerId': '002', 'deviceModel': 'virtual-model', 'services': [
                {'serviceId': sid, 'serviceType': value['service_type'], 'characteristics': [
                    {'characteristicName': value['char_name'], 'characteristicType': 'int', 'method': 'RW'}]}
                for sid, value in product['services'].items()]}
        (self.profiles / '9MU7-profile.json').write_text(json.dumps(profile))
        codec = freezing.core_module('identity_store')
        self.endpoint = self.root / 'endpoint.json'
        self.endpoint.write_text(json.dumps({'version': 1, 'read_only': True,
            'root_id_sha256': codec.digest(b'11111111-2222-4333-8444-555555555555'),
            'cloud_host_sha256': codec.digest(b'cloud.test.invalid'),
            'resolved_addresses': ['192.0.2.1'],
            'matched_owned_connections': [{'peer': '192.0.2.1', 'port': 5683, 'inode': '1234'}]}))
        self.endpoint.chmod(0o600)

    def imported(self, name='imported'):
        return importing.import_identity(self.out, self.frozen, self.profiles, self.endpoint, self.root / name)

    def test_identity_conversion_keeps_parent_bytes_and_every_old_cloud_id(self):
        self.prepare()
        result = self.imported()
        codec = freezing.core_module('identity_store')
        self.assertEqual(result['source_sn_sha256'], codec.digest(self.raw_SN))
        self.assertEqual(result['canonical_sn_sha256'], codec.digest(self.raw_SN[:-1]))
        self.assertEqual(len(result['children']), 1)
        self.assertFalse(result['adoption_ready'])
        catalog = json.loads((self.root / 'imported/ha-children.local.json').read_text())
        self.assertTrue(catalog[0]['existing_cloud_only'])
        self.assertEqual(catalog[0]['sn'], self.data['devices'][1]['identifiers'][0][2])
        child = json.loads((self.root / ('imported/ha-child-' + catalog[0]['key'] + '.json')).read_text())
        self.assertEqual(child['devId'], 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee')
        self.assertEqual((self.out / 'payload' / fixtures.cold.RUNTIME / 'config/device_sn').read_bytes(), self.raw_SN)
        for path in (self.root / 'imported').rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        with self.assertRaises(ValueError):
            self.imported()

    def test_changed_frozen_binding_and_wrong_endpoint_are_rejected_before_output(self):
        self.prepare()
        bindings = self.frozen / 'bindings.json'
        original = bindings.read_text()
        changed = json.loads(original)
        changed['devices'][0]['key'] = '../escape'
        bindings.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.imported()
        self.assertFalse((self.root / 'imported').exists())
        bindings.write_text(original)
        endpoint = json.loads(self.endpoint.read_text())
        endpoint['matched_owned_connections'][0]['peer'] = '198.51.100.1'
        self.endpoint.write_text(json.dumps(endpoint))
        with self.assertRaises(ValueError):
            self.imported()
        self.assertFalse((self.root / 'imported').exists())
