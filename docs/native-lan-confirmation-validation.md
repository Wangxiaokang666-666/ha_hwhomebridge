# 独立 LAN 与 C 确认链路验证（2026-10-21）

后续 App 直连网络准备、观察器的重启日志隔离修复与当前阻塞见 [App LAN 记录](app-direct-lan-acceptance.md)；下文保留该轮脚本协议验收的原始边界。

本轮不切换原 HA，不访问原 HA API、修改现有灯光或米家自动化。所有控制目标都来自已有的虚构配对身份和 `oh_single_` 实体清单。原生 x86 Linux 主机当前不可用，用户已确认暂无；这一项仍待补。

## 已定位并修复的 C 问题

旧 `ha_children.c` 的回环 HTTP 客户端一直读取到 EOF，才解析 JSON。即使已收到完整响应，服务端迟迟不关闭连接也会触发接收超时，从而把已经执行的 HA 控制记成未确认。

实际 C 对照测试向旧二进制返回完整的 `HTTP/1.0` 响应和正确的 `Content-Length`，随后保持连接 13 秒。旧二进制等到接收超时后拒绝（原配置为 12 秒），虚拟 HA 状态已经改变；修复后按帧长度结束读取，约 3 毫秒确认。这里的毫秒数仅是回环故障注入耗时，不能当作家庭设备延迟。

修复同时拒绝缺失、重复、异常的长度字段、额外帧字节、截断响应及不支持的 Transfer-Encoding。收到部分数据时继续接收；不能因为 JSON 前缀看似有效就忽略缺失的帧字节。保持既有 HTTP/1.0 回环服务合同，不新增写入重试。

C 新增进程内请求编号、读写类型、总耗时、系统错误及可公开的失败类型；Python 回环服务记录对应请求号、排队时间和执行结果。日志不包含请求载荷、设备身份、PIN、账号或密钥，服务端错误字符串仅在白名单中输出。已明确离线的常规轮询不会持续输出诊断告警。上游本地会话协商中的密钥 INFO 日志也由固定补丁移除。

**此前 App“空场景”那次未确认的根因仍未完全确定。** 旧日志没有记录传输阶段或服务端异常，不能把本次可复现缺陷直接认定为历史失败的唯一原因。

## 验收范围

| 项目 | 本轮结果 | 边界 |
| --- | --- | --- |
| Linux 构建 | ARM64、x86_64 各 7 个静态 ELF，源码及产物摘要核对通过 | x86 运行依赖 qemu-user |
| C 回环故障注入 | 两架构各 9 个场景通过；ARM 另与旧二进制对照复现 | 无 SDK 主任务或真实云端 |
| 原生 ARM Linux + 实际 ARM HA | 新 C 与正式公共路由运行，保持同一虚拟配对身份 | 不代表原生 x86 |
| 模拟 amd64 + 实际 amd64 HA | 新 C 启动、云端在线及 6 次加密 LAN 控制／HA 读回通过 | qemu-user，未验证物理 x86 性能 |
| 加密本地协议 | 发现、身份核对、会话协商、AES-GCM 控制及 HA 读回通过 | 手写协议客户端，不是智慧生活 App |
| 重复控制 | 30 次：风速一／六档、氛围灯、空场景，各反复写入及读回 | 全部为虚拟实体，每次只产生一个 HA 服务调用 |
| 反向校验 | 篡改 GCM、重复序号及未支持八档均被拒绝，零 HA 写入 | 不削弱 SDK 认证或重放校验 |
| Mac 局域网地址入口 | 显式重启后，专用 SSH/UDP 转发下发现及 6 次加密控制通过 | 同机地址测试，未证明外部物理对端或原机直连 LAN |
| 启停与重启 | 停用时原生所有者为零且发现无应答；显式重启后恢复同一身份和一个所有者 | 只启停独立虚拟网关 |
| 原生 x86 / 原机 LAN / App LAN | 待补 | 不能标为生产接管通过 |

本地发现使用 `st=cloudSetup`；配对后本地控制另使用 `st=ohLocalControl`、`.sys/sessMngr` 和 `e2eCtrl`。SDK 发现端口可以通过现有参数设置，本地控制端口仍是上游的 UDP 5686；不能把实验发现端口 15686 当作完整的 LAN 控制入口。生产端口与暴露方式仍需在正式接管前单独确认。

公共组件回归 85 项、工具回归 125 项及配置校验通过。工具回归在独立 Python 虚拟环境提供既有 `pyelftools` 测试依赖，未修改用户的全局 Python 或原 HA。两架构的 C 离线／恢复状态机及 CoAP 对齐回归也通过。脱敏摘要见[验证记录](../experiments/openharmony_bridge/evidence/native-lan-confirmation-validation.json)；原始日志、HA 夹具和编译产物只保留在本地私有实验目录。

## 可复现入口

```sh
# 独立 Linux 构建；固定依赖按 sources.lock.json 校验。
python3 experiments/openharmony_bridge/bootstrap.py
sh experiments/openharmony_bridge/build-ha-static.sh aarch64
sh experiments/openharmony_bridge/build-ha-static.sh x86_64

# 不联网、不启动 SDK 的真实 C 故障注入。
python3 experiments/openharmony_bridge/ha_backend_trace_test.py \
  experiments/openharmony_bridge/build-ha-static-arm64/ha_child_state_test
python3 experiments/openharmony_bridge/ha_backend_trace_test.py \
  experiments/openharmony_bridge/build-ha-static-x86_64/ha_child_state_test --runner qemu-x86_64

# 拿到独立 x86 Linux 宿主后先只读运行；结果只是候选环境判断。
python3 tools/ha_x86_host_preflight.py
```

`ha_backend_trace_test.py --baseline-binary /path/to/old/ha_child_state_test` 可额外对照旧 C：它只加载临时虚构子身份、回环替身和故障场景，不运行云端 SDK。

`gateway_lan_acceptance.py` 需要已有的独立 HA 虚拟夹具、匹配的自有 profile 和配对资料。它核对专用测试标记、`OHPOC-` 根身份及全部虚拟实体，再操作明确的实验卡；不会拿生产配置当实验输入。与 `gateway_app_acceptance.py` 同时运行后，使用测试环境的实际 IP/端口：

```sh
python3 experiments/openharmony_bridge/gateway_lan_acceptance.py \
  --config /isolated/config --address TEST_GATEWAY_IP \
  --discovery-port 15686 --control-port 5686 --repeats 5
```

Jev 只用于分类“发现通道”“认证控制”“模拟 x86”和“历史原因证据不足”；协议、文件摘要、身份、实际写入次数和验收结论由确定性检查负责。集成没有新增 AI 运行依赖。

本轮没有重打既有 b8 包，也没有部署到原 HA。生产准备目录继续保持 `offline_preparation: true`。贡献分支及分批 PR 说明见[贡献材料](pull-requests/contribution-series.md)。
