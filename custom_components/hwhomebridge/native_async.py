"""阻塞式原生 SDK 调用的统一出口。

实测根因
--------
`HilinkSyncBrgDevStatus` / `UpdateHAStatus` 会获取 SDK 内部互斥锁
（`M2mMutexLock`）。SDK 在批处理绑定期间可长时间持有该锁，此时若 HA
事件循环线程正阻塞在这些调用上，整个实例会失去响应（HTTP / WebSocket
全部超时），表现为「配置设备时 HA 卡死、连接中断」。

实测证据（gdb 抓取卡死瞬间 HA 主线程的调用栈）：

    __pthread_mutex_timedlock
     <- libhilink_bridge.so
     <- HilinkSyncBrgDevStatus
     <- _ctypes / libffi
     <- _PyEval_EvalFrameDefault
     <- _asyncio                    # 事件循环线程

SDK 侧同步佐证：

    LD_NOTICE hilink_thread_adapter.c:241, wait mutex timeout 120000 ms
    LD_CRIT   hilink_mutex_ex.c:61, lock too long!
              LoopDispatchEvent/120002/488,M2mMutexLock/120000/280

同一把锁在 SDK 回调期间也可能被持着：`OnBridgeStatusCB` 在 SDK 线程上进入
Python 后若同步调用 SDK，就有重入同一把非递归锁的风险。

做法
----
阻塞式原生调用统一提交到本模块的专用执行器：

* 单线程 → 天然串行，与 SDK 单把锁的语义一致，调用顺序得以保持。
* 事件循环线程只入队，绝不直接进入原生代码。
* SDK 回调线程只入队，不在回调里重入原生调用。
* 回调处理体单独一条线程（`_callback_executor`）—— 它要拿的正是回调发生时
  可能被 SDK 持着的锁，与业务调用共用一条线程会让两者互相堵死（见下文）。

超时只表示「放弃等待」，无法终止已经进入原生代码的那次调用；它的作用是
让事件循环与日志继续工作。
"""

import concurrent.futures
import logging
import threading

_LOGGER = logging.getLogger(__name__)

# 单线程执行器：串行执行阻塞式原生调用
_sdk_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="hwhomebridge-sdk"
)

# 独立单线程执行器：只跑 SDK 回调的处理体。
# 必须与 _sdk_executor 分开 —— 回调处理体里的 HILINK_GetDevStatus() 要拿的正是
# 回调发生时可能被 SDK 持着的那把锁。若两者共用一条线程，一旦该调用卡住，
# 后续所有设备注册（HilinkSyncBrgDevStatus）都会永久排队，桥接静默失效。
_callback_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="hwhomebridge-cb"
)

# 单次等待的默认上限（秒）
DEFAULT_NATIVE_TIMEOUT = 30

# 排队深度告警阈值：超过即说明 SDK 正长期持锁
_PENDING_WARN_THRESHOLD = 8

_pending_lock = threading.Lock()
_pending_count = 0
_warned = False


def _enter_queue():
    global _pending_count, _warned
    with _pending_lock:
        _pending_count += 1
        depth = _pending_count
        if depth >= _PENDING_WARN_THRESHOLD and not _warned:
            _warned = True
            _LOGGER.warning(
                "SDK 调用排队已达 %s 项，说明原生库长时间未释放内部锁；"
                "事件循环仍保持响应",
                depth,
            )


def _exit_queue():
    global _pending_count, _warned
    with _pending_lock:
        _pending_count -= 1
        if _pending_count == 0:
            _warned = False


def _guarded(func, args, kwargs):
    """执行原生调用并维护排队计数。"""
    _enter_queue()
    try:
        return func(*args, **kwargs)
    finally:
        _exit_queue()


def submit_native(func, *args, **kwargs):
    """把阻塞式原生调用提交到专用线程，立即返回 Future。

    供已经位于工作线程的调用者使用（SDK 回调线程、executor 任务）。
    调用方不需要等待结果。
    """
    return _sdk_executor.submit(_guarded, func, args, kwargs)


def submit_callback(func, *args, **kwargs):
    """把 SDK 回调的处理体提交到**独立**线程，立即返回 Future。

    与 `submit_native` 分开是必要的：回调处理体里要调用 `HILINK_GetDevStatus()`
    等需要 SDK 锁的函数，而回调发生时 SDK 可能正持着那把锁。两者若共用一条
    线程，一次卡住的回调就会把后续所有设备注册堵死。
    """
    return _callback_executor.submit(_guarded, func, args, kwargs)


def drain_native(timeout=DEFAULT_NATIVE_TIMEOUT):
    """等待已排队的原生调用全部执行完。

    执行器是单线程且 FIFO，因此提交一个哨兵任务并等它返回，即表示此前所有
    任务都已出队执行完毕。销毁 SDK（`exit_brg`）或删除持久化文件前必须先
    排空，否则会在原生调用还在进行时拆掉底层资源。

    注意：不能以 `_pending_count == 0` 作为提前返回的条件 —— 该计数由执行线程
    维护，任务「已提交但尚未开始执行」时它仍是 0，提前返回会在任务真正跑起来
    之前就放行清理。
    """
    try:
        _sdk_executor.submit(lambda: None).result(timeout=timeout)
        return True
    except concurrent.futures.TimeoutError:
        _LOGGER.warning(
            "等待 %s 秒后仍有原生调用未完成，继续后续清理", timeout
        )
        return False
    except Exception as e:  # pylint: disable=broad-except
        _LOGGER.error("排空原生调用队列时出错: %s", e)
        return False
