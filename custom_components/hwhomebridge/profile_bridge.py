"""把已注册的公共 VirtualDevice 投影到云端 profile，保留原 SN 和 HA 路由。"""
import json
import math
from datetime import datetime, timedelta, timezone

from .availability import require_state


class ProfileBridge:
    """只暴露已有 HA 映射的 characteristic；profile 中其余能力不冒充支持。"""

    def __init__(self, router, device, profile):
        self.router, self.device = router, device
        self.services = {}
        self.bindings = {}
        self.timer_services = set()
        for internal, definition in device.product.services.items():
            if internal not in device.service_entries or definition.ha_mapping is None:
                continue
            candidates = [(service, characteristic) for service in profile['services']
                          for characteristic in service['characteristics']
                          if characteristic['characteristicName'] == definition.char_name]
            preferred = definition.c_service_id or internal
            exact = [pair for pair in candidates if pair[0]['serviceId'] == preferred]
            if exact:
                candidates = exact
            if len(candidates) != 1:
                raise ValueError('profile characteristic is missing or ambiguous')
            service, characteristic = candidates[0]
            sid = service['serviceId']
            self.services[sid] = service['serviceType']
            fields = self.bindings.setdefault(sid, {})
            if definition.char_name in fields:
                raise ValueError('duplicate mapped profile characteristic')
            fields[definition.char_name] = (internal, characteristic)
            mapping = definition.ha_mapping
            if mapping.value_mapping and mapping.value_mapping.mapping_type == 'delay_to_select':
                self.timer_services.add(sid)
        self.delay_deadlines = {}

    def snapshot(self):
        result = {}
        for sid, fields in self.bindings.items():
            merged = {}
            for name, (internal, _) in fields.items():
                definition = self.device.product.services[internal]
                mapping = definition.ha_mapping
                entity_state = self.router._hass.states.get(self.device.get_entity_for_service(internal))
                require_state(entity_state)
                if mapping.value_mapping and mapping.value_mapping.mapping_type == 'delay_to_select':
                    entity = self.device.get_entity_for_service(internal)
                    state = self.router._hass.states.get(entity)
                    if state is None:
                        raise ValueError('mapped timer entity unavailable')
                    hours = next((int(k) for k, v in mapping.value_mapping.mapping_dict.items()
                                  if k.isdigit() and v == state.state), None)
                    if hours is None:
                        raise ValueError('unmapped timer state')
                    if hours == 0:
                        merged.update(action=2, num=0, delay=[{}])
                    else:
                        previous = self.delay_deadlines.get(sid)
                        if previous is None or previous[0] != hours:
                            previous = (hours, datetime.now(timezone.utc) + timedelta(hours=hours))
                            self.delay_deadlines[sid] = previous
                        merged.update(action=0, num=1, delay=[{'enable': 1,
                            'end': previous[1].strftime('%Y%m%dT%H%M%SZ'), 'sid': 'switch', 'para': 'on'}])
                else:
                    raw = self.router.get_char_state(self.device.sn, definition.c_service_id or internal)
                    values = json.loads(raw) if raw else {}
                    if name not in values:
                        raise ValueError('mapped characteristic missing from HA projection')
                    merged[name] = values[name]
            result[sid] = merged
        return result

    def write(self, sid, payload):
        if sid not in self.bindings or not isinstance(payload, dict) or not payload:
            raise ValueError('unknown or empty profile command')
        fields = self.bindings[sid]
        timer = sid in self.timer_services
        if timer:
            payload = self._validate_timer(payload)
        else:
            if not set(payload) <= set(fields):
                raise ValueError('unmapped profile characteristic')
            # 先验证整条指令，避免部分成功后才发现只读或非法字段。
            for name, value in payload.items():
                internal, schema = fields[name]
                mapping = self.device.product.services[internal].ha_mapping
                if 'W' not in schema['method'] or mapping.action in (None, 'read_only'):
                    raise ValueError('read-only characteristic')
                if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
                    raise ValueError('finite numeric command required')
                if schema['characteristicType'] != 'float' and type(value) is not int:
                    raise ValueError('integer command required')
                if ('min' in schema and value < float(schema['min'])) or ('max' in schema and value > float(schema['max'])):
                    raise ValueError('profile range exceeded')
                if 'enumList' in schema and value not in [int(row['enumVal']) for row in schema['enumList']]:
                    raise ValueError('unknown profile enum')
        if timer:
            commands = [payload]
        else:
            commands = [{name: value} for name, value in payload.items()]
        for command in commands:
            expected = next(iter(fields.values()))[0] if timer else fields[next(iter(command))][0]
            if self.router._determine_target_service(command, self.device) != expected:
                raise ValueError('legacy command cannot preserve the explicit service')
        self.snapshot()
        for command in commands:
            if not self.router.route_action(self.device.sn, command):
                raise ValueError('public HA router rejected command')

    @staticmethod
    def _validate_timer(payload):
        if (set(payload) - {'action', 'num', 'delay'} or not {'action', 'delay'} <= set(payload)
                or type(payload['action']) is not int):
            raise ValueError('timer action and array required')
        payload = {**payload, 'num': payload.get('num', 0 if payload['action'] == 2 else 1)}
        if payload['action'] not in (0, 1, 2) or type(payload['num']) is not int or not isinstance(payload['delay'], list):
            raise ValueError('invalid timer command')
        if payload['action'] == 2:
            if payload['num'] != 0 or payload['delay'] not in ([], [{}]):
                raise ValueError('invalid timer cancellation')
        else:
            if payload['num'] != 1 or len(payload['delay']) != 1:
                raise ValueError('one native select timer supported')
            item = payload['delay'][0]
            if not isinstance(item, dict) or set(item) - {'enable', 'end', 'sid', 'para'}:
                raise ValueError('invalid timer item')
            item = {**item, 'enable': item.get('enable', 1)}
            if type(item['enable']) is not int or item['enable'] != 1:
                raise ValueError('invalid timer enable')
            if item.get('sid', 'switch') != 'switch' or item.get('para', 'on') != 'on':
                raise ValueError('unsupported timer target')
            end = datetime.strptime(item.get('end', ''), '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
            if end <= datetime.now(timezone.utc):
                raise ValueError('expired timer')
            payload['delay'] = [item]
        return payload
