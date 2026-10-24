"""实验身份及云目录适配；角色、组合与协议约束由公共框架处理。"""
import hashlib
import re

from ..capability_registry import CapabilityRegistry, read_json, unique_object
from ..role_binding import integer


def child_sn(parent, key):
    if not isinstance(parent, str) or not parent.startswith("OHPOC-") or len(parent) >= 40:
        raise ValueError("independent OHPOC parent required")
    if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9-]{1,31}", key):
        raise ValueError("invalid child key")
    return "OHPOC-HA-" + hashlib.sha256(f"{parent}#ha-{key}-v1".encode()).hexdigest()[:24]


def managed_child_sn(parent, key):
    """同一已接管网关的补充卡身份；保留已有实验卡的算法。"""
    if isinstance(parent, str) and parent.startswith('OHPOC-'):
        return child_sn(parent, key)
    if not isinstance(parent, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,16}', parent):
        raise ValueError('verified legacy parent SN required')
    if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9-]{1,31}', key):
        raise ValueError('invalid child key')
    return 'HWHB-HA-' + hashlib.sha256(f'{parent}#ha-{key}-v1'.encode()).hexdigest()[:24]


class OpenHarmonyRegistry(CapabilityRegistry):
    def catalog(self, devices):
        result = []
        for key, device in devices.items():
            definition = self.definitions[device.product.pid]
            metadata = definition["cloud"]
            result.append({"key": key, "pid": device.product.pid,
                           **{field: metadata[field] for field in ("devType", "manu", "model", "protType")},
                           "services": {sid: definition["services"][sid].get("cloud_service_type", svc.service_type)
                                        for sid, svc in device.product.services.items()}})
        return result
