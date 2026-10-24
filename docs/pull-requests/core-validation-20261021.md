# 公共能力贡献验证（2026-10-21）

基线：上游 `main` 的 `9033ac0`。范围：本地 `codex/reusable-fan-core`，不含 OpenHarmony、部署工具或私有设备资料。

- macOS / Python 3.12：52 项公共测试通过。
- 独立原生 ARM Linux / 官方 ARM64 HA 2026.10.1、Python 3.14：52 项公共测试通过。
- 同一 ARM HA 镜像内，`tests/ha_capability_integration.py` 通过真实 HA 状态、服务与实体注册表验证：保留原主灯对象和绑定、风速／模式／方向、原生定时确认／取消／重建，以及实体归属和部分只读拒绝。
- 原产品配置及虚构能力配置校验通过；`git diff --check` 通过。

Linux 测试容器使用无网络、只读源码挂载、临时 `/tmp` 和移除全部 capabilities 的方式运行。真实 HA 入口禁止加载 HiLink SDK；测试结果不是智慧生活发现、配对、云端、LAN 控制、原生 x86 或实物验收。

测试源为提交前工作树实际字节，不依赖未跟踪的实验脚本或个人路径。当前分支的公共模块没有导入 `openharmony` 或 `gateway_*`，旧 SDK、配置流、正式版本和原 001–008 配置均无贡献差异。

提交分为公共风扇映射、角色／定时／组合，以及旧主卡保留与离线隔离三批。PR 正文见[草稿](01-reusable-capabilities.md)。尚未 fork、push 或创建远端 PR。
