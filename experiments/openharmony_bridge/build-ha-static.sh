#!/bin/sh
set -eu
# 每个目标独立输出，不覆盖当前运行中的程序。
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
arch=${1:-aarch64}
case "$arch" in
  aarch64) output=arm64 ;;
  x86_64) output=x86_64 ;;
  *) echo "Usage: $0 [aarch64|x86_64]" >&2; exit 2 ;;
esac
export POC_ZIG_TARGET="$arch-linux-musl"
build="$root/build-ha-static-$output"
cmake -S "$root" -B "$build" \
  -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR="$arch" \
  -DCMAKE_C_COMPILER="$root/zig-cc.sh" -DCMAKE_EXE_LINKER_FLAGS=-static \
  -DPOC_AILIFE_LEGACY_DISCOVERY=ON -DPOC_LAN_PORT=15684 -DPOC_LOG_LEVEL=4
cmake --build "$build" -j4
python3 "$root/build_manifest.py" "$build" "$arch"
