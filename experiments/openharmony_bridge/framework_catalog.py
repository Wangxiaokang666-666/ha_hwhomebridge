"""框架与已配对独立 C runtime 之间的身份、profile 和目录校验。"""
import hashlib
import importlib
from pathlib import Path
import re

from framework_loader import load_framework

configuration = importlib.import_module(load_framework().__name__ + ".configuration")
ROOT = Path(__file__).resolve().parent


def verify_catalog(router, runtime, gateway, assets=None):
    assets = assets or ROOT / "assets"
    if runtime.parent != ROOT or not runtime.name.startswith("runtime-") or not runtime.is_dir():
        raise ValueError("existing independent runtime required")
    if gateway.parent != ROOT or not gateway.name.endswith(".local.json"):
        raise ValueError("independent gateway configuration required")
    root = configuration.read_json(gateway)
    if root.get("prodId") != "ZG37" or root.get("enable_virtual_child") is not True or root.get("enable_ha_children") is not True:
        raise ValueError("paired experimental gateway with phase-one fixture required")
    old = {item["key"]: item for item in configuration.read_json(runtime / "ha-children.local.json")}
    catalog = router.registry.catalog(router.devices)
    for item in catalog:
        device = router.devices[item["key"]]
        if configuration.child_sn(root["sn"], item["key"]) != device.sn:
            raise ValueError("parent identity mismatch")
        metadata = router.registry.definitions[item["pid"]]["cloud"]
        profile_file = assets / (item["pid"] + "-profile.json")
        if hashlib.sha256(profile_file.read_bytes()).hexdigest() != metadata["profile_sha256"]:
            raise ValueError("profile hash mismatch")
        profile = configuration.read_json(profile_file)
        if profile["prodId"] != item["pid"] or profile["protocolType"] != "WiFi" or item["model"] != profile["deviceModel"] or item["manu"] != profile["manufacturerId"] or item["devType"] != profile["deviceTypeId"] or item["protType"] != 1:
            raise ValueError("profile identity mismatch")
        profile_services = {s["serviceId"]: s["serviceType"] for s in profile["services"]}
        for sid, st in item["services"].items():
            if sid not in profile_services or st != profile_services[sid] and not (re.fullmatch(r"switch[1-4]", sid) and st == sid):
                raise ValueError("cloud service not backed by profile")
            source_service = next(s for s in profile["services"] if s["serviceId"] == sid)
            expected = {}
            for characteristic in source_service["characteristics"]:
                schema = {"type": characteristic["characteristicType"], "method": characteristic["method"]}
                for source, target in (("min", "min"), ("max", "max"), ("maxLength", "max_length")):
                    if source in characteristic:
                        schema[target] = int(characteristic[source])
                if "enumList" in characteristic:
                    schema["values"] = [int(x["enumVal"]) for x in characteristic["enumList"]]
                expected[characteristic["characteristicName"]] = schema
            definition = router.registry.definitions[item["pid"]]["services"][sid]
            if definition["characteristics"] != expected or definition.get("char_name") is not None and definition["char_name"] not in expected:
                raise ValueError("framework characteristics differ from pinned profile")
        identity_path = runtime / ("ha-child-" + item["key"] + ".json")
        if identity_path.exists():
            identity = configuration.read_json(identity_path)
            if identity.get("sn") != device.sn or identity.get("parent_sn") != root["sn"]:
                raise ValueError("saved child identity mismatch")
            if identity.get("devId"):
                previous = old.get(item["key"])
                if not previous or any(previous.get(field) != item[field] for field in ("pid", "devType", "manu", "model", "protType")):
                    raise ValueError("refusing to change a paired product identity")
    if set(old) - {item["key"] for item in catalog}:
        raise ValueError("refusing to silently drop registered experiment cards")
    return catalog
