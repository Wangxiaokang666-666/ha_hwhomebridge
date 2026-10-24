"""通过已固定SSH通道只读确认原HA进程的云连接；不exec容器、不写原机。"""
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess

from ha_binding_freeze import read_private
from ha_cold_export import absolute, verify, RUNTIME
from ha_entity_preflight import core_module
from ha_host_preflight import ssh_arguments
from ha_production_readonly import private_write


def owned_connections(raw, addresses):
    owned = set(re.findall(r'^socket:\[(\d+)\]$', raw, flags=re.MULTILINE))
    result = []
    for line in raw.splitlines():
        row = line.split()
        if (len(row) < 10 or not re.fullmatch(r'[0-9A-Fa-f]{8}(?:[0-9A-Fa-f]{24})?:[0-9A-Fa-f]{4}', row[2])
                or row[9] not in owned or row[3] != '01'):
            continue
        ip_hex, port_hex = row[2].split(':')
        words = [int(ip_hex[n:n + 8], 16) for n in range(0, len(ip_hex), 8)]
        peer = str(ipaddress.ip_address(struct.pack('<' + 'I' * len(words), *words)))
        if peer in addresses:
            result.append({'peer': peer, 'port': int(port_hex, 16), 'inode': row[9]})
    return result


def collect(cold, output, host, port, key, known_hosts, container='homeassistant'):
    cold, output = absolute(cold), absolute(output)
    verify(cold)
    if output.exists() or output.is_relative_to(cold) or cold.is_relative_to(output):
        raise ValueError('new private endpoint evidence directory required')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', container):
        raise ValueError('explicit safe container name required')
    manifest = read_private(cold / 'cold-export.json')
    original = Path(manifest['source_config'])
    if output.is_relative_to(original) or original.is_relative_to(output):
        raise ValueError('endpoint output must stay outside the original HA configuration')
    store = core_module('identity_store')
    source = cold / 'payload' / RUNTIME / 'config'
    identity = store.decode(store.private_read(source / 'config.cfg'), store.private_read(source / 'bridge.cfg'),
                            store.private_read(source / 'device_ac'), manifest['source_mac'])
    addresses = {str(ipaddress.ip_address(row[4][0])) for row in socket.getaddrinfo(identity['url'], None, socket.AF_UNSPEC)}
    ssh = ssh_arguments(host, port, key, known_hosts)
    def read(command):
        result = subprocess.run([*ssh, command], capture_output=True, timeout=30, check=True)
        if len(result.stdout) > 2 * 1024 * 1024:
            raise ValueError('bounded host evidence required')
        return result.stdout.decode()
    engine = 'docker --host unix:///var/run/docker.sock '
    template = '{{json .Id}}\t{{json .State.Running}}\t{{json .State.StartedAt}}\t{{json .HostConfig.NetworkMode}}'
    inspect = engine + 'inspect --format ' + shlex.quote(template) + ' ' + shlex.quote(container)
    before = read(inspect)
    owner = [json.loads(value) for value in before.strip().split('\t')]
    if owner[0] != manifest['owner']['container_id'] or owner[1] is not True or owner[3] != 'host':
        raise ValueError('same original running host-network Core container required')
    processes = read(engine + 'top ' + shlex.quote(container) + ' -eo pid,comm')
    pids = [row[0] for line in processes.splitlines() if len(row := line.split()) == 2
            and row[0].isdigit() and row[1] in ('python', 'python3')]
    if len(pids) != 1:
        raise ValueError('one identifiable HA Python process required')
    pid = pids[0]
    raw = read('for fd in /proc/' + pid + '/fd/*; do readlink "$fd"; done; cat /proc/' + pid + '/net/tcp /proc/' + pid + '/net/tcp6')
    matches = owned_connections(raw, addresses)
    if not matches or len({row['port'] for row in matches}) != 1 or read(inspect) != before:
        raise ValueError('stable unambiguous owned cloud endpoint connection required')
    proof = {'version': 1, 'read_only': True, 'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'root_id_sha256': store.digest(identity['dev_id'].encode()),
        'cloud_host_sha256': store.digest(identity['url'].encode()), 'matched_owned_connections': matches,
        'resolved_addresses': sorted(addresses), 'HA_process_host_PID': pid,
        'socket_evidence_sha256': store.digest(raw.encode()), 'container_id': owner[0], 'Core_started_at': owner[2]}
    output.mkdir(mode=0o700)
    private_write(output / 'owned-cloud-sockets.private.txt', raw.encode())
    private_write(output / 'cloud-endpoint.private.json', json.dumps(proof, indent=2).encode())
    return proof


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cold', 'output', 'key', 'known-hosts'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=22222)
    parser.add_argument('--container', default='homeassistant')
    args = parser.parse_args()
    try:
        proof = collect(args.cold, args.output, args.host, args.port, args.key, args.known_hosts, args.container)
        print(json.dumps({'owned_cloud_connections': len(proof['matched_owned_connections']),
                          'matched_ports': sorted({row['port'] for row in proof['matched_owned_connections']}),
                          'original_HA_written': False}))
    except Exception:
        print(json.dumps({'passed': False, 'error': 'read-only endpoint verification failed; original HA was not changed'}))
        raise SystemExit(1) from None
