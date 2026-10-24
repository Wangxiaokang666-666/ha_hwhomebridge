"""公共角色绑定：调用方提供同一 HA 设备的实体清单，不按名称猜控制目标。"""
import re
import math
from types import MappingProxyType


class RoleBindings:
    def __init__(self, device_id, roles, owned_entities):
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("HA device ID required")
        if not isinstance(roles, dict) or not roles:
            raise ValueError("explicit role bindings required")
        owned = frozenset(owned_entities)
        checked = {}
        for role, entity in roles.items():
            if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", role):
                raise ValueError("invalid role name")
            if not isinstance(entity, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity):
                raise ValueError("invalid role entity")
            if entity not in owned:
                raise ValueError("role entity does not belong to the HA device inventory")
            if entity in checked.values():
                raise ValueError("roles must bind distinct entities")
            checked[role] = entity
        self.device_id = device_id
        self.roles = MappingProxyType(checked)

    def entity(self, role, domain=None):
        try:
            entity = self.roles[role]
        except (KeyError, TypeError):
            raise ValueError("unbound role") from None
        if domain is not None and entity.split(".")[0] != domain:
            raise ValueError("role domain does not match mapping")
        return entity


def integer(value, low, high):
    """有界整数转换；bool、非有限数和字符串都不是协议整数。"""
    if type(value) is int:
        if low <= value <= high:
            return value
    elif type(value) is float and math.isfinite(value) and value.is_integer() and low <= value <= high:
        return int(value)
    raise ValueError("value outside supported range")
