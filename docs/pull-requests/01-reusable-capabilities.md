# PR 草稿：为原桥接增加可复用风扇映射、角色绑定与原生定时

目标：`Wangxiaokang666-666/ha_hwhomebridge` 的 `main`。本地贡献分支：`codex/reusable-fan-core`，基线 `9033ac0`。尚未 push 或创建 PR。

## 可直接用于 PR 的正文

原路由用固定百分比处理风速，无法表达不同设备的档位、自然风和方向极性；多个 HA 实体也缺少明确的功能角色。本变更新增配置驱动的双向风扇映射、实体角色绑定、设备原生定时和组合规划，复用现有产品注册表、VirtualDevice 和控制路由。

既有设备继续使用原发现流程、SN 和旧 SDK。已有主灯被组合保留时，沿用原对象和服务绑定；补充卡只在显式声明、同一实体且为无额外副作用的二值开关时允许共享控制。设备缺失或不可用时拒绝写入，并隔离到对应卡片，不影响其他可用卡片。

所有新产品与实体测试资料均为虚构。此 PR 不附带 OpenHarmony 引擎、私有 profile、配对资料或部署工具，不会创建第二个网关。合入公共框架不等于旧闭源 SDK 已支持新风扇 PID；原生引擎与正式设备适配分后续 PR 审查。

验证命令：

```sh
python3 -m unittest discover -s tests
python3 custom_components/hwhomebridge/validate_config.py
python3 custom_components/hwhomebridge/validate_config.py tests/fixtures/capabilities/product_registry.json
```

独立 HA Core 验证入口：`tests/ha_capability_integration.py`。该入口只使用虚构实体和本地 HA 服务；运行结果不能替代 HiLink 注册、App 验收或真实硬件验收。最新结果和环境见[贡献验证记录](core-validation-20261021.md)。

## 审查与后续依赖

1. 检查三层配置继承、显式组合匹配和未启用新能力时的旧路由兼容。
2. 检查实体归属、重复控制拒绝、保留主卡和受限开关共享。
3. 检查定时调用设备自己的启用、时长和剩余实体，重建路由不重置定时。
4. 原生引擎后续 PR 须提供来源、固定依赖、两架构构建、单身份接管和回退证据。
5. 设备适配及发布安装后续 PR 分别提供完整功能边界、保留式升级和新装证据。

提交范围不包含 `manifest.json`、原配置流、`hwbridge.py`、旧 001–008 产品配置或原生二进制变更。Jev 仅参与开发验收证据分类，组件没有 AI 运行依赖。
