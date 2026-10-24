"""受管网关的 HA 原生表单；所有配置先预览校验，再原子保存到同一条目。"""
import copy
import secrets

from homeassistant.helpers import config_validation as cv, selector

from .const import CONF_GATEWAY_REVISION, managed_directory
from .gateway_management import GatewayManagement, OPTION, revision
from .card_layouts import apply_layout


class GatewayManagementFlow:
    def _current_management_revision(self):
        return revision([managed_directory(self.config_entry), self.config_entry.unique_id,
                         self.config_entry.options.get(OPTION)])

    async def _management_begin(self):
        if getattr(self, '_management', None) is None:
            self._management = await GatewayManagement.create(self.hass, self.config_entry)
            self._management_revision = self._current_management_revision()
            self._management_document = self._management.document(self.config_entry.options.get(OPTION))

    async def async_step_managed_devices(self, user_input=None):
        from .config_flow import vol
        await self._management_begin()
        if self._management.spec.settings.get('managed_catalog_protocol') != 1:
            return self.async_abort(reason='managed_update_required')
        errors = {}
        candidates = self._management.candidates(self._management_document)
        if user_input is not None:
            selected = user_input.get('devices', [])
            try:
                if not isinstance(selected, list) or set(selected) - set(candidates) or len(set(selected)) != len(selected):
                    raise ValueError('select listed source devices')
                self._management_document['enabled'] = selected
                configured = self._management.old_sources | {row['id'] for row in self._management_document['devices']}
                self._management_pending = sorted(set(selected) - configured)
                if self._management_pending:
                    return await self._management_add_next()
                return await self.async_step_managed_confirm()
            except (ValueError, KeyError, TypeError):
                errors['base'] = 'invalid_managed_devices'
        return self.async_show_form(step_id='managed_devices', errors=errors,
            description_placeholders={'device_count': str(len(candidates))},
            data_schema=vol.Schema({vol.Optional('devices', default=self._management_document['enabled']): cv.multi_select(candidates)}))

    async def _management_add_next(self):
        source = self._management_pending.pop(0)
        self._management_editing = self._management.new_recipe(source)
        if self._management_editing['kind'] == 'composition':
            return await self.async_step_managed_layout()
        return await self.async_step_managed_roles()

    async def async_step_managed_role_device(self, user_input=None):
        from .config_flow import vol
        await self._management_begin()
        if self._management.spec.settings.get('managed_catalog_protocol') != 1:
            return self.async_abort(reason='managed_update_required')
        recipes = self._management_document['devices']
        names = self._management.candidates(self._management_document)
        choices = {row['id']: names.get(row['id'], row.get('name', row['id'])) for row in recipes}
        if not choices:
            return self.async_abort(reason='no_managed_roles')
        if user_input is not None:
            if user_input.get('device') not in choices:
                return self.async_abort(reason='invalid_managed_device')
            self._management_editing = copy.deepcopy(next(row for row in recipes if row['id'] == user_input['device']))
            self._management_pending = []
            if self._management_editing['kind'] == 'composition':
                return await self.async_step_managed_layout()
            return await self.async_step_managed_roles()
        return self.async_show_form(step_id='managed_role_device', data_schema=vol.Schema({
            vol.Required('device'): vol.In(choices)}))

    async def async_step_managed_layout(self, user_input=None):
        from .config_flow import vol
        row = self._management_editing
        choices = {'original': '保留原模板'}
        original = {**row, 'layout': 'original'}
        try:
            apply_layout(self._management.registry, self._management.product(original), 'switches')
            if '2151' in self._management.spec.settings['profiles']:
                choices['switches'] = '六档开关卡（不显示七／八档和摇头）'
        except ValueError:
            pass
        if user_input is not None:
            layout = user_input.get('layout')
            if layout not in choices:
                return self.async_abort(reason='invalid_managed_device')
            if layout != row.get('layout', 'original'):
                row.pop('cards', None)
            row['layout'] = layout
            return await self.async_step_managed_cards()
        return self.async_show_form(step_id='managed_layout', data_schema=vol.Schema({
            vol.Required('layout', default=row.get('layout', 'original')): vol.In(choices)}))

    async def async_step_managed_cards(self, user_input=None):
        from .config_flow import vol
        row = self._management_editing
        cards = self._management.cards(row)
        names = {'fan-wifi': '风扇（原厂模板）', 'fan-controls-0': '风速一至四档',
                 'fan-controls-1': '风速五、六档／自然风／电源', 'light': '主灯（沿用原卡）',
                 'aux': '正反转与氛围灯', 'light-options': '灯光设置', 'timer-controls': '完整原生定时（兼容模板）'}
        options = {}
        for card in cards:
            key = card['key']
            if card.get('primary') and row['id'] in self._management.old_sources:
                options[key] = '原主灯（保留原卡与绑定）'
            elif card['pid'] in self._management.spec.settings['profiles']:
                labels = [value.get('options', {}).get('label') for value in card.get('services', {}).values()]
                labels = [label for label in labels if label]
                options[key] = names.get(key, '／'.join(labels) or key)
        errors = {}
        if user_input is not None:
            selected = user_input.get('cards', [])
            if not selected or set(selected) - set(options) or len(set(selected)) != len(selected):
                errors['base'] = 'invalid_managed_cards'
            else:
                row['cards'] = selected
                return await self.async_step_managed_roles()
        return self.async_show_form(step_id='managed_cards', errors=errors,
            data_schema=vol.Schema({vol.Required('cards', default=row.get('cards', list(options))): cv.multi_select(options)}))

    async def async_step_managed_roles(self, user_input=None):
        from .config_flow import vol
        row = self._management_editing
        required = self._management.role_domains(row)
        errors = {}
        try:
            _, owned, _ = self._management.info(row['id'])
        except ValueError:
            return self.async_abort(reason='managed_source_unavailable')
        if user_input is not None:
            try:
                candidate = {**row, 'roles': {role: user_input[role] for role in required}}
                self._management.validate_roles(candidate)
                recipes = self._management_document['devices']
                recipes[:] = [item for item in recipes if item['id'] != row['id']] + [candidate]
                if self._management_pending:
                    return await self._management_add_next()
                return await self.async_step_managed_confirm()
            except (ValueError, KeyError, TypeError):
                errors['base'] = 'invalid_managed_roles'
        fields = {}
        for role, domain in required.items():
            candidates = sorted(entity for entity in owned if entity.startswith(domain + '.'))
            default = row['roles'].get(role)
            if default not in candidates:
                default = candidates[0] if len(candidates) == 1 else None
            key = vol.Required(role, default=default) if default is not None else vol.Required(role)
            fields[key] = selector.EntitySelector({'include_entities': candidates, 'domain': domain})
            if not candidates:
                errors['base'] = 'managed_role_missing'
        return self.async_show_form(step_id='managed_roles', errors=errors, data_schema=vol.Schema(fields))

    async def async_step_managed_confirm(self, user_input=None):
        from .config_flow import vol
        errors = {}
        try:
            if self._current_management_revision() != self._management_revision:
                raise ValueError('configuration changed in another flow')
            router, notes = await self._management.preview(self._management_document, self.config_entry.entry_id)
            if user_input is not None:
                if user_input.get('confirm') is not True:
                    errors['base'] = 'managed_confirm_required'
                else:
                    return self._backend_result({**self.config_entry.options, OPTION: self._management_document,
                                                CONF_GATEWAY_REVISION: secrets.token_hex(8)})
        except (ValueError, KeyError, TypeError, OSError):
            return self.async_show_form(step_id='managed_confirm', errors={'base': 'invalid_managed_plan'},
                data_schema=vol.Schema({}))
        return self.async_show_form(step_id='managed_confirm', errors=errors,
            description_placeholders={'device_count': str(len(self._management_document['enabled'])),
                                      'card_count': str(len(router.catalog)), 'notes': '\n\n'.join(notes) or '没有额外模板限制。'},
            data_schema=vol.Schema({vol.Required('confirm', default=False): bool}))
