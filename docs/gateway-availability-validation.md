# 单网关的子设备离线隔离：1.1.0b7 验证

## 修复的问题

旧逻辑启动时预读所有卡片，一盏 `unavailable` 的灯会使整个受管网关启动失败。运行中读取失败，旧 SDK 又可能保留离线前缓存；OpenHarmony 子设备则始终发送 `online`。这些行为会阻碍保留原清单的生产身份接管。

新增公共 `availability.require_state()` 与 `SourceUnavailable`，只将缺失、`unknown`、`unavailable` 的实体作为临时离线。配置映射、实体归属、产品指纹、身份和参数错误继续拒绝，不把所有 `ValueError` 吞成离线。公共 HA 传输不再因为一个实体缺失而拒绝其他卡片的状态。

启动跳过单卡暂时离线；每张卡按实际读取的依赖判断可用性。未启用定时器时，不额外要求其剩余时间传感器可用。写入先确认当前状态可读，再调用 HA 服务。回环协议正常返回 `ok=true, available=true, state=...`；离线返回 `ok=false, available=false, error=SourceUnavailable`，不附带旧状态或伪造的关闭值。

旧 SDK 所有者允许从空缓存启动；每卡分别同步状态 `0/1`，不调用注册 `3` 或删除 `2`。读取失败清空该卡缓存，查询返回空指针；恢复后即使数值与离线前相同也重报。根网关重连会重新同步。该路径有公共回归和原生接口替身测试，本轮没有完整 x86 HA 的真实旧 SDK 回退验收。

OpenHarmony C 代码区分当前源可用性、待发送状态与云端已确认状态。离线清空上报缓存，发送 `offline`；恢复后发送 `online` 并刷新状态。同步失败前三次重试，此后每 30 秒开始下一轮，不永久卡死。根连接恢复重新同步。启动要求每张卡得到有效云端状态回执；回执可以是离线，不要求家里所有设备都在线。

## 部署合同

受管目录的 `deployment.json` 需要 `availability_protocol: 1`，且必须配套本轮重建的 `bridge_poc` 和校验摘要。未声明该协议的旧目录在停止工作引擎前被拒绝。不能只改一个数字继续用旧二进制；应在离线目录重新生成、核对原身份和完整映射。普通旧 SDK 条目不受这个新合同影响。

`manifest.json` 与 `tools/ha_upgrade.py` 同步为 `1.1.0b7`。包沿用保留式升级：Python 和两种架构的原生程序可更新，原 `config/`、`hilink_bridge/`、`profile/`、配置条目与配对运行目录不随包覆盖。包安装成功不表示已切换生产引擎。

## 本轮实测

- 本机和独立 ARM Linux：公共测试各 77 项、工具测试各 116 项通过，配置校验通过。
- aarch64、x86_64 静态编译通过。真实 C 子设备状态机在 ARM 原生 Linux、Linux 下 qemu-x86_64 两种方式运行通过；云端接口使用替身，不能算真实云/App 验收。
- 独立 HA Core 2026.10.1：一个已配对的虚构父网关、8 个旧品类卡及 7 个风扇/灯光补充卡共用一个原生进程。启动前让其中一个旧灯离线，实际云端确认该卡 `offline`，其余卡片可用；风扇六档写入与 HA 读回通过。运行中掉线、相同数值恢复、HA 重启及恢复后旧灯开关写入均通过。
- ARM 旧库不在已验真的接管哈希范围内。注入 OpenHarmony 进程故障后，系统拒绝未经验证的旧库，安全停用；显式重试恢复原身份。没有绕过这个限制，本轮不声称真实旧 SDK 成功回退。
- 独立 Linux 使用真实安装工具执行 `b6 → b7 → b6 → b7`，逐项验证 56 个包文件；原条目字节、配对目录、原配置/库/profile 保持不变。安装器没有启动 HA。安装后另行启动 HA 验证。
- 原 HA 只读复核：原条目与 22 个受保护文件未变，旧 SDK 网关仍报告在线，原 32 个设备清单保留。本轮未安装、重启或停止原 HA，未修改灯光或米家自动化。

真实云端与 HA 服务验证不等于智慧生活 App 按钮验收。各层结果和保留的缺口见 [脱敏记录](../experiments/openharmony_bridge/evidence/gateway-availability-validation.json)。

## 复现入口

```sh
python3 -m unittest discover -s tests
python3 -m unittest discover -s tools -p 'test_*.py'
python3 custom_components/hwhomebridge/validate_config.py
sh experiments/openharmony_bridge/build-ha-static.sh aarch64
sh experiments/openharmony_bridge/build-ha-static.sh x86_64

# 在独立 Linux 执行；仅回环替身，不启动 SDK 主任务。
python3 experiments/openharmony_bridge/ha_child_availability_test.py \
  experiments/openharmony_bridge/build-ha-static-arm64/ha_child_availability_test
python3 experiments/openharmony_bridge/ha_child_availability_test.py \
  experiments/openharmony_bridge/build-ha-static-x86_64/ha_child_availability_test --runner qemu-x86_64
```

真实 HA 入口为 `gateway_availability_ha_test.py exercise/restart`，需已标记、实体均为 `oh_single_` 的独立虚构夹具；ARM 使用 `--native-only`，不放宽生产校验。原接口名不能直接假设为 `eth0`，应在测试容器读取接口清单后配置；本轮 rootless Podman 的实际接口是 `tap0`。文件安装/还原入口为 `gateway_availability_install_test.py`，仅接受虚构网关且要求容器停止。

Jev 仅分类“单设备暂时离线”“映射配置错误”及“离线探针证据范围”；复杂实现、身份、授权与通过条件由 Codex 和确定性检查负责，集成没有新增 AI 运行依赖。

## 继续生产接管前

1. 完成冻结的 32 个旧设备与书房角色/定时/组合能力的最终离线受管目录，保留现有主灯卡和所有旧 SN。
2. 在可用 x86 Linux 上补验本轮代码的真实旧 SDK 回退；补齐智慧生活端组合卡控制。
3. 核对最终包与离线目录、刷新源清单并按既定维护方案交接唯一身份，再验证原 HA/App、重启及失败回退。

当前仍为 `production_adoption_ready=false`；这一轮解决离线隔离，不新建第二个生产网关。

## 1.1.0b8 后续进展

上文保留 `b7` 当时的证据范围。后续已补齐 32 个旧身份加 7 张补充卡片的离线受管目录，以及真实 amd64 HA/旧 SDK 的云端回退和重启恢复；运行于 ARM Linux 的 qemu-user 环境，不是物理 x86 主机。最新 App 验收、生产目录的启动阻止和后续交接门槛见[最终单网关准备](final-single-gateway-preparation.md)。生产仍未切换。
