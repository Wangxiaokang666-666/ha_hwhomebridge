# OpenHarmony 实现基线

本基线保留配置驱动后端、HA 配置流、自编 C 源码、固定上游依赖及隔离测试。生产旧 SDK、产品配置、灯光适配器和米家自动化不在迁移范围内。

## 交付与保留范围

| 内容 | 处理 |
| --- | --- |
| `custom_components/hwhomebridge/openharmony/` | 提交框架、实例示例和公开产品元数据，不包含实际实体绑定或配对资料 |
| 集成入口、配置流、常量和翻译 | 提交可选后端入口及后续共存实现，旧条目默认仍为 HiLink |
| 实验 C 源码、CMake、依赖锁及补丁脚本 | 显式提交；依赖下载到忽略的 `_deps/`，构建产物不提交 |
| 框架测试、C 回归和全新 HA 夹具 | 提交；只使用虚拟实体和新生成、PIN 已过期的身份 |
| 配对 KV、token、PIN、AC Key、私有实例和原始日志 | 保留本地私有目录，不提交、不复制到未配对测试夹具 |
| 历史 Mac REST 代理、SSH/发现中继及生产配对验收宿主 | 保留本地工作成果，不作为本次可交付基线 |
| profile 和 CA | 从公开地址获取并校验固定哈希；下载副本不提交 |

先前约 512 组历史代理对照依赖旧代理和其硬编码实体，不属于此可移植测试基线。正式基线保留独立的映射、灯效、定时及控制边界回归；不能把未提交历史工具作为新克隆环境的隐性依赖。

## 从源码重建

在仓库根目录执行，使用 Python 3.12+、CMake、Zig 和 curl。静态构建入口分别支持 Linux ARM64 与 x86_64，缺省仍为 ARM64；目标平台的真实运行证据须单独检查，不能用交叉编译结果代替。

```sh
python3 experiments/openharmony_bridge/bootstrap.py
python3 experiments/openharmony_bridge/prepare_test_assets.py experiments/openharmony_bridge/assets
sh experiments/openharmony_bridge/build-ha-static.sh aarch64
sh experiments/openharmony_bridge/build-ha-static.sh x86_64
python3 experiments/openharmony_bridge/framework_test.py
```

`bootstrap.py` 校验 `sources.lock.json` 中的下载哈希，并应用已固定上下文的兼容补丁，包含 CoAP/TLS 序号等非对齐网络访问修复。保留检查器，不能关闭检查掩盖问题。

将生成的 `coap_alignment_test`、`ha_child_state_test` 和 `virtual_child_state_test` 放入匹配架构的独立 Linux 后执行：

```sh
./coap_alignment_test
python3 ha_child_smoke.py ./ha_child_state_test
python3 virtual_child_smoke.py ./virtual_child_state_test
```

这些测试使用临时目录和回环假服务，不访问 HA 或华为云，不代表 App 验收。

## 独立 HA 夹具

向全新的测试 `/config` 放入组件 Python 源码、两个配置目录和翻译，排除旧 SDK 库、生产配置和所有私有文件。在宿主侧运行：

```sh
python3 experiments/openharmony_bridge/ha_configflow_fixture.py /absolute/test-config \
  --source experiments/openharmony_bridge \
  --binary experiments/openharmony_bridge/build-ha-static-arm64/bridge_poc \
  --assets experiments/openharmony_bridge/assets
```

夹具从已提交注册表生成 C 子设备目录，不再读取 `runtime-oh-cloud/` 或历史证据目录；生成随机新根身份、随机测试密钥及已过期 PIN。应仅挂载到新建隔离 HA 容器，不能挂载到生产 HA 或现用配对网关。

HA 配置目录中的实例不含 REST 连接或 token。配对文件仍为敏感数据，实验随机夹具也不应提交。HA 本身的状态/服务夹具必须仅接受 `oh_flow_*` 虚拟实体；旧 SDK 替身与真实 SDK 的证据必须区分。

## 验收边界

此前配对 App 验收在 ARM64 独立环境完成；M0 归档不重新操作真实风扇或灯光。同一 HA 新旧后端共存、旧条目兼容和故障隔离已在 M1 独立验证，复现步骤见[共存说明](openharmony-coexistence.md)；M2 的 x86_64 Linux 虚拟机及未配对真实旧 SDK 共存验证见 [M2 说明](openharmony-x86-sdk-validation.md)。部署通道、生产安装、配对跨主机交接及上线验收分别属于 M3–M5。

Jev 辅助文件用途分类，最终提交清单和私密资料排除由 Codex 复核。
