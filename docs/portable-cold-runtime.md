# HA OS 宿主免安装冷备运行时

2026-10-17。已准备 Linux x86_64 解压包，并在独立环境用 **HA OS 18.3 官方用户空间、真实 Docker 和合成身份**验证导出、校验、新目录还原。宿主不需要 Python、pip 或网络下载。**本轮没有把包传到原机，没有执行原机冷备、安装、停机、重启或设备控制。**

## 包内容与适用范围

包内是便携 CPython 3.12.15、cryptography 50.0.2、cffi 2.1.1、pycparser 3.11、冷备工具和离线身份解码器，含项目及依赖许可文件。没有 HiLink SDK、私有 profile、HA token、原机 MAC、网关身份或配对资料；不会启动 SDK。只提供 `self-test`、`export`、`verify`、`restore`。

Python 和 wheel 的原始 URL、固定 SHA256 在 `tools/cold_runtime.lock.json`；构建器只读本地缓存，拒绝缺失或哈希不符的依赖。入口清除外部 Python 配置，使用隔离模式，禁止写 `.pyc`，逐文件检查包清单。**先核对已发布压缩包 SHA256 再解压**；包内清单不是发布者签名，不能替代可信来源的压缩包哈希。

当前只验收 **Linux x86_64**，不是全静态程序，仍使用宿主 glibc。已通过 HA OS 18.3 实际用户空间的动态库加载；ARM、其他 HA OS 版本及真实 HA OS 内核启动仍需各自验收。包不包含 Docker 客户端，使用宿主现有 Docker；也不包含在线实体采集工具的完整运行依赖。

冷备合同不变：原未托管旧 SDK 条目、完整组件/运行目录/注册表、已验证版本的旧 amd64 SDK、原 MAC、稳定在线清单；真实 systemd 宿主、已停止的原所有者、关闭自动重启、无共享写入与可见宿主占用。工具只读本机 Docker/Podman 的 `inspect`/`ps`，不提供启停或容器 `exec`。普通插件/容器不能代替宿主。已经带托管网关目录的条目仍拒绝，不能误读旧路径遗留状态。

## 构建

在有 Python 3.12+ 的构建机，从完整可信源码检出运行。按 lock 中 URL 下载依赖到缓存；目标宿主不做这一步。构建时不执行这些依赖、不访问 HA、不读取本机凭据。

```sh
python3 tools/build_cold_runtime.py --cache /secure/cold-runtime-cache \
  --output /secure/new-cold-runtime-build
```

输出包含 `hwhomebridge-cold-runtime-x86_64/`、压缩包及 `.sha256`。同一源码和固定缓存两次构建的压缩包字节相同。Python 发行包的 terminfo 数据库不用于冷备，构建时省略，避免大小写不敏感文件系统的别名链接冲突；其他运行文件与许可保留。

本次最终包：`hwhomebridge-cold-runtime-linux-x86_64.tar.gz`。

```text
fe5732626b7e04e139c8484f863ac7529d310ad8744519818d665ca9c081c5f1
```

## 未来维护窗口的命令

以下仅为操作说明，本轮没有在原机执行。需另行安排停机窗口、排他所有者核对和安全输出位置；包可放在 root 控制的私有维护目录，不放进 HA 组件或配置目录。原接口、宿主配置路径与在线预检文件取自当时的实际核对结果，不照抄实验值。

```sh
# 核对可信SHA256后，解压到私有维护目录；不会安装系统包。
sha256sum -c hwhomebridge-cold-runtime-linux-x86_64.tar.gz.sha256
tar xzf hwhomebridge-cold-runtime-linux-x86_64.tar.gz
cd hwhomebridge-cold-runtime-x86_64
./cold-backup self-test

# 仅在已自行停好原所有者后。HA_CONFIG 是已经核对的宿主真实挂载路径。
# 工具不会替你停HA、关闭自动重启或处理其他所有者。
./cold-backup export --engine docker --container homeassistant \
  --config "$HA_CONFIG" --interface enp2s1 \
  --preflight /secure/preflight.private.json --output /secure/new-cold-export

# 离线验真，不连接华为云、不启动SDK；root创建的导出用root验真。
./cold-backup verify --output /secure/new-cold-export

# 仅还原到全新私有包装目录，结果在 new-raw-restore/config/。
# 目标不得位于原HA配置、导出目录内，也不能已存在。
./cold-backup restore --output /secure/new-cold-export --target /secure/new-raw-restore
```

导出和还原都包含完整 HA 集成条目，含其他集成凭据及配对资料，必须保持私有存储。`restore` 要求独立宿主管理员上下文；外层包装目录固定 0700，内部恢复原 UID/GID、普通权限和修改时间，保留空目录。特殊权限位拒绝，不静默改成其他模式；inode、ctime 不作为跨目录恢复承诺。校验失败不发布有效目标，也不覆盖原配置。

**原始还原不是原机部署或网关激活**，不会创建接管目录、改配置条目或启动后端。它用于离线恢复演练与后续转换输入，不能把 `config/` 直接覆盖到在线 HA。仍需冻结旧灯绑定、处理实体问题、完成原身份转换与正式父 SN/原生目录验收。

## 独立验收边界

从[官方 HA OS 18.3 发布](https://github.com/home-assistant/operating-system/releases/tag/18.3)的 generic x86_64 更新包校验 SHA256、提取实际 EROFS 根文件系统。在独立 Ubuntu VM 中运行这个用户空间，保持 VM 的真实 PID、网络和 `/proc`，通过本机 Docker socket 检查合成所有者。源配置在内外使用同一绝对路径，宿主文件占用检测没有通过路径别名绕过。没有伪造 PID 1、MAC、停机标志或修改冷备保护条件。便携 Python 的动态库边界依据[上游运行文档](https://github.com/astral-sh/python-build-standalone/blob/main/docs/running.rst)，最终以实际用户空间自检和冷备测试为准。

这验证了 HA OS 用户空间 ABI、无系统 Python 的启动、真实宿主进程与 Docker 检查；**不是启动一台完整 HA OS，也不是原机停机验收**。验收只使用虚构 HA 注册表和合成 v3/v4 配对数据，SDK 文件只作哈希与备份输入，没有加载或配对。所有测试容器和临时 bind mount 结束清理，实验 VM 停止。

脱敏结果见[免安装运行时验收](../experiments/openharmony_bridge/evidence/portable-cold-runtime.json)。Jev 只筛选便携目录、普通容器和在线文件复制三种候选；ABI、运行条件与通过判断由 Codex 和实际测试完成，工具不依赖 AI。

本机 179 项回归与配置校验通过，独立 Linux 的冷备和打包工具 32 项通过。最终包完整重跑了导出、校验、还原及故障拒绝；普通容器中自检与 Docker 只读访问可用，但冷备导出明确拒绝，没有产生目录。还原后的文件和目录元数据另行核对通过，最终包 SHA256 与实测文件一致。

下一步先刷新生产只读清单、记录旧灯绑定歧义，再另行安排维护窗口生成生产冷备；正式接管前还需复核冻结旧绑定。本轮没有改变单网关接管门槛或原 HA 安装包。

后续只读刷新和独立窗口演练已完成，见[生产冷备维护窗口手册](plans/production-cold-maintenance-window.md)。手册补齐 Supervisor watchdog 的记录与恢复、成功和失败后的原所有者恢复，以及原 Core 恢复运行后再做离线还原的顺序；原机仍未传包、停机或冷备。运行时源码和以上压缩包哈希不变。
