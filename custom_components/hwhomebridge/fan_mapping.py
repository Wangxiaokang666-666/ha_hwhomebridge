"""风扇的显式双向映射，供公共路由和实验传输共用；不创建网关或定时器。"""
import math


FAN_ACTIONS = frozenset({"set_speed", "set_direction", "set_fan_mode"})


def is_mapped_fan(mapping):
    """旧 set_speed 无映射时继续使用原行为，新能力必须显式配置。"""
    return mapping.action in FAN_ACTIONS and (
        mapping.value_mapping is not None or mapping.action != "set_speed"
    )


def _integer(value):
    if type(value) is int:
        return value
    if type(value) is not float or not math.isfinite(value) or int(value) != value:
        raise ValueError("fan enum/percentage must be a finite integer")
    return int(value)


def validate_fan_mapping(mapping):
    """拒绝含糊或不可逆的配置，不猜档位、不使用最近值。"""
    if not is_mapped_fan(mapping):
        return
    if mapping.domain != "fan" or not isinstance(mapping.options, dict):
        raise ValueError("mapped fan requires fan domain and options object")
    vm = mapping.value_mapping
    if vm is None or vm.mapping_type != "enum_to_number" or not isinstance(vm.mapping_dict, dict) or not vm.mapping_dict:
        raise ValueError("mapped fan requires an explicit enum_to_number table")
    service = mapping.options.get("ha_service", "set_percentage")
    if mapping.action == "set_direction":
        expected_attr = "direction"
    elif mapping.action == "set_fan_mode" and service == "set_preset_mode":
        expected_attr = "preset_mode"
    else:
        if service != "set_percentage":
            raise ValueError("unsupported fan service")
        expected_attr = "percentage"
    if mapping.value_attr != expected_attr:
        raise ValueError("fan readback attribute does not match action")
    values = []
    for enum, value in vm.mapping_dict.items():
        if not isinstance(enum, str) or not enum.isascii() or not enum.isdecimal() or str(int(enum)) != enum:
            raise ValueError("fan enum keys must be canonical nonnegative integers")
        if expected_attr == "direction":
            if value not in ("forward", "reverse"):
                raise ValueError("invalid direction mapping")
        elif expected_attr == "preset_mode":
            if not isinstance(value, str) or not value:
                raise ValueError("invalid preset mapping")
        elif not 0 <= _integer(value) <= 100:
            raise ValueError("fan percentage outside 0..100")
        if value in values:
            raise ValueError("fan mapping must have unambiguous readback")
        values.append(value)
    if "unmapped" in mapping.options:
        if _integer(mapping.options["unmapped"]) < 0:
            raise ValueError("unmapped fan enum must be nonnegative")
    if mapping.options.get('binary_choice'):
        if (expected_attr != 'percentage' or set(vm.mapping_dict) != {'0', '1'}
                or vm.mapping_dict['0'] != 0 or vm.mapping_dict['1'] <= 0
                or mapping.options.get('unmapped') != 0):
            raise ValueError('binary fan choice requires stop and one positive percentage')


def fan_command(mapping, value):
    """将华为枚举转换为一个白名单内的 HA 服务和参数。"""
    validate_fan_mapping(mapping)
    if not is_mapped_fan(mapping):
        raise ValueError("explicit fan mapping required")
    key = str(_integer(value))
    table = mapping.value_mapping.mapping_dict
    if key not in table:
        raise ValueError("unsupported fan enum")
    parameter = table[key]
    if mapping.action == "set_direction":
        return "set_direction", {"direction": parameter}
    if mapping.action == "set_fan_mode" and mapping.options.get("ha_service") == "set_preset_mode":
        return "set_preset_mode", {"preset_mode": parameter}
    return "set_percentage", {"percentage": _integer(parameter)}


def fan_state(mapping, state, attributes):
    """报告 HA 实际值；未知/缺失状态不伪造枚举或回显最后一次命令。"""
    validate_fan_mapping(mapping)
    if not is_mapped_fan(mapping):
        raise ValueError("explicit fan mapping required")
    if state not in ("on", "off"):
        return None
    if mapping.options.get('binary_choice') and state == 'off':
        return 0
    value = (attributes or {}).get(mapping.value_attr)
    if value is None or isinstance(value, bool):
        return None
    if mapping.value_attr == "percentage":
        try:
            value = _integer(value)
        except ValueError:
            return None
        if not 0 <= value <= 100:
            return None
    elif not isinstance(value, str):
        return None
    for enum, candidate in mapping.value_mapping.mapping_dict.items():
        if candidate == value:
            return int(enum)
    return mapping.options.get("unmapped")
