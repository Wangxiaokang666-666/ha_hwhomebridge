# 原机独立维护入口与 Docker 只读预检

2026-10-17。经用户明确授权，已向原机宿主追加专用公钥，保留原有授权，完成原机真实 Docker 的在线只读预检。不改集成运行代码和 b5 包，不安装、停机、重启原 HA，不调整灯光或米家自动化。工具从完整源码检出运行。

## 入口选择与目前缺口

优先使用 **HA OS 宿主 SSH 的 22222 端口**。它由宿主提供，不依赖 Core 或 File editor。SSH 插件运行在容器内，不能替代冷备导出要求的宿主进程、权限和文件视图。依据：[HA OS 官方调试文档](https://developers.home-assistant.io/docs/operating-system/debugging/)。

原机检查：22222 有 SSH 服务，22 未开放，Docker TCP 2375/2376 未开放；现有两个 SSH 插件仍报错。专用公钥现已通过宿主认证，**原机独立 SSH 和真实 Docker 在线预检均通过**。没有给 Docker 开 TCP，没有启动或配置插件，也没有用停止 Core 来测试通道。

专用 Ed25519 私钥、公钥、SSH 配置和原机公钥清单只保存在本机私有目录，不提交 Git。通过用户已批准的指纹精确核对客户端公钥；SSH 严格核对准备阶段固定的宿主公钥记录，没有自动接受新指纹。宿主指纹没有做带外核对，不将既有 TOFU 记录冒充带外验证。

原机 Supervisor 为 2026.09.3，API 已实际读取确认。按[该版本官方接口](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/api/os.py)和[追加实现](https://github.com/home-assistant/supervisor/blob/2026.09.3/supervisor/os/manager.py)，对固定 `/os/ssh/authorized_keys` 先 GET，再仅 POST 一条经过格式和指纹验证的公钥，最后 GET 核对。原有两条记录完整保留，清单现为三条；POST 仅一次，DELETE 为零。没有覆盖授权文件、修改插件或执行 `ha os import`。后续操作继续遵守当前授权范围，不能凭 root 通道可用就部署或启停 HA。

## 只读宿主探针

`tools/ha_host_preflight.py` 从本机通过已认证的 SSH 标准输入运行 `tools/ha_host_probe.sh`，原机不落脚本文件。仅接受固定容器名和接口名字符集，固定 root 用户、公钥登录、严格宿主公钥检查，不读取用户 SSH 配置，不使用 agent，不建转发；不允许任意远端命令。

探针要求 root、PID 1 为 systemd，拒绝容器标记；强制 Docker 本机 Unix socket，拒绝 rootless。只使用 Docker `info`、`inspect`，不读取环境变量、注册表凭据、配对 KV 或容器内部文件。读取：

- Docker ID、Core 容器 ID、状态、启动时间、重启策略和配置用户。
- `/config` 的精确可写宿主挂载，配置根、组件根、旧运行根和三份 HA 注册表的类型、UID/GID、模式、大小；这些路径及父路径不得经符号链接。
- 正在运行的 Core 宿主进程 UID、实际 host 网络命名空间、指定接口 MAC。
- 宿主 TCP/UDP 四张内核表中的端口快照，以及是否能找到宿主 Python 命令。

结果仅写本机全新 0700 目录，文件 0600。默认只输出布尔结果。认证、权限、路径、输出格式或命名空间不符时拒绝，不产生有效预检结果。日志、MAC、路径、容器 ID 和原文件权限只留私有报告。

```sh
# 已完成公钥授权和宿主指纹核对之后；仍不停止Core。
python3 tools/ha_host_preflight.py --host <原机地址> --port 22222 \
  --key /secure/maintenance/id_ed25519 \
  --known-hosts /secure/maintenance/known_hosts \
  --container homeassistant --interface enp2s1 \
  --output /secure/new-host-preflight
```

在线探针不会为了通过冷备检查而关闭自动重启，也不会给停机中的 Core 出具在线验收。端口快照不等于新后端端口已可绑定；源根权限不等于完整冷备目录全部安全；有宿主 Python 也不证明 `cryptography` 等依赖可用。**本次确认原机 HA OS 宿主没有 Python**，要先在独立环境准备经过验证的离线运行时，不能把 Ubuntu 测试机依赖或 Core 容器内 Python 冒充宿主依赖，更不能临时往原 HA 安装依赖。

后续冷备仍由 `ha_cold_export.py` 单独检查停机、自动重启、共享写入、可见宿主占用、完整目录一致性与身份解码。在线报告始终保持 `cold_backup=false`、`production_install_allowed=false`；单靠这些工具没有停机或部署授权。

## 书房主灯与氛围灯

本轮重新读取原设备/实体注册表、状态和服务，32 个旧设备仍完整可识别。书房显式 13 个角色和 7 张补充卡状态投影保持通过。旧书房逻辑设备为一个 PID 002，旧配置只有 `switch`、`brightness`、`cct` 三个服务；观察到的实体顺序重放均指向主灯，氛围灯也会通过旧默认灯匹配，反转实体顺序后会改变选择。

`tools/ha_legacy_binding_audit.py` 只 GET 已有的 `/api/hassio/core/logs/latest`，受大小限制且禁止重定向；不改 logger，不触发一次灯光控制来制造日志。原版没有直接读取运行时映射表的接口；本次启动以来的已有日志没有旧桥接路由记录。因此**本轮没有拿到原服务当前实际绑定的直接证据**，不确认氛围灯已独立绑定，也不自动冻结、覆盖或换绑。即使找到历史路由日志，它也只能证明当时路由，不能自动升级为当前运行映射证明。

```sh
python3 tools/ha_legacy_binding_audit.py --url http://<原机地址> \
  --token-file /secure/ha_token --preflight /secure/preflight.private.json \
  --source-id <清单中的书房源设备ID> --output /secure/new-binding-audit
```

主灯与氛围灯歧义、厨房筒灯灯组的一个不可用实体继续阻止接管。未来可在维护窗口根据原注册流程、冷备与显式角色共同审阅冻结方案；不能在本轮只读范围内用改日志、重载或设备控制强行消除证据缺口。

## 独立验证与下一步

脱敏证据见[维护通道验收](../../experiments/openharmony_bridge/evidence/production-maintenance-channel.json)。测试使用独立 x86_64 Ubuntu、真实 Docker、虚构注册表、合成配对资料及只运行 sleep 的容器；没有启动 SDK 或连接家庭设备。测试密钥授权只写入独立 VM，结束移除本轮授权和测试容器，停止 VM。

真实 Docker 29.1.3 的运行中、自动重启、共享写入、宿主文件占用拒绝，以及停机导出、验真、第三目录还原、篡改拒绝均通过。SSH 完整探针通过，虚构 Core 容器停止后 SSH 仍可用，在线探针对停机容器明确拒绝且不发布报告。最终源码与独立 Linux 验收源码哈希一致。Mac 168 项回归和配置校验通过；新增 12 项回归也在独立 Linux 通过。

原机 HA OS 18.3、Docker 29.7.2 的 root systemd 宿主预检随后通过：配置实际挂载、普通根路径、文件 UID/GID 和模式、Core 运行 UID 0、host 网络命名空间及接口 MAC 可读取；目录与注册表的所有者符合运行 UID，原容器重启策略为 no（仅读取，没有修改）。两次检查中 Docker/Core 身份、Core 启动时间、挂载和根元数据相同。授权前后 38 个组件文件与桥接配置条目完全一致，b5 包原 SHA256 不变。原机没有停机测试、冷备、SDK KV 读取或部署。详见[本次原机记录](../../experiments/openharmony_bridge/evidence/production-host-authorized-preflight.json)，前文独立验收记录保留授权前时点的事实。

宿主免安装运行时已在独立环境准备，见[便携冷备运行时](../portable-cold-runtime.md)：随包带固定哈希的 Python 和依赖，原机不必安装 Python；已在官方 HA OS 18.3 用户空间验证，但尚未传入或运行于原机。下一步顺序：刷新只读清单并保留旧绑定歧义 → 另行安排维护窗口生成真实冷备 → 审阅冻结旧绑定和实体问题 → 原身份离线转换、正式父 SN 及原生目录验证 → 才能安排单网关安装、启用与回退。此次没有生成生产冷备，也没有更新原 HA。

本轮按 TypeSafe 技能由 Jev 对“宿主 SSH / Core 文件入口”和“配置重放 / 实际绑定证据”分类，见[脱敏结果](../../experiments/openharmony_bridge/evidence/jev-maintenance-routing.json)；授权后又对[单条公钥追加操作](../../experiments/openharmony_bridge/evidence/jev-host-key-append-routing.json)归类。复杂判断、身份、用户授权和通过条件由 Codex 与确定性代码负责。工具运行不依赖 AI。

同日后续：已再次刷新原机只读清单、已有日志和宿主证据，32 个旧设备、书房 13 个角色和 7 张组合卡未变，两个接管阻塞仍保留；38 个组件文件、原条目、Core 身份/启动时间及 watchdog/boot 原值不变。新增离线观察复核工具及[冷备窗口执行和恢复手册](production-cold-maintenance-window.md)，独立真实 Docker 演练成功冷备、陈旧清单失败后的同一测试所有者恢复和在线后离线还原。没有实测真实 Supervisor 启停，没有触碰原机配对 KV、设备或自动化。
