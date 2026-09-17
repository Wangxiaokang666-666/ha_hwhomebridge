"""hwhomebridge integration entry point."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .hwbridge import start_hw_hilink_bridge

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict):
    """Set up the integration package.

    The bridge belongs to the config entry lifecycle and is started from
    ``async_setup_entry``. Starting it here makes Home Assistant's config
    entry state independent from the actual bridge initialization.
    """
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up hwhomebridge from a config entry."""
    if not hass.data.get("hwhomebridge_bridge_started"):
        _LOGGER.info("Starting hwhomebridge for config entry %s", entry.entry_id)
        await start_hw_hilink_bridge(hass)
        hass.data["hwhomebridge_bridge_started"] = True
    _LOGGER.info("hwhomebridge config entry setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """集成被永久删除时调用：删除网关、清理持久化文件

    HA 删除流程：先 async_unload_entry（移除实体状态，C 库仍存活），
    再 async_remove_entry（此时 C 库可用，exit_brg 可正常调用）。
    """
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
