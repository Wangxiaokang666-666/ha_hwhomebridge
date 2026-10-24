"""设备原生定时：角色绑定、状态确认和协议转换；不创建软件倒计时。"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from .availability import require_state
from .role_binding import integer


def numeric_state(state):
    if state["state"] in ("unknown", "unavailable") or isinstance(state["state"], bool):
        raise ValueError("native timer state unavailable")
    value = float(state["state"])
    if not math.isfinite(value):
        raise ValueError("native timer value must be finite")
    return value


@dataclass(frozen=True)
class TimerBinding:
    enable: str
    duration: str
    remaining: str
    maximum: int
    duration_unit: str = "minutes"
    remaining_unit: str = "minutes"

    @classmethod
    def parse(cls, data):
        required = {"enable", "duration", "remaining", "maximum"}
        if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {"duration_unit", "remaining_unit"}:
            raise ValueError("invalid native timer definition")
        result = cls(**data)
        integer(result.maximum, 1, 10080)
        roles = (result.enable, result.duration, result.remaining)
        if any(not isinstance(role, str) or not role for role in roles) or len(set(roles)) != 3:
            raise ValueError("native timer requires three distinct roles")
        if result.duration_unit not in ("minutes", "seconds") or result.remaining_unit not in ("minutes", "seconds"):
            raise ValueError("unsupported native timer unit")
        return result

    def validate_roles(self, bindings):
        for role, domain in ((self.enable, "switch"), (self.duration, "number"), (self.remaining, "sensor")):
            bindings.entity(role, domain)

    @property
    def duration_scale(self):
        return 60 if self.duration_unit == "seconds" else 1

    @property
    def remaining_scale(self):
        return 60 if self.remaining_unit == "seconds" else 1


def timers_for_mapping(mapping):
    if mapping.action == "countdown":
        timers = mapping.options.get("timers")
        if not isinstance(timers, dict) or not timers:
            raise ValueError("countdown requires native timers")
        return {key: TimerBinding.parse(data) for key, data in timers.items()}
    if mapping.action in ("native_timer", "timer_array"):
        return {"timer": TimerBinding.parse(mapping.options.get("timer"))}
    return {}


def validate_timer_mapping(mapping):
    timers = timers_for_mapping(mapping)
    if not timers:
        return
    if mapping.domain != "number":
        raise ValueError("native timer maps to a number role")
    if mapping.action == "countdown":
        fields = mapping.options.get("fields", {})
        if set(fields) != {"target", "enabled", "minutes", "remaining"} or any(not isinstance(x, str) or not x for x in fields.values()) or len(set(fields.values())) != 4:
            raise ValueError("invalid countdown fields")
        targets = mapping.options.get("targets")
        if not isinstance(targets, dict) or not targets:
            raise ValueError("invalid countdown targets")
        for key, selected in targets.items():
            if not isinstance(key, str) or not key.isascii() or not key.isdecimal() or str(int(key)) != key:
                raise ValueError("invalid countdown target enum")
            if not isinstance(selected, list) or not selected or any(x not in timers for x in selected) or len(set(selected)) != len(selected):
                raise ValueError("unknown countdown target")
        combined = mapping.options.get("combined_target")
        if combined not in targets or set(targets[combined]) != set(timers):
            raise ValueError("combined target must include all native timers")
    elif mapping.action == "timer_array":
        for key in ("target_sid", "target_field"):
            if not isinstance(mapping.options.get(key), str) or not mapping.options[key]:
                raise ValueError("timer array target required")


class NativeTimerController:
    """传输必须在 HA 服务执行完成后返回；等待策略由所在工作线程提供。"""

    def __init__(self, bindings, transport, authorize, wait):
        self.bindings = bindings
        self.transport = transport
        self.authorize = authorize
        self.wait = wait

    def state(self, states, role):
        return require_state(states.get(self.bindings.entity(role)))

    def enabled(self, timer, states):
        value = self.state(states, timer.enable)["state"]
        if value not in ("on", "off"):
            raise ValueError("native timer enable state is not boolean")
        return value == "on"

    def configured(self, timer, states):
        value = numeric_state(self.state(states, timer.duration)) / timer.duration_scale
        if not 0 <= value <= timer.maximum:
            raise ValueError("native timer duration outside range")
        return value

    def remaining(self, timer, states=None):
        timer.validate_roles(self.bindings)
        states = self.transport.states() if states is None else states
        if not self.enabled(timer, states):
            return 0
        value = numeric_state(self.state(states, timer.remaining)) / timer.remaining_scale
        if not 0 <= value <= timer.maximum:
            raise ValueError("native timer remainder outside range")
        return math.ceil(value)

    def prepare(self, timer, minutes):
        timer.validate_roles(self.bindings)
        minutes = integer(minutes, 0, timer.maximum)
        self.authorize([self.bindings.entity(timer.enable), self.bindings.entity(timer.duration)])
        states = self.transport.states()
        self.enabled(timer, states)
        if minutes:
            self.configured(timer, states)
            remainder = numeric_state(self.state(states, timer.remaining))
            if not 0 <= remainder <= timer.maximum * timer.remaining_scale:
                raise ValueError("native timer remainder outside range")
            attributes = self.state(states, timer.duration).get("attributes", {})
            value = minutes * timer.duration_scale
            low, high = attributes.get("min", 0), attributes.get("max", timer.maximum * timer.duration_scale)
            if any(type(bound) not in (int, float) or not math.isfinite(bound) for bound in (low, high)) or low > high:
                raise ValueError("invalid HA number range")
            if value < low or value > high:
                raise ValueError("duration outside HA number range")
            step = attributes.get("step", 1)
            if not isinstance(step, (int, float)) or isinstance(step, bool) or not math.isfinite(step) or step <= 0:
                raise ValueError("invalid HA number step")
            if not math.isclose((value - low) / step, round((value - low) / step), abs_tol=1e-6):
                raise ValueError("duration incompatible with HA number step")
        return minutes

    def set(self, timer, minutes):
        minutes = self.prepare(timer, minutes)
        if not minutes and not self.enabled(timer, self.transport.states()):
            return
        if minutes:
            self.transport.call("number", "set_value", self.bindings.entity(timer.duration), value=minutes * timer.duration_scale)
            # 先核对时长，防止按旧时长启用设备；失败不继续发开关命令。
            self.wait(lambda: self.configured(timer, self.transport.states()) == minutes)
        self.transport.call("switch", "turn_on" if minutes else "turn_off", self.bindings.entity(timer.enable))
        def confirmed():
            states = self.transport.states()
            enabled = self.enabled(timer, states)
            return enabled == bool(minutes) and (not minutes or self.configured(timer, states) == minutes and self.remaining(timer, states) > 0)
        self.wait(confirmed)


class NativeTimerProtocol:
    """分钟值、单个延时数组、组合倒计时共用同一设备定时控制器。"""

    def __init__(self):
        self.forms = {}

    def read(self, controller, sn, sid, mapping, field, states):
        options = mapping.options
        timers = timers_for_mapping(mapping)
        if mapping.action == "countdown":
            fields = options["fields"]
            remaining = {key: controller.remaining(timer, states) for key, timer in timers.items()}
            positive = {key for key, value in remaining.items() if value}
            inferred = next((key for key, targets in options["targets"].items() if set(targets) == positive), options["combined_target"])
            selection, minutes = self.forms.get((sn, sid), (int(inferred), None))
            selected = options["targets"][str(selection)]
            values = [remaining[key] for key in selected if remaining[key] > 0]
            if minutes is None:
                configured = [controller.configured(timers[key], states) for key in selected]
                minutes = next((int(value) for value in configured if value > 0), 1)
            return {fields["target"]: selection, fields["minutes"]: minutes,
                    fields["enabled"]: int(bool(values)), fields["remaining"]: min(values) if values else 0}
        timer = timers["timer"]
        minutes = controller.remaining(timer, states)
        if mapping.action == "native_timer":
            return {field: minutes}
        result = {"action": 0, "num": int(minutes > 0), "timer": []}
        if minutes:
            source = controller.state(states, timer.remaining)
            measured = datetime.fromisoformat(source["last_updated"])
            if measured.tzinfo is None:
                raise ValueError("timer timestamp needs a timezone")
            seconds = numeric_state(source) * 60 / timer.remaining_scale
            end = (measured + timedelta(seconds=seconds)).astimezone(timezone.utc)
            result["timer"] = [{"id": 1, "enable": 1, "sid": options["target_sid"], "para": options["target_field"],
                                "paraValue": 0, "end": end.strftime("%Y%m%dT%H%M%SZ")}]
        return result

    def write(self, controller, sn, sid, mapping, field, data, now=None):
        options = mapping.options
        timers = timers_for_mapping(mapping)
        form = None
        if mapping.action == "countdown":
            fields = options["fields"]
            if set(data) - {fields[key] for key in ("target", "enabled", "minutes")}:
                raise ValueError("unsupported countdown fields")
            current = self.read(controller, sn, sid, mapping, field, controller.transport.states())
            target = integer(data.get(fields["target"], current[fields["target"]]), 0, 2147483647)
            selected = options["targets"].get(str(target))
            if not selected:
                raise ValueError("unsupported countdown target")
            minutes = integer(data.get(fields["minutes"], current[fields["minutes"]]), 1, min(timers[key].maximum for key in selected))
            controller.authorize([controller.bindings.entity(role) for key in selected
                                  for role in (timers[key].enable, timers[key].duration)])
            operations = []
            if fields["enabled"] in data:
                enabled = integer(data[fields["enabled"]], 0, 1)
                operations = [(timers[key], minutes if enabled else 0) for key in selected]
            form = (target, minutes)
        elif mapping.action == "timer_array":
            if set(data) - {"action", "num", "timer"}:
                raise ValueError("invalid timer-array fields")
            command = integer(data.get("action"), 0, 2)
            entries = data.get("timer", [])
            if not isinstance(entries, list) or len(entries) > 1:
                raise ValueError("one native countdown supported")
            if "num" in data and integer(data["num"], 0, 1) != len(entries):
                raise ValueError("timer count does not match array")
            if command == 2:
                if entries and (not isinstance(entries[0], dict) or set(entries[0]) != {"id"} or integer(entries[0]["id"], 1, 1) != 1):
                    raise ValueError("unknown native countdown ID")
                minutes = 0
            else:
                if len(entries) != 1 or not isinstance(entries[0], dict):
                    raise ValueError("one native delayed-off timer required")
                entry = entries[0]
                if set(entry) - {"id", "enable", "sid", "para", "paraValue", "end"} or entry.get("sid", options["target_sid"]) != options["target_sid"] or entry.get("para", options["target_field"]) != options["target_field"]:
                    raise ValueError("unsupported timer target")
                integer(entry.get("paraValue", 0), 0, 0)
                integer(entry.get("id", 1), 1, 1)
                enabled = integer(entry.get("enable", 1), 0, 1)
                minutes = 0
                if enabled:
                    end = datetime.strptime(entry["end"], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                    minutes = math.ceil((end - (now or datetime.now(timezone.utc))).total_seconds() / 60)
                    integer(minutes, 1, timers["timer"].maximum)
            operations = [(timers["timer"], minutes)]
        else:
            if set(data) != {field}:
                raise ValueError("one native timer characteristic required")
            operations = [(timers["timer"], data[field])]
        # 合并目标先全部预检，不能第二个目标无权限时已改了第一个设备。
        for timer, minutes in operations:
            controller.prepare(timer, minutes)
        for timer, minutes in operations:
            controller.set(timer, minutes)
        if form is not None:
            self.forms[sn, sid] = form
