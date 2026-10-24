"""记录源码、固定依赖与静态 ELF；生成产物不能替代 Linux 运行验收。"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import subprocess

ROOT = Path(__file__).resolve().parent
SOURCES = (
    "CMakeLists.txt", "bootstrap.py", "sources.lock.json", "build-ha-static.sh",
    "zig-cc.sh", "build_manifest.py", "main.c", "linux_network.c", "poc_network.h",
    "ha_children.c", "ha_children.h", "virtual_child.c", "virtual_child.h",
    "coap_alignment_test.c", "ha_child_state_test.c", "virtual_child_state_test.c",
    "speke_vectors.c", "identity_state_probe.c", "ha_child_availability_test.c",
)
OUTPUTS = ("ha_child_availability_test", "bridge_poc", "coap_alignment_test", "ha_child_state_test", "virtual_child_state_test", "speke_vectors", "identity_state_probe")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_elf(path, arch):
    data = path.read_bytes()
    machine = {"aarch64": 183, "x86_64": 62}[arch]
    if data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != machine:
        raise ValueError(f"wrong ELF architecture: {path.name}")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    # PT_INTERP/PT_DYNAMIC indicate a dynamically linked output.
    if any(struct.unpack_from("<I", data, offset + i * size)[0] in (2, 3) for i in range(count)):
        raise ValueError(f"static ELF required: {path.name}")
    return {"architecture": arch, "static": True, "sha256": digest(path)}


def manifest(build, arch):
    source_files = {name: digest(ROOT / name) for name in SOURCES}
    source_hash = hashlib.sha256(json.dumps(source_files, sort_keys=True).encode()).hexdigest()
    revision = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True)
    result = {
        "version": 1, "target": arch + "-linux-musl",
        "source_revision": revision.stdout.strip() if revision.returncode == 0 else "source-archive",
        "source_files_sha256": source_files, "source_tree_sha256": source_hash,
        "dependencies": json.loads((ROOT / "sources.lock.json").read_text()),
        "zig_version": subprocess.check_output(["zig", "version"], text=True).strip(),
        "cmake_version": subprocess.check_output(["cmake", "--version"], text=True).splitlines()[0],
        "artifacts": {name: inspect_elf(build / name, arch) for name in OUTPUTS},
        "linux_runtime_verified": False,
    }
    (build / "build-info.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("build", type=Path)
    parser.add_argument("arch", choices=("aarch64", "x86_64"))
    args = parser.parse_args()
    result = manifest(args.build, args.arch)
    print(json.dumps({"target": result["target"], "static_artifacts": len(result["artifacts"])}))
