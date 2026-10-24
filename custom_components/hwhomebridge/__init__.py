"""hwhomebridge integration entry point."""

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (DOMAIN, BACKEND_LEGACY, BACKEND_OPENHARMONY, BACKEND_DISABLED,
                    CONF_OH_DIRECTORY, CONF_BACKEND_TYPE, CONF_GATEWAY_DIRECTORY, CONF_GATEWAY_REVISION, configured_backend, backend_type, legacy_entry_id)

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict):
    """准备组件；配置流确认后才启动选定后端，保留显式 YAML 旧入口。"""
    _LOGGER.info("Preparing hwhomebridge integration")
    hass.data.setdefault(DOMAIN, {})
    # 配置流加载组件时不提前启动旧库；显式 YAML 入口保留旧行为。
    if DOMAIN in config and legacy_entry_id(hass) is None:
        await _start_legacy_once(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """各条目只拥有自己的后端；旧 SDK 常驻不阻止新增独立 C 实例。"""
    data = hass.data.setdefault(DOMAIN, {})
    lock = data.setdefault("setup_lock", asyncio.Lock())
    async with lock:
        runtime = None
        try:
            kind, mode = backend_type(entry), configured_backend(entry)
            managed = entry.options.get(CONF_GATEWAY_DIRECTORY, entry.data.get(CONF_GATEWAY_DIRECTORY))
            if not managed and mode not in (kind, BACKEND_DISABLED):
                raise ValueError("entry backend type is fixed; add a separate entry")
            identity = entry.unique_id
            if managed:
                if kind != BACKEND_LEGACY:
                    raise ValueError('adoption must retain the original legacy entry')
                if data.get('legacy_attempted'):
                    raise RuntimeError('resident legacy SDK requires HA restart before managed adoption')
                if mode != BACKEND_DISABLED:
                    from .gateway_deployment import load_gateway
                    from .gateway_lifecycle import GatewayLifecycle
                    spec = await hass.async_add_executor_job(load_gateway, hass.config.config_dir, managed, entry.unique_id)
                    from .backend_entries import openharmony_unique_id
                    sn_identity = openharmony_unique_id(spec.root['sn'])
                    for other in hass.config_entries.async_entries(DOMAIN):
                        if other.entry_id == entry.entry_id:
                            continue
                        directory = other.options.get(CONF_GATEWAY_DIRECTORY, other.data.get(CONF_GATEWAY_DIRECTORY))
                        if (directory == managed or other.data.get('gateway_identity') == spec.identity
                                or other.unique_id == sn_identity or other.data.get('gateway_sn_identity') == sn_identity):
                            raise ValueError('gateway identity/directory already owned by another entry')
                        active = data.get(other.entry_id, {}).get('runtime')
                        if active and (active.deployment.port == spec.port or active.deployment.lan_port == spec.lan_port):
                            raise ValueError('gateway ports already owned by another entry')
                    runtime = GatewayLifecycle(hass, spec, entry.entry_id)
                    runtime.relative_directory = managed
                    await runtime.start(mode, entry.options.get(CONF_GATEWAY_REVISION,
                        entry.data.get(CONF_GATEWAY_REVISION, 'initial')), entry.options.get('gateway_devices'))
                    mode = runtime.active
                    for device in runtime.router.manager.get_all_devices():
                        source = dr.async_get(hass).async_get(device.ha_device_id)
                        dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id,
                            identifiers={(DOMAIN, entry.entry_id, device.sn)},
                            name=(source.name_by_user or source.name) if source else device.product.name,
                            model=device.product.name)
                    hass.config_entries.async_update_entry(entry, data={**entry.data,
                        'gateway_identity': spec.identity, 'gateway_sn_identity': sn_identity})
            elif kind == BACKEND_OPENHARMONY:
                from .backend_entries import check_openharmony_conflicts
                from .openharmony.deployment import load_deployment
                from .openharmony.ha_backend import HAOpenHarmonyBackend
                directory = entry.options.get(CONF_OH_DIRECTORY, entry.data.get(CONF_OH_DIRECTORY, ""))
                # 停用时不要求运行目录可读；已保存身份和目录仍防止重复创建。
                if mode == BACKEND_OPENHARMONY:
                    spec = await hass.async_add_executor_job(load_deployment, hass.config.config_dir, directory)
                    identity = check_openharmony_conflicts(hass, spec, entry.entry_id)
                    runtime = HAOpenHarmonyBackend(hass, spec)
                    runtime.relative_directory = directory
                    await runtime.start()
            elif legacy_entry_id(hass) != entry.entry_id:
                raise ValueError("only one legacy SDK entry is allowed")
            if kind == BACKEND_LEGACY:
                identity = DOMAIN
            if not managed:
                hass.config_entries.async_update_entry(entry,
                    data={**entry.data, CONF_BACKEND_TYPE: kind}, unique_id=identity)
            if mode == BACKEND_LEGACY and not managed:
                await _start_legacy_once(hass)
                await _setup_legacy_entry(hass, entry)
        except (OSError, ValueError, KeyError, RuntimeError, TimeoutError) as error:
            if runtime is not None:
                await runtime.stop()
            raise ConfigEntryNotReady("Bridge not ready; check entry identity, isolated directory, entities and ports") from error
        data[entry.entry_id] = {"backend": mode, "runtime": runtime}
        entry.async_on_unload(entry.add_update_listener(_backend_options_updated))
        if runtime:
            async def stop_on_shutdown(_event):
                await runtime.stop()
            entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_on_shutdown))
            if not managed:
                runtime.watch(entry.entry_id)
    return True


async def _start_legacy_once(hass):
    data = hass.data.setdefault(DOMAIN, {})
    if not data.get("legacy_attempted"):
        data["legacy_attempted"] = True
        from .hwbridge import start_hw_hilink_bridge
        await start_hw_hilink_bridge(hass)


async def _backend_options_updated(hass, entry):
    data = hass.data[DOMAIN]
    current = data.get(entry.entry_id)
    desired = configured_backend(entry)
    managed = entry.options.get(CONF_GATEWAY_DIRECTORY, entry.data.get(CONF_GATEWAY_DIRECTORY))
    if (backend_type(entry) == BACKEND_LEGACY and data.get("legacy_attempted")
            and (managed or desired != BACKEND_LEGACY)):
        from homeassistant.components import persistent_notification
        persistent_notification.async_create(hass,
            "后端选择已保存。旧后端无法在当前进程中完全卸载，请重启 Home Assistant 后应用新的选择。",
            title="HuaweiHome Bridge：需要重启", notification_id="hwhomebridge_backend_restart")
        return
    if current is None:
        if entry.disabled_by is None:
            await hass.config_entries.async_reload(entry.entry_id)
        return
    if managed:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    directory = entry.options.get(CONF_OH_DIRECTORY, entry.data.get(CONF_OH_DIRECTORY))
    if desired != current["backend"] or (desired == BACKEND_OPENHARMONY and directory != current["runtime"].relative_directory):
        await hass.config_entries.async_reload(entry.entry_id)


async def _setup_legacy_entry(hass, entry):
    from .hwbridge import service_router, _create_bridge_device_entry_async, register_hilink_callbacks

    # 重载集成时 async_unload_entry 已 nullify 回调，这里重新注册
    register_hilink_callbacks()

    dev_reg = dr.async_get(hass)

    # 为在 async_setup_entry 之前已注册的设备补建 HA 设备注册表条目
    if service_router is not None:
        for vd in service_router.sn_manager.get_all_devices():
            device_entry = dev_reg.async_get(vd.ha_device_id)
            name = ""
            model = ""
            manufacturer = ""
            if device_entry:
                name = device_entry.name_by_user or device_entry.name or ""
                model = device_entry.model or ""
                manufacturer = device_entry.manufacturer or ""
            _create_bridge_device_entry_async(vd, name, model, manufacturer)

    _LOGGER.info("hwhomebridge config entry setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """停止自有 OpenHarmony 子进程，或按原方式置空旧库回调。"""
    data = hass.data.setdefault(DOMAIN, {})
    active = data.get(entry.entry_id)
    if active and active.get("runtime") is not None:
        await active["runtime"].stop()
    elif active and active["backend"] == BACKEND_LEGACY:
        from .hwbridge import nullify_hilink_callbacks
        nullify_hilink_callbacks()
    data.pop(entry.entry_id, None)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """集成被永久删除时调用：删除网关、清理持久化文件

    HA 删除流程：先 async_unload_entry（移除实体状态，C 库仍存活），
    再 async_remove_entry（此时 C 库可用，exit_brg 可正常调用）。
    """
    # OpenHarmony 删除配置只停止自有进程，绝不解绑或清理配对目录。
    if (backend_type(entry) != BACKEND_LEGACY
            or entry.options.get(CONF_GATEWAY_DIRECTORY, entry.data.get(CONF_GATEWAY_DIRECTORY))):
        return
    from .hwbridge import stop_hw_hilink_bridge

    # 根据语言选择通知文案
    lang = hass.config.language or "en"
    if lang.startswith("zh"):
        notif_title = "请重启 Home Assistant"
        notif_message = (
            "HuaweiHome Bridge 已删除，如需重新添加本集成，请先重启 Home Assistant。"
            "否则本集成将无法重新正常工作。"
        )
    else:
        notif_title = "Please restart Home Assistant"
        notif_message = (
            "HuaweiHome Bridge has been removed. If you want to re-add this integration, "
            "please restart Home Assistant first, otherwise the integration will not work properly."
        )

    # 先创建重启通知（在 exit_brg 之前，防止 SDK 退出导致进程崩溃后通知丢失）
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": notif_title,
                "message": notif_message,
                "notification_id": "hwhomebridge_restart_required",
            },
            blocking=True,
        )
        _LOGGER.info("Restart notification created successfully")
    except Exception as e:
        _LOGGER.error(f"Failed to create restart notification: {e}")
    finally:
        # 确保无论通知是否成功，都执行清理
        await hass.async_add_executor_job(stop_hw_hilink_bridge, hass)
        _LOGGER.info("hwhomebridge integration removed, gateway cleaned up from cloud")
