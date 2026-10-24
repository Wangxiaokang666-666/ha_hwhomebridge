# 原网关身份导入：SN 兼容、实际绑定冻结与离线验真

本轮继续原 HA 的单网关接管准备。原机仍运行已验收的 `1.1.0b6` 和旧 SDK；本页新增的源码与工具尚未安装到原 HA，也没有切换生产引擎。

## 已完成的三个检查

### 1. SN 解释以实际旧库行为为准

只对 SHA256 为 `0579e6f3cee0320d1533e5a96cc6d72a065a43050eff4edcb7afd6a9a52c7b58` 的旧库作结论。静态核对其文件初始化路径，随后在独立 ARM Linux 的 mount/network namespace 内通过 qemu 执行 x86_64 库的文件初始化和 `GetGatewaySN()`；不调用 SDK `main`、解绑或云接口，不载入生产身份。

| 合成输入 | 旧库实际行为 | 接管解析 |
| --- | --- | --- |
| 16 个安全 ASCII 字符＋LF | 保留 16 字符，文件不变 | 接受 |
| 16 个安全 ASCII 字符＋NUL | 保留 16 字符，文件不变 | 接受 |
| 较短字符＋LF | 生成新 SN 并改写合成文件 | 拒绝 |
| 较短字符＋NUL | 同样生成新 SN | 拒绝 |
| 超过 16 字符＋LF | 只读前 16 字符 | 拒绝，避免静默截断 |

关键不只是 `fgets(buffer, 17, file)`：该版本还检查长度恰为 16，否则走重生分支。不能用 `strip()` 或“1–39 字符都有效”的通用规则替代。`identity_store.legacy_gateway_sn()` 因此只接受上表两种明确格式，返回规范值而不改原文件。原机实际为第一种。

接管校验将规范 SN 与目标比较；LF 导入还要求原 SDK 版本和原始文件摘要收据。即使 LF/NUL 规范值相同，已记录的原始字节发生变化也拒绝继续。实验身份的既有子卡算法保持不变；正式父 SN 的补充卡使用独立命名空间和稳定摘要，不新建父网关。

合成探针见 [legacy_sn_probe.c](../experiments/openharmony_bridge/legacy_sn_probe.c)。它调用的是固定版本内部文件函数，**仅用于离线验收，不是扩展 ABI，也不是生产运行入口**。运行前必须检查完整库哈希，只在独立 Linux 的无网络 namespace 中把合成 `SNPROBE-` 文件目录挂载到该旧库默认读取位置，并用 strace 验证网络调用为零。不能把探针用于真实运行目录或其他库版本。

### 2. 冻结实际路由，避免实体顺序决定旧灯绑定

`ha_binding_freeze.py observe` 读取现有 HA 管理员诊断和配置条目状态；两次观察必须来自同一已加载旧后端进程、同一原条目、同一完整设备清单。不会加载原生库、调用 HA 服务或写原 HA。

`freeze` 完全离线，核对冷备、全部旧 SN、PID、实体归属、服务集合和产品配置；以实际常驻路由的实体选择生成 `bindings.json`，不采用匹配器重放得到的候选绑定。完整产品映射摘要由公共 `product_fingerprint()` 计算，受管路由启动时也会复核，避免控制语义悄悄变化。厂家覆盖无法唯一重建时拒绝。

实际完成 32 个旧设备冻结。书房旧灯的开关、亮度、色温均保留主灯绑定；厨房一个不可用灯组仍在原清单中。冻结收据保存不可用项，不改绑定、不排除该设备，也不把不可用状态当作关闭。

```sh
python3 tools/ha_binding_freeze.py observe \
  --cold /secure/cold-export --url http://HA_ADDRESS \
  --token-file /secure/ha-token --output /secure/observed
python3 tools/ha_binding_freeze.py freeze \
  --cold /secure/cold-export \
  --first /secure/observed/observation-1.private.json \
  --second /secure/observed/observation-2.private.json \
  --output /secure/frozen
```

所有输出必须是全新私有目录；观察包含家庭实体标识，不上传 Git。绑定清单与收据有摘要关联，缺少/新增子设备、跨设备实体、服务遗漏、后端或进程变化均拒绝。

### 3. 原身份离线转换及真正的 C 读取器验真

`ha_paired_identity_import.py` 只消费已验真的冷备、对应的绑定冻结、原品类 profile 和明确的云端连接观察。生产连接观察已从原 HA Python 进程持有的 socket inode 核对到解码的云主机，确认端口；不是套用实验默认值。读取行为未修改原 HA。

云端连接观察可通过 `ha_cloud_endpoint.py` 复现：沿用已固定公钥的独立 SSH，仅读取同一 Core 容器、其 Python 进程 FD 和 TCP 表，支持 IPv4/IPv6；只有已建立、属于该进程且匹配解码云主机 DNS 的连接才接受，端口有歧义就拒绝。不会执行容器命令或写原机。

转换器从原冷备取得 SN、48 字节 AC、原接口 MAC 和加密 KV，验证固定旧 SDK 后解码。根产品字段来自该版本已核对的常量；不复制实验父身份、PIN 或子设备身份。32 个旧子设备沿用各自的 SN 和云 ID，并标记 `existing_cloud_only`，不申请重新注册或覆盖旧云元数据。

```sh
python3 tools/ha_cloud_endpoint.py \
  --cold /secure/cold-export --host HA_HOST --port 22222 \
  --key /secure/maintenance-key --known-hosts /secure/pinned-hosts \
  --output /secure/endpoint
python3 tools/ha_paired_identity_import.py \
  --cold /secure/cold-export --frozen /secure/frozen \
  --profiles /secure/reviewed-profiles \
  --endpoint-proof /secure/endpoint/cloud-endpoint.private.json \
  --output /secure/imported-runtime
```

端点证据须关联原根 ID 摘要、解码主机摘要、DNS 地址及该 HA 进程持有的已建立连接。导入输出只包含离线验真所需的原生存储、旧子目录、网关参数和收据，**不是完整 HA 部署目录**；不包含启用配置或可用的新配对 PIN。

实际转换已由独立 Linux 上的 ARM 原生 `identity_state_probe` 读取；父网关 ID、secret、PSK、账号哈希、auth ID、auth code 全部摘要与原冷备一致。32 个旧子身份完整保留，网络 namespace 隔离且 strace 网络调用为零。没有启动 SDK 主任务、没有同时上线第二个生产身份。生产资料、云地址及其独立标识摘要只保存到私有目录。

## 验证与下一步

本机与独立 ARM Linux 的公共测试各 69 项、工具回归各 116 项通过，配置校验通过；新增测试覆盖 SN 重生/截断拒绝、原始字节收据、正式补充卡稳定性、常驻观察一致性、跨设备绑定、清单篡改和错误端点拒绝。原生验收与脱敏计数见 [验证记录](../experiments/openharmony_bridge/evidence/production-identity-offline.json)。

Jev 只分类证据：实际路由观察可以说明当前绑定，冷备及离线读取器不能替代云端/App 接管验收；没有决定授权、身份或通过。复杂判断由 Codex 与确定性检查负责。分类记录见 [Jev 记录](../experiments/openharmony_bridge/evidence/jev-production-adoption-evidence.json)。

尚未满足生产切换条件：

1. 单子设备离线隔离已在 `1.1.0b7` 实现，并通过独立 ARM HA 的实际云端、控制和重启验证；见 [离线隔离验收](gateway-availability-validation.md)。本轮尚未补齐 x86 HA 真实旧 SDK 的回退实测，不能据此宣布生产可切换。
2. 将冻结旧设备与书房组合能力汇入最终受管目录，保留旧主灯卡，校验六档、自然风、反转、原生定时、氛围灯和完整灯光映射。
3. 完成最终安装包及独立 HA 生命周期回归，才在原条目切换唯一引擎，再验收实际 App 控制、重启恢复和故障回退。本轮没有再次停止、重启或安装原 HA。

上述是身份导入阶段的门槛快照。`1.1.0b8` 已继续完成最终离线目录合成和独立 amd64 真实旧 SDK 回退，详见[最终单网关准备](final-single-gateway-preparation.md)。该目录仍禁止两种引擎启动，不能把离线验真解释为生产接管完成。
