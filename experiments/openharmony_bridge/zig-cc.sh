#!/bin/sh
set -eu
# 交叉编译仅作构建证据；运行验收必须在 Linux 单独执行。
exec zig cc -target "${POC_ZIG_TARGET:-x86_64-linux-musl}" "$@"
