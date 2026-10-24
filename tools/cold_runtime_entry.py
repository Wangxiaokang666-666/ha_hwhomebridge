"""随免安装包运行；只提供离线自检、冷备导出、校验和新目录还原。"""
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import sys


def validate(root):
    manifest = json.loads((root/'runtime-manifest.json').read_text())
    if manifest['version'] != 1 or manifest['platform'] != 'linux-x86_64':
        raise ValueError('unsupported portable runtime')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if not p.is_dir() or p.is_symlink()}
    if actual != set(manifest['files']) | {'runtime-manifest.json'}:
        raise ValueError('runtime inventory mismatch')
    for name, info in manifest['files'].items():
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('unsafe runtime path')
        path = root/name
        if info['type'] == 'link':
            if not path.is_symlink() or os.readlink(path) != info['target'] or not path.resolve().is_relative_to(root):
                raise ValueError('runtime link mismatch')
        elif not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
            raise ValueError('runtime file mismatch')
    if sys.platform != 'linux' or platform.machine() != 'x86_64':
        raise ValueError('Linux x86_64 runtime required')
    return manifest


def main():
    root = Path(__file__).resolve().parent
    validate(root)
    sys.path[:0] = [str(root/'third_party'), str(root/'src/tools')]
    if sys.argv[1:] == ['self-test']:
        import ha_entity_preflight
        import cryptography
        codec = ha_entity_preflight.core_module('identity_store')
        assert len(codec.hkdf(bytes(48),bytes(16),b'portable-cold-test')) == 32
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        cipher = Cipher(algorithms.AES(bytes(16)),modes.CBC(bytes(16)))
        encrypted = cipher.encryptor().update(bytes(16))
        assert cipher.decryptor().update(encrypted) == bytes(16)
        print(json.dumps({'passed':True,'python':platform.python_version(),'cryptography':cryptography.__version__,
                          'bundled_runtime':True,'system_Python_required':False,'SDK_started':False}))
        return
    if not sys.argv[1:] or sys.argv[1] not in ('export','verify','restore','--help','-h'):
        raise ValueError('only self-test/export/verify/restore supported')
    sys.argv[0] = str(root/'src/tools/ha_cold_export.py')
    runpy.run_path(sys.argv[0],run_name='__main__')


if __name__ == '__main__':
    try:main()
    except Exception:
        print(json.dumps({'passed':False,'error':'portable cold runtime validation failed; no HA lifecycle change requested'}))
        raise SystemExit(1) from None
