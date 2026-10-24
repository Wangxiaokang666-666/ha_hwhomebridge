"""读取已有Core日志核对旧灯路由证据；不改日志级别，不触发设备控制或换绑。"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import urllib.request

from ha_production_readonly import MAX_FILE, NoRedirect, SupervisorReader, private_write


def read_logs(url, token):
    # 固定已存在的只读日志资源，无可变路径、查询、重定向或follow流。
    origin = SupervisorReader(url, token).url
    request = urllib.request.Request(origin + '/api/hassio/core/logs/latest',
                                     headers={'Authorization': 'Bearer ' + token}, method='GET')
    with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
        data = response.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ValueError('existing Core log exceeds private read limit')
        return data


def audit(raw, preflight, source_id):
    matches = [b for b in preflight['legacy_bindings'] if b['source_id'] == source_id]
    if len(matches) != 1:
        raise ValueError('exactly one inventoried original device required')
    binding = matches[0]
    sn = binding['sn']
    historical = []
    # 只接受原组件带命名空间的明确sn/service/entity记录；不采纳普通文本猜测。
    pattern = re.compile(r'\[custom_components\.hwhomebridge\.service_router\] route_action: sn=([^,]+), service=([^,]+), entity=([^,]+), payload=')
    for line_number, line in enumerate(raw.decode(errors='replace').splitlines(), 1):
        match = pattern.search(line)
        if match and match[1] == sn:
            historical.append({'line': line_number, 'service': match[2], 'entity': match[3]})
    return {'version': 1, 'kind': 'hwhomebridge-existing-log-binding-audit',
            'date_utc': datetime.now(timezone.utc).isoformat(), 'source_id': source_id, 'sn': sn,
            'log_sha256': hashlib.sha256(raw).hexdigest(), 'log_bytes': len(raw),
            'observed_order_replay': binding['entities'], 'ambiguous_choices': binding.get('ambiguous_choices', {}),
            'historical_routes': historical, 'historical_routes_observed': bool(historical),
            'original_runtime_bindings_confirmed': False, 'migration_allowed': False,
            'reason': 'existing route logs are historical evidence; no live binding API is available in deployed component',
            'log_level_changes': 0, 'HA_service_calls': 0, 'binding_writes': 0}


async def collect(url, token_file, preflight_file, source_id, output):
    output = output.absolute()
    if output.exists() or output.resolve() != output:
        raise ValueError('new symlink-free private output required')
    preflight = json.loads(preflight_file.read_text())
    if not preflight.get('two_pass_registry_stable') or not preflight.get('inventory_complete'):
        raise ValueError('complete stable original inventory required')
    raw = await asyncio.to_thread(read_logs, url, token_file.read_text().strip())
    report = audit(raw, preflight, source_id)
    output.mkdir(mode=0o700)
    private_write(output / 'core-latest.private.log', raw)
    private_write(output / 'binding-audit.private.json', json.dumps(report, ensure_ascii=False, indent=2).encode())
    return report


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--token-file', required=True, type=Path)
    parser.add_argument('--preflight', required=True, type=Path)
    parser.add_argument('--source-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(collect(args.url, args.token_file, args.preflight, args.source_id, args.output))
    except Exception:
        print(json.dumps({'log_audit_complete': False, 'error': 'existing log inspection failed; no changes requested'}))
        raise SystemExit(1) from None
    print(json.dumps({'log_audit_complete': True, 'historical_routes_observed': result['historical_routes_observed'],
                      'original_runtime_bindings_confirmed': False, 'migration_allowed': False}))
