#!/usr/bin/env python3
"""静态核对旧库的公开 ABI；不加载 ELF、不启动 SDK、不读取运行配置。"""
import argparse
import hashlib
import json
from pathlib import Path

LEGACY_EXPORTS = frozenset({
    "exit_brg", "main", "GetGatewaySN", "HILINK_SetAutoAc",
    "HILINK_SetPreferredIface", "HILINK_GetDevStatus",
    "RegPINQueryCallback", "RegGetRoomInfoCallback", "RegHomeAssistantPyCB",
    "HilinkSyncBrgDevStatus", "UpdateHAStatus",
})
EXPECTED_MACHINES = {"amd64": "EM_X86_64", "aarch64": "EM_AARCH64"}
SDK_ADAPTER_HOOKS = (
    "HilinkGetBrgDevInfo", "HilinkGetBrgSvcInfo",
    "HilinkPutBrgDevCharState", "HilinkGetBrgDevCharState",
)


def inspect_library(path, architecture):
    """仅公开符号名和文件元数据，不输出数据段或内置凭据。"""
    from elftools.elf.elffile import ELFFile

    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        elf = ELFFile(stream)
        if elf.elfclass != 64 or not elf.little_endian:
            raise ValueError("expected a little-endian ELF64 library")
        if elf["e_type"] != "ET_DYN" or elf["e_machine"] != EXPECTED_MACHINES[architecture]:
            raise ValueError("library type or architecture mismatch")
        table = elf.get_section_by_name(".dynsym")
        if table is None:
            raise ValueError("dynamic symbol table is missing")
        exports = {
            symbol.name for symbol in table.iter_symbols()
            if symbol.name and symbol["st_shndx"] != "SHN_UNDEF"
            and symbol["st_info"]["bind"] in ("STB_GLOBAL", "STB_WEAK")
            and symbol["st_other"]["visibility"] in ("STV_DEFAULT", "STV_PROTECTED")
        }
        missing = sorted(LEGACY_EXPORTS - exports)
        extra = sorted(exports - LEGACY_EXPORTS)
        imports = {symbol.name for symbol in table.iter_symbols()
                   if symbol["st_shndx"] == "SHN_UNDEF"}
        hooks = dict.fromkeys(SDK_ADAPTER_HOOKS, "absent_from_dynsym")
        for name in SDK_ADAPTER_HOOKS:
            if name in exports:
                hooks[name] = "exported"
            elif name in imports:
                hooks[name] = "imported"
        return {
            "architecture": architecture,
            "machine": elf["e_machine"],
            "sha256": digest,
            "size_bytes": path.stat().st_size,
            "has_static_symbol_table": elf.get_section_by_name(".symtab") is not None,
            "exports": sorted(exports),
            "missing_legacy_exports": missing,
            "unreviewed_exports": extra,
            "sdk_adapter_hooks": hooks,
            "abi_classification": "legacy_callbacks_only" if not missing and not extra else "needs_review",
        }


def audit(component):
    native = component / "hilink_bridge"
    sources = sorted(str(path.relative_to(component)) for path in native.rglob("*")
                     if path.is_file() and path.suffix in (".c", ".h", ".cc", ".cpp"))
    libraries = [inspect_library(native / "lib" / arch / "libhilink_bridge.so", arch)
                 for arch in EXPECTED_MACHINES]
    return {
        "schema_version": 1,
        "method": "ELF static inspection; no library load or SDK execution",
        "libraries": libraries,
        "bridge_sources": sources,
        "dynamic_product_registration": "not_verified",
        "single_gateway_new_fan_acceptance": "not_run",
        "release_gate": "not_met",
        "limitation": "Export names cannot prove internal behavior or an undocumented ABI. Source/headers or a reviewed extension ABI and isolated registration evidence are still required.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", type=Path, default=Path(__file__).resolve().parents[1] / "custom_components/hwhomebridge")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = json.dumps(audit(args.component), indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
