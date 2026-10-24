"""真实 C 回环协议故障注入；只使用临时虚拟身份，不启动 SDK 或连接云端。"""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time


def run(binary, runner=(), baseline=None):
    mode, state, writes = 'accepted', 0, []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            nonlocal state
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            request_id = self.headers.get('X-HWHB-Request-Id')
            assert request_id is None or request_id.isdecimal()
            if 'data' in request:
                writes.append(request)
                state = request['data']['on']
            if mode == 'receive_timeout':
                time.sleep(13)
            if mode == 'invalid_http':
                self.send_response(503)
                payload = b'{}'
            else:
                self.send_response(200)
                if mode == 'applied_but_unconfirmed':
                    # HA 已执行不等于端到端成功；此处只复现一种可能原因。
                    result = {'ok': False, 'error': 'TimeoutError'}
                elif mode == 'unsafe_error':
                    result = {'ok': False, 'error': 'private-payload-sentinel'}
                elif mode == 'unavailable':
                    result = {'ok': False, 'available': False, 'error': 'SourceUnavailable'}
                else:
                    result = {'ok': True, 'available': True, 'state': {'switch': {'on': state}}}
                payload = b'not-json' if mode == 'invalid_json' else json.dumps(result).encode()
            self.send_header('Content-Length', str(len(payload) + (5 if mode == 'truncated_http' else 0)))
            self.end_headers()
            try:
                self.wfile.write(payload)
                self.wfile.flush()
                if mode == 'keep_open':
                    time.sleep(13)
            except BrokenPipeError:
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cases = []
    try:
        with tempfile.TemporaryDirectory() as directory:
            root, parent, key = Path(directory), 'OHPOC-local-ha-smoke', 'virtual'
            sn = 'OHPOC-HA-' + hashlib.sha256(f'{parent}#ha-{key}-v1'.encode()).hexdigest()[:24]
            values = {
                'ha-children.local.json': [{'key': key, 'pid': 'fixture', 'devType': 'fixture',
                    'manu': 'fixture', 'services': {'switch': 'switch'}, 'existing_cloud_only': True}],
                'ha-child-virtual.json': {'version': 1, 'parent_sn': parent, 'sn': sn,
                    'udid': hashlib.sha256(f'OpenHarmonyHA{key}{sn}'.encode()).hexdigest(),
                    'devId': 'test-virtual', 'on': 0},
            }
            for name, value in values.items():
                path = root / name
                path.write_text(json.dumps(value)); path.chmod(0o600)
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            if baseline:
                mode = 'keep_open'
                original = subprocess.run([*runner, str(baseline.resolve()), directory, 'put',
                    'test-virtual', 'switch', '{"on":1}'], capture_output=True, text=True, timeout=18,
                    env={**os.environ, 'POC_HA_PROXY_PORT': str(server.server_port)})
                assert original.returncode == 2 and state == 1 and len(writes) == 1
                assert '"write_confirmed":true' not in original.stdout
            for mode, reason, error in [
                ('accepted', 'accepted', 'none'),
                ('keep_open', 'accepted', 'none'),
                ('applied_but_unconfirmed', 'backend_rejected', 'TimeoutError'),
                ('unsafe_error', 'backend_rejected', 'BackendError'),
                ('unavailable', 'backend_rejected', 'SourceUnavailable'),
                ('invalid_http', 'invalid_http', 'none'),
                ('invalid_json', 'invalid_json', 'none'),
                ('truncated_http', 'truncated_http', 'none'),
                ('receive_timeout', 'receive_timeout', 'none'),
            ]:
                state = 0
                count = len(writes)
                started = time.monotonic()
                result = subprocess.run([*runner, str(binary.resolve()), directory, 'put',
                    'test-virtual', 'switch', '{"on":1}'], capture_output=True, text=True, timeout=18,
                    env={**os.environ, 'POC_HA_PROXY_PORT': str(server.server_port)})
                diagnostics = [json.loads(line[3:]) for line in result.stdout.splitlines()
                    if line.startswith('HA ') and json.loads(line[3:]).get('event') == 'backend_result']
                assert len(diagnostics) == 1, result.stdout
                trace = diagnostics[0]
                assert (trace['reason'], trace['error_type']) == (reason, error), trace
                success = mode in ('accepted', 'keep_open')
                assert result.returncode == (0 if success else 2), result.stdout
                assert ('"write_confirmed":true' in result.stdout) == success
                if mode == 'keep_open':
                    assert time.monotonic() - started < 3, 'complete response must not wait for EOF'
                assert len(writes) == count + 1, 'a failure must not automatically replay a write'
                assert state == 1, 'the virtual HA action must really apply before confirmation'
                assert 'private-payload-sentinel' not in result.stdout + result.stderr
                cases.append({'case': mode, 'reason': reason, 'error_type': error,
                    'elapsed_seconds': round(time.monotonic() - started, 3), 'writes': 1})
            assert state == 1 and before == {p.name: p.read_bytes() for p in root.iterdir()}
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    proof = {'passed': True, 'cases': cases, 'production_access': False,
        'baseline_complete_response_rejected': baseline is not None,
        'real_cloud_or_app_verified': False, 'historical_failure_root_cause_proven': False}
    print(json.dumps(proof))
    return proof


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('binary', type=Path)
    parser.add_argument('--runner')
    parser.add_argument('--baseline-binary', type=Path)
    args = parser.parse_args()
    run(args.binary, [args.runner] if args.runner else [], args.baseline_binary)
