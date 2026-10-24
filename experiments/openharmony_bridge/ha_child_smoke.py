"""Linux C 多子设备路由验收：仅用临时目录及回环 HTTP mock。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

binary = str(Path(sys.argv[1]).resolve())
requests = []
failed = False
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        requests.append(request)
        response = {'ok': not failed, 'state': {'switch': {'on': int(request['key'] == 'lamp')}}}
        payload = json.dumps(response).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
env = {**os.environ, 'POC_HA_PROXY_PORT': str(server.server_port)}
with tempfile.TemporaryDirectory(prefix='oh-ha-child-') as directory:
    root = Path(directory)
    config = [{'key': key, 'pid': '9MU7', 'devType': '119', 'manu': '002', 'services': {'switch': 'switch'}} for key in ('fan', 'lamp')]
    path = root / 'ha-children.local.json'; path.write_text(json.dumps(config)); path.chmod(0o600)
    def run(*args, ok=True):
        result = subprocess.run([binary, directory, *args], env=env, capture_output=True, text=True)
        assert (result.returncode == 0) == ok, (args, result.stdout, result.stderr)
        return result.stdout
    run('init')
    original = {}
    for key in ('fan', 'lamp'):
        path = root / f'ha-child-{key}.json'; state = json.loads(path.read_text()); original[key] = state['sn']; state['devId'] = 'own-' + key; path.write_text(json.dumps(state)); path.chmod(0o600)
    assert '\n{"on":0}\n' in run('get', 'own-fan', 'switch')
    assert '\n{"on":1}\n' in run('get', 'own-lamp', 'switch')
    assert requests[-2]['key'] == 'fan' and requests[-1]['key'] == 'lamp'
    run('put', 'own-fan', 'switch', '{"on":1}')
    count = len(requests)
    for args in [('get', 'foreign', 'switch'), ('put', 'own-fan', 'netInfo', '{"on":1}'), ('put', 'own-fan', 'switch', '[1]'), ('put', 'own-fan', 'switch', '{"on":1} trailing')]:
        run(*args, ok=False)
    assert len(requests) == count
    failed = True
    run('put', 'own-fan', 'switch', '{"on":1}', ok=False)
    failed = False
    run('init')
    assert all(json.loads((root / f'ha-child-{key}.json').read_text())['sn'] == original[key] for key in original)
    path = root / 'ha-child-fan.json'; saved = path.read_text(); path.chmod(0o644); run('init', ok=False); path.chmod(0o600)
    data = json.loads(saved); data['parent_sn'] = 'OHPOC-other'; path.write_text(json.dumps(data)); run('init', ok=False); path.write_text(saved)
    path.write_text('{broken'); run('init', ok=False); path.write_text(saved)
    config[1]['key'] = 'fan'; (root / 'ha-children.local.json').write_text(json.dumps(config)); run('init', ok=False)
server.shutdown()
print(json.dumps({'passed': ['两子设备按云端 ID 精确路由到不同 HA key', '合法写入经回环代理应答', '陌生 ID、错误服务和无效 JSON 不转发', '代理明确失败时 C 返回失败', '跨进程身份恢复', '拒绝过宽权限、跨父身份、损坏状态和重复 key'], 'real_ha_accessed': False}, ensure_ascii=False, indent=2))
