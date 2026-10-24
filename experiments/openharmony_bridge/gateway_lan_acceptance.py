"""已配对虚拟网关的加密 LAN 客户端；不接受家庭实体，不打印账号或密钥。"""
import argparse
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import secrets
import socket
import struct
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encode_option(number):
    if number < 13:
        return number, b''
    if number < 269:
        return 13, bytes([number - 13])
    return 14, struct.pack('!H', number - 269)


def packet(method, token, options, payload=b''):
    result = bytes([0x40 | len(token), method]) + secrets.token_bytes(2) + token
    previous = 0
    for number, value in sorted(options, key=lambda item: item[0]):
        delta, first = encode_option(number - previous)
        size, second = encode_option(len(value))
        result += bytes([delta << 4 | size]) + first + second + value
        previous = number
    return result + (b'\xff' + payload if payload else b'')


def parse_packet(data):
    if len(data) < 4 or data[0] >> 6 != 1:
        raise ValueError('invalid CoAP response')
    position = 4 + (data[0] & 15)
    token = data[4:position]
    while position < len(data) and data[position] != 255:
        head = data[position]; position += 1
        parts = []
        for part in (head >> 4, head & 15):
            if part == 13:
                part = 13 + data[position]; position += 1
            elif part == 14:
                part = 269 + int.from_bytes(data[position:position + 2], 'big'); position += 2
            elif part == 15:
                raise ValueError('invalid CoAP option')
            parts.append(part)
        position += parts[1]
    if position >= len(data):
        return token, b''
    return token, data[position + 1:]


class Client:
    def __init__(self, address, port, auth_code):
        self.address, self.port, self.auth_code = address, port, auth_code
        self.channel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.channel.settimeout(.25)
        self.puuid = secrets.token_hex(16).encode()
        self.sequence = 100

    def exchange(self, frame, token, port=None, encrypted=False, timeout=20):
        self.channel.sendto(frame, (self.address, port or self.port))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, peer = self.channel.recvfrom(65535)
            except socket.timeout:
                continue
            if peer[0] != self.address:
                continue
            if data[0] >> 4 & 3 == 0:
                self.channel.sendto(b'\x60\x00' + data[2:4], peer)
            received, payload = parse_packet(data)
            if not payload or received != token:
                continue
            if encrypted:
                raw = base64.b64decode(payload, validate=True)
                payload = AESGCM(self.key).decrypt(raw[:12], raw[12:], None)
            return json.loads(payload)
        raise TimeoutError('LAN reply not received')

    def negotiate(self):
        nonce = secrets.token_bytes(8)
        token = secrets.token_bytes(4)
        payload = json.dumps({'sn1': nonce.hex(), 'seq': self.sequence, 'modeSupport': 4}).encode()
        options = [(11, b'.sys'), (11, b'sessMngr'), (2056, self.puuid)]
        response = self.exchange(packet(2, token, options, payload), token)
        assert response['errcode'] == 0 and response['modeResp'] == 4
        self.sequence = response['seq']
        self.confirmed_sequence = self.sequence
        self.session = bytes.fromhex(response['sessId'])
        self.key = hashlib.pbkdf2_hmac('sha256', self.auth_code,
            nonce + bytes.fromhex(response['sn2']), 1, 16)

    def control(self, device_id, sid, data, *, tamper=False, replay=False):
        if not replay:
            self.sequence += 1
        token, nonce = secrets.token_bytes(4), secrets.token_bytes(12)
        payload = json.dumps({'seq': self.confirmed_sequence if replay else self.sequence,
            'data': [{'devId': device_id, 'sid': sid, 'data': data}]}).encode()
        raw = nonce + AESGCM(self.key).encrypt(nonce, payload, None)
        if tamper:
            raw = raw[:-1] + bytes([raw[-1] ^ 1])
        encrypted = base64.b64encode(raw)
        options = [(11, b'e2eCtrl'), (2048, self.session)]
        response = self.exchange(packet(2, token, options, encrypted), token, encrypted=True,
            timeout=2 if tamper or replay else 20)
        assert response['errcode'] == 0, 'SDK did not confirm LAN control'
        self.confirmed_sequence = self.sequence
        return response


def run(config, address, discovery_port, control_port, repeats=1):
    marker = json.loads((config / 'fixture-scope.json').read_text())
    assert marker == {'purpose': 'existing-gateway-ha-lifecycle-virtual-only', 'production_access': False}
    root = config / '.storage/hwhomebridge/gateways/current'
    gateway = json.loads((root / 'gateway.local.json').read_text())
    assert gateway['sn'].startswith('OHPOC-')
    entities = json.loads((config / 'fixture-states.private.json').read_text())
    assert all(entity.split('.')[1].startswith('oh_single_') for entity in entities)
    settings = json.loads((root / 'deployment.json').read_text())
    path = config / 'custom_components/hwhomebridge/identity_store.py'
    spec = importlib.util.spec_from_file_location('identity_store', path)
    store = importlib.util.module_from_spec(spec); spec.loader.exec_module(store)
    identity = store.decode(store.private_read(root / 'legacy/config.cfg'),
        store.private_read(root / 'legacy/bridge.cfg'), store.private_read(root / 'legacy/device_ac'), settings['source_mac'])
    client = Client(address, control_port, identity['auth_code'])
    try:
        token = secrets.token_bytes(4)
        discovery = client.exchange(packet(1, token,
            [(11, b'.well-known'), (11, b'core'), (15, b'st=cloudSetup')]), token, port=discovery_port)
        # 上游发现帧只暴露 SN 的前三/后三位；完整身份另由已配对本地发现核对。
        expected_sn = gateway['sn'][:3] + '*' + gateway['sn'][-3:]
        assert discovery.get('devInfo', {}).get('sn') == expected_sn, 'unexpected discovery identity'
        token = secrets.token_bytes(4)
        local = client.exchange(packet(1, token, [(11, b'.well-known'), (11, b'core'),
            (15, b'st=ohLocalControl'), (2056, client.puuid)]), token)
        assert local['devId'] == identity['dev_id'] and local['errcode'] == 0
        client.negotiate()
        catalog = json.loads((root / 'runtime/ha-children.local.json').read_text())
        products = json.loads((root / 'mapping/product_registry.json').read_text())['products']
        roles = json.loads((root / 'instances.json').read_text())['devices'][0]['roles']
        audit = config / 'fixture-service-calls.private.jsonl'
        native = root / 'openharmony.private.log'
        cases = []
        scenarios = [('fan-wifi', '28BF', 'fan', 'fan', [(1, 25), (6, 87)]),
            ('aux', '20UC', 'switch2', 'ambient', [(0, 'off'), (1, 'on')]),
            ('effects-2', '2151', 'switch4', 'main', [(1, 'on'), (0, 'off')])]
        for key, cloud_pid, sid, role, values in scenarios:
            row = next(row for row in catalog if row['key'] == key and row['pid'] == cloud_pid)
            child = json.loads((root / ('runtime/ha-child-' + row['key'] + '.json')).read_text())
            assert child['parent_sn'] == gateway['sn'] and child['sn'] == row['sn']
            assert sid in row['services']
            for value, expected in values * repeats:
                product = next(product for product in products if product['pid'] == cloud_pid)
                field = product['services'][sid]['char_name']
                audit_offset = audit.stat().st_size if audit.exists() else 0
                native_offset = native.stat().st_size
                before = json.loads((config / 'fixture-states.private.json').read_text())
                started = time.monotonic()
                client.control(child['devId'], sid, {field: value})
                after = json.loads((config / 'fixture-states.private.json').read_text())
                entity = roles[role]
                actual = after[entity]['attributes']['percentage'] if role == 'fan' else after[entity]['state']
                assert actual == expected, 'virtual HA readback differs from LAN request'
                assert all(after[name] == state for name, state in before.items() if name != entity)
                with audit.open() as stream:
                    stream.seek(audit_offset)
                    calls = [json.loads(line) for line in stream if line.strip()]
                assert len(calls) == 1 and calls[0]['entity'] == entity, 'unexpected HA call or replay'
                with native.open() as stream:
                    stream.seek(native_offset)
                    confirmed = [json.loads(line[3:]) for line in stream if line.startswith('HA ')
                        and json.loads(line[3:]).get('write_confirmed')]
                assert confirmed == [{'key': key, 'sid': sid, 'write_confirmed': True}]
                cases.append({'key': row['key'], 'sid': sid, 'value': value,
                    'SDK_encrypted_response_confirmed': True,
                    'HA_service_call_and_readback_confirmed': True, 'HA_write_calls': len(calls),
                    'elapsed_seconds': round(time.monotonic() - started, 3)})
        count = sum(1 for line in audit.open() if line.strip())
        fan = next(row for row in catalog if row['key'] == 'fan-wifi')
        child = json.loads((root / ('runtime/ha-child-' + fan['key'] + '.json')).read_text())
        rejected = []
        for name, flags in [('tampered_GCM', {'tamper': True}), ('replayed_sequence', {'replay': True})]:
            try:
                client.control(child['devId'], 'fan', {'speed': 1}, **flags)
            except TimeoutError:
                rejected.append(name)
            else:
                raise AssertionError('invalid LAN control was accepted')
        try:
            client.control(child['devId'], 'fan', {'speed': 8})
        except AssertionError:
            rejected.append('unsupported_gear')
        else:
            raise AssertionError('unsupported gear was accepted')
        assert sum(1 for line in audit.open() if line.strip()) == count
        proof = {'passed': True, 'production_access': False, 'discovery_identity_matched': True,
            'AES_GCM_session_confirmed': True, 'cases': cases,
            'invalid_controls_rejected_without_HA_calls': rejected,
            'app_LAN_verified': False, 'native_x86_verified': False}
        print(json.dumps(proof)); return proof
    finally:
        client.channel.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--address', required=True)
    parser.add_argument('--discovery-port', type=int, default=5683)
    parser.add_argument('--control-port', type=int, default=5686)
    parser.add_argument('--repeats', type=int, choices=range(1, 11), default=1)
    args = parser.parse_args()
    run(args.config, args.address, args.discovery_port, args.control_port, args.repeats)
