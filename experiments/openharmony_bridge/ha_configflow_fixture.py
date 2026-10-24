"""为独立 HA 测试创建全新、不可配对的实验身份和虚拟实体映射，不复用绑定。"""
import argparse
import asyncio
import importlib
import hashlib
import json
import os
from pathlib import Path

from framework_loader import load_framework
import secrets
import shutil
import uuid


def prepare(config, source, binary_source=None, assets=None):
    assets = assets or source / "assets"
    os.umask(0o077)
    directory = config / ".storage/hwhomebridge/openharmony/lab"
    if directory.exists():
        raise ValueError("fixture must use a fresh isolated directory")
    directory.mkdir(mode=0o700, parents=True)
    (directory / "runtime").mkdir(mode=0o700)
    (directory / "profiles").mkdir(mode=0o700)
    binary = directory / "bridge_poc"
    shutil.copyfile(binary_source or source / "build-ailife-arm64/bridge_poc", binary)
    binary.chmod(0o700)
    shutil.copyfile(assets / "huawei-cbg-root-ca.pem", directory / "ca.pem")
    component = config / "custom_components/hwhomebridge/openharmony/config"
    instance = json.loads((component / "instances.example.json").read_text())
    instance.pop("ha", None)
    parent = "OHPOC-" + uuid.uuid4().hex[:24]
    instance["parent_sn"] = parent
    for device in instance["devices"]:
        device["id"] = "configflow-fixture-" + device["id"]
        device["roles"] = {role: entity.split(".")[0] + ".oh_flow_" + role for role, entity in device["roles"].items()}
    (directory / "instances.json").write_text(json.dumps(instance))
    gateway = {"sn": parent, "prodId": "ZG37", "model": "DL-01W", "devTypeId": "A17",
               "devTypeName": "Smart Bridge", "manuId": "002", "manuName": "DALEN",
               "devName": "OpenHarmony HA config flow TEST", "protType": 1,
               "ac_key_hex": secrets.token_hex(48), "pin": str(secrets.randbelow(90000000) + 10000000),
               "pin_expires_at": 1, "ca_file": "/config/.storage/hwhomebridge/openharmony/lab/ca.pem",
               "enable_virtual_child": True, "enable_ha_children": True}
    (directory / "gateway.local.json").write_text(json.dumps(gateway))
    settings = {"version": 1, "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "interface": "eth0", "port": 18091, "lan_port": 15684}
    (directory / "deployment.json").write_text(json.dumps(settings))
    framework = load_framework()
    transport = importlib.import_module(framework.__name__ + ".transport").ScopedTransport()
    router = asyncio.run(framework.OpenHarmonyRouter.create(transport, component, instance))
    catalog = router.registry.catalog(router.devices)
    (directory / "runtime/ha-children.local.json").write_text(json.dumps(catalog))
    for product in json.loads((component / "product_registry.json").read_text())["products"]:
        shutil.copyfile(assets / (product["pid"] + "-profile.json"), directory / "profiles" / (product["pid"] + ".json"))
    for path in directory.rglob("*"):
        if path.is_file() and path != binary:
            path.chmod(0o600)
    return {"fresh_independent_identity": True, "pairing_pin_expired": True,
            "virtual_entities": len(instance["devices"][0]["roles"]), "port": settings["port"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--assets", type=Path)
    args = parser.parse_args()
    print(json.dumps(prepare(args.config, args.source, args.binary, args.assets)))
