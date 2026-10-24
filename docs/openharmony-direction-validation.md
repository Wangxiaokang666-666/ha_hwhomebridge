# 书房风扇方向修复的独立环境验收

修复源码提交为 `41d472a`。本轮只把 `openharmony/config/adapters/xiaomi/sflt11.json` 和 `openharmony/config/compositions.json` 同步到已配对的独立实验 HA；两份原文件另存私有备份，其余 52 个组件文件哈希不变。部署文件与源码哈希一致，不替换 C 程序、profile、SN 或配对 KV，不安装或重启原 HA。

## 网络恢复

故障时 Mac 可以访问 HA，实验 Linux 的公网 TCP 正常，但访问 HA 和平板的局域网 TCP 会立即被拒绝；Linux 没有拒绝流量的 filter 规则。正常停止并重新启动这台 Lima VZ 实例后，Linux 直连 HA 的认证请求及 13 个源实体的轮询恢复。没有增加 Mac 代理、SSH 数据隧道，也没有修改系统权限、原 HA 或米家自动化。

这些现象将故障范围缩小到虚拟网络出口；本轮没有确定最初的触发原因。Jev 仅辅助分类排查优先级，网络恢复由实际请求确认。

## 已完成的验证

- 25 项框架回归测试通过。
- 独立网关的 `aux/switch1` 和 `timer-controls/verticalswing` 分别执行反转开启、关闭，共四次真实 HA 服务写入。开启对应该型号 HA `forward`，关闭对应 HA `reverse`；每次两个卡片回报一致。
- 通过独立 HA 的真实选项流停用、启用后端，确认旧 C 子进程停止、新进程启动。首次 30 秒上线轮询未等到全部卡片，随后复查八张卡片全部上线，身份保持一致，无重新绑定或 PIN 回调。
- 重新启用后，HA `reverse` 正确回报为两个卡片的反转关闭。
- 源 HA 审计只有四次 `fan.set_direction`；九个灯光、设置及灯定时实体的完整状态记录保持一致。风扇开关、档位和测试前方向均保持原值。

上述四次写入来自独立后端的回环接口。MatePad 起初锁屏且调试连接断开，用户解锁后已恢复连接并补齐以下 App 验收：

- 在智慧生活里核对“书房风扇灯实验”的 SN，与独立网关保存的 `timer-controls` 身份一致。
- 实际点击“风扇反转”开启、关闭；App 截图分别显示蓝色开启、灰色关闭。源 HA 分别收到 `forward`、`reverse`，C 层记录两次 `timer-controls/verticalswing` 写入确认和两次服务读取回调。
- App 阶段只有这两次风扇方向写入。九个灯光相关实体的完整 HA 记录、风扇定时、开关和档位保持不变，最终方向恢复。网关及八张卡片在线，配对身份不变，C 程序无异常退出。
- 临时延长的平板熄屏时间已通过 `power-shell timeout -r` 恢复。

App 补验在前述后端重新启用之后进行，覆盖恢复后的实际控制。本轮没有通过 App 操作 `aux` 卡片；它的双向读写由前述回环接口验证。Jev 辅助区分了真实 App 链路证据和后台 API 证据；软件链路验证不包含现场观察叶片旋转方向。

脱敏结果见 [验收记录](../experiments/openharmony_bridge/direction-recovery-verification.json)。后续已将这两处修复打入 `1.1.0b4` 安装包，并完成独立 x86_64 安装、还原验证，详见 [b4 发布与预检记录](m3-b4-release-preflight.md)。旧 `b3` 归档保持不变。
