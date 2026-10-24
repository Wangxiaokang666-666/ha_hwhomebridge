"""使用虚构凭据覆盖离线转换的损坏、错误密钥和旧回调栈边界。"""
import ctypes as C
import hashlib
import hmac
import json
import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from legacy_identity_import import build_store, decode, hkdf, prepare
from single_engine_owner import LegacyCallbacks


AC = bytes(range(48))
MAC = '02:11:22:33:44:55'
ROOT_ID = '11111111-2222-4333-8444-555555555555'
CHILD_ID = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'


def encrypt(data, salt):
    key = hmac.digest(AC[:16] + bytes.fromhex(MAC.replace(':', '')), salt, 'sha256')
    encoder = Cipher(algorithms.AES(key[:16]), modes.CBC(key[16:])).encryptor()
    return encoder.update(data) + encoder.finalize()


def fixture(count=1, duplicate=False):
    config = bytearray(1588)
    struct.pack_into('<I', config, 0, 1552)
    config[36] = 3
    config[44:60] = bytes(range(16))
    login = ROOT_ID.encode().ljust(40, b'\0') + b's' * 32 + bytes(8) + b'p' * 16
    config[76:172] = encrypt(login, config[44:60])
    config[172:236] = b'cloud.test.invalid'.ljust(64, b'\0')
    config[400:440] = (b'a' * 32).ljust(40, b'\0')
    config[440:456] = encrypt(b'c' * 16, config[44:60])
    config[1244:1260] = bytes(range(16, 32))
    key = hkdf(AC[:16] + bytes.fromhex(MAC.replace(':', '')), bytes(config[1244:1260]), b'hilink_hkdf_base_info')
    encoder = Cipher(algorithms.AES(key[:16]), modes.CBC(key[16:])).encryptor()
    config[1260:1324] = encoder.update(b'f' * 64) + encoder.finalize()
    config[4:36] = hashlib.sha256(config[36:]).digest()
    bridge = bytearray(5192)
    struct.pack_into('<II', bridge, 0, 5136, 4)
    bridge[40:56] = bytes(range(32, 48))
    payload = struct.pack('<I', count) + bytes(12)
    for index in range(count):
        child_id = CHILD_ID if index == 0 or duplicate else f'aaaaaaaa-bbbb-4ccc-8ddd-{index:012x}'
        sn = 'HB0123456789abcd' if index == 0 or duplicate else f'HB{index:014x}'
        payload += child_id.encode().ljust(40, b'\0') + sn.encode().ljust(40, b'\0')
    payload = payload.ljust(5136, b'\0')
    bridge[56:] = encrypt(payload, bridge[40:56])
    bridge[8:40] = hashlib.sha256(bridge[40:5176]).digest()
    return bytes(config), bytes(bridge)


class OfflineImportTest(unittest.TestCase):
    def test_fields_and_target_authenticated_storage(self):
        config, bridge = fixture()
        identity = decode(config, bridge, AC, MAC)
        self.assertEqual(identity['dev_id'], ROOT_ID)
        self.assertEqual(identity['child_id'], CHILD_ID)
        self.assertEqual(identity['auth_code'], b'c' * 16)
        gateway = dict(sn='OHPOC-0123456789', model='TEST', manuName='TEST', ac_key_hex=AC.hex())
        stores = build_store(identity, gateway, 5683)
        udid = hashlib.sha256(b'TESTTESTOHPOC-0123456789').hexdigest().encode()
        for name, data in stores.items():
            key = hkdf(AC, data[4:20], udid)
            self.assertEqual(struct.unpack_from('<I', data, 52)[0], len(data) - 56)
            if name == 'dev_data':
                plain = AESGCM(key[:16]).decrypt(key[16:], data[56:] + data[20:36], None)
                self.assertEqual(plain[2:43].rstrip(b'\0').decode(), ROOT_ID)
            else:
                self.assertEqual(hmac.digest(key, data[56:], 'sha256'), data[20:52])
                self.assertEqual(data[56:58], b'\1\2')

    def test_wrong_ac_mac_and_corruption_are_rejected(self):
        config, bridge = fixture()
        for ac, mac in [(bytes(48), MAC), (AC, '02:11:22:33:44:56')]:
            with self.assertRaises(ValueError):
                decode(config, bridge, ac, mac)
        for source, position in [('config', 100), ('bridge', 100), ('bridge', 5191)]:
            values = {'config': config, 'bridge': bridge}
            broken = bytearray(values[source]); broken[position] ^= 1; values[source] = bytes(broken)
            with self.assertRaises(ValueError):
                decode(values['config'], values['bridge'], AC, MAC)

    def test_unknown_versions_and_invalid_child_records_refused(self):
        config, bridge = fixture()
        bad = bytearray(config); bad[36] = 4
        bad[4:36] = hashlib.sha256(bad[36:]).digest()
        with self.assertRaises(ValueError):
            decode(bytes(bad), bridge, AC, MAC)
        bad = bytearray(bridge); bad[4] = 5
        with self.assertRaises(ValueError):
            decode(config, bytes(bad), AC, MAC)
        bad = bytearray(bridge)
        payload = struct.pack('<I', 2) + bytes(5132)
        bad[56:] = encrypt(payload, bad[40:56])
        bad[8:40] = hashlib.sha256(bad[40:5176]).digest()
        with self.assertRaises(ValueError):
            decode(config, bytes(bad), AC, MAC)

    def test_multiple_records_and_capacity_preserve_individual_cloud_ids(self):
        for count in (2, 8, 64):
            config, bridge = fixture(count)
            children = decode(config, bridge, AC, MAC)['children']
            self.assertEqual(len(children), count)
            self.assertEqual(len({row['sn'] for row in children}), count)
        config, bridge = fixture(2, duplicate=True)
        with self.assertRaises(ValueError):
            decode(config, bridge, AC, MAC)

    def test_import_requires_complete_unique_inventory_and_explicit_new_children(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ('legacy', 'runtime'):
                (root / name).mkdir(mode=0o700)
            def write(name, value):
                data = value if isinstance(value, bytes) else json.dumps(value).encode()
                p = root / name; p.write_bytes(data); p.chmod(0o600)
            write('scope.json', {'version': 1, 'purpose': 'single-engine-virtual-only', 'production_access': False})
            gateway = dict(sn='OHPOC-0123456789', model='TEST', manuName='TEST', ac_key_hex=AC.hex())
            write('gateway.local.json', gateway)
            write('legacy/device_ac', AC); write('legacy/device_sn', gateway['sn'].encode() + b'\0')
            config, bridge = fixture(8)
            write('legacy/config.cfg', config); write('legacy/bridge.cfg', bridge)
            sdk = root / 'sdk'; sdk.write_bytes(b'version-pinned-test')
            from legacy_category_fixture import CLOUD_PIDS
            catalog = [{'key': 'old-' + pid, 'framework_pid': pid, 'pid': cloud, 'sdk_index': index,
                        'sn': 'HB0123456789abcd' if index == 0 else f'HB{index:014x}'}
                       for index, (pid, cloud) in enumerate(CLOUD_PIDS.items())]
            with patch('legacy_identity_import.SDK_SHA256', hashlib.sha256(sdk.read_bytes()).hexdigest()):
                for invalid in (catalog[:-1], catalog + [catalog[0]], [{**catalog[0], 'sdk_index': 7}] + catalog[1:]):
                    write('runtime/ha-children.local.json', invalid)
                    with self.assertRaises(ValueError):
                        prepare(root, root / 'rejected', MAC, 5683, sdk)
                    self.assertFalse((root / 'rejected').exists())
                new = {'key': 'fan', 'pid': '28BF', 'sn': 'OHPOC-HA-new'}
                write('runtime/ha-children.local.json', catalog + [new])
                for enabled, name in [(False, 'old-only'), (True, 'combined')]:
                    receipt = prepare(root, root / name, MAC, 5683, sdk, include_new_children=enabled)
                    target = json.loads((root / name / 'ha-children.local.json').read_text())
                    self.assertEqual(len(receipt['children']), 8)
                    self.assertEqual(len(target), 8 + int(enabled))
                    self.assertTrue(all(row['existing_cloud_only'] for row in target[:8]))
                self.assertEqual((root / 'legacy/config.cfg').read_bytes(), config)
                self.assertEqual((root / 'legacy/bridge.cfg').read_bytes(), bridge)


class CallbackBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.callbacks = LegacyCallbacks([{'sn': 'TEST', 'sdk_index': 1, 'services': {'switch': 'switch'}}],
                                         {'pin': '12345678', 'pin_expires_at': 0},
                                         self.state, {b'TEST': {'switch': {'on': 1}}})

    def test_native_callbacks_use_cache_and_bounded_raw_queue(self):
        with patch('json.loads', side_effect=AssertionError('JSON must run on owner thread')), \
             patch('json.dumps', side_effect=AssertionError('encoding must run on owner thread')), \
             patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('HTTP on callback stack')):
            for _ in range(40):
                self.callbacks.action(b'TEST', b'{"on":0}')
            self.assertEqual(len(self.callbacks.pending), 32)
            self.assertEqual(self.state['write_rejected'], 8)
            self.assertEqual(C.string_at(self.callbacks.get(b'TEST', b'switch')), b'{"on":1}')
            self.assertEqual(self.callbacks.status(b'TEST', b'switch'), 1)
        self.callbacks.update(b'TEST', {'switch': {'on': 0}})
        self.assertEqual(json.loads(C.string_at(self.callbacks.get(b'TEST', b'switch'))), {'on': 0})

    def test_unknown_target_null_service_and_expired_pin(self):
        self.callbacks.action(b'OTHER', b'{"on":0}')
        self.callbacks.action(b'TEST', None)
        self.assertFalse(self.callbacks.pending)
        self.assertFalse(self.callbacks.get(b'TEST', None))
        self.assertFalse(self.callbacks.get(b'OTHER', b'switch'))
        self.assertFalse(self.callbacks.status(b'TEST', None))
        self.assertEqual(self.callbacks.kind(b'OTHER'), -1)
        self.assertEqual(self.callbacks.pin(), 0)

    def test_multiple_children_have_separate_buffers_and_native_indices(self):
        rows = [{'sn': sn, 'sdk_index': index, 'services': {'switch': 'switch'}}
                for sn, index in [('ONE', 0), ('TWO', 7)]]
        callbacks = LegacyCallbacks(rows, {'pin': '0', 'pin_expires_at': 0}, {}, {
            b'ONE': {'switch': {'on': 0}}, b'TWO': {'switch': {'on': 1}}})
        self.assertEqual(callbacks.kind(b'ONE'), 0)
        self.assertEqual(callbacks.kind(b'TWO'), 7)
        first = callbacks.get(b'ONE', b'switch')
        second = callbacks.get(b'TWO', b'switch')
        self.assertNotEqual(first, second)
        self.assertEqual(C.string_at(first), b'{"on":0}')
        self.assertEqual(C.string_at(second), b'{"on":1}')
        callbacks.action(b'TWO', b'{"on":0}')
        self.assertEqual(callbacks.pending.popleft(), (b'TWO', b'{"on":0}'))


if __name__ == '__main__':
    unittest.main()
