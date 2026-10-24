"""后端传输边界：实体白名单、REST 实验入口及 HA 原生服务调用入口。"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .configuration import integer, unique_object
from ..availability import SourceUnavailable
from ..ha_transport import ScopedTransport, HomeAssistantTransport

_LOGGER = logging.getLogger(__name__)




class RESTTransport(ScopedTransport):
    def __init__(self, url, token_file, audit=None):
        if not url.startswith(("http://", "https://")):
            raise ValueError("invalid HA URL")
        self.url = url.rstrip("/")
        self.token = Path(token_file).read_text().strip()
        self.audit = audit
        self.allowed = frozenset()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.cached = None
        self.cached_at = 0

    def request(self, path, data=None):
        request = urllib.request.Request(self.url + "/api/" + path,
            data=None if data is None else json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        with self.opener.open(request, timeout=4) as response:
            return json.load(response)

    def states(self):
        if self.cached is None or time.monotonic() - self.cached_at >= .5:
            self.cached = self.filter_states(self.request("states"))
            self.cached_at = time.monotonic()
        return self.cached

    def call(self, domain, action, entity, **values):
        self.validate_call(domain, action, entity, values)
        self.request(f"services/{domain}/{action}", {"entity_id": entity, **values})
        self.cached = None
        if self.audit:
            fd = os.open(self.audit, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(json.dumps({"time": time.time(), "domain": domain, "action": action,
                                         "entity": entity, "values": values}, ensure_ascii=False) + "\n")




def make_server(router, port, read_only=False):
    """保持已验证 C 子设备接口；仅回环监听，拒绝未声明的 key/SID/实体。"""
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            started = time.monotonic()
            queued = entered = started
            operation, phase = "unknown", "decode"
            request_id = self.headers.get("X-HWHB-Request-Id", "0")
            if not request_id.isascii() or not request_id.isdecimal() or len(request_id) > 20:
                request_id = "0"
            try:
                if self.path != "/bridge":
                    raise ValueError("unknown endpoint")
                size = integer(int(self.headers.get("Content-Length", "0")), 1, 8192)
                self.connection.settimeout(15)
                request = json.loads(self.rfile.read(size), object_pairs_hook=unique_object)
                if not isinstance(request, dict) or set(request) not in ({"key"}, {"key", "sid", "data"}):
                    raise ValueError("invalid request fields")
                if read_only and "data" in request:
                    raise ValueError("read-only backend")
                operation = "write" if "data" in request else "read"
                phase = "dispatch"
                queued = time.monotonic()
                with lock:
                    entered = time.monotonic()
                    state = router.write(request["key"], request["sid"], request["data"]) if "data" in request else router.snapshot(request["key"])
                response = {"ok": True, "available": True, "state": state}
            except SourceUnavailable:
                response = {"ok": False, "available": False, "error": "SourceUnavailable"}
            except Exception as error:
                response = {"ok": False, "error": type(error).__name__}
            if operation == "write" or not response["ok"] and response.get("error") != "SourceUnavailable":
                log = _LOGGER.info if response["ok"] else _LOGGER.warning
                log("bridge_result %s", json.dumps({
                    "request_id": request_id, "operation": operation,
                    "phase": phase, "ok": response["ok"],
                    "error_type": response.get("error", "none"),
                    "queue_ms": round((entered - queued) * 1000),
                    "elapsed_ms": round((time.monotonic() - started) * 1000),
                }, separators=(",", ":")))
            payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(router, port, read_only=False):
    with make_server(router, port, read_only) as server:
        server.serve_forever()
