"""
Service Router - 控制路由与状态上报

负责：
1. 控制命令路由：从 C 回调 (OnPyActionCB) 的 SN + payload 路由到正确的 entity
2. 状态上报路由：从 HA state change 事件路由到正确的 C 侧上报接口
3. 设备注册：聚合 HA device 下的 entity，创建 VirtualDevice 并注册
"""

import json
import os
import logging
from datetime import datetime, timedelta
from typing import Optional

from .product_registry import ProductRegistry, ProductDef, ServiceDef, HAMapping
from .product_matcher import ProductMatcher, DeviceInfo
from .virtual_device import VirtualDevice, ServiceEntry, SNManager
from .service_action import ServiceActionDispatcher

_LOGGER = logging.getLogger(__name__)


class ServiceRouter:
    """控制路由：整合 VirtualDevice、SNManager、ServiceActionDispatcher，
    提供完整的控制命令路由和状态上报功能
    """

    def __init__(self):
        self.registry = ProductRegistry()
        self.matcher = ProductMatcher(self.registry)
        self.sn_manager = SNManager()
        self.action_dispatcher = ServiceActionDispatcher()
        self._lib = None  # C library reference，用于调用 UpdateHAStatus 等
        self._hass = None  # HA instance

    def set_hass(self, hass):
        """设置 HomeAssistant 实例"""
        self._hass = hass
        self.action_dispatcher.set_hass(hass)

    def set_lib(self, lib):
        """设置 C library 引用"""
        self._lib = lib

    async def load_product_registry(self, config_path: str) -> bool:
        """加载产品定义配置（异步）

        加载框架层（product_registry.json）和适配器层（adapters/ 目录）。

        Args:
            config_path: product_registry.json 的路径（框架层）
                         adapters 目录从 config_path 的同级目录推导

        Returns:
            True 表示框架层加载成功
        """
        config_dir = os.path.dirname(config_path)
        adapters_dir = os.path.join(config_dir, 'adapters')
        return await self.registry.load(config_path, adapters_dir)

    # -------------------------------------------------------------------
    # 设备注册与聚合
    # -------------------------------------------------------------------

    def register_device(self, device_id: str, device_name: str,
                        entity_list: list, model: str = "",
                        manufacturer: str = "",
                        reuse_sn: str = None) -> Optional[VirtualDevice]:
        """注册一个 HA device 到服务路由中

        执行以下步骤：
        1. 收集 device 信息，构建 DeviceInfo
        2. 通过 ProductMatcher 匹配到产品定义
        3. 创建 VirtualDevice，建立 service → entity 映射
        4. 分配 SN 并注册到 SNManager

        Args:
            device_id: HA device_id
            device_name: HA device name
            entity_list: 该 device 下的 entity 列表，每个元素是 dict：
                {"entity_id": str, "domain": str, "device_class": str|None}
            model: device model
            manufacturer: device manufacturer
            reuse_sn: 如果提供，复用此 SN 而非重新生成（用于设备更新场景）

        Returns:
            创建的 VirtualDevice，如果注册失败返回 None
        """
        # 1. 构建 DeviceInfo 用于产品匹配
        entity_domains = []
        entity_device_classes = {}
        for e in entity_list:
            domain = e.get("domain", "")
            entity_domains.append(domain)
            device_class = e.get("device_class")
            if device_class:
                entity_device_classes[domain] = device_class

        device_info = DeviceInfo(
            device_id=device_id,
            name=device_name,
            model=model,
            manufacturer=manufacturer,
            entity_domains=entity_domains,
            entity_device_classes=entity_device_classes
        )

        # 2. 产品匹配
        product, strategy = self.matcher.match_with_detail(device_info)
        if product is None:
            _LOGGER.debug(f"Device {device_id} ({model}) did not match any product, skip")
            return None

        # 3. 创建 VirtualDevice
        if reuse_sn:
            # 更新场景：复用原 SN
            # 先注销旧的 VirtualDevice（如果存在）
            self.sn_manager.unregister(reuse_sn)
            sn = reuse_sn
        else:
            # 新设备：生成新 SN
            sn = SNManager.generate_sn(device_id)
        
        vd = VirtualDevice(sn=sn, product=product, ha_device_id=device_id)

        # 4. 建立 service → entity 映射
        mapping_count = self._build_service_mapping(vd, product, entity_list)

        if mapping_count == 0:
            _LOGGER.warning(f"Device {device_id}: product {product.pid} matched, "
                            f"but no service-entity mapping could be established, skip")
            return None

        # 5. 注册到 SNManager
        result = self.sn_manager.register(vd)
        if not result:
            _LOGGER.error(f"Device {device_id}: SNManager registration failed (SN conflict?)")
            return None

        _LOGGER.info(f"Registered device: sn={sn}, product={product.pid} ({product.name}), "
                      f"device={device_id}, strategy={strategy}, "
                      f"mappings={mapping_count}")
        return vd

    def _build_service_mapping(self, vd: VirtualDevice, product: ProductDef,
                                entity_list: list) -> int:
        """根据 ProductDef 的 service 定义，将 entity 映射到 service

        Args:
            vd: VirtualDevice
            product: 产品定义
            entity_list: entity 列表

        Returns:
            成功建立的映射数量
        """
        mapping_count = 0

        for svc_id, svc_def in product.services.items():
            # 根据 ServiceDef.ha_mapping 的 domain 来匹配 entity
            best_entity = self._find_best_entity(svc_def, entity_list)
            if best_entity is not None:
                entry = ServiceEntry(
                    entity_id=best_entity["entity_id"],
                    domain=best_entity.get("domain", ""),
                    service_name=svc_id
                )
                vd.add_service_entry(svc_id, entry)
                mapping_count += 1
                _LOGGER.debug(f"  Mapped service '{svc_id}' -> entity '{best_entity['entity_id']}'")
            else:
                _LOGGER.debug(f"  Service '{svc_id}': no matching entity found")

        return mapping_count

    def _find_best_entity(self, svc_def: ServiceDef, entity_list: list) -> Optional[dict]:
        """根据 ServiceDef 的 ha_mapping，在 entity 列表中找到最佳匹配

        匹配逻辑（递进式）：
        1. domain 必须匹配
        2. device_class 如果定义，必须匹配：
           - 有匹配 → 在匹配集合中继续
           - 无匹配且有 name_keywords → 在 domain 候选中做关键词匹配
           - 无匹配且无 name_keywords → 返回 None（不回退到任意 entity）
        3. name_keywords 如果定义，选择匹配分数最高的（包含匹配）
        4. name_keywords 定义但无匹配时，回退到第一个候选

        Args:
            svc_def: service 定义
            entity_list: entity 列表，每个元素包含 entity_id, domain, device_class, name

        Returns:
            最佳匹配的 entity dict，如果没有匹配返回 None
        """
        mapping = svc_def.ha_mapping
        if not mapping or not mapping.domain:
            return None

        # Level 1: domain 过滤（必须匹配）
        candidates = []
        for entity in entity_list:
            entity_domain = entity.get("domain", "")
            if entity_domain == mapping.domain:
                candidates.append(entity)

        if not candidates:
            _LOGGER.debug(f"_find_best_entity: no entity with domain '{mapping.domain}'")
            return None

        # Level 1.5: exclude_keywords 排除（实体名/id 包含任意关键词则排除）
        if mapping.exclude_keywords:
            filtered = []
            for entity in candidates:
                entity_name = (entity.get("name") or "").lower()
                entity_id = (entity.get("entity_id") or "").lower()
                excluded = False
                for kw in mapping.exclude_keywords:
                    kw_lower = kw.lower()
                    if kw_lower in entity_name or kw_lower in entity_id:
                        excluded = True
                        break
                if not excluded:
                    filtered.append(entity)
            if filtered:
                candidates = filtered
                _LOGGER.debug(f"_find_best_entity: exclude_keywords filtered to {len(candidates)} candidates")

        # Level 1.6: include_keywords 包含（实体名/id 必须包含至少一个关键词才保留）
        if mapping.include_keywords:
            filtered = []
            for entity in candidates:
                entity_name = (entity.get("name") or "").lower()
                entity_id = (entity.get("entity_id") or "").lower()
                for kw in mapping.include_keywords:
                    kw_lower = kw.lower()
                    if kw_lower in entity_name or kw_lower in entity_id:
                        filtered.append(entity)
                        break
            if filtered:
                candidates = filtered
                _LOGGER.debug(f"_find_best_entity: include_keywords filtered to {len(candidates)} candidates")

        # Level 2: device_class 过滤（如果定义，必须匹配）
        if mapping.device_class:
            filtered = []
            for entity in candidates:
                entity_dc = entity.get("device_class")
                if entity_dc == mapping.device_class:
                    filtered.append(entity)

            if filtered:
                candidates = filtered
            else:
                # device_class 定义但无匹配：
                # - 若有 name_keywords，仍尝试在 domain 候选中做关键词匹配
                # - 若无 name_keywords，不回退到任意 entity，返回 None
                if not mapping.name_keywords:
                    _LOGGER.debug(f"_find_best_entity: no entity with device_class "
                                 f"'{mapping.device_class}', no name_keywords fallback, "
                                 f"skipping service")
                    return None
                _LOGGER.debug(f"_find_best_entity: no entity with device_class "
                             f"'{mapping.device_class}', fallback to name_keywords matching")

        # Level 3: name_keywords 匹配（如果定义）
        if mapping.name_keywords:
            best_entity = None
            best_score = -1

            for entity in candidates:
                entity_name = (entity.get("name") or "").lower()
                entity_id = (entity.get("entity_id") or "").lower()

                score = 0
                for kw in mapping.name_keywords:
                    kw_lower = kw.lower()
                    if kw_lower in entity_name:
                        score += 2
                    elif kw_lower in entity_id:
                        score += 1

                if score > best_score:
                    best_score = score
                    best_entity = entity

            if best_score > 0 and best_entity:
                _LOGGER.debug(f"_find_best_entity: matched by name_keywords, "
                             f"entity={best_entity['entity_id']}, score={best_score}")
                return best_entity

            _LOGGER.debug(f"_find_best_entity: name_keywords defined but no match, "
                         f"fallback to first candidate")
            return candidates[0] if candidates else None

        # 无 name_keywords，返回第一个候选
        return candidates[0] if candidates else None

    # -------------------------------------------------------------------
    # 控制命令路由（C → Python 方向）
    # -------------------------------------------------------------------

    def route_action(self, sn: str, payload: str) -> bool:
        """路由控制命令：SN + payload → entity + action

        此方法由 C 回调 OnPyActionCB 调用。

        Args:
            sn: 设备 SN
            payload: JSON 格式的控制命令

        Returns:
            True 表示路由成功
        """
        # 1. 查找 VirtualDevice
        vd = self.sn_manager.get_by_sn(sn)
        if vd is None:
            _LOGGER.warning(f"route_action: SN {sn} not found in registered devices")
            return False

        # 2. 解析 payload
        try:
            payload_dict = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError:
            _LOGGER.error(f"route_action: invalid payload JSON: {payload}")
            return False

        # 3. 确定目标 service
        service_id = self._determine_target_service(payload_dict, vd)
        if service_id is None:
            _LOGGER.warning(f"route_action: cannot determine target service for payload: {payload_dict}")
            return False

        # 4. 路由到 entity
        entity_id = vd.route_action(service_id, payload_dict)
        if entity_id is None:
            _LOGGER.warning(f"route_action: service '{service_id}' has no entity mapping")
            return False

        # 5. 获取 ServiceDef，执行操作
        svc_def = vd.product.services.get(service_id)
        if svc_def is None or svc_def.ha_mapping is None:
            _LOGGER.warning(f"route_action: service '{service_id}' has no ServiceDef or HAMapping")
            return False

        _LOGGER.info(f"route_action: sn={sn}, service={service_id}, "
                      f"entity={entity_id}, payload={payload_dict}")

        # 声明式控制覆盖：检查 on_command（替代原硬编码电饭煲逻辑）
        # 当 switch.on=1 且 ha_mapping 定义了 on_command 时，执行声明的控制逻辑
        on_value = payload_dict.get("on")
        if on_value == 1 and svc_def.ha_mapping.on_command:
            return self._execute_on_command(sn, vd, svc_def, payload_dict)

        # 缓存华为枚举值（用于非双射映射的反向状态上报）
        if svc_def.ha_mapping and svc_def.ha_mapping.value_mapping:
            vm = svc_def.ha_mapping.value_mapping
            if vm.mapping_type in ("enum_to_number", "enum_to_text_multi"):
                for key in payload_dict:
                    payload_val = str(payload_dict[key])
                    if payload_val in vm.mapping_dict:
                        vd.last_commanded_enum[service_id] = payload_val
                        _LOGGER.debug(f"Cached commanded enum for {service_id}: {payload_val}")
                        break

        return self.action_dispatcher.dispatch(
            sn=sn,
            entity_id=entity_id,
            mapping=svc_def.ha_mapping,
            payload=payload_dict
        )

    def _execute_on_command(self, sn: str, vd: VirtualDevice,
                             svc_def: ServiceDef, payload: dict) -> bool:
        """执行声明式的 on_command 控制覆盖

        当 switch.on=1 且 ha_mapping.on_command 定义时，替代直接 dispatch。
        支持的控制类型：
        - trigger_service: 触发另一个 service（如 cooker），使用其 default_mode

        Args:
            sn: 设备 SN
            vd: VirtualDevice
            svc_def: 当前 switch 服务的 ServiceDef
            payload: 原始 payload

        Returns:
            True 表示执行成功
        """
        cmd = svc_def.ha_mapping.on_command
        if not cmd:
            return False

        cmd_type = cmd.get("type")

        if cmd_type == "trigger_service":
            target_service = cmd.get("service")
            if not target_service:
                _LOGGER.warning(f"on_command trigger_service: missing 'service' field")
                return False

            target_entity = vd.get_entity_for_service(target_service)
            target_svc_def = vd.product.services.get(target_service)

            if not target_entity or not target_svc_def or not target_svc_def.ha_mapping:
                _LOGGER.warning(f"on_command: target service '{target_service}' or entity not found")
                return False

            # 确定模式值：优先使用当前 HA 状态对应的华为枚举值，否则用 default_mode
            mode = target_svc_def.ha_mapping.default_mode or "1"

            if self._hass and target_svc_def.ha_mapping.value_mapping:
                vm = target_svc_def.ha_mapping.value_mapping
                if vm.mapping_type == "enum_to_text_multi":
                    state = self._hass.states.get(target_entity)
                    if state and state.state not in ("unavailable", "unknown", ""):
                        hw_mode = vm.transform_ha_to_hw(state.state)
                        if hw_mode:
                            mode = hw_mode

            _LOGGER.info(f"on_command trigger_service: sn={sn}, target={target_service}, "
                         f"entity={target_entity}, mode={mode}")

            return self.action_dispatcher.dispatch(
                sn=sn,
                entity_id=target_entity,
                mapping=target_svc_def.ha_mapping,
                payload={"mode": mode}
            )

        _LOGGER.warning(f"on_command: unknown type '{cmd_type}'")
        return False

    def _determine_target_service(self, payload: dict, vd: VirtualDevice) -> Optional[str]:
        """根据 payload 内容确定目标 service

        策略：
        1. 根据 payload 中的 key 推断 service（支持一个 key 映射到多个可能 service）
        2. 如果无法推断，尝试每个已注册的 service

        Args:
            payload: 控制命令的 payload
            vd: VirtualDevice

        Returns:
            service ID，找不到时返回 None
        """
        key_service_candidates = {
            "on": ["switch", "action"],
            "brightness": ["brightness"],
            "speed": ["speed"],
            "red": ["color"],
            "colorTemperature": ["cct", "color_temp"],
            "temperature": ["temperature"],
            "mode": ["mode", "cooker", "alarmBell"],
            "status": ["status"],
            "timer": ["timer"],
            "delay": ["delay"],
            "leftTime": ["leftTime"],
            "action": ["action"],
            "cooker": ["cooker"],
            "pressure": ["pressure"],
            "electric": ["electric"],
            "totalConsum": ["electric"],
            "currentVoltage1": ["voltage"],
            "currentPower1": ["power"],
            "currentElectric1": ["current"],
            "heatingTarget": ["heatingTarget"],
            "preserveTarget": ["temperature"],
        }

        for key in payload:
            candidates = key_service_candidates.get(key, [])
            for service_id in candidates:
                if service_id in vd.service_entries:
                    return service_id

        available_services = list(vd.service_entries.keys())
        if len(available_services) == 1:
            return available_services[0]

        _LOGGER.warning(f"route_action: cannot determine target service for payload: {payload}, "
                       f"available services: {available_services}")
        return None

    # -------------------------------------------------------------------
    # 状态上报路由（Python → C 方向）
    # -------------------------------------------------------------------

    def report_state(self, sn: str, entity_id: str, entity_state) -> bool:
        """将 HA entity 的状态上报到 HiLink

        此方法由 HA state change 事件触发。

        上报策略：
        1. 根据 service 的 value_attr 和 domain 决定上报方式
        2. charName 从 svc_def.char_name 读取（与 profile characteristicName 一致）
        3. 不存在的服务不报错，静默跳过

        Args:
            sn: 设备 SN
            entity_id: 发生变化的 entity_id
            entity_state: HA state 对象

        Returns:
            True 表示上报成功
        """
        vd = self.sn_manager.get_by_sn(sn)
        if vd is None:
            return False

        # 找到该 entity 对应的所有 services（支持一个 entity 映射到多个服务）
        service_ids = vd.get_all_services_for_entity(entity_id)
        if not service_ids:
            return False

        # 对每个 service 上报状态
        success = False
        for service_id in service_ids:
            svc_def = vd.product.services.get(service_id)
            if svc_def is None or svc_def.ha_mapping is None:
                continue

            try:
                self._report_by_type(sn, service_id, svc_def, entity_state)
                success = True
            except Exception as e:
                _LOGGER.error(f"report_state error for {entity_id}, service={service_id}: {e}")

        return success

    def _report_by_type(self, sn: str, service_id: str,
                               svc_def: ServiceDef, entity_state):
        """根据 service 类型分发上报逻辑

        关键：UpdateHAStatus 的 payload 中的 key 必须与 profile 中的
        characteristicName 一致，否则上报无效。
        char_name 从 svc_def.char_name 读取，该值在 product_registry.json 中配置，
        与 profile 中的 characteristicName 保持一致，用于构建上报payload的key。
        c_svc_id 为 C 侧 serviceId（=profile serviceId），当 ServiceDef 定义了
        c_service_id 时使用之（多 characteristic 服务场景），否则等于 service_id。

        Args:
            sn: 设备 SN
            service_id: Python 侧逻辑服务 ID（如 "switch", "brightness" 等）
            svc_def: 服务定义（包含 char_name, c_service_id, ha_mapping 等）
            entity_state: HA state 对象
        """
        mapping = svc_def.ha_mapping
        domain = mapping.domain
        value_attr = mapping.value_attr
        char_name = svc_def.char_name
        c_svc_id = svc_def.c_service_id or service_id

        if value_attr == "state" and domain in ("switch", "light", "fan"):
            # switch 类型：上报 on/off
            is_on = entity_state.state == "on"
            self._update_hw_bridge_status(sn, c_svc_id, {char_name: 1 if is_on else 0})

            # light/fan 类型需要额外上报属性
            if entity_state.state == "on" and domain in ("light", "fan"):
                self._report_additional_attributes(sn, entity_state, mapping)

        elif value_attr == "state" and domain in ("sensor", "binary_sensor"):
            state_value = entity_state.state
            
            if mapping.value_mapping:
                vm_type = mapping.value_mapping.mapping_type
                
                if vm_type == "text_to_enum":
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state_value)
                    try:
                        hw_enum = int(hw_value)
                        self._update_hw_bridge_status(sn, c_svc_id, {char_name: hw_enum})
                        _LOGGER.debug(f"Applied text_to_enum mapping: '{state_value}' -> {hw_enum}")
                    except (ValueError, TypeError):
                        _LOGGER.warning(f"Cannot convert mapped value '{hw_value}' to int for {service_id}")
                
                elif vm_type == "number_to_enum_multi":
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state_value)
                    try:
                        hw_enum = int(hw_value)
                        self._update_hw_bridge_status(sn, c_svc_id, {char_name: hw_enum})
                        _LOGGER.debug(f"Applied number_to_enum_multi mapping: '{state_value}' -> {hw_enum}")
                        
                        # 电饭煲特殊处理：status 变化时同步更新 switch 状态
                        # switch 状态由工作状态决定：工作中/暂停中/预约中 → 开，待机/完成 → 关
                        if service_id == "status":
                            is_running = hw_enum in (1, 2, 3)  # running, pause, ordered
                            switch_val = 1 if is_running else 0
                            self._update_hw_bridge_status(sn, "switch", {"on": switch_val})
                            _LOGGER.debug(f"Synced switch state based on status: {hw_enum} -> {'on' if is_running else 'off'}")
                            
                            # 更新 VirtualDevice 的 switch_state 缓存（用于 C 回调查询）
                            vd = self.sn_manager.get_by_sn(sn)
                            if vd:
                                vd.switch_state = switch_val
                    except (ValueError, TypeError):
                        _LOGGER.warning(f"Cannot convert mapped value '{hw_value}' to int for {service_id}")
                
                elif vm_type == "seconds_to_minutes":
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state_value)
                    self._update_hw_bridge_status(sn, c_svc_id, {char_name: hw_value})
                    _LOGGER.debug(f"Applied seconds_to_minutes mapping: '{state_value}' -> {hw_value} min")
                
                else:
                    self._try_report_number(sn, c_svc_id, char_name, state_value, svc_def.value_type or "int", mapping.value_scale)
            else:
                self._try_report_number(sn, c_svc_id, char_name, state_value, svc_def.value_type or "int", mapping.value_scale)

        elif value_attr == "state" and domain == "select":
            state = entity_state.state
            if state and state not in ("unavailable", "unknown"):
                if mapping.value_mapping and mapping.value_mapping.mapping_type == "enum_to_text_multi":
                    # 优先使用缓存的华为枚举值
                    vd = self.sn_manager.get_by_sn(sn)
                    cached_enum = vd.last_commanded_enum.get(service_id) if vd else None
                    if cached_enum and cached_enum in mapping.value_mapping.mapping_dict:
                        # 检查缓存值对应的 HA 列表中是否包含当前状态
                        ha_values = mapping.value_mapping.mapping_dict[cached_enum]
                        if isinstance(ha_values, list) and state in ha_values:
                            hw_enum = int(cached_enum)
                            self._update_hw_bridge_status(sn, c_svc_id, {char_name: hw_enum})
                            _LOGGER.debug(f"Applied enum_to_text_multi reverse mapping with cache: '{state}' -> {hw_enum}")
                            return
                    
                    # 无缓存或缓存不匹配，使用默认反向映射
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state)
                    try:
                        hw_enum = int(hw_value)
                        self._update_hw_bridge_status(sn, c_svc_id, {char_name: hw_enum})
                        _LOGGER.debug(f"Applied enum_to_text_multi reverse mapping: '{state}' -> {hw_enum}")
                    except (ValueError, TypeError):
                        _LOGGER.warning(f"Cannot convert mapped value '{hw_value}' to int for {service_id}")
                elif mapping.value_mapping and mapping.value_mapping.mapping_type == "text_to_enum":
                    # select 域 text_to_enum：HA选项文本 → 华为枚举值（如美的电饭煲工作状态）
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state)
                    try:
                        hw_enum = int(hw_value)
                        self._update_hw_bridge_status(sn, c_svc_id, {char_name: hw_enum})
                        _LOGGER.debug(f"Applied text_to_enum mapping (select): '{state}' -> {hw_enum}")

                        # status 变化时同步更新 switch 状态（与 sensor 域逻辑一致）
                        if service_id == "status":
                            is_running = hw_enum in (1, 2, 3)
                            switch_val = 1 if is_running else 0
                            self._update_hw_bridge_status(sn, "switch", {"on": switch_val})
                            _LOGGER.debug(f"Synced switch state based on status: {hw_enum} -> {'on' if is_running else 'off'}")
                            vd = self.sn_manager.get_by_sn(sn)
                            if vd:
                                vd.switch_state = switch_val
                    except (ValueError, TypeError):
                        _LOGGER.warning(f"Cannot convert mapped value '{hw_value}' to int for {service_id}")
                elif mapping.value_mapping and mapping.value_mapping.mapping_type == "delay_to_select":
                    # delay服务反向状态上报
                    self._report_delay_state(sn, c_svc_id, state, mapping)
                else:
                    self._update_hw_bridge_status(sn, c_svc_id, {char_name: self._encode_select_value(state)})

        elif value_attr == "state" and domain == "number":
            # number 类型：上报数值
            try:
                value = float(entity_state.state)
                
                # 检查是否有值映射（反向：number → enum）
                if mapping.value_mapping and mapping.value_mapping.mapping_type == "enum_to_number":
                    vd = self.sn_manager.get_by_sn(sn)
                    cached_enum = vd.last_commanded_enum.get(service_id) if vd else None
                    if cached_enum and mapping.value_mapping.mapping_dict.get(cached_enum) == int(value):
                        hw_value = cached_enum
                    else:
                        hw_value = mapping.value_mapping.transform_ha_to_hw(int(value))
                    self._update_hw_bridge_status(sn, c_svc_id, {char_name: int(hw_value)})
                    _LOGGER.debug(f"Applied enum_to_number reverse mapping: {value} -> {hw_value}")
                else:
                    self._update_hw_bridge_status(sn, c_svc_id, {char_name: int(value)})
            except (ValueError, TypeError):
                _LOGGER.debug(f"Cannot parse number value '{entity_state.state}' for {service_id}")

        elif value_attr == "state" and domain == "button":
            # button 类型：不需要上报状态变化
            pass

        elif value_attr == "brightness":
            # brightness 单独服务（HA 0-255 → 华为 0-100）
            attrs = entity_state.attributes or {}
            if "brightness" in attrs and attrs["brightness"] is not None:
                br = attrs["brightness"]
                brightness_range = mapping.brightness_range or 100
                br_huawei = int(br * brightness_range / 255) if br else 0
                self._update_hw_bridge_status(sn, c_svc_id, {char_name: br_huawei})

        elif service_id == "cct" or (value_attr == "" and domain == "light" and service_id == "cct"):
            # cct 色温服务
            attrs = entity_state.attributes or {}
            if "color_temp_kelvin" in attrs and attrs["color_temp_kelvin"] is not None:
                color_temp = attrs["color_temp_kelvin"]
                self._update_hw_bridge_status(sn, c_svc_id, {char_name: color_temp})

        else:
            _LOGGER.debug(f"Unhandled report type: domain={domain}, "
                           f"value_attr={value_attr}, service_id={service_id}")

    def _report_additional_attributes(self, sn: str, entity_state,
                                       mapping: HAMapping):
        """上报额外的属性（亮度、色温等）

        仅在 light 的 switch 为 ON 时调用。
        上报的 service_id 必须与 C 侧 SvcInfo 中定义的服务一致。
        """
        if not self._lib:
            return

        attrs = entity_state.attributes if entity_state.attributes else {}

        # 亮度 (C 侧 service: "brightness")，HA 0-255 → 华为 0-100
        if "brightness" in attrs and attrs["brightness"] is not None:
            br = attrs["brightness"]
            br_huawei = int(br * 100 / 255) if br else 0
            self._update_hw_bridge_status(sn, "brightness", {"brightness": br_huawei})

        # 色温 (C 侧 service: "cct")
        if "color_temp_kelvin" in attrs and attrs["color_temp_kelvin"] is not None:
            color_temp = attrs["color_temp_kelvin"]
            self._update_hw_bridge_status(sn, "cct", {"colorTemperature": color_temp})

    def _try_report_number(self, sn: str, c_svc_id: str, char_name: str, state_value: str, value_type: str = "int", value_scale: float = None):
        """尝试将状态值解析为数值并上报

        Args:
            sn: 设备 SN
            c_svc_id: C 侧 serviceId（用于调用 _update_hw_bridge_status）
            char_name: profile characteristicName
            state_value: 待解析的状态值字符串
            value_type: 数值类型，"int" 或 "float"，对应 profile characteristicType
            value_scale: 值缩放因子（如 0.001 将 mA 转为 A），None 表示不缩放
        """
        try:
            value = float(state_value)
            if value_scale is not None:
                value = value * value_scale
            if value_type == "float":
                self._update_hw_bridge_status(sn, c_svc_id, {char_name: round(value, 2)})
            else:
                self._update_hw_bridge_status(sn, c_svc_id, {char_name: int(value)})
        except (ValueError, TypeError):
            _LOGGER.debug(f"Cannot parse sensor value '{state_value}' for {c_svc_id}")

    @staticmethod
    def _encode_select_value(option: str) -> int:
        """将 select 选项编码为整数值

        select 选项没有固定的整数映射，这里使用选项字符串的 hash 取模来生成。
        实际效果取决于华为侧的 profile 定义，如果映射不准确，
        可以在 product_registry.json 中添加 value_map 来精确控制。

        Args:
            option: select 选项字符串

        Returns:
            对应的整数值
        """
        return hash(option) & 0x7FFFFFFF  # 确保为正整数

    def _update_hw_bridge_status(self, sn: str, svcid: str, payload):
        """调用 C library 的 UpdateHAStatus 上报状态

        注意：svcid 必须与 C 侧 SvcInfo 中定义的 service ID 一致，
        否则 HiLink SDK 会拒绝该请求。如果传入的 svcid 在 C 侧不存在，
        不会抛出异常，但上报会被忽略。

        Args:
            sn: 设备 SN
            svcid: 服务 ID，必须与 C 侧 SvcInfo 中的 svcId 一致
            payload: JSON格式的payload，可以是dict或str
        """
        if not self._lib:
            return

        # 统一转换为JSON字符串
        if isinstance(payload, dict):
            payload_str = json.dumps(payload)
        elif isinstance(payload, str):
            payload_str = payload
        else:
            _LOGGER.error(f"Invalid payload type: {type(payload)}, expected dict or str")
            return

        try:
            self._lib.UpdateHAStatus(
                self._c_char_p(sn),
                self._c_char_p(svcid),
                self._c_char_p(payload_str)
            )
            _LOGGER.debug(f"UpdateHAStatus called: sn={sn}, svcid={svcid}, payload={payload_str}")
        except Exception as e:
            _LOGGER.error(f"Failed to update HW bridge status: "
                           f"sn={sn}, svcid={svcid}, payload={payload_str}, error={e}")

    def _c_char_p(self, s):
        """将 Python str 转换为 ctypes c_char_p"""
        from ctypes import c_char_p
        return c_char_p(s.encode('utf-8'))

    # -------------------------------------------------------------------
    # 状态查询（供 HilinkGetBrgDevCharState 回调使用）
    # -------------------------------------------------------------------

    def get_char_state(self, sn: str, svc_id: str) -> Optional[str]:
        """获取指定服务的状态，返回 JSON 字符串

        供 C 侧 HilinkGetBrgDevCharState 回调使用。
        通过 VirtualDevice 查找对应 entity 的当前状态，
        根据 ServiceDef 的 char_name 构建返回 JSON。

        支持多 characteristic 服务：当多个 ServiceDef 共享同一个 c_service_id
        （= C 侧 svcId）时，聚合所有匹配 service 的状态为一个完整 JSON。
        例如 electric 服务有 totalConsum/currentVoltage1/currentPower1/currentElectric1
        四个 characteristic，分别对应不同的 Python 侧 service_id。

        Args:
            sn: 设备 SN
            svc_id: C 侧服务 ID（= profile serviceId，如 "switch", "electric"）

        Returns:
            JSON 字符串，如 '{"on":1}' 或 '{"totalConsum":4,"currentVoltage1":220}'
            查询失败返回 None
        """
        if not self._hass:
            return None

        vd = self.sn_manager.get_by_sn(sn)
        if vd is None:
            _LOGGER.debug(f"get_char_state: SN {sn} not found")
            return None

        # 查找所有 c_service_id 或 service_id 匹配 svc_id 的已注册 services
        matched_services = []
        for registered_svc_id in vd.service_entries:
            svc_def = vd.product.services.get(registered_svc_id)
            if svc_def is None:
                continue
            c_id = svc_def.c_service_id or registered_svc_id
            if c_id == svc_id:
                matched_services.append((registered_svc_id, svc_def))

        if not matched_services:
            _LOGGER.debug(f"get_char_state: service '{svc_id}' has no entity for SN {sn}")
            return None

        # 单个匹配：直接构建 JSON（保持原有行为）
        if len(matched_services) == 1:
            reg_svc_id, svc_def = matched_services[0]
            entity_id = vd.get_entity_for_service(reg_svc_id)
            if entity_id is None:
                return None
            state = self._hass.states.get(entity_id)
            if state is None:
                _LOGGER.debug(f"get_char_state: entity {entity_id} has no state")
                return None
            return self._build_char_state_json(reg_svc_id, svc_def, state, entity_id, vd)

        # 多个匹配：聚合所有 characteristic 为一个 JSON
        merged_payload = {}
        for reg_svc_id, svc_def in matched_services:
            entity_id = vd.get_entity_for_service(reg_svc_id)
            if entity_id is None:
                continue
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            partial = self._build_char_state_json(reg_svc_id, svc_def, state, entity_id, vd)
            if partial:
                try:
                    merged_payload.update(json.loads(partial))
                except json.JSONDecodeError:
                    _LOGGER.warning(f"get_char_state: failed to merge partial JSON '{partial}' for {reg_svc_id}")

        if not merged_payload:
            return None

        _LOGGER.debug(f"get_char_state: aggregated {len(matched_services)} services for "
                      f"svcId='{svc_id}', payload={merged_payload}")
        return json.dumps(merged_payload)

    def _build_char_state_json(self, svc_id: str, svc_def: 'ServiceDef',
                                entity_state, entity_id: str, vd=None) -> Optional[str]:
        """根据 service 定义和 entity 状态，构建 HilinkGetBrgDevCharState 所需的 JSON

        核心逻辑：
        - char_name（即 profile characteristicName）作为 JSON key
        - 根据 domain/value_attr 确定如何读取状态值
        - brightness 需要特殊处理（0-255 → 0-100 转换）

        Args:
            svc_id: 服务 ID
            svc_def: 服务定义
            entity_state: HA state 对象
            entity_id: entity ID

        Returns:
            JSON 字符串，如 '{"on":1}'，失败返回 None
        """
        mapping = svc_def.ha_mapping
        if not mapping:
            return None

        char_name = svc_def.char_name
        domain = mapping.domain
        value_attr = mapping.value_attr
        state_value = entity_state.state

        try:
            # --- switch / light / fan: 返回 on/off ---
            if value_attr == "state" and domain in ("switch", "light", "fan"):
                is_on = (state_value == "on")
                return json.dumps({char_name: 1 if is_on else 0})

            # --- brightness: 返回 0-100 ---
            if svc_id == "brightness":
                if state_value == "on":
                    brightness = entity_state.attributes.get('brightness', 0)
                    brightness_huawei = int(brightness * 100 / 255) if brightness else 0
                    return json.dumps({char_name: brightness_huawei})
                else:
                    return json.dumps({char_name: 0})

            # --- cct: 色温，返回 color_temp_kelvin ---
            if svc_id == "cct":
                attrs = entity_state.attributes or {}
                color_temp = attrs.get("color_temp_kelvin") or 0
                return json.dumps({char_name: color_temp})

            # --- sensor / binary_sensor: 状态类，返回枚举值或原始数值 ---
            if domain in ("sensor", "binary_sensor"):
                if mapping.value_mapping:
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state_value)
                    return json.dumps({char_name: int(hw_value)})
                # 无 value_mapping 的 sensor，尝试解析为数值
                try:
                    num_value = float(state_value)
                    if mapping.value_scale is not None:
                        num_value = num_value * mapping.value_scale
                    if svc_def.value_type == "float":
                        return json.dumps({char_name: round(num_value, 2)})
                    return json.dumps({char_name: int(num_value)})
                except (ValueError, TypeError):
                    return json.dumps({char_name: 0})

            # --- select: 返回选项对应的枚举值 ---
            if domain == "select":
                if not state_value or state_value in ("unavailable", "unknown"):
                    return json.dumps({char_name: 0})
                if mapping.value_mapping and mapping.value_mapping.mapping_type == "enum_to_text_multi":
                    # 优先使用缓存的华为枚举值
                    cached_enum = vd.last_commanded_enum.get(svc_id) if vd else None
                    if cached_enum and cached_enum in mapping.value_mapping.mapping_dict:
                        # 检查缓存值对应的 HA 列表中是否包含当前状态
                        ha_values = mapping.value_mapping.mapping_dict[cached_enum]
                        if isinstance(ha_values, list) and state_value in ha_values:
                            hw_enum = int(cached_enum)
                            _LOGGER.debug(f"build_char_state_json: using cached enum {hw_enum} for state '{state_value}'")
                            return json.dumps({char_name: hw_enum})
                    
                    # 无缓存或缓存不匹配，使用默认反向映射
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state_value)
                    try:
                        return json.dumps({char_name: int(hw_value)})
                    except (ValueError, TypeError):
                        _LOGGER.warning(f"build_char_state_json: cannot convert '{hw_value}' to int for {svc_id}, state='{state_value}'")
                        return json.dumps({char_name: 0})
                elif mapping.value_mapping:
                    hw_value = mapping.value_mapping.transform_ha_to_hw(state_value)
                    try:
                        return json.dumps({char_name: int(hw_value)})
                    except (ValueError, TypeError):
                        _LOGGER.warning(f"build_char_state_json: cannot convert '{hw_value}' to int for {svc_id}, state='{state_value}'")
                        return json.dumps({char_name: 0})
                return json.dumps({char_name: 0})

            # --- number: 返回数值 ---
            if domain == "number":
                try:
                    value = int(float(state_value))
                    if mapping.value_mapping and mapping.value_mapping.mapping_type == "enum_to_number":
                        cached_enum = vd.last_commanded_enum.get(svc_id) if vd else None
                        if cached_enum and mapping.value_mapping.mapping_dict.get(cached_enum) == value:
                            hw_value = cached_enum
                        else:
                            hw_value = mapping.value_mapping.transform_ha_to_hw(value)
                        return json.dumps({char_name: int(hw_value)})
                    return json.dumps({char_name: value})
                except (ValueError, TypeError):
                    return json.dumps({char_name: 0})

            # --- button: 不需要状态查询，但 switch 服务需要返回缓存的开关状态 ---
            if domain == "button":
                if svc_id == "switch" and vd is not None:
                    return json.dumps({char_name: vd.switch_state})
                return None

            # --- 默认兜底 ---
            try:
                return json.dumps({char_name: int(float(state_value))})
            except (ValueError, TypeError):
                return json.dumps({char_name: 0})

        except Exception as e:
            _LOGGER.error(f"build_char_state_json error for {entity_id}, "
                          f"svc={svc_id}: {e}")
            return None

    # -------------------------------------------------------------------
    # 设备注册/注销（HiLink 侧）
    # -------------------------------------------------------------------

    def unregister_device(self, device_id: str) -> Optional[VirtualDevice]:
        """注销设备：上报离线并清理 VirtualDevice

        Args:
            device_id: HA 的 device_id

        Returns:
            被注销的 VirtualDevice，如果不存在返回 None
        """
        vd = self.sn_manager.get_by_device_id(device_id)
        if vd is None:
            _LOGGER.debug(f"Device {device_id} not registered, skip unregister")
            return None

        sn = vd.sn
        _LOGGER.info(f"Unregistering device: sn={sn}, device_id={device_id}")

        # 设备恢复出厂，删除云测信息 (status=2)
        self.register_device_to_hilink(sn, 2)

        # 从 SN 管理器中移除
        self.sn_manager.unregister(sn)

        _LOGGER.info(f"Device unregistered: sn={sn}, device_id={device_id}")
        return vd

    def register_device_to_hilink(self, sn: str, status: int) -> bool:
        """将设备注册到 HiLink（调用 C library 的 HilinkSyncBrgDevStatus）

        Args:
            sn: 设备 SN
            status: 1=在线(已知设备), 3=新设备

        Returns:
            True 表示调用成功
        """
        if not self._lib:
            return False

        try:
            rlt = self._lib.HilinkSyncBrgDevStatus(self._c_char_p(sn), status)
            _LOGGER.info(f"HilinkSyncBrgDevStatus({sn}, {status}) = {rlt}")
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to call HilinkSyncBrgDevStatus: {e}")
            return False

    def sync_device_state(self, vd: VirtualDevice) -> bool:
        """同步设备的完整状态到 HiLink

        Args:
            vd: VirtualDevice

        Returns:
            True 表示同步成功
        """
        # 上报每个 service 的状态
        for service_id, entry in vd.service_entries.items():
            # 获取 entity 的当前状态
            if self._hass:
                state = self._hass.states.get(entry.entity_id)
                if state:
                    self.report_state(vd.sn, entry.entity_id, state)
        return True

    def _report_delay_state(self, sn: str, c_svc_id: str, state: str, mapping: HAMapping):
        """上报delay服务的完整状态
        
        Args:
            sn: 设备SN
            c_svc_id: C 侧 serviceId（=profile serviceId）
            state: select实体的状态（如"3小时"）
            mapping: HAMapping定义
        """
        if not self._lib:
            return
        
        # 从select状态反向查找小时数
        hours = None
        for hour_str, option_text in mapping.value_mapping.mapping_dict.items():
            if option_text == state:
                try:
                    hours = int(hour_str)
                except ValueError:
                    continue
                break
        
        if hours is None:
            _LOGGER.warning(f"Cannot find hours mapping for select state '{state}'")
            return
        
        # 特殊处理：关闭倒计时（hours=0）
        if hours == 0:
            delay_payload = {
                "delay": [{}],
                "action": 2,  # 删除
                "num": 0
            }
            self._update_hw_bridge_status(sn, c_svc_id, delay_payload)
            _LOGGER.info(f"Reported delay close state: select '{state}' -> action=2 (delete)")
            return
        
        # 计算UTC结束时间
        now = datetime.utcnow()
        end_time = now + timedelta(hours=hours)
        end_time_str = end_time.strftime("%Y%m%dT%H%M%SZ")
        
        _LOGGER.info(f"Reporting delay state: select '{state}' -> {hours}h, end_time={end_time_str}")
        
        # 上报完整的delay payload
        delay_payload = {
            "delay": [{
                "enable": 1,
                "end": end_time_str,
                "para": "on",
                "sid": "switch"
            }],
            "action": 0,
            "num": 1
        }
        
        self._update_hw_bridge_status(sn, c_svc_id, delay_payload)
        _LOGGER.info(f"Reported delay state: {delay_payload}")
