"""同一网关的用户配置层；冻结资料只读，HA 选项保存选择和角色。"""
import copy
import hashlib
import json
import re
from collections import Counter

from .const import DOMAIN, managed_directory
from .gateway_deployment import load_gateway
from .ha_transport import async_owned_entity_inventory
from .openharmony.configuration import OpenHarmonyRegistry, managed_child_sn
from .product_matcher import DeviceInfo, ProductMatcher
from .role_binding import RoleBindings
from .native_timer import timers_for_mapping
from .card_layouts import apply_layout, template_notes

OPTION = 'gateway_devices'


def revision(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class GatewayManagement:
    @classmethod
    async def create(cls, hass, entry):
        spec = await hass.async_add_executor_job(load_gateway, hass.config.config_dir,
                                                 managed_directory(entry), entry.unique_id)
        return await cls.for_deployment(hass, spec)

    @classmethod
    async def for_deployment(cls, hass, spec):
        from homeassistant.helpers import device_registry
        self = cls()
        self.hass, self.spec = hass, spec
        self.registry = OpenHarmonyRegistry()
        await self.registry.load_configuration(spec.directory / 'mapping')
        self.matcher = ProductMatcher(self.registry)
        self.device_registry = device_registry.async_get(hass)
        self.old_sources = {row['source_id'] for row in spec.bindings['devices']}
        self.base_instances = spec.read('instances.json')
        self.base_sources = {row['id']: row for row in self.base_instances['devices']}
        return self

    def initial(self):
        return {'version': 1, 'identity': self.spec.identity,
                'enabled': sorted(self.old_sources | set(self.base_sources)),
                'devices': [{**copy.deepcopy(row), 'kind': 'composition'}
                            for row in self.base_sources.values()]}

    def document(self, value=None):
        value = copy.deepcopy(self.initial() if value is None else value)
        if (set(value) != {'version', 'identity', 'enabled', 'devices'} or value['version'] != 1
                or value['identity'] != self.spec.identity or not isinstance(value['devices'], list)
                or not isinstance(value['enabled'], list) or len(set(value['enabled'])) != len(value['enabled'])):
            raise ValueError('management configuration belongs to another gateway')
        ids = [row.get('id') for row in value['devices']]
        if any(not isinstance(source, str) or not source for source in ids) or len(set(ids)) != len(ids):
            raise ValueError('unique managed source devices required')
        if set(value['enabled']) - (self.old_sources | set(ids)):
            raise ValueError('unknown enabled device')
        for row in value['devices']:
            if (set(row) - {'kind', 'id', 'name', 'model', 'manufacturer', 'roles', 'cards', 'layout', 'key_prefix'}
                    or row.get('kind') not in ('composition', 'light')):
                raise ValueError('invalid managed device recipe')
            if row['kind'] == 'light' and row['id'] in self.old_sources:
                raise ValueError('existing primary identity cannot be replaced')
            if row['id'] in self.base_sources:
                original = self.base_sources[row['id']]
                if row.get('key_prefix', '') != original.get('key_prefix', '') or row['kind'] != 'composition':
                    raise ValueError('existing card namespace cannot change')
            elif row['kind'] == 'composition' and row.get('key_prefix') != self.source_prefix(row['id']):
                raise ValueError('new card namespace must be derived from the source device')
        return value

    @staticmethod
    def source_prefix(source_id):
        return 'd' + hashlib.sha256(source_id.encode()).hexdigest()[:10] + '-'

    def info(self, source_id):
        source = self.device_registry.async_get(source_id)
        if source is None or source.disabled_by is not None:
            raise ValueError('source device is missing or disabled')
        if any(identifier[0] == DOMAIN for identifier in source.identifiers):
            raise ValueError('a bridge child cannot be its own source')
        entities = async_owned_entity_inventory(self.hass, {source_id})[source_id]
        original = self.base_sources.get(source_id, {})
        model = source.model or original.get('model', '')
        return source, entities, DeviceInfo(source_id, source.name_by_user or source.name or source_id,
            model, source.manufacturer or '', sorted({entity.split('.')[0] for entity in entities}))

    def product(self, row):
        info = DeviceInfo(row['id'], row.get('name', ''), row.get('model', ''),
                          row.get('manufacturer', ''), sorted({x.split('.')[0] for x in row['roles'].values()}))
        product, _ = self.matcher.match_with_detail(info, include_compositions=True)
        if product is None or product.composition not in self.registry.bundles:
            raise ValueError('no supported composition adapter')
        if row.get('layout', 'original') != 'original':
            product = apply_layout(self.registry, product, row['layout'])
        return product

    def cards(self, row):
        product = self.product(row)
        return self.registry.bundles[product.composition]

    def card_products(self, row):
        source_product = self.product(row)
        for card in self.registry.bundles[source_product.composition]:
            product = (copy.deepcopy(source_product) if source_product.pid == card['pid']
                       else self.registry.merge(self.registry.get_product(card['pid'])))
            for sid, override in card.get('services', {}).items():
                product.services[sid].ha_mapping = self.registry._merge_ha_mapping(product.services[sid].ha_mapping, override)
            yield card, product

    def role_domains(self, row):
        if row['kind'] == 'light':
            return {'main': 'light'}
        result = {}
        cards = self.cards(row)
        selected = row.get('cards', [card['key'] for card in cards])
        if not selected or set(selected) - {card['key'] for card in cards}:
            raise ValueError('select supported cards')
        for card, product in self.card_products(row):
            if card['key'] not in selected:
                continue
            if card.get('primary') and row['id'] in self.old_sources:
                result[card.get('primary_role', 'main')] = 'light'
                continue
            for service in product.services.values():
                mapping = service.ha_mapping
                if mapping.action == 'aggregate':
                    continue
                if mapping.role in result and result[mapping.role] != mapping.domain:
                    raise ValueError('role domains disagree')
                result[mapping.role] = mapping.domain
                for timer in timers_for_mapping(mapping).values():
                    result.update({timer.enable: 'switch', timer.duration: 'number', timer.remaining: 'sensor'})
        return result

    def candidates(self, document):
        known = self.old_sources | {row['id'] for row in document['devices']}
        result = {}
        for device in self.device_registry.devices.values():
            try:
                _, entities, info = self.info(device.id)
                product, _ = self.matcher.match_with_detail(info, include_compositions=True)
                if device.id not in known and not (any(x.startswith('light.') for x in entities)
                        or product is not None and product.composition in self.registry.bundles):
                    continue
                result[device.id] = info.name + (f' · {info.model}' if info.model else '')
            except ValueError:
                continue
        # 来源被删除时仍允许取消勾选；不能因此卡死其他设备管理。
        for source in known - set(result):
            result[source] = '不可用设备（可停用）'
        counts = Counter(result.values())
        return {source: name + (f' · {source[-6:]}' if counts[name] > 1 else '')
                for source, name in result.items()}

    def new_recipe(self, source_id):
        _, entities, info = self.info(source_id)
        product, _ = self.matcher.match_with_detail(info, include_compositions=True)
        composed = product is not None and product.composition in self.registry.bundles
        row = {'id': source_id, 'name': info.name, 'model': info.model, 'manufacturer': info.manufacturer,
               'kind': 'composition' if composed else 'light', 'roles': {}}
        if composed:
            row['roles'] = {f'candidate_{i}': entity for i, entity in enumerate(sorted(entities))}
            row['key_prefix'] = self.source_prefix(source_id)
            try:
                apply_layout(self.registry, product, 'switches')
                row['layout'] = 'switches' if '2151' in self.spec.settings['profiles'] else 'original'
            except ValueError:
                row['layout'] = 'original'
        return row

    def validate_roles(self, row):
        _, owned, info = self.info(row['id'])
        # 新配置只接受当前注册表元数据；不能伪造型号绕过产品匹配。
        if row.get('model', '') != info.model:
            raise ValueError('device model changed; select the device again')
        roles = RoleBindings(row['id'], row['roles'], owned)
        required = self.role_domains(row)
        if set(roles.roles) != set(required):
            raise ValueError('complete role bindings required')
        for role, domain in required.items():
            roles.entity(role, domain)
        if row['id'] in self.old_sources and 'main' in roles.roles:
            main = {entity for old in self.spec.bindings['devices'] if old['source_id'] == row['id']
                    for entity in old['entities'].values()}
            if roles.roles['main'] not in main:
                raise ValueError('preserved primary light cannot be rebound')
        if row['kind'] == 'light':
            state = self.hass.states.get(roles.entity('main'))
            if state is None or not {'brightness', 'color_temp_kelvin'} <= set(state.attributes):
                raise ValueError('a standard brightness and color-temperature light is required')
        else:
            states = {entity: self.hass.states.get(entity) for entity in roles.roles.values()}
            for card, product in self.card_products(row):
                if card['key'] not in row.get('cards', [c['key'] for c in self.cards(row)]):
                    continue
                if card.get('primary') and row['id'] in self.old_sources:
                    continue
                if card['pid'] not in self.spec.settings['profiles']:
                    raise ValueError('verified product profile is not installed')
                for service in product.services.values():
                    mapping = service.ha_mapping
                    if mapping.action == 'aggregate':
                        continue
                    state = states[roles.entity(mapping.role)]
                    if mapping.domain == 'fan' and mapping.action in ('set_speed', 'set_direction', 'set_fan_mode'):
                        if state is None or mapping.value_attr not in state.attributes:
                            raise ValueError('the fan does not advertise the selected capability')
                    effect = mapping.options.get('effect')
                    if effect:
                        if state is None or effect not in state.attributes.get('effect_list', []):
                            raise ValueError('the light does not advertise this effect')
        return roles

    async def preview(self, document, entry_id):
        from .gateway_router import GatewayRouter
        document = self.document(document)
        for row in document['devices']:
            if row['id'] in document['enabled']:
                self.validate_roles(row)
        router = await GatewayRouter.create(self.hass, self.spec, entry_id, document)
        if len(router.catalog) > 64:
            raise ValueError('the gateway supports at most 64 cards')
        notes = []
        for row in document['devices']:
            if row['id'] in document['enabled'] and row['kind'] == 'composition':
                notes.extend(template_notes([c for c in self.cards(row)
                    if c['key'] in row.get('cards', [c['key'] for c in self.cards(row)])]))
        return router, list(dict.fromkeys(notes))


def prepare_runtime(spec, catalog):
    """独占所有者持锁后准备活动目录；原冻结目录和账号 KV 不复制、不重建。"""
    import os
    from .gateway_lifecycle import save_state
    from .openharmony.deployment import private_path
    keys = [row['key'] for row in catalog]
    if (len(keys) > 64 or len(set(keys)) != len(keys)
            or any(not isinstance(key, str) or not re.fullmatch(r'[a-z0-9-]{1,31}', key) for key in keys)):
        raise ValueError('unique bounded runtime card keys required')
    active = spec.runtime / 'managed'
    pending = []
    for row in catalog:
        filename = 'ha-child-' + row['key'] + '.json'
        old, current = spec.runtime / filename, active / filename
        source = current if current.exists() else old
        if source.exists():
            private_path(source)
            value = json.loads(source.read_text())
            if value['sn'] != row['sn'] or value['parent_sn'] != spec.root['sn']:
                raise ValueError('existing active child identity differs')
            if not current.exists():
                pending.append((current, value))
    active.mkdir(mode=0o700, exist_ok=True)
    private_path(active, True)
    target = spec.runtime / 'sdk-state'
    private_path(target, True)
    link = active / 'sdk-state'
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise ValueError('SDK state link targets another identity')
    elif link.exists():
        raise ValueError('managed SDK state must reference original paired state')
    else:
        os.symlink('../sdk-state', link)
    for path, value in pending:
        save_state(path, value)
    save_state(active / 'ha-children.local.json', catalog)
    return active


def retained_cards(spec, active, registry):
    """仅保留有完整本地身份的旧子卡，并复核公开产品和确定性 SN。"""
    from .identity_store import private_read
    previous = spec.runtime / 'managed/ha-children.local.json'
    rows = list(spec.catalog)
    if previous.exists():
        rows.extend(json.loads(private_read(previous)))
    known = {row['key']: row for row in active}
    original = {row['key']: row for row in spec.catalog}
    result = []
    for row in rows:
        key = row['key']
        if key in known:
            continue
        if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9-]{1,31}', key):
            raise ValueError('invalid retained card key')
        if not child_is_bound(spec, row):
            continue
        if key in original:
            if row != original[key]:
                raise ValueError('previous catalog changed an original card')
        else:
            definition = registry.definitions.get(row['pid'])
            if definition is None or row['pid'] not in spec.settings['profiles']:
                raise ValueError('retained product has no verified profile')
            if row.get('sn') != managed_child_sn(spec.root['sn'], key):
                raise ValueError('retained card changed its identity')
            cloud = definition['cloud']
            if any(row.get(name) != cloud[name] for name in ('devType', 'manu', 'model', 'protType')):
                raise ValueError('retained card metadata differs')
            if (not row.get('services') or any(sid not in definition['services']
                    or value != definition['services'][sid].get('cloud_service_type', definition['services'][sid]['service_type'])
                    for sid, value in row['services'].items())):
                raise ValueError('retained services differ from verified definitions')
        result.append(row)
        known[key] = row
    return result


def child_is_bound(spec, row):
    """未曾配对的停用候选不送入 SDK，避免生成无用云端卡片。"""
    from .identity_store import private_read
    for directory in (spec.runtime / 'managed', spec.runtime):
        path = directory / ('ha-child-' + row['key'] + '.json')
        if path.exists():
            state = json.loads(private_read(path))
            if state['sn'] != row['sn'] or state['parent_sn'] != spec.root['sn']:
                raise ValueError('disabled child identity differs')
            return bool(state.get('devId'))
    return False
