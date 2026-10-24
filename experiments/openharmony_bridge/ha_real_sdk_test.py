"""真实闭源 SDK + 真实 OpenHarmony C 共存；仅供隔离、未配对测试环境。

复用 M1 生命周期场景。ctypes errcheck 只记录已完成的真实 C 调用，
不替换 SDK 函数返回值，不伪造网关在线，不使用生产实体或配对资料。
"""
import argparse
import asyncio
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import sys

import ha_coexistence_test as common
from homeassistant.const import __version__ as HA_VERSION

SDK_LAN_PORT = 5683


def owned_udp_ports():
    owned = set()
    for fd in Path('/proc/self/fd').iterdir():
        try:
            owned.add(os.readlink(fd))
        except FileNotFoundError:
            continue
    ports = set()
    for table in ('udp', 'udp6'):
        for line in Path('/proc/net', table).read_text().splitlines()[1:]:
            columns = line.split()
            if f'socket:[{columns[9]}]' in owned:
                ports.add(int(columns[1].split(':')[-1], 16))
    return ports


class NativeSDKTrace:
    evidence = 'real libhilink_bridge.so; ctypes errcheck observation only; unpaired/offline'

    def __init__(self):
        self.starts = self.registrations = self.nullifications = self.removals = 0
        self.callbacks = None
        self.reports = []
        self.library = None
        self.main_result = None

    def observe(self, library):
        self.library = library

        def started(result, _function, _args):
            self.starts += 1
            self.main_result = result
            if result != 0:
                raise RuntimeError('real legacy SDK startup failed')
            return result

        def registered(result, _function, args):
            self.callbacks = args
            if args[0] is None:
                self.nullifications += 1
            else:
                self.registrations += 1
            return result

        def reported(result, _function, args):
            self.reports.append(json.loads(args[2].value))
            return result

        def removed(result, _function, _args):
            self.removals += 1
            return result

        for name, observer in {'main': started, 'RegHomeAssistantPyCB': registered,
                               'UpdateHAStatus': reported, 'exit_brg': removed}.items():
            getattr(library, name).errcheck = observer
        return library

    def verify_identity(self):
        directory = common.CONFIG / '.storage/hwhomebridge/config'
        sn = self.library.GetGatewaySN()
        assert sn, 'real legacy gateway identity missing'
        ac = directory / 'device_ac'
        assert ac.is_file()
        identity = {'gateway_sn_sha256': hashlib.sha256(sn).hexdigest(),
                    'device_ac_sha256': hashlib.sha256(ac.read_bytes()).hexdigest()}
        saved = common.CONFIG / 'legacy-identity.private.json'
        if saved.exists():
            assert json.loads(saved.read_text()) == identity, 'real legacy identity changed'
        else:
            saved.write_text(json.dumps(identity))
            saved.chmod(0o600)

    def verify_runtime(self):
        assert SDK_LAN_PORT in owned_udp_ports(), 'real legacy SDK UDP listener lost'
        assert self.main_result == 0
        proof = {'legacy_udp_ports': [SDK_LAN_PORT], 'real_legacy_main_returned_zero': True}
        (common.CONFIG / 'legacy-runtime-proof.json').write_text(json.dumps(proof))


def install_real_sdk(hass):
    from custom_components.hwhomebridge import hwbridge as legacy
    from custom_components.hwhomebridge.product_registry import ProductDef, ServiceDef, HAMapping
    from custom_components.hwhomebridge.virtual_device import VirtualDevice, ServiceEntry
    sdk = NativeSDKTrace()
    load = legacy.cdll.LoadLibrary
    sdk_path = str(Path(legacy.hilink_bridge_path) / 'lib/amd64/libhilink_bridge.so')

    def observe_load(path):
        is_sdk = path == sdk_path
        if is_sdk:
            assert SDK_LAN_PORT not in owned_udp_ports(), 'legacy LAN port already owned before SDK load'
        library = load(path)
        assert isinstance(library, ctypes.CDLL)
        return sdk.observe(library) if is_sdk else library

    # 仅为目标 SDK 附加观测钩子，其他库及所有原生函数保持原样。
    legacy.cdll.LoadLibrary = observe_load
    original_start = legacy.start_hw_hilink_bridge
    product = ProductDef('002', 'Isolated legacy fixture', services={
        'switch': ServiceDef('switch', 'switch', 'on', ha_mapping=HAMapping(
            domain='light', action='turn_on_off', value_attr='state'))})
    vd = VirtualDevice('LEGACY-FIXTURE-SN', product, 'legacy-source-device')
    vd.add_service_entry('switch', ServiceEntry(common.LEGACY_LIGHT, 'light', 'switch'))
    vd.online = True  # 仅虚拟灯实体可用；不改变 SDK/网关在线状态。

    async def start(actual_hass):
        await original_start(actual_hass)
        assert sdk.library is legacy.lib and sdk.starts == 1
        assert legacy.service_router.sn_manager.register(vd)

    legacy.start_hw_hilink_bridge = start
    return legacy, sdk, vd


async def check_real_sdk(hass, sdk, vd, entry):
    from custom_components.hwhomebridge import hwbridge as legacy
    from custom_components.hwhomebridge.native_async import drain_native
    from homeassistant.helpers import device_registry as dr
    assert sdk.starts == 1 and sdk.removals == 0
    assert legacy.lib is sdk.library and sdk.callbacks[0] is legacy.pActionCB
    # 保留原组件的异步启动语义；HA 完成启动后再等待原生身份和监听器就绪。
    for _ in range(150):
        if sdk.library.GetGatewaySN() and SDK_LAN_PORT in owned_udp_ports():
            break
        await asyncio.sleep(.2)
    else:
        raise TimeoutError('real legacy SDK identity and LAN listener not ready within 30 seconds')
    status = await hass.async_add_executor_job(sdk.library.HILINK_GetDevStatus)
    assert status != 1 and not legacy.bridge_work, 'test must remain unpaired/offline'
    sdk.verify_identity()
    sdk.verify_runtime()
    assert any('libhilink_bridge.so' in line for line in Path('/proc/self/maps').read_text().splitlines())
    device = dr.async_get(hass).async_get_device_by_identifier(
        ('hwhomebridge', entry.entry_id, vd.sn), entry.entry_id)
    assert device is not None and device.config_entry_id == entry.entry_id
    before = len(sdk.reports)
    for on in (1, 0):
        reports_before_action = len(sdk.reports)
        # 直接调用真实注册的 ctypes 回调；不声称这是云端下发。
        await hass.async_add_executor_job(sdk.callbacks[0], vd.sn.encode(), json.dumps({'on': on}).encode())
        await hass.async_block_till_done()
        state = hass.states.get(common.LEGACY_LIGHT)
        assert state.state == ('on' if on else 'off')
        assert legacy.pDevStatusCB(vd.sn.encode(), b'switch') == on
        # 未配对网关的自动上报应暂停，不对未在原生 SDK 注册的虚拟 SN 强行上报。
        assert await hass.async_add_executor_job(drain_native)
        assert len(sdk.reports) == reports_before_action
    assert len(sdk.reports) == before


async def legacy_restart_transition(disabled_at_boot):
    """保存旧后端停用后跨 HA 进程应用，再独立重新启用。"""
    hass, calls, _roles, legacy, sdk, vd = await common.make_hass(install_real_sdk)
    try:
        entries = hass.config_entries.async_entries('hwhomebridge')
        old = next(e for e in entries if e.data.get('backend_type') == 'legacy')
        new = next(e for e in entries if e.data.get('backend_type') == 'openharmony')
        native = common.runtime(hass, new)
        assert native.process.returncode is None
        result = {'legacy_SDK': sdk.evidence, 'OpenHarmony_C': 'real Linux x86_64 process',
                  'real_devices_accessed': False}
        if disabled_at_boot:
            assert old.options['backend'] == 'disabled'
            assert hass.data['hwhomebridge'][old.entry_id]['backend'] == 'disabled'
            assert sdk.starts == 0 and sdk.library is None and legacy.lib is None
            result['saved_legacy_disable_prevents_library_load_in_new_HA_process'] = True
            await common.options(hass, old, 'legacy')
            await check_real_sdk(hass, sdk, vd, old)
            result['reenabling_legacy_starts_real_SDK_once_without_restarting_OH'] = True
        else:
            await check_real_sdk(hass, sdk, vd, old)
            await common.options(hass, old, 'disabled')
            assert old.options['backend'] == 'disabled'
            assert hass.data['hwhomebridge'][old.entry_id]['backend'] == 'legacy'
            await check_real_sdk(hass, sdk, vd, old)
            result['legacy_disable_saved_without_hot_unloading_real_SDK'] = True
        assert common.runtime(hass, new) is native and native.process.returncode is None
        assert common.hashes() == json.loads((common.CONFIG / 'restart-identities.json').read_text())
        assert all(row['entity'].split('.')[1].startswith('oh_flow_') for row in calls)
        result.update(legacy_starts=sdk.starts, legacy_unbind_calls=sdk.removals, passed=True)
        common.RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        owned = [value['runtime'] for value in hass.data.get('hwhomebridge', {}).values()
                 if isinstance(value, dict) and value.get('runtime')]
        await hass.async_stop()
        assert all(item.process.returncode is not None for item in owned)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--restore', action='store_true')
    parser.add_argument('--oh-first', action='store_true')
    transition = parser.add_mutually_exclusive_group()
    transition.add_argument('--save-legacy-disabled', action='store_true')
    transition.add_argument('--restore-legacy-disabled', action='store_true')
    args = parser.parse_args()
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('full x86_64 Linux environment required')
    if 'vendor_id' not in Path('/proc/cpuinfo').read_text():
        raise SystemExit('x86 kernel CPU information required; user-mode emulation is insufficient')
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING)
    if args.save_legacy_disabled or args.restore_legacy_disabled:
        if args.restore or args.oh_first:
            parser.error('legacy transition must be tested as a separate HA process')
        asyncio.run(legacy_restart_transition(args.restore_legacy_disabled))
    else:
        asyncio.run(common.exercise(args.oh_first, args.restore,
                                   install_legacy=install_real_sdk, check_legacy=check_real_sdk))
    result = json.loads(common.RESULT.read_text())
    result.update(json.loads((common.CONFIG / 'legacy-runtime-proof.json').read_text()))
    library = common.CONFIG / 'custom_components/hwhomebridge/hilink_bridge/lib/amd64/libhilink_bridge.so'
    result.update(homeassistant_version=HA_VERSION, python_version=platform.python_version(),
                  legacy_so_sha256=hashlib.sha256(library.read_bytes()).hexdigest(),
                  OpenHarmony_binary_sha256=hashlib.sha256((common.LAB / 'bridge_poc').read_bytes()).hexdigest())
    result.update(real_legacy_library_loaded=True,
                  legacy_remained_unpaired=True, legacy_registered_callbacks_invoked_locally=True,
                  legacy_report_entry_called_directly=False, legacy_cloud_control_verified=False,
                  legacy_automatic_reporting_paused_while_unpaired=True,
                  x86_kernel_cpuinfo_verified=True)
    common.RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    # 旧 SDK 按现有设计常驻到进程结束；避免其线程进入 Python 解释器析构阶段。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
