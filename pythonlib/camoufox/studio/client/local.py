"""Local runs of checked-out profiles: sync before start, return after stop, without Qt."""

import asyncio
import socket
from pathlib import Path
from uuid import uuid4

from platformdirs import user_data_path

from ..engine import BrowserEngine, StudioError, public_error
from ..kernel_cache import host_platform
from ..store import ValidationError, validate_id
from ..team.transport import exact_origin
from ..workspace import WorkspaceLock
from .sync import ClientStore, HttpTransport, SyncEngine, SyncError

HEARTBEAT = 15
LOCAL_SETTINGS = frozenset({'max_running', 'automation_enabled'})


def default_base():
    return user_data_path('TigerBrowser', appauthor=False)


class LocalClient:
    """One server's cache workspace on this computer; every coroutine runs on the same event loop."""

    def __init__(self, base, origin, workspace_id, notify, *, engine_factory=BrowserEngine):
        exact_origin(origin)
        self.origin, self.notify = origin, notify
        self.automation_error, self._automation_cookie = None, ''
        self._automation_lock = asyncio.Lock()
        self.lock = WorkspaceLock(Path(base) / 'servers' / validate_id(workspace_id))
        self.lock.acquire()
        try:
            self.store = ClientStore(self.lock.path.parent)
            self.store.bind_origin(origin)
            self.sync = SyncEngine(self.store, HttpTransport(origin))
            self.engine = engine_factory(self.store, self._state, self._message, self._failure)
        except BaseException:
            self.lock.release()
            raise
        self.engine.authorize_start = self._authorize
        self.states, self.errors, self._locks, self._tasks = {}, {}, {}, set()
        self._operations, self._closing, self._shutdown_task = set(), False, None

    def _message(self, text):
        self.notify(None, {'message': text})

    def _failure(self, text):
        self.notify(None, {'error': text})

    def _lock(self, profile_id):
        return self._locks.setdefault(profile_id, asyncio.Lock())

    def _spawn(self, coroutine, *, foreground=False):
        def finished(result):
            self._tasks.discard(result)
            self._operations.discard(result)
            # A cancelled heartbeat caller may no longer retrieve its shielded worker's error.
            if not result.cancelled():
                result.exception()

        task = asyncio.get_running_loop().create_task(coroutine)
        (self._operations if foreground else self._tasks).add(task)
        task.add_done_callback(finished)
        return task

    async def _operation(self, function, *args, **kwargs):
        if self._closing:
            raise StudioError('客户端正在退出。')
        return await asyncio.shield(self._spawn(function(*args, **kwargs), foreground=True))

    async def _leases(self):
        return await asyncio.to_thread(self.sync.leases)

    async def _authorize(self, profile_id):
        # Checked before and after preparation, so a revocation during startup stops the launch.
        lease = (await self._leases()).get(profile_id)
        if lease is None or lease['state'] != 'ready' or not lease.get('launched'):
            raise StudioError('此环境没有签出到本机，或已被管理员强制解锁。')

    def _state(self, profile_id, state):
        self.states[profile_id] = state
        self.notify(profile_id, self.status(profile_id))
        if state['state'] in ('stopped', 'error'):
            self._spawn(self._resume(profile_id, stopped=True))

    async def _resume(self, profile_id, *, stopped=False):
        async with self._lock(profile_id):
            # A failed teardown also fences later heartbeat retries, not just the stop callback.
            if self.engine.recovery_required or profile_id in self.engine.sessions:
                return
            lease = (await self._leases()).get(profile_id)
            if lease is None:
                return
            try:
                if stopped and lease['state'] == 'ready' and lease.get('launched'):
                    await asyncio.to_thread(self.sync.finish, profile_id)
                await asyncio.to_thread(self.sync.resume_profile, profile_id)
                self.errors.pop(profile_id, None)
            except Exception as error:
                # Background retries record a fixed code instead of ending the retry loop.
                self.errors[profile_id] = error.code if isinstance(error, SyncError) else 'operation_failed'
        self.notify(profile_id, self.status(profile_id))

    async def launch(self, profile_id, checkout=None, token=None, *, headless=None):
        """Mirror a new lease or continue the one held here, then start the browser locally."""
        await self._operation(self._launch, profile_id, checkout, token, headless=headless)

    async def _launch(self, profile_id, checkout=None, token=None, *, headless=None, main_world_eval=None):
        validate_id(profile_id)
        if profile_id in self.engine.sessions:
            raise StudioError('此环境已在本机运行或启动中。')
        async with self._lock(profile_id):
            lease = (await self._leases()).get(profile_id)
            try:
                if checkout is not None:
                    if checkout.get('profile_id') != profile_id:
                        raise SyncError('checkout_denied')
                    await asyncio.to_thread(self.sync.checkout, checkout, token)
                elif lease is None:
                    raise SyncError('not_checked_out')
                elif lease['state'] in ('syncing', 'revoked'):
                    await asyncio.to_thread(self.sync.resume_profile, profile_id)
                await asyncio.to_thread(self.sync.begin_local, profile_id)
                self.errors.pop(profile_id, None)
            except SyncError as error:
                self.errors[profile_id] = error.code
                self.notify(profile_id, self.status(profile_id))
                raise
        overrides = {'main_world_eval': True} if main_world_eval is True else {}
        await self.engine.start(profile_id, headless=headless, **overrides)
        return self.engine.sessions[profile_id]

    async def configure_automation(self, cookie):
        """Serve local automation only while the setting is on and the member's server login is valid."""
        return await self._operation(self._configure_automation, cookie)

    async def _configure_automation(self, cookie):
        from .automation import ServerSession
        async with self._automation_lock:
            automation = self.engine.automation
            wanted = cookie if cookie and self.store.get_settings()['automation_enabled'] else ''
            # A repeated cookie notification must not rotate the token callers already read.
            if wanted and wanted == self._automation_cookie and automation.server:
                return
            await self.engine.configure_automation(False)
            self.automation_error, self._automation_cookie = None, ''
            if not wanted:
                return
            session = ServerSession(self.origin, wanted)
            try:
                await asyncio.to_thread(session.renew_local_automation)
            except SyncError as error:
                self.automation_error = error.code
                return
            automation.access = session
            automation.start_profile = self._automation_start
            automation.create_profile = self._automation_create
            await self.engine.configure_automation(True)
            self._automation_cookie = wanted

    async def _automation_start(self, profile_id, headless, *, main_world_eval=None):
        return await self._operation(self._start_automation, profile_id, headless, main_world_eval=main_world_eval)

    async def _start_automation(self, profile_id, headless, *, main_world_eval=None):
        from .automation import automation_error
        try:
            if profile_id in await self._leases():
                return await self._launch(profile_id, headless=headless, main_world_eval=main_world_eval)
            session = self.engine.automation.access
            created = await asyncio.to_thread(session.checkout, profile_id, self.device())
            return await self._launch(profile_id, created['checkout'], created['token'],
                                      headless=headless, main_world_eval=main_world_eval)
        except SyncError as error:
            raise automation_error(error.code) from None

    async def _automation_create(self, data):
        return await self._operation(self._create_automation, data)

    async def _create_automation(self, data):
        from .automation import automation_error
        try:
            return await asyncio.to_thread(self.engine.automation.access.create_profile, data)
        except SyncError as error:
            if error.code == 'invalid_request':
                raise ValidationError('invalid_request') from None
            raise automation_error(error.code) from None

    def automation_status(self):
        automation = self.engine.automation
        return {'enabled': bool(automation.server), 'connection_file': str(automation.discovery_path),
                'error': self.automation_error}

    async def stop(self, profile_id):
        await self.engine.stop(validate_id(profile_id))

    async def cancel(self, profile_id):
        return await self._operation(self._cancel, profile_id)

    async def _cancel(self, profile_id):
        async with self._lock(validate_id(profile_id)):
            await asyncio.to_thread(self.sync.cancel, profile_id)
            self.errors.pop(profile_id, None)
        self.notify(profile_id, self.status(profile_id))

    async def run(self):
        """Heartbeats for running profiles; stopped ones keep retrying their download or return."""
        while not self._closing:
            for profile_id, lease in (await self._leases()).items():
                if self._closing:
                    return
                if profile_id in self.engine.sessions:
                    try:
                        beat = await asyncio.shield(self._spawn(asyncio.to_thread(self.sync.heartbeat, profile_id)))
                        self.errors.pop(profile_id, None)
                        if beat['release_requested']:
                            self._message('管理员收回了本机运行授权，环境将关闭并签回。')
                            await self.engine.stop(profile_id)
                    except Exception as error:
                        # A running browser keeps going offline; only a revocation stops it.
                        code = error.code if isinstance(error, SyncError) else 'operation_failed'
                        self.errors[profile_id] = code
                        if code == 'checkout_revoked':
                            await self.engine.stop(profile_id)
                elif lease['state'] != 'ready':
                    await asyncio.shield(self._spawn(self._resume(profile_id)))
            await asyncio.sleep(HEARTBEAT)

    def status(self, profile_id):
        lease = self.sync.leases().get(profile_id)
        state = self.states.get(profile_id, {'state': 'stopped'})
        return {'browser': state['state'], 'error': state.get('error'),
                'lease': lease['state'] if lease else None, 'sync_error': self.errors.get(profile_id),
                'recovery_required': self.engine.recovery_required}

    def local_states(self):
        profiles = set(self.sync.leases()) | set(self.engine.sessions)
        return {profile_id: self.status(profile_id) for profile_id in sorted(profiles)}

    def device(self):
        """The identity this computer declares when checking out; it is stable for this server's cache."""
        record = self.store.metadata('client_device').get('')
        if record is None:
            record = {'device_id': str(uuid4()), 'device_name': socket.gethostname()[:128]}
            self.store.save_metadata('client_device', record)
        return {**record, 'platform': host_platform()}

    def stale_copies(self):
        return [{'checkout_id': checkout_id, **record} for checkout_id, record in sorted(self.sync.stale().items())]

    def data_folder(self, profile_id):
        if validate_id(profile_id) not in self.sync.leases():
            raise SyncError('not_checked_out')
        return self.store.profile_dir(profile_id)

    def local_settings(self):
        settings = self.store.get_settings()
        return {key: settings[key] for key in sorted(LOCAL_SETTINGS)}

    def save_local_settings(self, data):
        if not isinstance(data, dict) or not set(data) <= LOCAL_SETTINGS:
            raise SyncError('invalid_request')
        self.store.save_settings(data)
        return self.local_settings()

    async def shutdown(self):
        """Stop browsers, try every pending return once, and release the cache only after a clean stop."""
        if self._shutdown_task is None:
            self._closing = True
            self._shutdown_task = asyncio.create_task(self._shutdown())
            self._shutdown_task.add_done_callback(lambda task: None if task.cancelled() else task.exception())
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self):
        # Admitted checkouts must persist their lease before browser teardown and the final return.
        await asyncio.gather(*self._operations, return_exceptions=True)
        try:
            await self.engine.shutdown()
        except Exception as error:
            raise StudioError(public_error(error)) from None
        # Cancelling a to_thread caller cannot stop its upload; wait before retrying or unlocking.
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await asyncio.to_thread(self.sync.resume)
        self.lock.release()
