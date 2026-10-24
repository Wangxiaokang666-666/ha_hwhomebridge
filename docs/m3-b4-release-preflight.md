# 1.1.0b4 安装包与原 HA 备份、预检

范围更新：`b4` 保留为双后端实验升级包及历史验证产物，不作为最终单网关贡献版本。后续按[单网关复用与贡献计划](plans/reusable-fan-single-gateway-contribution-plan.md)收敛；本文的备份和预检证据仍保留，不表示现在可以按旧方案部署第二网关。

`1.1.0b4` 已包含书房风扇的两处方向修复：厂家适配器的 `verticalswing` 和组合卡片的 `switch1`。旧 `b3` 包保留。正式产物位于 `tools/dist/m3-b4-final/`，脱敏证据见 [m3-b4-verification.json](../experiments/openharmony_bridge/m3-b4-verification.json)。本轮没有安装、停止或重启原 HA。

## 安装包

- 文件：`hwhomebridge-1.1.0b4.zip`，9,224,273 字节。
- SHA256：`a222610c00f0ac832d916748c69e4b7076ed5460f410c5520d550335338ceb3f`。
- 包内38个文件，相比 `b3` 只改变组件版本号和两份方向配置；x86_64、aarch64 的静态 C 程序完全相同。
- 同目录附升级、运行预检、生产只读采集、在线备份四个工具；`SHA256SUMS` 全部通过校验。使用与包配套的工具，不用旧版工具读取新包。

独立 x86_64 Linux 中，使用全新未配对夹具，从 `1.0.0` 执行预检、组件/条目冷备、安装、还原。实际 HA `2026.10.1` 镜像中的 Python `3.14.6` 探针通过；两处修复均落盘，25个保留文件不变，最终42个原文件及权限、条目字节完全还原，运行目录标记未变。

这次新包演练没有启动目标 HA 或 SDK。真实双 SDK 的启动、重启与共存证据沿用原 `b3` 的 M3 记录；书房方向配置另已在独立配对网关通过智慧生活实际点击验收，见[方向验收](openharmony-direction-validation.md)。不能把离线安装演练称为原 HA 运行验收。

## 原 HA 在线配置备份

已在原 HA 创建一份仅包含 HA 配置的 Supervisor partial 备份，排除数据库，不包含插件和额外目录。归档35,573,760字节，SHA256 为 `401307405fb3d13326a3f9faa2e6963a097be846e170d566ddeff26a001589b5`。下载后回读哈希、校验内层 gzip CRC 和 tar 路径、检查必要文件，未向其他目录解包或运行其中的代码。

本机私有目录：`/Users/kevin/.config/ha_hwhomebridge/backups/m3-b4-online-20261016/`。目录0700、文件0600，含完整 HA 条目和其他集成凭据，禁止提交 Git。Supervisor 中保留该备份；以同一私有日志目录续检没有创建重复备份。

归档包含完整 `core.config_entries`、38个组件文件及运行中的旧 SDK 目录。组件哈希与备份前、后的两份只读检查副本完全一致。归档记录的组件文件 UID 为0、权限0644、无链接；这只是归档元数据，不能代替实际 HA 运行 UID 和现场文件权限检查。

这是一份**在线配置备份**。运行中的第三方 SDK 不参与一致性协调，归档中的 KV 不保证同一时刻的一致状态。不能把它当作已验证的一致冷备，也不能直接用于配对身份交接。停机冷备、M4 的最新 KV 导出和唯一所有者交接仍待维护窗口完成。

在线备份范围依据原机 Supervisor `2026.09.3` 的[HA 备份实现](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/homeassistant/module.py)核对：通过备份开始/结束通知保存配置，不停止 Core；请求不包含插件，因此不触发插件备份停启。工具限定已审查的 Supervisor/Core 版本，默认只预检；POST 结果不确定时，使用本地先写日志和精确名称查找，禁止盲目重发创建请求。

```sh
# 默认只预检；凭据只从本地文件读取。
python3 ha_online_backup.py --url http://192.168.2.122 \
  --token-file /Users/kevin/.config/ha_hwhomebridge/ha_token \
  --output /srv/private-backups/m3-online

# 显式创建在线备份。若中断，使用相同 output 续检，不换目录重复创建。
python3 ha_online_backup.py create --url http://192.168.2.122 \
  --token-file /Users/kevin/.config/ha_hwhomebridge/ha_token \
  --output /srv/private-backups/m3-online
```

## 原 HA 部署预检结果

原机仍为 HA OS18.3、Core2026.10.1、amd64，主网卡为 `enp2s1`。仍只有原 `1.0.0` 组件和一个桥接条目，38个组件文件及桥接条目在本轮前后均未改变。用新包在本机叠加这份生产副本，配置校验通过、22个原文件保持不变，HA版本及架构匹配。没有向原 HA 上传组件或新身份。

13个源实体的完整状态记录保持一致。现有独立配对网关的容器启动时间、C进程及配对身份也未变，云端及八张卡片在线。没有设备服务调用，没有改变现有桥接、灯光或米家自动化。

`production_install_allowed=false`。仍缺：

1. Core 停止时仍可用的独立维护入口；File editor Ingress 依赖 Core。两套 SSH 仍为 error，22端口不通；Samba 未启动，139/445不通。本轮没有修改或启动它们。
2. HA OS Docker 的部署适配器，以及原机实际运行 UID、现场文件权限和网络命名空间内端口检查。
3. 维护窗口中的停止所有者冷备份，以及 M4 的排他身份交接。

Jev 只辅助把材料归为“在线备份”“归档权限元数据”和“部分完成的部署准备”；具体操作、校验及是否允许安装由 Codex 和确定性工具核对。没有运行时 AI 依赖。
