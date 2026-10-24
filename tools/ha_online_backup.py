"""仅创建 HA 配置在线备份并下载校验；不安装、还原、停启 Core 或插件。

只适用于已核对在线备份实现的 Supervisor/Core 版本。在线 SDK KV 不保证一致，
此归档不能当作停机冷备份或配对身份交接文件。默认只预检；create 显式创建。
"""
import argparse
import asyncio
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tarfile
from time import monotonic
import urllib.parse
import urllib.request
import uuid

from ha_production_readonly import NoRedirect, SupervisorReader

SUPERVISOR = '2026.09.3'
CORE = '2026.10.1'
MAX_ARCHIVE = 2 * 1024**3
MAX_CONTENT = 8 * 1024**3


def file_sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def body(name):
    if not re.fullmatch(r'codex-m3-online-\d{8}T\d{6}-[a-f0-9]{8}', name):
        raise ValueError('generated backup name required')
    return {'name': name, 'homeassistant': True, 'addons': [], 'folders': [],
            'homeassistant_exclude_database': True, 'compressed': True,
            'background': True, 'location': [None]}


def allowed(endpoint, method, data, name):
    if method == 'get' and data is None:
        return endpoint in ('/supervisor/info', '/core/info', '/backups') or bool(
            re.fullmatch(r'/backups/[a-f0-9]{8}/info', endpoint))
    return method == 'post' and endpoint == '/backups/new/partial' and data == body(name)


class BackupClient(SupervisorReader):
    async def call(self, endpoint, method='get', data=None, name=''):
        if not allowed(endpoint, method, data, name):
            raise ValueError('operation outside online backup scope')
        self.counter += 1
        request = {'id': self.counter, 'type': 'supervisor/api', 'endpoint': endpoint, 'method': method}
        if data is not None:
            request['data'] = data
        await self.ws.send(json.dumps(request))
        response = json.loads(await asyncio.wait_for(self.ws.recv(), 45))
        if response.get('id') != self.counter or not response.get('success'):
            raise ValueError('online backup request failed; do not create a duplicate')
        return response['result']


def private_json(path, data):
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def find_backup(listing, name):
    matches = [item for item in listing.get('backups', []) if item.get('name') == name]
    if len(matches) > 1:
        raise ValueError('duplicate backup names require manual inspection')
    return matches[0]['slug'] if matches else None


def download(origin, token, slug, destination, expected_size):
    if not re.fullmatch(r'[a-f0-9]{8}', slug) or not 0 < expected_size <= MAX_ARCHIVE:
        raise ValueError('bounded completed backup required')
    if shutil.disk_usage(destination.parent).free < expected_size * 2:
        raise ValueError('insufficient local backup space')
    temporary = destination.with_name(destination.name + '.partial')
    request = urllib.request.Request(origin + f'/api/hassio/backups/{slug}/download',
                                     headers={'Authorization': 'Bearer ' + token}, method='GET')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    count = 0
    digest = hashlib.sha256()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream, opener.open(request, timeout=60) as response:
        os.fchmod(stream.fileno(), 0o600)
        while chunk := response.read(1024 * 1024):
            count += len(chunk)
            if count > expected_size:
                raise ValueError('backup download exceeds declared size')
            digest.update(chunk)
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())
    if count != expected_size:
        raise ValueError('truncated backup download')
    os.replace(temporary, destination)
    if file_sha(destination) != digest.hexdigest():
        raise ValueError('local backup reread differs')
    return digest.hexdigest()


def safe_member(member):
    path = PurePosixPath(member.name)
    if path.is_absolute() or '..' in path.parts or '\\' in member.name:
        raise ValueError('unsafe archive path')
    return str(path)


def verify_archive(path, expected_name):
    component = 'data/custom_components/hwhomebridge/'
    inventory, found = {}, set()
    total = 0
    with tarfile.open(path, 'r:') as outer:
        names = {}
        for member in outer:
            name = safe_member(member)
            if name in names:
                raise ValueError('duplicate outer archive member')
            names[name] = member
        metadata = names.get('backup.json')
        inner = names.get('homeassistant.tar.gz')
        if not metadata or not metadata.isfile() or metadata.size > 65536 or not inner or not inner.isfile():
            raise ValueError('HA backup archive missing required members')
        meta = json.load(outer.extractfile(metadata))
        if meta.get('name') != expected_name or meta.get('protected') or not meta.get('homeassistant'):
            raise ValueError('unexpected backup identity or encryption')
        if meta.get('addons', meta.get('apps', [])) or meta.get('folders'):
            raise ValueError('backup unexpectedly includes apps or folders')
        with gzip.GzipFile(fileobj=outer.extractfile(inner), mode='rb') as expanded, tarfile.open(fileobj=expanded, mode='r|') as content:
            for member in content:
                name = safe_member(member)
                total += member.size
                if total > MAX_CONTENT:
                    raise ValueError('oversized expanded HA backup')
                if name in found or len(found) >= 200000:
                    raise ValueError('duplicate HA archive member')
                found.add(name)
                if name.startswith(component):
                    if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                        raise ValueError('component archive contains links or special files')
                    if member.isfile():
                        digest = hashlib.file_digest(content.extractfile(member), 'sha256').hexdigest()
                        inventory[name[len(component):]] = {'sha256': digest, 'uid': member.uid,
                                                           'gid': member.gid, 'mode': member.mode}
                if name == 'data/.storage/core.config_entries' and not member.isfile():
                    raise ValueError('HA entries must be a regular archive member')
            # Read through the gzip trailer too; tar's end markers alone do not verify CRC.
            while expanded.read(1024 * 1024):
                if expanded.tell() > MAX_CONTENT:
                    raise ValueError('oversized expanded HA backup')
    if not inventory or 'data/.storage/core.config_entries' not in found:
        raise ValueError('component or complete HA entries missing')
    if not any(n.startswith('data/.storage/hwhomebridge/config/') for n in found):
        raise ValueError('legacy runtime absent from HA backup')
    return {'component_files': inventory, 'HA_entries_present': True, 'legacy_runtime_present': True,
            'archive_component_links': 0, 'expanded_bytes': total}


async def run(origin, token_file, output, create):
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('plain HA origin required')
    origin = origin.rstrip('/')
    output = output.absolute()
    if output.resolve() != output:
        raise ValueError('symlink-free local output required')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = output.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('private owned output directory required')
    for path in output.iterdir():
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
            raise ValueError('private regular output files required')
    token = token_file.read_text().strip()
    journal_path = output / 'journal.private.json'
    created_on_this_connection = False
    async with BackupClient(origin, token) as client:
        supervisor = await client.call('/supervisor/info')
        core = await client.call('/core/info')
        if supervisor.get('version') != SUPERVISOR or core.get('version') != CORE:
            raise ValueError('online backup implementation not reviewed for target versions')
        if not create:
            return {'preflight_passed': True, 'backup_created': False, 'Core_stop_or_restart_requested': False}
        if journal_path.exists():
            journal = json.loads(journal_path.read_text())
            if journal['origin'] != origin:
                raise ValueError('backup journal belongs to another HA origin')
            body(journal['name'])
        else:
            journal = {'origin': origin, 'name': 'codex-m3-online-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]}
            body(journal['name'])
            # Write ahead: an uncertain POST is never repeated. Resume searches its exact name.
            journal['request_sent'] = True
            private_json(journal_path, journal)
            response = await client.call('/backups/new/partial', 'post', body(journal['name']), journal['name'])
            created_on_this_connection = True
            journal.update({k: response[k] for k in ('slug', 'job_id') if k in response})
            private_json(journal_path, journal)
        deadline = monotonic() + 600
        while monotonic() < deadline:
            slug = find_backup(await client.call('/backups'), journal['name'])
            if slug:
                break  # Supervisor only adds a successfully completed archive to this list.
            await asyncio.sleep(5)
        else:
            raise TimeoutError('backup not confirmed; resume this directory without resubmitting')
        detail = await client.call(f'/backups/{slug}/info')
        if detail.get('homeassistant') != CORE or detail.get('addons') or detail.get('apps') or detail.get('folders') or detail.get('protected') or detail.get('homeassistant_exclude_database') is not True:
            raise ValueError('completed backup scope differs')
        journal['slug'] = slug
        private_json(journal_path, journal)
        archive = output / 'HA-config-online.private.tar'
        if not archive.exists():
            checksum = await asyncio.to_thread(download, origin, token, slug, archive, detail['size_bytes'])
            journal['archive_sha256'] = checksum
            private_json(journal_path, journal)
        else:
            checksum = file_sha(archive)
            if checksum != journal.get('archive_sha256') or archive.stat().st_size != detail['size_bytes']:
                raise ValueError('existing archive differs or lacks completed download receipt')
        inventory = await asyncio.to_thread(verify_archive, archive, journal['name'])
        private_json(output / 'archive-inventory.private.json', inventory)
        result = {'online_backup_created': True, 'local_archive_verified': True,
                  'archive_bytes': archive.stat().st_size, 'archive_sha256': checksum,
                  'contains_HA_config': True, 'complete_HA_entries_present': True,
                  'legacy_runtime_included_online': True, 'runtime_KV_consistency_verified': False,
                  'component_files': len(inventory['component_files']), 'database_excluded': True,
                  'addons_included': False, 'Core_stop_or_restart_requested': False,
                  'HA_websocket_survived_backup_job': created_on_this_connection,
                  'cold_backup': False, 'pairing_handover_snapshot': False,
                  'supervisor_version': SUPERVISOR, 'core_version': CORE}
        private_json(output / 'report.json', result)
        return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('preflight', 'create'), nargs='?', default='preflight')
    parser.add_argument('--url', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args.url, args.token_file, args.output, args.action == 'create'))
    except Exception:
        print(json.dumps({'complete': False, 'message': 'Backup not fully verified. Keep the private journal and resume the same directory; never blindly create another backup.'}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
