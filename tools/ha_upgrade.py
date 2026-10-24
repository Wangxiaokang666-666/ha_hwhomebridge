"""离线升级工具：默认预检，只支持已有 HiLink 组件的保留式升级。

在目标 Linux 的文件通道中运行；本机容器检查是停机证据，不是跨主机身份锁。
不迁移 SDK KV，不调用 HA 服务，不启动/停止 HA 或桥接。
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import stat
import struct
import subprocess
import tempfile
import zipfile

COMPONENT = Path("custom_components/hwhomebridge")
PRESERVE = ("config/", "hilink_bridge/", "profile/")
VERSION = "1.1.0b11"
TESTED_HA = "2026.10.2"
MAX_BYTES = 128 * 1024 * 1024
CONTAINER_ENGINE = "podman"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text())


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def regular_tree(root):
    """只处理普通文件；拒绝链接、设备及无法由当前用户保全的属主。"""
    result = {}
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        if info.st_uid != os.getuid() or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("owned regular files/directories required")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = {
                "sha256": digest(path.read_bytes()), "mode": stat.S_IMODE(info.st_mode)}
    return result


def safe_absolute(path):
    path = path.absolute()
    if path.resolve() != path or ".." in path.parts:
        raise ValueError("symlink-free absolute path required")
    return path


def preserved(name):
    return any(name.startswith(prefix) for prefix in PRESERVE)


def without_bytecode(tree):
    return {name: detail for name, detail in tree.items() if '__pycache__' not in PurePosixPath(name).parts}


def elf(data, architecture):
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != {"x86_64": 62, "aarch64": 183}[architecture]:
        raise ValueError("native ELF architecture mismatch")
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    if size < 56 or offset + size * count > len(data):
        raise ValueError("invalid ELF headers")
    if any(struct.unpack_from("<I", data, offset + size * i)[0] in (2, 3) for i in range(count)):
        raise ValueError("static native ELF required")


def payload_allowed(name):
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).as_posix() != name or name.startswith("/") or ".." in parts or "\\" in name:
        return False
    if preserved(name) or ".local." in name or "__pycache__" in parts:
        return False
    if len(parts) == 1:
        return name.endswith((".py", ".json"))
    return (parts[0] == "translations" and len(parts) == 2 and name.endswith(".json") or
            parts[0] == "brand" and len(parts) == 2 and name.endswith(".png") or
            parts[0] == "openharmony" and (name.endswith((".py", ".json")) or
                len(parts) == 4 and parts[1] == "bin" and parts[2] in ("x86_64", "aarch64") and parts[3] in ("bridge_poc", "identity_state_probe")))


def build(repo, output, builds):
    repo = safe_absolute(repo)
    if output.exists():
        raise ValueError("new output directory required")
    payload = {}
    tracked = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z", str(COMPONENT)], text=True)
    for relative in filter(None, tracked.split("\0")):
        name = Path(relative).relative_to(COMPONENT).as_posix()
        if payload_allowed(name):
            source = repo / relative
            if source.is_symlink() or not source.is_file():
                raise ValueError("regular tracked source required")
            payload[name] = source.read_bytes()
    if json.loads(payload["manifest.json"])["version"] != VERSION:
        raise ValueError("component version differs from release tool")
    provenance = {}
    for architecture, directory in builds.items():
        info = read_json(directory / "build-info.json")
        source = repo / "experiments/openharmony_bridge"
        for name, expected in info["source_files_sha256"].items():
            if digest((source / name).read_bytes()) != expected:
                raise ValueError("native build source differs from checkout")
        binary = (directory / "bridge_poc").read_bytes()
        elf(binary, architecture)
        if info["target"] != architecture + "-linux-musl" or digest(binary) != info["artifacts"]["bridge_poc"]["sha256"]:
            raise ValueError("native build manifest mismatch")
        payload[f"openharmony/bin/{architecture}/bridge_poc"] = binary
        probe = (directory / "identity_state_probe").read_bytes()
        elf(probe, architecture)
        if digest(probe) != info['artifacts']['identity_state_probe']['sha256']:
            raise ValueError('identity probe build checksum mismatch')
        payload[f"openharmony/bin/{architecture}/identity_state_probe"] = probe
        payload[f"openharmony/bin/{architecture}/build-info.json"] = (directory / "build-info.json").read_bytes()
        provenance[architecture] = info["source_tree_sha256"]
    manifest = {"format": 1, "kind": "hwhomebridge-preserving-upgrade", "version": VERSION,
                "tested_ha": TESTED_HA, "architectures": sorted(builds), "native_sources": provenance,
                "source_revision": subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip(),
                "preserve_prefixes": list(PRESERVE), "files": {name: digest(data) for name, data in sorted(payload.items())}}
    output.mkdir(parents=True, mode=0o700)
    archive = output / f"hwhomebridge-{VERSION}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as stream:
        items = {"release.json": json.dumps(manifest, sort_keys=True, indent=2).encode(),
                 **{"component/" + k: v for k, v in payload.items()}}
        for name, data in sorted(items.items()):
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            stream.writestr(entry, data)
    archive.chmod(0o600)
    package_hash = digest(archive.read_bytes())
    write_json(output / "release.json", {**manifest, "archive_sha256": package_hash})
    sums = [f"{package_hash}  {archive.name}"]
    for name in ("ha_upgrade.py", "ha_runtime_preflight.py", "ha_production_readonly.py", "ha_online_backup.py"):
        data = Path(__file__).with_name(name).read_bytes()
        (output / name).write_bytes(data)
        (output / name).chmod(0o600)
        sums.append(f"{digest(data)}  {name}")
    (output / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return {"version": VERSION, "archive_sha256": package_hash, "payload_files": len(payload), "preserved_content_shipped": False}


def load_package(path, expected):
    if path.stat().st_size > MAX_BYTES or digest(path.read_bytes()) != expected:
        raise ValueError("package SHA256 mismatch or oversized package")
    with zipfile.ZipFile(path) as stream:
        entries = stream.infolist()
        names = [i.filename for i in entries]
        if len(names) != len(set(names)) or sum(i.file_size for i in entries) > MAX_BYTES:
            raise ValueError("duplicate or oversized package contents")
        if any(stat.S_ISLNK(i.external_attr >> 16) or i.is_dir() for i in entries):
            raise ValueError("regular package members required")
        manifest = json.loads(stream.read("release.json"))
        files = manifest["files"]
        if manifest["format"] != 1 or manifest["kind"] != "hwhomebridge-preserving-upgrade" or manifest["version"] != VERSION:
            raise ValueError("unsupported release")
        if manifest.get("tested_ha") != TESTED_HA or set(manifest.get("architectures", [])) != {"aarch64", "x86_64"}:
            raise ValueError("release compatibility metadata mismatch")
        if set(names) != {"release.json", *("component/" + n for n in files)} or not all(payload_allowed(n) for n in files):
            raise ValueError("package scope violation")
        payload = {name: stream.read("component/" + name) for name in files}
    if any(digest(data) != files[name] for name, data in payload.items()):
        raise ValueError("package member checksum mismatch")
    if json.loads(payload["manifest.json"])["version"] != manifest["version"]:
        raise ValueError("component version mismatch")
    for arch in manifest["architectures"]:
        binary = payload[f"openharmony/bin/{arch}/bridge_poc"]
        info = json.loads(payload[f"openharmony/bin/{arch}/build-info.json"])
        elf(binary, arch)
        probe = payload[f"openharmony/bin/{arch}/identity_state_probe"]
        elf(probe, arch)
        if digest(probe) != info['artifacts']['identity_state_probe']['sha256']:
            raise ValueError('identity probe checksum mismatch')
        source_hash = digest(json.dumps(info['source_files_sha256'], sort_keys=True).encode())
        if (digest(binary) != info["artifacts"]["bridge_poc"]["sha256"] or info['target'] != arch + '-linux-musl' or
                source_hash != info['source_tree_sha256'] or source_hash != manifest['native_sources'][arch]):
            raise ValueError("native checksum mismatch")
    return manifest, payload


def podman(*args):
    # 保留函数名供既有离线调用方使用；显式选择本机引擎，不继承远程context。
    if CONTAINER_ENGINE == "docker":
        prefix = ["docker", "--host", "unix:///var/run/docker.sock"]
    elif CONTAINER_ENGINE == "podman":
        prefix = ["podman", "--remote=false"]
    else:
        raise ValueError("explicit local Docker/Podman engine required")
    environment = dict(os.environ)
    for name in ("DOCKER_CONTEXT", "DOCKER_HOST", "CONTAINER_HOST", "CONTAINER_CONNECTION"):
        environment.pop(name, None)
    result = subprocess.run([*prefix, *args], env=environment, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise ValueError("local container operation failed; inspect private diagnostics on target")
    return result.stdout


def overlap(a, b):
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def stopped_container(config, container):
    info = json.loads(podman("inspect", container))[0]
    mounts = info.get("Mounts", [])
    if not any(m.get("Destination") == "/config" and Path(m["Source"]).resolve() == config for m in mounts):
        raise ValueError("container must mount this exact HA config at /config")
    if info["State"].get("Running") or info["State"].get("Paused") or info["State"].get("Restarting"):
        raise ValueError("target HA must be stopped before maintenance")
    if info["HostConfig"].get("RestartPolicy", {}).get("Name", "") not in ("", "no", "none"):
        raise ValueError("disable target auto-restart before maintenance")
    ids = podman("ps", "-q").split()
    if ids:
        running = json.loads(podman("inspect", *ids))
        for other in running:
            for mount in other.get("Mounts", []):
                if mount.get("Source") and overlap(Path(mount["Source"]).resolve(), config):
                    raise ValueError("running container shares target files; stop it first")
    return info


def target_paths(config):
    config = safe_absolute(config)
    component = config / COMPONENT
    entries = config / ".storage/core.config_entries"
    if not component.is_dir() or not entries.is_file():
        raise ValueError("existing component and core.config_entries required; upgrade only")
    for path in (component, entries):
        safe_absolute(path)
    regular_tree(component)
    if entries.stat().st_uid != os.getuid():
        raise ValueError("HA file-channel UID must own target files")
    if not all(os.access(path, os.W_OK | os.X_OK) for path in (config, component.parent, entries.parent)):
        raise ValueError("writable HA component and storage directories required")
    return config, component, entries


def runtime_probe_result(raw):
    marked = [line[len('HA_RUNTIME_PREFLIGHT '):] for line in raw.splitlines()
              if line.startswith('HA_RUNTIME_PREFLIGHT ')]
    if len(marked) > 1:
        raise ValueError('ambiguous target runtime probe output')
    # 兼容早期完整JSON响应；有启动器额外输出时只接受唯一的明确标记。
    try:
        result = json.loads(marked[0] if marked else raw)
    except (ValueError, TypeError) as error:
        raise ValueError('target runtime probe did not return a valid result') from error
    if not isinstance(result, dict):
        raise ValueError('target runtime probe must return an object')
    return result


def integration_entries(storage):
    return [entry for entry in storage["data"]["entries"] if entry["domain"] == "hwhomebridge"]


def preflight(config, package, sha256, container, probe):
    config, component, entries = target_paths(config)
    manifest, payload = load_package(package, sha256)
    info = stopped_container(config, container)
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    if platform.system() != "Linux" or arch not in manifest["architectures"]:
        raise ValueError("supported native Linux target required")
    old_library = component / "hilink_bridge/lib" / ("amd64" if arch == "x86_64" else arch) / "libhilink_bridge.so"
    required = (old_library, component / "config/product_registry.json", component / "hilink_bridge/config/hilink.cfg",
                component / "hilink_bridge/config/hilink_bak.cfg")
    if not all(path.is_file() and os.access(path, os.R_OK) for path in required):
        raise ValueError("existing legacy library and configuration required")
    own = integration_entries(read_json(entries))
    if not own:
        raise ValueError("existing hwhomebridge entry required")
    required_bytes = 4 * sum(p.stat().st_size for p in component.rglob("*") if p.is_file()) + 2 * sum(map(len, payload.values())) + 64 * 1024 * 1024
    if shutil.disk_usage(config).free < required_bytes:
        raise ValueError("insufficient disk space for backup, staging and restore")
    # 使用目标实际镜像读版本、UID；此临时容器无网络，不加载桥接、不写 /config。
    raw = podman("run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--user", info["Config"].get("User") or "0",
                 "-v", str(config) + ":/config:ro", "-v", str(probe.resolve()) + ":/probe.py:ro",
                 "--entrypoint", "python3", info["Image"], "-B", "/probe.py", "--offline")
    runtime = runtime_probe_result(raw)
    if runtime["ha_version"] != manifest["tested_ha"] or runtime["architecture"] != arch or not runtime["owned_readable_config"]:
        raise ValueError("HA version, runtime architecture or runtime UID/permissions mismatch")
    return {"passed": True, "scope": "offline file installation only", "release": manifest["version"],
            "package_sha256": sha256, "architecture": arch, "runtime": runtime,
            "stopped_container_id": info["Id"], "free_bytes": shutil.disk_usage(config).free,
            "required_bytes": required_bytes, "integration_entries": len(own),
            "legacy_sdk_sha256": digest(old_library.read_bytes()),
            "activation_ready": False, "activation_pending": ["live interface and free ports", "native deployment/profile/catalog and current entity checks", "M4 exclusive identity handover"]}


@contextlib.contextmanager
def maintenance(config, container):
    import fcntl
    # 不在 HA .storage 中创建锁，避免改动运行目录。
    lock = config.parent / ("." + config.name + ".hwhomebridge-maintenance.lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stopped_container(config, container)
        yield


def backup(config, destination, container):
    config, component, entries = target_paths(config)
    destination = safe_absolute(destination)
    if destination.exists() or overlap(config, destination):
        raise ValueError("new backup directory outside HA config required")
    needed = sum(path.stat().st_size for path in component.rglob('*') if path.is_file()) + entries.stat().st_size + 1024 * 1024
    if not destination.parent.is_dir() or shutil.disk_usage(destination.parent).free < needed:
        raise ValueError("existing backup parent with sufficient disk space required")
    with maintenance(config, container):
        before = regular_tree(component)
        entry_bytes = entries.read_bytes()
        destination.mkdir(parents=True, mode=0o700)
        shutil.copytree(component, destination / "component", copy_function=shutil.copy2)
        (destination / "core.config_entries").write_bytes(entry_bytes)
        (destination / "core.config_entries").chmod(0o600)
        if regular_tree(destination / "component") != before or regular_tree(component) != before or entries.read_bytes() != entry_bytes:
            raise ValueError("target changed during backup")
        metadata = {"format": 1, "config": str(config), "component": before,
                    "entries_sha256": digest(entry_bytes), "runtime_KV_included": False}
        write_json(destination / "backup.json", metadata)
    verify_backup(destination, config)
    return {"passed": True, "backed_up_files": len(before), "runtime_KV_included": False}


def verify_backup(directory, config):
    safe_absolute(directory)
    regular_tree(directory)
    if directory.stat().st_mode & 0o077:
        raise ValueError("private backup directory required")
    meta = read_json(directory / "backup.json")
    if meta["format"] != 1 or meta["config"] != str(config) or regular_tree(directory / "component") != meta["component"]:
        raise ValueError("backup identity or component checksum mismatch")
    if digest((directory / "core.config_entries").read_bytes()) != meta["entries_sha256"]:
        raise ValueError("backup entry checksum mismatch")
    return meta


def replace_directory(component, staging):
    # 两次同文件系统 rename；间隙中断时保留 previous 和 staging，绝不删除唯一副本。
    previous = component.with_name(".hwhomebridge-previous")
    if previous.exists():
        raise ValueError("unfinished directory switch; recover previous first")
    component.rename(previous)
    try:
        staging.rename(component)
    except BaseException:
        previous.rename(component)
        raise
    shutil.rmtree(previous)


def install(config, package, sha256, directory, container, probe):
    preflight(config, package, sha256, container, probe)
    config, component, entries = target_paths(config)
    manifest, payload = load_package(package, sha256)
    with maintenance(config, container):
        meta = verify_backup(directory, config)
        if (directory / "installed.json").exists() or regular_tree(component) != meta["component"] or digest(entries.read_bytes()) != meta["entries_sha256"]:
            raise ValueError("fresh unchanged backup required before install")
        with tempfile.TemporaryDirectory(prefix=".hwhomebridge-stage-", dir=component.parent) as tmp:
            staging = Path(tmp) / "component"
            shutil.copytree(component, staging, copy_function=shutil.copy2)
            for cache in list(staging.rglob('__pycache__')):
                shutil.rmtree(cache)
            for name, data in payload.items():
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.write_bytes(data)
                path.chmod(0o700 if name.endswith(("/bridge_poc", "/identity_state_probe")) else 0o600)
            installed = regular_tree(staging)
            for name, detail in meta["component"].items():
                if name not in payload and '__pycache__' not in PurePosixPath(name).parts and installed.get(name) != detail:
                    raise ValueError("preserved file changed")
            # 先写恢复凭据；即使 switch 后进程中断，也能核对并恢复。
            write_json(directory / "installed.json", {"package_sha256": sha256, "component": installed})
            stopped_container(config, container)
            replace_directory(component, staging)
    return {"passed": True, "release": manifest["version"], "written_files": len(payload), "entries_modified": False, "runtime_modified": False, "ha_started": False}


def restore(config, directory, container, restore_entries=False):
    config, component, entries = target_paths(config)
    with maintenance(config, container):
        meta = verify_backup(directory, config)
        receipt = read_json(directory / "installed.json")
        if without_bytecode(regular_tree(component)) != without_bytecode(receipt["component"]):
            raise ValueError("component changed after install; preserve changes before restore")
        current = read_json(entries)
        saved = read_json(directory / "core.config_entries")
        old = integration_entries(saved)
        changed = integration_entries(current) != old
        if changed and not restore_entries:
            raise ValueError("integration entries changed; explicit --restore-entries required")
        other = [entry for entry in current["data"]["entries"] if entry["domain"] != "hwhomebridge"]
        if {e["entry_id"] for e in other} & {e["entry_id"] for e in old}:
            raise ValueError("entry ID conflict; manual recovery required")
        with tempfile.TemporaryDirectory(prefix=".hwhomebridge-restore-", dir=component.parent) as tmp:
            staging = Path(tmp) / "component"
            shutil.copytree(directory / "component", staging, copy_function=shutil.copy2)
            if regular_tree(staging) != meta["component"]:
                raise ValueError("restore staging checksum mismatch")
            stopped_container(config, container)
            replace_directory(component, staging)
        if changed:
            current["data"]["entries"] = other + old
            temporary = entries.with_name(".core.config_entries.hwhomebridge-restore")
            write_json(temporary, current)
            temporary.replace(entries)
        write_json(directory / "restored.json", {"passed": True, "entries_restored": changed, "runtime_KV_restored": False})
    return {"passed": True, "component_exactly_restored": regular_tree(component) == meta["component"],
            "entries_restored": changed, "unrelated_entries_preserved": True, "runtime_KV_restored": False, "ha_started": False}


def main():
    global CONTAINER_ENGINE
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="preflight", choices=("build", "preflight", "backup", "install", "restore"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--container")
    parser.add_argument("--engine", choices=("podman", "docker"), default="podman")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--restore-entries", action="store_true")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--native-x86_64", type=Path)
    parser.add_argument("--native-aarch64", type=Path)
    parser.add_argument("--probe", type=Path, default=Path(__file__).with_name("ha_runtime_preflight.py"))
    args = parser.parse_args()
    CONTAINER_ENGINE = args.engine
    try:
        if args.action == "build":
            if not args.output or not args.native_x86_64 or not args.native_aarch64:
                parser.error("build requires --output and both --native-* directories")
            result = build(args.repo, args.output, {"x86_64": args.native_x86_64, "aarch64": args.native_aarch64})
        else:
            if not args.config or not args.container:
                parser.error("target --config and --container required")
            if args.action in ("preflight", "install") and (not args.package or not args.sha256):
                parser.error("trusted --package and --sha256 required")
            if args.action in ("backup", "install", "restore") and not args.backup:
                parser.error("explicit --backup required")
            if args.action == "preflight":
                result = preflight(args.config, args.package, args.sha256, args.container, args.probe)
            elif args.action == "backup":
                result = backup(args.config, args.backup, args.container)
            elif args.action == "install":
                result = install(args.config, args.package, args.sha256, args.backup, args.container, args.probe)
            else:
                result = restore(args.config, args.backup, args.container, args.restore_entries)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        # 文件路径/JSON 异常可能携带配置和身份，CLI 只输出操作类型；私下定位。
        reason = str(error) if type(error) is ValueError else "validation failed; use documented checks before retry"
        print(json.dumps({"passed": False, "action": args.action, "error": reason}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
