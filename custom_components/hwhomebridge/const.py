"""Constants for hwhomebridge integration."""

DOMAIN = "hwhomebridge"

CONF_BACKEND = "backend"
CONF_OH_DIRECTORY = "openharmony_directory"
CONF_BACKEND_TYPE = "backend_type"
CONF_GATEWAY_DIRECTORY = "gateway_directory"
CONF_GATEWAY_REVISION = "gateway_revision"
BACKEND_LEGACY = "legacy"
BACKEND_OPENHARMONY = "openharmony"
BACKEND_DISABLED = "disabled"
BACKENDS = (BACKEND_LEGACY, BACKEND_OPENHARMONY, BACKEND_DISABLED)


def configured_backend(entry):
    """旧配置没有字段时保持原后端；未知值不自动回退。"""
    backend = entry.options.get(CONF_BACKEND, entry.data.get(CONF_BACKEND, BACKEND_LEGACY))
    if backend not in BACKENDS:
        raise ValueError("unknown bridge backend")
    return backend


def backend_type(entry):
    """条目类型固定；兼容尚未写入类型的旧配置和单后端原型。"""
    kind = entry.data.get(CONF_BACKEND_TYPE)
    if kind is None and (CONF_GATEWAY_DIRECTORY in entry.options or CONF_GATEWAY_DIRECTORY in entry.data):
        return BACKEND_LEGACY
    if kind is None:
        selected = configured_backend(entry)
        has_openharmony_config = (
            selected == BACKEND_OPENHARMONY
            or entry.data.get(CONF_BACKEND) == BACKEND_OPENHARMONY
            or CONF_OH_DIRECTORY in entry.options
            or CONF_OH_DIRECTORY in entry.data
        )
        kind = BACKEND_OPENHARMONY if has_openharmony_config else BACKEND_LEGACY
    if kind not in (BACKEND_LEGACY, BACKEND_OPENHARMONY):
        raise ValueError("unknown bridge entry type")
    return kind


def legacy_entry_id(hass):
    """旧 SDK 的设备注册始终归旧条目，不依赖多条目的加载顺序。"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        try:
            if backend_type(entry) == BACKEND_LEGACY:
                return entry.entry_id
        except ValueError:
            continue
    return None


# 来自这些集成（platform）的实体不做映射，避免循环接入。
# 例如社区插件 ha-huawei-smarthome（domain: huawei_smarthome）将华为设备接入 HA，
# 若再次桥接回华为会导致设备循环。
SKIP_PLATFORMS = {"huawei_smarthome"}


def managed_directory(entry):
    return entry.options.get(CONF_GATEWAY_DIRECTORY, entry.data.get(CONF_GATEWAY_DIRECTORY))
