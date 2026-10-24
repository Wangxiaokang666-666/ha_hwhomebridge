# 本次整合的完整改动清单

统计快照：`codex/openharmony-ha-integration-plan` 的 `0ba6011`，相对本地 `origin/main` 的 `9033ac0`。本清单及随后新增的 LAN 验收材料不计入该历史快照；不代表上游远端今天仍停留在这一提交。

已有 **222 个差异文件，新增 26,798 行、删除 67 行**。这些数字包括实验、测试和验收记录，不能当作生产运行代码量。干净贡献分支 `codex/reusable-fan-core` 截至 `a749af2` 是 **27 文件、+2,792 / -9 行**，只含公共能力及其文档、测试、PR 草稿。

| 范围 | 文件数 | 新增行 | 删除行 | 用途 |
| --- | ---: | ---: | ---: | --- |
| HA 组件 | 47 | 4,422 | 67 | 公共能力、正式条目生命周期、实验引擎适配和声明配置 |
| 工具 | 38 | 4,952 | 0 | 构建、预检、身份导入、冷备、保留式升级与相应测试 |
| 实验及验证 | 90 | 12,117 | 0 | C 引擎、固定依赖补丁、Linux 夹具、脱敏证据 |
| 公共测试 | 15 | 1,926 | 0 | 公共映射、生命周期、隔离、实际 HA 业务链路 |
| 文档 | 32 | 3,381 | 0 | 设计、安装、备份、维护、验收和贡献材料 |

## 准备阶段增量与当前总范围

准备阶段的 `62fa74d` 另外新增 5 个文件、修改上述快照中 2 个文件，共有 227 个差异文件。随后实际 LAN 验收新增 1 份脱敏记录，并更新验收说明及本清单；当前相对上游基线共有 **228 个差异文件**：HA 组件 47、工具 39、实验及验证 90、公共测试 15、文档 37。干净贡献分支仍为 27 文件，没有把实验工具混入第一批公共框架贡献。

| 状态 | 文件 | 内容 |
| --- | --- | --- |
| 新增 | [docs/change-inventory.md](change-inventory.md) | 本清单，包含历史全部文件及本轮增量 |
| 新增 | [docs/app-direct-lan-acceptance.md](app-direct-lan-acceptance.md) | App LAN 的前置条件、实际边界和验收步骤 |
| 新增 | [docs/plans/app-direct-lan-preparation-validation.json](plans/app-direct-lan-preparation-validation.json) | 独立 HA 重启、观察器修复及尚未通过的 App/LAN 项 |
| 新增 | [docs/plans/change-inventory-classification.json](plans/change-inventory-classification.json) | Jev 模块分类及 Codex 复核记录 |
| 新增 | [tools/ha_macos_lab_lan.sh](../tools/ha_macos_lab_lan.sh) | 临时 Mac 实验桥接脚本；管理员启动及实际 LAN 发现现已验证 |
| 修改 | [experiments/openharmony_bridge/gateway_app_acceptance.py](../experiments/openharmony_bridge/gateway_app_acceptance.py) | 排除历史确认，记录启动轮次及本次 C 写入结果 |
| 修改 | [docs/native-lan-confirmation-validation.md](native-lan-confirmation-validation.md) | 链接后续 App 直连进展，不改变旧轮次的验收结论 |
| 后续新增 | [docs/plans/app-direct-lan-runtime-validation.json](plans/app-direct-lan-runtime-validation.json) | 物理 App 的 LAN 发现、首次控制失败、云端对照、实际 HA 重启和网络恢复断言 |

新增 6 个文件加上后面的 222 文件清单，覆盖当前全部 228 个差异路径；历史未跟踪文件仍在文末单独列出。当前 App LAN **发现**已通过，LAN **服务控制**仍未通过。云端空场景控制和重启后首次关闭已通过；平板 IPv4 受限测试存在 IPv6 外网通道，未计为完全断网验收。这轮实测只更新文档和证据，没有修改 C 或 HA 运行代码。

## 已实现的能力

| 内容 | 具体包含 | 当前边界 |
| --- | --- | --- |
| 声明式风扇 | 档位与百分比、自然风、开关、方向双向映射；厂家方向极性修复 | 书房型号为六档＋自然风；模板多余的七／八档拒绝，米家同步自动化不改 |
| 通用角色绑定 | 同一设备的风扇、主灯、氛围灯、方向、定时实体按角色绑定；缺失／重复／跨设备校验 | 用户实例保存在私有存储，贡献代码不写家庭实体 |
| 设备原生定时 | 时长、启用和剩余时间映射、单位转换及可用性检查 | 使用设备自身定时，不在桥接内运行一个替代倒计时 |
| 组合产品 | 一台 HA 设备可形成风扇、辅助开关、定时及灯效补充卡；保留旧主卡、旧 SN | 多张子卡共用一个父网关；同一角色的普通开关仅允许受限共享 |
| 灯光保留 | 旧 001–008 类别转接；原主灯开关、亮度、色温继续由原绑定路由；补充氛围灯与灯效 | 真实书房灯的全套功能曾分项验证；本轮只操作虚构实体 |
| 离线隔离 | 单个子设备离线、写入拒绝、状态缓存清理和恢复 | 一个子卡离线不拖垮网关或其他卡 |
| 单网关生命周期 | 原 HA 配置条目显式选择引擎，单一所有者、启停、失败回滚、重启恢复 | 最终只启用一个根网关；仓库仍留有早期独立后端入口，不能当作最终双网关方案 |
| 旧身份接管 | 严格导入旧 SN、AC、账户和子设备资料，冻结旧映射与 profile 摘要，离线校验 | 不启发式重新匹配居民设备；生产准备目录保持离线保护 |
| OpenHarmony C 引擎 | 可重建 Linux ARM64／x86_64 静态程序、虚拟子设备注册、服务路由、云端状态和 LAN 认证控制 | 核心源码仍位于实验目录；正式源码发布布局及许可证清单仍待整理 |
| 协议修复 | SPEKE/v2 HMAC 兼容、非对齐 CoAP 读取、绑定资料持久化、配对身份恢复 | 不绕过认证、GCM 或重放校验；配对密钥与派生密钥不入公开日志 |
| C 控制确认 | 按 HTTP Content-Length 读取完整帧、严格拒绝坏帧、关联请求编号和安全错误诊断 | 已复现并修复“完整响应仍等 EOF 超时”；历史 App 空场景失败尚不能据此唯一归因 |
| 构建与升级 | 固定依赖及 SHA、两架构产物清单、保留式安装包、安装和还原演练 | 当前旧 b8 包基于 `33c3b94`，不含 `0ba6011` 的 C 修复；尚非完整 HACS 新装 Release |
| 备份与预检 | 实体清单、旧绑定审计、维护 SSH 通道、Docker 挂载审查、在线备份与停写冷备、免安装运行时 | 当前任务不执行原机冷备、不部署、不启停原 HA |
| 诊断与验收 | 脱敏诊断、HA 真运行夹具、实际旧 SDK、回退和负向协议校验、App 验收观察器 | 模拟 amd64 不等于原生 x86；脚本 LAN 不等于智慧生活 App 直连 |
| 上游贡献 | 已提炼三提交的公共能力分支及第一批 PR 正文，规划后续引擎／配置／发布分批贡献 | 尚未 push、未创建远端 MR／PR；整合分支不宜整体作为第一批贡献 |

## 当前验证与发布缺口

- ARM Linux 与模拟 amd64 的脚本认证 LAN、控制读回、C 确认和重启已通过；原生 x86 没有独立主机，仍待实测。
- 智慧生活 App 的直连 LAN、空场景首次写入以及重启后首次写入，应以独立网络、实际 App 请求和唯一 HA 调用共同验收。最新进展见 [App 直连 LAN 记录](app-direct-lan-acceptance.md)，不回填历史结果。
- 正式发布还需要整理 C 源码路径、许可证、依赖和可分发边界，重新构建包含最新修复的安装包，单独验证新装路径。
- 原 HA 曾在此前获授权维护窗口升级到 b6、恢复旧网关和 32 个旧设备；后续 OpenHarmony 唯一引擎接管仍未执行。本轮不改其状态，旧冷备也不能直接替代未来窗口的新冷备。

## 文件级清单

以下为上述历史快照的全部 222 个差异文件；标记“公共批次”的文件也出现在干净贡献分支，但两分支的具体内容仍应分别审查。

### custom_components/hwhomebridge

| 文件 | + / - | 公共批次 |
| --- | ---: | :---: |
| [custom_components/hwhomebridge/__init__.py](../custom_components/hwhomebridge/__init__.py) | 138 / 18 |  |
| [custom_components/hwhomebridge/availability.py](../custom_components/hwhomebridge/availability.py) | 14 / 0 | 是 |
| [custom_components/hwhomebridge/backend_entries.py](../custom_components/hwhomebridge/backend_entries.py) | 45 / 0 |  |
| [custom_components/hwhomebridge/capability_registry.py](../custom_components/hwhomebridge/capability_registry.py) | 97 / 0 | 是 |
| [custom_components/hwhomebridge/capability_router.py](../custom_components/hwhomebridge/capability_router.py) | 305 / 0 | 是 |
| [custom_components/hwhomebridge/composition.py](../custom_components/hwhomebridge/composition.py) | 186 / 0 | 是 |
| [custom_components/hwhomebridge/config_flow.py](../custom_components/hwhomebridge/config_flow.py) | 189 / 16 |  |
| [custom_components/hwhomebridge/const.py](../custom_components/hwhomebridge/const.py) | 53 / 0 |  |
| [custom_components/hwhomebridge/diagnostics.py](../custom_components/hwhomebridge/diagnostics.py) | 45 / 0 |  |
| [custom_components/hwhomebridge/fan_mapping.py](../custom_components/hwhomebridge/fan_mapping.py) | 102 / 0 | 是 |
| [custom_components/hwhomebridge/gateway_deployment.py](../custom_components/hwhomebridge/gateway_deployment.py) | 168 / 0 |  |
| [custom_components/hwhomebridge/gateway_lifecycle.py](../custom_components/hwhomebridge/gateway_lifecycle.py) | 230 / 0 |  |
| [custom_components/hwhomebridge/gateway_router.py](../custom_components/hwhomebridge/gateway_router.py) | 113 / 0 |  |
| [custom_components/hwhomebridge/gateway_worker.py](../custom_components/hwhomebridge/gateway_worker.py) | 120 / 0 |  |
| [custom_components/hwhomebridge/ha_transport.py](../custom_components/hwhomebridge/ha_transport.py) | 77 / 0 | 是 |
| [custom_components/hwhomebridge/hwbridge.py](../custom_components/hwhomebridge/hwbridge.py) | 7 / 13 |  |
| [custom_components/hwhomebridge/identity_store.py](../custom_components/hwhomebridge/identity_store.py) | 131 / 0 |  |
| [custom_components/hwhomebridge/legacy_callbacks.py](../custom_components/hwhomebridge/legacy_callbacks.py) | 99 / 0 |  |
| [custom_components/hwhomebridge/manifest.json](../custom_components/hwhomebridge/manifest.json) | 5 / 2 |  |
| [custom_components/hwhomebridge/native_timer.py](../custom_components/hwhomebridge/native_timer.py) | 265 / 0 | 是 |
| [custom_components/hwhomebridge/openharmony/.gitignore](../custom_components/hwhomebridge/openharmony/.gitignore) | 1 / 0 |  |
| [custom_components/hwhomebridge/openharmony/README.md](../custom_components/hwhomebridge/openharmony/README.md) | 13 / 0 |  |
| [custom_components/hwhomebridge/openharmony/__init__.py](../custom_components/hwhomebridge/openharmony/__init__.py) | 5 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/adapters/default/20UC.json](../custom_components/hwhomebridge/openharmony/config/adapters/default/20UC.json) | 32 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/adapters/default/2151.json](../custom_components/hwhomebridge/openharmony/config/adapters/default/2151.json) | 48 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/adapters/default/28BF.json](../custom_components/hwhomebridge/openharmony/config/adapters/default/28BF.json) | 68 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/adapters/default/2KJ0.json](../custom_components/hwhomebridge/openharmony/config/adapters/default/2KJ0.json) | 48 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/adapters/default/2NAL.json](../custom_components/hwhomebridge/openharmony/config/adapters/default/2NAL.json) | 112 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/adapters/xiaomi/sflt11.json](../custom_components/hwhomebridge/openharmony/config/adapters/xiaomi/sflt11.json) | 59 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/compositions.json](../custom_components/hwhomebridge/openharmony/config/compositions.json) | 246 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/instances.example.json](../custom_components/hwhomebridge/openharmony/config/instances.example.json) | 32 / 0 |  |
| [custom_components/hwhomebridge/openharmony/config/product_registry.json](../custom_components/hwhomebridge/openharmony/config/product_registry.json) | 461 / 0 |  |
| [custom_components/hwhomebridge/openharmony/configuration.py](../custom_components/hwhomebridge/openharmony/configuration.py) | 38 / 0 |  |
| [custom_components/hwhomebridge/openharmony/deployment.py](../custom_components/hwhomebridge/openharmony/deployment.py) | 112 / 0 |  |
| [custom_components/hwhomebridge/openharmony/ha_backend.py](../custom_components/hwhomebridge/openharmony/ha_backend.py) | 110 / 0 |  |
| [custom_components/hwhomebridge/openharmony/router.py](../custom_components/hwhomebridge/openharmony/router.py) | 29 / 0 |  |
| [custom_components/hwhomebridge/openharmony/transport.py](../custom_components/hwhomebridge/openharmony/transport.py) | 118 / 0 |  |
| [custom_components/hwhomebridge/product_matcher.py](../custom_components/hwhomebridge/product_matcher.py) | 14 / 8 | 是 |
| [custom_components/hwhomebridge/product_registry.py](../custom_components/hwhomebridge/product_registry.py) | 41 / 1 | 是 |
| [custom_components/hwhomebridge/profile_bridge.py](../custom_components/hwhomebridge/profile_bridge.py) | 140 / 0 |  |
| [custom_components/hwhomebridge/role_binding.py](../custom_components/hwhomebridge/role_binding.py) | 45 / 0 | 是 |
| [custom_components/hwhomebridge/service_action.py](../custom_components/hwhomebridge/service_action.py) | 16 / 1 | 是 |
| [custom_components/hwhomebridge/service_router.py](../custom_components/hwhomebridge/service_router.py) | 29 / 0 | 是 |
| [custom_components/hwhomebridge/strings.json](../custom_components/hwhomebridge/strings.json) | 99 / 0 |  |
| [custom_components/hwhomebridge/translations/en.json](../custom_components/hwhomebridge/translations/en.json) | 55 / 4 |  |
| [custom_components/hwhomebridge/translations/zh-Hans.json](../custom_components/hwhomebridge/translations/zh-Hans.json) | 55 / 4 |  |
| [custom_components/hwhomebridge/validate_config.py](../custom_components/hwhomebridge/validate_config.py) | 7 / 0 | 是 |

### docs

| 文件 | + / - | 公共批次 |
| --- | ---: | :---: |
| [docs/fan-capabilities.md](../docs/fan-capabilities.md) | 45 / 0 | 是 |
| [docs/final-single-gateway-preparation.md](../docs/final-single-gateway-preparation.md) | 89 / 0 |  |
| [docs/gateway-availability-validation.md](../docs/gateway-availability-validation.md) | 62 / 0 |  |
| [docs/m3-b4-release-preflight.md](../docs/m3-b4-release-preflight.md) | 54 / 0 |  |
| [docs/native-lan-confirmation-validation.md](../docs/native-lan-confirmation-validation.md) | 66 / 0 |  |
| [docs/openharmony-baseline.md](../docs/openharmony-baseline.md) | 62 / 0 |  |
| [docs/openharmony-coexistence.md](../docs/openharmony-coexistence.md) | 78 / 0 |  |
| [docs/openharmony-direction-validation.md](../docs/openharmony-direction-validation.md) | 28 / 0 |  |
| [docs/openharmony-installation.md](../docs/openharmony-installation.md) | 118 / 0 |  |
| [docs/openharmony-x86-sdk-validation.md](../docs/openharmony-x86-sdk-validation.md) | 86 / 0 |  |
| [docs/plans/ha-existing-gateway-lifecycle.md](../docs/plans/ha-existing-gateway-lifecycle.md) | 77 / 0 |  |
| [docs/plans/legacy-identity-offline-import.md](../docs/plans/legacy-identity-offline-import.md) | 80 / 0 |  |
| [docs/plans/legacy-multi-arm-evaluation.md](../docs/plans/legacy-multi-arm-evaluation.md) | 71 / 0 |  |
| [docs/plans/legacy-native-audit.json](../docs/plans/legacy-native-audit.json) | 69 / 0 |  |
| [docs/plans/legacy-native-extension-gate.md](../docs/plans/legacy-native-extension-gate.md) | 38 / 0 |  |
| [docs/plans/legacy-native-public-source-research.json](../docs/plans/legacy-native-public-source-research.json) | 821 / 0 |  |
| [docs/plans/legacy-native-public-source-research.md](../docs/plans/legacy-native-public-source-research.md) | 100 / 0 |  |
| [docs/plans/openharmony-ha-integration-plan.md](../docs/plans/openharmony-ha-integration-plan.md) | 156 / 0 |  |
| [docs/plans/openharmony-single-engine-evaluation.md](../docs/plans/openharmony-single-engine-evaluation.md) | 103 / 0 |  |
| [docs/plans/production-cold-export.md](../docs/plans/production-cold-export.md) | 80 / 0 |  |
| [docs/plans/production-cold-maintenance-window.md](../docs/plans/production-cold-maintenance-window.md) | 103 / 0 |  |
| [docs/plans/production-maintenance-channel.md](../docs/plans/production-maintenance-channel.md) | 67 / 0 |  |
| [docs/plans/reusable-fan-core-validation.json](../docs/plans/reusable-fan-core-validation.json) | 91 / 0 |  |
| [docs/plans/reusable-fan-single-gateway-contribution-plan.md](../docs/plans/reusable-fan-single-gateway-contribution-plan.md) | 283 / 0 |  |
| [docs/plans/role-timer-composition-validation.json](../docs/plans/role-timer-composition-validation.json) | 129 / 0 |  |
| [docs/portable-cold-runtime.md](../docs/portable-cold-runtime.md) | 73 / 0 |  |
| [docs/production-identity-adoption.md](../docs/production-identity-adoption.md) | 82 / 0 |  |
| [docs/production-readonly-preflight.md](../docs/production-readonly-preflight.md) | 67 / 0 |  |
| [docs/pull-requests/01-reusable-capabilities.md](../docs/pull-requests/01-reusable-capabilities.md) | 31 / 0 | 是 |
| [docs/pull-requests/contribution-series.md](../docs/pull-requests/contribution-series.md) | 31 / 0 |  |
| [docs/pull-requests/core-validation-20261021.md](../docs/pull-requests/core-validation-20261021.md) | 14 / 0 | 是 |
| [docs/role-timer-compositions.md](../docs/role-timer-compositions.md) | 127 / 0 | 是 |

### experiments

| 文件 | + / - | 公共批次 |
| --- | ---: | :---: |
| [experiments/openharmony_bridge/.gitignore](../experiments/openharmony_bridge/.gitignore) | 9 / 0 |  |
| [experiments/openharmony_bridge/CMakeLists.txt](../experiments/openharmony_bridge/CMakeLists.txt) | 104 / 0 |  |
| [experiments/openharmony_bridge/bootstrap.py](../experiments/openharmony_bridge/bootstrap.py) | 504 / 0 |  |
| [experiments/openharmony_bridge/build-ha-static.sh](../experiments/openharmony_bridge/build-ha-static.sh) | 18 / 0 |  |
| [experiments/openharmony_bridge/build_manifest.py](../experiments/openharmony_bridge/build_manifest.py) | 61 / 0 |  |
| [experiments/openharmony_bridge/coap_alignment_test.c](../experiments/openharmony_bridge/coap_alignment_test.c) | 51 / 0 |  |
| [experiments/openharmony_bridge/coexistence-verification.json](../experiments/openharmony_bridge/coexistence-verification.json) | 125 / 0 |  |
| [experiments/openharmony_bridge/cold_export_acceptance.py](../experiments/openharmony_bridge/cold_export_acceptance.py) | 122 / 0 |  |
| [experiments/openharmony_bridge/direction-recovery-verification.json](../experiments/openharmony_bridge/direction-recovery-verification.json) | 169 / 0 |  |
| [experiments/openharmony_bridge/evidence/final-single-gateway-validation.json](../experiments/openharmony_bridge/evidence/final-single-gateway-validation.json) | 189 / 0 |  |
| [experiments/openharmony_bridge/evidence/gateway-availability-validation.json](../experiments/openharmony_bridge/evidence/gateway-availability-validation.json) | 100 / 0 |  |
| [experiments/openharmony_bridge/evidence/ha-existing-gateway-lifecycle.json](../experiments/openharmony_bridge/evidence/ha-existing-gateway-lifecycle.json) | 163 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-cold-export-routing.json](../experiments/openharmony_bridge/evidence/jev-cold-export-routing.json) | 100 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-cold-window-evidence.json](../experiments/openharmony_bridge/evidence/jev-cold-window-evidence.json) | 72 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-final-single-gateway-evidence.json](../experiments/openharmony_bridge/evidence/jev-final-single-gateway-evidence.json) | 80 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-gateway-availability.json](../experiments/openharmony_bridge/evidence/jev-gateway-availability.json) | 80 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-ha-lifecycle-routing.json](../experiments/openharmony_bridge/evidence/jev-ha-lifecycle-routing.json) | 111 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-host-key-append-routing.json](../experiments/openharmony_bridge/evidence/jev-host-key-append-routing.json) | 41 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-legacy-handover-routing.json](../experiments/openharmony_bridge/evidence/jev-legacy-handover-routing.json) | 39 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-legacy-multi-routing.json](../experiments/openharmony_bridge/evidence/jev-legacy-multi-routing.json) | 88 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-maintenance-routing.json](../experiments/openharmony_bridge/evidence/jev-maintenance-routing.json) | 68 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-native-lan-evidence.json](../experiments/openharmony_bridge/evidence/jev-native-lan-evidence.json) | 100 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-portable-cold-runtime.json](../experiments/openharmony_bridge/evidence/jev-portable-cold-runtime.json) | 49 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-production-adoption-evidence.json](../experiments/openharmony_bridge/evidence/jev-production-adoption-evidence.json) | 63 / 0 |  |
| [experiments/openharmony_bridge/evidence/jev-single-engine-evidence-selection.json](../experiments/openharmony_bridge/evidence/jev-single-engine-evidence-selection.json) | 42 / 0 |  |
| [experiments/openharmony_bridge/evidence/legacy-identity-handover-evaluation.json](../experiments/openharmony_bridge/evidence/legacy-identity-handover-evaluation.json) | 307 / 0 |  |
| [experiments/openharmony_bridge/evidence/legacy-multi-arm-evaluation.json](../experiments/openharmony_bridge/evidence/legacy-multi-arm-evaluation.json) | 243 / 0 |  |
| [experiments/openharmony_bridge/evidence/native-lan-confirmation-validation.json](../experiments/openharmony_bridge/evidence/native-lan-confirmation-validation.json) | 769 / 0 |  |
| [experiments/openharmony_bridge/evidence/portable-cold-runtime.json](../experiments/openharmony_bridge/evidence/portable-cold-runtime.json) | 110 / 0 |  |
| [experiments/openharmony_bridge/evidence/production-b6-deployment.json](../experiments/openharmony_bridge/evidence/production-b6-deployment.json) | 78 / 0 |  |
| [experiments/openharmony_bridge/evidence/production-cold-export.json](../experiments/openharmony_bridge/evidence/production-cold-export.json) | 96 / 0 |  |
| [experiments/openharmony_bridge/evidence/production-cold-window.json](../experiments/openharmony_bridge/evidence/production-cold-window.json) | 106 / 0 |  |
| [experiments/openharmony_bridge/evidence/production-host-authorized-preflight.json](../experiments/openharmony_bridge/evidence/production-host-authorized-preflight.json) | 86 / 0 |  |
| [experiments/openharmony_bridge/evidence/production-identity-offline.json](../experiments/openharmony_bridge/evidence/production-identity-offline.json) | 89 / 0 |  |
| [experiments/openharmony_bridge/evidence/production-maintenance-channel.json](../experiments/openharmony_bridge/evidence/production-maintenance-channel.json) | 118 / 0 |  |
| [experiments/openharmony_bridge/evidence/single-engine-evaluation.json](../experiments/openharmony_bridge/evidence/single-engine-evaluation.json) | 301 / 0 |  |
| [experiments/openharmony_bridge/fetch_huawei_ca.py](../experiments/openharmony_bridge/fetch_huawei_ca.py) | 35 / 0 |  |
| [experiments/openharmony_bridge/framework_catalog.py](../experiments/openharmony_bridge/framework_catalog.py) | 63 / 0 |  |
| [experiments/openharmony_bridge/framework_loader.py](../experiments/openharmony_bridge/framework_loader.py) | 15 / 0 |  |
| [experiments/openharmony_bridge/framework_test.py](../experiments/openharmony_bridge/framework_test.py) | 304 / 0 |  |
| [experiments/openharmony_bridge/gateway_app_acceptance.py](../experiments/openharmony_bridge/gateway_app_acceptance.py) | 138 / 0 |  |
| [experiments/openharmony_bridge/gateway_availability_ha_test.py](../experiments/openharmony_bridge/gateway_availability_ha_test.py) | 133 / 0 |  |
| [experiments/openharmony_bridge/gateway_availability_install_test.py](../experiments/openharmony_bridge/gateway_availability_install_test.py) | 78 / 0 |  |
| [experiments/openharmony_bridge/gateway_ha_acceptance.py](../experiments/openharmony_bridge/gateway_ha_acceptance.py) | 306 / 0 |  |
| [experiments/openharmony_bridge/gateway_ha_fixture.py](../experiments/openharmony_bridge/gateway_ha_fixture.py) | 70 / 0 |  |
| [experiments/openharmony_bridge/gateway_ha_sequence.py](../experiments/openharmony_bridge/gateway_ha_sequence.py) | 83 / 0 |  |
| [experiments/openharmony_bridge/gateway_install_acceptance.py](../experiments/openharmony_bridge/gateway_install_acceptance.py) | 70 / 0 |  |
| [experiments/openharmony_bridge/gateway_lan_acceptance.py](../experiments/openharmony_bridge/gateway_lan_acceptance.py) | 219 / 0 |  |
| [experiments/openharmony_bridge/ha_backend_trace_test.py](../experiments/openharmony_bridge/ha_backend_trace_test.py) | 130 / 0 |  |
| [experiments/openharmony_bridge/ha_child_availability_test.c](../experiments/openharmony_bridge/ha_child_availability_test.c) | 81 / 0 |  |
| [experiments/openharmony_bridge/ha_child_availability_test.py](../experiments/openharmony_bridge/ha_child_availability_test.py) | 141 / 0 |  |
| [experiments/openharmony_bridge/ha_child_smoke.py](../experiments/openharmony_bridge/ha_child_smoke.py) | 55 / 0 |  |
| [experiments/openharmony_bridge/ha_child_state_test.c](../experiments/openharmony_bridge/ha_child_state_test.c) | 22 / 0 |  |
| [experiments/openharmony_bridge/ha_children.c](../experiments/openharmony_bridge/ha_children.c) | 538 / 0 |  |
| [experiments/openharmony_bridge/ha_children.h](../experiments/openharmony_bridge/ha_children.h) | 14 / 0 |  |
| [experiments/openharmony_bridge/ha_coexistence_test.py](../experiments/openharmony_bridge/ha_coexistence_test.py) | 446 / 0 |  |
| [experiments/openharmony_bridge/ha_configflow_fixture.py](../experiments/openharmony_bridge/ha_configflow_fixture.py) | 69 / 0 |  |
| [experiments/openharmony_bridge/ha_m3_install_test.py](../experiments/openharmony_bridge/ha_m3_install_test.py) | 151 / 0 |  |
| [experiments/openharmony_bridge/ha_real_sdk_test.py](../experiments/openharmony_bridge/ha_real_sdk_test.py) | 247 / 0 |  |
| [experiments/openharmony_bridge/identity_state_probe.c](../experiments/openharmony_bridge/identity_state_probe.c) | 105 / 0 |  |
| [experiments/openharmony_bridge/legacy_category_check.py](../experiments/openharmony_bridge/legacy_category_check.py) | 77 / 0 |  |
| [experiments/openharmony_bridge/legacy_category_fixture.py](../experiments/openharmony_bridge/legacy_category_fixture.py) | 108 / 0 |  |
| [experiments/openharmony_bridge/legacy_identity_import.py](../experiments/openharmony_bridge/legacy_identity_import.py) | 108 / 0 |  |
| [experiments/openharmony_bridge/legacy_identity_import_test.py](../experiments/openharmony_bridge/legacy_identity_import_test.py) | 201 / 0 |  |
| [experiments/openharmony_bridge/legacy_sn_probe.c](../experiments/openharmony_bridge/legacy_sn_probe.c) | 36 / 0 |  |
| [experiments/openharmony_bridge/linux_network.c](../experiments/openharmony_bridge/linux_network.c) | 94 / 0 |  |
| [experiments/openharmony_bridge/m2-verification.json](../experiments/openharmony_bridge/m2-verification.json) | 389 / 0 |  |
| [experiments/openharmony_bridge/m2-x86-lima.yaml](../experiments/openharmony_bridge/m2-x86-lima.yaml) | 31 / 0 |  |
| [experiments/openharmony_bridge/m3-b4-verification.json](../experiments/openharmony_bridge/m3-b4-verification.json) | 292 / 0 |  |
| [experiments/openharmony_bridge/m3-production-preflight.json](../experiments/openharmony_bridge/m3-production-preflight.json) | 180 / 0 |  |
| [experiments/openharmony_bridge/m3-verification.json](../experiments/openharmony_bridge/m3-verification.json) | 235 / 0 |  |
| [experiments/openharmony_bridge/main.c](../experiments/openharmony_bridge/main.c) | 387 / 0 |  |
| [experiments/openharmony_bridge/multi-arm-lima.yaml](../experiments/openharmony_bridge/multi-arm-lima.yaml) | 31 / 0 |  |
| [experiments/openharmony_bridge/poc_network.h](../experiments/openharmony_bridge/poc_network.h) | 6 / 0 |  |
| [experiments/openharmony_bridge/portable_cold_acceptance.py](../experiments/openharmony_bridge/portable_cold_acceptance.py) | 183 / 0 |  |
| [experiments/openharmony_bridge/prepare_coexistence_fixture.py](../experiments/openharmony_bridge/prepare_coexistence_fixture.py) | 72 / 0 |  |
| [experiments/openharmony_bridge/prepare_test_assets.py](../experiments/openharmony_bridge/prepare_test_assets.py) | 43 / 0 |  |
| [experiments/openharmony_bridge/prepare_upgrade_fixture.py](../experiments/openharmony_bridge/prepare_upgrade_fixture.py) | 62 / 0 |  |
| [experiments/openharmony_bridge/single_engine_control_check.py](../experiments/openharmony_bridge/single_engine_control_check.py) | 75 / 0 |  |
| [experiments/openharmony_bridge/single_engine_fixture.py](../experiments/openharmony_bridge/single_engine_fixture.py) | 263 / 0 |  |
| [experiments/openharmony_bridge/single_engine_identity_check.py](../experiments/openharmony_bridge/single_engine_identity_check.py) | 96 / 0 |  |
| [experiments/openharmony_bridge/single_engine_native_check.py](../experiments/openharmony_bridge/single_engine_native_check.py) | 67 / 0 |  |
| [experiments/openharmony_bridge/single_engine_owner.py](../experiments/openharmony_bridge/single_engine_owner.py) | 233 / 0 |  |
| [experiments/openharmony_bridge/sources.lock.json](../experiments/openharmony_bridge/sources.lock.json) | 23 / 0 |  |
| [experiments/openharmony_bridge/speke_vectors.c](../experiments/openharmony_bridge/speke_vectors.c) | 73 / 0 |  |
| [experiments/openharmony_bridge/virtual_child.c](../experiments/openharmony_bridge/virtual_child.c) | 273 / 0 |  |
| [experiments/openharmony_bridge/virtual_child.h](../experiments/openharmony_bridge/virtual_child.h) | 15 / 0 |  |
| [experiments/openharmony_bridge/virtual_child_smoke.py](../experiments/openharmony_bridge/virtual_child_smoke.py) | 74 / 0 |  |
| [experiments/openharmony_bridge/virtual_child_state_test.c](../experiments/openharmony_bridge/virtual_child_state_test.c) | 22 / 0 |  |
| [experiments/openharmony_bridge/zig-cc.sh](../experiments/openharmony_bridge/zig-cc.sh) | 4 / 0 |  |

### tests

| 文件 | + / - | 公共批次 |
| --- | ---: | :---: |
| [tests/fixtures/capabilities/adapters/default/fixture-fan.json](../tests/fixtures/capabilities/adapters/default/fixture-fan.json) | 132 / 0 | 是 |
| [tests/fixtures/capabilities/adapters/default/fixture-light.json](../tests/fixtures/capabilities/adapters/default/fixture-light.json) | 48 / 0 | 是 |
| [tests/fixtures/capabilities/adapters/example/fan.json](../tests/fixtures/capabilities/adapters/example/fan.json) | 10 / 0 | 是 |
| [tests/fixtures/capabilities/compositions.json](../tests/fixtures/capabilities/compositions.json) | 15 / 0 | 是 |
| [tests/fixtures/capabilities/product_registry.json](../tests/fixtures/capabilities/product_registry.json) | 216 / 0 | 是 |
| [tests/framework_imports.py](../tests/framework_imports.py) | 17 / 0 | 是 |
| [tests/ha_capability_integration.py](../tests/ha_capability_integration.py) | 154 / 0 | 是 |
| [tests/test_capability_availability.py](../tests/test_capability_availability.py) | 41 / 0 | 是 |
| [tests/test_diagnostics.py](../tests/test_diagnostics.py) | 48 / 0 |  |
| [tests/test_fan_capabilities.py](../tests/test_fan_capabilities.py) | 250 / 0 | 是 |
| [tests/test_gateway_availability.py](../tests/test_gateway_availability.py) | 171 / 0 |  |
| [tests/test_gateway_identity.py](../tests/test_gateway_identity.py) | 92 / 0 |  |
| [tests/test_gateway_lifecycle.py](../tests/test_gateway_lifecycle.py) | 141 / 0 |  |
| [tests/test_profile_bridge.py](../tests/test_profile_bridge.py) | 91 / 0 |  |
| [tests/test_role_timer_composition.py](../tests/test_role_timer_composition.py) | 500 / 0 | 是 |

### tools

| 文件 | + / - | 公共批次 |
| --- | ---: | :---: |
| [tools/.gitignore](../tools/.gitignore) | 2 / 0 |  |
| [tools/audit_legacy_native.py](../tools/audit_legacy_native.py) | 97 / 0 |  |
| [tools/build_cold_runtime.py](../tools/build_cold_runtime.py) | 126 / 0 |  |
| [tools/cold_runtime.lock.json](../tools/cold_runtime.lock.json) | 32 / 0 |  |
| [tools/cold_runtime_entry.py](../tools/cold_runtime_entry.py) | 58 / 0 |  |
| [tools/ha_binding_freeze.py](../tools/ha_binding_freeze.py) | 198 / 0 |  |
| [tools/ha_cloud_endpoint.py](../tools/ha_cloud_endpoint.py) | 103 / 0 |  |
| [tools/ha_cold_export.py](../tools/ha_cold_export.py) | 413 / 0 |  |
| [tools/ha_entity_preflight.py](../tools/ha_entity_preflight.py) | 332 / 0 |  |
| [tools/ha_gateway_prepare.py](../tools/ha_gateway_prepare.py) | 267 / 0 |  |
| [tools/ha_gateway_verify.py](../tools/ha_gateway_verify.py) | 79 / 0 |  |
| [tools/ha_host_preflight.py](../tools/ha_host_preflight.py) | 120 / 0 |  |
| [tools/ha_host_probe.sh](../tools/ha_host_probe.sh) | 44 / 0 |  |
| [tools/ha_legacy_binding_audit.py](../tools/ha_legacy_binding_audit.py) | 80 / 0 |  |
| [tools/ha_maintenance_review.py](../tools/ha_maintenance_review.py) | 72 / 0 |  |
| [tools/ha_online_backup.py](../tools/ha_online_backup.py) | 265 / 0 |  |
| [tools/ha_paired_identity_import.py](../tools/ha_paired_identity_import.py) | 143 / 0 |  |
| [tools/ha_production_readonly.py](../tools/ha_production_readonly.py) | 337 / 0 |  |
| [tools/ha_runtime_preflight.py](../tools/ha_runtime_preflight.py) | 57 / 0 |  |
| [tools/ha_upgrade.py](../tools/ha_upgrade.py) | 475 / 0 |  |
| [tools/ha_x86_host_preflight.py](../tools/ha_x86_host_preflight.py) | 47 / 0 |  |
| [tools/native_probes/header_layout.c](../tools/native_probes/header_layout.c) | 8 / 0 |  |
| [tools/native_probes/sdk_link.c](../tools/native_probes/sdk_link.c) | 3 / 0 |  |
| [tools/probe_legacy_native_symbols.py](../tools/probe_legacy_native_symbols.py) | 53 / 0 |  |
| [tools/requirements-native-audit.txt](../tools/requirements-native-audit.txt) | 2 / 0 |  |
| [tools/test_audit_legacy_native.py](../tools/test_audit_legacy_native.py) | 41 / 0 |  |
| [tools/test_cold_runtime.py](../tools/test_cold_runtime.py) | 68 / 0 |  |
| [tools/test_ha_binding_freeze.py](../tools/test_ha_binding_freeze.py) | 92 / 0 |  |
| [tools/test_ha_cloud_endpoint.py](../tools/test_ha_cloud_endpoint.py) | 23 / 0 |  |
| [tools/test_ha_cold_export.py](../tools/test_ha_cold_export.py) | 367 / 0 |  |
| [tools/test_ha_gateway_prepare.py](../tools/test_ha_gateway_prepare.py) | 113 / 0 |  |
| [tools/test_ha_host_preflight.py](../tools/test_ha_host_preflight.py) | 126 / 0 |  |
| [tools/test_ha_maintenance_review.py](../tools/test_ha_maintenance_review.py) | 92 / 0 |  |
| [tools/test_ha_online_backup.py](../tools/test_ha_online_backup.py) | 139 / 0 |  |
| [tools/test_ha_paired_identity_import.py](../tools/test_ha_paired_identity_import.py) | 80 / 0 |  |
| [tools/test_ha_production_readonly.py](../tools/test_ha_production_readonly.py) | 150 / 0 |  |
| [tools/test_ha_upgrade.py](../tools/test_ha_upgrade.py) | 222 / 0 |  |
| [tools/test_ha_x86_host_preflight.py](../tools/test_ha_x86_host_preflight.py) | 26 / 0 |  |

## 未提交及私有内容

快照时另有 **35 个未跟踪的旧实验文件**，不属于上面 222 个差异文件，也未自动加入本轮提交。它们主要是早期配对、Mac 转发、Linux REST 桥接、容器打包和探针；是否保留应逐个审查。`__pycache__` 是运行缓存。

- `experiments/openharmony_bridge/.containerignore`
- `experiments/openharmony_bridge/Containerfile`
- `experiments/openharmony_bridge/HA_CONFIG_FLOW.md`
- `experiments/openharmony_bridge/HA_PAIRED_GATEWAY.md`
- `experiments/openharmony_bridge/LINUX.md`
- `experiments/openharmony_bridge/README.md`
- `experiments/openharmony_bridge/framework_bridge.py`
- `experiments/openharmony_bridge/ha_bridge.py`
- `experiments/openharmony_bridge/ha_bridge_test.py`
- `experiments/openharmony_bridge/ha_configflow_test.py`
- `experiments/openharmony_bridge/ha_paired_container.service`
- `experiments/openharmony_bridge/ha_paired_gateway.py`
- `experiments/openharmony_bridge/ha_paired_guard_test.py`
- `experiments/openharmony_bridge/ha_paired_handover.py`
- `experiments/openharmony_bridge/lan_relay.py`
- `experiments/openharmony_bridge/lima.yaml`
- `experiments/openharmony_bridge/linux_bundle.py`
- `experiments/openharmony_bridge/linux_bundle_test.py`
- `experiments/openharmony_bridge/linux_fan_check.py`
- `experiments/openharmony_bridge/linux_runtime.py`
- `experiments/openharmony_bridge/linux_verify.py`
- `experiments/openharmony_bridge/native_probe.py`
- `experiments/openharmony_bridge/native_wire_probe.c`
- `experiments/openharmony_bridge/prepare_framework_catalog.py`
- `experiments/openharmony_bridge/prepare_ha_children.py`
- `experiments/openharmony_bridge/prepare_image.py`
- `experiments/openharmony_bridge/prepare_pairing.py`
- `experiments/openharmony_bridge/prepare_probe.py`
- `experiments/openharmony_bridge/qemu-run.sh`
- `experiments/openharmony_bridge/sdk_smoke.py`
- `experiments/openharmony_bridge/smoke_test.py`
- `experiments/openharmony_bridge/speke_reference.py`
- `experiments/openharmony_bridge/stage2_products.json`
- `experiments/openharmony_bridge/udp_agent.py`
- `experiments/openharmony_bridge/verification.json`

配对资料、HA token、PIN、AC、证书、生产备份、原始家庭清单、App 截图、SDK 私有 profile 和本地编译二进制留在受保护的本机目录或忽略目录，不在贡献材料中。已有闭源 SDK 的分发权限须沿用上游规则另行确认，不能把“本机能运行”当作“可公开发布”。

Jev 对 32 个 Python 模块的主要职责做了初步分类；Codex 对低置信度项及所有归类复核，按实际代码整理这份清单。模型原始分类及复核分类见 [分类记录](plans/change-inventory-classification.json)，不新增集成的 AI 运行依赖。
