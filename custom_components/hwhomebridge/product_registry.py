"""
Product Registry - 产品定义注册表（框架层 + 默认适配器 + 厂家适配器）

框架层（config/product_registry.json）：
  - 华为侧产品定义：PID, service_type, char_name
  - auto_match 规则：标准品类的自动兜底匹配（灯/开关等零配置接入）

默认适配器层（config/adapters/default/*.json）：
  - 华为标准 HA 映射：domain, action, value_mapping 等（从华为 profile 提取）
  - 无 match_rules，不参与匹配，只被厂家适配器继承

厂家适配器层（config/adapters/<vendor>/*.json）：
  - match_rules：厂家/型号识别规则
  - 可选 service 覆盖：只写与默认适配器不同的字段

合并规则：厂家适配器字段覆盖默认适配器（非 None 字段覆盖）
"""

import json
import os
import copy
import logging
import aiofiles
from dataclasses import dataclass, field
from typing import Optional

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action + Domain 兼容性验证
# ---------------------------------------------------------------------------

VALID_ACTION_DOMAIN_MAP = {
    "turn_on_off": ["switch", "light", "fan", "select"],
    "set_brightness": ["light"],
    "set_color_temp": ["light"],
    "set_color": ["light"],
    "set_speed": ["fan"],
    "set_temperature": ["climate", "water_heater"],
    "press": ["button"],
    "set_option": ["select", "input_select"],
    "set_value": ["number", "input_number"],
    "read_only": ["sensor", "binary_sensor", "number", "input_number", "select"],
}


def validate_action_domain(action: Optional[str], domain: Optional[str]) -> bool:
    """验证 action 和 domain 的兼容性

    Args:
        action: 控制动作
        domain: HA domain

    Returns:
        True 表示兼容，False 表示不兼容
    """
    if not action or not domain:
        return True

    valid_domains = VALID_ACTION_DOMAIN_MAP.get(action)
    if not valid_domains:
        _LOGGER.warning(f"Unknown action '{action}', skip validation")
        return True

    if domain not in valid_domains:
        _LOGGER.error(
            f"Invalid action+domain combination: action='{action}', domain='{domain}'. "
            f"Valid domains for '{action}': {valid_domains}"
        )
        return False

    return True


# ---------------------------------------------------------------------------
# 数据模型：值映射
# ---------------------------------------------------------------------------

@dataclass
class ValueMapping:
    """值映射定义：用于华为侧与HA侧之间的值转换

    支持以下映射类型：
    1. enum_to_number: 华为枚举值 → HA数值（如：mode枚举→温度数值）
    2. text_to_enum: HA文本状态 → 华为枚举值（如：sensor/select状态→status枚举）
    3. enum_to_text_multi: 华为枚举值 → 多个可能的HA文本值（用于select，支持多插件适配）
    4. number_to_enum_multi: HA数值 → 华为枚举值（支持多值匹配，用于sensor状态）
    5. seconds_to_minutes: HA秒数 → 华为分钟数（用于timer，通过divide_by除法转换）
    6. delay_to_select: 华为delay服务 → HA select选项（用于灭蚊器定时等）
    """
    mapping_type: str = ""
    mapping_dict: dict = field(default_factory=dict)

    def transform_hw_to_ha(self, value):
        """华为值 → HA值（控制命令时使用）

        用于enum_to_number类型：华为枚举 → HA数值
        用于enum_to_text_multi类型：返回可能值列表，需在service_action中匹配
        """
        str_value = str(value)

        if self.mapping_type == "enum_to_text_multi":
            possible_values = self.mapping_dict.get(str_value, [])
            return possible_values if possible_values else [str_value]

        return self.mapping_dict.get(str_value, value)

    def transform_ha_to_hw(self, value):
        """HA值 → 华为值（状态上报时使用）

        对于text_to_enum：直接查找mapping_dict（HA文本 → 华为枚举）
        对于enum_to_number：反向查找（HA数值 → 华为枚举）
        对于enum_to_text_multi：多值反向查找（HA文本 → 华为枚举），支持关键词匹配
        对于number_to_enum_multi：多值反向查找（HA数值 → 华为枚举），支持关键词匹配
        对于seconds_to_minutes：秒数除以divide_by得到分钟数

        注意：对于 *_multi 类型，使用关键词匹配（包含匹配），即配置的关键词是实际状态的子串即可匹配
        """
        str_value = str(value)

        if self.mapping_type == "text_to_enum":
            return self.mapping_dict.get(str_value, value)

        if self.mapping_type == "enum_to_number":
            for k, v in self.mapping_dict.items():
                if str(v) == str_value:
                    return k
            return value

        if self.mapping_type in ("enum_to_text_multi", "number_to_enum_multi"):
            for hw_val, ha_values in self.mapping_dict.items():
                if isinstance(ha_values, list):
                    for ha_val in ha_values:
                        # 关键词匹配：配置的值是实际状态的子串即可匹配
                        if str(ha_val) in str_value:
                            return hw_val
                elif str(ha_values) in str_value:
                    return hw_val
            return value

        if self.mapping_type == "seconds_to_minutes":
            divide_by = self.mapping_dict.get("divide_by", 60)
            try:
                return int(float(value) / divide_by)
            except (ValueError, TypeError):
                return value

        return value


# ---------------------------------------------------------------------------
# 数据模型：HA 映射
# ---------------------------------------------------------------------------

@dataclass
class HAMapping:
    """描述一个 Huawei service 如何映射到 HA entity

    框架层提供默认值，适配器层可覆盖任意字段。
    """
    domain: str = ""                         # HA domain，如 "switch", "sensor", "light"
    action: Optional[str] = None             # 控制动作，如 "turn_on_off", "set_brightness"
    value_attr: Optional[str] = None         # 状态属性，如 "state", "brightness"
    device_class: Optional[str] = None       # HA device_class，如 "temperature"
    name_keywords: Optional[list] = None     # entity name 关键词列表，评分匹配多个同 domain entity
    exclude_keywords: Optional[list] = None  # 排除关键词（实体名/id 包含这些词的实体被排除）
    include_keywords: Optional[list] = None  # 包含关键词（实体名/id 必须包含至少一个才保留）
    value_scale: Optional[float] = None      # 值缩放因子
    value_range: Optional[tuple] = None      # 值范围 (min, max)
    brightness_range: Optional[int] = None   # 亮度范围上限（灯光专用）
    color_temperature_range: Optional[int] = None  # 色温范围上限（灯光专用，Kelvin）
    color_temperature_min: Optional[int] = None      # 色温范围下限（灯光专用，Kelvin）
    value_mapping: Optional[ValueMapping] = None   # 值映射（用于复杂转换场景）
    # 以下为适配器层新增字段
    stop_option: Optional[str] = None        # select域 turn_on_off off 时选择的选项（如 "停止"）
    on_command: Optional[dict] = None        # on=1 时的声明式控制覆盖（如触发 cooker 服务）
    default_mode: Optional[str] = None       # 默认模式（用于 cooker 等需要默认值的服务）


# ---------------------------------------------------------------------------
# 数据模型：Service 定义
# ---------------------------------------------------------------------------

@dataclass
class ServiceDef:
    """华为产品的 service 定义，以及与 HA entity 的映射关系

    service_type 和 char_name 由框架层提供（华为侧定义，不变）。
    ha_mapping 由框架层提供默认值，适配器层可覆盖。
    c_service_id 用于多 characteristic 服务场景：当华为侧一个 serviceId 下有多个
    characteristic 时，框架层拆分为多个 service_def，各自有独立的 service_id 和
    char_name，但共享同一个 c_service_id（=华为侧 profile 的 serviceId）。
    上报和查询时使用 c_service_id 调用 C 侧接口，确保与 C 侧 SvcInfo 一致。
    value_type 对应 profile 的 characteristicType（"int"/"float"），决定上报时
    数值是 int 还是 float，默认 "int"。
    """
    service_id: str                          # service ID，如 "switch"，Python 侧逻辑标识
    service_type: str                        # service type，如 "switch"
    char_name: Optional[str] = None          # profile 中的 characteristicName，用于构建上报payload的key
    c_service_id: Optional[str] = None       # C 侧 serviceId（=profile serviceId），为 None 时等于 service_id
    value_type: Optional[str] = None         # profile characteristicType，如 "int"/"float"，默认 "int"
    ha_mapping: HAMapping = field(default_factory=HAMapping)


# ---------------------------------------------------------------------------
# 数据模型：产品识别规则
# ---------------------------------------------------------------------------

@dataclass
class MatchRule:
    """识别规则基类"""
    rule_type: str = ""


@dataclass
class ModelExactMatch(MatchRule):
    """model 精确匹配：entity.device_entry.model 与 model 值完全一致"""
    rule_type: str = "model_exact"
    model: str = ""


@dataclass
class ModelKeywordMatch(MatchRule):
    """model 关键词匹配：device.model 中包含指定关键词

    用途：
    - 匹配同一产品线的多个型号（如 xiaomi.kettle.* 系列产品）
    - 支持多个关键词，任意一个匹配即可

    示例：
    {"type": "model_keyword", "keywords": ["kettle", "ym3"]}
    可匹配 xiaomi.kettle.ym3pro、xiaomi.kettle.ym3s 等
    """
    rule_type: str = "model_keyword"
    keywords: list = field(default_factory=list)


@dataclass
class NameKeywordMatch(MatchRule):
    """device name 关键词匹配：device name 中包含指定关键词"""
    rule_type: str = "name_keyword"
    keywords: list = field(default_factory=list)


@dataclass
class EntityCompositionMatch(MatchRule):
    """entity 组合匹配：同一 device 下的 entity domain 组合满足要求"""
    rule_type: str = "entity_composition"
    required_domains: list = field(default_factory=list)
    sensor_classes: Optional[list] = None


# ---------------------------------------------------------------------------
# 数据模型：产品定义（框架层）
# ---------------------------------------------------------------------------

@dataclass
class ProductDef:
    """华为产品定义（框架层）

    包含华为侧定义（PID, service_type, char_name）和默认 HA 映射。
    标准品类可定义 auto_match 实现零配置接入。
    复杂品类的 ha_mapping 由适配器层提供覆盖。
    """
    pid: str                                      # 华为产品ID，如 "001"
    name: str = ""                                # 产品名称，如 "床头灯"
    auto_match: Optional[dict] = None             # 自动匹配规则 {required_domains, name_keywords}
    services: dict = field(default_factory=dict)  # dict[str, ServiceDef]


# ---------------------------------------------------------------------------
# 数据模型：适配器定义（适配器层）
# ---------------------------------------------------------------------------

@dataclass
class AdapterDef:
    """适配器定义（适配器层）

    引用框架层的 PID，提供 match_rules 和可选的 service 覆盖。
    services 中的值是 ha_mapping 字段的 dict（未解析为 HAMapping，在 merge 时合并）。
    """
    pid: str                                      # 引用框架层 ProductDef 的 PID
    match_rules: list = field(default_factory=list)  # list[MatchRule]
    services: dict = field(default_factory=dict)     # service_id -> dict of ha_mapping fields


# ---------------------------------------------------------------------------
# 产品注册表
# ---------------------------------------------------------------------------

class ProductRegistry:
    """产品注册表：加载框架层 + 适配器层，提供合并查询

    框架层（product_registry.json）：PID → ProductDef（含默认 ha_mapping + auto_match）
    适配器层（adapters/<vendor>/*.json）：list[AdapterDef]（含 match_rules + 可选覆盖）

    匹配流程：
    1. 遍历适配器的 match_rules 精确匹配
    2. 无适配器匹配时，遍历框架的 auto_match 兜底
    """

    def __init__(self):
        self._products: dict[str, ProductDef] = {}   # 框架层: pid -> ProductDef
        self._defaults: dict[str, AdapterDef] = {}    # 默认适配器层: pid -> AdapterDef
        self._adapters: list[AdapterDef] = []         # 厂家适配器层: all adapters

    async def load(self, framework_path: str, adapters_dir: str) -> bool:
        """加载框架层和适配器层配置（异步）

        Args:
            framework_path: product_registry.json 的路径
            adapters_dir: adapters 目录的路径

        Returns:
            True 表示框架层加载成功（适配器层失败不阻塞）
        """
        fw_ok = await self._load_framework(framework_path)
        ad_ok = await self._load_adapters(adapters_dir)
        return fw_ok

    async def _load_framework(self, file_path: str) -> bool:
        """加载框架层 JSON"""
        if not os.path.exists(file_path):
            _LOGGER.error(f"Product registry file not found: {file_path}")
            return False

        try:
            async with aiofiles.open(file_path, 'r', encoding='utf-8') as f:
                content = await f.read()
                data = json.loads(content)
        except (json.JSONDecodeError, IOError) as e:
            _LOGGER.error(f"Failed to load product registry: {e}")
            return False

        if not isinstance(data, dict) or 'products' not in data:
            _LOGGER.error("Invalid product registry format: missing 'products' key")
            return False

        return self._parse_products(data)

    def _parse_products(self, data: dict) -> bool:
        """解析框架层产品定义"""
        products_data = data.get('products', [])
        if not isinstance(products_data, list):
            _LOGGER.error("Invalid 'products' format: expected a list")
            return False

        success_count = 0
        for product_data in products_data:
            try:
                product = self._parse_product(product_data)
                if product is not None:
                    self._products[product.pid] = product
                    success_count += 1
            except Exception as e:
                _LOGGER.error(f"Failed to parse product definition: {e}")
                continue

        _LOGGER.info(f"Loaded {success_count} product definitions, "
                      f"total {len(self._products)} products")
        return success_count > 0

    def _parse_product(self, data: dict) -> Optional[ProductDef]:
        """解析单个框架层产品定义"""
        pid = data.get('pid')
        if not pid:
            _LOGGER.warning("Product definition missing 'pid', skipping")
            return None

        product = ProductDef(pid=pid)
        product.name = data.get('name', '')
        product.auto_match = data.get('auto_match')

        # 解析 service 定义（含默认 ha_mapping）
        services_data = data.get('services', {})
        for svc_id, svc_data in services_data.items():
            service = self._parse_service(svc_id, svc_data)
            if service is not None:
                product.services[svc_id] = service

        return product

    def _parse_service(self, svc_id: str, data: dict) -> Optional[ServiceDef]:
        """解析框架层 service 定义（含 service_type, char_name, 默认 ha_mapping）"""
        service = ServiceDef(
            service_id=svc_id,
            service_type=data.get('service_type', svc_id),
            char_name=data.get('char_name'),
            c_service_id=data.get('c_service_id'),
            value_type=data.get('value_type')
        )

        mapping_data = data.get('ha_mapping')
        if mapping_data and isinstance(mapping_data, dict):
            service.ha_mapping = self._parse_ha_mapping(mapping_data)

        return service

    def _parse_ha_mapping(self, mapping_data: dict) -> HAMapping:
        """从 dict 解析 HAMapping（框架层和适配器层共用）"""
        # 解析 value_mapping
        value_mapping = None
        vm_data = mapping_data.get('value_mapping')
        if vm_data and isinstance(vm_data, dict):
            value_mapping = ValueMapping(
                mapping_type=vm_data.get('type', ''),
                mapping_dict=vm_data.get('mapping', {})
            )

        mapping = HAMapping(
            domain=mapping_data.get('domain', ''),
            action=mapping_data.get('action'),
            value_attr=mapping_data.get('value_attr'),
            device_class=mapping_data.get('device_class'),
            name_keywords=mapping_data.get('name_keywords'),
            exclude_keywords=mapping_data.get('exclude_keywords'),
            include_keywords=mapping_data.get('include_keywords'),
            value_scale=mapping_data.get('value_scale'),
            value_range=tuple(mapping_data['value_range']) if 'value_range' in mapping_data else None,
            brightness_range=mapping_data.get('brightness_range'),
            color_temperature_range=mapping_data.get('colorTemperature_range'),
            color_temperature_min=mapping_data.get('colorTemperature_min'),
            value_mapping=value_mapping,
            stop_option=mapping_data.get('stop_option'),
            on_command=mapping_data.get('on_command'),
            default_mode=mapping_data.get('default_mode'),
        )

        # 验证 action + domain
        if not validate_action_domain(mapping.action, mapping.domain):
            _LOGGER.warning(
                f"Invalid action+domain combination: "
                f"action='{mapping.action}', domain='{mapping.domain}'. This may cause runtime errors."
            )

        return mapping

    async def _load_adapters(self, adapters_dir: str) -> bool:
        """递归扫描适配器目录，加载所有 .json 文件

        目录结构：
        - adapters/default/*.json → 默认适配器（无 match_rules，提供标准 ha_mapping）
        - adapters/<vendor>/*.json → 厂家适配器（有 match_rules + 可选覆盖）
        """
        if not os.path.exists(adapters_dir):
            _LOGGER.info(f"Adapters directory not found: {adapters_dir}, skipping")
            return True

        default_dir = os.path.join(adapters_dir, 'default')
        vendor_count = 0
        default_count = 0

        for root, dirs, files in os.walk(adapters_dir):
            for filename in sorted(files):
                if not filename.endswith('.json'):
                    continue
                filepath = os.path.join(root, filename)
                try:
                    async with aiofiles.open(filepath, 'r', encoding='utf-8') as f:
                        content = await f.read()
                        data = json.loads(content)

                    adapter = self._parse_adapter(data, filepath)
                    if adapter is not None:
                        # 判断是 default 还是 vendor
                        if os.path.dirname(filepath) == default_dir:
                            self._defaults[adapter.pid] = adapter
                            default_count += 1
                        else:
                            self._adapters.append(adapter)
                            vendor_count += 1
                except (json.JSONDecodeError, IOError) as e:
                    _LOGGER.error(f"Failed to load adapter {filepath}: {e}")

        _LOGGER.info(f"Loaded {default_count} default adapters, "
                      f"{vendor_count} vendor adapters from {adapters_dir}")
        return True

    def _parse_adapter(self, data: dict, filepath: str = "") -> Optional[AdapterDef]:
        """解析适配器 JSON"""
        pid = data.get('pid')
        if not pid:
            _LOGGER.warning(f"Adapter {filepath} missing 'pid', skipping")
            return None

        # 检查 PID 是否在框架层存在
        if pid not in self._products:
            _LOGGER.warning(f"Adapter {filepath}: PID '{pid}' not found in framework, skipping")
            return None

        adapter = AdapterDef(pid=pid)

        # 解析匹配规则
        rules_data = data.get('match_rules', [])
        for rule_data in rules_data:
            rule = self._parse_match_rule(rule_data)
            if rule is not None:
                adapter.match_rules.append(rule)

        # services 保持为 raw dict（在 merge 时合并到框架的 HAMapping）
        services_data = data.get('services', {})
        if isinstance(services_data, dict):
            adapter.services = services_data

        return adapter

    def _parse_match_rule(self, data: dict) -> Optional[MatchRule]:
        """解析识别规则"""
        rule_type = data.get('type', '')
        if rule_type == 'model_exact':
            return ModelExactMatch(model=data.get('model', ''))
        elif rule_type == 'model_keyword':
            return ModelKeywordMatch(keywords=data.get('keywords', []))
        elif rule_type == 'name_keyword':
            return NameKeywordMatch(keywords=data.get('keywords', []))
        elif rule_type == 'entity_composition':
            return EntityCompositionMatch(
                required_domains=data.get('required_domains', []),
                sensor_classes=data.get('sensor_classes')
            )
        else:
            _LOGGER.warning(f"Unknown match rule type: {rule_type}")
            return None

    # -------------------------------------------------------------------
    # 合并逻辑
    # -------------------------------------------------------------------

    def merge(self, product: ProductDef, adapter: Optional[AdapterDef] = None) -> ProductDef:
        """合并框架层 + 默认适配器 + 厂家适配器

        三层合并：
        1. 框架层提供 service_type 和 char_name（华为侧定义，不变）
        2. 默认适配器提供标准 ha_mapping（从华为 profile 提取）
        3. 厂家适配器覆盖默认 ha_mapping 的差异字段

        Args:
            product: 框架层 ProductDef
            adapter: 厂家适配器 AdapterDef（可选，None 时只用 default）

        Returns:
            合并后的 ProductDef
        """
        pid = product.pid
        merged = ProductDef(
            pid=pid,
            name=product.name,
            auto_match=product.auto_match,
        )

        default_adapter = self._defaults.get(pid)

        for svc_id, fw_svc in product.services.items():
            # 第一层：从默认适配器获取基础 ha_mapping
            base_mapping = HAMapping()
            if default_adapter:
                def_svc_data = default_adapter.services.get(svc_id)
                if def_svc_data and isinstance(def_svc_data, dict):
                    base_mapping = self._parse_ha_mapping(def_svc_data)

            # 第二层：厂家适配器覆盖
            if adapter:
                ad_svc_data = adapter.services.get(svc_id)
                if ad_svc_data and isinstance(ad_svc_data, dict):
                    merged_mapping = self._merge_ha_mapping(base_mapping, ad_svc_data)
                else:
                    merged_mapping = base_mapping
            else:
                merged_mapping = base_mapping

            merged_svc = ServiceDef(
                service_id=svc_id,
                service_type=fw_svc.service_type,
                char_name=fw_svc.char_name,
                c_service_id=fw_svc.c_service_id,
                value_type=fw_svc.value_type,
                ha_mapping=merged_mapping
            )
            merged.services[svc_id] = merged_svc

        # 处理适配器中有但框架中没有的 service（兜底）
        if adapter:
            for svc_id, svc_data in adapter.services.items():
                if svc_id not in merged.services and isinstance(svc_data, dict):
                    _LOGGER.warning(f"Adapter has service '{svc_id}' not in framework for PID {adapter.pid}")
                    mapping = self._parse_ha_mapping(svc_data)
                    merged.services[svc_id] = ServiceDef(
                        service_id=svc_id,
                        service_type=svc_data.get('service_type', svc_id),
                        char_name=svc_data.get('char_name'),
                        c_service_id=svc_data.get('c_service_id'),
                        value_type=svc_data.get('value_type'),
                        ha_mapping=mapping
                    )

        return merged

    def _merge_ha_mapping(self, base: HAMapping, override: dict) -> HAMapping:
        """将适配器覆盖字段合并到框架默认 HAMapping

        规则：override 中存在的字段覆盖 base，不存在的保留 base。
        """
        # 解析 value_mapping
        value_mapping = base.value_mapping
        vm_data = override.get('value_mapping')
        if vm_data and isinstance(vm_data, dict):
            value_mapping = ValueMapping(
                mapping_type=vm_data.get('type', ''),
                mapping_dict=vm_data.get('mapping', {})
            )

        mapping = HAMapping(
            domain=override.get('domain', base.domain),
            action=override.get('action', base.action),
            value_attr=override.get('value_attr', base.value_attr),
            device_class=override.get('device_class', base.device_class),
            name_keywords=override.get('name_keywords', base.name_keywords),
            exclude_keywords=override.get('exclude_keywords', base.exclude_keywords),
            include_keywords=override.get('include_keywords', base.include_keywords),
            value_scale=override.get('value_scale', base.value_scale),
            value_range=tuple(override['value_range']) if 'value_range' in override else base.value_range,
            brightness_range=override.get('brightness_range', base.brightness_range),
            color_temperature_range=override.get('colorTemperature_range', base.color_temperature_range),
            color_temperature_min=override.get('colorTemperature_min', base.color_temperature_min),
            value_mapping=value_mapping,
            stop_option=override.get('stop_option', base.stop_option),
            on_command=override.get('on_command', base.on_command),
            default_mode=override.get('default_mode', base.default_mode),
        )

        # 验证合并后的 action + domain
        if not validate_action_domain(mapping.action, mapping.domain):
            _LOGGER.warning(
                f"Merged ha_mapping has invalid action+domain: "
                f"action='{mapping.action}', domain='{mapping.domain}'"
            )

        return mapping

    # -------------------------------------------------------------------
    # 查询接口
    # -------------------------------------------------------------------

    def get_product(self, pid: str) -> Optional[ProductDef]:
        """根据 PID 获取框架层产品定义"""
        return self._products.get(pid)

    def get_all_products(self) -> dict:
        """获取所有框架层产品定义（用于 auto_match 兜底）"""
        return dict(self._products)

    def get_all_adapters(self) -> list:
        """获取所有厂家适配器（用于 match_rules 匹配，不含 default）"""
        return list(self._adapters)

    def get_default_adapter(self, pid: str) -> Optional[AdapterDef]:
        """获取指定 PID 的默认适配器"""
        return self._defaults.get(pid)

    def get_product_count(self) -> int:
        """获取框架层产品数量"""
        return len(self._products)

    def get_adapter_count(self) -> int:
        """获取厂家适配器数量（不含 default）"""
        return len(self._adapters)

    def get_default_count(self) -> int:
        """获取默认适配器数量"""
        return len(self._defaults)

    def list_product_ids(self) -> list:
        """列出所有产品 PID"""
        return list(self._products.keys())
