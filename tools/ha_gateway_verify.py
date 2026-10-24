"""独立无网络容器中的生产目录验真；只运行身份读取器，绝不启动SDK。"""
import argparse
import json
from pathlib import Path
import platform
import socket
import shutil
import tempfile
import subprocess

from ha_entity_preflight import core_module, digest
from ha_binding_freeze import read_private


def verify(bundle, config=Path('/config')):
    if platform.system() != 'Linux' or {name for _, name in socket.if_nameindex()} != {'lo'}:
        raise ValueError('verification requires an isolated Linux network namespace with loopback only')
    proof = read_private(bundle / 'preparation-receipt.private.json')
    if (proof.get('kind') != 'hwhomebridge-offline-gateway-preparation'
            or proof.get('activation_blocked') is not True or proof['parent_gateways'] != 1):
        raise ValueError('blocked single-parent preparation required')
    spec = core_module('gateway_deployment').load_gateway(config, proof['target_relative_directory'], 'hwhomebridge')
    store = core_module('identity_store')
    actual = {str(p.relative_to(spec.directory)): store.digest(p.read_bytes())
              for p in spec.directory.rglob('*') if p.is_file()}
    if actual != proof['files_sha256']:
        raise ValueError('prepared directory changed')
    if digest(spec.bindings) != spec.receipt['bindings_sha256']:
        raise ValueError('frozen source bindings changed')
    for mode in ('legacy', 'openharmony'):
        try:
            spec.verify_engine(mode)
        except ValueError as error:
            if 'offline production preparation' not in str(error):
                raise
        else:
            raise ValueError('preparation unexpectedly permits engine startup')
    for row in spec.catalog:
        if row['key'] in spec.receipt['new_child_keys']:
            if row['sn'] != core_module('openharmony.configuration').managed_child_sn(spec.root['sn'], row['key']):
                raise ValueError('supplement child identity changed')
    before = {str(p.relative_to(spec.runtime)): store.digest(p.read_bytes())
              for p in spec.runtime.rglob('*') if p.is_file()}
    # 上游 KV 读取使用 rb+；只给读取器一份可写临时副本，原准备目录始终只读挂载。
    with tempfile.TemporaryDirectory(prefix='hwhomebridge-reader-') as temporary:
        copied = Path(temporary) / 'sdk-state'
        shutil.copytree(spec.runtime / 'sdk-state', copied)
        copied_before = {p.name: store.digest(p.read_bytes()) for p in copied.iterdir()}
        process = subprocess.run([str(spec.probe), str(copied), str(spec.gateway)],
            capture_output=True, text=True, check=True, timeout=45)
        if copied_before != {p.name: store.digest(p.read_bytes()) for p in copied.iterdir()}:
            raise ValueError('identity reader modified its temporary KV copy')
    values = [json.loads(line[15:]) for line in process.stdout.splitlines() if line.startswith('IDENTITY_PROBE ')]
    if (len(values) != 1 or values[0].get('bound_identity_loaded') is not True
            or values[0]['dev_id_sha256'] != spec.identity
            or values[0]['fields'] != spec.receipt['account_field_sha256']
            or values[0]['network_started'] is not False):
        comparison = {'single_result': len(values) == 1}
        if len(values) == 1:
            comparison.update(bound=values[0].get('bound_identity_loaded'),
                parent=values[0].get('dev_id_sha256') == spec.identity,
                fields={key: values[0].get('fields', {}).get(key) == expected for key, expected in spec.receipt['account_field_sha256'].items()})
        raise ValueError('native reader identity comparison failed: ' + json.dumps(comparison))
    after = {str(p.relative_to(spec.runtime)): store.digest(p.read_bytes())
             for p in spec.runtime.rglob('*') if p.is_file()}
    if before != after:
        raise ValueError('native reader changed identity files')
    return {'passed': True, 'architecture': platform.machine(), 'parent_gateways': 1,
        'old_children': len(spec.bindings['devices']), 'supplement_children': len(spec.receipt['new_child_keys']),
        'native_identity_reader_verified': True, 'paired_files_unchanged': True,
        'network_namespace_loopback_only': True, 'both_engine_starts_blocked': True,
        'SDK_main_started': False, 'production_adoption_ready': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle)))
