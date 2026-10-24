"""当前路由的只读诊断；不导入旧库、不调用原生接口、不返回配对资料。"""
from datetime import datetime, timezone
import os
import sys

from .const import DOMAIN, configured_backend


async def async_get_config_entry_diagnostics(hass, entry):
    current = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    runtime = current.get('runtime')
    manager = None
    source = 'unavailable'
    online_callback = None
    if runtime is not None:
        router = getattr(runtime, 'router', None)
        manager = getattr(router, 'manager', None)
        source = 'managed_router'
    elif current.get('backend') == 'legacy':
        # 只读取已经加载的模块；诊断本身绝不能加载或初始化SDK。
        resident = sys.modules.get(__package__ + '.hwbridge')
        router = getattr(resident, 'service_router', None)
        manager = getattr(router, 'sn_manager', None)
        if manager is not None:
            source = 'resident_legacy_router'
            online_callback = bool(getattr(resident, 'bridge_work', False))
    devices = []
    if manager is not None:
        for device in manager.get_all_devices():
            devices.append({
                'source_device_id': device.ha_device_id,
                'framework_pid': device.product.pid,
                'services': {sid: service.entity_id
                             for sid, service in device.service_entries.items()},
            })
    return {
        'observed_at_utc': datetime.now(timezone.utc).isoformat(),
        'HA_process_pid': os.getpid(),
        'selected_backend': configured_backend(entry),
        'active_backend': current.get('backend'),
        'binding_source': source,
        'runtime_bindings_available': manager is not None,
        'legacy_cloud_online_callback': online_callback,
        'devices': sorted(devices, key=lambda row: row['source_device_id']),
    }
