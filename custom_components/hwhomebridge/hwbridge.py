"""
hwbridge.py - 核心桥接逻辑

重构版本，使用 ServiceRouter + VirtualDevice 架构：
- ServiceRouter 管理产品注册、匹配、路由
- VirtualDevice 聚合同一 HA device 下的多个 entity
- SN 管理从 1:1 (entity) 演进为 1:N (VirtualDevice)

兼容性：
- 保留原有的 C 回调接口 (RegHomeAssistantPyCB)
- 保留原有的 C library 调用方式
- 新增的模块不影响 C 侧的任何接口
"""

import os
import shutil
import asyncio
import platform
import psutil
import socket
import json
import logging
from ctypes import *

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import EVENT_DEVICE_REGISTRY_UPDATED
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_STATE_CHANGED
)

from .service_router import ServiceRouter
from .pin_manager import PINManager
from .const import SKIP_PLATFORMS

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局变量
# ---------------------------------------------------------------------------

bridge_work = False
start_work = False
ghass = None
lib = None
service_router = None  # ServiceRouter 实例（替代原来的 light_plt/fan_plt 等）

hilink_bridge_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hilink_bridge")
# 运行时用户态目录，指向 HA 持久存储区 .storage/hwhomebridge/
# C SDK (HILINK_CONFIG_DIR)、device_ac、new_device.txt 均落此目录
hilink_config_dir = None
hilink_cfg_files = ("bridge.cfg",
                     "bridge_bak.cfg",
                     "hilink_cert.cfg",
                     "hilink_cert_bak.cfg",
                     "pidMap.cfg",
                     "pidMap_bak.cfg",
                     "subDevInfo.cfg",
                     "subDevInfo_bak.cfg",
                     "timer.cfg",
                     "timer_bak.cfg")

# 设备持久化（保存 device_id 而非 entity_id）
# 运行时在 start_hw_hilink_bridge 中拼接到 hilink_config_dir 下，避免污染 config 根目录
saved_device_file = None
registered_device_ids = set()  # 已注册到 HiLink 的 device_id 集合
saved_device_ids = set()        # 从持久化文件恢复的 device_id 集合

# 状态缓冲区池：保持 bytes 对象引用，防止被垃圾回收导致 C 侧访问悬空指针
# C 侧会在回调返回时拷贝内容，Python 侧保持引用直到下次调用或进程结束
_char_state_bytes = {}  # {(sn, svc_id): bytes_object}


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

async def start_hw_hilink_bridge(hass: HomeAssistant):
    """启动 HiLink 桥接"""
    global ghass, lib, service_router

    ghass = hass

    # 初始化 ServiceRouter
    service_router = ServiceRouter()
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'config', 'product_registry.json')
    result = await service_router.load_product_registry(config_path)
    if result:
        _LOGGER.info(f"Product registry loaded successfully, "
                      f"{service_router.registry.get_product_count()} products, "
                      f"{service_router.registry.get_default_count()} defaults, "
                      f"{service_router.registry.get_adapter_count()} vendor adapters")
    else:
        _LOGGER.error("Failed to load product registry, device matching will not work")

    global hilink_config_dir, saved_device_file

    hilink_config_dir = hass.config.path('.storage', 'hwhomebridge', 'config')

    os.makedirs(hilink_config_dir, exist_ok=True)

    # 将随插件发布的固定配置文件复制到运行时目录，C SDK 启动时需要这些文件
    # 始终覆盖：升级后固定配置可能有更新
    fixed_cfg_src_dir = os.path.join(hilink_bridge_path, 'config')
    for fname in ('hilink.cfg', 'hilink_bak.cfg'):
        src = os.path.join(fixed_cfg_src_dir, fname)
        dst = os.path.join(hilink_config_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, dst)

    os.environ['HILINK_CONFIG_DIR'] = hilink_config_dir + '/'
    saved_device_file = os.path.join(hilink_config_dir, 'new_device.txt')
    _LOGGER.info(f"hwhomebridge runtime dir: {hilink_config_dir}")

    # 加载 C 库（按 CPU 架构选择对应的 so）
    machine = platform.machine() or ""
    arch = ""
    if machine.lower() in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine.lower() in ("aarch64", "arm64"):
        arch = "aarch64"

    if not arch:
        _LOGGER.error(f"Unsupported CPU architecture: {machine}, bridge will not start")
        return

    so_path = os.path.join(hilink_bridge_path, "lib", arch, "libhilink_bridge.so")
    if not os.path.exists(so_path):
        _LOGGER.error(f"SO file not found: {so_path}, bridge will not start")
        return

    _LOGGER.info(f"Loading hilink bridge so: {so_path}")
    try:
        dll = cdll.LoadLibrary
        lib = dll(so_path)
    except Exception as e:
        _LOGGER.error(f"Failed to load so {so_path}: {e}, bridge will not start")
        return
    _LOGGER.info(f"open hilink bridge so success.")

    # 设置A_C（48字节随机字符串）
    # 优先从 device_ac 文件读取已保存的 ac，避免每次启动重新生成
    device_ac_file = os.path.join(hilink_config_dir, 'device_ac')
    ac = _load_or_create_ac(device_ac_file)
    ac_value = (c_ubyte * 48)(*ac)
    lib.HILINK_SetAutoAc(ac_value, 48)

    # 设置返回 const char* 的函数的 restype，避免 ctypes 默认按 int 截断指针
    lib.GetGatewaySN.restype = c_char_p

    # 注册 C 回调
    lib.RegHomeAssistantPyCB(pActionCB, pTypeCheckCB, pDevStatusCB, pBridgeStatusCB, pGetCharStateCB)
    
    # 注册PIN查询回调
    try:
        lib.RegPINQueryCallback(pPINQueryCB)
        _LOGGER.info("PIN query callback registered successfully")
    except AttributeError:
        _LOGGER.warning("C library does not support RegPINQueryCallback, PIN feature may not work")
    except Exception as e:
        _LOGGER.error(f"Failed to register PIN query callback: {e}")

    # 设置 ServiceRouter 的依赖
    service_router.set_hass(hass)
    service_router.set_lib(lib)

    # 启动 C 库
    rlt = lib.main()
    _LOGGER.info(f"start hilink bridge rlt={rlt}.")

    # 监听 HA 启动完成事件
    # 注意：如果HA已经启动完成，事件不会触发，需要主动检查
    if hass.state.value == "RUNNING":
        _LOGGER.info("Home Assistant already running, directly initializing bridge")
        # HA已经启动，直接调用初始化
        await handle_started_event(None)
    else:
        _LOGGER.info("Waiting for Home Assistant to start")
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, handle_started_event)


async def handle_started_event(event):
    """HA 启动完成后的处理
    
    架构优化：移除对特定平台（xiaomi_miot）的依赖
    - 不再等待特定平台加载
    - 改为等待 HA 完全启动后直接监听状态变化
    - 设备发现逻辑移至 _discover_and_register_devices
    
    Args:
        event: 事件对象，当HA已经启动时直接调用时为None
    """
    global start_work

    _LOGGER.info("Home Assistant started, initializing bridge")
    
    # 给其他集成一些时间完成加载（可选）
    # 实际上 EVENT_HOMEASSISTANT_STARTED 已经在所有集成加载后触发
    # 这里只是添加日志便于调试
    await asyncio.sleep(2)  # 短暂延迟确保所有集成的实体都已创建
    
    _LOGGER.info("Bridge initialization complete, starting state monitoring")

    # 监听状态变化事件
    ghass.bus.async_listen(EVENT_STATE_CHANGED, handle_state_event)

    # 监听设备删除事件
    ghass.bus.async_listen(EVENT_DEVICE_REGISTRY_UPDATED, handle_device_registry_updated)

    start_work = True
    
    _LOGGER.info("start_work set to True")
    
    # 发现并注册设备（不再依赖 OnBridgeStatusCB 的触发）
    # OnBridgeStatusCB 可能在 HA 启动前就触发并等待 start_work，
    # 但等待循环不可靠，导致 notify_new_device 未被调用。
    # 这里直接调用确保设备发现一定执行。
    _LOGGER.info("Starting device discovery from handle_started_event")
    notify_new_device(ghass)

    # 设备注册完成后，延迟同步所有设备状态到 HiLink
    # 延迟原因：C 侧在 HilinkSyncBrgDevStatus(3) 后会执行 batch bind，
    # 期间 UpdateHAStatus 调用会被 C 侧丢弃甚至导致崩溃。
    # 等待 batch bind 完成后再上报状态。
    if service_router is not None and service_router.sn_manager.get_device_count() > 0:
        _LOGGER.info("Scheduling delayed state sync for newly registered devices")
        ghass.loop.call_later(30, _delayed_resync_all_devices)


def stop_hw_hilink_bridge(hass: HomeAssistant):
    """停止 HiLink 桥接，删除子设备+重置网关，清理持久化文件

    在集成被永久删除（async_remove_entry）时通过 executor 调用。
    1. 遍历所有已注册子设备，逐个调用 status=2 删除云端信息
    2. 调用 C 侧 exit_brg() 重置网关并退出 SDK 后台线程
    """
    global bridge_work, start_work

    bridge_work = False
    start_work = False

    # 1. 逐个删除子设备云端信息（C 侧 exit_brg 不会级联删除子设备）
    if service_router is not None:
        all_vds = service_router.sn_manager.get_all_devices()
        for vd in all_vds:
            _LOGGER.info(f"Removing sub-device from HiLink cloud: sn={vd.sn}")
            service_router.register_device_to_hilink(vd.sn, 2)

    # 2. 调用 C 侧 exit_brg() 重置网关并退出后台线程
    if lib is not None:
        try:
            rlt = lib.exit_brg()
            _LOGGER.info(f"exit_brg() = {rlt}")
        except Exception as e:
            _LOGGER.error(f"Failed to call exit_brg: {e}")

    # 清理 Python 侧状态
    if service_router is not None:
        service_router.sn_manager.clear()
    registered_device_ids.clear()
    saved_device_ids.clear()

    # 删除持久化文件（与 OnBridgeStatusCB devStatus==9 路径一致）
    if saved_device_file and os.path.exists(saved_device_file):
        os.remove(saved_device_file)
    for filename in hilink_cfg_files:
        onefile = os.path.join(hilink_config_dir, filename)
        if os.path.exists(onefile):
            os.remove(onefile)

    _LOGGER.info("HiLink bridge stopped, gateway removed from cloud")


# ---------------------------------------------------------------------------
# C 回调函数
# ---------------------------------------------------------------------------

def OnPyActionCB(sn_data, payload_data):
    """C 回调：HiLink 控制命令到达

    通过 ServiceRouter 路由到正确的 entity。
    """
    sn = bytes.decode(sn_data)
    payload = bytes.decode(payload_data)
    _LOGGER.info(f"=== OnPyActionCB called, sn={sn}, payload={payload}")

    if service_router is not None:
        result = service_router.route_action(sn, payload)
        if not result:
            _LOGGER.warning(f"OnPyActionCB: route_action failed for sn={sn}, payload={payload}")
    else:
        _LOGGER.error("OnPyActionCB: service_router not initialized")
        _LOGGER.warning(f"OnPyActionCB: route_action failed for sn={sn}, payload={payload}")


def OnTypeCheckCB(sn_data):
    """C 回调：查询设备类型索引

    在新架构中，通过 SN 查找 VirtualDevice，
    然后根据产品定义返回产品索引。
    但由于 C 侧的 map_prodIds 数组仍然是基于索引的，
    这里需要一个从 PID 到索引的映射。
    """
    sn = bytes.decode(sn_data)

    if service_router is None:
        return -1

    vd = service_router.sn_manager.get_by_sn(sn)
    if vd is None:
        return -1

    # 查找 PID 在 C 侧 map_prodIds 数组中的索引
    pid = vd.product.pid
    pid_index = _get_pid_index(pid)
    if pid_index < 0:
        _LOGGER.warning(f"OnTypeCheckCB: PID {pid} not found in C-side map_prodIds")
        return -1

    return pid_index


def OnDevStatusCB(sn_data, svcId):
    """C 回调：查询设备状态
    
    根据不同的服务类型返回相应的状态值：
    - switch: 返回 0 或 1
    - brightness: 返回 0-100 的亮度值
    - 其他数值型服务：返回对应的数值
    
    特殊处理：当 switch 服务映射到 button entity 时，
    button 没有 on/off 状态，需要使用缓存的 switch_state。
    """
    sn = bytes.decode(sn_data)

    if service_router is None:
        return 0

    vd = service_router.sn_manager.get_by_sn(sn)
    if vd is None:
        return 0

    svc_id_decoded = bytes.decode(svcId) if isinstance(svcId, bytes) else svcId

    # 通过 VirtualDevice 查找对应 entity 的状态
    entity_id = vd.get_entity_for_service(svc_id_decoded)
    if entity_id is None:
        # 默认用第一个 service 的 entity
        entity_id = vd.get_entity_for_service("switch")

    if entity_id and ghass:
        state = ghass.states.get(entity_id)
        domain = entity_id.split('.')[0] if entity_id else ""
        
        # 特殊处理：button/select 域没有 on/off 状态，使用缓存的 switch_state
        # button: 电饭煲取消按钮（小米电饭煲）
        # select: 电饭煲工作状态 select（美的电饭煲），通过 text_to_enum 映射后缓存
        if svc_id_decoded == "switch" and domain in ("button", "select"):
            _LOGGER.debug(f"OnDevStatusCB: {domain} entity {entity_id}, using cached switch_state={vd.switch_state}")
            return vd.switch_state
        
        # brightness 服务：返回亮度值 (0-100)
        if svc_id_decoded == "brightness":
            if state and state.state == 'on':
                brightness = state.attributes.get('brightness', 0)
                # HA 的亮度范围是 0-255，需要转换为华为的 0-100
                brightness_huawei = int(brightness * 100 / 255) if brightness else 0
                _LOGGER.debug(f"OnDevStatusCB: brightness entity {entity_id}, HA brightness={brightness}, Huawei brightness={brightness_huawei}")
                return brightness_huawei
            else:
                return 0
        
        # 普通处理：从 entity state 读取
        if state and state.state == 'on':
            return 1

    return 0


def _delayed_resync_all_devices():
    """延迟重新同步所有设备状态到 HiLink

    在 OnBridgeStatusCB(devStatus=1) 时调度，延迟 30 秒执行。
    此时 C 侧的 batch bind（批量绑定子设备到云端）应该已完成，
    UpdateHAStatus 调用不会再被 "batch ops priority time" 丢弃。
    """
    if not bridge_work:
        _LOGGER.info("Delayed resync skipped: bridge is offline")
        return
    if service_router is None:
        return
    _LOGGER.info("Starting delayed state re-sync for all devices")
    for vd in service_router.sn_manager.get_all_devices():
        service_router.sync_device_state(vd)
        _LOGGER.info(f"Re-synced state for sn={vd.sn} after gateway online (delayed)")


def OnGetCharStateCB(sn_data, svcId):
    """C 回调：查询指定服务的状态，返回 JSON 字符串

    供 HilinkGetBrgDevCharState 使用，通过 ServiceRouter
    查询对应 VirtualDevice 的 entity 状态，返回符合 profile 的 JSON。

    Args:
        sn_data: 设备 SN（C 字符串）
        svcId: 服务 ID（C 字符串）

    Returns:
        bytes 对象（自动转换为 c_char_p），如 '{"on":1}' 或 '{"brightness":50}'
        查询失败返回 None
        
    注意：
        - C 侧会在回调返回时立即拷贝内容
        - Python 侧缓存 bytes 对象防止过早被垃圾回收
        - 下次调用同一 (sn, svc_id) 时会覆盖旧的 bytes 对象
    """
    sn = bytes.decode(sn_data)
    svc_id_decoded = bytes.decode(svcId) if isinstance(svcId, bytes) and svcId else ""

    _LOGGER.debug(f"OnGetCharStateCB: sn={sn}, svcId={svc_id_decoded}")

    if service_router is None:
        return None

    result = service_router.get_char_state(sn, svc_id_decoded)
    if result is not None:
        _LOGGER.debug(f"OnGetCharStateCB: sn={sn}, svcId={svc_id_decoded}, result={result}")
        # 缓存 bytes 对象，防止在 C 侧拷贝前被垃圾回收
        # C 侧会在回调返回时立即拷贝内容，下次调用时覆盖此缓存
        result_bytes = result.encode('utf-8')
        key = (sn, svc_id_decoded)
        _char_state_bytes[key] = result_bytes
        return result_bytes
    
    return None


def OnBridgeStatusCB(status):
    """C 回调：桥接状态变化"""
    devStatus = lib.HILINK_GetDevStatus()
    _LOGGER.info(f"=== hwbridge status is {status}, dev status is {devStatus}.")

    if 1 == devStatus:
        global bridge_work
        bridge_work = True
        
        if not start_work:
            return
        
        if ghass is not None:
            _LOGGER.info("Gateway back online, re-discovering devices")
            notify_new_device(ghass)
            # 网关上线后，对所有已注册的 VirtualDevice 重新同步状态
            # notify_new_device 会跳过已注册设备，但它们的初始状态
            # 可能因网关未上线而未成功上报
            #
            # 注意：网关上线后 C 侧会执行 batch bind（批量绑定子设备到云端），
            # 期间 UpdateHAStatus 调用会被 C 侧丢弃（batch ops priority time,
            # ignore information report）。需要延迟等到 batch bind 完成后再
            # 上报状态，否则状态上报会被全部丢弃。
            if service_router is not None:
                _LOGGER.info("Scheduling delayed state re-sync (waiting for batch bind to complete)")
                ghass.loop.call_later(30, _delayed_resync_all_devices)
    elif 9 == devStatus:
        bridge_work = False
        if start_work:
            registered_device_ids.clear()
            saved_device_ids.clear()
            if saved_device_file and os.path.exists(saved_device_file):
                os.remove(saved_device_file)
            for filename in hilink_cfg_files:
                onefile = os.path.join(hilink_config_dir, filename)
                if os.path.exists(onefile):
                    os.remove(onefile)
            _LOGGER.info("Bridge went offline (devStatus=9) after startup, cleaned persisted files")
        else:
            _LOGGER.info("devStatus=9 during startup, skipping persisted file cleanup")


# 注册 C 回调的 ctypes 类型
BRG_ACTION_FUNC = CFUNCTYPE(None, c_char_p, c_char_p)
pActionCB = BRG_ACTION_FUNC(OnPyActionCB)

BRG_TYPECHECK_FUNC = CFUNCTYPE(c_int, c_char_p)
pTypeCheckCB = BRG_TYPECHECK_FUNC(OnTypeCheckCB)

BRG_DEVSTATUS_FUNC = CFUNCTYPE(c_int, c_char_p, c_char_p)
pDevStatusCB = BRG_DEVSTATUS_FUNC(OnDevStatusCB)

BRG_GETCHARSTATE_FUNC = CFUNCTYPE(c_char_p, c_char_p, c_char_p)
pGetCharStateCB = BRG_GETCHARSTATE_FUNC(OnGetCharStateCB)

BRG_NOTIFYSTATUS_FUNC = CFUNCTYPE(None, c_int)
pBridgeStatusCB = BRG_NOTIFYSTATUS_FUNC(OnBridgeStatusCB)


# ---------------------------------------------------------------------------
# PIN查询回调函数（供C侧SDK调用）
# ---------------------------------------------------------------------------

def OnPINQueryCB():
    """C回调：查询当前PIN码
    
    当SDK收到智慧生活APP的PIN校验请求时，调用此函数获取正确的PIN码。
    
    Returns:
        int: 当前有效的8位PIN码整数
        0: 无有效PIN码或已过期
    """
    pin = PINManager.get_current_pin()
    if pin:
        _LOGGER.info(f"C-side queried PIN: {pin}")
        return pin
    else:
        _LOGGER.warning("C-side queried PIN, but no valid PIN available")
        return 0


PIN_QUERY_FUNC = CFUNCTYPE(c_int)
pPINQueryCB = PIN_QUERY_FUNC(OnPINQueryCB)


# ---------------------------------------------------------------------------
# 设备发现与注册
# ---------------------------------------------------------------------------

def notify_new_device(hass: HomeAssistant):
    """发现并注册新设备

    核心改动：使用 ServiceRouter 的聚合逻辑替代原来的
    逐 entity 注册方式。新的方式按 device 维度注册，
    一个 HA device 对应一个 VirtualDevice（一个 SN）。
    """
    # 加载持久化的设备列表
    if saved_device_file and os.path.exists(saved_device_file):
        with open(saved_device_file, 'r') as f:
            for line in f:
                device_id = line.strip()
                if device_id:
                    saved_device_ids.add(device_id)
        _LOGGER.info(f"Loaded {len(saved_device_ids)} saved device IDs")
    else:
        _LOGGER.warning(f"saved_device_file is not set (value={saved_device_file}), "
                        "persisted device list will not be loaded")

    # 通过 HA 的 device_registry 和 entity_registry 发现设备
    _discover_and_register_devices(hass)


def _discover_and_register_devices(hass: HomeAssistant):
    """发现并注册 HA device 到 HiLink

    架构优化：移除对特定平台的依赖
    
    核心流程：
    1. 从 HA 的 device_registry 中获取所有设备
    2. 对每个设备，通过 product_matcher 尝试匹配产品
    3. 如果匹配成功，通过 ServiceRouter 注册
    4. 注册成功后同步到 HiLink
    
    过滤策略：
    - 跳过没有实体的设备
    - 跳过无法匹配产品定义的设备（静默跳过，不报错）
    - 支持任意来源的设备（xiaomi/tuya/zigbee/mqtt等）
    """
    if service_router is None:
        _LOGGER.error("ServiceRouter not initialized, cannot discover devices")
        return

    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)

    # 获取所有设备（不再限制为特定平台）
    all_devices = device_reg.devices
    
    _LOGGER.info(f"Starting device discovery, total {len(all_devices)} devices in registry")

    registered_count = 0
    skipped_count = 0
    
    # 对每个 device，调用 _register_single_device
    for device_id in all_devices.keys():
        result = _register_single_device(device_id)
        if result:
            registered_count += 1
        else:
            skipped_count += 1
    
    _LOGGER.info(f"Device discovery complete: {registered_count} registered, "
                 f"{skipped_count} skipped")


def _register_single_device(device_id: str, reuse_sn: str = None, is_update: bool = False) -> bool:
    """注册单个设备到 HiLink
    
    从 HA 的 device_registry 和 entity_registry 收集设备信息，
    通过 ServiceRouter 注册到 HiLink。
    
    Args:
        device_id: HA 的 device_id
        reuse_sn: 复用指定的 SN（用于设备更新场景）
        is_update: 是否为更新场景（True 则不保存到持久化文件）
    
    Returns:
        True 表示注册成功，False 表示跳过或失败
    """
    if service_router is None:
        _LOGGER.error("ServiceRouter not initialized, cannot register device")
        return False
    
    if ghass is None:
        return False
    
    # 防重复注册：如果设备已注册且不是更新场景，跳过
    if not is_update and device_id in registered_device_ids:
        _LOGGER.debug(f"Device {device_id} already registered, skip")
        return False
    
    device_reg = dr.async_get(ghass)
    entity_reg = er.async_get(ghass)
    
    device_entry = device_reg.async_get(device_id)
    if not device_entry:
        _LOGGER.debug(f"Device {device_id} not found in registry")
        return False
    
    # 收集该 device 下的所有 entity
    device_entities = er.async_entries_for_device(entity_reg, device_id)
    
    # 跳过没有实体的设备
    if not device_entities:
        _LOGGER.debug(f"Device {device_id} has no entities, skipping")
        return False
    
    entity_list = []
    for entry in device_entities:
        # 跳过来自反向桥接集成（如 huawei_smarthome）的实体，避免循环接入
        if getattr(entry, "platform", None) in SKIP_PLATFORMS:
            _LOGGER.debug(f"Skipping entity {entry.entity_id} from platform '{entry.platform}' to avoid bridge loop")
            continue

        state = ghass.states.get(entry.entity_id)
        entity_name = state.name if state and hasattr(state, 'name') else ""
        
        # device_class 从两个来源获取：1) RegistryEntry 2) State.attributes
        device_class = entry.device_class
        if not device_class and state:
            device_class = state.attributes.get('device_class')
        
        entity_list.append({
            "entity_id": entry.entity_id,
            "domain": entry.domain,
            "device_class": device_class,
            "name": entity_name,
        })

    # 过滤后若无可用实体，跳过该设备
    if not entity_list:
        _LOGGER.debug(f"Device {device_id} has no eligible entities after platform filter, skipping")
        return False

    # 构建设备信息
    model = device_entry.model or ""
    name = device_entry.name_by_user or device_entry.name or ""
    manufacturer = device_entry.manufacturer or ""

    _LOGGER.debug(f"Registering device: id={device_id}, name={name}, model={model}, "
                  f"manufacturer={manufacturer}, entities={len(entity_list)}, "
                  f"reuse_sn={reuse_sn}, is_update={is_update}")
    
    # 通过 ServiceRouter 注册（内部会通过 product_matcher 匹配）
    # 如果匹配失败，register_device 会返回 None
    vd = service_router.register_device(
        device_id=device_id,
        device_name=name,
        entity_list=entity_list,
        model=model,
        manufacturer=manufacturer,
        reuse_sn=reuse_sn
    )

    if vd is not None:
        # 注册到 HiLink
        if is_update:
            # 更新场景：设备已在线，status=1
            service_router.register_device_to_hilink(vd.sn, 1)
        elif device_id in saved_device_ids:
            # 已知设备，status=1
            service_router.register_device_to_hilink(vd.sn, 1)
        else:
            # 新设备，先用status=3注册，再调用status=1确认上线
            service_router.register_device_to_hilink(vd.sn, 3)
            service_router.register_device_to_hilink(vd.sn, 1)
            _save_device_id(device_id)

        registered_device_ids.add(device_id)

        # 标记设备为在线，启用状态变化上报（handle_state_event 依赖此标志）
        vd.online = True

        # 延迟同步初始状态
        # 在 C 侧已上线状态下，HilinkSyncBrgDevStatus(3) 会触发 batch bind，
        # 此时立即调用 UpdateHAStatus 会导致 C 侧状态冲突崩溃。
        # 延迟到 batch bind 完成后再上报状态。
        _LOGGER.info(f"Device registered, state sync deferred: sn={vd.sn}, device_id={device_id}, "
                     f"name={name}, model={model}")
        return True
    else:
        # 匹配失败，静默跳过
        _LOGGER.debug(f"Device {device_id} ({name}, model={model}) did not match any product, skipping")
        return False


# ---------------------------------------------------------------------------
# 状态变化处理
# ---------------------------------------------------------------------------

def handle_state_event(event):
    """处理 HA state change 事件，将状态同步到 HiLink"""
    if 'entity_id' not in event.data:
        return

    entity_id = event.data['entity_id']
    
    if not bridge_work:
        return

    # 通过 ServiceRouter 查找 entity 所属的 VirtualDevice
    if service_router is None:
        return

    vd = service_router.sn_manager.get_virtual_device_by_entity(entity_id)
    if vd is None:
        return

    old_state = event.data['old_state']
    new_state = event.data['new_state']

    # 处理设备离线状态
    if new_state and new_state.state == 'unavailable':
        if vd.online:
            vd.online = False
            service_router.register_device_to_hilink(vd.sn, 0)
            _LOGGER.info(f"Device {entity_id} went offline, notified HiLink")
        return

    # 处理设备恢复在线
    if old_state and old_state.state == 'unavailable':
        if not vd.online:
            vd.online = True
            service_router.register_device_to_hilink(vd.sn, 1)
            _LOGGER.info(f"Device {entity_id} came back online, notified HiLink")

    # 同步状态到 HiLink
    if vd.online:
        service_router.report_state(vd.sn, entity_id, new_state)

def handle_device_registry_updated(event):
    """处理 HA 设备注册表变化事件

    支持三种事件类型：
    - create: 新设备接入，自动注册到 HiLink
    - update: 设备信息更新，重新匹配并注册
    - remove: 设备删除，从 HiLink 注销
    """
    if not bridge_work:
        return

    if service_router is None:
        return

    event_type = event.data.get('action')
    device_id = event.data.get('device_id')

    if not event_type or not device_id:
        return

    _LOGGER.debug(f"Device registry event: action={event_type}, device_id={device_id}")

    if event_type == 'create':
        _handle_new_device_event(device_id)
    elif event_type == 'update':
        _handle_device_update_event(device_id, event)
    elif event_type == 'remove':
        _handle_device_remove_event(device_id)


def _handle_new_device_event(device_id: str):
    """处理新设备创建事件
    
    新设备创建时，实体可能还未完全加载，延迟处理以确保实体就绪。
    """
    if device_id in registered_device_ids:
        _LOGGER.debug(f"Device {device_id} already registered, skip create event")
        return
    
    _LOGGER.info(f"New device created in HA: device_id={device_id}, scheduling registration")
    
    # 延迟处理：等待实体的创建完成
    # 使用 hass.loop.call_later 或 asyncio.create_task 实现
    # 这里用同步延迟，因为回调可能在后台线程
    if ghass:
        ghass.loop.call_later(2, lambda: _register_single_device(device_id))


def _handle_device_update_event(device_id: str, event):
    """处理设备更新事件
    
    设备更新时，检查关键字段是否变化，决定是否重新注册。
    关键字段：model, manufacturer, name_by_user, name
    """
    if device_id not in registered_device_ids:
        # 未注册的设备更新，按新设备处理
        _LOGGER.debug(f"Device {device_id} updated but not registered, treating as new device")
        _handle_new_device_event(device_id)
        return
    
    # 获取旧设备信息
    vd = service_router.sn_manager.get_by_device_id(device_id)
    if not vd:
        return
    
    # 从事件数据中提取设备变化信息
    changes = event.data.get('changes', {})
    
    # 检查关键字段是否变化
    # HA 的 changes 格式可能是 {field: (old_value, new_value)} 或 {field: new_value}
    key_fields = {'model', 'manufacturer', 'name_by_user', 'name'}
    changed_fields = key_fields & set(changes.keys())
    
    if not changed_fields:
        _LOGGER.debug(f"Device {device_id} updated but no key fields changed, skip re-registration")
        return
    
    _LOGGER.info(f"Device {device_id} key fields changed: {changed_fields}, re-registering")
    
    # 重新注册设备（复用原 SN）
    _register_single_device(device_id, reuse_sn=vd.sn, is_update=True)


def _handle_device_remove_event(device_id: str):
    """处理设备删除事件"""
    _LOGGER.info(f"Device removed in HA: device_id={device_id}, reporting offline to HiLink")
    
    vd = service_router.unregister_device(device_id)
    if vd is not None:
        registered_device_ids.discard(device_id)
        _LOGGER.info(f"Device {device_id} (sn={vd.sn}) reported offline and cleaned up")
    else:
        _LOGGER.debug(f"Device {device_id} was not registered in HiLink, skip offline report")


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _get_pid_index(pid: str) -> int:
    """查询 PID 在 C 侧 map_prodIds 数组中的索引

    C 侧的 map_prodIds 数组：
    index 0 -> "001"
    index 1 -> "002"
    index 2 -> "003"
    index 3 -> "004"
    index 4 -> "005"
    index 5 -> "006"
    index 6 -> "007"
    index 7 -> "008"

    此映射需要与 C 侧保持同步。后续可以通过自动生成来保证一致性。
    """
    pid_index_map = {
        "001": 0,
        "002": 1,
        "003": 2,
        "004": 3,
        "005": 4,
        "006": 5,
        "007": 6,
        "008": 7,
    }
    return pid_index_map.get(pid, -1)


def _save_device_id(device_id: str):
    """持久化 device_id

    使用 hass.loop 的 executor 在后台线程执行文件写入，
    避免在事件循环中执行阻塞 I/O。
    """
    if device_id not in saved_device_ids:
        saved_device_ids.add(device_id)
        if ghass is not None:
            ghass.loop.run_in_executor(None, _write_device_id_to_file, device_id)
        elif saved_device_file:
            with open(saved_device_file, "a") as f:
                f.write(device_id + '\n')


def _write_device_id_to_file(device_id: str):
    """在后台线程中执行文件写入"""
    try:
        if not saved_device_file:
            _LOGGER.warning("saved_device_file is not set, cannot persist device_id")
            return
        with open(saved_device_file, "a") as f:
            f.write(device_id + '\n')
    except Exception as e:
        _LOGGER.error(f"Failed to save device_id {device_id}: {e}")


def _load_or_create_ac(device_ac_file: str) -> bytes:
    """加载或创建 A_C（48字节随机字符串）

    优先从 device_ac 文件读取已保存的 ac，避免每次启动重新生成。
    如果文件不存在，则生成新的 ac 并保存到文件供下次使用。

    Args:
        device_ac_file: device_ac 文件路径

    Returns:
        48 字节的 ac 数据
    """
    if os.path.exists(device_ac_file):
        try:
            with open(device_ac_file, 'rb') as f:
                ac = f.read()
            if len(ac) != 48:
                _LOGGER.warning(f"device_ac file has invalid ac length {len(ac)}, regenerating")
                ac = os.urandom(48)
                _save_ac_to_file(device_ac_file, ac)
            else:
                _LOGGER.info("Loaded AC from device_ac file")
            return ac
        except Exception as e:
            _LOGGER.error(f"Failed to load AC from device_ac file: {e}, regenerating")
            ac = os.urandom(48)
            _save_ac_to_file(device_ac_file, ac)
            return ac
    else:
        ac = os.urandom(48)
        _save_ac_to_file(device_ac_file, ac)
        _LOGGER.info("Generated new AC and saved to device_ac file")
        return ac


def _save_ac_to_file(device_ac_file: str, ac: bytes):
    """保存 ac 到 device_ac 文件"""
    try:
        os.makedirs(os.path.dirname(device_ac_file), exist_ok=True)
        with open(device_ac_file, 'wb') as f:
            f.write(ac)
    except Exception as e:
        _LOGGER.error(f"Failed to save AC to device_ac file: {e}")


def get_network_info() -> dict[str, str]:
    """获取本机网络接口信息"""
    interfaces = psutil.net_if_addrs()
    results: dict[str, str] = {}
    for name, addresses in interfaces.items():
        if name == 'hassio' or name.startswith('docker'):
            continue
        for address in addresses:
            if (
                address.family != socket.AF_INET
                or not address.address
                or not address.netmask
            ):
                continue
            if address.address == '127.0.0.1':
                continue
            results[name] = address.address
    return results
