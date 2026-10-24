"""从冷备、常驻绑定和角色清单准备唯一网关目录；不连接HA，不启动SDK。"""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile

from ha_binding_freeze import read_private
from ha_cold_export import absolute, COMPONENT, RUNTIME
from ha_entity_preflight import core_module, registry_proof, digest, commands
from ha_paired_identity_import import import_identity
from ha_production_readonly import private_write
from ha_upgrade import TESTED_HA, elf

ROOT = Path(__file__).resolve().parents[1]
RELATIVE = '.storage/hwhomebridge/gateways/current'


def write_json(path, value):
    private_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode())


def check_services(manager, snapshot):
    required = set()
    for device in manager.get_all_devices():
        for service in device.product.services.values():
            mapping = service.ha_mapping
            if mapping is None:
                continue
            if core_module('fan_mapping').is_mapped_fan(mapping):
                for enum in mapping.value_mapping.mapping_dict:
                    action, _ = core_module('fan_mapping').fan_command(mapping, int(enum))
                    required.add((mapping.domain, action))
            else:
                required.update((mapping.domain, action) for action in commands(mapping))
            for timer in core_module('native_timer').timers_for_mapping(mapping).values():
                required.update({('switch', 'turn_on'), ('switch', 'turn_off'), ('number', 'set_value')})
    if any(action not in snapshot['services'].get(domain, []) for domain, action in required):
        raise ValueError('required HA control service is not registered')


def native_files(directory):
    store = core_module('identity_store')
    info = json.loads((directory / 'build-info.json').read_text())
    if info['target'] != 'x86_64-linux-musl':
        raise ValueError('production target requires validated amd64 artifacts')
    for name, expected in info['source_files_sha256'].items():
        if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts:
            raise ValueError('invalid native source path')
        if store.digest((ROOT / 'experiments/openharmony_bridge' / name).read_bytes()) != expected:
            raise ValueError('native source differs from checkout')
    if store.digest(json.dumps(info['source_files_sha256'], sort_keys=True).encode()) != info['source_tree_sha256']:
        raise ValueError('native source manifest mismatch')
    result = {}
    for name in ('bridge_poc', 'identity_state_probe'):
        raw = (directory / name).read_bytes()
        elf(raw, 'x86_64')
        if store.digest(raw) != info['artifacts'][name]['sha256']:
            raise ValueError('native artifact checksum mismatch')
        result[name] = raw
    return result, info


check_profile = core_module('profile_validation').check_profile

async def prepare(*, cold, frozen, profiles, endpoint, instances, snapshot, native, ca,
                  output, mapping=None, target_config='/config'):
    cold, frozen, profiles, native, output = map(absolute, (cold, frozen, profiles, native, output))
    mapping = absolute(mapping or ROOT / 'custom_components/hwhomebridge/openharmony/config')
    target = PurePosixPath(target_config)
    if not target.is_absolute() or '..' in target.parts or str(target) != target_config:
        raise ValueError('canonical absolute target config path required')
    original = read_private(cold / 'cold-export.json')
    source_config = Path(original['source_config'])
    inputs = [cold, frozen, profiles, mapping, native, absolute(instances), absolute(snapshot), absolute(ca), source_config, ROOT]
    if (output.exists() or not output.parent.is_dir()
            or any(output.is_relative_to(p) or p.is_relative_to(output) for p in inputs)):
        raise ValueError('new private staging output outside every source required')
    fresh = read_private(snapshot)
    proof = original['entity_preflight']
    if (fresh['ha_version'] != TESTED_HA or len(fresh['entries']) != 1
            or fresh['entries'][0]['entry_id'] != original['entry']['entry_id']
            or fresh['entries'][0]['state'] != 'loaded'
            or registry_proof(fresh['devices'], fresh['entities'], set(proof['source_ids']), set(proof['bridge_ids'])) != proof['registry_proof']):
        raise ValueError('fresh original entry and unchanged cold registry required')
    files, build = native_files(native)
    entities = {row['entity_id']: row for row in fresh['entities']}
    devices = {row['id']: row for row in fresh['devices']}
    roles = read_private(instances)
    if set(roles) - {'version', 'devices', 'parent_sn'} or roles.get('version') != 1 or not roles['devices']:
        raise ValueError('offline role instance configuration required; no remote transport')
    if len({row['id'] for row in roles['devices']}) != len(roles['devices']):
        raise ValueError('duplicate source in role configuration')
    owned = {}
    for source in roles['devices']:
        if source['id'] not in proof['source_ids'] or devices[source['id']].get('disabled_by') is not None:
            raise ValueError('composition must extend an enabled original physical device')
        if source.get('model', devices[source['id']].get('model')) != devices[source['id']].get('model'):
            raise ValueError('composition model differs from the observed physical device')
        source.setdefault('model', devices[source['id']].get('model') or '')
        owned[source['id']] = {entity for entity, row in entities.items()
                              if row.get('device_id') == source['id'] and row.get('disabled_by') is None}
    store = core_module('identity_store')
    registry_module, virtual = core_module('product_registry'), core_module('virtual_device')
    configuration = core_module('openharmony.configuration')
    registry = registry_module.ProductRegistry()
    config = ROOT / 'custom_components/hwhomebridge/config'
    if not await registry.load(str(config / 'product_registry.json'), str(config / 'adapters')):
        raise ValueError('public legacy mapping required')
    states = {row['entity_id']: row for row in fresh['states']}
    class OfflineTransport(core_module('ha_transport').ScopedTransport):
        def states(self):
            return self.filter_states(states.values())
        def call(self, *_args, **_kwargs):
            raise RuntimeError('offline preparation cannot control HA')
    class Router(core_module('capability_router').CapabilityRouter):
        registry_class = configuration.OpenHarmonyRegistry
    # 所有身份文件只在私有临时目录中重建；失败不留下可启用目录。
    with tempfile.TemporaryDirectory(prefix='.hwhomebridge-prepare-', dir=output.parent) as temporary:
        stage = Path(temporary)
        receipt = await asyncio.to_thread(import_identity, cold, frozen, profiles, endpoint, stage / 'imported')
        imported = stage / 'imported'
        bindings = read_private(frozen / 'bindings.json')
        parent = read_private(imported / 'gateway.local.json')
        if 'parent_sn' in roles and roles['parent_sn'] != parent['sn']:
            raise ValueError('role configuration cannot replace the original parent identity')
        roles['parent_sn'] = parent['sn']
        manager = virtual.SNManager()
        for row in bindings['devices']:
            product = registry.get_product(row['pid'])
            adapter = None
            if row.get('adapter'):
                choices = [a for a in registry.get_all_adapters() if a.pid == row['pid'] and a.services == row['adapter']]
                if len(choices) != 1:
                    raise ValueError('frozen adapter not uniquely reconstructable')
                adapter = choices[0]
            product = registry.merge(product, adapter)
            if registry_module.product_fingerprint(product) != row['product_sha256']:
                raise ValueError('current public mapping differs from frozen production mapping')
            device = virtual.VirtualDevice(row['sn'], product, row['source_id'])
            for sid, entity in row['entities'].items():
                mapping_def = product.services[sid].ha_mapping
                device.add_service_entry(sid, virtual.ServiceEntry(entity, mapping_def.domain, sid))
            if not manager.register(device):
                raise ValueError('duplicate old identity')
        supported = {path.name.removesuffix('-profile.json') for path in profiles.glob('*-profile.json')}
        router = await Router.create(OfflineTransport(), mapping, roles, owned_entities=owned,
            supported_pids=supported, sn_manager=manager,
            sn_factory=lambda parent, source, key: configuration.managed_child_sn(parent, key))
        check_services(manager, fresh)
        if len(router.preserved_cards) != len(roles['devices']):
            raise ValueError('each original device must retain its primary card')
        additions = router.registry.catalog(router.devices)
        for row in additions:
            row['sn'] = router.devices[row['key']].sn
        catalog = read_private(imported / 'ha-children.local.json') + additions
        if (not 1 <= len(catalog) <= 64 or len({r['key'] for r in catalog}) != len(catalog)
                or len({r['sn'] for r in catalog}) != len(catalog)):
            raise ValueError('unique bounded old and supplemental child catalog required')
        hashes, profile_bytes = {}, {}
        for row in catalog:
            raw = (profiles / (row['pid'] + '-profile.json')).read_bytes()
            if len(raw) > 131072:
                raise ValueError('bounded product profile required')
            definition = router.registry.definitions[row['pid']] if row in additions else None
            check_profile(json.loads(raw), row, definition)
            hashes[row['pid']], profile_bytes[row['pid']] = store.digest(raw), raw
        unavailable = []
        for key in router.devices:
            try:
                router.snapshot(key)
            except core_module('availability').SourceUnavailable:
                unavailable.append(key)
        bundle = stage / 'bundle'
        bundle.mkdir(mode=0o700)
        directory = bundle / 'config' / RELATIVE
        directory.mkdir(mode=0o700, parents=True)
        shutil.copytree(imported / 'sdk-state', directory / 'runtime/sdk-state')
        for path in imported.glob('ha-child-*.json'):
            private_write(directory / 'runtime' / path.name, path.read_bytes())
        shutil.copytree(mapping, directory / 'mapping')
        legacy = cold / 'payload' / RUNTIME / 'config'
        shutil.copytree(legacy, directory / 'legacy')
        parent.update(pin='00000000', pin_expires_at=1, ca_file=str(target / RELATIVE / 'ca.pem'))
        private_write(directory / 'ca.pem', Path(ca).read_bytes())
        for name, raw in files.items():
            private_write(directory / name, raw); (directory / name).chmod(0o700)
        for pid, raw in profile_bytes.items():
            private_write(directory / 'profiles' / (pid + '.json'), raw)
        for name, value in {'bindings.json': bindings, 'instances.json': roles, 'gateway.local.json': parent,
                            'runtime/ha-children.local.json': catalog}.items():
            write_json(directory / name, value)
        receipt.update(new_child_keys=[r['key'] for r in additions], profile_sha256=hashes,
            target_catalog_sha256=store.digest((directory / 'runtime/ha-children.local.json').read_bytes()),
            source_import_receipt_sha256=store.digest((imported / 'import-receipt.json').read_bytes()))
        write_json(directory / 'runtime/import-receipt.json', receipt)
        inputs = [directory / n for n in ('bindings.json', 'instances.json', 'gateway.local.json')]
        inputs += list((directory / 'mapping').rglob('*.json')) + list((directory / 'profiles').glob('*.json'))
        settings = {'version': 1, 'entry_unique_id': original['entry']['unique_id'],
            'input_sha256': {str(p.relative_to(directory)): store.digest(p.read_bytes()) for p in inputs},
            'root_id_sha256': receipt['root_id_sha256'], 'source_mac': original['source_mac'],
            'interface': original['source_interface'], 'port': 18093, 'lan_port': 15686,
            'startup_timeout': 120, 'availability_protocol': 1, 'managed_catalog_protocol': 1, 'profiles': hashes,
            'binary_sha256': store.digest(files['bridge_poc']), 'probe_sha256': store.digest(files['identity_state_probe']),
            'offline_preparation': True}
        write_json(directory / 'deployment.json', settings)
        result = {'version': 1, 'kind': 'hwhomebridge-offline-gateway-preparation',
            'prepared_utc': datetime.now(timezone.utc).isoformat(),
            'source_cold_created_utc': original['created_utc'],
            'integration_version': json.loads((ROOT / COMPONENT / 'manifest.json').read_text())['version'],
            'old_children': len(bindings['devices']), 'new_children': len(additions), 'parent_gateways': 1,
            'preserved_primary_cards': len(router.preserved_cards), 'unavailable_supplement_cards': len(unavailable),
            'source_cold_manifest_sha256': receipt['cold_manifest_sha256'], 'fresh_snapshot_sha256': digest(fresh),
            'native_source_tree_sha256': build['source_tree_sha256'], 'source_role_configuration_sha256': digest(read_private(instances)),
            'target_relative_directory': RELATIVE, 'activation_blocked': True, 'native_started': False,
            'original_HA_written': False, 'production_adoption_ready': False,
            'files_sha256': {str(p.relative_to(directory)): store.digest(p.read_bytes()) for p in directory.rglob('*') if p.is_file()}}
        write_json(bundle / 'preparation-receipt.private.json', result)
        for path in bundle.rglob('*'):
            path.chmod(0o700 if path.is_dir() or path.name in files else 0o600)
        bundle.rename(output)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('cold', 'frozen', 'profiles', 'endpoint', 'instances', 'snapshot', 'native', 'ca', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--mapping', type=Path)
    parser.add_argument('--target-config', default='/config')
    args = parser.parse_args(); os.umask(0o077)
    try:
        result = asyncio.run(prepare(**vars(args)))
        print(json.dumps({key: result[key] for key in ('old_children', 'new_children', 'parent_gateways',
            'preserved_primary_cards', 'activation_blocked', 'original_HA_written', 'production_adoption_ready')}))
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__, 'original_HA_written': False}))
        raise SystemExit(1) from None
