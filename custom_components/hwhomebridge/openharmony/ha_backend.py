"""HA 负责原生服务路由和独立 C 子进程生命周期；停止时保留所有配对文件。"""
import asyncio
import json
import logging
import os
import socket
import threading

from .transport import HomeAssistantTransport, make_server

_LOGGER = logging.getLogger(__name__)


class HAOpenHarmonyBackend:
    def __init__(self, hass, deployment):
        self.hass = hass
        self.deployment = deployment
        self.process = None
        self.server = None
        self.thread = None
        self.log = None
        self.stopping = False
        self.stop_lock = asyncio.Lock()
        self.watch_task = None

    def watch(self, entry_id):
        async def monitor():
            await self.process.wait()
            if not self.stopping:
                _LOGGER.error("OpenHarmony C exited (%s); reloading its entry only", self.process.returncode)
                self.hass.async_create_task(self.hass.config_entries.async_reload(entry_id))
        self.watch_task = self.hass.async_create_background_task(monitor(), "OpenHarmony process monitor")

    async def start(self):
        spec = self.deployment
        transport = HomeAssistantTransport(self.hass, writable=spec.router.write_entities)
        transport.bind({entity for roles in spec.router.roles.values() for entity in roles.values()})
        spec.router.transport = transport
        try:
            # 先检查 LAN 端口，失败时不启动 C，也不影响现有桥接。
            def check_lan_port():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    probe.bind(("0.0.0.0", spec.lan_port))
            await self.hass.async_add_executor_job(check_lan_port)
            # 工作线程通过 HA 主循环查询状态；缺失实体时不启动 C。
            for key in spec.router.devices:
                await self.hass.async_add_executor_job(spec.router.snapshot, key)
            self.server = await self.hass.async_add_executor_job(make_server, spec.router, spec.port)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="openharmony-http")
            self.thread.start()
            self.log = await self.hass.async_add_executor_job(self._open_log)
            self.process = await asyncio.create_subprocess_exec(str(spec.binary), str(spec.runtime), "--live", str(spec.gateway),
                env={**os.environ, "POC_INTERFACE": spec.interface, "POC_CLOUD_STATUS": "1",
                     "POC_HA_PROXY_PORT": str(spec.port), "POC_LAN_PORT": str(spec.lan_port)},
                stdin=asyncio.subprocess.DEVNULL, stdout=self.log, stderr=self.log)
            for _ in range(25):
                await asyncio.sleep(.2)
                if self.process.returncode is not None:
                    raise RuntimeError("OpenHarmony C exited during startup")
                if await self.hass.async_add_executor_job(self._sdk_started):
                    return
            raise TimeoutError("OpenHarmony SDK did not confirm startup")
        except BaseException:
            await self.stop()
            raise

    def _open_log(self):
        path = self.deployment.runtime / "ha-sdk.private.log"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        self.log_offset = os.fstat(fd).st_size
        return os.fdopen(fd, "ab", buffering=0)

    def _sdk_started(self):
        with (self.deployment.runtime / "ha-sdk.private.log").open("rb") as stream:
            stream.seek(self.log_offset)
            lines = stream.read().decode(errors="replace").splitlines()
        return any(json.loads(line[4:]).get("event") == "sdk_start" and json.loads(line[4:]).get("result") == 0
                   for line in lines if line.startswith("POC "))

    async def stop(self):
        async with self.stop_lock:
            await self._stop()

    async def _stop(self):
        self.stopping = True
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.watch_task:
            self.watch_task.cancel()
            await asyncio.gather(self.watch_task, return_exceptions=True)
            self.watch_task = None
        if self.server:
            await self.hass.async_add_executor_job(self.server.shutdown)
            await self.hass.async_add_executor_job(self.server.server_close)
            self.server = None
        if self.thread:
            await self.hass.async_add_executor_job(self.thread.join, 2)
            self.thread = None
        if self.log:
            self.log.close()
            self.log = None
