# M3：保留式安装包、备份和预检

本文记录实验后端的保留式安装工具及历史验证，不是最终单网关版本的安装指南。用户最终要求已明确为复用原集成、只保留一个网关，实施以[单网关复用与贡献计划](plans/reusable-fan-single-gateway-contribution-plan.md)为准，暂不执行旧的第二网关迁入方案。

本工具只升级已有 hwhomebridge 组件。它不安装全新旧 SDK，不启动或停止 HA，不启用 OpenHarmony，不调用设备服务，也不迁移配对身份。默认动作是 `preflight`。原 HA 的现有 File editor 文件读取已验证，详见[生产只读预检](production-readonly-preflight.md)；系统命令通道、原权限及停机维护条件尚未验证，当前不得执行生产安装。

当前包已包含书房风扇的方向修复，版本为 `1.1.0b4`。本轮新包验证和原 HA 在线备份见 [b4 发布与预检记录](m3-b4-release-preflight.md)；原 M3 的真实双 SDK 启动验收记录对应 `b3`，两个版本的 C 程序相同。

## 安装包范围

组件版本为 `1.1.0b4`，当前仅验过 HA Core `2026.10.1`，其他版本预检拒绝。包内带文件清单和 SHA256、构建来源，以及 x86_64/aarch64 静态 `bridge_poc` 与对应 `build-info.json`。静态程序来自 M2 的同一固定源码，M3 不修改 C 实现。ARM64 程序的本次验证只覆盖打包、架构和哈希，不代表本次在 ARM64 重跑了安装验收。

构建时只选择 Git 跟踪的公开组件代码、翻译、图标和 OpenHarmony 定义；不会递归打包 experiments。以下内容不装入升级包，安装时从目标原样保留：

- `config/`：旧产品表和全部适配器，包括目标自行添加的文件。
- `hilink_bridge/`：原闭源 SDK 库、启动配置及目录内其他文件。
- `profile/`：原私有 profile。
- HA `.storage` 中的运行配置、SDK KV、配对身份和集成条目。安装动作不修改这些文件。

新静态程序存入组件 `openharmony/bin/<arch>/`，不会自动复制到运行目录。新增网关仍须在 `.storage/hwhomebridge/openharmony/<instance>/` 单独准备。安装不改变条目类型、不新增条目、不切换运行所有者。原组件未知文件保留，Python 字节码缓存在安装暂存目录内清除。

构建命令在仓库根目录执行；先按 M2 文档构建两种架构：

```sh
python3 tools/ha_upgrade.py build \
  --output tools/dist/m3 \
  --native-x86_64 experiments/openharmony_bridge/build-ha-static-x86_64 \
  --native-aarch64 experiments/openharmony_bridge/build-ha-static-arm64
```

输出包括升级 ZIP、外部 `release.json`、`SHA256SUMS` 和两个独立 Python 工具。ZIP 的 `release.json` 校验每个成员；外部 SHA256 必须从已审查交付物取得，不接受来自未知包自身的哈希作为可信依据。构建元数据的 Git revision 是打包时的基线提交；具体文件以清单哈希为准。ZIP 时间戳固定，相同基线和文件可重现。

## 文件通道与停机条件

当前自动化适配器只支持**在目标 Linux 本地调用 Podman**。必须取得目标配置目录的真实文件通道，确认目标容器名称、`/config` 的实际 bind mount、运行镜像、UID、配置目录权限，并另行停止外部自动拉起任务。工具不通过 HA token 执行 OS 写入。HA OS 的 Terminal/SSH/Samba 等入口是否可用，需要实地确认权限与路径；不能直接套用本工具的 Podman 命令到 HA OS 的 Docker 宿主。

工具检查指定容器已停止、没有暂停/重启状态、没有自动重启策略，以及当前 Podman 用户可见的运行容器没有挂载目标目录或其父/子目录。配置和组件文件须由文件通道用户拥有，拒绝符号链接及特殊文件。工具再次在变更前核对停机状态，并用本地文件锁防止两个升级命令并发。

这只能证明本地 Podman 的已知运行所有者停止，不能发现其他 Podman 用户、其他引擎或另一台机器。执行方仍须在维护窗口禁止外部调度启动；跨主机配对身份的排他交接留在 M4。生产文件通道未验证、所有者不明确时，只保留报告，不做覆盖。

## 离线演练顺序

下列路径和容器名均是待替换示例，**不指向原 HA**。先将安装包及工具复制到已确认的目标文件通道；不要复制正在运行的配对目录。`--config` 应为宿主实际映射到 `/config` 的目录。容器必须已经停止，备份目录必须是目标配置目录外的全新目录，并使用可用空间充足的已存在父目录。

```sh
# 默认仅预检：实际目标镜像在无网络、只读临时容器中确认版本/CPU/UID/可读权限。
python3 ha_upgrade.py --config /srv/ha-test/config \
  --container ha-test --package hwhomebridge-1.1.0b4.zip --sha256 <可信SHA256>

# 备份整个旧组件及完整 core.config_entries，权限0700/0600；确认备份完整性。
python3 ha_upgrade.py backup --config /srv/ha-test/config \
  --container ha-test --backup /srv/ha-test-backups/before-m3

# 安装要求备份与当前组件、条目仍一致；不会启用条目或启动 HA。
python3 ha_upgrade.py install --config /srv/ha-test/config \
  --container ha-test --package hwhomebridge-1.1.0b4.zip --sha256 <可信SHA256> \
  --backup /srv/ha-test-backups/before-m3
```

离线预检还检查包的内部/外部哈希、目标旧 SDK/产品表/配置是否存在、Linux CPU 架构、磁盘空间、文件写入权限。临时版本探针无网络、只读挂载配置、禁用字节码写入，不加载桥接。目标 HA 版本或运行 UID 不匹配时拒绝。备份另检查其所在文件系统容量。

`passed: true` 仅代表文件安装条件通过；`activation_ready` 仍为 false。原生实例启动前还须在目标 HA 的网络命名空间及 UID 下调用 `ha_runtime_preflight.activation(config, relative, states)`：`states` 必须取当前 HA 实体状态，不能用陈旧导出替代。该探针检查实际网卡及 IPv4、TCP/UDP 端口、实例执行位/架构/哈希、目录属主和私密权限、绑定实体可用性、固定 profile 和服务目录一致性。不输出 SN、PIN、AC，不启动 C；身份所有权仍由 M4 交接负责。已经启动的网关占用端口会使探针拒绝，此时应先按维护流程停用该条目，不能抢占端口。

安装后不要直接启用任何复制的已配对身份。独立演练使用全新未配对身份及虚拟实体；生产试运行要先完成 M4 的最新 KV 和唯一所有者交接。

## 备份与还原边界

备份含敏感的完整 `core.config_entries`，只保存在权限0700的备份根目录内，文件0600，禁止提交 Git 或附到公开报告。组件内容按原权限保全，私有文件由备份根目录保护。工具记录每个文件的内容和权限并回读校验。**此备份不含 `.storage/hwhomebridge` 的 SDK KV，也不是配对网关回退快照。** 迁移后的 KV 回退必须先停当前所有者，再使用其最新状态反向交接，不使用这里的组件备份替代。

```sh
# 停止目标 HA 后，还原原组件；不启动 HA、不解绑网关。
python3 ha_upgrade.py restore --config /srv/ha-test/config \
  --container ha-test --backup /srv/ha-test-backups/before-m3

# 若测试中改变了 hwhomebridge 条目，默认拒绝，核对后显式恢复这些条目。
python3 ha_upgrade.py restore --config /srv/ha-test/config \
  --container ha-test --backup /srv/ha-test-backups/before-m3 --restore-entries
```

还原前核对已安装文件的内容/权限；安装后被修改的配置或代码会阻止覆盖，生成的字节码缓存除外。条目没变化时保持 `core.config_entries` 原始字节不动；显式恢复时只替换 `domain=hwhomebridge` 的条目，保留其他集成条目及 HA 存储元数据的新变化，并在切换组件前检查条目 ID 冲突。显式恢复条目会撤销备份后的新增桥接条目；仅在已核对独立实例和身份所有者的维护窗口使用，不能当作设备解绑或跨主机回退。

目录切换使用同文件系统 rename，先保留 `.hwhomebridge-previous`，再放置暂存目录。普通异常会把原目录移回；若进程/宿主在两次 rename 之间中断，保持 HA 停机，检查 previous、暂存目录和备份清单，确认后手工恢复，不清理唯一副本。工具发现 previous 会拒绝继续。组件与条目存储不构成一个跨文件事务；中断后应按备份清单离线修复并复核条目，不能先启动 HA。工具不自动重启或跨主机切换所有者。

## 本次验收与未完成项

M3 的脱敏证据见 `experiments/openharmony_bridge/m3-verification.json`。验证基于独立 QEMU 全系统 x86_64 Linux 和真实 HA 镜像，旧闭源 SDK 使用新的未配对身份；不复制现有实验网关的配对状态，不连接原 HA 的控制服务。安装包的 ARM64 文件没有在本次 ARM64 HA 中执行。

首次 M3 隔离验收时，生产仍缺 OS 文件通道与部署适配器、目标环境检查和生产冷备份。后续[生产只读预检](production-readonly-preflight.md)已确认实际CPU/网卡和 File editor 读取，并生成本地在线检查副本；系统命令通道、运行 UID/原权限、实际端口及生产冷备份仍未补齐。M4 的排他身份交接和维护窗口也未执行，不声称生产安装、智慧生活端新增验收、实体灯光控制或长期观察已完成。

Jev 辅助将公开代码/静态程序归为打包项，将原 SDK/适配器归为保留项，将 KV/备份归为私密数据，并将缺失文件通道归为阻断项。实际路径、校验、备份和还原规则由确定性代码执行，没有运行时 AI 依赖。

## 复现 M3 安装验收

在完整 Git 历史的仓库中，先按 M2 准备公开 assets 和 x86_64 构建，再创建**全新**夹具：

```sh
python3 experiments/openharmony_bridge/prepare_upgrade_fixture.py /tmp/m3-new-fixture \
  --binary experiments/openharmony_bridge/build-ha-static-x86_64/bridge_poc \
  --assets experiments/openharmony_bridge/assets
```

夹具从 `8af2809` 取旧版 `1.0.0` 组件，加入目标侧自定义文件标记、私有 profile 标记和旧后端条目；新网关使用随机未配对身份、过期 PIN 和 `oh_flow_*` 虚拟实体。它不读取任何生产配置。将夹具和安装包传到独立 x86_64 Linux，恢复目录0700、文件0600、测试 ELF 和 `bridge_poc` 0700。只传这套新夹具，不传现有网关的运行目录。

建立 `podman network create --internal <独立网络>`，用 HA `2026.10.1` 镜像创建一个**尚未启动**的目标容器。挂载夹具 `config:/config`、`tests:/tests:ro`、`tests:/tools:ro`，入口设为 `python3 /tests/ha_m3_install_test.py installed`。同一夹具重建容器时复用 `test-mac.private`，以便旧 SDK 保持同一个测试身份；只允许一个容器运行。

先按前面的预检、备份、安装命令操作该停止的容器，再启动它，将 stdout/stderr 写到权限0600的私有日志。退出码须为0，`config/m3-installed-results.json` 须为 `passed: true`。此阶段在真实 HA 状态上验证启用预检，并注入错误 profile、服务目录、执行位、网卡、端口及缺失实体，随后创建新条目、启动真实 C、验证虚拟风扇写入/读回和灯光拒绝。

保存结果后删除已退出容器，用相同目录和 MAC 重建，最后参数改成 `restarted`。检查 `m3-restarted-results.json`，验证新旧身份、已保存条目、真实旧 SDK 和新 C 的恢复。两个场景结束均检查新 C 已退出、旧 SDK 没有解绑。冷备份之前不会运行新旧 SDK。

随后在 HA 停止时执行还原；条目因测试发生变化，先确认默认还原拒绝，再用 `--restore-entries` 恢复旧条目。核对组件文件及权限与冷备份一致、SDK KV 未被还原工具覆盖。重建容器，最后参数改为 `restored`，检查 `m3-restored-results.json`，确认原组件真实加载、仅剩旧条目且旧 SDK 身份不变。不要把这些未配对 SDK 的结果称作云端或智慧生活控制验收。

文件安全回归可独立运行：

```sh
python3 -m unittest discover -s tools -p 'test_*.py'
```

本次还用独立旁路容器确认：HA 正在运行时禁止备份，另一个运行容器即使只读挂载目标配置也会阻止维护。该旁路容器不运行 HA/桥接，结束后停止并移除。
