#!/usr/bin/env python3
"""真实 C 模块的隔离路由与持久化检查。不启动云连接，不替代 App 验收。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

binary = Path(sys.argv[1]).resolve()
checks = []
with tempfile.TemporaryDirectory(prefix="oh-virtual-child-") as temporary:
    root = Path(temporary)
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700)

    def run(*args, good=True, directory=runtime):
        result = subprocess.run([str(binary), str(directory), *args], capture_output=True, text=True, timeout=5)
        assert (result.returncode == 0) == good, (args[0], result.returncode)
        return result.stdout

    run("init")
    state_file = runtime / "virtual-child.json"
    initial = json.loads(state_file.read_text())
    assert initial["sn"].startswith("OHPOC-CSW-") and not initial["devId"] and initial["on"] == 0
    assert state_file.stat().st_mode & 0o777 == 0o600
    other = root / "same-parent"
    other.mkdir(mode=0o700)
    run("init", directory=other)
    assert json.loads((other / "virtual-child.json").read_text()) == initial
    checks.append("同一实验父身份生成相同子设备 SN/UDID，初始资料权限 0600")
    # 合成 ID 仅供本地测试，绝不传给云端。
    fixture = {**initial, "devId": "synthetic-owned-child"}
    state_file.write_text(json.dumps(fixture))
    run("put", fixture["devId"], "switch", '{"on":1}')
    assert '{"on":1}' in run("get", fixture["devId"], "switch")
    checks.append("子设备开关写入后由新 C 进程读回 1")
    saved = state_file.read_bytes()
    for action in ["get", "put"]:
        extra = ['{"on":0}'] if action == "put" else []
        run(action, "foreign-child", "switch", *extra, good=False)
        run(action, fixture["devId"], "netInfo", *extra, good=False)
        assert state_file.read_bytes() == saved
    checks.append("拒绝其他设备 ID 和未实现的服务，状态保持不变")
    invalid = ['{"on":2}', '{"on":-1}', '{"on":0.5}', '{"on":"1"}', '{"on":true}',
               '{"on":null}', '{}', '{"on":1,"on":0}', '{"on":1,"extra":0}',
               '{"on":0} garbage', '{"ON":0}', '[{"on":0}]', 'x' * 129]
    for payload in invalid:
        run("put", fixture["devId"], "switch", payload, good=False)
        assert state_file.read_bytes() == saved
    checks.append(f"{len(invalid)} 种无效开关请求被拒绝且不改变持久化状态")
    outside = root / "outside"
    outside.write_text("unchanged")
    (runtime / "virtual-child.tmp").symlink_to(outside)
    run("put", fixture["devId"], "switch", '{"on":0}', good=False)
    assert outside.read_text() == "unchanged" and state_file.read_bytes() == saved
    (runtime / "virtual-child.tmp").unlink()
    checks.append("持久化临时文件为符号链接时拒绝写入，外部文件及原状态不变")
    run("put", fixture["devId"], "switch", '{"on":0}')
    assert '{"on":0}' in run("get", fixture["devId"], "switch")
    checks.append("再次写入 0 并跨进程恢复，身份及云端 ID 保持一致")
    valid = json.loads(state_file.read_text())
    for changed in [{**valid, "parent_sn": "OHPOC-other-parent"}, {**valid, "sn": "OHPOC-CSW-other"},
                    {**valid, "on": True}, {**valid, "on": 2}, {**valid, "version": 2},
                    {**valid, "devId": "bad/id"}, {**valid, "extra": 1}]:
        state_file.write_text(json.dumps(changed))
        run("init", good=False)
    state_file.write_text("broken")
    run("init", good=False)
    state_file.write_text(json.dumps(valid))
    state_file.chmod(0o644)
    run("init", good=False)
    checks.append("拒绝损坏、跨父设备、错误版本、非法值及权限过宽的已保存资料，不静默重建身份")

print(json.dumps({"checks": checks, "network_started": False, "app_service_write_verified": False}, ensure_ascii=False, indent=2))
