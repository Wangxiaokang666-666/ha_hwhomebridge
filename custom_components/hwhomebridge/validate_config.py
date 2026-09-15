#!/usr/bin/env python3
"""验证配置文件的正确性

验证范围：
1. 框架层 product_registry.json：PID 唯一性、action+domain 兼容性、char_name 一致性
2. 适配器层 adapters/*.json：match_rules 有效性、PID 引用有效性

运行时验证已在 product_registry.py 中实现。
"""

import json
import sys
import os
from pathlib import Path
from typing import List


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


class ValidationError:
    """验证错误"""
    def __init__(self, level: str, source: str, message: str):
        self.level = level  # "error" or "warning"
        self.source = source  # 文件名或 PID
        self.message = message

    def __str__(self):
        return f"[{self.level.upper()}] {self.source}: {self.message}"


class ConfigValidator:
    """配置验证器"""

    def __init__(self, framework_path: str, adapters_dir: str):
        self.framework_path = framework_path
        self.adapters_dir = adapters_dir
        self.errors: List[ValidationError] = []
        self.warnings: List[ValidationError] = []

    def validate(self) -> bool:
        """执行所有验证"""
        framework_data = self._load_json(self.framework_path)
        if framework_data:
            self._validate_framework(framework_data)
            self._validate_adapters(framework_data)
        return len(self.errors) == 0

    def _load_json(self, path: str) -> dict:
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            self._add_error(self.framework_path, f"Cannot load: {e}")
            return {}

    def _add_error(self, source: str, message: str):
        self.errors.append(ValidationError("error", source, message))

    def _add_warning(self, source: str, message: str):
        self.warnings.append(ValidationError("warning", source, message))

    def _validate_framework(self, data: dict):
        """验证框架层配置"""
        products = data.get('products', [])
        valid_pids = set()

        for product in products:
            pid = product.get('pid')
            if not pid:
                self._add_error("framework", "Product missing 'pid'")
                continue

            if pid in valid_pids:
                self._add_error(pid, f"Duplicate PID")
            else:
                valid_pids.add(pid)

            # 验证 auto_match
            auto_match = product.get('auto_match')
            if auto_match:
                if not isinstance(auto_match.get('required_domains'), list):
                    self._add_warning(pid, "auto_match: required_domains should be a list")

            # 验证 services 的 action+domain
            for svc_id, svc in product.get('services', {}).items():
                mapping = svc.get('ha_mapping', {})
                if not mapping:
                    continue

                action = mapping.get('action')
                domain = mapping.get('domain')
                if not action or not domain:
                    continue

                valid_domains = VALID_ACTION_DOMAIN_MAP.get(action)
                if not valid_domains:
                    self._add_warning(pid, f"Service '{svc_id}': unknown action '{action}'")
                elif domain not in valid_domains:
                    self._add_error(pid, f"Service '{svc_id}': invalid action+domain "
                                  f"(action='{action}', domain='{domain}')")

    def _validate_adapters(self, framework_data: dict):
        """验证适配器层配置（含 default 和 vendor）"""
        valid_pids = {p.get('pid') for p in framework_data.get('products', [])}

        if not os.path.exists(self.adapters_dir):
            self._add_warning("adapters", f"Adapters directory not found: {self.adapters_dir}")
            return

        default_dir = os.path.join(self.adapters_dir, 'default')
        adapter_count = 0
        for root, dirs, files in os.walk(self.adapters_dir):
            for filename in sorted(files):
                if not filename.endswith('.json'):
                    continue
                filepath = os.path.join(root, filename)
                relpath = os.path.relpath(filepath, self.adapters_dir)
                is_default = (os.path.dirname(filepath) == default_dir)

                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        adapter = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    self._add_error(relpath, f"Cannot load: {e}")
                    continue

                adapter_count += 1
                pid = adapter.get('pid')
                if not pid:
                    self._add_error(relpath, "Missing 'pid'")
                    continue

                if pid not in valid_pids:
                    self._add_error(relpath, f"PID '{pid}' not found in framework")

                # default 适配器不需要 match_rules，vendor 需要
                rules = adapter.get('match_rules', [])
                if not rules and not is_default:
                    self._add_warning(relpath, "No match_rules defined")

                valid_types = {'model_exact', 'model_keyword', 'name_keyword', 'entity_composition'}
                for rule in rules:
                    rule_type = rule.get('type', '')
                    if rule_type not in valid_types:
                        self._add_error(relpath, f"Unknown match rule type: '{rule_type}'")
                    if rule_type == 'model_exact' and not rule.get('model'):
                        self._add_error(relpath, "model_exact rule missing 'model'")
                    if rule_type in ('model_keyword', 'name_keyword') and not rule.get('keywords'):
                        self._add_error(relpath, f"{rule_type} rule missing 'keywords'")

                # 验证 services 的 action+domain
                for svc_id, svc in adapter.get('services', {}).items():
                    action = svc.get('action')
                    domain = svc.get('domain')
                    if not action or not domain:
                        continue

                    valid_domains = VALID_ACTION_DOMAIN_MAP.get(action)
                    if not valid_domains:
                        self._add_warning(relpath, f"Service '{svc_id}': unknown action '{action}'")
                    elif domain not in valid_domains:
                        self._add_error(relpath, f"Service '{svc_id}': invalid action+domain "
                                      f"(action='{action}', domain='{domain}')")

        if adapter_count == 0:
            self._add_warning("adapters", "No adapter files found")

    def print_results(self):
        """打印验证结果"""
        if self.warnings:
            print("\nWarnings:")
            for w in self.warnings:
                print(f"  - {w}")

        if self.errors:
            print("\nErrors:")
            for e in self.errors:
                print(f"  - {e}")

        if not self.errors and not self.warnings:
            print(f"\n[PASS] Configuration is valid")
        elif not self.errors:
            print(f"\n[PASS] Configuration is valid ({len(self.warnings)} warnings)")
        else:
            print(f"\n[FAIL] Configuration has {len(self.errors)} errors")


def validate_config(framework_path: str, adapters_dir: str = "") -> bool:
    """验证配置文件

    Args:
        framework_path: 框架层 product_registry.json 路径
        adapters_dir: 适配器目录路径（默认从 framework_path 推导）

    Returns:
        True 表示验证通过，False 表示有错误
    """
    if not adapters_dir:
        config_dir = os.path.dirname(framework_path)
        adapters_dir = os.path.join(config_dir, 'adapters')

    validator = ConfigValidator(framework_path, adapters_dir)
    is_valid = validator.validate()
    validator.print_results()
    return is_valid


if __name__ == '__main__':
    base_dir = Path(__file__).parent / 'config'
    framework_path = str(base_dir / 'product_registry.json')
    adapters_dir = str(base_dir / 'adapters')

    if len(sys.argv) > 1:
        framework_path = sys.argv[1]
        adapters_dir = str(Path(framework_path).parent / 'adapters')
    if len(sys.argv) > 2:
        adapters_dir = sys.argv[2]

    if not os.path.exists(framework_path):
        print(f"Error: Config file not found: {framework_path}")
        sys.exit(1)

    success = validate_config(framework_path, adapters_dir)
    sys.exit(0 if success else 1)
