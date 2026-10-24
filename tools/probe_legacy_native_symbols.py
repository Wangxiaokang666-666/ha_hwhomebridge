#!/usr/bin/env python3
"""仅在隔离 Linux 内加载旧库并查询符号；不调用 SDK 入口或读取配对配置。"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform

from audit_legacy_native import LEGACY_EXPORTS, SDK_ADAPTER_HOOKS


def probe(path):
    if platform.system() != "Linux":
        raise RuntimeError("run inside the documented isolated Linux container")
    library = ctypes.CDLL(str(path.resolve()), mode=os.RTLD_NOW | os.RTLD_LOCAL)
    symbols = {}
    for name in sorted(LEGACY_EXPORTS | set(SDK_ADAPTER_HOOKS)):
        try:
            getattr(library, name)
        except AttributeError:
            symbols[name] = False
        else:
            symbols[name] = True
    return {
        "schema_version": 1,
        "method": "dlopen and symbol lookup only; no SDK entry point called",
        "os": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "library_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "symbols": symbols,
        "single_gateway_new_fan_acceptance": "not_run",
        "release_gate": "not_met",
        "limitation": "Symbol lookup does not test registration, pairing, cloud connectivity or identity migration.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(probe(args.library), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
