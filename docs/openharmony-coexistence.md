# 同一 HA 中的新旧后端共存

M0/M1 在独立 Linux ARM64、HA Core 2026.10.1 / Python 3.14.6 中完成。旧闭源 SDK 用调用替身；旧 Python 路由、ctypes 回调、设备注册和状态监听使用项目实现。OpenHarmony 使用从干净源码归档重建的真实静态 C 程序。测试只注册 `oh_flow_*` 虚拟实体，容器位于 Podman 内部网络，不携带生产 token、配对身份或旧 SDK 库。

脱敏结果见 [coexistence-verification.json](../experiments/openharmony_bridge/coexistence-verification.json)。本页 M1 结果不含新一轮智慧生活 App 验收、x86_64 或真实闭源 SDK 共存验收。后续完成的 M2 结果单列在 [x86_64 与真实 SDK 验证](openharmony-x86-sdk-validation.md)，生产安装和身份交接仍需完成 M3–M5。

## 条目与运行资源

- 旧 HiLink 保持一个条目，唯一 ID 仍为 `hwhomebridge`。没有后端字段的历史条目仍解释为旧后端。
- 新条目保存固定的 `backend_type=openharmony`，唯一 ID 为 `hwhomebridge:openharmony:<根 SN 的 SHA256>`。原型条目的 domain 唯一 ID 会在成功启动时迁移，根/子设备身份不变。
- 各条目只能启用自己的类型或停用。增加另一种后端须添加独立条目，不能通过选项覆盖已有条目。
- 旧 SDK 保持进程单例；设备注册按旧条目查找，不再假定列表第一条就是旧条目。删除 OpenHarmony 只停止该实例，不进入旧桥接解绑路径。
- 每个新实例拥有自己的目录、C 进程、回环/LAN 端口及停止/异常监听。重复根身份、目录、活动端口或可写实体会被拒绝；外部 UDP 端口占用会阻止候选实例启动。
- HA 启动串行检查实例资源，配置错误只让对应条目重试，不自动启动另一后端。停用时不要求损坏的运行目录可读，便于修复失败配置。
- 旧 SDK 常驻后选择停用，只保存选项并提示 HA 重启；当前进程中的旧桥接继续工作。此限制不会阻止启停新实例。

停用或删除新条目都保留其私有目录及身份。重新添加同一目录恢复同一网关；禁止在两台主机同时运行同一配对身份。M1 没有执行配对身份交接。

## 写入范围

私有 `deployment.json` 支持 `write_policy`：

| 值 | 可写范围 |
| --- | --- |
| `fan_only`，缺省值 | 已绑定风扇、风扇原生定时开关及持续时间 |
| `read_only` | 全部只读 |

其他值拒绝加载。完整灯光映射、八张组合卡片及状态读取继续保留；本阶段新实例拒绝主灯、氛围灯、灯效、灯光设置及灯定时写入。路由层先检查联合定时的全部目标，避免先执行风扇再拒绝灯光造成部分成功。HA 传输层再次核对实体白名单；不能伪造写入成功。HA 配置页显示当前实例、保存的选择和写入策略。

旧 HiLink 的灯光路由及产品映射不受该策略影响。上线准备时，还需在智慧生活卡片名称或说明中标出只读范围；不得改变已绑定的 card key、PID 或 SN。

## 复现隔离测试

先按[基线说明](openharmony-baseline.md)获取公开资源并构建 ARM64 程序。以下宿主命令从仓库根目录执行；只对全新的临时目录使用，不能指向正在运行的实例。

```sh
export OH_TEST_ROOT="$(mktemp -d)"
python3 - <<'PY'
import os, shutil
from pathlib import Path
root = Path(os.environ['OH_TEST_ROOT'])
source = Path('custom_components/hwhomebridge')
for item in source.rglob('*'):
    if item.suffix not in ('.py', '.json') or {'hilink_bridge', 'profile', '__pycache__'} & set(item.parts):
        continue
    target = root / 'config' / item
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(item, target)
(root / 'tests').mkdir()
shutil.copyfile('experiments/openharmony_bridge/ha_coexistence_test.py', root / 'tests/ha_coexistence_test.py')
PY
python3 experiments/openharmony_bridge/ha_configflow_fixture.py "$OH_TEST_ROOT/config" \
  --source experiments/openharmony_bridge \
  --binary experiments/openharmony_bridge/build-ha-static-arm64/bridge_poc \
  --assets experiments/openharmony_bridge/assets
```

将该目录传到独立 ARM64 Linux。传输后需核对 UID 并恢复目录 `0700`、文件 `0600`、`bridge_poc` `0700`；Python 安全解包过滤器可能扩大目录权限，后端会拒绝加载。确保 Linux 宿主路径中的测试目录专用，运行：

```sh
podman network create --internal hwhomebridge-coexistence-test
podman run --rm --network hwhomebridge-coexistence-test \
  -v "$OH_TEST_ROOT/config:/config" -v "$OH_TEST_ROOT/tests:/tests:ro" \
  --entrypoint python3 ghcr.io/home-assistant/home-assistant:2026.10.1 \
  /tests/ha_coexistence_test.py
```

保存 `/config/coexistence-results.json` 后，对同一目录重新运行并加 `--restore`，验证新的 HA 进程恢复两条配置和身份。再生成另一套全新夹具，首次加 `--oh-first`，覆盖反向顺序及原型 domain 唯一 ID 迁移；随后同样运行 `--restore`。结果文件每次覆盖，应分别保留。首次测试会主动 SIGKILL 新 C 进程，日志出现 `exited (-9)` 是故障注入的一部分；最终必须以退出码和 `passed: true` 为准。

## 本次覆盖与后续缺口

已覆盖两种创建顺序、历史条目、固定条目类型、重复身份/目录/端口/可写实体、风扇写入及读回、灯光和联合定时拒绝、独立只读实例、新实例启停/删除/重加、C 崩溃恢复、二进制哈希错误及修复、外部 LAN 端口占用，以及两种顺序下的 HA 整体重启。

每个完整场景中旧 SDK 启动记录为一次，解绑记录为零；旧 ctypes 控制和状态监听经过虚拟灯开关验证。框架 22 项回归还保留完整灯光行为验证，并覆盖风扇白名单。干净归档构建的 C 字节对齐、多子设备路由及虚拟开关持久化回归均在真实 ARM64 Linux 通过。原产品配置验证通过；只读核对的 13 个源 HA 实体前后完全一致。

M2 已另行完成 x86_64 Linux 虚拟机运行和使用独立未配对身份的真实旧 SDK 共存，详见 [M2 说明](openharmony-x86-sdk-validation.md)。M1 调用替身结果与 M2 原生库结果分别保存；两者均不代替后续生产控制验收。下一步为 M3 的安装、备份和部署预检。

Jev 用于 M0 文件用途分类，Codex 复核提交边界和生命周期逻辑；组件没有新增 Jev 运行时依赖。
