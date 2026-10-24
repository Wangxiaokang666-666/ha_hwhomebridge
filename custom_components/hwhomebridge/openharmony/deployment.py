"""HA 原生后端的独立目录校验；不读取 REST token，不启动任何进程。"""
import asyncio
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import stat as stat_module

from .configuration import child_sn, integer, read_json
from .router import OpenHarmonyRouter
from .transport import ScopedTransport


@dataclass
class Deployment:
    directory: Path
    binary: Path
    gateway: Path
    runtime: Path
    interface: str
    port: int
    lan_port: int
    router: OpenHarmonyRouter
    gateway_sn: str
    write_policy: str


def private_path(path, directory=False):
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("private owned OpenHarmony files required")
    if not (stat_module.S_ISDIR(stat.st_mode) if directory else stat_module.S_ISREG(stat.st_mode)):
        raise ValueError("unexpected OpenHarmony file type")


def load_deployment(config_directory, relative_directory):
    if not isinstance(relative_directory, str) or Path(relative_directory).is_absolute():
        raise ValueError("relative HA configuration directory required")
    config = Path(config_directory).resolve()
    base = config / ".storage/hwhomebridge/openharmony"
    directory = config / relative_directory
    if ".." in directory.parts or directory.resolve() != directory.absolute() or not directory.is_relative_to(base) or directory == base:
        raise ValueError("isolated OpenHarmony directory required")
    private_path(directory, True)
    for name in ("deployment.json", "gateway.local.json", "instances.json", "bridge_poc"):
        private_path(directory / name)
    runtime = directory / "runtime"
    private_path(runtime, True)
    private_path(runtime / "ha-children.local.json")
    settings = read_json(directory / "deployment.json")
    if settings.get("version") != 1:
        raise ValueError("unsupported deployment version")
    port = integer(settings["port"], 1024, 65535)
    lan_port = integer(settings["lan_port"], 1024, 65535)
    interface = settings["interface"]
    if not isinstance(interface, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,15}", interface):
        raise ValueError("invalid network interface")
    binary = directory / "bridge_poc"
    if not os.access(binary, os.X_OK) or hashlib.sha256(binary.read_bytes()).hexdigest() != settings["binary_sha256"]:
        raise ValueError("OpenHarmony executable checksum mismatch")
    gateway = directory / "gateway.local.json"
    root = read_json(gateway)
    if root.get("prodId") != "ZG37" or root.get("enable_virtual_child") is not True or root.get("enable_ha_children") is not True:
        raise ValueError("isolated ZG37 gateway and preserved fixture required")
    ca = Path(root["ca_file"])
    if not ca.is_relative_to(directory) or ca.resolve() != ca:
        raise ValueError("local CA certificate required")
    private_path(ca)
    instance = read_json(directory / "instances.json")
    if "ha" in instance or instance["parent_sn"] != root["sn"]:
        raise ValueError("native HA instance must not contain REST credentials")
    config_path = Path(__file__).parent / "config"
    router = asyncio.run(OpenHarmonyRouter.create(ScopedTransport(), config_path, instance))
    policy = settings.get("write_policy", "fan_only")
    router.set_write_policy(policy)
    expected = router.registry.catalog(router.devices)
    actual = read_json(runtime / "ha-children.local.json")
    if {row["key"]: row for row in actual} != {row["key"]: row for row in expected}:
        raise ValueError("C catalog differs from framework")
    for key, device in router.devices.items():
        definition = router.registry.definitions[device.product.pid]
        profile_path = directory / "profiles" / (device.product.pid + ".json")
        if hashlib.sha256(profile_path.read_bytes()).hexdigest() != definition["cloud"]["profile_sha256"]:
            raise ValueError("pinned profile checksum mismatch")
        profile = read_json(profile_path)
        metadata = definition["cloud"]
        if (profile["prodId"] != device.product.pid or profile["protocolType"] != "WiFi" or
            metadata["model"] != profile["deviceModel"] or metadata["manu"] != profile["manufacturerId"] or
            metadata["devType"] != profile["deviceTypeId"] or metadata["protType"] != 1):
            raise ValueError("profile product identity mismatch")
        profile_services = {svc["serviceId"]: svc for svc in profile["services"]}
        for sid, service in definition["services"].items():
            source = profile_services[sid]
            expected_schema = {}
            for characteristic in source["characteristics"]:
                schema = {"type": characteristic["characteristicType"], "method": characteristic["method"]}
                for original, target in (("min", "min"), ("max", "max"), ("maxLength", "max_length")):
                    if original in characteristic:
                        schema[target] = int(characteristic[original])
                if "enumList" in characteristic:
                    schema["values"] = [int(row["enumVal"]) for row in characteristic["enumList"]]
                expected_schema[characteristic["characteristicName"]] = schema
            if service["characteristics"] != expected_schema or service["service_type"] != source["serviceType"]:
                raise ValueError("framework service differs from pinned profile")
        identity_path = runtime / ("ha-child-" + key + ".json")
        if identity_path.exists():
            private_path(identity_path)
            identity = read_json(identity_path)
            if identity.get("sn") != child_sn(root["sn"], key) or identity.get("parent_sn") != root["sn"]:
                raise ValueError("saved child identity mismatch")
    return Deployment(directory, binary, gateway, runtime, interface, port, lan_port, router, root["sn"], policy)
