# 旧 SDK 公开源码与单网关扩展调查

核查日期：2026-10-16。结论：找到同类 HiLink 网桥的公开 C 适配源码和头文件，明确了需要扩展的四个回调；**仍未取得原项目可重建的适配层及匹配的两架构 SDK，也未验证原网关同时注册旧灯光和新风扇。** 原 HA、配对文件、灯光和米家自动化没有访问或改动。

机器证据见 [调查记录](legacy-native-public-source-research.json) 和 [当前旧库静态检查](legacy-native-audit.json)。源码检索有范围限制，未命中不等于互联网绝对不存在。

## 上游及分支核查

- 上游 [main@9033ac0](https://github.com/Wangxiaokang666-666/ha_hwhomebridge/tree/9033ac073157f05dc9f3387b56efcb07aad4aaaf)：检查完整历史中24个可达提交，没有 C/C++ 源码、头文件或原生构建文件；发布标签 `v1.0.0`、`v1.0.1` 没有附加发布资产。
- 通过 GitHub API 检查上游与18个 fork，共19个仓库、24个当前分支文件树，返回均未截断。没有找到可重建 C 层。大部分使用当前两份库；较早 fork 仍保存早期 aarch64 库或其兼容补丁。
- 当前 amd64/aarch64 库均只有11个公开入口、无静态符号表。上游早期库、`caideone` 的 `codex/haos-gcompat` 版本各有10个公开入口、同样没有静态符号表；没有找到额外产品注册入口。
- 已有 [Issue #30](https://github.com/Wangxiaokang666-666/ha_hwhomebridge/issues/30) 请求桥接 C 源码和编译环境，[Issue #35](https://github.com/Wangxiaokang666-666/ha_hwhomebridge/issues/35) 请求风扇等品类。当前未找到维护者提供的适配源码或匹配 SDK 下载资料。只读取这些讨论，没有发消息或新建 Issue。

GitHub 搜索也覆盖 `HilinkSyncBrgDevStatus`、`HilinkGetBrgDevInfo`、`hilink_profile_bridge.c`、`map_prodIds` 和 SDK 库名。搜索结果用于发现线索，技术判断以实际文件和 ELF 为准。

补查的 [HUAWEI-HiLink/DeviceSDK](https://github.com/HUAWEI-HiLink/DeviceSDK) 当前文件树只有 README，没有 SDK 下载资产；[ESP8266 示例](https://github.com/xushoucai/esp8266-hilink) 为该芯片平台的端设备工程，也未找到本项目所需的 Linux 网桥适配入口。仓库名称包含 SDK 不代表提供了可用的两架构资源。

## 找到的公开源码能解决什么

### honyar：明确 C 适配合同，不能直接替换原库

[ppq-ppq/honyar@8b5a4df](https://github.com/ppq-ppq/honyar/tree/8b5a4dfd2970d4de4a17229d4f41f663f7594a1c) 包含：

- [`hilink_profile_bridge.h`](https://github.com/ppq-ppq/honyar/blob/8b5a4dfd2970d4de4a17229d4f41f663f7594a1c/hilink2/adapter/profile_adapter/include/hilink_profile_bridge.h)：设备信息、服务列表、上下行回调合同。
- [`hilink_profile_bridge.c`](https://github.com/ppq-ppq/honyar/blob/8b5a4dfd2970d4de4a17229d4f41f663f7594a1c/hilink2/adapter/profile_adapter/hilink_profile_bridge.c)：按子设备 SN 查找产品信息和服务列表的厂家实现。
- `hilink2/makefile`：使用 `arm-rockchip-linux-gnueabihf-` 工具链，链接闭源 `hilinkdevicesdk`、`hilinkota` 等库。

| 接口 | 公开示例中的职责 | 对本项目的意义 |
| --- | --- | --- |
| `HilinkGetBrgDevInfo` | 根据 SN 填写 PID、型号、设备类型等 | 新风扇必须返回自己的真实产品定义，不能冒用旧灯光索引 |
| `HilinkGetBrgSvcInfo` | 根据 SN 填写服务类型、SID 和数量 | 同一个 SDK 实例可以询问不同子设备的不同服务目录 |
| `HilinkPutBrgDevCharState` | 收到指定 SN、SID 的控制内容 | 应路由到公共 ServiceRouter，保留明确 SID |
| `HilinkGetBrgDevCharState` | 读取指定子设备、服务的真实状态 | 应复用公共回读并遵循 SDK 的缓冲区释放约定 |

这个公开 SDK 的 `.dynsym` **导入**上述四个回调，交由厂家适配层实现。当前 hwhomebridge 成品库则既不动态导入也不导出这些符号：对应适配已封装在成品内。由此判断，不能通过普通链接或 `LD_PRELOAD` 覆盖这四个入口来扩展现有成品；尚不排除未公开机制，但没有可验证证据。

两套 `hilink/`、`hilink2/` 所带 `libhilinkdevicesdk.so` 的 SHA256 相同，实际都是 **ELF32 ARM**。`hilink2` 两份静态库的217个设备 SDK 对象、7个 OTA 对象也全部为 ELF32 ARM。它们不是 x86_64 或 aarch64 SDK；第三方文档中泛称“x86_64 / 嵌入式 ARM”不能作为架构依据。

该仓库未声明仓库级许可证，所读适配文件保留 Huawei All rights reserved 版权头。本次只记录路径、合同观察、哈希和架构，没有将其源码、头文件、SDK、profile 或凭据复制进贡献代码。它不是原 hwhomebridge 的 Python 回调封装源码，不能直接拿来重建同一份库。

### OneConnect：可参考单父网关协议，不能证明旧绑定兼容

[xiasi0/ha-oneconnect-bridge@db23564](https://github.com/xiasi0/ha-oneconnect-bridge/tree/db23564189dc0d8cae8f097eb9e803ced40b76ef) 为 Apache-2.0 的 Python 实现。`business/child_manager.py` 在已有父会话内遍历注册子设备；`child_protocol.py` 处理 batchBind、devInfoSync、控制和状态上报。

检查该快照的配置流和存储实现，新增条目会生成新序列号，使用自己的持久化格式；未找到旧 hwhomebridge SDK KV 导入器。其已检查的产品目录也未包含本轮所需 `28BF` 或旧 `9MT*`/`9MU7` 系列条目。因此它能提供协议实现参考，不能视为保留原绑定的现成替代品。本次没有运行或安装该项目。

既有 OpenHarmony 实验已验证的独立身份仍是另一组证据，本轮没有重新构建、启动或配对它；不能把此前独立配对成功转算为原 SDK 身份迁移成功。

## 本轮独立验证

1. 静态审计两架构当前旧库、两份历史/补丁库和公开 ARM SDK，读取 ELF/符号及架构元数据，不导出数据段或配置。
2. 启动独立 x86_64 Linux VM，确认没有运行中的桥接容器或用户桥接服务。在官方 HA `2026.10.1` / Python `3.14.6` 镜像内，仅加载当前 amd64 库并查询符号。11个现有入口可解析，四个适配回调均不可解析。**没有调用 SDK 的 main、退出、注册、状态或其他入口。**
3. 用公开头文件交叉编译独立布局探针，在相同 Linux 容器中运行，得到 `BrgDevInfo=348` 字节、`prodId` 偏移40、`protType` 偏移344，服务表1920字节、上限20。这只确认该头文件的布局，不证明当前旧库采用同一版本合同。
4. 在 Mac 使用 Zig `0.15.1` 分别向 `x86_64-linux-gnu`、`aarch64-linux-gnu` 链接公开 ARM32 SDK，均按预期失败于对象架构不兼容。未执行链接测试程序；这不是 Linux 主机上的完整 SDK 构建。
5. 静态审计原有3项回归通过。没有修改组件运行代码、001–008目录、SN算法或原生库。

容器无网络、根文件系统只读、探针/库只读挂载、丢弃全部 capabilities，禁止提升权限；没有挂载 HA 配置、token、生产或实验配对目录。完成后容器退出，x86 VM 恢复停止；已配对的 aarch64 实验 VM 全程停止。

### 复现入口

静态工具命令仍见[扩展门槛](legacy-native-extension-gate.md)。动态查询工具需和 `audit_legacy_native.py` 放在同一个只读目录，目录中仅放待测库及探针；在准备好的独立 Linux 中执行：

```sh
podman run --rm --pull never --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -v /tmp/hwhomebridge-native-symbols:/work:ro --workdir /work \
  --entrypoint python3 ghcr.io/home-assistant/home-assistant:2026.10.1 \
  -B /work/probe_legacy_native_symbols.py /work/legacy.so
```

`tools/native_probes/header_layout.c` 只查询结构布局；`sdk_link.c` 只用于链接检查，**不要运行后者**。头文件及 SDK 需在仓库外另行准备；本仓库不附带这些第三方文件。

```sh
zig cc -target x86_64-linux-musl -static -I "$SDK_HEADER_DIR" \
  tools/native_probes/header_layout.c -o /tmp/header-layout
zig cc -target x86_64-linux-gnu -I "$SDK_HEADER_DIR" \
  tools/native_probes/sdk_link.c "$SDK_LIBRARY_DIR/libhilinkdevicesdk.a" \
  -o /tmp/sdk-link-check
```

## 原来的一个网关应怎样扩展

以下是基于公开合同的待实现方案，**不是已存在或已通过验收的扩展 ABI**：

1. 保留原 SDK 引擎、根 SN、配对目录、集成条目和一个运行实例。取得原适配源码，或由维护者在适配层增加带版本的产品目录接口；不需要要求厂商开放 SDK 核心源码，但需要匹配 SDK、头文件和两架构构建资料。
2. C 适配层从公共产品目录生成设备信息/服务目录，保持原001–008索引、实际 PID、SID 和已有子设备身份。追加风扇及组合产品，不改变旧槽位含义。
3. 四个适配回调使用同一个 SN→产品/服务目录。上下行和 `UpdateHAStatus` 的缓存也须覆盖新 SID/字段；只添加产品数组、不补状态缓存和回调路由不够。
4. Python 继续使用已提炼的角色、原生定时、组合和厂家映射。旧主灯沿用旧 VirtualDevice、SN/PID，新增风扇及补充功能卡都挂在同一个父身份下。
5. 能力查询返回 ABI 版本、支持目录及目录哈希；旧库无新能力时仅保留旧行为。明确正常停止与解绑分开、回调生命周期、SDK线程/内存分配和缓冲区上限。具体函数名/签名须结合取得的 SDK 确认，不猜内部地址或补写二进制。

取得构建资料后先用**独立全新测试身份**验证一个父网关内旧灯光与新风扇同时注册、App写入/回读及正常重启恢复；再单独验证旧 SDK 测试身份及其 KV 能否由重建库接管、回退。不能直接把生产在线备份拿来启动第二个同身份实例。

旧灯光需验证原有开关、亮度、色温和相关服务；书房风扇需验证六档、自然风、厂家方向极性、原生定时及完整灯光/氛围/设置组合。米家四档同步导致的档位回写仍按用户要求跳过，不改其自动化。只有两架构、唯一根身份、读写和重启证据齐全，才进入原配置流和正式安装包阶段。

如果拿不到原适配源码/扩展 ABI 或匹配的64位 SDK，旧库路线当前仍受阻。可继续研究 OpenHarmony 作为**唯一引擎**的完整兼容替换，但必须先证明全部旧产品和测试身份/KV兼容；目前没有依据承诺原生产网关无损接管，不以第二个网关绕过这个门槛。

给原维护者的最小资料清单已收敛为：去除凭据的桥接适配 C/头文件、当前 SDK 版本与 amd64/aarch64 库的获取/构建方式、旧产品/SID目录与版本约定、持久化格式兼容和正常停止接口。已有[问题草稿](legacy-native-extension-gate.md)可附到既有 #30/#35；本轮仅准备资料，尚未发送。公共资料已经能定位需要改的接口，但不能靠它补出原库里被静态封装的适配实现。

Jev `jev-1.13.0` 实际用于四条公开线索的角色分类；其 OneConnect 分类置信度较低，未用于自动选架构。源码、ABI、链接、身份及发行结论由 Codex 直接核对。
