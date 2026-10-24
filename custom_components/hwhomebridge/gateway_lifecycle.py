"""一个配置条目和身份的进程生命周期；失败只退回同一身份已验证的引擎。"""
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import sys
import threading

from .availability import SourceUnavailable
from .gateway_router import GatewayRouter
from .openharmony.transport import make_server

_LOGGER = logging.getLogger(__name__)


def save_state(path, data):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class GatewayLifecycle:
    def __init__(self, hass, spec, entry_id):
        self.hass, self.deployment, self.entry_id = hass, spec, entry_id
        self.process = self.server = self.thread = self.log = self.owner = self.monitor = None
        self.active = 'disabled'
        self.stopping = False
        self.guard = asyncio.Lock()
        self.journal_path = spec.directory / 'lifecycle.json'
        self.state = {'version': 1, 'identity': spec.identity, 'last_good': None, 'active': 'disabled'}
        if self.journal_path.exists():
            self.state = spec.read('lifecycle.json')
            if (self.state['identity'] != spec.identity or self.state.get('version') != 1
                    or self.state.get('last_good') not in (None, 'legacy', 'openharmony')
                    or self.state.get('active') not in ('disabled', 'legacy', 'openharmony')):
                raise ValueError('lifecycle journal belongs to a different gateway')

    async def start(self, desired, revision, management=None):
        fd = os.open(self.deployment.directory / 'engine-owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.owner = os.fdopen(fd, 'a')
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if management is None:
                self.router = await GatewayRouter.create(self.hass, self.deployment, self.entry_id)
            else:
                self.router = await GatewayRouter.create(self.hass, self.deployment, self.entry_id, management)
            if management is not None:
                from .gateway_management import prepare_runtime
                self.worker_runtime = await self.hass.async_add_executor_job(prepare_runtime,
                    self.deployment, self.router.catalog)
            retry = revision != self.state.get('revision')
            interrupted = self.state.get('pending')
            failed = self.state.get('failed_backend') or (interrupted or {}).get('to')
            if not retry and failed:
                desired = (self.state.get('last_good') or 'disabled') if interrupted or self.state['active'] != 'disabled' else 'disabled'
            if desired != 'disabled':
                for row in getattr(self.router, 'catalog', self.deployment.catalog):
                    try:
                        await self.hass.async_add_executor_job(self.router.snapshot, row['key'])
                    except SourceUnavailable:
                        _LOGGER.info('Gateway child source unavailable at startup; isolated until recovery')
                self.server = await self.hass.async_add_executor_job(make_server, self.router, self.deployment.port)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name='hwhomebridge-gateway')
                self.thread.start()
            self.state['revision'] = revision
            await self.switch(desired, explicit=retry)
            if not retry and failed:
                self.state['failed_backend'] = failed
                await self._save()
        except BaseException:
            await self.stop()
            raise

    async def switch(self, target, *, explicit=False):
        async with self.guard:
            previous = self.active if self.active != 'disabled' else self.state.get('last_good')
            if target == 'disabled':
                await self._stop_engine()
                self.active = 'disabled'
                self.state.update(active='disabled', pending=None)
                await self._save()
                return
            # 文件/身份预检失败发生在停掉已工作的引擎之前。
            try:
                await self.hass.async_add_executor_job(self.deployment.verify_engine, target)
            except Exception:
                if self.active != 'disabled':
                    raise
                if previous and previous != target:
                    await self._launch(previous)
                    self.state.update(active=previous, failed_backend=target, pending=None)
                    await self._save()
                    return
                raise
            self.state['pending'] = {'from': previous, 'to': target}
            await self._save()
            await self._stop_engine()
            try:
                await self._launch(target)
            except Exception:
                await self._stop_engine()
                self.state.update(failed_backend=target, pending=None)
                if previous and previous != target:
                    await self.hass.async_add_executor_job(self.deployment.verify_engine, previous)
                    await self._launch(previous)
                    self.state['active'] = previous
                    await self._save()
                    return
                await self._save()
                raise
            self.state.update(active=target, last_good=target, failed_backend=None, pending=None)
            await self._save()

    async def _save(self):
        await self.hass.async_add_executor_job(save_state, self.journal_path, self.state)
        current = self.hass.data.get('hwhomebridge', {}).get(self.entry_id)
        if current and current.get('runtime') is self:
            current['backend'] = self.active

    async def _launch(self, mode):
        spec = self.deployment
        await self.hass.async_add_executor_job(spec.verify_engine, mode)
        path = spec.directory / (mode + '.private.log')
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.log_offset = os.fstat(fd).st_size
        self.log = os.fdopen(fd, 'ab', buffering=0)
        (spec.directory / 'legacy-status.json').unlink(missing_ok=True)
        self.process = await asyncio.create_subprocess_exec(sys.executable,
            str(Path(__file__).with_name('gateway_worker.py')), mode, str(spec.directory),
            env={**os.environ, 'HWHB_PARENT_PID': str(os.getpid()), 'HWHB_LEGACY_LIBRARY': str(spec.legacy_library),
                 'HWHB_ACTIVE_RUNTIME': str(getattr(self, 'worker_runtime', spec.runtime))},
            pass_fds=(self.owner.fileno(),), stdin=asyncio.subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        timeout = spec.settings.get('startup_timeout', 45)
        if type(timeout) is not int or not 5 <= timeout <= 120:
            raise ValueError('bounded startup timeout required')
        for _ in range(timeout * 5):
            await asyncio.sleep(.2)
            if self.process.returncode is not None:
                raise RuntimeError('selected native engine exited')
            if await self.hass.async_add_executor_job(self._ready, mode):
                self.active = mode
                self.monitor = self.hass.async_create_background_task(self._watch(self.process, mode), 'single gateway process monitor')
                return
        raise TimeoutError('selected paired engine did not come online')

    def _ready(self, mode):
        spec = self.deployment
        if mode == 'legacy':
            try:
                status = spec.read('legacy-status.json')
            except (OSError, ValueError):
                return False
            return status['sdk_started'] and status['native_SN_confirmed'] and status['device_status'] == 1
        with (spec.directory / 'openharmony.private.log').open('rb') as stream:
            stream.seek(self.log_offset)
            parent_online = False
            children_synced = set()
            for line in stream.read().decode(errors='replace').splitlines():
                try:
                    if line.startswith('POC '):
                        parent_online |= json.loads(line[4:]).get('event') == 'cloud_online'
                    elif line.startswith('HA '):
                        response = json.loads(line[3:])
                        if (response.get('phase') == 3 and response.get('accepted') is True
                                and response.get('code') == 0 and response.get('failures') == 0):
                            children_synced.add(response['key'])
                except (ValueError, KeyError):
                    continue
        catalog = getattr(getattr(self, 'router', None), 'catalog', spec.catalog)
        return parent_online and {row['key'] for row in catalog} <= children_synced

    async def _watch(self, process, mode):
        await process.wait()
        if self.stopping or self.process is not process:
            return
        async with self.guard:
            if self.stopping or self.process is not process:
                return
            self.monitor = None
            await self._stop_engine()
            self.state.update(failed_backend=mode, pending=None)
            fallback = 'legacy' if mode == 'openharmony' else None
            if fallback:
                try:
                    await self.hass.async_add_executor_job(self.deployment.verify_engine, fallback)
                    await self._launch(fallback)
                    self.state.update(active=fallback, last_good=fallback)
                except Exception:
                    await self._stop_engine()
                    self.active = 'disabled'
                    self.state['active'] = 'disabled'
                    _LOGGER.error('Gateway fallback failed; paired files retained, no new identity created')
            else:
                self.active = 'disabled'
                self.state['active'] = 'disabled'
            await self._save()

    async def _stop_engine(self):
        monitor, self.monitor = self.monitor, None
        if monitor and monitor is not asyncio.current_task():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 10)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

    async def stop(self):
        self.stopping = True
        async with self.guard:
            await self._stop_engine()
            if self.server:
                await self.hass.async_add_executor_job(self.server.shutdown)
                await self.hass.async_add_executor_job(self.server.server_close)
                self.server = None
            if self.thread:
                await self.hass.async_add_executor_job(self.thread.join, 2)
                self.thread = None
            if self.owner:
                self.owner.close()
                self.owner = None
