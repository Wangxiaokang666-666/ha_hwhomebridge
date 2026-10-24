# 原网关冷备导出和真实实体预检

2026-10-17。本轮只新增维护工具、测试和文档，不修改集成运行代码，不重打 b5 包。原 HA 保持在线，没有安装、重启、设备控制或米家自动化变更。**尚未生成生产冷备，也不允许据此部署。**

## 交付范围

`tools/ha_entity_preflight.py` 用既有 token 读取 HA 的配置概况、设备注册表、实体注册表、状态、服务清单和集成条目元数据。仅允许六种只读 WebSocket 命令，最大响应 32 MiB；不提供服务调用、配置修改或 Supervisor 操作入口。两遍观察后只保留原桥接及显式角色涉及的数据，私有目录 0700、文件 0600。

工具从原桥接的三元标识反查确定性 SN 和唯一源设备，使用当前可信源码中的公共 ProductRegistry、ServiceRouter、RoleBindings、CapabilityRouter 重放匹配和只读状态投影。下载的生产 Python 不会被执行。禁用、不可用、反向桥接实体、归属冲突、缺失服务和顺序敏感的匹配均会记录。主灯和氛围灯存在歧义时，不自动重绑。`proposed_roles` 与 `ambiguous_choices` 只供复核，`original_runtime_bindings_confirmed=false`、`parent_identity_verified=false` 和 `migration_allowed=false` 不会自动改成真。

`tools/ha_cold_export.py` 在维护窗口中，通过独立于 Core 的本机 systemd Linux 宿主通道运行。Docker/Podman 只允许 `inspect` 和 `ps`，强制本机连接，不执行 stop/start/exec，也不安装或还原原 HA。它要求：

- 指定容器精确挂载目标 `/config`，已停止且无存活 PID，自动重启关闭。
- 指定容器引擎内没有其他活动的共享可写挂载；可见的同 UID 宿主进程没有持有源文件或源工作目录。不能检查进程时直接拒绝，不能把“无权限”当成“无所有者”。
- 运行于宿主系统上下文，拒绝 Docker/Podman 容器或插件的 PID/文件视图；Core 使用 host 网络，从实际宿主接口读取 MAC。HA OS 的预期维护上下文是宿主 root，不能用 File editor 用户或 SSH 插件容器冒充。
- 全新输出在配置目录之外。普通文件、链接、数量、空间、大小均受检查，符号链接、硬链接、特殊文件及源目录中的输出被拒绝。

导出范围为整个 `custom_components/hwhomebridge/`、整个 `.storage/hwhomebridge/`、完整 `core.config_entries`、`core.device_registry`、`core.entity_registry`。包括旧 SDK、用户适配器、私有 profile、SN/AC、配对 KV、注册及排除列表。**完整条目中包含其他集成的凭据，SDK KV 也包含授权信息；只在私有存储中保存，禁止提交 Git或粘贴聊天。** 本工具是桥接子系统快照，不是整机或完整 HA 系统备份。

工具记录原文件及目录的 UID/GID、模式、时间、inode 等校验元数据，两遍核对文件清单、字节和元数据，并复查容器 ID、启动/停止时间和 MAC。访问时间不属于“不变”断言，inode/ctime 是一致性检查数据，不承诺可在新目录恢复。输出统一为私有权限，原权限保存在清单中；空目录也保留。校验通过才发布最终目录，失败不留有效导出。

已验证的旧 amd64 SDK 哈希及 root v3 / bridge v4 格式由公共 `identity_store.py` 离线解码。原 MAC、原 AC、根云端 ID、五组授权字段和全部旧子身份可重新核对，不加载旧库，不连接华为云。未知 SDK 或无法解码的身份拒绝。另一个 UID、另一个容器引擎、另一个主机或外部自动拉起任务仍需操作员排他核对；这不是分布式身份锁。

当前合同针对尚未托管的原旧 SDK 条目。已带 `gateway_directory` 的托管条目明确拒绝，避免误读停用前遗留在旧路径的 KV；已托管后端的长期备份需要另行定义合同。

## 冷备有效不等于可以接管

在线实体不可用、旧映射歧义、排除列表及云端子身份与在线清单不一致，都必须保留原样。只要源注册表稳定且完整可识别，仍可以在停机后保存一致冷备；这些问题分别进入 `entity_preflight_passed`、`child_inventory_matches_live`、`excluded_devices_present`。任何导出或验真结果都保持 `adoption_ready=false`。

`cold-export.json` 与 `payload/` 是私有原始快照，不能直接填入后端切换页。它不是 `GatewayDeployment` 接管目录，没有创建 OpenHarmony 存储或新身份，也没有对旧配置条目做更改。后续离线转换必须显式冻结已核对的原服务绑定和角色，验证原身份、原生读取器及最终目录，再考虑启用。

## 操作方式

从完整当前源码检出运行，不能只下载一个脚本。Python 需具备 `aiofiles`、`cryptography`；在线读取还需 `websockets`。不要把依赖安装到运行中的原 HA。b5 保留式升级包未包含本轮新工具，包的原 SHA256 不变。

```sh
# 本机：先用既有只读工具取得生产静态组件副本；不读取运行 KV。
python3 tools/ha_production_readonly.py --url http://<HA地址> \
  --token-file /secure/ha_token --output /secure/new-online-copy --snapshot

# 本机：实例只写设备ID和显式角色；无REST凭据，不填实验父SN替代原父身份。
python3 tools/ha_entity_preflight.py --url http://<HA地址> \
  --token-file /secure/ha_token --component /secure/new-online-copy/component \
  --instances /secure/proposed-roles.json --capability-config /secure/frozen-mapping \
  --output /secure/new-entity-preflight
# 不检查新组合时，省略 instances 和 capability-config，两项必须同时提供。

# 以下只能在未来维护窗口、已自行停好原所有者且有独立通道后执行。
# CONFIG 是宿主实际路径，HA OS 不能照抄示例路径。此命令不会停止HA。
python3 tools/ha_cold_export.py export --engine docker --container homeassistant \
  --config "$CONFIG" --interface enp2s1 \
  --preflight /secure/new-entity-preflight/preflight.private.json \
  --output /secure/new-cold-export

# 本机或独立Linux离线验真；不启动SDK或HA。
python3 tools/ha_cold_export.py verify --output /secure/new-cold-export
```

默认 CLI 只打印计数和布尔结果。`preflight.private.json`、`snapshot.private.json`、角色实例、原始冷备均留在受保护目录。`structure_passed=false` 表示接管检查没有通过，不表示读取失败或已更改 HA。

## 本轮证据与生产待办

独立验收和生产只读结果见[脱敏记录](../../experiments/openharmony_bridge/evidence/production-cold-export.json)。独立 Linux 使用虚构注册表与合成 v3/v4 凭据、真实 Podman 停机证据及已固定版本的 SDK 文件字节；测试容器只运行 sleep，没有运行 HA/SDK、配对或连接家庭设备。Docker 分支有契约测试，实际 HA OS Docker 宿主尚未实测；不能把 Podman 通过当成原 HA OS 现场通过。原始快照还原只发生在第三个全新测试目录。

公共 60、工具 63、离线身份 8、实验框架 25 项，共 156 项通过，配置校验通过；新工具 21 项也在独立 x86_64 Linux 的官方 HA Python 环境通过。实际宿主 root 演练覆盖运行中、自动重启、共享写入、本机文件占用、成功导出/验真/还原和篡改拒绝；普通用户看不到进程 FD 时，另行验证了明确拒绝且不发布导出。

生产只读前后 38 个组件文件和原桥接条目完全一致。原网关关联 32 个设备。书房风扇灯 13 个角色归属正确，7 张补充卡的公共状态投影通过。当前仍有两项接管阻塞：

1. 书房风扇灯的旧默认灯光映射对实体顺序敏感，主灯与氛围灯可能成为同一旧服务的不同候选。需要确认并冻结旧运行绑定；本轮没有修改适配器、设备选择或原灯卡。
2. 厨房筒灯灯组的一个实体不可用，涉及旧灯的多个服务。需要恢复设备或单独制定保留/处理方案；本轮未发任何控制，也未删除或排除它。

后续顺序：独立维护通道和原机权限预检 → 维护窗口内真实生产冷备 → 原身份离线转换和服务绑定复核 → 原生读取器及唯一网关目录验收 → 才能安排安装、启用与真实 App 控制复验。当前原生目录的 `child_sn()` 仍限定 OHPOC 父 SN，生产原 SN 尚未冷读确认；正式父 SN 规则及最终原生目录必须单独补齐、实测，不能用预检占位父身份绕过这一门槛。

Jev 本轮只对在线参考副本、实体观察、停机导出候选和维护窗口证据归类，见[脱敏结果](../../experiments/openharmony_bridge/evidence/jev-cold-export-routing.json)。密码格式、进程权限和是否通过均由 Codex 与确定性代码核对；工具运行不依赖 AI。

2026-10-17 后续：独立环境已补真实 Docker 的相同导出、验真、还原和故障拒绝，以及专用 SSH 公钥到宿主探针的验证。原机 22222 开放但现有密钥未通过认证，原机 Docker 仍未实测；原机已有日志没有旧灯路由记录，绑定歧义继续保留。详见[维护入口与 Docker 预检](production-maintenance-channel.md)。这次补充没有改变本页的生产停机、身份和接管门槛。

同日用户授权追加专用公钥后：原有两条授权完整保留，专用宿主 SSH 和原机 Docker 在线预检现已通过；HA/Core 没有停止或重启，组件与桥接条目不变。原机宿主确认没有 Python，因此冷备工具的宿主离线运行时仍需先在独立环境解决。本次没有生成生产冷备或验证停机后的原机通道，详见[原机脱敏记录](../../experiments/openharmony_bridge/evidence/production-host-authorized-preflight.json)。

后续已补[免安装 Linux x86_64 冷备运行时](../portable-cold-runtime.md)，固定便携 Python 与 wheel 哈希，保留原宿主、停机和一致性检查。新增 `restore --output <冷备> --target <全新私有包装目录>`，只写新目录的 `config/`，恢复原字节、UID/GID、普通权限、修改时间及空目录，不覆盖原 HA，不创建接管目录或启动 SDK。该包在独立环境的官方 HA OS 18.3 用户空间验证，不依赖目标系统 Python；原机尚未接收或执行这个包，生产冷备与部署门槛不变。
