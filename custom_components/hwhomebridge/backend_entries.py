"""条目身份和资源冲突检查；不启动 SDK，也不清理任何运行目录。"""
from pathlib import Path
import hashlib

from .const import DOMAIN, BACKEND_OPENHARMONY, CONF_OH_DIRECTORY, backend_type


def openharmony_unique_id(gateway_sn):
    return f"{DOMAIN}:openharmony:" + hashlib.sha256(gateway_sn.encode()).hexdigest()


def check_openharmony_conflicts(hass, spec, entry_id=None):
    identity = openharmony_unique_id(spec.gateway_sn)
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.entry_id == entry_id:
            continue
        if entry.data.get('gateway_sn_identity') == identity:
            raise ValueError('gateway identity already owned by the original managed entry')
        active = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get('runtime')
        if active and hasattr(active, 'journal_path'):
            other = active.deployment
            if other.port == spec.port or other.lan_port == spec.lan_port:
                raise ValueError('gateway port already active')
            if active.router.entities & spec.router.write_entities:
                raise ValueError('writable entity already owned by the original managed entry')
        try:
            if backend_type(entry) != BACKEND_OPENHARMONY:
                continue
        except ValueError:
            continue
        if entry.unique_id == identity:
            raise ValueError("OpenHarmony gateway identity already configured")
        directory = entry.options.get(CONF_OH_DIRECTORY, entry.data.get(CONF_OH_DIRECTORY))
        if directory and (Path(hass.config.config_dir) / directory).resolve() == spec.directory:
            raise ValueError("OpenHarmony directory already configured")
        current = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("runtime")
        if current:
            other = current.deployment
            if other.gateway_sn == spec.gateway_sn or other.runtime == spec.runtime:
                raise ValueError("OpenHarmony identity or runtime already active")
            if other.router.write_entities & spec.router.write_entities:
                raise ValueError("OpenHarmony writable entity already owned by another instance")
            if other.port == spec.port or other.lan_port == spec.lan_port:
                raise ValueError("OpenHarmony port already active")
    return identity
