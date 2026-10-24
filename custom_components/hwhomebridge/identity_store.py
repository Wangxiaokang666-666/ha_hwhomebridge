"""已核对旧SDK格式的只读身份解码；不启动网络，不改写旧KV。"""
import hashlib
import hmac
import os
import re
import struct
import uuid
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

SDK_SHA256 = '0579e6f3cee0320d1533e5a96cc6d72a065a43050eff4edcb7afd6a9a52c7b58'


def legacy_gateway_sn(raw):
    """已核对版本用fgets(buffer,17)读SN；拒绝会截断或歧义的文件。"""
    if (not isinstance(raw, bytes) or len(raw) != 17 or raw[-1:] not in (b'\0', b'\n')
            or not re.fullmatch(rb'[A-Za-z0-9_-]{16}', raw[:-1])):
        raise ValueError('unambiguous bounded legacy gateway SN required')
    return raw[:-1].decode('ascii')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def private_read(path):
    info = path.lstat()
    if path.is_symlink() or not path.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private owned regular input required')
    return path.read_bytes()


def text(data, *, empty=False):
    value, separator, padding = data.partition(b'\0')
    if not separator or any(padding) or (not value and not empty):
        raise ValueError('invalid fixed-width text')
    if any(byte < 32 or byte > 126 for byte in value):
        raise ValueError('invalid text encoding')
    return value.decode('ascii')


def decrypt(data, key):
    decoder = Cipher(algorithms.AES(key[:16]), modes.CBC(key[16:])).decryptor()
    return decoder.update(data) + decoder.finalize()


def hkdf(material, salt, info):
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(material)


def decode(config, bridge, ac, mac):
    if len(ac) != 48 or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', mac):
        raise ValueError('48-byte AC and original six-byte interface MAC required')
    if len(config) != 1588 or struct.unpack_from('<I', config)[0] != 1552 or config[36] != 3:
        raise ValueError('unsupported legacy root format')
    if not hmac.compare_digest(hashlib.sha256(config[36:]).digest(), config[4:36]):
        raise ValueError('legacy root checksum mismatch')
    material = ac[:16] + bytes.fromhex(mac.replace(':', ''))
    key = hmac.digest(material, config[44:60], 'sha256')
    login = decrypt(config[76:172], key)
    root_id, secret = text(login[:40]), text(login[40:80])
    if str(uuid.UUID(root_id)) != root_id or len(secret) != 32 or not any(login[80:]):
        raise ValueError('legacy root decryption rejected')
    uid_key = hkdf(material, config[1244:1260], b'hilink_hkdf_base_info')
    uid = decrypt(config[1260:1324], uid_key)
    auth_id = text(config[400:440])
    if not re.fullmatch(rb'[0-9a-f]{64}', uid) or not re.fullmatch(r'[0-9a-f]{32}', auth_id):
        raise ValueError('legacy account metadata rejected')
    # bridge v4 的校验范围沿用旧 SDK：salt + 除最后一块外的密文。
    # 最后一块必须是未使用的零记录；不能据此把该校验称为认证加密。
    if len(bridge) != 5192 or struct.unpack_from('<II', bridge) != (5136, 4):
        raise ValueError('unsupported legacy child format')
    if not hmac.compare_digest(hashlib.sha256(bridge[40:5176]).digest(), bridge[8:40]):
        raise ValueError('legacy child checksum mismatch')
    child_key = hmac.digest(material, bridge[40:56], 'sha256')
    children = decrypt(bridge[56:], child_key)
    count = struct.unpack_from('<I', children)[0]
    if not 1 <= count <= 64 or any(children[4:16]) or any(children[16 + count * 80:]) or any(children[-16:]):
        raise ValueError('invalid bounded legacy child records')
    records = []
    for offset in range(16, 16 + count * 80, 80):
        child_id, child_sn = text(children[offset:offset + 40]), text(children[offset + 40:offset + 80])
        if str(uuid.UUID(child_id)) != child_id or not re.fullmatch(r'HB[0-9a-f]{14}', child_sn):
            raise ValueError('legacy child identity rejected')
        records.append({'dev_id': child_id, 'sn': child_sn})
    if len({row['dev_id'] for row in records}) != count or len({row['sn'] for row in records}) != count:
        raise ValueError('duplicate legacy child identity')
    return dict(dev_id=root_id, secret=secret, psk=login[80:96],
                url=text(config[172:236]), backup_url='',
                uid_hash=uid.decode(), auth_id=auth_id, auth_code=decrypt(config[440:456], key),
                children=records, child_id=records[0]['dev_id'], child_sn=records[0]['sn'])


def fixed(value, width):
    raw = value.encode('ascii')
    if len(raw) >= width:
        raise ValueError('target field exceeds SDK width')
    return raw.ljust(width, b'\0')


def encode_store(payload, ac, udid, *, encrypted):
    salt = os.urandom(16)
    key = hkdf(ac, salt, udid.encode('ascii'))
    if encrypted:
        result = AESGCM(key[:16]).encrypt(key[16:], payload, None)
        body, tag = result[:-16], result[-16:].ljust(32, b'\0')
    else:
        body, tag = payload, hmac.digest(key, payload, 'sha256')
    return b'\1\0\0\0' + salt + tag + struct.pack('<I', len(payload)) + body


def build_store(identity, gateway, port):
    if not 1 <= port <= 65535:
        raise ValueError('explicit observed cloud port required')
    def endpoint(host):
        if not host:
            return ''
        if not re.fullmatch(r'[a-zA-Z0-9.-]+', host):
            raise ValueError('unsupported legacy cloud endpoint')
        return f'{host}:{port}'
    payload = (b'\1\0' + fixed(identity['dev_id'], 41) + fixed(identity['secret'], 41) + identity['psk']
               + fixed(endpoint(identity['url']), 65) + fixed(endpoint(identity['backup_url']), 65)
               + fixed(identity['uid_hash'], 65) + fixed(identity['auth_id'], 33)
               + identity['auth_code'] + b'\0' + bytes(167))
    assert len(payload) == 512
    udid = digest((gateway['manuName'] + gateway['model'] + gateway['sn']).encode())
    ac = bytes.fromhex(gateway['ac_key_hex'])
    return {'dev_data': encode_store(payload, ac, udid, encrypted=True),
            'conn_data': encode_store(b'\1\2' + bytes(126), ac, udid, encrypted=False)}
