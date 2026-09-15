# 适配器配置指南

## 概述

Value Mapping（值映射）机制用于解决华为设备 与 HA 设备实体之间的值转换问题。

### 典型场景

1. **虚拟服务**：华为侧有 mode 服务（枚举），但 HA 侧只有 number 实体（温度值）
2. **状态值转换**：HA sensor/select 状态是中文文本，华为侧需要枚举值
3. **多插件适配**：不同 HA 插件对同一设备的表示可能不同（中文/英文），需要灵活匹配

## 配置位置

value_mapping 定义在**适配器层**的 service 配置中：

- **默认适配器** `config/adapters/default/<PID>.json`：华为标准映射（从 profile enumList 提取）
- **厂家适配器** `config/adapters/<vendor>/<category>.json`：覆盖默认映射的差异部分

```json
// config/adapters/default/005.json (电水壶默认适配器)
{
    "pid": "005",
    "services": {
        "mode": {
            "domain": "number",
            "action": "set_value",
            "value_attr": "state",
            "value_mapping": {
                "type": "enum_to_number",
                "mapping": {
                    "1": 100,
                    "5": 85,
                    "7": 80
                }
            }
        }
    }
}
```

## 支持的映射类型

| 类型 | 方向 | 用途 |
|------|------|------|
| `enum_to_number` | 华为枚举 → HA 数值 | mode 枚举 → 温度数值 |
| `text_to_enum` | HA 文本 → 华为枚举 | sensor/select 状态文本 → 枚举值 |
| `enum_to_text_multi` | 华为枚举 → HA 文本列表 | select 选项匹配（多厂商中英文） |
| `number_to_enum_multi` | HA 数值 → 华为枚举 | sensor 数值状态 → 枚举值 |
| `seconds_to_minutes` | HA 秒数 → 华为分钟 | timer 转换（除以 divide_by） |
| `delay_to_select` | 华为 delay 服务 → HA select 选项 | 倒计时服务映射 |

---

## 示例1：枚举→数值映射（mode→温度）

```json
{
    "domain": "number",
    "action": "set_value",
    "value_attr": "state",
    "value_mapping": {
        "type": "enum_to_number",
        "mapping": {
            "1": 100,
            "5": 85,
            "7": 80
        }
    }
}
```

**控制流程**：
- 用户在华为 APP 选择"黄茶 80℃"（枚举值 7）
- 系统映射到温度值 80
- 调用 HA 的 `number.set_value(80)`

**状态上报**：
- HA 的 number 实体状态变为 80
- 系统反向映射到枚举值 "7"
- 上报给华为侧：`{"mode": 7}`

---

## 示例2：文本→枚举映射（状态→枚举）

```json
{
    "domain": "sensor",
    "action": "read_only",
    "value_attr": "state",
    "value_mapping": {
        "type": "text_to_enum",
        "mapping": {
            "待机": 0,
            "升温中": 2,
            "制作中": 3,
            "保温中": 4,
            "完成": 6
        }
    }
}
```

**状态上报流程**：
- HA sensor 状态变为"升温中"
- 系统映射到枚举值 2
- 上报给华为侧：`{"status": 2}`

**厂家覆盖**：小米水壶的状态文本与华为标准不同，在厂家适配器中覆盖：

```json
// config/adapters/xiaomi/kettle.json
{
    "services": {
        "status": {
            "value_mapping": {
                "type": "text_to_enum",
                "mapping": {
                    "待机中": 0,
                    "加热中": 2,
                    "沸腾中": 2,
                    "降温中": 4,
                    "定温中": 4
                }
            }
        }
    }
}
```

> text_to_enum 也支持 select 域。如美的电饭煲的工作状态是 select 实体，同样用 text_to_enum 映射。

---

## 示例3：多值匹配映射（适配不同插件）

**enum_to_text_multi**：华为枚举 → 多个可能的 HA 文本值（用于 select）

```json
{
    "domain": "select",
    "action": "set_option",
    "value_attr": "state",
    "value_mapping": {
        "type": "enum_to_text_multi",
        "mapping": {
            "1": ["快煮饭", "Quick Cook", "quick"],
            "2": ["精煮饭", "Fine Cook", "煮饭"]
        }
    }
}
```

**控制流程**：
- 用户在华为 APP 选择"快煮饭"（枚举值 1）
- 系统获取 HA select entity 的实际 options 列表
- 从 `["快煮饭", "Quick Cook", "quick"]` 中找到匹配项
- 调用 HA 的 `select.select_option(matched_option)`

**状态上报流程**：
- HA select 状态变为"Quick Cook"
- 系统反向查找：在哪个映射值列表中包含"Quick Cook"
- 找到 `"1": ["快煮饭", "Quick Cook", ...]`
- 上报给华为侧：`{"mode": 1}`

**匹配优先级**：精确匹配 > 大小写不敏感匹配 > 子串匹配

---

## 示例4：数值→枚举多值映射（sensor 状态）

**number_to_enum_multi**：HA 数值/文本 → 华为枚举（支持多值匹配）

```json
{
    "domain": "sensor",
    "action": "read_only",
    "value_attr": "state",
    "value_mapping": {
        "type": "number_to_enum_multi",
        "mapping": {
            "0": [0, "Idle", "空闲", "待机"],
            "1": [1, "Cooking", "烹饪中", "工作中"],
            "2": [2, "Keep Warm", "保温"],
            "3": [3, "Done", "完成", "Finished"]
        }
    }
}
```

**上报时**（HA→华为）：
- HA sensor 状态为 "Cooking"（或 1 或 "烹饪中"）
- 系统遍历 mapping，找到 "1" 列表中包含 "Cooking"
- 上报给华为侧：`{"status": 1}`

---

## 示例5：时间单位转换（seconds_to_minutes）

```json
{
    "domain": "sensor",
    "action": "read_only",
    "value_attr": "state",
    "value_mapping": {
        "type": "seconds_to_minutes",
        "divide_by": 60
    }
}
```

**上报时**：
- HA sensor 状态为 780（秒）
- 系统计算 780 / 60 = 13
- 上报给华为侧：`{"time": 13}`（分钟）

---

## 示例6：倒计时服务映射（delay_to_select）

华为灭蚊器的 delay 服务有复杂的 payload 结构（action + delay 数组），需要映射到 HA 的 select 实体。

```json
{
    "domain": "select",
    "action": "set_option",
    "value_attr": "state",
    "value_mapping": {
        "type": "delay_to_select",
        "mapping": {
            "0": "关闭",
            "3": "3小时",
            "8": "8小时",
            "12": "12小时"
        },
        "close_action": 2
    }
}
```

**控制流程**（华为→HA）：
- 华为下发关闭倒计时（action=2）→ 映射到 "关闭" 选项
- 华为下发创建倒计时（action=0 + end_time）→ 解析 UTC 时间计算小时数 → 匹配最接近的选项

**状态上报**（HA→华为）：
- HA select 状态为"3小时" → 反向查找 hours=3 → 构建完整 delay payload 上报

---

## 实现细节

### 核心类

**ValueMapping** (`product_registry.py`):

```python
@dataclass
class ValueMapping:
    mapping_type: str = ""
    mapping_dict: dict = field(default_factory=dict)

    def transform_hw_to_ha(self, value):
        """华为值 → HA值（控制命令时使用）"""

    def transform_ha_to_hw(self, value):
        """HA值 → 华为值（状态上报时使用）"""
```

### 集成点

1. **service_action.py**:
   - `dispatch()` 方法调用 `_apply_value_mapping()`
   - `_handle_set_option()` 支持 `enum_to_text_multi` 多值匹配
   - `_handle_set_value()` 支持 `enum_to_number` 转换
   - `_find_matching_option()` 执行匹配算法

2. **service_router.py**:
   - `_report_by_type()` 根据 domain 和 mapping_type 分发上报逻辑
   - 支持缓存 `last_commanded_enum`，用于非双射映射的反向状态上报

---

## 注意事项

1. **配置格式**：mapping 中的 key 和 value 都作为字符串处理
2. **未映射值**：如果找不到映射关系，返回原值
3. **向后兼容**：如果不定义 value_mapping，保持原有行为
4. **多值匹配优先级**：精确匹配 > 大小写不敏感匹配 > 子串匹配
5. **三层继承**：厂家适配器的 value_mapping **整体覆盖**默认适配器的 value_mapping（不支持条目级合并），如需修改单个条目需完整复制
6. **select + text_to_enum**：text_to_enum 不仅支持 sensor 域，也支持 select 域（如美的电饭煲工作状态）

---
