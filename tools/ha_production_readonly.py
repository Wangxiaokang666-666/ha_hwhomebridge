"""通过现有 File editor 做原 HA 只读预检；仅在本机保存在线副本。

不提供安装、重启、服务调用、命令执行或远端文件写入接口。
Ingress 登录会创建短期认证会话；它不改变插件设置或 HA 配置。
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from ha_upgrade import load_package, preserved

EDITOR_ROOT = PurePosixPath('/homeassistant')
COMPONENT = EDITOR_ROOT / 'custom_components/hwhomebridge'
ENTRIES = EDITOR_ROOT / '.storage/core.config_entries'
RUNTIME = EDITOR_ROOT / '.storage/hwhomebridge'
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
READ_ENDPOINTS = {'/supervisor/info', '/host/info', '/os/info', '/core/info', '/core/stats',
                  '/network/info', '/addons', '/backups', '/backups/info', '/mounts',
                  '/addons/core_configurator/info'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def supervisor_request_allowed(endpoint, method):
    if method == 'post':
        return endpoint == '/ingress/session'
    return method == 'get' and (endpoint in READ_ENDPOINTS or
        bool(re.fullmatch(r'/backups/[a-f0-9]{8}/info', endpoint)))


def file_request_allowed(route, path):
    if route not in ('listdir', 'download') or not isinstance(path, str):
        return False
    candidate = PurePosixPath(path)
    if str(candidate) != path or '..' in candidate.parts or any(c in path for c in ('%', '\\', '\n', '\r', '\0')):
        return False
    if route == 'download' and candidate == ENTRIES:
        return True
    if route == 'listdir' and candidate == RUNTIME:
        return True
    if not candidate.is_relative_to(COMPONENT):
        return False
    return route == 'listdir' or snapshot_candidate(str(candidate.relative_to(COMPONENT)))


def snapshot_candidate(name):
    """组件静态文件；未知文件和任何运行状态均只列清单，不复制。"""
    path = PurePosixPath(name)
    if str(path) != name or path.is_absolute() or '..' in path.parts:
        return False
    if any(p.startswith('.') or p == '__pycache__' for p in path.parts) or '.local.' in name:
        return False
    if name.startswith('hilink_bridge/config/'):
        return name in ('hilink_bridge/config/hilink.cfg', 'hilink_bridge/config/hilink_bak.cfg')
    if len(path.parts) == 1:
        return path.suffix == '.py' or name in ('manifest.json', 'strings.json')
    allowed = {'config': '.json', 'translations': '.json', 'brand': '.png', 'profile': '.json'}
    if path.parts[0] in allowed:
        return path.suffix == allowed[path.parts[0]]
    if name.startswith('hilink_bridge/lib/'):
        return path.suffix == '.so'
    return (name.startswith('openharmony/config/') and path.suffix == '.json' or
            path.parts[0] == 'openharmony' and len(path.parts) == 2 and path.suffix == '.py')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('redirect refused to protect credentials')


class SupervisorReader:
    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('HA origin URL required')
        self.url = url.rstrip('/')
        self.token = token
        self.counter = 0
        self.read_requests = 0
        self.auth_sessions = 0

    async def __aenter__(self):
        import websockets
        self.ws = await websockets.connect(self.url.replace('http:', 'ws:', 1).replace('https:', 'wss:', 1) + '/api/websocket',
                                           max_size=32 * 1024 * 1024, open_timeout=15)
        await self.ws.recv()
        await self.ws.send(json.dumps({'type': 'auth', 'access_token': self.token}))
        if json.loads(await self.ws.recv()).get('type') != 'auth_ok':
            await self.ws.close()
            raise ValueError('HA authentication failed')
        return self

    async def __aexit__(self, *_args):
        await self.ws.close()

    async def request(self, endpoint, method='get'):
        if not supervisor_request_allowed(endpoint, method):
            raise ValueError('non-read-only Supervisor operation refused')
        self.counter += 1
        await self.ws.send(json.dumps({'id': self.counter, 'type': 'supervisor/api',
                                      'endpoint': endpoint, 'method': method}))
        response = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
        if response.get('id') != self.counter or not response.get('success'):
            raise ValueError('Supervisor read or ingress authentication failed')
        if method == 'get':
            self.read_requests += 1
        else:
            self.auth_sessions += 1
        return response['result']


class FileReader:
    def __init__(self, origin, ingress_url, session):
        parsed = urllib.parse.urlsplit(ingress_url)
        if parsed.scheme or parsed.netloc or not re.fullmatch(r'/api/hassio_ingress/[A-Za-z0-9_-]+/?', parsed.path) or parsed.query or parsed.fragment:
            raise ValueError('same-origin File editor ingress required')
        self.base = origin + ingress_url.rstrip('/')
        self.session = session
        self.opener = urllib.request.build_opener(NoRedirect)
        self.read_requests = 0

    def get(self, route, path):
        if not file_request_allowed(route, path):
            raise ValueError('file route outside read-only scope')
        key = 'path' if route == 'listdir' else 'filename'
        url = self.base + '/api/' + route + '?' + urllib.parse.urlencode({key: path})
        request = urllib.request.Request(url, headers={'Cookie': 'ingress_session=' + self.session}, method='GET')
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    raise ValueError('file response too large')
                if route == 'download' and not response.headers.get('Content-Disposition'):
                    raise ValueError('file download unavailable')
        except (urllib.error.URLError, TimeoutError):
            raise ValueError('read-only file request failed') from None
        self.read_requests += 1
        return json.loads(data) if route == 'listdir' else data

    def inventory(self):
        queue = [str(COMPONENT)]
        rows = []
        seen = set()
        while queue:
            directory = queue.pop(0)
            if directory in seen or len(seen) >= 64:
                raise ValueError('directory inventory limit or cycle')
            seen.add(directory)
            listing = self.get('listdir', directory)
            if listing.get('error') or listing.get('abspath') != directory:
                raise ValueError('component directory listing unavailable')
            for row in listing.get('content', []):
                name = row['name']
                if name in ('.', '..') or any(c in name for c in ('/', '\\', '%', '\n', '\r', '\0')):
                    raise ValueError('unsafe directory member')
                expected = directory + '/' + name
                if row['fullpath'] != expected or row['type'] not in ('file', 'dir'):
                    raise ValueError('unexpected directory member')
                relative = str(PurePosixPath(expected).relative_to(COMPONENT))
                rows.append({'name': relative, 'type': row['type'], 'size': row['size'],
                             'modified': row['modified'], 'created': row['created']})
                if row['type'] == 'dir' and name not in ('.git', '__pycache__'):
                    queue.append(expected)
                if len(rows) > 512:
                    raise ValueError('component inventory too large')
        return rows


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(data)
    path.chmod(0o600)


def owned_entries(raw):
    source = json.loads(raw)
    return {'scope': 'hwhomebridge entries only; never replace core.config_entries with this file',
            'storage_metadata': {k: source.get(k) for k in ('version', 'minor_version', 'key')},
            'entries': [row for row in source['data']['entries'] if row['domain'] == 'hwhomebridge']}


def inspect_files(reader, output, snapshot):
    inventory = reader.inventory()
    files = {}
    for row in inventory:
        if row['type'] != 'file' or not snapshot_candidate(row['name']):
            continue
        if not 0 <= row['size'] <= MAX_FILE or sum(map(len, files.values())) + row['size'] > MAX_TOTAL:
            raise ValueError('component file size outside inspection limits')
        files[row['name']] = reader.get('download', str(COMPONENT / row['name']))
        if sum(map(len, files.values())) > MAX_TOTAL:
            raise ValueError('component snapshot limit exceeded')
    raw_entries = reader.get('download', str(ENTRIES))
    own = owned_entries(raw_entries)
    if not own['entries']:
        raise ValueError('no existing hwhomebridge entry')
    # 第二遍验证实际字节；只保存桥接条目，其他集成的原文不落盘。
    for name, data in files.items():
        if sha(reader.get('download', str(COMPONENT / name))) != sha(data):
            raise ValueError('component changed during online inspection')
    latest_entries = reader.get('download', str(ENTRIES))
    if owned_entries(latest_entries) != own or reader.inventory() != inventory:
        raise ValueError('component or owned entries changed during inspection')
    manifest = {'kind': 'online inspection copy; NOT cold backup or pairing snapshot',
                'inventory_visibility': 'File editor visible entries; ignored paths may be absent',
                'original_UID_modes_symlinks_verified': False, 'SDK_runtime_KV_included': False,
                'core_entries_complete': False, 'source_core_entries_sha256': sha(raw_entries),
                'full_core_entries_file_stable': sha(raw_entries) == sha(latest_entries),
                'files': {name: sha(data) for name, data in files.items()}, 'inventory': inventory}
    if snapshot:
        for name, data in files.items():
            private_write(output / 'component' / name, data)
        private_write(output / 'hwhomebridge.entries.private.json', json.dumps(own, ensure_ascii=False, indent=2).encode())
        private_write(output / 'online-copy.private.json', json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        if any(sha((output / 'component' / n).read_bytes()) != sha(d) for n, d in files.items()):
            raise ValueError('local inspection copy verification failed')
    return files, manifest, own


def overlay_preflight(files, package, expected):
    release, payload = load_package(package, expected)
    with tempfile.TemporaryDirectory(prefix='ha-production-overlay-') as temporary:
        root = Path(temporary)
        for name, data in {**files, **payload}.items():
            private_write(root / name, data)
        protected = {name: sha(data) for name, data in files.items() if preserved(name)}
        if any(sha((root / name).read_bytes()) != h for name, h in protected.items()):
            raise ValueError('local overlay changed preserved files')
        check = subprocess.run([sys.executable, '-B', str(root / 'validate_config.py')], capture_output=True, timeout=30)
        return {'package_version': release['version'], 'package_sha256': expected,
                'tested_ha': release['tested_ha'], 'architectures': release['architectures'],
                'local_overlay_config_valid': check.returncode == 0,
                'preserved_files_in_local_overlay': len(protected), 'remote_files_written': 0}


async def collect(url, token_file, output, snapshot=False, package=None, expected=None):
    output = output.absolute()
    if output.resolve() != output or output.exists():
        raise ValueError('new symlink-free private output directory required')
    token = token_file.read_text().strip()
    output.mkdir(parents=True, mode=0o700)
    async with SupervisorReader(url, token) as supervisor:
        facts = {endpoint: await supervisor.request(endpoint) for endpoint in
                 ('/supervisor/info', '/host/info', '/os/info', '/core/info', '/network/info', '/addons', '/backups')}
        addon = await supervisor.request('/addons/core_configurator/info')
        if addon.get('state') != 'started' or not addon.get('ingress'):
            raise ValueError('existing running File editor required; will not start one')
        session = await supervisor.request('/ingress/session', 'post')
        reader = FileReader(supervisor.url, addon['ingress_url'], session['session'])
        files, copy, own = await asyncio.to_thread(inspect_files, reader, output, snapshot)
        backup_details = []
        for backup in facts['/backups'].get('backups', []):
            detail = await supervisor.request('/backups/' + backup['slug'] + '/info')
            backup_details.append({'date': detail.get('date'), 'type': detail.get('type'),
                                   'contains_HA': bool(detail.get('homeassistant')), 'protected': detail.get('protected')})
        core, host, network = (facts[k] for k in ('/core/info', '/host/info', '/network/info'))
        summary = {'date_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'production read-only preflight',
            'host': {k: host.get(k) for k in ('operating_system', 'kernel', 'disk_total', 'disk_used', 'disk_free')},
            'core': {k: core.get(k) for k in ('version', 'arch', 'machine', 'port')},
            'network': [{'interface': item['interface'], 'enabled': item.get('enabled'), 'primary': item.get('primary')}
                        for item in network.get('interfaces', [])],
            'file_channel': {'addon': 'core_configurator', 'version': addon['version'], 'running': True,
                             'read_verified': True, 'write_tested': False, 'editor_path': str(COMPONENT),
                             'HA_path': '/config/custom_components/hwhomebridge', 'OS_shell_verified': False},
            'deployed_component': {'version': json.loads(files['manifest.json'])['version'],
                                   'files_read': len(files), 'bytes_read_per_pass': sum(map(len, files.values())),
                                   'inventory_files': sum(row['type'] == 'file' for row in copy['inventory']),
                                   'inventory_files_not_copied': sum(row['type'] == 'file' for row in copy['inventory']) - len(files),
                                   'two_pass_stable': True, 'hwhomebridge_entry_count': len(own['entries'])},
            'online_copy': {'saved_locally': snapshot, 'local_files_read_back_verified': snapshot,
                            'cold_backup': False, 'original_UID_modes_symlinks_verified': False,
                            'FileEditor_visible_files_only': True,
                            'SDK_runtime_KV_included': False, 'all_HA_entries_saved': False},
            'addons': [{'slug': item['slug'], 'name': item.get('name'), 'state': item.get('state')}
                       for item in facts['/addons'].get('addons', [])
                       if item['slug'] in ('core_configurator', 'core_samba') or item['slug'].endswith('_ssh')],
            'existing_backups': backup_details,
            'audit': {'Supervisor_GET_requests': supervisor.read_requests,
                      'ingress_auth_sessions_created': supervisor.auth_sessions, 'FileEditor_GET_requests': reader.read_requests,
                      'remote_file_write_requests': 0, 'remote_exec_requests': 0, 'HA_service_calls': 0,
                      'install_requests': 0, 'restart_requests': 0, 'addon_start_or_config_change_requests': 0},
            'production_install_allowed': False,
            'pending': ['verified OS command channel and HA OS Docker adapter',
                        'actual HA runtime UID, original file modes and symlink checks',
                        'free ports in actual HA network namespace',
                        'maintenance window and stopped-owner cold backup', 'M4 exclusive identity handover']}
        library = files.get('hilink_bridge/lib/amd64/libhilink_bridge.so')
        if library:
            summary['deployed_component']['amd64_SDK_sha256'] = sha(library)
        if package:
            if not expected:
                raise ValueError('trusted package SHA256 required')
            overlay = await asyncio.to_thread(overlay_preflight, files, package, expected)
            overlay['HA_version_matches'] = core['version'] == overlay['tested_ha']
            architecture = {'amd64': 'x86_64', 'aarch64': 'aarch64'}.get(core['arch'], core['arch'])
            overlay['architecture_matches'] = architecture in overlay['architectures']
            summary['package_preflight'] = overlay
        private_write(output / 'report.json', json.dumps(summary, ensure_ascii=False, indent=2).encode())
        return summary


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--snapshot', action='store_true', help='本机保存在线组件和桥接条目副本；不含运行 KV')
    parser.add_argument('--package', type=Path)
    parser.add_argument('--sha256')
    args = parser.parse_args()
    try:
        result = asyncio.run(collect(args.url, args.token_file, args.output, args.snapshot, args.package, args.sha256))
    except Exception:
        # 网络错误可能包含 Ingress 会话路径，禁止在公开 CLI 输出异常原文。
        print(json.dumps({'read_only_preflight_complete': False, 'error': 'read-only inspection failed; no install or restart requested'}))
        raise SystemExit(1) from None
    print(json.dumps({'read_only_preflight_complete': True, 'component_files': result['deployed_component']['files_read'],
                      'online_copy_saved': result['online_copy']['saved_locally'], 'production_install_allowed': False}))


if __name__ == '__main__':
    main()
