# 旧 SDK 测试身份离线导入

范围：只处理 `single-engine-virtual-only` 实验目录。此入口不是原 HA 安装器，不能传入家庭网关资料。当前支持经过核对的 amd64 SDK 二进制、根配置 v3、子配置 v4。后续已补齐多子设备及 ARM Linux 运行，见[多子设备与 ARM 记录](legacy-multi-arm-evaluation.md)；下文保留首次单灯接管的历史证据。

## 本轮闭环结果

[机器验收记录](../../experiments/openharmony_bridge/evidence/legacy-identity-handover-evaluation.json)保存脱敏结果；原始凭据、文件、GDB 日志及冷备份均留在私有实验目录，没有提交。

| 步骤 | 已取得的证据 |
| --- | --- |
| 旧 SDK 修复基线 | 原 `.so` 不变，智慧生活开关、亮度、色温均写入并读回；虚拟 HA 与旧引擎冷重启后仍可控制 |
| 离线转换 | 原生读取器确认同一根 ID 和五组登录/授权字段；错误 AC、错误 UDID、损坏目标密文均拒绝 |
| 唯一引擎接管 | 停旧引擎后启动 OpenHarmony，原根和原灯云端 ID 不变；只上线，不新增绑定或同步旧设备资料 |
| 接管后 App 控制 | 同一张旧灯卡完成开关、45% 亮度、2009K 色温写入，C 确认及虚拟 HA 读回一致 |
| 整组冷重启 | 虚拟 HA 和 C 引擎均重启，根/子身份与开灯、45%、2009K 状态一致；无 PIN、新绑定或资料同步，第二所有者被锁拒绝 |
| 重启后控制 | App 显示 45% 与 2009K，再关灯成功，C 和 HA 确认 |
| 回退 | 停 OpenHarmony，以原 KV 启动旧 SDK；原灯卡重新开灯成功，保留 45% 和 2009K；旧 KV 摘要未变，无 PIN、解绑或恢复出厂 |
| 回退后的独立身份核对 | 再次读取旧 SDK 原生测试缓存，根 ID 和全部五组字段摘要与离线转换完全一致 |

本轮公共框架 46 项、框架接入 25 项、工具 42 项和新增离线/回调 5 项测试通过；原配置校验、x86 Linux 原生身份边界检查通过。x86_64/aarch64 均构建六个静态 ELF，实际 App 验收只在 x86_64 Linux 上执行。测试容器正常停止、x86 实验 VM 已关机，平板熄屏设置恢复；未调用解绑入口，私有身份保留供后续验证。

Jev 仅对公开故障现象做任务分类，见[记录](../../experiments/openharmony_bridge/evidence/jev-legacy-handover-routing.json)。栈溢出定位、文件格式、密码算法、实现和验收结论由 Codex 核对，没有向 Jev 发送身份或凭据。

## 崩溃原因与修复

旧 SDK 控制实验灯时，原封装在 SDK 的小栈线程中同步执行 HTTP、JSON 和字符编码。两次 GDB 捕获的 SIGSEGV 都落在 Python 3.14 执行期间，栈指针已进入原生线程的保护页。空 SID 保护和返回空状态无法解决这个问题；不能把旁边的 `svcNum=0` 日志当作根因。

现在 SDK 回调只做有界命令入队和缓存读取。主线程负责 HTTP、JSON、状态确认及 SDK 状态上报；缓存 C 缓冲区与回调对象始终保活。正常停机结束独立进程，不调用会解绑的 `exit_brg`，也不让 Python 在 SDK 线程仍运行时回收回调。原 `.so` 文件没有修改，没有按私有函数偏移打补丁。

本轮已经用智慧生活真实操作旧 SDK 虚拟灯，完成开关、亮度和 6491K 色温写入；冷重启虚拟 HA 和旧引擎后，又完成关灯和 HA 读回，没有再次请求 PIN。

## 离线文件合同

需要原 48 字节 AC、原网卡的 6 字节 MAC、根 SN 和型号等公开产品参数，以及停止后的 `config.cfg`、`bridge.cfg`。MAC 是旧版本本地加密材料的一部分，只有 SN 和文件不足以解密。

| 文件 | 已验证格式 | 检查 |
| --- | --- | --- |
| `config.cfg` | 1588 字节，头部长度 1552，版本 3 | 文件 SHA256；HMAC-SHA256 派生 AES-CBC 密钥；另一个 salt 的 HKDF-SHA256 解开 UID 摘要 |
| `bridge.cfg` | 5192 字节，长度 5136，版本 4 | 旧 SDK 的 SHA256 范围、salt 和 AES-CBC；首次核对单条记录；现接受最多64条，逐条校验ID/SN及空闲区域 |
| `dev_data` | OpenHarmony 512 字节配置加 56 字节头部 | 48 字节 AC 和产品 UDID 参与 HKDF；AES-GCM |
| `conn_data` | OpenHarmony 128 字节连接配置加 56 字节头部 | HKDF 后的 HMAC-SHA256，已联网标志 |

旧格式的 SHA256 是损坏检查，不是认证加密。尤其子文件最后一个密文块不在旧校验范围内，因此导入器还检查未使用记录全零；不把旧校验能力夸大成防篡改保证。输入应来自本地可信冷备份。

根云端 ID、secret/PSK、授权码、授权码 ID 和 UID 摘要已与旧 SDK 实际载入的测试数据比对。RAM 读取仅用于独立核对算法，转换代码只读文件并使用标准密码库，不读取进程内存，也不调用 SDK 私有函数。

转换生成新目录和私有回执，不覆盖源 KV。目标存储再交给自编 OpenHarmony `identity_state_probe` 读取，逐字段比较摘要。探针不启动任务或网络。测试覆盖错误 AC、错误 MAC、文件损坏、未知版本和回调边界。

## 旧灯身份处理

保留旧灯 SN 和原云端 `devId`。`existing_cloud_only` 模式只请求该 ID 上线和读写，跳过 `batchBind`、`devInfoSync`，不更改云端已有 UDID、型号、MAC 或父关系。目标文件里的 UDID 只是实验 C 读取器的本地一致性标记，不宣称已经解出旧云端 UDID。缺少原云端 ID 时直接拒绝，不退化为新增绑定。

原云端资料更新及正式 HA 宿主仍须后续验证；多子设备和其余旧品类的当前结果见后续 ARM 记录。

## 操作顺序

先冷停止唯一 SDK 所有者，并确认文件锁已释放。保留完整冷备份和原 MAC。用单独 Python 环境安装 `cryptography`，在实验冷副本上运行：

```sh
python3 experiments/openharmony_bridge/legacy_identity_import.py \
  "$LAB_COPY" "$NEW_IMPORT_DIRECTORY" \
  --legacy-mac "$ORIGINAL_TEST_MAC" --cloud-port "$OBSERVED_TEST_CLOUD_PORT" \
  --legacy-sdk "$VERIFIED_AMD64_SDK"

# Linux：与 bridge_poc 同一构建目录下的原生离线读取器。
"$STATIC_BUILD/identity_state_probe" \
  "$NEW_IMPORT_DIRECTORY/sdk-state" "$NEW_IMPORT_DIRECTORY/gateway.local.json"
```

端口必须来自该测试网关实际连接；本轮观察值为 5683。不能靠猜默认端口。该旧格式没有经过核对的备用端点合同，本轮仅转换主端点。

显式实验接管要求输出位于 `$LAB/offline-import/<name>`，并验证源摘要、根产品参数、原生读取结果和子身份，然后在同一 `engine-owner.lock` 下启动：

```sh
python3 -B experiments/openharmony_bridge/single_engine_owner.py \
  openharmony "$LAB" "$STATIC_BUILD/bridge_poc" \
  --import-directory "$LAB/offline-import/converted"
```

没有显式导入参数，旧配对目录依然禁止启动新引擎。正常回退是停 OpenHarmony、检查锁释放、启动原旧 SDK，继续读取原 KV 和最新 HA 状态；不恢复过期业务状态、不解绑、不重新配对。接管、重启和回退的在线结果以本轮机器验收记录为准。
