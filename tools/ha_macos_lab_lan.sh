#!/bin/bash
# 临时提供独立 Lima 实验机的二层网络；不连接 HA，不安装常驻服务。
set -euo pipefail

mode=${1:-check}
binary=${2:-}
expected=b8a72a62237312f2f756027dea504a844edeb40014702d4a320292c026d282b0
directory=/private/var/run/hwhomebridge-lan-test
socket=$directory/bridged.sock

if [[ $(uname -s) != Darwin || $(uname -m) != arm64 ]]; then
    echo '仅支持 Apple Silicon Mac 的独立实验网络。' >&2
    exit 2
fi
if [[ $mode != check && $mode != run ]]; then
    echo '用法：ha_macos_lab_lan.sh check|run /path/to/socket_vmnet' >&2
    exit 2
fi
if [[ ! -f $binary || ! -x $binary ]]; then
    echo '需要已下载的 socket_vmnet v1.2.2 arm64 官方二进制。' >&2
    exit 2
fi
actual=$(shasum -a 256 "$binary" | awk '{print $1}')
if [[ $actual != "$expected" ]]; then
    echo '二进制摘要不匹配，拒绝启动。' >&2
    exit 2
fi
/sbin/ifconfig en0 >/dev/null
if [[ -e $directory ]]; then
    echo '实验运行目录已存在，请先结束已有实验进程；本脚本不会覆盖。' >&2
    exit 2
fi
if [[ $mode == check ]]; then
    echo '已核对官方 arm64 二进制摘要和 en0；run 需要本机管理员权限。'
    echo "Lima unmanaged socket: $socket"
    exit 0
fi
if [[ $EUID != 0 ]]; then
    echo '请在本机终端用 sudo 运行；不要把管理员密码发到聊天。' >&2
    exit 2
fi

umask 077
# 父目录由 root 管理，mkdir 原子拒绝重复运行；不写 sudoers/LaunchDaemon。
mkdir -m 0755 "$directory"
child=
cleanup() {
    trap - EXIT INT TERM
    if [[ -n $child ]]; then
        kill "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    rm -f "$socket" "$directory/socket_vmnet"
    rmdir "$directory"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
install -o root -g wheel -m 0755 "$binary" "$directory/socket_vmnet"
actual=$(shasum -a 256 "$directory/socket_vmnet" | awk '{print $1}')
[[ $actual == "$expected" ]]
echo '正在启动临时实验桥接；以组件日志和 socket 可用为准。保留终端，测试结束按 Ctrl-C 清理。'
"$directory/socket_vmnet" --socket-group=staff --vmnet-mode=bridged \
    --vmnet-interface=en0 "$socket" &
child=$!
wait "$child"
