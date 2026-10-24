"""取得固定哈希的公开 profile/CA；不读取任何 HA 或配对资料。"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
CA_SHA256 = '0a2524f990393c4fbd2a6b45a0071d3c34e605413b8eddb67108317d0472ebc9'


def prepare(destination):
    destination.mkdir(parents=True, exist_ok=True)
    config = ROOT.parents[1] / 'custom_components/hwhomebridge/openharmony/config/product_registry.json'
    for product in json.loads(config.read_text())['products']:
        path = destination / (product['pid'] + '-profile.json')
        if not path.exists():
            raw = subprocess.check_output(['curl', '-fsSL', '--max-time', '30',
                                           '--max-filesize', '262144', product['cloud']['profile_url']])
            # 旧验收固定了原始/两空格/紧凑三种下载保存格式，语义不变也可能换行不同。
            obj = json.loads(raw)
            candidates = (raw, json.dumps(obj, ensure_ascii=False, indent=2).encode(),
                          json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode())
            raw = next((value for value in candidates
                        if hashlib.sha256(value).hexdigest() == product['cloud']['profile_sha256']), None)
            if raw is None:
                raise ValueError('public profile checksum mismatch: ' + product['pid'])
            path.write_bytes(raw)
        if hashlib.sha256(path.read_bytes()).hexdigest() != product['cloud']['profile_sha256']:
            raise ValueError('cached profile checksum mismatch: ' + product['pid'])
    ca = destination / 'huawei-cbg-root-ca.pem'
    if not ca.exists():
        subprocess.run([sys.executable, str(ROOT / 'fetch_huawei_ca.py'), str(ca)], check=True)
    if hashlib.sha256(ca.read_bytes()).hexdigest() != CA_SHA256:
        raise ValueError('public CA checksum mismatch')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('destination', type=Path)
    print(prepare(parser.parse_args().destination))
