"""原生小栈回调只入队和读缓存；所有重活由SDK所有者主线程执行。"""
from collections import deque
import ctypes as C
import json
import time

class LegacyCallbacks:
    """SDK小栈线程仅投递命令或读取缓存；HTTP与SDK调用由主线程完成。"""
    def __init__(self, catalog, gateway, state, snapshots):
        if (not catalog or len(catalog) > 64 or len({row['sn'] for row in catalog}) != len(catalog)
                or any(type(row['sdk_index']) is not int or not 0 <= row['sdk_index'] < 8 for row in catalog)):
            raise ValueError('bounded unique legacy SDK inventory required')
        self.pending = deque()
        self.children = {row['sn'].encode(): row for row in catalog}
        self.cache, self.encoded = {}, {}
        self.buffers = {(sn, sid): C.create_string_buffer(1024)
                        for sn, row in self.children.items() for sid in row['services']}
        for sn, snapshot in snapshots.items():
            self.update(sn, snapshot)
        @C.CFUNCTYPE(None, C.c_char_p, C.c_char_p)
        def action(sn, payload):
            state['action_callbacks'] = state.get('action_callbacks', 0) + 1
            if sn not in self.encoded or not payload:
                return
            # SDK线程栈很小；回调禁止HTTP、JSON转换或再次调用SDK。
            if len(self.pending) < 32:
                self.pending.append((sn, payload))
            else:
                state['write_rejected'] = state.get('write_rejected', 0) + 1
        @C.CFUNCTYPE(C.c_int, C.c_char_p)
        def kind(sn):
            return self.children[sn]['sdk_index'] if sn in self.children else -1
        @C.CFUNCTYPE(C.c_int, C.c_char_p, C.c_char_p)
        def status(sn, sid):
            if sn not in self.children or not sid:
                return 0
            try:
                values = self.cache.get(sn, {}).get(sid.decode(), {})
                return int(next(iter(values.values()), 0))
            except Exception:
                return 0
        @C.CFUNCTYPE(None, C.c_int)
        def notify(value):
            state['last_notification'] = value
        @C.CFUNCTYPE(C.c_void_p, C.c_char_p, C.c_char_p)
        def get(sn, sid):
            try:
                name = sid.decode() if sid else ''
                if (sn, name) not in self.buffers:
                    state['unsupported_read_count'] = state.get('unsupported_read_count', 0) + 1
                    return 0
                self.buffers[sn, name].value = self.encoded[sn][name]
                return C.addressof(self.buffers[sn, name])
            except Exception:
                return 0
        @C.CFUNCTYPE(C.c_int)
        def pin():
            state['pin_callbacks'] = state.get('pin_callbacks', 0) + 1
            return int(gateway['pin']) if time.time() < gateway['pin_expires_at'] else 0
        self.action, self.kind, self.status = action, kind, status
        self.notify, self.get, self.pin = notify, get, pin

    def invalidate(self, sn):
        self.cache.pop(sn, None)
        self.encoded.pop(sn, None)

    def update(self, sn, snapshot):
        encoded = {sid: json.dumps(value, separators=(',', ':')).encode() for sid, value in snapshot.items()}
        if any(len(value) >= 1024 for value in encoded.values()):
            raise ValueError('legacy cached response too large')
        self.cache[sn], self.encoded[sn] = snapshot, encoded


class LegacyPublisher:
    """主线程同步每个旧子设备的在线状态；不注册、不解绑、不缓存离线值。"""
    def __init__(self, library, callbacks):
        self.library, self.callbacks = library, callbacks
        self.announced, self.reported = {}, {}

    def invalidate(self, sn):
        self.callbacks.invalidate(sn)
        self.reported = {key: value for key, value in self.reported.items() if key[0] != sn}

    def publish(self, root_online):
        if not root_online:
            self.announced.clear()
            self.reported.clear()
            return
        for sn in self.callbacks.children:
            available = sn in self.callbacks.encoded
            if self.announced.get(sn) != available:
                self.library.HilinkSyncBrgDevStatus(sn, int(available))
                self.announced[sn] = available
            if not available:
                continue
            for sid, value in self.callbacks.encoded[sn].items():
                if self.reported.get((sn, sid)) != value:
                    self.library.UpdateHAStatus(sn, sid.encode(), value)
                    self.reported[sn, sid] = value
