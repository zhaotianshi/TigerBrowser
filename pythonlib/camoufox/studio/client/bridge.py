"""The QWebChannel object that lets the server's Vue pages run checked-out profiles on this computer."""

import asyncio
import json
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from ..client_releases import MINIMUM_PROTOCOL, PROTOCOL
from ..engine import StudioError
from ..kernel_cache import host_platform
from ..store import ValidationError, validate_id
from ..workspace import WorkspaceLockError
from . import update
from .local import LocalClient
from .sync import SyncError


def _reply(value=None, error=None):
    return json.dumps({'error': error} if error else {'ok': value})


def _code(error):
    if isinstance(error, SyncError):
        return error.code
    if isinstance(error, ValidationError) and str(error).isidentifier():
        return str(error)
    if isinstance(error, WorkspaceLockError):
        return 'client_workspace_locked'
    if isinstance(error, StudioError):
        return 'local_launch_failed'
    return 'operation_failed'


class Bridge(QObject):
    """Accepts only profile IDs, lease responses and fixed local setting fields; never file paths."""

    localChanged = Signal(str, str)
    updateChanged = Signal(str)
    _notified = Signal(str, str)
    _updated = Signal(str)
    _downloaded = Signal(str)

    def __init__(self, base, origin, loop, *, allowed, client_factory=LocalClient):
        super().__init__()
        self.base, self.origin, self.loop = base, origin, loop
        # The channel cannot name its caller, so every call rechecks the page's current origin.
        self.allowed = allowed
        self.client_factory = client_factory
        self.client = None
        self._heartbeat = None
        self.cookie = ''
        # A verified installer the shell starts after local runs are returned and the cache is released.
        self.pending_install = None
        self._notified.connect(self.localChanged)
        self._updated.connect(self.updateChanged)
        self._downloaded.connect(self._install)

    def set_session_cookie(self, cookie):
        """The web view's login cookie authorizes local automation; logout or expiry withdraws it."""
        self.cookie = cookie
        if self.client is not None:
            self._configure()

    def _configure(self):
        future = asyncio.run_coroutine_threadsafe(self.client.configure_automation(self.cookie), self.loop)
        future.add_done_callback(lambda result: result.exception())

    def _notify(self, profile_id, state):
        # Called on the browser loop; the queued signal reaches the page on the Qt thread.
        self._notified.emit(profile_id or '', json.dumps(state))

    def _on_loop(self, function, timeout=10):
        """Read state owned by the browser loop without racing its updates."""
        async def call():
            return function()
        return asyncio.run_coroutine_threadsafe(call(), self.loop).result(timeout)

    def _submit(self, coroutine, profile_id):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)

        def done(result):
            try:
                result.result()
            except Exception as error:
                self._notify(profile_id, {**self.client.status(profile_id), 'error_code': _code(error)})

        future.add_done_callback(done)

    def _attached(self):
        if self.client is None:
            raise SyncError('client_not_attached')
        return self.client

    def _call(self, function):
        if not self.allowed():
            return _reply(error='origin_denied')
        try:
            return _reply(function())
        except Exception as error:
            return _reply(error=_code(error))

    @Slot(result=str)
    def capabilities(self):
        # The page compares these with GET /client/update, which it reads with its own login.
        return self._call(lambda: {'protocol': PROTOCOL, 'minimum_protocol': MINIMUM_PROTOCOL,
                                   'platform': host_platform(), 'version': update.current_version(),
                                   'packaged': update.packaged_app() is not None,
                                   'attached': self.client is not None})

    @Slot(str, result=str)
    def attach(self, workspace_id):
        """Open the cache for the logged-in server; its ID comes from /me and never changes for that server."""
        def attach():
            validate_id(workspace_id)
            if self.client is not None:
                if self.client.lock.path.parent.name != workspace_id:
                    raise SyncError('workspace_changed')
                return self.client.device()
            self.client = self.client_factory(self.base, self.origin, workspace_id, self._notify)
            self._heartbeat = asyncio.run_coroutine_threadsafe(self.client.run(), self.loop)
            self._configure()
            return self.client.device()
        return self._call(attach)

    @Slot(str, str, result=str)
    def launchLocal(self, profile_id, lease):
        """Start a profile here; `lease` is the check-out response, or empty to continue a held lease."""
        def launch():
            client = self._attached()
            validate_id(profile_id)
            data = json.loads(lease) if lease else {}
            if not isinstance(data, dict) or set(data) - {'checkout', 'token'}:
                raise SyncError('invalid_request')
            self._submit(client.launch(profile_id, data.get('checkout'), data.get('token')), profile_id)
            return True
        return self._call(launch)

    @Slot(str, result=str)
    def stopLocal(self, profile_id):
        def stop():
            self._submit(self._attached().stop(validate_id(profile_id)), profile_id)
            return True
        return self._call(stop)

    @Slot(str, result=str)
    def cancelLocal(self, profile_id):
        def cancel():
            self._submit(self._attached().cancel(validate_id(profile_id)), profile_id)
            return True
        return self._call(cancel)

    @Slot(result=str)
    def localStates(self):
        return self._call(lambda: self._on_loop(self._attached().local_states))

    @Slot(result=str)
    def pendingReturns(self):
        def pending():
            client = self._attached()
            states = self._on_loop(client.local_states)
            return {'profiles': {key: value for key, value in states.items() if value['lease'] != 'ready'},
                    'stale': client.stale_copies()}
        return self._call(pending)

    @Slot(str, result=str)
    def openDataFolder(self, profile_id):
        def open_folder():
            folder = self._attached().data_folder(profile_id)
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))):
                raise SyncError('operation_failed')
            return True
        return self._call(open_folder)

    @Slot(result=str)
    def localSettings(self):
        return self._call(lambda: self._attached().local_settings())

    @Slot(str, result=str)
    def saveLocalSettings(self, settings):
        def save():
            saved = self._attached().save_local_settings(json.loads(settings))
            self._configure()
            return saved
        return self._call(save)

    @Slot(result=str)
    def automationStatus(self):
        return self._call(lambda: self._on_loop(self._attached().automation_status))

    def _cookie(self):
        if not self.cookie:
            raise SyncError('session_expired')
        return self.cookie

    def _in_background(self, function, done):
        """Network work for updates runs off the Qt thread; its outcome arrives as updateChanged."""
        cookie = self._cookie()
        future = asyncio.run_coroutine_threadsafe(asyncio.to_thread(function, cookie), self.loop)

        def finished(result):
            try:
                done(result.result())
            except Exception as error:
                self._updated.emit(json.dumps({'state': 'error', 'error': _code(error)}))

        future.add_done_callback(finished)
        return True

    @Slot(result=str)
    def installUpdate(self):
        """Download and verify the installer; it starts only after every local browser has stopped."""
        def download(cookie):
            package = update.check(self.origin, cookie)['package']
            if package is None:
                raise SyncError('client_update_unavailable')
            return update.download(self.origin, cookie, package, Path(self.base) / 'updates')

        def start():
            self._cookie()
            if update.packaged_app() is None:
                raise SyncError('client_update_unsupported')
            if self.running():
                raise SyncError('local_runs_active')
            self._updated.emit(json.dumps({'state': 'downloading'}))
            return self._in_background(download, lambda path: self._downloaded.emit(str(path)))
        return self._call(start)

    def _install(self, path):
        # Runs on the Qt thread; a profile started during the download keeps the client open.
        if self.running():
            self._updated.emit(json.dumps({'state': 'error', 'error': 'local_runs_active'}))
            return
        self.pending_install = Path(path)
        self._updated.emit(json.dumps({'state': 'installing'}))
        QCoreApplication.quit()

    def running(self):
        return self.client is not None and self._on_loop(lambda: bool(self.client.engine.sessions))

    async def shutdown(self):
        if self._heartbeat is not None:
            self._heartbeat.cancel()
        if self.client is not None:
            await self.client.shutdown()
