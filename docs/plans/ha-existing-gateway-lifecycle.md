# 同一网关的 HA 后端生命周期

日期：2026-10-17。沿用原 `hwhomebridge` 配置条目、父网关及旧子设备身份。家庭 HA、本来的桥接进程、灯光和米家自动化不在本轮部署范围。

## 已接入的公共入口

原条目的「配置」增加「接管当前网关 / 后端切换」，提供原 HiLink SDK、OpenHarmony、停用三个显式选择。接管目录限定在 HA 配置目录的 `.storage/hwhomebridge/gateways/<目录>`。保留原 entry_id、unique_id、其他 data/options，以及桥接设备的原标识 `(hwhomebridge, entry_id, sn)`。目录和身份不能被第二个条目重复占用。

不填写接管目录的旧配置继续使用原入口。旧 SDK 如果已经加载进 HA 进程，新选择先保存并提示重启；不会在同一进程里假装完成卸载。重启后，托管模式下两种 SDK 都只在各自子进程运行，HA 只保留公共业务路由。

`gateway_deployment.py` 验证原 SDK 格式、来源 MAC、原 AC/SN、云端父身份及五组授权字段摘要、每条旧子身份、完整目录、映射和 profile。原生身份探针还会实际读取 OpenHarmony 存储。C 程序的接管模式在启动 SDK 前再次确认已有云端身份；缺失或错误时退出，不创建新父网关。profile 与配对资料仍属私有文件，不随源码或通用安装包发行。

`gateway_router.py` 继续使用原产品注册表、厂家适配器、VirtualDevice、SNManager、ServiceRouter，并接上角色、组合和设备原生定时。旧品类通过公共 profile 投影保留原服务。实体归属取实际 HA 实体注册表；缺失、禁用、排除的原设备拒绝接管，避免换引擎意外重新启用。不可用状态不会被当成关灯上报。

## 启停和回退规则

父进程持有唯一所有者文件锁，子进程继承锁；Linux 父进程退出保护终止所有者。切换前校验目标，停止并等待旧进程退出，再启动新进程。准备中及最后成功引擎写入经 fsync 的事务记录。启动成功以实际已配对 SDK 上线为准。

OpenHarmony 还必须收到目录中全部子设备的上线成功回执。文件锁只覆盖本机同一接管目录，不能阻止另一台机器运行同一份身份；生产接管前必须先冷停原所有者。首次接管先选择原 SDK，确认上线并建立成功记录，再显式切换 OpenHarmony；没有成功记录时不能承诺自动回退。

新引擎启动失败时，只能恢复同一身份已验证的上一引擎。OpenHarmony 意外退出时尝试旧 SDK；旧 SDK 也失败则停用并保留配对文件，不循环创建身份。回退原因保留到 HA 重启后，用户重新显式选择才重试。配置页同时显示保存选择、实际运行引擎和失败引擎。

停用、卸载条目、HA 正常关闭均停止自有子进程，保留配对文件，不调用解绑或清空接口。删除配置之后，若要恢复原 entry_id，须恢复原配置条目备份，不能用重新添加来冒充原条目。

旧 SDK 的已验证回退仅覆盖 amd64 指定 SHA256，且要求原网卡 MAC；其他版本或 ARM 旧库会明确拒绝。ARM 的 OpenHarmony 单引擎能力已有上一轮独立 Linux 实测，但本轮跨引擎正式 HA 生命周期验收以 x86_64 为准。回退旧 SDK 时保留原 001–008 能力，新风扇补充卡需要重新选择 OpenHarmony。

## 私有目录合同

目录包含 `deployment.json`、`gateway.local.json`、`bindings.json`、`instances.json`、`mapping/`、`profiles/`、本机架构 `bridge_poc`/`identity_state_probe`、原 SDK 冷副本 `legacy/`、已导入身份 `runtime/` 及 v2 导入回执。目录 0700、普通文件 0600、程序 0700，归 HA 运行用户所有。`deployment.json` 固定原 unique_id、来源 MAC、监听接口/端口、程序及输入文件摘要。

公共密码格式实现已提到 `identity_store.py`，小栈缓存回调提到 `legacy_callbacks.py`。实验生成器 `gateway_ha_fixture.py` 只接受标记为虚构设备的冷副本，不能拿来直接生成家庭生产迁移目录。生产导出器、真实实体清单复核及授权文件发行范围仍是部署前的下一道门槛；本轮没有读取或更改家庭配对文件。

## 安装与还原

发布版本为 `1.1.0b5`。通用保留式升级包包含公共 Python、翻译及两种 Linux 架构静态程序和身份探针，继续排除并保留原 `config/`、`hilink_bridge/`、`profile/`。`.storage` 内配对目录不由文件升级工具改写，也不包含在普通组件备份中；需另行保全完整私有身份目录。

```sh
python3 tools/ha_upgrade.py build --repo "$REPO" --output "$NEW_OUTPUT" \
  --native-x86_64 "$X86_BUILD" --native-aarch64 "$ARM_BUILD"
# 以下只在已停止的独立目标HA上执行；工具不会启动或停止HA。
python3 ha_upgrade.py backup --config "$HA_CONFIG" --container "$HA_CONTAINER" --backup "$NEW_BACKUP"
python3 ha_upgrade.py preflight --config "$HA_CONFIG" --container "$HA_CONTAINER" \
  --package "$PACKAGE" --sha256 "$PACKAGE_SHA256"
python3 ha_upgrade.py install --config "$HA_CONFIG" --container "$HA_CONTAINER" \
  --backup "$NEW_BACKUP" --package "$PACKAGE" --sha256 "$PACKAGE_SHA256"
# 目标停止后可还原组件；配置已改变时，显式恢复原条目。
python3 ha_upgrade.py restore --config "$HA_CONFIG" --container "$HA_CONTAINER" \
  --backup "$NEW_BACKUP" --restore-entries
```

本轮验证的是在已配置旧条目的独立 HA 上安装升级和还原。此包不是空目录全新安装包；原项目完整目录安装方式继续保留。源代码构建、原生架构和发行授权检查也不能替代家庭 HA 实际验收。

## 独立验收

使用已配对的虚拟父网关、八类虚拟旧子设备和七张风扇补充卡；只允许 `oh_single_*` 虚构实体。实际 HA Core 2026.10.1、真实 amd64 旧 SDK、真实 OpenHarmony 静态程序，不通过 Mac、SSH 或 HA REST 代理承接控制。

测试入口为 `gateway_ha_acceptance.py` 和 `gateway_install_acceptance.py`。[脱敏验收记录](../../experiments/openharmony_bridge/evidence/ha-existing-gateway-lifecycle.json)已记录最终 b5 包的通过结果：

- 保留原配置条目及 unique_id，旧八类桥接设备复用原 HA 注册表 ID；新增第二网关入口被拒绝。
- 原 SDK 上线后切换 OpenHarmony，全部 15 个子设备收到上线成功回执；虚构旧灯开关和风扇档位经实际 HA 服务写入并读回。
- 显式停用、重新加载、HA 自带禁用/启用均保留身份并回收自有进程；旧 SDK 从未加载进 HA 父进程。
- 杀死启动中的候选进程、杀死运行中的 OpenHarmony 均回退同一身份的旧 SDK；HA 冷启动保留失败选择，显式重试才重新启动 OpenHarmony。
- OpenHarmony 正常冷启动、外部 SIGKILL HA 后原生所有者退出、再冷启动和风扇读写通过。
- b4→b5 的真实保留式安装、文件替换故障自动还原、显式还原 b4 及还原后实际 HA 启动通过。原配置、旧库、profile、其他集成条目保留；SDK KV 未被文件还原覆盖。
- 最后对原始冷副本全局核对，根身份、五组授权字段及旧、新子身份不变。测试容器全部停止。

公共 60、工具 42、离线导入 8、实验框架 25 项，共 135 项通过，配置校验通过。两架构静态构建及包内 ELF/来源摘要检查通过。本轮未重新操作智慧生活 App 或真实家电，不把 HTTP 路由写读当作本轮硬件验收。

最终测试包 `hwhomebridge-1.1.0b5.zip`，SHA256：`e2492d1a9d73579b75e1fa21b455a69dd768fdc9f89dadc9db127184e6df6962`。包内 48 个公共源码文件与最终工作区逐字节一致，另有两架构程序、探针与构建清单共 6 文件；构建清单中的 Git revision 表示构建时的基线提交，具体文件摘要才是本次未提交源码的证据。

在已核对的虚拟目录和测试容器中，`gateway_ha_sequence.py` 串行启动实际 HA 进程：回退后的冷启动、显式重试、OpenHarmony 冷启动、外部 SIGKILL HA、再次冷启动恢复。它检查测试目录挂载和进程命令后才注入故障，不查找或停止其他 HA。文件还原后再用 `restored` 阶段确认原组件及原停用条目可加载；还原不会覆盖最新 SDK KV。

Jev 本轮只将条目、进程所有权、失败回退、安装四类缺口归类，见[脱敏分类结果](../../experiments/openharmony_bridge/evidence/jev-ha-lifecycle-routing.json)；身份密码格式、生命周期实现、故障判定和验收由 Codex 核对。

下一步先补生产冷备导出与清单预检，把原条目、旧子身份、实际实体角色和自定义适配完整导出；再补全量安装/HACS 升级和目标架构的发行验收。获得维护窗口后才执行原 HA 接管及真实灯光、风扇验收。本轮未访问原 HA、未改动现有桥接或米家自动化，也没有把实验父身份替换到生产。

后续进展：[生产冷备导出与真实清单预检](production-cold-export.md)的工具已完成独立 Linux 验证。原 HA 仅做只读采集，32 个原设备可识别，书房 13 个角色及 7 张补充卡状态投影通过；旧灯映射歧义和不可用灯组仍需处理。实际生产冷备、正式父 SN/原生目录、安装及 App 验收尚未进行，不沿用实验身份绕过这些门槛。
