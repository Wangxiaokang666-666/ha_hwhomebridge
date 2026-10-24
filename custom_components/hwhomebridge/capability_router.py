"""公共角色与组合路由；只使用现有父网关和调用方提供的本地传输。"""
from __future__ import annotations

import json
import math
import time
import asyncio

from .availability import require_state
from .product_matcher import DeviceInfo, ProductMatcher
from .service_router import ServiceRouter
from .fan_mapping import fan_command, fan_state, is_mapped_fan
from .capability_registry import CapabilityRegistry
from .role_binding import RoleBindings
from .composition import plan_composition, publish_compositions, service_dependencies
from .native_timer import NativeTimerController, NativeTimerProtocol, timers_for_mapping


class CapabilityRouter(ServiceRouter):
    registry_class = CapabilityRegistry

    def __init__(self, transport):
        super().__init__()
        self.registry = self.registry_class()
        self.matcher = ProductMatcher(self.registry)
        self.transport = transport
        self.devices = {}
        self.roles = {}
        self.bindings = {}
        self.dependencies = {}
        self.preserved_cards = {}
        self.timer_protocol = NativeTimerProtocol()
        self.forms = self.timer_protocol.forms
        self.write_entities = None
        self.settle_seconds = .8
        self.fan_settle_seconds = 2
        self.confirm_timeout = 6

    @classmethod
    async def create(cls, transport, configuration, instances, *, owned_entities,
                     supported_pids, sn_factory=None, sn_manager=None):
        self = cls(transport)
        if sn_manager is not None:
            self.sn_manager = sn_manager
        await self.registry.load_configuration(configuration)
        if instances.get("version") != 1 or not isinstance(instances.get("devices"), list):
            raise ValueError("invalid instance configuration")
        parent = instances["parent_sn"]
        if getattr(self.sn_manager, "capability_parent_identity", parent) != parent:
            raise ValueError("one router cannot attach to a second parent gateway")
        self.parent_identity = parent
        for source in instances["devices"]:
            if not isinstance(source.get("id"), str) or not source["id"]:
                raise ValueError("HA device registry ID required")
            if source["id"] not in owned_entities:
                raise ValueError("owned entity inventory required for each HA device")
            roles = source["roles"]
            RoleBindings(source["id"], roles, owned_entities[source["id"]])
            info = DeviceInfo(device_id=source["id"], name=source.get("name", ""),
                              model=source.get("model", ""), manufacturer=source.get("manufacturer", ""),
                              entity_domains=sorted({entity.split(".")[0] for entity in roles.values()}))
            product, _ = self.matcher.match_with_detail(info, include_compositions=True)
            if not product:
                raise ValueError("device has no configured composition")
            if source.get('layout', 'original') != 'original':
                from .card_layouts import apply_layout
                product = apply_layout(self.registry, product, source['layout'])
            planned, preserved, bindings = plan_composition(
                self.registry, self.sn_manager, product, source, parent,
                owned_entities[source["id"]], supported_pids, sn_factory)
            if (set(planned) | set(preserved)) & (set(self.devices) | set(self.preserved_cards)):
                raise ValueError("duplicate card key")
            for device in planned.values():
                self.bindings[device.sn] = bindings
                self.roles[device.sn] = bindings.roles
                self.dependencies[device.sn] = service_dependencies(device, bindings)
            self.devices.update(planned)
            self.preserved_cards.update(preserved)
        allowed = {entity for roles in self.roles.values() for entity in roles.values()}
        if allowed:
            self.transport.bind(allowed)
        publish_compositions(self.sn_manager, self.devices)
        self.sn_manager.capability_parent_identity = parent
        return self

    def set_write_entities(self, entities):
        allowed = {entity for roles in self.roles.values() for entity in roles.values()}
        if not set(entities) <= allowed:
            raise ValueError("write policy contains unbound entities")
        self.write_entities = frozenset(entities)

    def authorize_entities(self, entities):
        if self.write_entities is not None and not set(entities).issubset(self.write_entities):
            raise ValueError("write policy rejects this entity")

    def entity(self, sn, role):
        try:
            return self.bindings[sn].entity(role)
        except (KeyError, TypeError):
            raise ValueError("unbound role") from None

    def timer_controller(self, sn):
        return NativeTimerController(self.bindings[sn], self.transport, self.authorize_entities,
                                     lambda predicate: self.wait(predicate, self.settle_seconds))

    @staticmethod
    def active(state):
        if state["state"] not in ("on", "off"):
            raise ValueError("HA entity unavailable or not boolean")
        return int(state["state"] == "on")

    def state(self, states, sn, role):
        return require_state(states.get(self.entity(sn, role)))

    def snapshot(self, key, states=None):
        if key not in self.devices:
            raise ValueError("unknown card")
        device = self.devices[key]
        states = self.transport.states() if states is None else states
        result = {}
        for sid, service in device.product.services.items():
            value = self.read_service(device, sid, service, states)
            if value is not None:
                result[sid] = value
        for sid, service in device.product.services.items():
            mapping = service.ha_mapping
            if mapping.action == "aggregate":
                field = service.char_name
                result[sid] = {field: int(any(result[x].get(mapping.options.get("field", field), 0) for x in mapping.options["members"]))}
        return result

    def read_service(self, device, sid, service, states):
        mapping, sn = service.ha_mapping, device.sn
        options, action, field = mapping.options, mapping.action, service.char_name
        if action == "aggregate":
            return None
        if timers_for_mapping(mapping):
            result = self.timer_protocol.read(self.timer_controller(sn), sn, sid, mapping, field, states)
            for characteristic, value in result.items():
                self.registry.validate_value(device.product.pid, sid, characteristic, value)
            return result
        state = self.state(states, sn, mapping.role)
        if action == "turn_on_off":
            value = self.active(state)
        elif action == "set_effect":
            value = int(bool(self.active(state)) and state["attributes"].get(mapping.value_attr) == options["effect"])
        elif is_mapped_fan(mapping):
            value = fan_state(mapping, state["state"], state["attributes"])
            if value is None:
                return None
        else:
            attribute = mapping.value_attr or ("color_temp_kelvin" if action == "set_color_temp" else None)
            value = state["state"] if attribute == "state" else state["attributes"].get(attribute)
            if value is None:
                return None
            if action == "set_brightness":
                value = round(float(value) * (mapping.brightness_range or 100) / options.get("ha_maximum", 255))
            elif mapping.value_mapping:
                reverse = {v: int(k) for k, v in mapping.value_mapping.mapping_dict.items()}
                value = reverse.get(value, options.get("unmapped"))
                if value is None:
                    return None
        value = self.registry.validate_value(device.product.pid, sid, field, value)
        result = {field: value, **options.get("constants", {})}
        if "label" in options:
            result[options.get("label_field", "name")] = options["label"]
        for characteristic, item in result.items():
            self.registry.validate_value(device.product.pid, sid, characteristic, item)
        return result

    def wait(self, predicate, seconds):
        deadline, confirmed = time.monotonic() + self.confirm_timeout, None
        while True:
            if predicate():
                if confirmed is None:
                    confirmed = time.monotonic()
                if time.monotonic() - confirmed >= seconds:
                    return
            else:
                confirmed = None
            if time.monotonic() >= deadline:
                raise TimeoutError("HA did not confirm a stable state")
            time.sleep(.25)


    def apply_scalar(self, entity, mapping, payload):
        self.authorize_entities([entity])
        action, options, domain = mapping.action, mapping.options, mapping.domain
        value = next(iter(payload.values()))
        if action == "turn_on_off":
            values = {}
            power = options.get("power_on")
            if value and power:
                current = self.transport.states()[entity]["attributes"].get(power["attribute"])
                if current is None:
                    raise ValueError("power-on attribute unavailable")
                if current <= power["maximum"]:
                    values = power["values"]
            self.transport.call(domain, "turn_on" if value else "turn_off", entity, **values)
        elif action == "set_brightness":
            self.transport.call(domain, "turn_on", entity, brightness=math.ceil(value * options.get("ha_maximum", 255) / (mapping.brightness_range or 100)))
        elif action == "set_color_temp":
            self.transport.call(domain, "turn_on", entity, color_temp_kelvin=value)
        elif action in ("set_speed", "set_fan_mode", "set_direction"):
            if options.get('binary_choice') and not value:
                current = self.transport.states()[entity]
                if fan_state(mapping, current['state'], current['attributes']) != 1:
                    return
            service, values = fan_command(mapping, value)
            self.transport.call(domain, service, entity, **values)
        elif action == "set_effect":
            if value:
                self.transport.call(domain, "turn_on", entity, effect=options["effect"])
            elif self.active(self.transport.states()[entity]) and self.transport.states()[entity]["attributes"].get(mapping.value_attr) == options["effect"]:
                self.transport.call(domain, "turn_off", entity)
        else:
            raise ValueError("unsupported action")

    def write(self, key, sid, data):
        if key not in self.devices or not isinstance(data, dict) or not data:
            raise ValueError("invalid command")
        device = self.devices[key]
        if sid not in device.product.services:
            raise ValueError("unsupported service")
        service = device.product.services[sid]
        mapping, sn, field = service.ha_mapping, device.sn, service.char_name
        action, options = mapping.action, mapping.options
        if action == "aggregate":
            raise ValueError("aggregate is read-only")
        for name, value in data.items():
            # 标准 timer 数组协议会回带只读 num；只能等于本次数组的长度。
            count_echo = action == "timer_array" and name == "num"
            self.registry.validate_value(device.product.pid, sid, name, value, write=not count_echo)
            if count_echo and (not isinstance(data.get("timer"), list) or value != len(data["timer"])):
                raise ValueError("timer count does not match array")
        # 不用离线前的值判断写入成功；读取失败时不调用 HA 控制服务。
        self.snapshot(key)
        if timers_for_mapping(mapping):
            self.timer_protocol.write(self.timer_controller(sn), sn, sid, mapping, field, data)
            return self.snapshot(key)
        if set(data) != {field}:
            raise ValueError("one supported characteristic required")
        entity = device.route_action(sid, data)
        if entity != self.entity(sn, mapping.role):
            raise ValueError("invalid entity route")
        self.apply_scalar(entity, mapping, data)
        def confirmed():
            result = self.snapshot(key)
            actual = result.get(sid, {})
            tolerance = options.get("tolerance", 0)
            matches = field in actual and abs(actual[field] - data[field]) <= tolerance
            if options.get("requires_on"):
                matches = matches and bool(self.active(self.state(self.transport.states(), sn, mapping.role)))
            return matches
        self.wait(confirmed, max(self.settle_seconds, self.fan_settle_seconds if options.get("requires_on") else 0))
        return self.snapshot(key)

    def route_action(self, sn, payload):
        """兼容项目路由入口，但强制携带 SID，避免多个 on 控件被错误推断。"""
        if not any(device.sn == sn for device in self.devices.values()):
            return super().route_action(sn, payload)
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict) or set(payload) != {"sid", "data"}:
            raise ValueError("explicit SID required")
        key = next((key for key, device in self.devices.items() if device.sn == sn), None)
        return self.write(key, payload["sid"], payload["data"])

    def get_char_state(self, sn, svc_id):
        """与项目的按 SN/SID 查询接口对接，所有数据仍来自当前 HA 状态。"""
        key = next((key for key, device in self.devices.items() if device.sn == sn), None)
        if key is None:
            return super().get_char_state(sn, svc_id)
        value = self.snapshot(key).get(svc_id)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")) if value is not None else None

    def report_capability_state(self, entity_id, states):
        """依赖的启用/剩余时间实体变化也触发所有相关子卡上报。"""
        reported = False
        for key, device in self.devices.items():
            if entity_id not in self.dependencies[device.sn]:
                continue
            try:
                values = self.snapshot(key, states)
            except (ValueError, KeyError, TypeError):
                # 暂时不可用的依赖不伪报为关闭，也不阻断其他卡片上报。
                continue
            for sid, payload in values.items():
                service = device.product.services[sid]
                self._update_hw_bridge_status(device.sn, service.c_service_id or sid, payload)
            reported = True
        return reported

    def report_state(self, sn, entity_id, entity_state):
        if self.sn_manager.get_by_sn(sn) is None:
            return False
        managed = any(device.sn == sn for device in self.devices.values())
        reported = False if managed else super().report_state(sn, entity_id, entity_state)
        if not any(entity_id in entities for entities in self.dependencies.values()):
            return reported
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._hass is not None and loop is self._hass.loop:
            # HA事件线程直接取本地快照，不等待本线程自己的服务协程。
            entities = {entity for roles in self.roles.values() for entity in roles.values()}
            states = {entity: state.as_dict() for entity in entities
                      if (state := self._hass.states.get(entity)) is not None}
        else:
            states = self.transport.states()
        return self.report_capability_state(entity_id, states) or reported
