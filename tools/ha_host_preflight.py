"""独立SSH宿主通道和Docker只读预检；不安装、重启、停止、exec或读取配对KV。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

from ha_production_readonly import private_write

MAX_OUTPUT = 2 * 1024 * 1024
SCRIPT = Path(__file__).with_name('ha_host_probe.sh')
STAT_PATHS = ('.', 'custom_components/hwhomebridge', '.storage/hwhomebridge',
              '.storage/core.config_entries', '.storage/core.device_registry', '.storage/core.entity_registry')


def assess(raw):
    if len(raw) > MAX_OUTPUT:
        raise ValueError('host probe output too large')
    rows = {}
    for line in raw.decode().splitlines():
        key, value = line.split('\t', 1)
        if key in rows:
            raise ValueError('duplicate host evidence')
        rows[key] = value
    if rows.get('version') != '1' or rows.get('complete') != 'true':
        raise ValueError('incomplete host probe')
    if rows['uid'] != '0' or rows['init'] != 'systemd' or rows['container_context'] != 'false':
        raise ValueError('root systemd host context required')
    info, owner, mounts = (json.loads(rows[k]) for k in ('docker_info', 'owner', 'mounts'))
    if not info['id'] or not info['root'] or any('rootless' in s for s in info.get('security') or []):
        raise ValueError('local rootful Docker daemon required')
    config = rows['config']
    targets = [m for m in mounts if m['Destination'] == '/config']
    if not config.startswith('/') or len(targets) != 1 or targets[0]['Source'] != config or not targets[0]['RW']:
        raise ValueError('exact writable original config mount required')
    metadata = {name: json.loads(rows['stat:' + name]) for name in STAT_PATHS}
    for name, stat in metadata.items():
        allowed_types = {'directory'} if name in STAT_PATHS[:3] else {'regular file', 'regular empty file'}
        if stat['type'] not in allowed_types:
            raise ValueError('ordinary config roots and registry files required')
        if not re.fullmatch(r'[0-7]{3,4}', stat['mode']):
            raise ValueError('invalid source permission evidence')
    state = owner['state']
    if not state.get('Running') or state.get('Paused') or state.get('Restarting') or state.get('Pid', 0) <= 0:
        raise ValueError('online preflight requires currently running Core; cold export is separate')
    if owner['network'] != 'host' or rows['host_net'] != rows['core_net']:
        raise ValueError('Core must use the actual host network namespace')
    if not re.fullmatch(r'(?:[a-f0-9]{2}:){5}[a-f0-9]{2}', rows['interface_mac']) or rows['interface_mac'] == '00:00:00:00:00:00':
        raise ValueError('actual interface MAC required')
    uid = int(rows['runtime_uid'])
    observed_ports = {}
    for protocol in ('tcp', 'tcp6', 'udp', 'udp6'):
        observed_ports[protocol] = sorted({int(value.rsplit(':', 1)[1], 16) for value in rows['ports:' + protocol].split()})
    # 宿主上有Python命令也不能证明cryptography等依赖可用；不自动安装任何依赖。
    return {'version': 1, 'kind': 'hwhomebridge-host-preflight', 'date_utc': datetime.now(timezone.utc).isoformat(),
            'host_channel_verified': True, 'Core_independent_by_transport': True, 'Core_stopped_tested': False,
            'actual_Docker_inspected': True, 'host_root_systemd': True, 'docker_id': info['id'],
            'container_id': owner['id'], 'core_started_at': state.get('StartedAt'), 'config': config,
            'runtime_uid': uid, 'configured_container_user': owner['user'], 'source_metadata': metadata,
            'host_network_verified': True, 'interface_mac': rows['interface_mac'], 'observed_ports': observed_ports,
            'source_owner_matches_runtime_uid': all(v['uid'] == uid for v in metadata.values()),
            'restart_policy': owner['restart'], 'host_python_found': bool(rows['python']),
            'cold_export_dependencies_verified': False, 'cold_backup': False, 'production_install_allowed': False,
            'pairing_KV_read': False, 'container_exec_calls': 0, 'remote_file_writes': 0, 'restart_or_stop_calls': 0}


def ssh_arguments(host, port, key, known_hosts):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}', host) or not 1 <= port <= 65535:
        raise ValueError('plain SSH host and port required')
    for path in (key, known_hosts):
        if (not path.is_absolute() or any(ord(c) < 32 for c in str(path)) or not path.is_file()
                or path.is_symlink() or path.stat().st_mode & 0o077):
            raise ValueError('existing private key and pinned host-key file required')
    return ['ssh', '-F', '/dev/null', '-T', '-p', str(port), '-i', str(key),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known_hosts),
            '-o', 'ConnectTimeout=8', '-o', 'ForwardAgent=no', '-o', 'ClearAllForwardings=yes', 'root@' + host]


def collect(host, port, key, known_hosts, output, container='homeassistant', interface='enp2s1'):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', container) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,15}', interface):
        raise ValueError('explicit container and interface required')
    output = output.absolute()
    if output.exists() or output.resolve() != output:
        raise ValueError('new symlink-free private output required')
    argv = ssh_arguments(host, port, key, known_hosts)
    result = subprocess.run([*argv, 'sh -s -- ' + container + ' ' + interface], input=SCRIPT.read_bytes(),
                            capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError('authenticated host inspection unavailable; no remote changes requested')
    report = assess(result.stdout)
    output.mkdir(mode=0o700)
    private_write(output / 'host-preflight.private.json', json.dumps(report, indent=2).encode())
    return report


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=22222)
    parser.add_argument('--key', required=True, type=Path)
    parser.add_argument('--known-hosts', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--container', default='homeassistant')
    parser.add_argument('--interface', default='enp2s1')
    args = parser.parse_args()
    try:
        result = collect(args.host, args.port, args.key, args.known_hosts, args.output, args.container, args.interface)
    except Exception:
        print(json.dumps({'host_preflight_complete': False, 'error': 'host inspection failed; no install, stop or restart requested'}))
        raise SystemExit(1) from None
    print(json.dumps({'host_preflight_complete': True, 'actual_Docker_inspected': result['actual_Docker_inspected'],
                      'host_python_found': result['host_python_found'], 'production_install_allowed': False}))


if __name__ == '__main__':
    main()
