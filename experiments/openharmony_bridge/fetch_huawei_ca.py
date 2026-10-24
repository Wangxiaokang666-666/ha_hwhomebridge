#!/usr/bin/env python3
"""从华为公开 PKI 门户取得根证书，校验固定指纹及实际 IoT TLS 主机名。"""
import argparse
import hashlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
from urllib.parse import urlparse

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
args = parser.parse_args()
portal = "https://pki.consumer.huawei.com/api/crl-portal/v1/pki/root-cas"
def download(url):
    return subprocess.check_output(["curl", "-fsSL", "--max-time", "20", url])
index = json.loads(download(portal))
url = next(item["url"] for item in index["files"] if item["name"] == "RootCa")
address = urlparse(url)
if address.scheme != "https" or address.hostname != "h5hosting-drcn.dbankcdn.cn":
    raise SystemExit("证书下载来源变化，需要重新核对官方来源")
certificate = download(url)
pem = subprocess.check_output(["openssl", "x509", "-inform",
    "PEM" if certificate.startswith(b"-----BEGIN") else "DER", "-outform", "PEM"], input=certificate)
digest = hashlib.sha256(pem).hexdigest()
if digest != "0a2524f990393c4fbd2a6b45a0071d3c34e605413b8eddb67108317d0472ebc9":
    raise SystemExit("官方证书指纹变化，需要核对后更新实验锁定值")
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_bytes(pem)
host = "wisedevice-hilink-drcn.things.dbankcloud.cn"
with socket.create_connection((host, 5683), timeout=8) as raw:
    with ssl.create_default_context(cafile=str(args.output)).wrap_socket(raw, server_hostname=host) as tls:
        print(json.dumps({"official_source": url, "sha256_pem": digest,
                          "cloud_certificate_and_hostname_verified": True, "tls": tls.version()}))
