"""合成冷备验证离线组装、原始字节保留与失败时不发布目录。"""
import asyncio
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import ha_gateway_prepare as preparing
import test_ha_paired_identity_import as fixtures


class GatewayPrepareTest(unittest.TestCase):
    setUp = fixtures.PairedIdentityImportTest.setUp
    write = fixtures.PairedIdentityImportTest.write
    export = fixtures.PairedIdentityImportTest.export
    observations = fixtures.PairedIdentityImportTest.observations
    prepare_identity = fixtures.PairedIdentityImportTest.prepare

    def inputs(self):
        self.prepare_identity()
        self.mapping = self.root / 'mapping'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'tests/fixtures/capabilities', self.mapping)
        adapter = self.mapping / 'adapters/example/fan.json'
        value = json.loads(adapter.read_text()); value['match_rules'] = [{'type': 'model_exact', 'model': 'fixture-light'}]
        adapter.write_text(json.dumps(value))
        (self.mapping / 'compositions.json').write_text(json.dumps({'bundles': {'fan_light': [
            {'key': 'light', 'pid': 'fixture-light', 'primary': True, 'primary_role': 'main'}]}}))
        roles = self.root / 'roles.json'
        roles.write_text(json.dumps({'version': 1, 'devices': [{'id': 'virtual-light-source',
            'model': 'fixture-light', 'roles': {'main': 'light.fixture'}}]})); roles.chmod(0o600)
        snapshot = self.root / 'fresh.json'
        snapshot.write_text(json.dumps({**self.data, 'ha_version': preparing.TESTED_HA})); snapshot.chmod(0o600)
        ca = self.root / 'ca.pem'; ca.write_text('synthetic CA fixture')
        native = self.root / 'native'; native.mkdir()
        return {'cold': self.out, 'frozen': self.frozen, 'profiles': self.profiles, 'endpoint': self.endpoint,
            'instances': roles, 'snapshot': snapshot, 'native': native, 'ca': ca, 'mapping': self.mapping,
            'output': self.root / 'prepared'}

    def run_prepare(self, arguments):
        # 不伪造运行验收；此测试只替换原生文件输入，验证文件组装和保护边界。
        with patch.object(preparing, 'native_files', return_value=(
                {'bridge_poc': b'synthetic bridge', 'identity_state_probe': b'synthetic reader'},
                {'source_tree_sha256': '0' * 64})):
            return asyncio.run(preparing.prepare(**arguments))

    def test_original_identity_and_bindings_are_preserved_in_blocked_bundle(self):
        arguments = self.inputs()
        before = {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob('*') if p.is_file()}
        result = self.run_prepare(arguments)
        self.assertEqual((result['old_children'], result['new_children'], result['parent_gateways']), (1, 0, 1))
        self.assertTrue(result['activation_blocked'])
        directory = arguments['output'] / 'config' / preparing.RELATIVE
        self.assertEqual((directory / 'legacy/device_sn').read_bytes(), self.raw_SN)
        settings = json.loads((directory / 'deployment.json').read_text())
        self.assertTrue(settings['offline_preparation'])
        self.assertEqual(settings['startup_timeout'], 120)
        root = json.loads((directory / 'gateway.local.json').read_text())
        self.assertTrue(root['preserve_bound_identity'])
        self.assertEqual(root['pin_expires_at'], 1)
        self.assertEqual(root['ca_file'], '/config/' + preparing.RELATIVE + '/ca.pem')
        self.assertEqual(before, {str(p.relative_to(self.out)): p.read_bytes() for p in self.out.rglob('*') if p.is_file()})
        self.assertEqual(arguments['output'].stat().st_mode & 0o077, 0)
        for path in arguments['output'].rglob('*'):
            self.assertEqual(path.stat().st_mode & 0o077, 0)
        with self.assertRaises(ValueError):
            self.run_prepare(arguments)

    def test_foreign_role_or_parent_is_rejected_without_publishing_partial_bundle(self):
        arguments = self.inputs()
        roles = json.loads(arguments['instances'].read_text())
        for mode in ('foreign_role', 'foreign_parent'):
            changed = json.loads(json.dumps(roles))
            if mode == 'foreign_role':
                changed['devices'][0]['roles']['main'] = 'light.foreign'
            else:
                changed['parent_sn'] = 'second-parent'
            arguments['instances'].write_text(json.dumps(changed))
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.run_prepare(arguments)
            self.assertFalse(arguments['output'].exists())
            self.assertFalse(list(self.root.glob('.hwhomebridge-prepare-*')))

    def test_changed_registry_is_rejected_before_any_directory_is_written(self):
        arguments = self.inputs()
        snapshot = json.loads(arguments['snapshot'].read_text())
        snapshot['entities'][0]['device_id'] = 'foreign-source'
        arguments['snapshot'].write_text(json.dumps(snapshot))
        with self.assertRaisesRegex(ValueError, 'unchanged cold registry'):
            self.run_prepare(arguments)
        self.assertFalse(arguments['output'].exists())

    def test_missing_required_HA_service_is_rejected_without_publishing(self):
        arguments = self.inputs()
        snapshot = json.loads(arguments['snapshot'].read_text())
        snapshot['services']['light'].remove('turn_off')
        arguments['snapshot'].write_text(json.dumps(snapshot))
        with self.assertRaisesRegex(ValueError, 'control service is not registered'):
            self.run_prepare(arguments)
        self.assertFalse(arguments['output'].exists())

    def test_cloud_wire_alias_does_not_rewrite_the_profile_service_type(self):
        profile = {'prodId': 'fixture', 'protocolType': 'WiFi', 'deviceTypeId': '1',
            'manufacturerId': 'fixture', 'deviceModel': 'fixture', 'services': [{
                'serviceId': 'switch1', 'serviceType': 'switch', 'characteristics': [
                    {'characteristicName': 'on', 'characteristicType': 'int', 'method': 'RW'}]}]}
        row = {'pid': 'fixture', 'devType': '1', 'manu': 'fixture', 'model': 'fixture', 'services': {'switch1': 'switch1'}}
        definition = {'services': {'switch1': {'service_type': 'switch', 'cloud_service_type': 'switch1',
            'characteristics': {'on': {'type': 'int', 'method': 'RW'}}}}}
        preparing.check_profile(profile, row, definition)
        row['services']['switch1'] = 'unreviewed-alias'
        with self.assertRaisesRegex(ValueError, 'undeclared cloud service alias'):
            preparing.check_profile(profile, row, definition)
