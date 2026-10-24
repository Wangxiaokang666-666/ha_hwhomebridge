# x86_64 与真实旧 SDK 共存验证

本阶段使用独立 x86_64 Linux 虚拟机，不在原 HA 或现用 ARM64 配对网关里运行测试。虚拟机采用 QEMU 完整系统仿真，包含 x86_64 内核及用户态；这与在 ARM64 内核上运行 `qemu-x86_64` 用户态程序不同，也不代表物理 x86_64 主机的性能验收。

## 本次结果（2026-10-16）

[脱敏记录](../experiments/openharmony_bridge/m2-verification.json) 包含两个目标的完整构建清单、平台信息及六个通过的 HA 进程场景。原始日志、MAC、AC 和身份资料保留在独立环境，不提交。

- 从干净源码归档加本轮构建入口重建 x86_64、ARM64，十个 ELF 的架构和静态链接检查通过；x86_64 在完整系统仿真的真实目标内核中运行 C 对齐、多子设备及持久化回归。
- HA 2026.10.1 / Python 3.14.6 运行原始 x86_64 旧库和真实 OpenHarmony C；旧库 SHA256 未变。目标库观测钩子按完整路径限定，其他图像库等动态加载保持原样。
- 六个独立 HA 进程覆盖两种创建顺序及重启恢复、保存旧后端停用、重启不加载旧库及重新启用。各通过场景的旧 SDK 最终初始化次数为 1，解绑次数为 0；原生 UDP 5683 保留。SDK 身份异步生成的启动窗口由验收就绪等待处理，产品组件代码未改。
- 两套全新 MAC 对应的旧网关身份及 AC 不同，各自跨重启保持；新网关根/子身份同样保持。全部服务调用只作用于虚拟实体，原生旧库保持未配对状态，自动云上报没有被强行开启。
- 框架 22 项测试和原产品配置校验通过；29 个原有保护文件一致，源 HA 13 个相关实体的状态与属性一致，现用 ARM64 配对网关的容器 ID 和启动时间一致。

尚未进行物理 x86_64 主机性能、这些新身份的配对/云端在线或原 HA 部署验收。后续按 M3–M5 准备安装包、备份、身份交接和生产验收。

## 构建与产物核对

从仓库根目录执行：

```sh
python3 experiments/openharmony_bridge/bootstrap.py
python3 experiments/openharmony_bridge/prepare_test_assets.py experiments/openharmony_bridge/assets
sh experiments/openharmony_bridge/build-ha-static.sh x86_64
sh experiments/openharmony_bridge/build-ha-static.sh aarch64
```

保留原来的缺省 ARM64 行为。输出分别在 `build-ha-static-x86_64/` 与 `build-ha-static-arm64/`，不能覆盖运行中的程序。每个目录的 `build-info.json` 记录源码文件哈希、源码汇总哈希、可取得的 Git revision、固定上游依赖、工具版本及五个 ELF 的架构/哈希。源码归档不含 Git 信息时标为 `source-archive`，通过源码文件哈希对照版本；不能把构建时的 HEAD 当成包含所有未提交变更的源码证明。

生成清单时拒绝错误架构、ELF 解释器和动态链接段。清单的 `linux_runtime_verified` 始终为 `false`：运行结果须由后续测试单独记录，构建脚本不能自行宣称通过 Linux 验收。继续保留上游补丁及检查器，未用关闭对齐或未定义行为检查来规避错误。

## 独立环境与新身份

可参考 `experiments/openharmony_bridge/m2-x86-lima.yaml` 创建专用虚拟机；固定 Ubuntu 镜像及 SHA256，不挂载项目或生产目录、不桥接生产局域网。macOS 使用该模板需要 QEMU 和 Lima x86_64 guest agent，可用 `limactl start --timeout=30m --tty=false --name hwhomebridge-m2-x86 experiments/openharmony_bridge/m2-x86-lima.yaml` 启动。首次启动须确认测试 VM 的 DNS 可用；CPU 仿真下包索引生成可能较慢，模板关闭无关的 command-not-found 索引。测试本身也可在其他独立 x86_64 Linux 上执行。

宿主侧先创建全新的目录：

```sh
python3 experiments/openharmony_bridge/prepare_coexistence_fixture.py /absolute/new-test-fixture \
  --binary /absolute/build-ha-static-x86_64/bridge_poc --arch x86_64 --real-legacy
```

夹具只复制组件 Python/公开 JSON、原始 x86_64 `libhilink_bridge.so`、两个只有公共服务地址的 SDK 启动配置，以及测试脚本。OpenHarmony 身份随机新建、PIN 已过期；旧 SDK 的 AC 和持久化资料由原组件在全新 `/config` 中创建。没有源 HA token、现有 SDK KV、生产实体绑定或已配对根身份。

`test-mac.private` 保存新生成的本地 MAC；同一夹具的 HA 进程重启必须使用同一 MAC，旧 SDK 会从 `eth0` 获取网络身份。目录及文件仍是私密测试资料，不提交。传输后校验属主，确保目录 `0700`、普通文件 `0600`、C 可执行文件 `0700`；不要放宽后端权限校验。

## 运行与恢复

在 x86_64 Linux 中先拉取 `ghcr.io/home-assistant/home-assistant:2026.10.1`。将 `OH_TEST_ROOT` 设置为仅属于测试的绝对目录，再执行：

```sh
podman network create --internal hwhomebridge-m2-test
umask 077
podman run --name hwhomebridge-m2-first --network hwhomebridge-m2-test \
  --mac-address "$(cat "$OH_TEST_ROOT/test-mac.private")" \
  -e PYTHONDONTWRITEBYTECODE= -e PYTHONPYCACHEPREFIX=/config/.cache/python \
  -v "$OH_TEST_ROOT/config:/config" -v "$OH_TEST_ROOT/tests:/tests:ro" \
  --entrypoint python3 ghcr.io/home-assistant/home-assistant:2026.10.1 \
  /tests/ha_real_sdk_test.py > "$OH_TEST_ROOT/first.private.log" 2>&1
```

不发布容器端口，网络 `Internal=true`，容器不能访问源 HA 或云服务。真实 SDK 的输出可能包含运行身份，只查看脱敏结果文件；不要公开原始日志。

完整测试成功后保存 `config/coexistence-results.json`，用同一配置、同一 MAC 再创建新容器，给脚本增加 `--restore`，验证两个入口随 HA 新进程恢复，以及旧 AC/根身份和新根/子身份保持。再生成另一套全新夹具，首次传 `--oh-first`，之后再次 `--restore`。四个结果要分别保存；结果文件会覆盖。

旧入口先创建的完整场景后，额外启动一个 HA 进程传 `--save-legacy-disabled`，验证只保存停用且旧库继续常驻；再启动新进程传 `--restore-legacy-disabled`，确认旧库未加载，然后重新启用旧入口且新 C 不重启。完成后再执行 `--restore`。本次验收应保留这两个额外结果，共六个独立 HA 进程场景。可复用同一镜像的 Python 字节码缓存；两套身份的 SDK 配置、AC、KV 和运行目录各自新建，使用不同的内部网络。

同一隔离 Linux 中还需执行：

```sh
./coap_alignment_test
python3 ha_child_smoke.py ./ha_child_state_test
python3 virtual_child_smoke.py ./virtual_child_state_test
```

这些程序由夹具放在 `tests/` 下，使用临时目录和回环服务，不访问实际 HA 或云端。被动检查镜像架构、虚拟机 `uname -m` 与测试解释器架构；不能只凭容器镜像标签推断内核架构。

## 真实 SDK 证据的范围

`ha_real_sdk_test.py` 复用 M1 的创建、启停、冲突、删除、崩溃及 HA 重启场景。加载器返回原始 `ctypes.CDLL`，原组件的库加载、`main()`、回调注册和状态查询均真实执行。`errcheck` 只观测完成的调用，不提供替身返回值。

每次检查确认库在进程映射中、原生初始化返回零、原生 UDP 5683 监听器存在、旧库仍未配对，以及旧设备注册条目的归属正确。`main()` 返回后身份可能仍在原生线程中异步生成，验收在 HA 启动完成后进行约 30 秒的有界等待，不改变旧组件的启动时序。对注册过的 ctypes 控制回调直接注入虚拟灯请求，再查询状态；这是本地回调检查，不是 SDK 从云端下发请求的证据。未配对状态下自动云上报应暂停，测试对此进行断言；虚拟 SN 尚未在原生 SDK 注册，不强行调用原生报告接口。原生子设备注册、云端控制和报告成功需要后续独立配对验收。

旧 SDK 按项目设计常驻到 HA 进程结束。测试在 HA 正常停止、新 C 已退出后结束整个进程，不调用旧库解绑来模拟普通重启。删除或重载新条目期间旧 SDK 必须仅启动一次，解绑次数必须为零。实际 App 配对、云端在线及生产迁移仍属于后续阶段。

Jev 辅助区分构建、用户态模拟、完整 x86_64 虚拟机、SDK 替身与真实 SDK 隔离验证。Codex 负责隔离措施、实现及验收结论。
