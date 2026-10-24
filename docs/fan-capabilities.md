# 公共风扇能力：第一批贡献

第一批代码把实验中验证过的风速、模式、方向转换接入原来的公共 Python 框架。它不新增产品、不注册新网关、不改变原001–008目录或SN，也不修改原生库、原配置流、版本号和安装入口。后续角色、原生定时、组合与HA本地传输的实现见[公共能力说明](role-timer-compositions.md)。

**这还不是可安装使用的风扇产品支持。** 原库新增 PID/profile 的接口与构建源码尚未取得；只有在同一原网关内验证过旧灯光和新风扇注册后，才能启用相应产品配置。公共能力已继续提炼，真实单网关原生接入和发布仍待完成。

## 配置方式

以下是开发者用的服务映射片段，不应直接加入当前生产产品目录。

```json
{
  "domain": "fan",
  "action": "set_speed",
  "value_attr": "percentage",
  "value_mapping": {
    "type": "enum_to_number",
    "mapping": {"1": 25, "2": 37, "3": 50, "4": 62, "5": 75, "6": 87}
  },
  "options": {}
}
```

- `set_speed`：枚举直接转为 `fan.set_percentage`，不再对映射结果乘4。上报和查询使用同一张表反查实际百分比。
- `set_direction`：`value_attr: "direction"`，表值只能为`forward`或`reverse`。默认极性和厂家极性分别定义；例如某型号可用`{"0":"reverse","1":"forward"}`，不会影响其他型号。
- `set_fan_mode`：支持以百分比表达的自然风，也支持`options.ha_service: "set_preset_mode"`配合`value_attr: "preset_mode"`。例如`{"2":"natural"}`调用HA预设模式；百分比通道则用`{"2":100}`，调用`set_percentage`。
- `options.unmapped`：可显式指定“当前未处于这个模式”的回报枚举。缺少该字段时，不报告无法反查的值；缺失属性、未知或不可用状态始终不伪造回报。

映射键为非负整数的标准字符串，百分比为0–100的整数，表必须可唯一反查。命令不在表中、含额外字段、实体域错误或字段归属含糊时拒绝执行，不按最近值猜档位，不返回上次下发值冒充当前状态。

公共注册表保留`options`并按完整字段进行厂家覆盖；传入配置被复制，避免多个型号共享可变字典。华为定义的`service_type`、`char_name`继续只由产品框架提供。

旧的、未配置映射的`set_speed`动作暂时保留历史换算行为，以免本批公共能力改变用户已有自定义适配；待迁移对应适配器并通过回归后再删除兼容路径。

## 代码与测试

`fan_mapping.py`负责纯转换，`ServiceActionDispatcher`负责投递HA服务，`ServiceRouter`共用转换完成字段路由、状态上报和查询。没有依赖`openharmony/`模块或书房实体ID；实验后端可以反过来调用公共转换，逐步删除重复代码。

```sh
python3 -m pip install aiofiles
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 custom_components/hwhomebridge/validate_config.py
```

测试使用假实体和假服务，只导入公共业务模块，不执行集成入口，不加载`.so`，不访问家庭HA。覆盖六档和三档配置、两种自然风通道、方向极性、错误输入、未确认状态、字段冲突、原001–008匹配、旧灯光读写及SDK回调线程向HA主循环投递。服务被成功投递不等于物理设备执行成功；真实单网关验收仍是后续原生门槛。
