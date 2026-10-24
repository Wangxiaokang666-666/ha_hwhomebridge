"""Config flow for HuaweiHome Bridge

配置流程：
1. 用户在HA设置中点击"添加集成" → 搜索"hwhomebridge"
2. 点击进入配置页面 → 显示提示页面（无PIN码）
3. 用户勾选确认后点击"提交" → 完成集成配置，启动网关
4. 用户再次点击"配置"按钮 → 选择功能：PIN绑定 或 设备管理
5a. PIN绑定 → 显示PIN码 → 在APP上绑定
5b. 设备管理 → 显示已接入设备列表 → 勾选/取消勾选设备
"""

import asyncio
import logging
import time
import secrets
try:
    import probatio as vol
except ImportError:  # 兼容较早 HA 版本。
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .const import (DOMAIN, CONF_BACKEND, CONF_OH_DIRECTORY, BACKEND_LEGACY,
                    BACKEND_OPENHARMONY, BACKEND_DISABLED, CONF_BACKEND_TYPE, CONF_GATEWAY_DIRECTORY, CONF_GATEWAY_REVISION,
                    configured_backend, backend_type, legacy_entry_id, managed_directory)
from .pin_manager import PINManager
from .gateway_management_flow import GatewayManagementFlow

_LOGGER = logging.getLogger(__name__)


def backend_schema(default, kind=None):
    choices = {BACKEND_LEGACY: "原有 HiLink", BACKEND_OPENHARMONY: "OpenHarmony 独立后端", "existing_gateway": "沿用已配对网关（进程托管）"}
    if kind is not None:
        choices = {kind: choices[kind], BACKEND_DISABLED: "停用"}
    else:
        # 正式新建入口只创建一个网关；旧实验条目仍可在原配置页维护。
        choices.pop(BACKEND_OPENHARMONY)
    return vol.Schema({vol.Required(CONF_BACKEND, default=default): vol.In(choices)})


async def validate_directory(hass, directory, entry_id=None):
    from .backend_entries import check_openharmony_conflicts
    from .openharmony.deployment import load_deployment
    spec = await hass.async_add_executor_job(load_deployment, hass.config.config_dir, directory)
    return check_openharmony_conflicts(hass, spec, entry_id)


def directory_schema(default=".storage/hwhomebridge/openharmony/lab"):
    return vol.Schema({vol.Required(CONF_OH_DIRECTORY, default=default): str})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for HuaweiHome Bridge.
    
    配置流程处理器：
    - 首次配置：显示提示页面，完成后启动网关
    - 后续配置：通过OptionsFlow显示PIN码或设备管理
    """
    
    VERSION = 1
    
    async def async_step_user(self, user_input=None):
        """第一步：显示欢迎页面
        
        用户首次添加集成时显示功能说明和使用步骤，
        用户点击"提交"后完成配置，网关自动启动。
        """
        
        # 用户点击"提交"，创建配置条目
        if user_input is not None:
            if self.hass.config_entries.async_entries(DOMAIN):
                return self.async_abort(reason='already_configured')
            backend = user_input.get(CONF_BACKEND, BACKEND_LEGACY)
            if backend == "existing_gateway":
                if legacy_entry_id(self.hass) is not None:
                    return self.async_abort(reason="already_configured")
                return await self.async_step_existing_gateway()
            if backend not in (BACKEND_LEGACY, BACKEND_OPENHARMONY):
                return self.async_abort(reason="invalid_backend")
            if backend == BACKEND_OPENHARMONY:
                return await self.async_step_openharmony()
            await self.async_set_unique_id(DOMAIN)
            if legacy_entry_id(self.hass) is not None:
                return self.async_abort(reason="already_configured")
            self._abort_if_unique_id_configured()
            _LOGGER.info("Config flow completed, gateway will start")
            return self.async_create_entry(
                title="Huawei HA Gateway",
                data={CONF_BACKEND: backend, CONF_BACKEND_TYPE: backend}
            )
        
        # 显示欢迎页面（无输入框）
        return self.async_show_form(
            step_id="user",
            data_schema=backend_schema(BACKEND_LEGACY),
        )

    async def async_step_existing_gateway(self, user_input=None):
        from .gateway_deployment import load_gateway
        errors = {}
        if user_input is not None:
            try:
                mode = user_input[CONF_BACKEND]
                if mode not in (BACKEND_LEGACY, BACKEND_OPENHARMONY):
                    raise ValueError('invalid managed engine')
                spec = await self.hass.async_add_executor_job(load_gateway, self.hass.config.config_dir,
                    user_input[CONF_GATEWAY_DIRECTORY], DOMAIN)
                await self.hass.async_add_executor_job(spec.verify_engine, mode)
            except (OSError, ValueError, KeyError):
                errors['base'] = 'invalid_gateway_directory'
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title='Huawei HA Gateway', data={**user_input,
                    CONF_BACKEND_TYPE: BACKEND_LEGACY, CONF_GATEWAY_REVISION: secrets.token_hex(8)})
        return self.async_show_form(step_id='existing_gateway', errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_GATEWAY_DIRECTORY, default='.storage/hwhomebridge/gateways/current'): str,
            vol.Required(CONF_BACKEND, default=BACKEND_LEGACY): vol.In({BACKEND_LEGACY:'HiLink', BACKEND_OPENHARMONY:'OpenHarmony'})}))

    async def async_step_openharmony(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                identity = await validate_directory(self.hass, user_input[CONF_OH_DIRECTORY])
            except (OSError, ValueError, KeyError):
                errors["base"] = "invalid_openharmony_directory"
            else:
                await self.async_set_unique_id(identity)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="OpenHarmony Gateway · 独立实例", data={
                    CONF_BACKEND: BACKEND_OPENHARMONY, CONF_BACKEND_TYPE: BACKEND_OPENHARMONY,
                    CONF_OH_DIRECTORY: user_input[CONF_OH_DIRECTORY]})
        return self.async_show_form(step_id="openharmony", data_schema=directory_schema(), errors=errors)
    
    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """获取选项配置流程"""
        return OptionsFlowHandler()


class OptionsFlowHandler(GatewayManagementFlow, config_entries.OptionsFlow):
    """选项配置流程处理器
    
    已配置后，点击"配置"按钮进入此流程。
    提供两个功能：
    - PIN绑定：显示PIN码用于在华为智慧生活APP上绑定
    - 设备管理：查看和管理已接入的设备
    """
    
    __DEFAULT_TIME_OUT = 300  # 5分钟
    
    def __init__(self):
        self._pin_task: asyncio.Task | None = None
        self._abort_reason = ""

    def _backend_result(self, data):
        # setup 失败时正常的运行期监听器不存在，也要让显式回退立即生效。
        if (data != self.config_entry.options and
            self.config_entry.entry_id not in self.hass.data.get(DOMAIN, {})):
            from . import _backend_options_updated
            async def apply_once(hass, entry):
                unsubscribe()
                await _backend_options_updated(hass, entry)
            unsubscribe = self.config_entry.add_update_listener(apply_once)
        return self.async_create_entry(title="", data=data)
    
    async def async_step_init(self, user_input=None):
        """选项配置入口：选择功能
        
        显示一个选择菜单，让用户选择是进行PIN绑定还是设备管理。
        """
        if user_input is not None:
            action = user_input.get("action")
            if action == "gateway_lifecycle":
                return await self.async_step_gateway_lifecycle()
            if action == "backend":
                return await self.async_step_backend()
            if action == "pin_binding":
                if configured_backend(self.config_entry) != BACKEND_LEGACY or managed_directory(self.config_entry):
                    return self.async_abort(reason="legacy_only")
                return await self.async_step_pin_binding()
            elif action == "device_management":
                if managed_directory(self.config_entry):
                    return await self.async_step_managed_devices()
                if configured_backend(self.config_entry) != BACKEND_LEGACY or managed_directory(self.config_entry):
                    return self.async_abort(reason="legacy_only")
                return await self.async_step_select_device()
            elif action == 'role_binding' and managed_directory(self.config_entry):
                return await self.async_step_managed_role_device()

        actions = {"backend": "桥接后端 / 启用与回退"}
        if backend_type(self.config_entry) == BACKEND_LEGACY:
            actions["gateway_lifecycle"] = "接管当前网关 / 后端切换"
        if configured_backend(self.config_entry) == BACKEND_LEGACY and not managed_directory(self.config_entry):
            actions = {"pin_binding": "PIN绑定", "device_management": "设备管理", **actions}
        elif managed_directory(self.config_entry):
            actions = {'device_management': '设备管理', 'role_binding': '角色绑定与功能卡片', **actions}
        
        # 显示选择菜单
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("action", default=next(iter(actions))): vol.In(actions),
            }),
        )

    async def async_step_gateway_lifecycle(self, user_input=None):
        from .gateway_deployment import load_gateway
        errors = {}
        default = managed_directory(self.config_entry) or '.storage/hwhomebridge/gateways/current'
        if user_input is not None:
            mode = user_input[CONF_BACKEND]
            try:
                if mode not in (BACKEND_LEGACY, BACKEND_OPENHARMONY, BACKEND_DISABLED):
                    raise ValueError('unknown backend')
                if mode != BACKEND_DISABLED:
                    spec = await self.hass.async_add_executor_job(load_gateway, self.hass.config.config_dir,
                        user_input[CONF_GATEWAY_DIRECTORY], self.config_entry.unique_id)
                    await self.hass.async_add_executor_job(spec.verify_engine, mode)
                    existing = self.config_entry.data.get('gateway_identity')
                    if existing and existing != spec.identity:
                        raise ValueError('existing gateway identity cannot change')
            except (OSError, ValueError, KeyError, RuntimeError):
                errors['base'] = 'invalid_gateway_directory'
            else:
                return self._backend_result({**self.config_entry.options, **user_input,
                    CONF_GATEWAY_REVISION: secrets.token_hex(8)})
        current = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        runtime = current.get('runtime')
        return self.async_show_form(step_id='gateway_lifecycle', errors=errors,
            description_placeholders={'active_backend':current.get('backend', BACKEND_DISABLED),
                'selected_backend':configured_backend(self.config_entry),
                'failed_backend':(runtime.state.get('failed_backend') if runtime else None) or '—'},
            data_schema=vol.Schema({
            vol.Required(CONF_GATEWAY_DIRECTORY, default=default): str,
            vol.Required(CONF_BACKEND, default=configured_backend(self.config_entry)): vol.In({
                BACKEND_LEGACY: '原 HiLink SDK', BACKEND_OPENHARMONY: 'OpenHarmony', BACKEND_DISABLED: '停用'})}))

    async def async_step_backend(self, user_input=None):
        if managed_directory(self.config_entry):
            return await self.async_step_gateway_lifecycle(user_input)
        if user_input is not None:
            mode = user_input.get(CONF_BACKEND)
            if mode not in (backend_type(self.config_entry), BACKEND_DISABLED):
                return self.async_abort(reason="invalid_backend")
            if mode == BACKEND_OPENHARMONY:
                return await self.async_step_openharmony()
            return self._backend_result({**self.config_entry.options, CONF_BACKEND: mode})
        selected = configured_backend(self.config_entry)
        current = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        active = current.get("backend", "stopped")
        policy = "原有设备与映射"
        if backend_type(self.config_entry) == BACKEND_OPENHARMONY:
            runtime = current.get("runtime")
            policy = "未运行；启用时按独立目录的写入策略校验"
            if runtime:
                policies = {"fan_only": "风扇及风扇定时可写，灯光只读", "read_only": "全部只读"}
                policy = policies[runtime.deployment.write_policy]
        return self.async_show_form(step_id="backend", data_schema=backend_schema(selected, backend_type(self.config_entry)),
            description_placeholders={"active_backend": active, "selected_backend": selected,
                                      "write_policy": policy})

    async def async_step_openharmony(self, user_input=None):
        errors = {}
        default = self.config_entry.options.get(CONF_OH_DIRECTORY,
                    self.config_entry.data.get(CONF_OH_DIRECTORY, ".storage/hwhomebridge/openharmony/lab"))
        if user_input is not None:
            try:
                identity = await validate_directory(self.hass, user_input[CONF_OH_DIRECTORY], self.config_entry.entry_id)
                if self.config_entry.unique_id not in (None, DOMAIN, identity):
                    raise ValueError("existing gateway identity cannot be changed")
            except (OSError, ValueError, KeyError):
                errors["base"] = "invalid_openharmony_directory"
            else:
                return self._backend_result({**self.config_entry.options,
                    CONF_BACKEND: BACKEND_OPENHARMONY, CONF_OH_DIRECTORY: user_input[CONF_OH_DIRECTORY]})
        return self.async_show_form(step_id="openharmony", data_schema=directory_schema(default), errors=errors)
    
    async def async_step_pin_binding(self, user_input=None):
        """PIN绑定入口：生成PIN码并显示"""
        if configured_backend(self.config_entry) != BACKEND_LEGACY or managed_directory(self.config_entry):
            return self.async_abort(reason="legacy_only")
        pin_code = PINManager.generate_pin()
        _LOGGER.info("Generated PIN for binding")
        
        if self._pin_task is None:
            self._pin_task = self.hass.async_create_task(
                self._wait_for_binding(pin_code)
            )
        
        return await self.async_step_show_pin()
    
    async def async_step_show_pin(self, user_input=None):
        """显示PIN码页面（无提交按钮）"""
        # 检查后台任务状态
        if self._pin_task.done():
            # 任务完成，检查是否有异常
            if err := self._pin_task.exception():
                if isinstance(err, TimeoutError):
                    _LOGGER.warning("PIN expired after timeout")
                    self._abort_reason = "pin_expired"
                else:
                    _LOGGER.error(f"Unexpected error: {err}")
                    self._abort_reason = "internal_error"
            else:
                # 绑定成功
                _LOGGER.info("Binding completed successfully")
                self._abort_reason = ""
            
            # 进入下一步（成功或失败）
            return self.async_show_progress_done(next_step_id="finish")
        
        # 显示PIN码（使用progress，无提交按钮）
        time_out = self.__DEFAULT_TIME_OUT // 60  # 转换为分钟
        return self.async_show_progress(
            step_id="show_pin",
            progress_action="wait_for_binding",
            description_placeholders={
                "pin_code": str(PINManager.get_current_pin() or ""),
                "time_out": time_out,
            },
            progress_task=self._pin_task,
        )
    
    async def async_step_finish(self, user_input=None):
        """完成配置"""
        if self._abort_reason:
            # 超时或错误，中止流程
            return self.async_abort(reason=self._abort_reason)
        
        # 绑定成功，创建配置条目
        return self.async_create_entry(title="", data=dict(self.config_entry.options))
    
    async def async_step_select_device(self, user_input=None):
        """设备管理：显示已接入设备列表，支持添加/移除桥接
        """
        if configured_backend(self.config_entry) != BACKEND_LEGACY or managed_directory(self.config_entry):
            return self.async_abort(reason="legacy_only")
        from .hwbridge import service_router
        
        if service_router is None:
            return self.async_abort(reason="integration_not_init")
        
        all_vds = service_router.sn_manager.get_all_devices()
        
        if not all_vds:
            return self.async_show_form(
                step_id="select_device",
                data_schema=vol.Schema({}),
                description_placeholders={"device_count": "0"},
            )
        
        dev_reg = dr.async_get(self.hass)
        options_dict = {}
        default_selected = []
        
        entry_id = self.config_entry.entry_id
        
        for vd in all_vds:
            name = vd.product.name or vd.product.pid
            orig_device = dev_reg.devices.get(vd.ha_device_id)
            if orig_device:
                name = orig_device.name_by_user or orig_device.name or name
            
            bridge_dev = None
            if entry_id:
                bridge_dev = dev_reg.async_get_device(
                    identifiers={(DOMAIN, entry_id, vd.sn)}
                )
            
            if not name and bridge_dev and bridge_dev.name:
                name = bridge_dev.name
            
            label = f"{name} ({vd.product.pid})"
            options_dict[vd.sn] = label
            
            if bridge_dev is not None:
                default_selected.append(vd.sn)
        
        if user_input is not None:
            selected_sns = set(user_input.get("devices", []))
            
            from .hwbridge import (
                _create_bridge_device_entry_async,
                _remove_bridge_device_entry_async,
                _add_excluded_device,
                _remove_excluded_device,
                _excluded_device_sns,
            )

            for vd in all_vds:
                orig_device = dev_reg.devices.get(vd.ha_device_id)
                name = vd.product.name or vd.product.pid
                model = ""
                manufacturer = ""
                if orig_device:
                    name = orig_device.name_by_user or orig_device.name or name
                    model = orig_device.model or ""
                    manufacturer = orig_device.manufacturer or ""

                if vd.sn not in selected_sns:
                    service_router.register_device_to_hilink(vd.sn, 2)
                    vd.online = False
                    _remove_bridge_device_entry_async(vd.sn)
                    _add_excluded_device(vd.sn)
                    _LOGGER.info(f"Device unregistered via options flow: sn={vd.sn}")
                else:
                    was_excluded = vd.sn in _excluded_device_sns
                    if was_excluded:
                        service_router.register_device_to_hilink(vd.sn, 3)
                        service_router.register_device_to_hilink(vd.sn, 1)
                        vd.online = True
                        _remove_excluded_device(vd.sn)
                        _create_bridge_device_entry_async(vd, name, model, manufacturer, clear_disabled=True)
                        _LOGGER.info(f"Device re-registered via options flow: sn={vd.sn}")
            
            return self.async_create_entry(title="", data=dict(self.config_entry.options))
        
        # 显示设备选择表单
        return self.async_show_form(
            step_id="select_device",
            data_schema=vol.Schema({
                vol.Required("devices", default=default_selected): cv.multi_select(options_dict),
            }),
            description_placeholders={"device_count": str(len(all_vds))},
        )
    
    async def _wait_for_binding(self, pin_code: int):
        """后台任务：等待用户在APP上完成绑定"""
        start_time = time.time()
        
        while True:
            await asyncio.sleep(2)
            
            # 检查是否超时
            if time.time() - start_time > self.__DEFAULT_TIME_OUT:
                raise TimeoutError("PIN expired")
            
            # 检查PIN是否仍然有效
            current_pin = PINManager.get_current_pin()
            if current_pin is None:
                # PIN已被清除
                _LOGGER.warning("PIN was cleared during binding")
                raise TimeoutError("PIN expired")
