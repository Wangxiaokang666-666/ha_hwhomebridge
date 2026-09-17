"""Constants for hwhomebridge integration."""

DOMAIN = "hwhomebridge"

# 来自这些集成（platform）的实体不做映射，避免循环接入。
# 例如社区插件 ha-huawei-smarthome（domain: huawei_smarthome）将华为设备接入 HA，
# 若再次桥接回华为会导致设备循环。
SKIP_PLATFORMS = {"huawei_smarthome"}
