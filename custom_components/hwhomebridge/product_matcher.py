"""
Product Matcher - 产品识别引擎

负责将 HA 中的 device 匹配到华为产品定义。

匹配流程（按优先级）：
1. 适配器 match_rules 匹配（model_exact → model_keyword → name_keyword → entity_composition）
   - 适配器定义在 config/adapters/<vendor>/*.json
   - 匹配成功后，合并框架默认 ha_mapping + 适配器覆盖字段
2. 框架 auto_match 兜底（标准品类零配置接入）
   - 框架定义在 config/product_registry.json 的 auto_match 字段
   - 根据 entity domain 组合 + 可选 name_keywords 自动匹配
3. 都不匹配 → 跳过该设备
"""

import logging
from dataclasses import dataclass
from typing import Optional

from .product_registry import (
    ProductRegistry, ProductDef, AdapterDef,
    ModelExactMatch, ModelKeywordMatch, NameKeywordMatch, EntityCompositionMatch
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceInfo:
    """HA device 的信息摘要，用于产品识别"""
    device_id: str
    name: str = ""
    model: str = ""
    manufacturer: str = ""
    entity_domains: list = None       # 该 device 下所有 entity 的 domain 列表
    entity_device_classes: dict = None  # {domain: device_class}

    def __post_init__(self):
        if self.entity_domains is None:
            self.entity_domains = []
        if self.entity_device_classes is None:
            self.entity_device_classes = {}


class ProductMatcher:
    """产品识别引擎：将 HA device 匹配到华为产品定义

    三级匹配策略：
    1. 适配器 match_rules（按规则类型优先级：model_exact > model_keyword > name_keyword > entity_composition）
    2. 框架 auto_match 兜底
    3. 不匹配 → 跳过
    """

    def __init__(self, registry: ProductRegistry):
        self.registry = registry

    def match(self, device_info: DeviceInfo) -> Optional[ProductDef]:
        """匹配 HA device 到华为产品定义

        Args:
            device_info: HA device 信息

        Returns:
            匹配成功时返回 ProductDef，否则返回 None
        """
        product, _ = self.match_with_detail(device_info)
        return product

    def match_with_detail(self, device_info: DeviceInfo) -> tuple:
        """匹配并返回详细信息

        Returns:
            (ProductDef, match_strategy) 匹配成功
            (None, None) 匹配失败
        """
        if not device_info.model and not device_info.name and not device_info.entity_domains:
            return None, None

        # Phase 1: 适配器 match_rules 匹配（按规则类型优先级）
        # 1a. model_exact
        result = self._match_adapters_by_rule_type(device_info, ModelExactMatch)
        if result is not None:
            product, strategy = result
            _LOGGER.info(f"Device {device_info.device_id} matched by model_exact: "
                         f"{product.pid} ({product.name})")
            return product, strategy

        # 1b. model_keyword
        result = self._match_adapters_by_rule_type(device_info, ModelKeywordMatch)
        if result is not None:
            product, strategy = result
            _LOGGER.info(f"Device {device_info.device_id} matched by model_keyword: "
                         f"{product.pid} ({product.name})")
            return product, strategy

        # 1c. name_keyword
        result = self._match_adapters_by_rule_type(device_info, NameKeywordMatch)
        if result is not None:
            product, strategy = result
            _LOGGER.info(f"Device {device_info.device_id} matched by name_keyword: "
                         f"{product.pid} ({product.name})")
            return product, strategy

        # 1d. entity_composition
        result = self._match_adapters_by_rule_type(device_info, EntityCompositionMatch)
        if result is not None:
            product, strategy = result
            _LOGGER.info(f"Device {device_info.device_id} matched by entity_composition: "
                         f"{product.pid} ({product.name})")
            return product, strategy

        # Phase 2: 框架 auto_match 兜底
        result = self._match_auto(device_info)
        if result is not None:
            product, strategy = result
            _LOGGER.info(f"Device {device_info.device_id} matched by auto_match: "
                         f"{product.pid} ({product.name})")
            return product, strategy

        _LOGGER.debug(f"Device {device_info.device_id} (model={device_info.model}, "
                       f"name={device_info.name}) no match found")
        return None, None

    def _match_adapters_by_rule_type(self, device_info: DeviceInfo,
                                      rule_type: type) -> Optional[tuple]:
        """遍历所有适配器，尝试指定规则类型的匹配

        Args:
            device_info: 设备信息
            rule_type: MatchRule 子类类型

        Returns:
            (merged ProductDef, strategy_str) 或 None
        """
        for adapter in self.registry.get_all_adapters():
            for rule in adapter.match_rules:
                if not isinstance(rule, rule_type):
                    continue
                if self._check_rule(rule, device_info):
                    product = self.registry.get_product(adapter.pid)
                    if product is None:
                        _LOGGER.warning(f"Adapter PID '{adapter.pid}' not found in framework")
                        continue
                    merged = self.registry.merge(product, adapter)
                    return merged, rule.rule_type
        return None

    def _check_rule(self, rule, device_info: DeviceInfo) -> bool:
        """检查单条匹配规则是否满足"""
        if isinstance(rule, ModelExactMatch):
            if not rule.model:
                return False
            if device_info.model == rule.model:
                _LOGGER.debug(f"ModelExactMatch: model={device_info.model}")
                return True
            return False

        if isinstance(rule, ModelKeywordMatch):
            if not rule.keywords:
                return False
            device_model = (device_info.model or '').lower()
            for keyword in rule.keywords:
                if keyword.lower() in device_model:
                    _LOGGER.debug(f"ModelKeywordMatch: keyword '{keyword}' in '{device_info.model}'")
                    return True
            return False

        if isinstance(rule, NameKeywordMatch):
            if not rule.keywords:
                return False
            name_lower = (device_info.name or '').lower()
            for keyword in rule.keywords:
                if keyword.lower() in name_lower:
                    _LOGGER.debug(f"NameKeywordMatch: keyword '{keyword}' in '{device_info.name}'")
                    return True
            return False

        if isinstance(rule, EntityCompositionMatch):
            return self._check_entity_composition(rule, device_info)

        return False

    def _check_entity_composition(self, rule: EntityCompositionMatch,
                                   device_info: DeviceInfo) -> bool:
        """检查 entity 组合是否匹配"""
        if not device_info.entity_domains:
            return False

        device_domains = set(device_info.entity_domains)

        # 检查必须包含的 domain
        required = set(rule.required_domains)
        if not required.issubset(device_domains):
            return False

        # 检查 sensor 的 device_class
        if rule.sensor_classes:
            for sensor_class in rule.sensor_classes:
                found = False
                for domain, dc in device_info.entity_device_classes.items():
                    if domain == 'sensor' and dc == sensor_class:
                        found = True
                        break
                if not found:
                    return False

        return True

    def _match_auto(self, device_info: DeviceInfo) -> Optional[tuple]:
        """框架 auto_match 兜底匹配

        检查每个有 auto_match 的框架产品：
        - required_domains: 设备必须包含的 entity domain（必须全部满足）
        - name_keywords: 设备名包含的关键词（可选，满足任一即可）

        Returns:
            (ProductDef, "auto_match") 或 None
        """
        if not device_info.entity_domains:
            return None

        device_domains = set(device_info.entity_domains)
        name_lower = (device_info.name or '').lower()

        for product in self.registry.get_all_products().values():
            auto_match = product.auto_match
            if not auto_match:
                continue

            # 检查 required_domains
            required = set(auto_match.get('required_domains', []))
            if required and not required.issubset(device_domains):
                continue

            # 检查 name_keywords（可选）
            name_keywords = auto_match.get('name_keywords')
            if name_keywords:
                matched = False
                for kw in name_keywords:
                    if kw.lower() in name_lower:
                        matched = True
                        break
                if not matched:
                    continue

            _LOGGER.debug(f"AutoMatch: {product.pid} matched for device {device_info.device_id}")
            # 合并框架 + 默认适配器（无厂家适配器，用标准 ha_mapping）
            merged = self.registry.merge(product, None)
            return merged, "auto_match"

        return None
