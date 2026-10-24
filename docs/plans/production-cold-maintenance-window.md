# 原 HA 冷备、保留式安装与恢复记录

2026-10-18 更新。用户已指定原 HA，并明确接受短暂停 Core、Node-RED、File editor 和 Supervisor，要求先验证恢复方案再部署。本轮已完成生产冷备、安装 `1.1.0b6`、恢复服务和智慧生活旧灯控制验证。仍沿用原配置条目、原网关和旧 SDK，**尚未切换到 OpenHarmony，尚未在原网关启用新风扇能力**。

脱敏结果见 [生产安装验收记录](../../experiments/openharmony_bridge/evidence/production-b6-deployment.json)。之前的[只读准备记录](../../experiments/openharmony_bridge/evidence/production-cold-window.json)是历史证据，其中“未部署”仅描述当时状态。

## 实际结果

- 原机 HA OS 18.3 / Core 2026.10.1 / Supervisor 2026.09.3，amd64，Docker 29.7.2。Core 使用 host 网络、UID 0。独立宿主 SSH 已固定公钥；没有带外核对记录，不声称已带外验证。
- 安装前完整冷备包含原组件、桥接运行目录和三份 HA 注册表。原机和本机私有副本均验真通过，解码的 32 个旧子设备与在线清单一致；在全新维护目录原样还原通过，未覆盖原 HA、未启动副本。
- 原 Core 容器 ID 和全部四个服务的镜像保留。Node-RED 被 Supervisor 自动重建，容器 ID 有变化，镜像相同；不能把“所有插件 ID 都不变”当成恢复条件。
- Core、Node-RED、File editor、Supervisor 均已恢复；`watchdog=true`、`boot=true`，临时 systemd runtime drop-in 已移除，原单元定义恢复。
- `1.1.0b6` 在原 HA 实际加载，原条目状态 `loaded`；旧 SDK 在线回调为真，当前路由仍有原来的 32 个设备和原品类。
- 22 个受保护静态文件逐一核对未变：原产品/适配器配置、SDK 和 profile。原条目 ID、unique ID、options 和原 data 字段保留；初始化只补入 `backend_type=legacy`，同时 HA 更新 `modified_at`。不是配置条目逐字节不变。
- HA 真实前端可见版本、32 个设备、原条目和当前运行/保存选择 `legacy`。查看后退出，没有提交后端或设备管理修改。
- MatePad 智慧生活中核对目标旧灯的序列号与原清单一致；实际操作开关、亮度和色温，HA 主灯分别读回变化。亮度恢复 100%，色温通过 HA 精确恢复 4000 K，最后从 App 关灯，回到测试前关闭状态。氛围灯、风扇的状态和更新时间在测试期间不变。
- 没有新建生产网关、重新配对、解绑、修改米家或 Node-RED 自动化定义。实际灯具发光效果没有额外的人工目视确认，验收依据是 App 和 HA 读回。

## 停机前必须核对的现场条件

配置目录同时被 Core、Node-RED、File editor 和 Supervisor 可写挂载持有。仅停止 Core 不满足冷备排他条件；不得忽略共享所有者来放行。宿主没有 Python，使用私有维护目录中的[免安装运行时](../portable-cold-runtime.md)，不向系统安装依赖，也不使用 Core 容器代替宿主维护通道。

宿主实际单元 `haos-supervisor.service` 为 `Restart=always`，其厂商定制 `/usr/sbin/haos-supervisor` 含镜像检查、删除和重新拉取分支。维护恢复不能直接运行这个启动入口，以免顺带更新镜像。

每次新窗口均重新确认：原条目与配置、实际 `/config` 挂载、Core 容器 ID/镜像、四个共享所有者、接口 MAC、watchdog/boot 原值、没有在途升级或恢复任务、SSH 可用及维护盘空间。不要猜 HA OS 配置路径，不开放 Docker TCP，不临时撤换宿主密钥。导出工具只能检查当前引擎和可见同 UID 宿主进程，不能代替其他机器和外部任务的排他确认。

维护目录须由 root 控制、权限 0700，位于配置目录之外；完整注册表含其他集成凭据，不能进入 Git、聊天或普通共享目录。预留至少 2 GiB 和独立私有副本位置。本次冷备仅覆盖桥接子系统及相关注册表，不是整机或 HA 数据库备份。

## 已验证的执行和恢复顺序

以下是现场核对后的操作顺序，不是可直接整段粘贴的无人值守脚本。每步记录返回码和实际状态，异常时先恢复原运行，再离线分析。

1. 记录原 `systemctl cat haos-supervisor.service`、四个容器的身份/镜像/状态、`ha core info --raw-json` 中 watchdog 和 boot。
2. 确认专用 runtime drop-in 不存在。在 `/run/systemd/system/haos-supervisor.service.d/` 新建仅本次维护使用的文件，权限 0600，清空 ExecStart 后只启动现有 Supervisor 容器并等待它退出：

   ```ini
   [Service]
   ExecStart=
   ExecStart=/bin/sh -c "/usr/bin/docker --host unix:///var/run/docker.sock start hassio_supervisor >/dev/null && exec /usr/bin/docker --host unix:///var/run/docker.sock wait hassio_supervisor"
   ```

   `daemon-reload` 后核对有效 ExecStart，必须没有厂商启动脚本。不能使用未通过演练的 `docker start --attach` 替代这一路径。
3. 设置 `ha core options --watchdog=false` 并读回。通过 `ha core stop` 正常停止 Core，不加 force；再停止 Supervisor 管理单元，最后停止 Node-RED 和 File editor，以免被其监督器拉起。
4. 至少观察 60 秒，四个原容器均无 Running/Paused/Restarting/Pid，身份与镜像稳定。导出前后还须由冷备工具检查所有权、可写挂载、宿主进程和原文件一致性。
5. 从可信运行时导出并验真；仅成功导出才作为有效快照：

   ```sh
   ./cold-backup export --engine docker --container homeassistant \
     --config "$HA_CONFIG" --interface enp2s1 \
     --preflight "$MAINT/preflight.private.json" --output "$COLD"
   ./cold-backup verify --output "$COLD"
   ```

6. 若本窗口安装组件，先用 `ha_upgrade.py backup` 保存组件和原条目，再 `preflight`、`install`。显式传 `--engine docker`、包 SHA256 和当前标记式探针。安装器保留 `config/`、`hilink_bridge/`、`profile/` 及运行 KV，不自动启用 OpenHarmony。
7. 无论导出/安装是否成功，源和原容器完整时，用上述临时单元启动已有 Supervisor，等待其 API 就绪。再次确认 Core 原 ID/镜像仍在，再 `ha core start`。恢复两个插件；允许 Supervisor 以原镜像重建插件，但必须核对对应服务正常运行，不运行更新/重装命令。
8. 读回四个服务状态，恢复记录的 watchdog 原值，确认 boot 未变。移除**本次创建的** runtime drop-in，`daemon-reload`，核对单元定义与原记录完全相同。失败收尾须分别尝试恢复 watchdog，不能因前面的插件容器 ID 断言失败而跳过。
9. 在线核对 API、原条目、实际路由和旧 SDK 在线回调；随后做 App 控制验收。导出副本私有传输到工作站并重新验真，离线还原只写全新目录：

   ```sh
   ./cold-backup restore --output "$COPIED_COLD" --target "$NEW_RESTORE"
   ```

   不启动还原目录，不在线覆盖全量注册表。原始还原核对字节、UID/GID、普通权限、mtime 和空目录，不等于原机回滚安装已执行。

Core stop/watchdog 语义已按实际版本的 [Supervisor API](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/api/homeassistant.py)及 [Core 实现](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/homeassistant/core.py)核对，CLI 选项在原机读 help 确认；定制单元另外做了实际现场演练和恢复，不能仅引用官方源码就视为通过。

## 本轮发现与修复

首次生产导出因在线/存储注册表表示不同而拒绝，没有安装代码。在线阶段补齐元数据后才重做：新版设备存储用单一 `config_entry_id`，WebSocket 用兼容列表；实体列表接口省略类别字段，改对已选设备逐项只读获取完整元数据。仍严格比较归属、类别和原文件，不把缺失字段当作 None。复合设备归属不明时拒绝自动投影。

原机 SN 文件以单个 LF 结尾，实验资料为 NUL。导出器现在只接受有界安全 ASCII 加其中一个终止字节，原始字节与哈希照存；LF 格式明确标记“原生解释待确认”。**没有修改源 SN，也没有放宽接管合同。**

Docker 启动探针前实际输出 `docker-default`，原先整段 JSON 解析失败。探针改用唯一 `HA_RUNTIME_PREFLIGHT ` 标记；重复标记、非对象或无有效 JSON 拒绝。原机实际 HA 镜像的只读、无网络探针已通过。

首次失败后的恢复中，Supervisor 自动重建 Node-RED，旧的“全部容器 ID 不变”断言中断了部分收尾；随后已恢复 watchdog、移除 drop-in 并核对所有服务。修订后的第二次冷备及第三次安装均完整收尾，不重复套用旧断言。

## 固定产物与验证边界

| 产物 | SHA256 |
| --- | --- |
| 本次生产冷备运行时 v2 | `008306121271c7dae91aa2c932f859df6441577ee42af03bab3ebf1d0fa2a672` |
| 本次安装包 `hwhomebridge-1.1.0b6.zip` | `0de843ba3d3247379b26be0eb184cdb58524f10ebe8e61e52ad0c3b95257bd71` |

之前准备的运行时 `fe5732626b7e04e139c8484f863ac7529d310ad8744519818d665ca9c081c5f1` 是旧产物，不能与本次 v2 混用。新运行时覆盖上述注册表与 LF 冷备修复；本次安装器另用当前 B6 源码。B6 包有 49 个公共文件和 6 个双架构原生产物，原生代码未改，逐个核对已验证 B5 产物和其源码哈希后复用。包不带厂商 SDK、profile 或凭据。

本机公共测试 64 项、工具测试 106 项、配置校验通过。独立 ARM Linux 上公共 64 项、升级工具 23 项、冷备/预检 33 项通过。四个合成 sleep 所有者演练覆盖仅停 Core 拒绝、全部停止后导出/验真、恢复原容器；同源宿主 Python 完成，最初 x86 模拟器中的便携入口超时不算通过。

为验证定制恢复机制，另在原 HA OS 上运行了无网络、无 HA 数据挂载的 sleep 容器和独立测试单元，验证原容器恢复、跳过厂商脚本、移除临时单元后恢复定义并清理；不是独立虚拟机，也不是那时就已验证真实 Supervisor 生命周期。后续生产窗口才完成真实 Supervisor/Core 停止与恢复。

Jev 用于验收证据分类，区分后端回调与真实前端验收；没有决定授权、网关身份或测试通过。具体执行和验收由 Codex 与确定性检查负责。

## 回退和后续接管

若 B6 加载失败：通过原独立维护通道停止已确认的写入者，从本次组件备份执行 `ha_upgrade.py restore --engine docker ... --restore-entries`，核对后恢复原服务。只恢复所属条目，保留其他集成最新记录和当前 SDK KV；不把全量冷备注册表直接覆盖到在线原 HA。本轮安装正常，因此没有在原机实际执行这一回滚。

任何失败都不删除条目、不在智慧生活解绑、不调用 `exit_brg`，也不启动第二个相同身份的引擎。原容器或源丢失时不要调用可能新建容器的普通 start；保留现场，先在新目录验真恢复，再逐项处理。

OpenHarmony 唯一引擎仍有明确未完成项：

1. 后续已完成固定旧库的 SN 原生复核、32 个实际绑定冻结，以及生产身份的无网络 C 读取器验真，详见[离线接管进度](../production-identity-adoption.md)。这些源码尚未安装到原 HA，也不等于已完成引擎接管。
2. 书房三项旧灯服务已按实际主灯绑定冻结；顺序重放仍有氛围灯候选，不能拿自动重放代替当前绑定。厨房不可用灯组保持原样；需补齐单子设备离线隔离，否则当前全量启动预读会失败。
3. 在独立环境验证正式身份格式和真实目录转换，核对旧子 SN/云 ID 保留、新组合卡目录以及失败回退。含生产身份的副本只能做无网络离线校验，不能与生产同时上线。
4. 条件补齐后，在原条目显式切换唯一引擎，再验收新风扇六档、自然风、正反转、定时及完整灯光、重启恢复和原 SDK 回退。本次 B6 旧 SDK 安装和旧灯验收不能替代这一阶段。
