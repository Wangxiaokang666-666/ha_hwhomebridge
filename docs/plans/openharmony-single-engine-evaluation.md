# OpenHarmony 唯一引擎隔离评估

核查日期：2026-10-17。**单引擎共用网关，以及旧 SDK 独立测试身份的接管、冷重启和回退闭环均已通过。** 本轮新增结果见[旧身份导入记录](legacy-identity-offline-import.md)，首次覆盖一盏虚拟旧灯；后续[多子设备与 ARM 实测](legacy-multi-arm-evaluation.md)已扩展旧001–008和同一父身份的15张子卡，正式 HA 生命周期迁移仍未验收。 因此还不能替换原 HA 的网关，也没有生成可以承诺无损迁移的正式安装包。

机器记录：[上轮单引擎评估](../../experiments/openharmony_bridge/evidence/single-engine-evaluation.json)、[本轮身份接管与回退](../../experiments/openharmony_bridge/evidence/legacy-identity-handover-evaluation.json)。原 SDK 的[源码/扩展调查](legacy-native-public-source-research.md)仍有效；旧库扩展门槛没有因为另一引擎的新身份实验通过而变成通过。

## 已验证的单引擎范围

实验 A 使用全新 `OHPOC-` 父身份，唯一的原生进程是自编 OpenHarmony C；HA Core 宿主仅提供公共业务路由。旧 SDK 没有在这个父身份下加载。两个实验身份 A/B 分时启动，用于不同验证，不是交付两个网关。

数据通道为：智慧生活 App → 华为云 → OpenHarmony C → Linux 回环 HTTP → 公共 ServiceRouter/CapabilityRouter → HA Core 服务 → 本机虚构实体。没有家庭 HA 客户端、生产 token、真实实体或米家自动化。发现配对时暂用 Mac 到 VM 的 UDP 转发；实际 App 控制和整组重启时转发已停止。

| 项目 | 当前证据 | 边界 |
| --- | --- | --- |
| 旧灯保留 | 公共框架 `002`、云端 PID `9MU7`；保留旧 VirtualDevice 对象、原 HB 序列号算法和已有 SN | 证明公共框架身份保留，不是旧 SDK 云端凭据迁移 |
| 唯一父网关 | 八张业务子卡均在同一 OpenHarmony 父会话下注册、同步、上线；另有一张最初阶段的虚拟开关 | 共九份子身份文件，不是九个根网关 |
| 旧灯 App 控制 | 开、关、亮度、色温均有真实 C 写入确认和虚拟 HA 回读；色温 App 显示与 HA 同为 6491K | 所控均为虚拟灯 |
| 新风扇 App 控制 | 开、关及重启后的控制有 C 写入确认和虚拟 HA 状态变化 | 其余风扇能力见下一行的本地 C 验证 |
| 六档、自然风、方向、定时、灯光组合 | 真实 C 回调经公共路由到 HA 服务，验证档位 1–6、自然风、两种方向、设备定时及取消、氛围灯、设置、效果 | 此检查本身不启动 SDK 或绑定云端；定时由假设备模拟，没有验收真实风扇/到期动作 |
| 整组重启 | HA 宿主及 C 进程停止后重启，根云端 ID、九份子身份、虚拟设备状态一致；没有 PIN 回调或新增子绑定 | 仅 A 的 OpenHarmony 自有配对格式 |
| x86_64 Linux | 六个静态 ELF 构建成功，C 身份/路由/离线存储探针实际运行；App 云端验收 | QEMU 中的独立 Linux，不是原 HA |
| aarch64 | 当前源码交叉构建六个静态 ELF 成功 | 本轮没有启动已连接家庭 HA 的 ARM 实验 VM，没有新的 ARM Linux/App 运行验收 |

私有目录里的组合配置把旧主灯作为 primary，移除补充卡中重复的主灯开关。生产配置、默认旧 SDK 后端、001–008框架和灯光代码都未修改。此次仅验证 `002` 旧灯品类，没有覆盖全部旧产品。

### 桥接协议类型的实际差异

旧灯第一次以 `protType=1` 注册，云端返回 `failureList.errcode=15013`。保持同一 SN、PID、型号等资料，仅改为桥接子设备所用的 `protType=6` 后，注册、服务同步、上线成功；App 设备信息中的 SN 与公共框架摘要核对一致。

没有推断 15013 的官方含义。这个对照说明：不能只因 profile 标记 WiFi 就把桥接子设备的 transport type 机械设为 1。正式产品目录需要明确记录桥接类型，并逐类验收。

## 旧 SDK 身份 B：上轮阻断记录

以下记录保留上轮失败现场；后续的崩溃修复、离线转换和在线接管见[旧身份导入记录](legacy-identity-offline-import.md)。

另建全新 B 根 SN、48 字节 AC、临时 PIN 和运行目录。旧 SDK 保留了预置测试 SN，智慧生活完成发现、PIN 配对，SDK 状态到 1；测试灯卡也出现在 App。没有使用生产配对资料，也没有解绑任何现有设备。

冷备份后，旧 SDK 可从自己的文件恢复到状态 1，没有再次请求 PIN，`config.cfg`、SN 和 AC 文件保持一致。**没有取得可核对的旧根云端 device ID，因此这只是旧 SDK 自身的在线恢复证据。**

实验 B 的子设备 App 控制没有通过：回调日志出现空 SID，SDK 日志同时出现服务表转换 `svcNum=0`，随后进程退出 139。实验封装补上空指针和 Python 回调异常保护后仍能复现，空对象返回也没有解决；没有定位到完整根因，不能据此断言生产 SDK 同样存在这个问题。最终封装对未知 SID 返回失败，不编造状态。B 的卡片出现、SDK 在线和注册调用已提交，都没有算作控制成功或可靠回退。

### 离线存储检查

新增 `identity_state_probe` 调用公开 OpenHarmony SDK 的 ModelDevInfo、SecurityStore、ConfigInfo，注册真实 UDID/AC 回调后，只在一次性副本上读取根身份。它不启动 SDK 任务或网络，只输出读取状态和身份摘要。

| 输入副本 | 结果 |
| --- | --- |
| A 自有 `dev_data`/`conn_data` + 正确资料 | 加载已绑定身份成功 |
| 同一 A 数据，AC 改一个字节 | 验证失败，没有加载身份 |
| 同一 A 数据，存储损坏 | 拒绝读取 |
| B 旧 SDK 原文件名 | 没有加载已绑定身份；读取返回 0 也不能当成功，缺少文件会走默认状态 |
| B 的 `config.cfg` 复制为 `dev_data` | 格式校验失败，没有加载身份 |

旧 SDK 的二进制 `config.cfg` 为 1588 字节，`bridge.cfg` 为 5192 字节；A 的 OpenHarmony 配对存储为 `dev_data` 568 字节、`conn_data` 184 字节。文件长度和命名差异只能证明不能直接复制，不能证明不存在转换方法。上轮没有经过验证的旧格式解码器，未按猜测的字段偏移转换，也没有启动 OpenHarmony 以旧 SN 重新配对。

上轮的 `single_engine_owner.py` 明确拒绝从含非空旧 `config.cfg`/备份的目录启动 OpenHarmony。已在 B 上验证拒绝发生在 C 程序启动前，测试容器无网络，旧配对文件不变。之后重新启动原来的 B 旧 SDK，恢复在线；完整子控制回退仍失败，不能标记接管、回退完成。

## 可复用实验入口

这些脚本在实验分支，**不是生产安装器、迁移器或自动发现家庭设备的入口**。需要现有 OpenHarmony 依赖/补丁、另行取得的合法 profile（此实验需本地 `evidence/9MU7-profile.json`）、本地根证书以及已有公共框架。profile 和配对文件不随本提交发行。

```sh
# 构建六个静态产物；包含不启动网络的身份存储探针。
sh experiments/openharmony_bridge/build-ha-static.sh x86_64
sh experiments/openharmony_bridge/build-ha-static.sh aarch64

# 新目录、全新测试身份；不要传原 HA 或已配对的运行目录。
python3 experiments/openharmony_bridge/single_engine_fixture.py prepare "$NEW_LAB"

# 在独立 Linux / HA 镜像中先启动虚拟业务宿主，检查回环请求确实就绪。
python3 -B experiments/openharmony_bridge/single_engine_fixture.py serve "$NEW_LAB"

# 另一个进程只运行一个引擎；正常终止不调用 SDK 解绑接口。
python3 -B experiments/openharmony_bridge/single_engine_owner.py \
  openharmony "$NEW_LAB" "$STATIC_BUILD/bridge_poc"

# 不启动 SDK 的本地 C 边界和控制路由检查；后者需要上面的虚拟宿主。
python3 -B experiments/openharmony_bridge/single_engine_native_check.py "$STATIC_BUILD/ha_child_state_test"
python3 -B experiments/openharmony_bridge/single_engine_control_check.py \
  "$STATIC_BUILD/ha_child_state_test" "$NEW_LAB/runtime/ha-children.local.json"

# 两份独立的、已停止的配对冷副本；只探测、不转换、不启动网络。
python3 -B experiments/openharmony_bridge/single_engine_identity_check.py \
  "$STATIC_BUILD/identity_state_probe" "$OPEN_PAIRED_COPY" "$LEGACY_PAIRED_COPY"
```

本轮容器采用只读根文件系统、源代码白名单只读挂载和单独 `/lab`；SDK 需要云端网络，业务 HTTP 只绑定 Linux 回环地址。宿主使用 HA Core `2026.10.1` / Python `3.14.6`。不是独立 HA 的配置流安装验收。启动程序通过文件锁拒绝同一根目录的第二个所有者，C 层仍要求测试根 `OHPOC-` 前缀。

上轮新增的 C 目录允许显式子 SN，校验类型、字符、长度、重复和已保存身份的一致性；没有显式 SN 时仍用原实验算法。这个能力使旧 VirtualDevice 的 SN 可以进入新引擎，但不导入旧云端 child ID、UDID 或鉴权资料。把它当作旧绑定迁移工具是不正确的。

## 上轮列出的交付门槛

本节保留当时的门槛顺序，完成状态以[后续导入记录](legacy-identity-offline-import.md)为准。

1. **先解决旧测试身份资料的读取合同。** 明确旧 SDK 文件版本、结构、完整性/加密方式及凭据语义；可以继续查 SDK 同版本公开资料或向维护者取得持久化合同。用 B 冷副本建立经过正反例校验的离线解码/导入器，同时核对旧根与子设备的云端 ID、SN、UDID、型号、MAC、父关系。没有身份对照就继续禁止在线接管。
2. **先补齐旧 SDK 实验基线。** 定位 B 的服务表/空 SID/退出 139，确认实验回调合同并取得 App 控制、读回、冷重启成功证据。这个基线不可靠时，不能承诺回退可用。
3. **再验证一次迁移循环。** 停旧测试所有者 → 冷备份 → 离线导入并核对云端 ID → OpenHarmony 唯一所有者在线和控制 → 冷重启 → 正常停机 → 回退旧测试所有者，保留最新 HA 状态，不调用解绑/恢复出厂。需要 App 与日志双重证据，任何一步失败都不进入原 HA。
4. **覆盖全旧产品和正式宿主。** 按原001–008目录补齐产品/SID、上下行、事件及异常语义；完成 x86_64/aarch64 真正 Linux 宿主验收，再接回原有一个配置条目和网关身份，提供显式引擎选择、预检、启停与回退。公共角色/组合/原生定时继续复用，不复制成两套业务实现。
5. **最后才做正式安装包。** 明确 profile、SDK/源代码许可证和可发行范围，验证干净安装、升级、备份还原以及唯一进程/唯一根身份，再为原 HA 准备停机窗口。此前不安装、不重启原 HA，也不改米家自动化。

本轮公共框架46项、框架接入25项、工具42项回归和原配置校验通过；身份探针通过正确资料、错误 AC、损坏数据三个控制检查。测试结束关闭所有本轮容器、停止发现转发、恢复平板熄屏设置，保留离线实验身份资料供继续排查。

按 TypeSafe 技能使用 Jev `jev-1.13.0` 做历史实验资料的复用分类；没有发送身份或凭据。低置信度的历史已配对实验没有自动复用，仍保持停止。单引擎设计、协议对照、C 边界、身份读取和迁移结论由 Codex 核对。结果见[分类记录](../../experiments/openharmony_bridge/evidence/jev-single-engine-evidence-selection.json)。本轮不修改干净公共贡献分支，也没有向上游发 MR/Issue；实验材料只提交当前实验分支。
