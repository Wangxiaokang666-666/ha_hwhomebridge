# 原 HA：M3 只读预检与生产备份准备

下文保留首次只读检查的范围与结果。后续已生成包含正反转修复的 `1.1.0b4` 包，并创建、下载、校验了一份不停止 HA 的在线配置备份，见 [b4 发布与预检记录](m3-b4-release-preflight.md)。在线归档包含运行中的旧 SDK KV，不能当作停机冷备或配对交接快照；独立维护通道和原机运行条件仍待验证。

2026-10-16 的本轮检查只读取现有服务和文件，不安装组件、不重启 HA、不修改插件设置、不启动 SSH/Samba、不调用设备服务。脱敏结果见 [m3-production-preflight.json](../experiments/openharmony_bridge/m3-production-preflight.json)。原始配置、在线副本和认证材料不提交 Git。

## 已打通的入口

原机为 Home Assistant OS **18.3**、Core **2026.10.1**、`amd64 / generic-x86-64`，Supervisor 报告剩余空间约 **265.7 GiB**。实际主网卡为 **`enp2s1`**；不能照搬实验机的 `eth0`。

提供的 token 属于 owner/admin。HA 的普通 REST API 可读；部分 `/api/hassio/...` REST 路径返回401，并不代表这个 token 没有管理员权限。通过版本对应的 `supervisor/api` WebSocket 命令、明确指定 `method=get`，已读到系统、插件、网络和备份信息。实现依据是 [HA 2026.10.1 的 Supervisor WebSocket 处理代码](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hassio/websocket_api.py)；该版本的 [REST 代理](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hassio/http.py)仅放行部分路径。

已有 **File editor 6.0.0** 正在运行，可以通过其现有 Ingress 认证入口读取文件。File editor 的 `/homeassistant/custom_components/hwhomebridge/` 对应 Core 的 `/config/custom_components/hwhomebridge/`。这一路径映射和 Ingress 访问方式也见[官方 File editor 文档](https://github.com/home-assistant/addons/blob/master/configurator/DOCS.md)。认证只创建短期 Ingress 会话，不修改插件配置；会话值不写入报告。

本轮直接验证了文件下载和目录读取。**没有测试远端写入，也没有取得系统命令通道。** File editor 只提供可见文件的清单，忽略规则可能隐藏部分路径；本轮未验证原文件属主、模式位或符号链接，不能把下载后本机的权限当作原机权限。

当前 File editor 入口经过 Core 的HTTP代理，这是从本轮请求地址和 [HA 的 Ingress 处理代码](https://github.com/home-assistant/core/blob/2026.10.1/homeassistant/components/hassio/ingress.py)作出的操作约束判断：不能依赖它在 Core 停止后继续提供文件访问。**先准备独立于 Core 的维护通道，再安排冷备份**，不要在仅有此入口时先停 Core。

## 在线副本和本地升级演练

生产组件版本为 **1.0.0**。本轮读取 File editor 可见的 **38 个静态组件文件**，约14.3 MB，以及 **1 个 hwhomebridge 条目**；两遍读取的文件字节、可见清单及桥接条目一致。本地副本根目录和子目录0700、文件0600，并重新校验文件哈希。

条目副本只保存 `domain=hwhomebridge` 的条目。其他集成的凭据原文不落盘，也不保存完整的 `core.config_entries`。这个筛选文件明确标注为局部条目副本，**不能直接覆盖 HA 的 `core.config_entries`**。

原机 `amd64` 旧 SDK SHA256 为 `0579e6f3cee0320d1533e5a96cc6d72a065a43050eff4edcb7afd6a9a52c7b58`，与 M2 真库验证使用的文件相同。从实际部署的 `hwbridge.py` 已确认，SDK KV 位于 **`/config/.storage/hwhomebridge/config`**。此次没有下载其中的 `device_ac`、配对 KV、设备列表，也没有读取或复制现有实验网关的配对目录。

在本机临时目录内，将已验证的 **1.1.0b3** 安装包叠加到生产组件副本：

- 包的整体/成员哈希与原 M3 安装包一致；HA 版本和CPU架构匹配。
- 使用包内已审查的配置校验器校验生产产品表/适配器，通过。
- 本地叠加后 **22 个保留文件**的内容不变，包括原 SDK、产品表/适配器及固定启动配置。
- 该步骤没有连接 HA 的控制服务，没有向 HA 上传或覆盖文件。

这份副本是**在线检查资料**，不是停机冷备份，也不是配对网关的回退快照。它不含 SDK KV、其他 HA 条目、完整权限元数据及忽略路径。以后安装前仍须在维护窗口确认唯一所有者已停止，再做完整的组件/条目冷备份；M4 的 KV 交接必须使用停止当前所有者后导出的最新状态。

## 仍缺的条件

两套 Advanced SSH & Web Terminal 均处于 error。只读配置和日志检查显示，两套都没有配置密码或公钥；22端口未连通。两套都配置了宿主22端口，后续应选其中一套处理，不能同时启用两套来占用同一端口；这不是本轮日志中已发生端口冲突的结论。本轮没有写认证配置、生成/下发密钥、修复插件或启动服务。Samba 为 stopped，445/139端口未连通，也未启动。

现存三份 Supervisor 备份均为2025年的 partial，详情均未包含 Home Assistant 配置。它们不能作为此次组件和条目的冷备份。此次没有触发新的 Supervisor 备份任务。

因此当前 `production_install_allowed=false`，仍需后续处理：

1. 确认并启用适合维护的系统命令/文件通道；若选择 SSH，先配置一种有效认证。此操作会改变插件配置，需要单独安排，本轮未做。
2. 为 HA OS 的 Docker 宿主准备适配流程。现有离线安装工具只支持本地 Podman，不能把 File editor 的可读能力当作 Docker 宿主控制能力。
3. 在正确的 Core 运行环境核对 UID、原文件权限/符号链接、磁盘写入条件及端口占用。File editor 处于另一容器，不能用它的用户或网络命名空间代替 Core。
4. 安排维护窗口，确认停止对象和自动拉起任务，完成生产冷备份，再考虑安装与 M4 身份交接。

## 如何重复只读检查

工具运行在本机，需要本机已有的 `websockets` Python 包；不向 HA 安装依赖。输出目录必须全新，在线副本应放在仓库外的受保护目录。以下为示例，不能包含 token 明文：

```sh
python3 tools/ha_production_readonly.py \
  --url http://<HA地址> \
  --token-file /secure/ha_token \
  --output /secure-backups/m3-readonly-new \
  --snapshot \
  --package tools/dist/m3/hwhomebridge-1.1.0b3.zip \
  --sha256 2f8a0d2052daabb9e0d4267e3a2ab8a0b12d46a1d9dda6171b4c982e3fa3789e
```

省略 `--snapshot` 时只在本机保存脱敏报告。工具对 Supervisor 路径做白名单校验；唯一的 POST 是 Ingress 登录认证。File editor 只允许两个 GET 路由：`listdir` 和 `download`。已拒绝 restart、reload、save、upload、delete、exec 等路由；特别不能因为HTTP方法是GET，就认定 File editor 的所有接口都只读。下载范围仅限组件和指定的桥接条目来源文件，运行 KV 下载被拒绝；重定向也被拒绝，防止认证材料转发到其他地址。

原 HA 的13个对照实体检查前后状态与属性一致；其中1个实体的时间/上下文元数据不同，因此没有声称完整快照相同。29个本地受保护源码/SDK文件未改变，现有已配对实验网关的容器及启动时间保持一致。实际设备服务调用、远端文件写入、安装、重启及插件启停请求均为0。新增12项只读隔离回归与原17项升级回归通过。

Jev 仅辅助把发现分为本轮可做、参考副本和后续维护。它把在线副本归入本轮可完成项，Codex 复核后明确限定其仅供检查，不能替代冷备份。未向 Jev 提供 token、原配置、条目内容或配对数据；工具运行时不依赖 Jev。
