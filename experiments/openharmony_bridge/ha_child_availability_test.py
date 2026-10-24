"""执行真实 C 状态机，仅连接本进程的回环 HA 替身；云端接口由 C 替身记录。"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading


def run(binary, runner=()):
    source = {'old-light': True, 'other-light': True, 'new-fan': True}
    writes = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            key = request['key']
            if source[key] == 'invalid':
                result = {'ok': False, 'error': 'ValueError'}
            elif not source[key]:
                result = {'ok': False, 'available': False, 'error': 'SourceUnavailable'}
            else:
                if 'data' in request:
                    writes.append(request)
                result = {'ok': True, 'available': True, 'state': {'switch': {'on': 1}}}
            payload = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers(); self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = 'OHPOC-local-ha-smoke'
            catalog = []
            for key in source:
                sn = 'OHPOC-HA-' + hashlib.sha256(f'{parent}#ha-{key}-v1'.encode()).hexdigest()[:24]
                catalog.append({'key': key, 'pid': 'fixture', 'devType': 'fixture', 'manu': 'fixture',
                    'services': {'switch': 'switch'}, 'existing_cloud_only': True})
                path = root / f'ha-child-{key}.json'
                path.write_text(json.dumps({'version': 1, 'parent_sn': parent, 'sn': sn,
                    'udid': hashlib.sha256(f'OpenHarmonyHA{key}{sn}'.encode()).hexdigest(),
                    'devId': 'test-' + key, 'on': 0})); path.chmod(0o600)
            (root / 'ha-children.local.json').write_text(json.dumps(catalog))
            (root / 'ha-children.local.json').chmod(0o600)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            process = subprocess.Popen([*runner, str(binary.resolve()), directory],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env={**os.environ, 'POC_HA_PROXY_PORT': str(server.server_port)})
            def command(text):
                process.stdin.write(text + '\n'); process.stdin.flush()
                while True:
                    line = process.stdout.readline()
                    if not line:
                        raise AssertionError('native harness exited: ' + process.stderr.read()[:1000])
                    if line.startswith('TEST '):
                        return json.loads(line[5:])
            try:
                source['old-light'] = False
                first = command('step old-light')
                assert first['status'] == 'offline' and first['pending'] == 1
                assert command('ack old-light')['announced'] == 0
                assert command('read old-light')['result'] == -1
                assert command('write old-light')['result'] == -1 and not writes
                for key in ('other-light', 'new-fan'):
                    assert command('step ' + key)['status'] == 'online'
                    assert command('ack ' + key)['announced'] == 1
                    assert command('step ' + key)['cached'] == 1
                assert command('write new-fan')['result'] == 0 and len(writes) == 1
                # 恢复时数值仍是原来的 on=1，也必须重新上报。
                source['old-light'] = True
                assert command('step old-light')['status'] == 'online'
                assert command('ack old-light')['announced'] == 1
                before_reports = command('step old-light')['reports']
                source['old-light'] = False
                offline = command('step old-light')
                assert offline['status'] == 'offline' and offline['cached'] == 0
                # 状态同步超时后重试，不能阻止其他卡读取。
                assert command('timeout old-light')['announced'] == 1
                assert command('read new-fan')['result'] == 0
                assert command('step old-light')['status'] == 'offline'
                assert command('ack old-light')['announced'] == 0
                source['old-light'] = True
                assert command('step old-light')['status'] == 'online'
                command('ack old-light')
                assert command('step old-light')['reports'] == before_reports + 1
                # 三次失败有退避，但不会永久失去恢复能力。
                source['old-light'] = False
                for _ in range(3):
                    command('step old-light'); command('fail old-light')
                blocked = command('step old-light')
                assert blocked['pending'] == 0
                retry = command('retry old-light')
                assert retry['pending'] == 1 and retry['status_requests'] == blocked['status_requests'] + 1
                command('ack old-light')
                source['old-light'] = True
                command('step old-light'); command('ack old-light'); command('step old-light')
                # 写入参数校验失败不宣告离线；读取错误则清缓存、报告离线。
                source['old-light'] = 'invalid'
                invalid = command('write old-light')
                assert invalid['result'] == -1 and invalid['available'] == 1
                assert command('step old-light')['status'] == 'offline'
                command('ack old-light')
                source['old-light'] = True
                command('step old-light'); command('ack old-light'); command('step old-light')
                command('reconnect old-light')
                for key in source:
                    assert command('step ' + key)['status'] == 'online'
                    command('ack ' + key)
                    final = command('step ' + key)
                    assert final['cached'] == 1
                assert final['bind_requests'] == final['sync_requests'] == 0
                assert before == {p.name: p.read_bytes() for p in root.iterdir()}
                process.stdin.close()
                assert process.wait(timeout=5) == 0
            finally:
                if process.poll() is None:
                    process.kill(); process.wait(timeout=5)
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    result = {'passed': True, 'virtual_children': 3, 'startup_offline_isolated': True,
        'stale_cache_rejected': True, 'identical_recovery_reported': True,
        'status_timeout_retry': True, 'retry_after_three_failures': True,
        'cloud_reconnect_resync': True, 'identities_unchanged': True,
        'batch_bind_calls': 0, 'metadata_sync_calls': 0, 'real_cloud_or_app_verified': False}
    print(json.dumps(result))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('--runner', help='可选用户态架构模拟器，例如 qemu-x86_64')
    args = parser.parse_args()
    run(args.binary, [args.runner] if args.runner else [])
