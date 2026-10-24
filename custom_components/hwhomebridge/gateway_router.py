"""接管后继续使用公共产品、角色、定时及控制路由；实体归属由HA注册表确认。"""
import hashlib
import time
from pathlib import Path


from .ha_transport import HomeAssistantTransport, async_owned_entity_inventory
from .profile_bridge import ProfileBridge
from .service_router import ServiceRouter
from .product_registry import product_fingerprint
from .virtual_device import VirtualDevice, ServiceEntry, SNManager
from .capability_router import CapabilityRouter
from .openharmony.configuration import OpenHarmonyRegistry, managed_child_sn


class GatewayRouter:
    disabled_keys = frozenset()

    @classmethod
    async def create(cls, hass, deployment, entry_id, management=None):
        from homeassistant.helpers import device_registry
        self = cls()
        self.legacy = ServiceRouter()
        self.legacy.set_hass(hass)
        config = Path(__file__).parent / 'config'
        if not await self.legacy.load_product_registry(str(config / 'product_registry.json')):
            raise ValueError('original product registry unavailable')
        self.bridges = {}
        self.entities = set()
        rows = list(deployment.bindings['devices'])
        self.disabled_keys = set()
        self.catalog = []
        instance = deployment.read('instances.json')
        configured = None
        if management is not None:
            if deployment.settings.get('managed_catalog_protocol') != 1:
                raise ValueError('rebuild the gateway directory with managed catalog support before saving')
            from .gateway_management import GatewayManagement
            configured = await GatewayManagement.for_deployment(hass, deployment)
            management = configured.document(management)
            self.disabled_keys.update(row['key'] for row in rows if row['source_id'] not in management['enabled'])
            instance = {'version': 1, 'parent_sn': instance['parent_sn'], 'devices': []}
            for recipe in management['devices']:
                if recipe['id'] in management['enabled']:
                    configured.validate_roles(recipe)
                if recipe['kind'] == 'light':
                    prototype = next((r for r in rows if r['pid'] == '002'), None)
                    if prototype is None:
                        raise ValueError('verified standard light profile is unavailable')
                    source_id = recipe['id']
                    key = 'device-' + hashlib.sha256(source_id.encode()).hexdigest()[:16]
                    rows.append({**prototype, 'key': key, 'source_id': source_id,
                        'sn': SNManager.generate_sn(source_id), 'adapter': None,
                        'product_sha256': None, 'entities': {sid: recipe['roles']['main'] for sid in prototype['entities']}})
                    if source_id not in management['enabled']:
                        self.disabled_keys.add(key)
                else:
                    source = {k: v for k, v in recipe.items() if k != 'kind'}
                    instance['devices'].append(source)
                    if source['id'] not in management['enabled']:
                        self.disabled_keys.update(source.get('key_prefix', '') + card['key'] for card in configured.cards(source))
        devices = device_registry.async_get(hass)
        excluded = deployment.directory / 'legacy/excluded_devices.txt'
        if excluded.exists() and excluded.read_text().strip():
            raise ValueError('excluded legacy devices require an explicit offline adoption plan')
        for row in rows:
            source = devices.async_get(row['source_id'])
            if row['key'] not in self.disabled_keys and (source is None or source.disabled_by is not None):
                raise ValueError('existing source device is missing or disabled')
        # 沿用既有桥接ID，也沿用其禁用决定；不因换引擎重新启用。
        disabled_sns = {row['sn'] for row in rows if row['key'] in self.disabled_keys}
        for device in device_registry.async_entries_for_config_entry(devices, entry_id):
            if device.disabled_by is not None and not any(identifier[-1] in disabled_sns for identifier in device.identifiers):
                raise ValueError('disabled bridge devices require an explicit offline adoption plan')
        inventory = async_owned_entity_inventory(hass, {row['source_id'] for row in rows})
        for row in rows:
            if row['key'] not in self.disabled_keys and set(row['entities'].values()) - inventory[row['source_id']]:
                raise ValueError('entity belongs to a different HA device')
            base = self.legacy.registry.get_product(row['pid'])
            if base is None or row['sn'] != SNManager.generate_sn(row['source_id']):
                raise ValueError('existing product or deterministic SN mismatch')
            adapter = None
            if row.get('adapter'):
                choices = [a for a in self.legacy.registry.get_all_adapters()
                           if a.pid == row['pid'] and a.services == row['adapter']]
                if len(choices) != 1:
                    raise ValueError('existing vendor adapter changed')
                adapter = choices[0]
            product = self.legacy.registry.merge(base, adapter)
            if row.get('product_sha256') is not None and row['product_sha256'] != product_fingerprint(product):
                raise ValueError('frozen original product mapping changed')
            device = VirtualDevice(row['sn'], product, row['source_id'])
            for sid, entity in row['entities'].items():
                mapping = product.services[sid].ha_mapping
                if mapping is None or entity.split('.')[0] != mapping.domain:
                    raise ValueError('existing service/entity mapping changed')
                device.add_service_entry(sid, ServiceEntry(entity, mapping.domain, sid))
            if not self.legacy.sn_manager.register(device):
                raise ValueError('duplicate existing SN')
            profile = deployment.read('profiles/' + row['cloud_pid'] + '.json')
            if deployment.settings['profiles'][row['cloud_pid']] != hashlib.sha256(
                    (deployment.directory / 'profiles' / (row['cloud_pid'] + '.json')).read_bytes()).hexdigest():
                raise ValueError('legacy profile changed')
            bridge = ProfileBridge(self.legacy, device, profile)
            catalog = next((item for item in deployment.catalog if item['key'] == row['key']), None)
            if catalog is None:
                prototype = next(item for item in deployment.catalog if item['pid'] == row['cloud_pid'])
                catalog = {**prototype, 'key': row['key'], 'sn': row['sn'], 'services': bridge.services}
                catalog.pop('existing_cloud_only', None)
            if catalog['sn'] != device.sn or catalog['pid'] != row['cloud_pid'] or catalog['services'] != bridge.services:
                raise ValueError('old cloud catalog differs from public router')
            self.bridges[row['key']] = bridge
            self.catalog.append(catalog)
            self.entities.update(row['entities'].values())
        composition_inventory = async_owned_entity_inventory(hass, {row['id'] for row in instance['devices']})
        if management is not None:
            for row in instance['devices']:
                if row['id'] not in management['enabled']:
                    # 停用来源不参与控制；实体消失也不能拖累其他卡片。
                    composition_inventory[row['id']] = set(row['roles'].values())
        class BoundRouter(CapabilityRouter):
            registry_class = OpenHarmonyRegistry
        self.capabilities = await BoundRouter.create(HomeAssistantTransport(hass), deployment.directory / 'mapping', instance,
            owned_entities=composition_inventory, supported_pids=set(deployment.settings['profiles']),
            sn_manager=self.legacy.sn_manager, sn_factory=lambda parent, source, key: managed_child_sn(parent, key))
        self.capabilities.set_hass(hass)
        expected = self.capabilities.registry.catalog(self.capabilities.devices)
        actual = {row['key']: {k: v for k, v in row.items() if k != 'sn'} for row in deployment.catalog
                  if row['key'] not in self.bridges}
        if management is None and {row['key']: row for row in expected} != actual:
            raise ValueError('composition catalog differs from configured capabilities')
        base_catalog = {row['key']: row for row in deployment.catalog}
        for row in expected:
            sn = self.capabilities.devices[row['key']].sn
            previous = base_catalog.get(row['key'])
            if previous is not None and (previous['sn'] != sn or previous['pid'] != row['pid']):
                raise ValueError('existing card identity or product cannot change')
            self.catalog.append({**row, 'sn': sn})
        if management is not None:
            # 已配对的旧布局只报离线，不静默删除或换 SN；未配对的旧候选不再注册。
            from .gateway_management import child_is_bound, retained_cards
            for row in retained_cards(deployment, self.catalog, self.capabilities.registry):
                self.catalog.append(row)
                self.disabled_keys.add(row['key'])
            original_keys = {row['key'] for row in deployment.bindings['devices']}
            self.catalog = [row for row in self.catalog if row['key'] not in self.disabled_keys
                            or row['key'] in original_keys or child_is_bound(deployment, row)]
        if len(self.catalog) > 64 or len({row['key'] for row in self.catalog}) != len(self.catalog):
            raise ValueError('unique bounded active card catalog required')
        from .profile_validation import check_profile
        for row in self.catalog:
            check_profile(deployment.read('profiles/' + row['pid'] + '.json'), row,
                          self.capabilities.registry.definitions.get(row['pid']))
        self.entities.update(entity for source in instance['devices'] for entity in source['roles'].values())
        self.manager = self.legacy.sn_manager
        return self

    def snapshot(self, key):
        if key in self.disabled_keys:
            from .availability import SourceUnavailable
            raise SourceUnavailable('bridge device disabled by user')
        return self.bridges[key].snapshot() if key in self.bridges else self.capabilities.snapshot(key)

    def write(self, key, sid, data):
        if key in self.disabled_keys:
            self.snapshot(key)
        if key not in self.bridges:
            return self.capabilities.write(key, sid, data)
        self.bridges[key].write(sid, data)
        deadline = time.monotonic() + 6
        while True:
            state = self.snapshot(key)
            value = state[sid]
            if sid == 'delay':
                confirmed = value['action'] == (2 if data['action'] == 2 else 0)
            else:
                confirmed = all(name in value and abs(value[name] - requested) <= (1 if sid == 'brightness' else 0)
                                for name, requested in data.items())
            if confirmed:
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError('HA did not confirm the requested state')
            time.sleep(.05)
