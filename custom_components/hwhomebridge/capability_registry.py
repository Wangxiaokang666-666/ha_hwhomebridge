"""显式加载角色和组合能力；不改变旧产品目录的默认加载入口。"""
import asyncio
import json
from pathlib import Path

from .product_registry import ProductRegistry, VALID_ACTION_DOMAIN_MAP
from .role_binding import integer
from .composition import validate_compositions


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 262144:
        raise ValueError("configuration too large")
    def reject_constant(value):
        raise ValueError("non-finite number")
    return json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant)


class CapabilityRegistry(ProductRegistry):
    """复用三层合并，额外核对可执行动作及 profile 的读写约束。"""

    ACTIONS = frozenset({"turn_on_off", "set_brightness", "set_color_temp", "set_speed",
                         "set_direction", "set_fan_mode", "set_effect", "native_timer",
                         "countdown", "timer_array", "aggregate"})

    @staticmethod
    def _check_mapping(mapping):
        if mapping.action not in CapabilityRegistry.ACTIONS:
            raise ValueError("action not implemented by capability router")
        if mapping.domain not in VALID_ACTION_DOMAIN_MAP[mapping.action]:
            raise ValueError("unsupported capability action/domain")
        if mapping.action == "turn_on_off" and mapping.domain not in ("fan", "light", "switch"):
            raise ValueError("unsupported capability power domain")
        if mapping.value_mapping and mapping.value_mapping.mapping_type != "enum_to_number":
            raise ValueError("unsupported capability value mapping")
        if mapping.action == "set_speed" and mapping.value_mapping is None:
            raise ValueError("capability fan requires an explicit speed table")
        return mapping

    async def load_configuration(self, directory):
        directory = Path(directory)
        framework = await asyncio.to_thread(read_json, directory / "product_registry.json")
        self.definitions = {}
        for item in framework["products"]:
            pid = item["pid"]
            if pid in self.definitions:
                raise ValueError("duplicate product")
            self.definitions[pid] = item
        default_pids = set()
        vendor_count = 0
        for path in sorted((directory / "adapters").rglob("*.json")):
            data = await asyncio.to_thread(read_json, path)
            if data["pid"] not in self.definitions or set(data) - {"pid", "services", "match_rules", "composition"}:
                raise ValueError("invalid adapter")
            if set(data.get("services", {})) - set(self.definitions[data["pid"]]["services"]):
                raise ValueError("adapter cannot invent Huawei services")
            if path.parent.name == "default":
                if data["pid"] in default_pids or data.get("match_rules"):
                    raise ValueError("invalid default adapter")
                default_pids.add(data["pid"])
            else:
                vendor_count += 1
        if not await self.load(str(directory / "product_registry.json"), str(directory / "adapters")):
            raise ValueError("registry load failed")
        if self.get_product_count() != len(self.definitions) or self.get_default_count() != len(default_pids) or self.get_adapter_count() != vendor_count:
            raise ValueError("incomplete registry")
        self.bundles = (await asyncio.to_thread(read_json, directory / "compositions.json"))["bundles"]
        validate_compositions(self)

    def validate_value(self, pid, sid, field_name, value, write=False):
        schema = self.definitions[pid]["services"][sid]["characteristics"].get(field_name)
        if not schema or write and "W" not in schema["method"]:
            raise ValueError("unsupported characteristic")
        kind = schema["type"]
        if kind in ("bool", "enum", "int"):
            value = integer(value, schema.get("min", 0), schema.get("max", 2147483647))
            if "values" in schema and value not in schema["values"]:
                raise ValueError("unsupported enum")
        elif kind == "string":
            if not isinstance(value, str) or len(value) > schema.get("max_length", 256):
                raise ValueError("invalid text")
        elif kind == "array[object]":
            if not isinstance(value, list) or len(value) > schema.get("max_items", 32) or any(not isinstance(item, dict) for item in value):
                raise ValueError("invalid object array")
        else:
            raise ValueError("unsupported profile type")
        return value
