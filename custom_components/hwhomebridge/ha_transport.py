"""公共本地HA传输；无REST token、HTTP监听、SSH或网关进程。"""
import asyncio
import re


class ScopedTransport:
    ACTIONS = {"fan": {"turn_on", "turn_off", "set_percentage", "set_direction", "set_preset_mode"},
               "light": {"turn_on", "turn_off"}, "switch": {"turn_on", "turn_off"},
               "number": {"set_value"}}
    PARAMETERS = {("fan", "turn_on"): {"percentage"}, ("fan", "turn_off"): set(),
                  ("fan", "set_percentage"): {"percentage"}, ("fan", "set_direction"): {"direction"},
                  ("fan", "set_preset_mode"): {"preset_mode"},
                  ("light", "turn_on"): {"brightness", "color_temp_kelvin", "effect"},
                  ("light", "turn_off"): set(), ("switch", "turn_on"): set(),
                  ("switch", "turn_off"): set(), ("number", "set_value"): {"value"}}

    def bind(self, entities):
        if not entities or any(not isinstance(x, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", x) for x in entities):
            raise ValueError("invalid entity allowlist")
        self.allowed = frozenset(entities)

    def validate_call(self, domain, action, entity, values=None):
        if entity not in self.allowed or entity.split(".")[0] != domain or action not in self.ACTIONS.get(domain, set()):
            raise ValueError("service call outside configured scope")
        if set(values or {}) - self.PARAMETERS[domain, action]:
            raise ValueError("service parameters outside configured scope")

    def filter_states(self, states):
        result = {s["entity_id"]: s for s in states if s["entity_id"] in self.allowed}
        # 缺失实体由各卡片在读取自身依赖时判断，不能拖累其他卡片。
        return result


class HomeAssistantTransport(ScopedTransport):
    """供公共能力工作线程调用；直接使用当前 HA 状态与本地服务。"""

    def __init__(self, hass, writable=None):
        self.writable = None if writable is None else frozenset(writable)
        self.hass = hass
        self.allowed = frozenset()

    def run_on_loop(self, factory):
        if not self.hass.loop.is_running():
            raise RuntimeError("HA event loop is not running")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is self.hass.loop:
            raise RuntimeError("Capability synchronous router must run in a worker thread")
        future = asyncio.run_coroutine_threadsafe(factory(), self.hass.loop)
        try:
            return future.result(timeout=5)
        except BaseException:
            future.cancel()
            raise

    def states(self):
        async def collect():
            return self.filter_states([state.as_dict() for state in self.hass.states.async_all()])
        return self.run_on_loop(collect)

    def call(self, domain, action, entity, **values):
        self.validate_call(domain, action, entity, values)
        if self.writable is not None and entity not in self.writable:
            raise ValueError("Capability transport is read-only for this entity")
        async def invoke():
            await self.hass.services.async_call(domain, action, {"entity_id": entity, **values}, blocking=True)
        return self.run_on_loop(invoke)


def async_owned_entity_inventory(hass, device_ids):
    """在HA主循环上从实体注册表取归属；禁用实体不进入控制清单。"""
    from homeassistant.helpers import entity_registry
    registry = entity_registry.async_get(hass)
    return {device_id: {entry.entity_id for entry in entity_registry.async_entries_for_device(
        registry, device_id, include_disabled_entities=False)} for device_id in device_ids}
