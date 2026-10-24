# 公共角色、原生定时与组合能力

这一批将已验证的实验业务逻辑移到公共框架，不创建第二个网关。公共模块不依赖`openharmony/`、REST token、SSH、Mac代理、HTTP监听或原生进程。实验层仅保留原实验SN算法、云目录格式及测试写权限策略，继续调用公共实现。

公共能力现已接入原配置条目的受管生命周期：OpenHarmony 可在同一实验网关下承载旧品类和新风扇；旧 SDK 仍只支持 001–008，作为回退。原生产网关尚未切换，原主灯和全部旧 SN 保留。最新证据及原机交接门槛见[最终单网关准备](final-single-gateway-preparation.md)。

## 模块职责

| 模块 | 作用 |
| --- | --- |
| `product_registry.py` | 公共HAMapping增加`role`，三层合并保留角色、参数及厂家组合名 |
| `product_matcher.py` | 普通发现保持原匹配；只有`include_compositions=True`才选择组合适配器 |
| `role_binding.py` | 将角色绑定到同一HA设备的实体清单，检查域、缺失角色和重复实体 |
| `capability_registry.py` | 显式加载能力目录，复用公共注册表；核对产品、服务、特性和读写约束 |
| `composition.py` | 规划稳定子卡身份、保留旧主卡、拒绝重复控制及未确认PID；仅发布内存路由 |
| `native_timer.py` | 设备原生定时控制及分钟值、延时数组、组合表单协议 |
| `capability_router.py` | 继承ServiceRouter，共用角色、读写、回报与定时；旧卡继续原动作/查询路径 |
| `ha_transport.py` | 直接访问当前HA的状态、实体注册表和本地服务；服务在HA主循环执行并等待完成 |

## 角色绑定与同设备归属

角色名描述用途，例如`fan`、`main`、`fan_timer_duration`；它不是实体名匹配关键词。适配器声明服务使用哪个角色，实例声明角色对应哪个实体。

```json
{
  "domain": "fan",
  "action": "set_direction",
  "value_attr": "direction",
  "role": "fan",
  "value_mapping": {"type": "enum_to_number", "mapping": {"0": "forward", "1": "reverse"}},
  "options": {}
}
```

实例里的角色绑定只描述用户选择，不能自行证明归属。正式调用方在HA主循环中使用`async_owned_entity_inventory(hass, device_ids)`取得实体注册表清单，再传给公共工厂；外部实体和禁用实体不会进入清单。角色域不符或重复绑定同一实体时拒绝整份计划。

同一角色可以被多个子卡服务引用；一个实例里不同角色不绑定同一实体。旧的非角色适配器继续原来的发现和实体评分流程。

## 单网关组合与旧灯光保护

组合引用公共产品和服务定义，只覆盖HA映射，不能添加华为服务或改`service_type`/`char_name`。例如测试用的虚构组合：

```json
{
  "bundles": {
    "fan_light": [
      {"key": "light", "pid": "fixture-light", "primary": true},
      {"key": "fan", "pid": "fixture-fan"}
    ]
  }
}
```

`fixture-*`只是测试数据，不是真实华为产品。实际PID仍须由原生后端确认支持。

- 工厂需要调用方提供原来的`parent_sn`、同设备实体清单、已验证PID清单及原`SNManager`。公共层不生成父网关身份、不启动SDK、不做云注册。
- `primary: true`遇到原主卡时保留同一个VirtualDevice对象、SN、PID、服务和已有控制路径，不把新模板强行套上去；保留关系记录在`preserved_cards`，不再次注册成新卡。
- 没有旧主卡的新设备，其主卡仍使用原`SNManager.generate_sn(device_id)`。只有新增逻辑卡使用“父身份＋物理设备＋卡片键”的版本化命名空间。
- 普通设备发现默认忽略组合适配器，避免新增风扇型号精确匹配抢走原灯光匹配。显式组合匹配与旧品类可使用同一份三层注册表。
- 新卡若重复原主卡的控制，整体拒绝，要求调整组合。混合了旧灯光控制和额外功能的模板不能自动删掉一半服务冒充完整profile；应选择合适的独立能力卡。
- 所有源设备和卡片先完成验证，再统一发布内存路由。后一个设备配置无效，不会留下前一个设备的半套注册。
- 同一个SNManager的能力路由不能改接第二个父网关；新PID不在后端确认清单时不发布。

`1.1.0b8` 增加显式共享声明：主卡可用 `primary_role` 核对原卡全部控制的目标；补充卡可用 `share_when_primary_preserved` 共享原主卡的普通开关。双方须为同实体、无值映射、无参数及 `on_command` 的二值开关；不能借此共享带副作用的服务。保留已声明的模板服务，原主卡对象、SN、PID 和绑定均不变，其余重复控制继续拒绝。书房组合因此保留原 `002` 主灯，`2NAL` 模板里的主灯开关明确指向同一实体。最终接入和原身份准备见[单网关配置](final-single-gateway-preparation.md)。

## 原生定时行为

定时配置绑定三个角色：启用开关、时长number、剩余时间sensor。

```json
{
  "enable": "fan_timer_enable",
  "duration": "fan_timer_duration",
  "remaining": "fan_timer_remaining",
  "maximum": 720,
  "duration_unit": "minutes",
  "remaining_unit": "minutes"
}
```

时长和剩余时间的单位分别支持`minutes`、`seconds`，华为侧以分钟表示；单位必须显式声明，默认分钟。启用前检查角色域、权限、有效状态及HA number的min/max/step，先写时长并等待读回一致，再打开启用开关。时长没有得到确认时，不继续启动设备定时。

取消只关闭设备定时开关；已经停用时不重复下发。组合目标先全部检查权限和范围，不能先改风扇再发现主灯只读。设备实际写入仍然不能保证跨目标事务：若执行过程中设备报错，返回失败，不虚构成功，也不擅自回滚其他物理设备。

三种协议共用同一控制器：

- `native_timer`：分钟数，0为取消，读实际剩余时间。
- `timer_array`：一个原生延时关闭任务，支持创建/编辑/取消，核对任务ID、目标服务、任务数量和结束时间；不支持任意动作或多任务软件队列。
- `countdown`：为风扇、灯等已声明目标提供选择/启停/分钟/剩余时间字段。表单选择只存在内存，定时本身由设备保存。

没有Python定时任务或软件倒计时状态文件。重建路由只读设备的启用、配置时长和剩余值，不重新发送时长/启用命令。剩余传感器即使没有直接映射到某个服务，也会触发相关卡片重算回报；查询和上报使用同一实际状态转换。

## 本地HA接入入口

以下展示业务层工厂；正式受管入口由 `GatewayRouter` 和 `GatewayLifecycle` 调用，不是用户单独执行的部署命令：

```python
inventory = async_owned_entity_inventory(hass, selected_device_ids)
transport = HomeAssistantTransport(hass)
router = await CapabilityRouter.create(
    transport,
    configuration_directory,
    instances,
    owned_entities=inventory,
    supported_pids=verified_pids,
    sn_manager=original_router.sn_manager,
)
router.set_hass(hass)
```

`verified_pids`必须来自已审查的原生能力目录，不能照抄配置里出现的PID作为支持证明。当前旧库还未提供这个新产品扩展证明。

同步`write`/`snapshot`在工作线程调用。HA传输会把服务调用交回原HA主循环，等待执行完成；主循环直接调用同步传输时拒绝等待，避免死锁。HA状态事件在主循环内直接取本地快照再排队上报，不反过来等待自身。多个服务都有`on`字段时必须携带明确SID；旧卡仍兼容原命令入口。

原生后端的 SID 回调、产品目录、服务注册和生命周期验收另行记录；这里的内存路由和假设备服务单测不能替代云端/App 证据。

## 独立测试

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 custom_components/hwhomebridge/validate_config.py
python3 custom_components/hwhomebridge/validate_config.py tests/fixtures/capabilities/product_registry.json
# 在官方HA镜像中另运行真实Core状态/服务/实体注册表测试：
python3 -B tests/ha_capability_integration.py
```

所有`tests/fixtures/capabilities/`产品、设备和实体均为虚构，没有私有profile、配对KV或个人凭据。Linux测试使用无网络、只读源文件及临时配置目录；不加载网关SDK，不访问家庭HA。真实HA测试只验收Core业务链路，未测试集成配置流、HiLink注册、智慧生活配对或真实硬件。
