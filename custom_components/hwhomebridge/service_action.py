"""
Service Action Dispatcher - 服务操作分发

负责将控制命令分发到正确的 HA entity。
替代原有的 light.py、fan.py、device.py 中分散的操作逻辑。

设计原则：
- 使用 HA 的 service call 机制（hass.services.async_call）统一调用
- 通过 ServiceDef.ha_mapping 中的 action 字段分发操作
- 支持后续扩展（电饭煲、灭蚊器等新品类）
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from .product_registry import HAMapping, ServiceDef

_LOGGER = logging.getLogger(__name__)


class ServiceActionDispatcher:
    """根据 ServiceDef 的定义，将控制命令分发到正确的 HA entity

    使用 HA 的 service call 机制统一调用不同 domain 的操作方法，
    替代原来直接调用 entity 的 async_turn_on/async_turn_off 等方法。
    """

    def __init__(self, hass=None):
        """
        Args:
            hass: HomeAssistant 实例，用于调用 service call
        """
        self._hass = hass

    def set_hass(self, hass):
        """设置 HomeAssistant 实例"""
        self._hass = hass

    def dispatch(self, sn: str, entity_id: str, mapping: HAMapping,
                 payload: dict) -> bool:
        """分发控制操作到 HA entity

        Args:
            sn: 设备SN，用于管理定时器等设备级别的任务
            entity_id: 目标 HA entity ID
            mapping: HAMapping 定义，描述如何操作该 entity
            payload: 控制命令的 payload

        Returns:
            True 表示分发成功，False 表示失败
        """
        if not entity_id:
            _LOGGER.error("dispatch called with empty entity_id")
            return False

        if mapping is None or not mapping.action:
            _LOGGER.error(f"dispatch: mapping or action is None for entity {entity_id}")
            return False

        action = mapping.action
        
        # 应用值映射转换（如果定义）
        transformed_payload = self._apply_value_mapping(payload, mapping)
        
        _LOGGER.info(f"Dispatching action: entity={entity_id}, action={action}, "
                     f"original_payload={payload}, transformed_payload={transformed_payload}")

        try:
            if action == "turn_on_off":
                return self._handle_turn_on_off(entity_id, transformed_payload, mapping)
            elif action == "set_brightness":
                return self._handle_set_brightness(entity_id, transformed_payload, mapping)
            elif action == "set_color_temp":
                return self._handle_set_color_temp(entity_id, transformed_payload, mapping)
            elif action == "set_color":
                return self._handle_set_color(entity_id, transformed_payload)
            elif action == "set_speed":
                return self._handle_set_speed(entity_id, transformed_payload)
            elif action == "set_temperature":
                return self._handle_set_temperature(entity_id, transformed_payload)
            elif action == "press":
                return self._handle_press(entity_id)
            elif action == "set_option":
                return self._handle_set_option(entity_id, transformed_payload, mapping)
            elif action == "set_value":
                return self._handle_set_value(entity_id, transformed_payload, mapping)
            elif action == "read_only":
                _LOGGER.debug(f"read_only action for {entity_id}, ignored")
                return True
            else:
                _LOGGER.warning(f"Unknown action: {action} for entity {entity_id}")
                return False
        except Exception as e:
            _LOGGER.error(f"Error dispatching action {action} to {entity_id}: {e}")
            return False

    def _apply_value_mapping(self, payload: dict, mapping: HAMapping) -> dict:
        """应用值映射转换（华为值 → HA值）

        Args:
            payload: 原始 payload
            mapping: HAMapping 定义

        Returns:
            转换后的 payload
        """
        if not mapping.value_mapping:
            return payload
        
        vm = mapping.value_mapping
        transformed = dict(payload)
        
        # 对于枚举→数值映射（如mode枚举→温度值）
        if vm.mapping_type == "enum_to_number":
            # 检查payload中的mode字段
            if "mode" in transformed:
                hw_value = transformed["mode"]
                ha_value = vm.transform_hw_to_ha(hw_value)
                # 转换为value字段，用于set_value操作
                transformed["value"] = ha_value
                _LOGGER.debug(f"Value mapping applied: enum '{hw_value}' -> number {ha_value}")
        
        # 对于delay→select映射（华为delay服务→HA select选项）
        elif vm.mapping_type == "delay_to_select":
            action = transformed.get("action", 0)
            close_action = vm.mapping_dict.get("close_action", 2) if isinstance(vm.mapping_dict.get("close_action"), int) else 2
            
            # 处理关闭倒计时（action=2或其他指定的close_action）
            if action == close_action:
                close_option = vm.mapping_dict.get("0", "")
                if close_option:
                    transformed["mode"] = close_option
                    _LOGGER.info(f"Delay close action detected: action={action} -> '{close_option}'")
                else:
                    _LOGGER.warning(f"No close option mapping (key '0') found")
                return transformed
            
            # 处理创建/编辑倒计时（action=0/1）
            delay_list = transformed.get("delay", [])
            if delay_list and len(delay_list) > 0:
                delay_info = delay_list[0]
                end_time_str = delay_info.get("end", "")
                
                if end_time_str:
                    try:
                        # 解析UTC时间格式：yyyyMMddTHHmmssZ
                        end_time = datetime.strptime(end_time_str, "%Y%m%dT%H%M%SZ")
                        now = datetime.utcnow()
                        
                        # 计算延迟小时数
                        delay_hours = (end_time - now).total_seconds() / 3600
                        
                        _LOGGER.info(f"Delay time parsed: end={end_time_str}, "
                                   f"delay_hours={delay_hours:.1f}h")
                        
                        # 获取所有支持的小时数（排除"0"关闭选项）
                        supported_hours = [int(k) for k in vm.mapping_dict.keys() 
                                         if k.isdigit() and int(k) > 0]
                        
                        if supported_hours:
                            # 找到最接近的有效选项（距离相等时选择较小的）
                            closest_hour = min(supported_hours, 
                                             key=lambda x: (abs(x - delay_hours), x))
                            
                            option = vm.mapping_dict.get(str(closest_hour), "")
                            
                            if option:
                                # 转换为mode字段，用于set_option操作
                                transformed["mode"] = option
                                _LOGGER.info(f"Delay mapped to closest select option: "
                                           f"{delay_hours:.1f}h -> {closest_hour}h ('{option}')")
                            else:
                                _LOGGER.warning(f"No mapping for closest_hour={closest_hour}, "
                                              f"available keys: {list(vm.mapping_dict.keys())}")
                    
                    except ValueError as e:
                        _LOGGER.error(f"Failed to parse delay end time '{end_time_str}': {e}")
        
        return transformed

    # -------------------------------------------------------------------
    # 具体 action 处理方法
    # -------------------------------------------------------------------

    def _handle_turn_on_off(self, entity_id: str, payload: dict,
                            mapping: HAMapping = None) -> bool:
        """处理开关操作

        支持的 domain：
        - button: press 操作（on=0 → press 取消，on=1 → 由 on_command 处理，忽略）
        - select: select_option 操作（on=0 → 选 stop_option，on=1 → 由 on_command 处理，忽略）
        - switch/light/fan: turn_on/turn_off 操作
        """
        if not self._hass:
            return False

        power_on = payload.get("on", 0) == 1
        domain = entity_id.split('.')[0]

        # button 域：只有 press，没有 turn_on/turn_off
        if domain == "button":
            if not power_on:
                self._call_service_async(
                    domain="button",
                    service="press",
                    entity_id=entity_id
                )
                _LOGGER.info(f"Button pressed (cancel): {entity_id}")
            else:
                _LOGGER.debug(f"Ignored switch=on for button entity {entity_id}")
            return True

        # select 域：通过 select_option 控制开关（如美的电饭煲工作状态）
        if domain == "select":
            if not power_on:
                stop_option = mapping.stop_option if mapping else "停止"
                self._call_service_async(
                    domain="select",
                    service="select_option",
                    entity_id=entity_id,
                    data={"option": str(stop_option)}
                )
                _LOGGER.info(f"Select stop option '{stop_option}': {entity_id}")
            else:
                _LOGGER.debug(f"Ignored switch=on for select entity {entity_id} (handled by on_command)")
            return True

        # 普通开关处理（switch/light/fan）
        service = "turn_on" if power_on else "turn_off"
        self._call_service_async(
            domain=domain,
            service=service,
            entity_id=entity_id
        )
        return True

    def _handle_set_brightness(self, entity_id: str, payload: dict,
                                mapping: HAMapping) -> bool:
        """处理亮度设置"""
        if not self._hass:
            return False

        br = payload.get("brightness", 0)
        if br == 0:
            return False

        # 根据 brightness_range 做缩放
        brightness_range = mapping.brightness_range or 255
        br = int(br * 255 / brightness_range)
        br = min(br, 255)

        self._call_service_async(
            domain="light",
            service="turn_on",
            entity_id=entity_id,
            data={"brightness": br}
        )
        return True

    def _handle_set_color_temp(self, entity_id: str, payload: dict,
                                mapping: HAMapping = None) -> bool:
        """处理色温设置

        根据 mapping 中的 colorTemperature_min 和 colorTemperature_range(max)
        对值进行裁剪，避免下发设备不支持的值。
        """
        if not self._hass:
            return False

        ct = payload.get("colorTemperature", 0)
        if ct == 0:
            return False

        # 裁剪到设备支持的范围内
        if mapping:
            ct_min = mapping.color_temperature_min or 2000
            ct_max = mapping.color_temperature_range or 6500
            if ct < ct_min:
                _LOGGER.info(f"Color temp {ct} below min {ct_min}, clamped to {ct_min}")
                ct = ct_min
            elif ct > ct_max:
                _LOGGER.info(f"Color temp {ct} above max {ct_max}, clamped to {ct_max}")
                ct = ct_max

        self._call_service_async(
            domain="light",
            service="turn_on",
            entity_id=entity_id,
            data={"color_temp_kelvin": ct}
        )
        return True

    def _handle_set_color(self, entity_id: str, payload: dict) -> bool:
        """处理颜色设置"""
        if not self._hass:
            return False

        r = payload.get("red", 0)
        g = payload.get("green", 0)
        b = payload.get("blue", 0)

        self._call_service_async(
            domain="light",
            service="turn_on",
            entity_id=entity_id,
            data={"rgb_color": (r, g, b)}
        )
        return True

    def _handle_set_speed(self, entity_id: str, payload: dict) -> bool:
        """处理速度设置（风扇）"""
        if not self._hass:
            return False

        speed = payload.get("speed", 0)
        percentage = min(speed * 4, 100)

        self._call_service_async(
            domain="fan",
            service="set_percentage",
            entity_id=entity_id,
            data={"percentage": percentage}
        )
        return True

    def _handle_set_temperature(self, entity_id: str, payload: dict) -> bool:
        """处理温度设置（适用于热水壶等设备）"""
        if not self._hass:
            return False

        # sensor 类 entity 通常不支持设置温度
        # 这个 action 主要用于扩展，目前暂不实现
        _LOGGER.info(f"set_temperature not implemented yet for {entity_id}")
        return False

    def _handle_press(self, entity_id: str) -> bool:
        """处理按钮操作（适用于电饭煲 action 等 button 类型 entity）"""
        if not self._hass:
            return False

        self._call_service_async(
            domain=entity_id.split('.')[0],
            service="press",
            entity_id=entity_id
        )
        return True

    def _handle_set_option(self, entity_id: str, payload: dict, mapping: HAMapping = None) -> bool:
        """处理选项设置（适用于 select 类型 entity，如水壶 mode、电饭煲 cooker）
        
        支持两种模式：
        1. 直接值：payload 中的 option/value/mode 字段直接作为选项
        2. 多值匹配：通过 enum_to_text_multi 映射，从 HA entity 的 options 中找匹配项
        """
        if not self._hass:
            return False

        hw_mode = payload.get("mode")
        
        if mapping and mapping.value_mapping and mapping.value_mapping.mapping_type == "enum_to_text_multi":
            possible_options = mapping.value_mapping.transform_hw_to_ha(hw_mode)
            
            if not isinstance(possible_options, list) or len(possible_options) == 0:
                _LOGGER.warning(f"set_option: no mapped values for hw_mode={hw_mode}")
                return False
            
            ha_options = self._get_entity_options(entity_id)
            matched_option = self._find_matching_option(possible_options, ha_options)
            
            if not matched_option:
                _LOGGER.warning(f"set_option: no match found for hw_mode={hw_mode}, "
                               f"possible={possible_options}, ha_options={ha_options}")
                return False
            
            _LOGGER.info(f"set_option: matched hw_mode={hw_mode} -> ha_option={matched_option}")
            option = matched_option
        else:
            option = payload.get("option") or payload.get("value") or payload.get("mode", "")
            if not option:
                _LOGGER.warning(f"set_option: no option value in payload for {entity_id}")
                return False

        self._call_service_async(
            domain="select",
            service="select_option",
            entity_id=entity_id,
            data={"option": str(option)}
        )
        return True
    
    def _get_entity_options(self, entity_id: str) -> list:
        """获取 select entity 的可用选项列表"""
        if not self._hass:
            return []
        
        try:
            state = self._hass.states.get(entity_id)
            if state and hasattr(state, 'attributes'):
                options = state.attributes.get("options", [])
                return options if isinstance(options, list) else []
        except Exception as e:
            _LOGGER.debug(f"Failed to get options for {entity_id}: {e}")
        
        return []
    
    def _find_matching_option(self, possible_values: list, ha_options: list) -> str:
        """从 HA options 中找到与 possible_values 匹配的选项"""
        if not ha_options:
            return None
        
        for pv in possible_values:
            pv_str = str(pv).strip()
            for ha_opt in ha_options:
                ha_str = str(ha_opt).strip()
                if pv_str == ha_str:
                    _LOGGER.debug(f"Matched exactly: '{pv_str}' == '{ha_str}'")
                    return ha_opt
                if pv_str.lower() == ha_str.lower():
                    _LOGGER.debug(f"Matched case-insensitive: '{pv_str}' == '{ha_str}'")
                    return ha_opt
        
        for pv in possible_values:
            pv_str = str(pv).strip()
            for ha_opt in ha_options:
                ha_str = str(ha_opt).strip()
                if pv_str in ha_str or ha_str in pv_str:
                    _LOGGER.debug(f"Matched substring: '{pv_str}' in '{ha_str}'")
                    return ha_opt
        
        _LOGGER.debug(f"No match: possible={[str(v).strip() for v in possible_values]}, ha={[str(v).strip() for v in ha_options]}")
        return None

    def _handle_set_value(self, entity_id: str, payload: dict, mapping: HAMapping = None) -> bool:
        """处理数值设置（适用于 number 类型 entity，如电饭煲 time 定时、电水壶温度）"""
        if not self._hass:
            return False

        # 优先使用转换后的value字段，其次从payload推断
        value = payload.get("value") or payload.get("heatingTarget") or payload.get("time") or payload.get("temperature") or 0
        try:
            value = int(value)
        except (ValueError, TypeError):
            _LOGGER.warning(f"set_value: invalid value in payload for {entity_id}")
            return False

        self._call_service_async(
            domain="number",
            service="set_value",
            entity_id=entity_id,
            data={"value": value}
        )
        return True

    # -------------------------------------------------------------------
    # HA service call 辅助方法
    # -------------------------------------------------------------------

    def _call_service_async(self, domain: str, service: str,
                            entity_id: str, data: dict = None):
        """异步调用 HA service

        通过 hass.loop.call_soon_threadsafe 确保在主事件循环中执行。
        这种方式可以在 C 回调线程中安全调用。
        """
        if not self._hass:
            _LOGGER.error("hass is None, cannot call service")
            return

        if data is None:
            data = {}

        try:
            self._hass.loop.call_soon_threadsafe(
                lambda: self._hass.async_create_task(
                    self._hass.services.async_call(
                        domain=domain,
                        service=service,
                        target={"entity_id": entity_id},
                        service_data=data
                    )
                )
            )
        except Exception as e:
            _LOGGER.error(f"Failed to schedule service call {domain}.{service} for {entity_id}: {e}")
