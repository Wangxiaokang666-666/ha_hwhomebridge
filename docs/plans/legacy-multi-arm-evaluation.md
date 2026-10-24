# 多子设备、旧品类及 ARM Linux 验证

日期：2026-10-17。目标仍是一个父网关、一个原生引擎；本轮只使用原先已配对的虚拟测试身份，不连接家庭 HA。当前分支新增公共 `profile_bridge.py`，实验宿主复用原产品注册表、厂家适配器、VirtualDevice、SNManager 和 ServiceRouter，没有复制一套设备动作实现。

## 兼容目录

产品索引来自本地未修改的 amd64 旧 SDK；产品/SID/读写属性与各 PID 的官方 profile 核对。profile 需另行合法取得，不随提交发行。可从 `https://smarthome-drcn.dbankcdn.com/device/guide/<PID>/<PID>.json` 取得本轮核对的公开资料；未将下载成功等同于获得再发行许可。

| 框架 PID | 云端 PID | 本轮覆盖的原映射 |
| --- | --- | --- |
| 001 | 9MTL | 开关、亮度、色温 |
| 002 | 9MU7 | 原主灯对象/SN、开关、亮度、色温 |
| 003 | 9MTJ | 开关、亮度、色温、有人/无人状态 |
| 004 | 9MTM | 压力状态只读 |
| 005 | 9MTR | 模式、目标温度、当前温度、状态、开关 |
| 006 | 9MTV | 开关、已有 select 原生定时选项 |
| 007 | 9MTW | 开关、烹饪模式、状态、剩余时间 |
| 008 | 9MTX | 开关、电量、电压、功率、电流 |

005 的 `heatingTarget` 与 `current` 合并到云端 `temperature`；008 四项电参量合并到 `electric`。服务类型取 profile，不能把 Python 内部的映射名或 `leftTime` 的旧类型当成云端服务定义。只暴露已有 HA 映射；未映射的保温温度、烹饪暂停/动作、OTA 等不能算作支持，写入会明确失败。旧 select 定时保持原适配器的小时选项及就近匹配语义，不冒充设备提供了精确剩余秒数。

## 多子设备导入

旧 SDK 在原虚拟父身份下实际新增七类虚拟设备，得到 8 条原生生成的子身份。停止旧所有者后，冷副本离线解码得到的云端 ID/SN 与显式目录一一匹配。记录布局为 16 字节头加最多 64 条 80 字节记录；校验计数、版本、固定宽度文本、重复 ID/SN、空闲区域与末块零填充。8 条是原生文件实测；64 条是合成格式及 Linux C 容量/越界测试，没有声称做过 64 个云端设备的规模验收。

转换继续要求已核对的 amd64 SDK SHA256、原 48 字节 AC、**原网卡 MAC** 和实验目录标记。目标在 ARM 上运行时，也使用来源 x86 网卡的 MAC 解码旧文件，不用 ARM 新网卡 MAC。转换后的 OpenHarmony 身份存储不再依赖来源网卡。

回执 v2 记录每个旧子设备摘要及源/目标目录摘要。默认只接管原子设备；显式 `--include-new-children` 才允许把已配置的新风扇补充卡加入同一父目录。旧子设备仅上线，跳过重新绑定和设备资料同步；新卡按正常注册流程处理。父身份不新建，独立虚拟开关禁用。缺失或重复子设备、目录变动和空云端 ID 均拒绝启动。

## 独立 Linux 运行

使用原 x86 Linux 实验机生成旧 SDK 冷文件；另建 `hwhomebridge-multi-arm`，VZ 原生 aarch64 Ubuntu，无宿主挂载。只复制源码白名单、ARM 静态 ELF 和虚拟实验目录。原 `hwhomebridge-poc` 保持关闭。HA Core 镜像提供本机虚构实体，业务 HTTP 仅监听回环，不读取家庭 HA token。

ARM 上实际执行原生身份探针、C 路由程序与完整 OpenHarmony 进程，8 个旧子设备沿用原 ID 上线，7 个新补充卡在同一父会话注册/同步/上线，均收到成功应答。公共路由在 x86 与 ARM 都覆盖上表的读写及只读拒绝；ARM 另覆盖六档、自然风、正反转、原生设备定时、灯光附加设置和效果。该虚拟设备验收不等于对所有实体家电做过硬件验收。

HA 后端和 C 所有者整组停止再启动，父 ID、五组授权/登录字段、15 个子身份和 HA 业务状态保持一致。重启后旧设备只走上线，新卡使用已有 ID 同步上线，不重新绑定。

智慧生活已实际完成旧002开关/67%亮度/2009K色温、旧008开关及电量/电压/电流显示、新风扇三档、旧006三小时定时、旧007模式6写入。实际 App 定时省略 `num`/`enable`，已按默认值归一化并加入回归；取消定时通过原生C路由验收，没有把未完成的 App 取消操作算作成功。其余旧品类由真实C→HA Core路由覆盖，未声称逐一穷尽全部App界面。

冷停止 ARM 所有者后，保留旧 KV 和最新虚拟 HA 状态，再启动 x86 原 SDK，状态回到1、根ID及五组原生缓存字段核对一致；同一旧灯卡在 App 关灯成功。新风扇不由旧 SDK 控制。全部125项Python测试、配置校验通过；C本地容量、重复云ID拒绝及两种Linux架构读写通过。

智慧生活实测和回退结果、测试数量及清理状态以[脱敏机器记录](../../experiments/openharmony_bridge/evidence/legacy-multi-arm-evaluation.json)为准。原始配对文件、回执、App 布局和日志留在私有目录，不提交。Jev 仅分类多子文件、共享 SID、ARM 验收缺口，见[分类记录](../../experiments/openharmony_bridge/evidence/jev-legacy-multi-routing.json)；映射、密码格式及验收由 Codex 核对。

## 重跑入口

先准备全新的虚拟目录，或者使用已停止、带实验标记的配对冷副本。不要对原 HA 运行这些命令。

```sh
sh experiments/openharmony_bridge/build-ha-static.sh aarch64
# 生成七类额外虚构实体；002 由原单引擎 fixture 提供。
python3 experiments/openharmony_bridge/legacy_category_fixture.py "$VIRTUAL_LAB"
# 在 Linux HA 镜像内运行虚拟业务宿主。
python3 -B experiments/openharmony_bridge/single_engine_fixture.py serve "$VIRTUAL_LAB"
# 路由检查不启动 SDK，使用临时身份文件和上面的本机 HA。
python3 experiments/openharmony_bridge/legacy_category_check.py \
  "$STATIC_BUILD/ha_child_state_test" "$VIRTUAL_LAB/runtime/ha-children.local.json"
python3 experiments/openharmony_bridge/single_engine_native_check.py \
  "$STATIC_BUILD/ha_child_state_test"
# 冷停止唯一所有者后，显式离线导入。
python3 experiments/openharmony_bridge/legacy_identity_import.py \
  "$VIRTUAL_LAB" "$VIRTUAL_LAB/offline-import/combined" \
  --legacy-sdk "$VERIFIED_AMD64_SDK" --legacy-mac "$ORIGINAL_TEST_MAC" \
  --cloud-port "$OBSERVED_CLOUD_PORT" --include-new-children
python3 -B experiments/openharmony_bridge/single_engine_owner.py \
  openharmony "$VIRTUAL_LAB" "$STATIC_BUILD/bridge_poc" \
  --import-directory "$VIRTUAL_LAB/offline-import/combined"
```

## 下一道交付门槛

本轮没有接入家庭设备或生成正式生产迁移包。下一步把公共 profile 投影与旧身份预检接入正式 HA 后端生命周期：保留原配置条目/父身份、显式引擎选择、唯一所有者和停止后回退；再在独立 HA 实例验证安装升级、事件上报、重载和失败回滚。还需逐个真实厂家确认映射及合法再发行范围。生产停机窗口、安装和重启仍需另行安排。
