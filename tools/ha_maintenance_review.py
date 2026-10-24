"""离线复核两端只读观察，输出脱敏计数；不是停机、接管或部署许可。"""
import argparse
from collections import Counter
import json
from pathlib import Path


HOST_FIELDS = ('docker_id', 'container_id', 'core_started_at', 'config', 'runtime_uid',
               'interface_mac', 'host_network_verified', 'restart_policy')


def load(path):
    return json.loads(path.read_text())


def review(before, after, host_before, host_after, preflight, binding):
    first, last = (load(p / 'online-copy.private.json') for p in (before, after))
    entries = [load(p / 'hwhomebridge.entries.private.json') for p in (before, after)]
    hosts = [load(p / 'host-preflight.private.json') for p in (host_before, host_after)]
    if not first['files'] or not last['files']:
        raise ValueError('complete online component observations required')
    if any(not h.get('host_channel_verified') or not h.get('host_root_systemd') for h in hosts):
        raise ValueError('two verified online host observations required')
    if (not preflight.get('inventory_complete') or not preflight.get('two_pass_registry_stable')
            or preflight.get('kind') != 'hwhomebridge-entity-preflight'):
        raise ValueError('complete stable entity preflight required')
    if (binding.get('kind') != 'hwhomebridge-existing-log-binding-audit'
            or binding.get('original_runtime_bindings_confirmed') is not False):
        raise ValueError('existing-log audit cannot establish current runtime binding')
    targets = [row for row in preflight['legacy_bindings']
               if row['source_id'] == binding['source_id'] and row['sn'] == binding['sn']]
    if (len(targets) != 1 or targets[0]['entities'] != binding['observed_order_replay']
            or targets[0]['ambiguous_choices'] != binding['ambiguous_choices']):
        raise ValueError('binding audit must refer to this exact entity observation')
    checks = {
        'component_bytes_unchanged': first['files'] == last['files'],
        'owned_entries_unchanged': entries[0] == entries[1],
        'same_running_Core_and_host': all(hosts[0][k] == hosts[1][k] for k in HOST_FIELDS),
        'adapter_copy_matches_entity_preflight': {
            name: value for name, value in first['files'].items()
            if name.startswith('config/') and name.endswith('.json')
        } == preflight['config_sha256'],
    }
    return {
        'version': 1, 'kind': 'hwhomebridge-maintenance-readonly-review',
        'observations_consistent': all(checks.values()), **checks,
        'component_files': len(first['files']), 'legacy_devices': len(preflight['legacy_inventory']),
        'proposed_role_count': sum(len(row['roles']) for row in preflight['proposed_roles']),
        'composition_cards': len(preflight['compositions']),
        'issue_counts': dict(Counter(row['code'] for row in preflight['issues'])),
        'target_legacy_pid': targets[0]['pid'],
        'target_ambiguous_services': sorted(targets[0]['ambiguous_choices']),
        'historical_route_count': len(binding['historical_routes']),
        'original_runtime_bindings_confirmed': False,
        'cold_backup': False, 'migration_allowed': False,
        'production_install_allowed': False,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('before', 'after', 'host-before', 'host-after', 'preflight', 'binding'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    try:
        result = review(args.before, args.after, args.host_before, args.host_after,
                        load(args.preflight), load(args.binding))
    except (KeyError, ValueError, OSError, TypeError):
        print(json.dumps({'observations_consistent': False, 'error': 'incomplete or mismatched private observations'}))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['observations_consistent'] else 1)
