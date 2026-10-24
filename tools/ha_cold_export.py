"""原单网关停机后的私有冷备导出与离线验真；从不停止、启动或写入原 HA。

须在独立维护通道的 Linux 宿主运行。只读 Docker/Podman inspect/ps 和本地文件。
在线检查、副本稳定或手填停止标志，均不能替代实际容器停止证据。
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import itertools
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile

from ha_entity_preflight import core_module, digest, registry_proof

COMPONENT = Path('custom_components/hwhomebridge')
RUNTIME = Path('.storage/hwhomebridge')
REGISTRIES = ('.storage/core.config_entries', '.storage/core.device_registry', '.storage/core.entity_registry')
MAX_FILES, MAX_FILE, MAX_TOTAL = 10000, 64 * 1024 * 1024, 256 * 1024 * 1024


def absolute(path):
    path = path.absolute()
    if path.resolve() != path or '..' in path.parts:
        raise ValueError('symlink-free absolute path required')
    return path


def overlap(first, second):
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


class ContainerOwner:
    def __init__(self, engine, name, config):
        if engine not in ('docker', 'podman') or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', name):
            raise ValueError('explicit local Docker/Podman container required')
        self.engine, self.name, self.config = engine, name, absolute(config)

    def run(self, *args):
        # 固定命令，不接受 shell、exec、start、stop 或远端 Docker context。
        if not args or args[0] not in ('inspect','ps') or args[0] == 'ps' and args != ('ps','-q'):
            raise ValueError('only local inspect/ps is permitted')
        environment = {**os.environ, 'DOCKER_HOST':'unix:///var/run/docker.sock', 'CONTAINER_HOST':''}
        environment.pop('DOCKER_CONTEXT', None)
        environment.pop('CONTAINER_CONNECTION', None)
        prefix = [self.engine, '--remote=false'] if self.engine == 'podman' else [self.engine]
        result = subprocess.run([*prefix, *args], env=environment, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise ValueError('local container inspection failed')
        return result.stdout

    def stopped(self):
        info = json.loads(self.run('inspect', self.name))[0]
        if not any(m.get('Destination') == '/config' and absolute(Path(m['Source'])) == self.config
                   for m in info.get('Mounts', []) if m.get('Source')):
            raise ValueError('container must mount this exact HA config at /config')
        state = info['State']
        if any(state.get(k) for k in ('Running', 'Paused', 'Restarting', 'Pid')):
            raise ValueError('HA owner must already be stopped')
        host = info['HostConfig']
        if host.get('RestartPolicy', {}).get('Name', '') not in ('', 'no', 'none'):
            raise ValueError('automatic container restart must be disabled')
        if host.get('NetworkMode') != 'host':
            raise ValueError('source MAC proof requires the actual host network namespace')
        active = self.run('ps', '-q').split()
        if active:
            for other in json.loads(self.run('inspect', *active)):
                for mount in other.get('Mounts', []):
                    if mount.get('Source') and mount.get('RW', True) and overlap(absolute(Path(mount['Source'])), self.config):
                        raise ValueError('active writable container shares source configuration')
        # StartedAt/FinishedAt/ID 固定，防止导出中间被监控器重新拉起后又停止。
        return {'engine':self.engine, 'container_id':info['Id'], 'started_at':state.get('StartedAt'),
                'finished_at':state.get('FinishedAt'), 'network':'host', 'restart_disabled':True}

    def mac(self, interface):
        if not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', interface):
            raise ValueError('explicit source interface required')
        value = (Path('/sys/class/net') / interface / 'address').read_text().strip().lower()
        if not re.fullmatch(r'(?:[a-f0-9]{2}:){5}[a-f0-9]{2}', value) or value == '00:00:00:00:00:00':
            raise ValueError('source interface MAC unavailable')
        return value


def fingerprint(info):
    return {key:getattr(info, 'st_' + key) for key in ('dev','ino','uid','gid','mode','nlink','size','mtime_ns','ctime_ns')}


def source_files(config):
    names = set()
    for name in (COMPONENT, RUNTIME, *(Path(n) for n in REGISTRIES)):
        root = config / name
        if not root.exists() or absolute(root) != root:
            raise ValueError('complete original component, runtime and registries required')
        paths = itertools.chain((root,), root.rglob('*')) if root.is_dir() else (root,)
        for path in paths:
            absolute(path)
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError('only ordinary source files and directories allowed')
            if path.is_file():
                if info.st_nlink != 1 or info.st_size > MAX_FILE:
                    raise ValueError('hard-linked or oversized source file refused')
                names.add(str(path.relative_to(config)))
                if len(names) > MAX_FILES:
                    raise ValueError('source inventory limit exceeded')
    return sorted(names)


def source_directories(config):
    roots = {config, config/'.storage', config/'custom_components'}
    for root in (config/COMPONENT, config/RUNTIME):
        for path in itertools.chain((root,),root.rglob('*')):
            if path.is_dir():
                roots.add(path)
                if len(roots) > MAX_FILES:
                    raise ValueError('source directory limit exceeded')
    return {str(p.relative_to(config)):fingerprint(p.stat()) for p in sorted(roots)}


def no_local_readers(config):
    """检查可见的同 UID 进程，拒绝仍持有源文件或源工作目录的本机进程。"""
    if (Path('/.dockerenv').exists() or Path('/run/.containerenv').exists()
            or not Path('/proc/1/comm').is_file() or Path('/proc/1/comm').read_text().strip() != 'systemd'):
        raise ValueError('run through a verified systemd Linux host maintenance channel, not an addon container')
    for process in Path('/proc').iterdir():
        try:
            if not process.name.isdigit() or process.stat().st_uid != os.getuid() or int(process.name) == os.getpid():
                continue
            links = [process/'cwd', *list((process/'fd').iterdir())]
            for link in links:
                try:
                    value = Path(os.readlink(link).removesuffix(' (deleted)'))
                    if value.is_absolute() and value.is_relative_to(config):
                        raise ValueError('local process still uses source configuration')
                except FileNotFoundError:
                    continue
        except FileNotFoundError:
            continue
        except PermissionError as error:
            raise ValueError('host process ownership cannot be verified; use the authorized host admin context') from error


def read_file(path):
    absolute(path)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_FILE:
            raise ValueError('ordinary bounded source file required')
        data = stream.read(MAX_FILE + 1)
        after = os.fstat(stream.fileno())
    if len(data) != before.st_size or fingerprint(before) != fingerprint(after) or fingerprint(path.lstat()) != fingerprint(after):
        raise ValueError('source changed during read')
    return data, {**fingerprint(after), 'sha256':hashlib.sha256(data).hexdigest()}


def read_json(path):
    return json.loads(read_file(path)[0])


def original_entry(config):
    entries = read_json(config / REGISTRIES[0])['data']['entries']
    own = [entry for entry in entries if entry['domain'] == 'hwhomebridge']
    if len(own) != 1:
        raise ValueError('exactly one original configuration entry required')
    entry = own[0]
    merged = {**entry.get('data', {}), **entry.get('options', {})}
    if merged.get('gateway_directory') or merged.get('openharmony_directory') or merged.get('backend') not in (None,'legacy','disabled'):
        raise ValueError('this exporter handles the original unmanaged legacy gateway only')
    return entry


def validate_inventory(config, report):
    if (report.get('version') != 1 or report.get('kind') != 'hwhomebridge-entity-preflight'
            or not report.get('inventory_complete') or not report.get('two_pass_registry_stable')):
        raise ValueError('complete stable two-pass source inventory required')
    entry = original_entry(config)
    if entry['entry_id'] != report['entry']['entry_id']:
        raise ValueError('original entry changed after entity preflight')
    if report['entry'].get('unique_id') is not None and entry.get('unique_id') != report['entry']['unique_id']:
        raise ValueError('original unique ID changed')
    devices = read_json(config / REGISTRIES[1])['data']['devices']
    entities = read_json(config / REGISTRIES[2])['data']['entities']
    proof = registry_proof(devices, entities, set(report['source_ids']), set(report['bridge_ids']))
    if digest(proof) != report['registry_sha256'] or proof != report['registry_proof']:
        raise ValueError('source registry changed after live preflight')
    files = {str(path.relative_to(config / COMPONENT)):hashlib.sha256(read_file(path)[0]).hexdigest()
             for path in sorted((config / COMPONENT / 'config').rglob('*.json'))}
    if files != report['config_sha256']:
        raise ValueError('source adapter configuration changed')
    return entry


def identity_proof(config, mac, report):
    codec = core_module('identity_store')
    library = config / COMPONENT / 'hilink_bridge/lib/amd64/libhilink_bridge.so'
    if hashlib.sha256(read_file(library)[0]).hexdigest() != codec.SDK_SHA256:
        raise ValueError('legacy SDK version has not passed identity format validation')
    base = config / RUNTIME / 'config'
    ac, sn = read_file(base / 'device_ac')[0], read_file(base / 'device_sn')[0]
    if len(sn) < 2 or sn[-1:] not in (b'\0', b'\n') or not re.fullmatch(rb'[A-Za-z0-9_-]{1,39}', sn[:-1]):
        raise ValueError('invalid original native SN')
    identity = codec.decode(read_file(base / 'config.cfg')[0], read_file(base / 'bridge.cfg')[0], ac, mac)
    excluded = base / 'excluded_devices.txt'
    excluded_present = bool(read_file(excluded)[0].strip()) if excluded.exists() else False
    rows = report['legacy_inventory']
    matched = len(rows) == len(identity['children']) and {r['sn'] for r in rows} == {r['sn'] for r in identity['children']}
    proof = {'root_id_sha256':codec.digest(identity['dev_id'].encode()),
        'native_sn_sha256':codec.digest(sn), 'AC_sha256':codec.digest(ac),
        'account_field_sha256':{name:codec.digest(identity[name] if isinstance(identity[name],bytes) else identity[name].encode())
            for name in ('secret','psk','uid_hash','auth_id','auth_code')},
        'children':[{'sn_sha256':codec.digest(row['sn'].encode()), 'id_sha256':codec.digest(row['dev_id'].encode())}
                    for row in identity['children']], 'legacy_sdk_sha256':codec.SDK_SHA256,
        'child_inventory_matches_live':matched, 'excluded_devices_present':excluded_present}
    if sn.endswith(b'\n'):
        # 原始字节及摘要照存；不把LF文件当成已确认的原生C字符串。
        proof['native_sn_storage_format'] = 'ascii-line-LF; native canonicalization pending'
    return proof


@contextmanager
def export_lock(parent):
    # 只在输出目录的父目录建锁，不向源 HA 写入任何文件。
    fd = os.open(parent / '.hwhomebridge-cold-export.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def export(config, output, owner, interface, preflight):
    config, output = absolute(config), absolute(output)
    if output.exists() or not output.parent.is_dir() or overlap(config, output):
        raise ValueError('new private output outside HA configuration required')
    with export_lock(output.parent):
        stopped = owner.stopped()
        no_local_readers(config)
        mac = owner.mac(interface)
        report = read_json(preflight)
        entry = validate_inventory(config, report)
        identity = identity_proof(config, mac, report)
        names = source_files(config)
        directories = source_directories(config)
        total = sum((config / name).stat().st_size for name in names)
        if total > MAX_TOTAL or shutil.disk_usage(output.parent).free < total + 1024 * 1024:
            raise ValueError('export exceeds bounds or available space')
        with tempfile.TemporaryDirectory(prefix='.hwhomebridge-export-', dir=output.parent) as temporary:
            stage = Path(temporary) / 'export'
            stage.mkdir(mode=0o700)
            for name in directories:
                (stage/'payload'/name).mkdir(parents=True,exist_ok=True,mode=0o700)
            inventory = {}
            copied = 0
            for name in names:
                data, inventory[name] = read_file(config / name)
                copied += len(data)
                if copied > MAX_TOTAL:
                    raise ValueError('source grew beyond export limits')
                target = stage / 'payload' / name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open('xb') as stream:
                    stream.write(data);stream.flush();os.fsync(stream.fileno())
                target.chmod(0o600)
            # 复查目录、字节、原元数据及容器生命周期，中间变化一律不发布。
            if (names != source_files(config) or directories != source_directories(config)
                    or any(read_file(config/name)[1] != info for name,info in inventory.items())):
                raise ValueError('source changed during cold export')
            no_local_readers(config)
            if owner.stopped() != stopped or owner.mac(interface) != mac:
                raise ValueError('owner lifecycle or source interface changed during export')
            manifest = {'version':1, 'kind':'hwhomebridge-stopped-owner-cold-export',
                'created_utc':datetime.now(timezone.utc).isoformat(), 'source_config':str(config),
                'owner':stopped, 'source_interface':interface, 'source_mac':mac,
                'entry':{'entry_id':entry['entry_id'], 'unique_id':entry.get('unique_id')},
                'files':inventory, 'identity':identity, 'entity_preflight':report,
                'runtime_KV_included':True, 'all_HA_entries_included':True,
                'entity_preflight_passed':report['structure_passed'], 'adoption_ready':False,
                'original_directories':directories, 'same_UID_local_readers_absent':True,
                'host_systemd_namespace_verified':True, 'export_UID':os.getuid(),
                'deployment_performed':False, 'cross_host_exclusion_verified':False}
            with (stage/'cold-export.json').open('x') as stream:
                json.dump(manifest,stream,ensure_ascii=False,indent=2);stream.flush();os.fsync(stream.fileno())
            (stage/'cold-export.json').chmod(0o600)
            verify(stage)
            stage.rename(output)
            fd = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
    return {'passed':True, 'runtime_KV_included':True, 'all_HA_entries_included':True,
            'paired_children':len(identity['children']), 'source_files_unchanged':True,
            'child_inventory_matches_live':identity['child_inventory_matches_live'],
            'adoption_ready':False,
            'deployment_performed':False, 'source_files_written':0}


def verify(directory):
    directory = absolute(directory)
    for path in (directory, *directory.rglob('*')):
        absolute(path)
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077 or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('private owned export required')
    manifest = read_json(directory/'cold-export.json')
    if manifest['version'] != 1 or manifest['kind'] != 'hwhomebridge-stopped-owner-cold-export':
        raise ValueError('invalid cold export manifest')
    payload = directory/'payload'
    actual = {str(p.relative_to(payload)) for p in payload.rglob('*') if p.is_file()}
    if actual != set(manifest['files']):
        raise ValueError('cold export file inventory mismatch')
    actual_dirs = {str(p.relative_to(payload)) for p in (payload,*payload.rglob('*')) if p.is_dir()}
    if actual_dirs != set(manifest['original_directories']):
        raise ValueError('cold export directory inventory mismatch')
    for name, source in manifest['files'].items():
        path = payload/name
        if '..' in Path(name).parts or Path(name).is_absolute() or hashlib.sha256(read_file(path)[0]).hexdigest() != source['sha256']:
            raise ValueError('cold export checksum mismatch')
    entry = validate_inventory(payload, manifest['entity_preflight'])
    if {'entry_id':entry['entry_id'], 'unique_id':entry.get('unique_id')} != manifest['entry']:
        raise ValueError('cold export entry mismatch')
    if identity_proof(payload,manifest['source_mac'],manifest['entity_preflight']) != manifest['identity']:
        raise ValueError('cold export native identity mismatch')
    return {'passed':True, 'paired_children':len(manifest['identity']['children']),
            'runtime_KV_included':True, 'deployment_performed':False, 'adoption_ready':False,
            'child_inventory_matches_live':manifest['identity']['child_inventory_matches_live']}


def restore(directory, target):
    """仅还原到全新私有包装目录下的config；绝不覆盖原HA或启动后端。"""
    directory, target = absolute(directory), absolute(target)
    verify(directory)
    manifest = read_json(directory/'cold-export.json')
    original = absolute(Path(manifest['source_config']))
    if (target.exists() or not target.parent.is_dir() or overlap(target, directory)
            or overlap(target, original)):
        raise ValueError('new restore wrapper outside original config and export required')
    if os.geteuid() != 0:
        raise ValueError('raw restore requires the independent host admin context')
    for records, file_type in ((manifest['files'], stat.S_ISREG), (manifest['original_directories'], stat.S_ISDIR)):
        for name, info in records.items():
            if (Path(name).is_absolute() or '..' in Path(name).parts or not file_type(info['mode'])
                    or info['mode'] & 0o7000 or any(type(info[k]) is not int or not 0 <= info[k] < 2**31 for k in ('uid','gid'))):
                raise ValueError('ordinary source metadata without special permission bits required')

    def metadata(path, info):
        current = path.stat()
        if (current.st_uid, current.st_gid) != (info['uid'], info['gid']):
            os.chown(path, info['uid'], info['gid'])
        path.chmod(stat.S_IMODE(info['mode']))
        os.utime(path, ns=(info['mtime_ns'], info['mtime_ns']))

    with export_lock(target.parent), tempfile.TemporaryDirectory(prefix='.hwhomebridge-restore-', dir=target.parent) as temporary:
        stage = Path(temporary)/'restored';stage.mkdir(mode=0o700)
        config = stage/'config'
        for name in manifest['original_directories']:
            (config/name).mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, info in manifest['files'].items():
            data = read_file(directory/'payload'/name)[0]
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise ValueError('export changed during raw restore')
            path = config/name
            with path.open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            metadata(path, info)
            actual = path.stat()
            if (actual.st_uid, actual.st_gid, actual.st_mode, actual.st_mtime_ns) != (info['uid'], info['gid'], info['mode'], info['mtime_ns']):
                raise ValueError('restored file metadata mismatch')
        for name in sorted(manifest['original_directories'], key=lambda name:len(Path(name).parts), reverse=True):
            metadata(config/name, manifest['original_directories'][name])
        verify(directory)
        result = {'passed':True, 'raw_restore_only':True, 'restored_files':len(manifest['files']),
                  'original_UID_GID_modes_and_mtime_preserved':True, 'source_HA_written':False,
                  'SDK_started':False, 'deployment_performed':False, 'adoption_ready':False}
        with (stage/'restore.json').open('x') as stream:
            json.dump(result, stream);stream.flush();os.fsync(stream.fileno())
        (stage/'restore.json').chmod(0o600)
        stage.rename(target)
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    return result


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export','verify','restore'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--engine',choices=('docker','podman'))
    parser.add_argument('--container')
    parser.add_argument('--interface')
    parser.add_argument('--preflight',type=Path)
    parser.add_argument('--target',type=Path, help='restore only: new private wrapper containing config/')
    args = parser.parse_args()
    try:
        if args.action == 'export':
            if not all((args.config,args.engine,args.container,args.interface,args.preflight)):
                parser.error('export requires config, local engine/container, interface and preflight')
            result = export(args.config,args.output,ContainerOwner(args.engine,args.container,args.config),args.interface,args.preflight)
        elif args.action == 'restore':
            if args.target is None:
                parser.error('restore requires a new private target wrapper')
            result = restore(args.output,args.target)
        else:result = verify(args.output)
        print(json.dumps(result))
    except Exception:
        print(json.dumps({'passed':False,'error':'cold export validation failed; source HA was not changed'}))
        raise SystemExit(1) from None
