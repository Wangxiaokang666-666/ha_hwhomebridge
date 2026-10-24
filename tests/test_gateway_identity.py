"""身份兼容只解析原字节；测试不加载SDK或连接HA。"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from framework_imports import load

store = load('identity_store')
deployment = load('gateway_deployment')
configuration = load('openharmony.configuration')
products = load('product_registry')


class GatewayIdentityTest(unittest.TestCase):
    def test_offline_production_preparation_cannot_start_either_engine(self):
        target = object.__new__(deployment.GatewayDeployment)
        target.settings = {'offline_preparation': True}
        for mode in ('legacy', 'openharmony'):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, 'offline production preparation'):
                target.verify_engine(mode)

    def test_old_managed_native_directory_is_refused_before_executable_or_probe_access(self):
        target = object.__new__(deployment.GatewayDeployment)
        target.interface, target.settings = 'fixture0', {}
        target.verify_source = Mock()
        target.verify_catalog = Mock()
        target.verify_inputs = Mock()
        with patch.object(deployment.socket, 'if_nameindex', return_value=[(1, 'fixture0')]):
            with self.assertRaisesRegex(ValueError, 'availability protocol 1'):
                target.verify_engine('openharmony')

    def test_frozen_mapping_fingerprint_changes_when_control_semantics_change(self):
        product = products.ProductDef('002', services={'switch': products.ServiceDef('switch', 'switch', 'on',
            ha_mapping=products.HAMapping(domain='light', action='turn_on_off'))})
        signature = products.product_fingerprint(product)
        product.services['switch'].ha_mapping.domain = 'switch'
        self.assertNotEqual(signature, products.product_fingerprint(product))

    def test_known_SN_formats_have_same_canonical_value_without_changing_bytes(self):
        for suffix in (b'\0', b'\n'):
            raw = b'SNPROBE-01234567' + suffix
            self.assertEqual(store.legacy_gateway_sn(raw), 'SNPROBE-01234567')
            self.assertEqual(raw[-1:], suffix)

    def test_truncated_or_ambiguous_SN_files_are_rejected(self):
        for raw in (b'', b'\n', b'A' * 17 + b'\n', b'A' * 17 + b'\0', b'ABC', b'ABC\n', b'ABC\0', b'ABC\r\n',
                    b'ABC\n\n', b'AB\0C\0', b'AB C\n', b'\xff\n', 'ABC\n'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                store.legacy_gateway_sn(raw)

    def test_managed_children_are_stable_and_scoped_and_leave_experiment_algorithm_unchanged(self):
        self.assertEqual(configuration.managed_child_sn('OHPOC-0123456789', 'fan'),
                         configuration.child_sn('OHPOC-0123456789', 'fan'))
        sn = configuration.managed_child_sn('SNPROBE-01234567', 'fan')
        self.assertEqual(sn, configuration.managed_child_sn('SNPROBE-01234567', 'fan'))
        self.assertNotEqual(sn, configuration.managed_child_sn('SNPROBE-76543210', 'fan'))
        self.assertNotEqual(sn, configuration.managed_child_sn('SNPROBE-01234567', 'ambient'))
        self.assertLess(len(sn), 40)
        for parent, key in [('parent\n', 'fan'), ('p', '../fan'), ('p' * 17, 'fan'), ('p', '')]:
            with self.assertRaises(ValueError):
                configuration.managed_child_sn(parent, key)
        with self.assertRaises(ValueError):
            configuration.child_sn('SNPROBE-01234567', 'fan')

    def test_LF_adoption_requires_raw_byte_receipt_and_rejects_later_terminator_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = {'device_sn': b'SNPROBE-01234567\n', 'device_ac': bytes(range(48)),
                      'config.cfg': b'fake-root', 'bridge.cfg': b'fake-children'}
            for name, value in values.items():
                (root / name).write_bytes(value)
                (root / name).chmod(0o600)
            identity = {'dev_id': 'original-root', 'secret': 'secret', 'psk': b'psk',
                'uid_hash': 'uid', 'auth_id': 'auth', 'auth_code': b'code',
                'children': [{'sn': 'old-child', 'dev_id': 'old-cloud-id'}]}
            spec = deployment.GatewayDeployment.__new__(deployment.GatewayDeployment)
            spec.legacy, spec.settings = root, {'source_mac': '02:00:00:00:00:01'}
            spec.root = {'sn': 'SNPROBE-01234567', 'ac_key_hex': values['device_ac'].hex()}
            spec.identity = store.digest(identity['dev_id'].encode())
            spec.receipt = {'account_field_sha256': {k: store.digest(v if isinstance(v, bytes) else v.encode())
                for k, v in identity.items() if k not in ('dev_id', 'children')},
                'children': [{'sn_sha256': store.digest(b'old-child'), 'id_sha256': store.digest(b'old-cloud-id')}]}
            with patch.object(deployment, 'decode', return_value=identity):
                with self.assertRaises(ValueError):
                    spec.verify_source()
                spec.receipt['source_sn_sha256'] = store.digest(values['device_sn'])
                spec.receipt['legacy_sdk_sha256'] = store.SDK_SHA256
                spec.verify_source()
                (root / 'device_sn').write_bytes(b'SNPROBE-01234567\0')
                with self.assertRaises(ValueError):
                    spec.verify_source()
