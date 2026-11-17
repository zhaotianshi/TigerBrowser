"""Single-owner authenticated ASGI management service for a local team workspace."""

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import shutil
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from uuid import uuid4

from anyio import CancelScope
from fastapi import FastAPI, Request, WebSocket
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

from ..access import MemberStore
from ..engine import BrowserEngine, StudioError
from ..kernels import kernel_id
from ..store import (PROFILE_FIELDS, PROXY_FIELDS, ProfileStore, ValidationError,
                     public_identity, validate_id)
from ..workspace import WorkspaceLock
from ..resources import blank_browser
from .repository import TeamRepository
from .control import ControlError, RemoteRun
from .native import KEYS

PREFIX = '/team/v1'
MAX_BODY = 65536
MAX_COOKIE_BODY = 10 * 1024 * 1024
MAX_CONFIGURATION_BODY = 16 * 1024 * 1024
MAX_PROXY_BODY = 5 * 1024 * 1024 + 65536
ASSETS = Path(__file__).with_name('web')
MEMBER_FIELDS = ('id', 'name', 'display', 'role', 'active', 'permission_version')
PROFILE_METADATA = ('id', 'name', 'group_name', 'tags', 'os', 'kernel', 'archived', 'revision')
ERROR_STATUS = {
    'session_expired': 401, 'invalid_credentials': 401, 'login_locked': 429,
    'access_denied': 403, 'admin_required': 403, 'role_permission_denied': 403,
    'profile_busy': 409, 'profile_archived': 409, 'revision_conflict': 409,
    'request_conflict': 409, 'run_conflict': 409, 'capacity_reached': 409,
    'service_busy': 409, 'recovery_required': 503,
    'profile_checked_out': 409, 'checkout_denied': 401, 'checkout_revoked': 409, 'checkout_closed': 409,
    'checkout_conflict': 409, 'checkout_incomplete': 409, 'checkout_committing': 409,
    'identity_present': 409, 'unsafe_profile_data': 409, 'invalid_kernel': 400, 'not_found': 404,
    'file_not_found': 404, 'invalid_archive_entry': 400, 'file_limit': 413, 'size_limit': 413,
    'kernel_not_cached': 404,
}
CHECKOUT_FIELDS = ('member_name', 'device_name', 'platform', 'created_at', 'heartbeat_at')


class TeamError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


def fields(data, required, optional=()):
    if not isinstance(data, dict) or set(data) - set(required) - set(optional) or set(required) - set(data):
        raise TeamError('invalid_request')
    return data


def public_command(command):
    return {key: command[key] for key in
            ('id', 'request_id', 'profile_id', 'run_id', 'operation', 'state', 'error', 'updated_at')}


def public_job(job):
    return {key: job[key] for key in
            ('id', 'request_id', 'kind', 'profile_id', 'state', 'result', 'error', 'created_at', 'updated_at')}


def public_profile(profile, session, run, proxy=None, checkout=None):
    item = {key: profile[key] for key in PROFILE_METADATA}
    item.update(locale=profile['locale'], headless=profile['headless'], proxy=proxy,
                last_launch_at=profile['last_launch_at'])
    item['permissions'] = (['checkout', 'manage', 'operate', 'view', 'watch'] if session['member']['role'] == 'admin'
                           else sorted(session['permissions'].get(profile['id'], [])))
    item['checked_out'] = ({'id': checkout['id'], 'state': checkout['state'],
                            **{key: checkout[key] for key in CHECKOUT_FIELDS},
                            'mine': checkout['member_id'] == session['member']['id']} if checkout else None)
    item['run'] = ({key: run[key] for key in ('id', 'state', 'mode', 'error', 'updated_at')}
                   if run else None)
    if run and 'display_backend' in run:
        item['run']['display_backend'] = run['display_backend']
    return item


class RateLimit:
    """Bounded per-peer windows, including unknown-member credential attempts."""

    def __init__(self):
        self.windows = OrderedDict()

    def check(self, peer, kind, limit):
        now = time.monotonic()
        while self.windows and next(iter(self.windows.values()))[0] + 60 <= now:
            self.windows.popitem(last=False)
        key = kind, peer
        if key not in self.windows:
            if len(self.windows) >= 1024:
                raise TeamError('rate_limited', 429)
            self.windows[key] = [now, 0]
        window = self.windows[key]
        if window[1] >= limit:
            raise TeamError('rate_limited', 429)
        window[1] += 1


class TeamService:
    def __init__(self, root, *, browser_mode='disabled', engine_factory=BrowserEngine, execution_boundary=None,
                 automation=None, kernel_root=None, staging_root=None, client_root=None):
        if browser_mode not in ('disabled', 'headless', 'desktop', 'remote'):
            raise ValueError('Unknown browser mode.')
        self.root = Path(root).expanduser().resolve()
        # Packages are not user data and backup rejects unknown files in the workspace root.
        if kernel_root is not None and Path(kernel_root).expanduser().resolve().is_relative_to(self.root):
            raise ValueError('Keep the kernel cache outside the workspace.')
        if client_root is not None and Path(client_root).expanduser().resolve().is_relative_to(self.root):
            raise ValueError('Keep the client installers outside the workspace.')
        self.kernel_root = kernel_root
        self.client_root = Path(client_root).expanduser().resolve() if client_root is not None else None
        self.kernel_cache = None
        self.staging_root = staging_root
        self.checkouts = None
        self.transfer_locks = {}
        self.mode = browser_mode
        self.engine_factory = engine_factory
        self.execution_boundary = execution_boundary
        if automation is not None and browser_mode == 'disabled':
            raise ValueError('Network automation requires an enabled browser execution mode.')
        self.automation_configuration = automation
        self.automation = self.api_keys = None
        self.owner = str(uuid4())
        self.lock = WorkspaceLock(self.root, boundary=execution_boundary, execution_owner=self.owner)
        self.store = self.repository = self.engine = None
        self.gate = asyncio.Lock()
        self.workers = asyncio.Semaphore(4)
        self.file_workers = asyncio.Semaphore(2)
        self.job_workers = asyncio.Semaphore(2)
        self.tasks = OrderedDict()
        self.jobs = OrderedDict()
        self.job_requests = {}
        self.profile_jobs = {}
        self.maintenance_job = None
        self.monitors = set()
        self.start_authorizations = {}
        self.connections = {}
        self.remote_connections = {}
        self.display_connections = {}
        self.viewers = {}
        self.remotes = {}
        self.closing = False
        self.failed = False
        self._closing = None
        self.rate = RateLimit()

    async def work(self, method, *args, **kwargs):
        return await self._run_in(self.workers, partial(method, *args, **kwargs))

    async def transfer_work(self, method, *args, **kwargs):
        # Hashing and copying profile data must not occupy the short-request workers.
        return await self._run_in(self.file_workers, partial(method, *args, **kwargs))

    async def _run_in(self, slots, call):
        # Cancellation cannot release a worker slot while its database/hash operation still runs.
        async with slots:
            task = asyncio.create_task(asyncio.to_thread(call))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

    async def open(self):
        native_factory = None
        if self.mode == 'remote':
            from .backend import select
            native_factory = select()
        if not (self.root / 'studio.sqlite3').is_file():
            raise StudioError('Select an existing locally initialized team workspace.')
        self.lock.acquire()
        try:
            self.store = await self.work(ProfileStore, self.root)
            if self.kernel_root is not None:
                from ..kernel_cache import KernelCache
                self.kernel_cache = await self.work(KernelCache, self.kernel_root)
            from ..api_keys import ApiKeys
            from ..checkouts import Checkouts, COMMIT
            self.api_keys = ApiKeys(self.store)
            self.repository = TeamRepository(self.store)
            # Clients key their local cache by this ID; a restored copy receives a new one.
            self.workspace_id = await self.work(self.store.workspace_id)
            if self.staging_root is not None:
                self.checkouts = await self.work(Checkouts, self.store, self.staging_root, self.kernel_cache)
                # Finish or roll forward interrupted returns before anything can read profile data.
                await self.work(self.checkouts.recover)
            elif await self.work(self.store.pending_commits, COMMIT):
                raise StudioError('Interrupted check-out returns require the original --staging-root.')
            if not await self.work(self.store.list_members):
                raise StudioError('Create the administrator with studio-team-init before serving this workspace.')
            unresolved = await self.work(self.repository.unresolved_runs)
        except BaseException:
            self.lock.release()
            raise
        if unresolved:
            # Even a manually removed workspace marker cannot erase durable browser ownership.
            raise StudioError('Unresolved execution records require manual recovery; workspace ownership is retained.')
        try:
            self.engine = self.engine_factory(self.store, lambda *_: None, lambda *_: None, lambda *_: None)
            self.engine.authorize_start = self._authorize_start
            if self.mode == 'remote':
                self.engine.native_factory = native_factory
            if self.automation_configuration is not None:
                from ..automation import AutomationBridge
                self.automation = AutomationBridge(self.engine,
                                                   start_profile=self.start_automation_profile,
                                                   create_profile=self.create_automation_profile,
                                                   restart_profile=self.restart_automation_profile,
                                                   recycle_profile=self.recycle_automation_profile,
                                                   can_recycle=self.can_recycle_automation_profile)
                options = dict(self.automation_configuration)
                cert, key = options.pop('cert', None), options.pop('key', None)
                if cert:
                    import ssl
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(cert, key)
                    options['ssl'] = context
                async def authenticate(token, peer):
                    return await self.work(self.api_keys.authenticate, token, peer)
                await self.automation.start_network(authenticate=authenticate, **options)
        except BaseException:
            if self.automation is not None:
                await self.automation.stop()
            self.lock.release()
            raise

    async def create_profile(self, member_store, data):
        async with self.gate:
            if self.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            return await self.work(member_store.save_profile, data)

    async def create_automation_profile(self, data):
        from ..automation import AutomationError
        try:
            return await self.create_profile(self.automation.access, data)
        except TeamError as error:
            raise AutomationError(error.code, '服务器暂时无法创建环境，请检查维护状态。', error.status) from None

    async def start_automation_profile(self, profile_id, headless, *, main_world_eval=None, maintenance=None):
        from ..automation import AutomationError
        mode = 'headless' if headless else self.mode
        if not headless and mode == 'headless':
            raise AutomationError('profile_mode_mismatch', '此服务只允许无头启动，请选择无头后台。', 409)
        try:
            command = await self.submit(self.automation.access, profile_id, 'start',
                                        {'request_id': str(uuid4()), 'mode': mode}, main_world_eval=main_world_eval,
                                        maintenance=maintenance)
            await asyncio.shield(self.tasks[command['id']])
            result = await self.work(self.repository.command, command['id'])
            if result['state'] != 'succeeded':
                if result['error'] == 'main_world_unsupported':
                    from ..engine import MainWorldUnsupportedError
                    raise AutomationError('main_world_unsupported', str(MainWorldUnsupportedError()), 409)
                raise AutomationError(result['error'] or 'profile_start_failed', '环境启动失败，请在控制台查看环境状态。', 409)
            return self.engine.sessions.get(profile_id)
        except (TeamError, ValidationError) as error:
            code = error.code if isinstance(error, TeamError) else str(error)
            raise AutomationError(code, '服务器无法启动该环境，请检查权限、运行模式和并发容量。', ERROR_STATUS.get(code, 409)) from None

    def can_recycle_automation_profile(self, profile_id, session, *, maintenance=None):
        remote = self.remotes.get(session.run_id)
        return (not self.closing and not self.failed and not self.engine.recovery_required
                and self.maintenance_job is None
                and (profile_id not in self.profile_jobs or self.profile_jobs[profile_id] is maintenance)
                and not (remote and remote.controller))

    async def recycle_automation_profile(self, profile_id, expected):
        return await self._restart_automation_profile(
            profile_id, expected, expected.headless,
            main_world_eval=expected.main_world_eval, idle=True)

    async def restart_automation_profile(self, profile_id, expected, headless):
        return await self._restart_automation_profile(
            profile_id, expected, headless, main_world_eval=True)

    async def _restart_automation_profile(self, profile_id, expected, headless, *, main_world_eval, idle=False):
        from ..automation import AutomationError
        access = self.automation.access
        maintenance = {'task': None}
        async with self.gate:
            await self.work(access.require_profile, profile_id, 'operate')
            if self.engine.sessions.get(profile_id) is not expected or expected.stop.is_set():
                raise AutomationError('session_changed', '环境会话已改变，未重启替换会话。', 409)
            if idle and (not self.can_recycle_automation_profile(profile_id, expected)
                         or not blank_browser(expected.context)):
                raise AutomationError('profile_busy', '环境仍有人工操作或标签页，未回收浏览器。', 409)
            run = await self.work(self.repository.latest_run, profile_id)
            if run is None or run['state'] != 'running' or run['id'] != expected.run_id:
                raise AutomationError('session_changed', '环境运行记录已改变，未重启替换会话。', 409)
            if profile_id in self.profile_jobs:
                raise AutomationError('profile_busy', '此环境正在维护，请稍后重试。', 409)
            self.profile_jobs[profile_id] = maintenance
        try:
            command = await self.submit(access, profile_id, 'stop',
                                        {'request_id': str(uuid4()), 'run_id': run['id']},
                                        maintenance=maintenance, require_idle=idle)
            await asyncio.shield(self.tasks[command['id']])
            result = await self.work(self.repository.command, command['id'])
            if result['state'] != 'succeeded':
                raise AutomationError(result['error'] or 'profile_stop_failed', '环境未能安全停止，未重新启动。', 409)
            return await self.start_automation_profile(profile_id, headless, main_world_eval=main_world_eval,
                                                       maintenance=maintenance)
        except (TeamError, ValidationError) as error:
            code = error.code if isinstance(error, TeamError) else str(error)
            raise AutomationError(code, '环境未能安全停止，请检查运行状态。', ERROR_STATUS.get(code, 409)) from None
        finally:
            # A cancelled callback can leave its shielded durable command running.
            # Neither that command nor the bridge fence may outlive the reservation.
            fence = (self.automation.recycles.get(profile_id)
                     or self.automation.restarts.get(profile_id))
            pending = [task for task in (maintenance['task'], fence) if task is not None]
            def release(_=None):
                if all(task.done() for task in pending) and self.profile_jobs.get(profile_id) is maintenance:
                    self.profile_jobs.pop(profile_id, None)
            for task in pending:
                if not task.done():
                    task.add_done_callback(release)
            release()

    async def access(self, cookie):
        session_id = await self.work(self.repository.session_id, cookie)
        session = await self.work(self.store.resolve_session, session_id) if session_id else None
        if not session:
            raise TeamError('session_expired', 401)
        return await self.work(MemberStore, self.store, session)

    async def snapshot(self, access):
        async with self.gate:
            def read():
                session = access.resolve()
                proxies = access.list_proxy_summaries()
                profiles = []
                for archived in (False, True):
                    for profile in access.list_profiles(archived):
                        run = self.repository.latest_run(profile['id'])
                        profiles.append(public_profile(profile, session, run, proxies.get(profile['proxy_id']),
                                                       self.repository.active_checkout(profile['id'])))
                access.resolve()
                return {'profiles': profiles}
            return await self.work(read)

    def read_profile(self, access, profile_id, *, detail=False):
        if detail:
            access.require_admin()
        try:
            profile = access.get_profile(profile_id)
        except KeyError:
            raise TeamError('not_found', 404) from None
        proxies = access.list_proxy_summaries()
        item = public_profile(profile, access.resolve(), self.repository.latest_run(profile_id),
                              proxies.get(profile['proxy_id']), self.repository.active_checkout(profile_id))
        if detail:
            item.update({key: profile[key] for key in PROFILE_FIELDS})
            item.update({key: profile[key] for key in ('created_at', 'updated_at', 'last_launch_at')})
            item['identity_present'] = profile['identity'] is not None
        access.resolve()
        return item

    async def remote_entry(self, access, profile_id):
        async with self.gate:
            profile = await self.work(self.read_profile, access, profile_id)
            run = profile['run']
            reason = None
            if 'watch' not in profile['permissions']:
                reason = 'watch_required'
            elif self.mode != 'remote':
                reason = 'remote_disabled'
            elif self.closing:
                reason = 'service_closing'
            elif self.failed or self.engine.recovery_required:
                reason = 'recovery_required'
            elif profile['checked_out']:
                reason = 'checked_out'
            elif not run or run['state'] == 'stopped':
                reason = 'not_running'
            elif run['state'] != 'running':
                reason = run['state']
            elif run['mode'] != 'remote':
                reason = 'not_remote'
            else:
                remote = self.remotes.get(run['id'])
                if remote is None:
                    reason = 'runtime_unavailable'
                else:
                    try:
                        await remote.watch(access)
                    except ControlError as error:
                        if error.code != 'run_conflict':
                            raise
                        reason = 'runtime_unavailable'
            await self.work(access.resolve)
            path = PREFIX + '/remote/' + run['id'] if reason is None else None
            return {'profile': profile, 'available': reason is None, 'reason': reason,
                    'viewer_path': '/#remote=' + profile['id'],
                    'transport': 'kasmvnc' if path else None, 'protocol_version': 2,
                    'control_path': path + '/control' if path else None,
                    'display_path': path + '/display' if path else None}

    def launch_modes(self):
        return {
            'disabled': [], 'headless': ['headless'],
            'desktop': ['desktop', 'headless'], 'remote': ['remote', 'headless'],
        }[self.mode]

    async def submit(self, access, profile_id, operation, data, *, main_world_eval=None,
                     maintenance=None, require_idle=False):
        fields(data, ('request_id',) if operation == 'start' else ('request_id', 'run_id'),
               ('mode',) if operation == 'start' else ())
        async with self.gate:
            await self.work(access.require_profile, profile_id, 'operate')
            if self.closing or self.failed or self.engine.recovery_required:
                raise TeamError('recovery_required', 503)
            if self.mode == 'disabled':
                raise TeamError('execution_disabled', 503)
            if (self.maintenance_job is not None or (profile_id in self.profile_jobs
                                                   and self.profile_jobs[profile_id] is not maintenance)):
                raise TeamError('profile_busy', 409)
            mode = data.get('mode', self.mode) if operation == 'start' else None
            if operation == 'start' and mode not in self.launch_modes():
                raise TeamError('invalid_request')
            command, created = await self.work(
                self.repository.reserve, access.session_id, profile_id, operation, data['request_id'],
                owner=self.owner, mode=mode, run_id=data.get('run_id'), main_world_eval=main_world_eval)
            if created:
                task = asyncio.create_task(self._execute(access, command, maintenance=maintenance,
                                                        require_idle=require_idle))
                task.add_done_callback(self._task_done)
                self.tasks[command['id']] = task
                while len(self.tasks) > 256 and next(iter(self.tasks.values())).done():
                    self.tasks.popitem(last=False)
            if maintenance is not None:
                maintenance['task'] = self.tasks.get(command['id'])
            return public_command(command)

    async def submit_job(self, access, kind, request_id, signature, operation, *, profile_id=None,
                         exclusive=False):
        validate_id(request_id)
        session = await self.work(access.resolve)
        member_id = session['member']['id']
        key = member_id, request_id
        async with self.gate:
            previous_id = self.job_requests.get(key)
            if previous_id is not None:
                previous = self.jobs[previous_id]
                if previous['kind'] != kind or previous['signature'] != signature:
                    raise TeamError('request_conflict', 409)
                return public_job(previous)
            if self.closing or self.failed or self.engine.recovery_required:
                raise TeamError('recovery_required', 503)
            if sum(job['state'] in ('accepted', 'running') for job in self.jobs.values()) >= 16:
                raise TeamError('capacity_reached', 409)
            if self.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            if exclusive:
                if self.profile_jobs or any(job['state'] in ('accepted', 'running') for job in self.jobs.values()):
                    raise TeamError('service_busy', 409)
                self.maintenance_job = request_id
            if profile_id is not None:
                if profile_id in self.profile_jobs:
                    raise TeamError('profile_busy', 409)
                self.profile_jobs[profile_id] = request_id
            now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
            job = {
                'id': str(uuid4()), 'request_id': request_id, 'kind': kind,
                'profile_id': profile_id, 'member_id': member_id, 'signature': signature,
                'state': 'accepted', 'result': None, 'error': None,
                'created_at': now, 'updated_at': now, 'secret': None,
            }
            task = asyncio.create_task(self._run_job(job, operation))
            job['task'] = task
            self.jobs[job['id']] = job
            self.job_requests[key] = job['id']
            while len(self.jobs) > 256:
                completed = next(((identifier, value) for identifier, value in self.jobs.items()
                                  if value['state'] not in ('accepted', 'running')), None)
                if completed is None:
                    break
                oldest_id, oldest = completed
                self.jobs.pop(oldest_id)
                self.job_requests.pop((oldest['member_id'], oldest['request_id']), None)
            return public_job(job)

    async def _run_job(self, job, operation):
        async with self.job_workers:
            job.update(state='running', updated_at=datetime.now(timezone.utc).isoformat(timespec='microseconds'))
            try:
                result = await operation(job)
                job.update(state='succeeded', result=result)
            except Exception as error:
                if isinstance(error, ValidationError):
                    code = str(error) if str(error) in ERROR_STATUS else 'invalid_request'
                elif isinstance(error, TeamError):
                    code = error.code
                else:
                    code = 'operation_failed'
                job.update(state='failed', error=code)
            finally:
                job['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='microseconds')
                async with self.gate:
                    if job['profile_id'] is not None:
                        self.profile_jobs.pop(job['profile_id'], None)
                    if self.maintenance_job == job['request_id']:
                        self.maintenance_job = None

    def _task_done(self, task):
        if task.cancelled() or task.exception() is not None:
            self.failed = True

    async def _authorize_start(self, profile_id):
        entry = self.start_authorizations.get(profile_id)
        if entry is None or self.closing:
            raise TeamError('start_cancelled', 409)
        await self.work(entry[1].require_profile, profile_id, 'operate')

    async def _execute(self, access, command, *, maintenance=None, require_idle=False):
        profile_id, run_id = command['profile_id'], command['run_id']
        session = None
        error = None
        try:
            async with self.gate:
                await self.work(access.require_profile, profile_id, 'operate')
                if self.closing:
                    raise TeamError('service_closing', 503)
                if command['operation'] == 'start':
                    await self.work(self.repository.require_starting, run_id, self.owner)
                    self.start_authorizations[profile_id] = (run_id, access)
                    mode = command['signature']['mode']
                    overrides = {'main_world_eval': True} if command['signature'].get('main_world_eval') is True else {}
                    await self.engine.start(profile_id, headless=mode == 'headless', native=mode == 'remote',
                                            run_id=run_id, owner=self.owner, **overrides)
                    session = self.engine.sessions[profile_id]
                else:
                    session = self.engine.sessions.get(profile_id)
                    if session is not None and session.run_id != run_id:
                        raise TeamError('run_conflict', 409)
                    if require_idle and (session is None
                            or not self.can_recycle_automation_profile(profile_id, session, maintenance=maintenance)
                            or not blank_browser(session.context)):
                        raise TeamError('profile_busy', 409)
                    await self.work(self.repository.update_run, run_id, self.owner, 'stopping')
                    await self.engine.stop(profile_id)
            if command['operation'] == 'start':
                await session.ready.wait()
                async with self.gate:
                    await self.work(access.require_profile, profile_id, 'operate')
                    if session.error or session.context is None or session.stop.is_set():
                        raise TeamError(session.error_code or 'launch_failed', 503)
                    await self.work(self.repository.update_run, run_id, self.owner, 'running')
                    if session.remote:
                        self.remotes[run_id] = RemoteRun(self, profile_id, run_id, session.remote)
                    self.start_authorizations.pop(profile_id, None)
                monitor = asyncio.create_task(self._watch_run(run_id, session))
                self.monitors.add(monitor)
                monitor.add_done_callback(self._task_done)
                monitor.add_done_callback(self.monitors.discard)
            else:
                if session:
                    await session.task
                await self._finish_run(run_id, session)
                if self.engine.recovery_required:
                    raise TeamError('recovery_required', 503)
        except Exception as exception:
            error = exception.code if isinstance(exception, TeamError) else 'operation_failed'
            if command['operation'] == 'start':
                if session:
                    await self.engine.stop(profile_id)
                    await session.task
                await self._finish_run(run_id, session)
        finally:
            if self.start_authorizations.get(profile_id, (None,))[0] == run_id:
                self.start_authorizations.pop(profile_id, None)
            await self.work(self.repository.finish_command, command['id'], error=error)

    async def _finish_run(self, run_id, session):
        self.remotes.pop(run_id, None)
        uncertain = self.engine.recovery_required
        await self.work(self.repository.update_run, run_id, self.owner,
                        'recovery_required' if uncertain else 'stopped',
                        error='recovery_required' if uncertain else
                        ((session.error_code or 'browser_failed') if session and session.error else None))

    async def _watch_run(self, run_id, session):
        await session.task
        await self._finish_run(run_id, session)

    async def revoke_connections(self):
        if self.automation is not None:
            await self.automation.expire()
        # Revoked pending launches are cancelled; existing browser runs are owned by the service, not the login.
        for profile_id, (_, access) in tuple(self.start_authorizations.items()):
            try:
                await self.work(access.require_profile, profile_id, 'operate')
            except ValidationError:
                await self.engine.stop(profile_id)
        for socket, cookie in tuple(self.connections.items()):
            try:
                await self.access(cookie)
            except (TeamError, ValidationError):
                await self.close_socket(socket, 4401)
                self.connections.pop(socket, None)
        for remote in tuple(self.remotes.values()):
            async with self.gate, remote.gate:
                await remote.expire()
        for socket, (cookie, remote, connection_id) in tuple(self.remote_connections.items()):
            try:
                await remote.watch(await self.access(cookie))
            except (TeamError, ValidationError, ControlError):
                viewer = self.viewers.get(connection_id)
                if viewer is not None:
                    await viewer.close(4401)
                await self.close_socket(socket, 4401)

    async def close_socket(self, socket, code):
        if socket.application_state == WebSocketState.DISCONNECTED:
            return
        try:
            await socket.close(code=code)
        except (WebSocketDisconnect, RuntimeError):
            if socket.application_state != WebSocketState.DISCONNECTED:
                self.failed = True

    async def _shutdown(self):
        self.closing = True
        if self.automation is not None:
            await self.automation.stop()
        for viewer in tuple(self.viewers.values()):
            await viewer.close(1012)
        for socket, (_, remote, connection_id) in tuple(self.remote_connections.items()):
            await self.close_socket(socket, 1012)
            await remote.disconnect(connection_id)
        for socket in tuple(self.connections):
            await self.close_socket(socket, 1012)
        self.connections.clear()
        job_results = await asyncio.gather(*(job['task'] for job in self.jobs.values()), return_exceptions=True)
        engine_error = None
        try:
            await self.engine.shutdown()
        except Exception as error:
            engine_error = error
        finally:
            results = await asyncio.gather(*self.tasks.values(), *self.monitors, return_exceptions=True)
        if engine_error is not None:
            raise engine_error
        if (self.failed or any(isinstance(result, BaseException) for result in results)
                or any(isinstance(result, BaseException) for result in job_results)):
            raise StudioError('Team command cleanup failed; workspace ownership is retained.')
        if await self.work(self.repository.unresolved_runs):
            raise StudioError('Unresolved execution records remain; workspace ownership is retained.')
        self.lock.release()

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._shutdown())
        cancelled = False
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError:
                cancelled = True
        self._closing.result()
        if cancelled:
            raise asyncio.CancelledError


def create_app(root, *, origin='http://127.0.0.1:8766', private_network_only=False,
               allow_client=(), browser_mode='disabled',
               engine_factory=BrowserEngine, execution_boundary=None,
               automation=None, kernel_root=None, staging_root=None, client_root=None):
    from .assets import built_assets
    from ..backup import MAX_BYTES as MAX_RETURN_BODY
    web_assets = built_assets(ASSETS)
    from .transport import allowed_client, client_allowlist, exact_origin, private_address, session_cookie
    parsed = exact_origin(origin)
    allowed_networks = client_allowlist(allow_client)
    if automation is not None:
        automation = {**automation, 'allowed_networks': allowed_networks,
                      'private_network_only': private_network_only}
    private_only = private_network_only
    secure = parsed.scheme == 'https'
    cookie_name = session_cookie(parsed.scheme)
    csrf_key = secrets.token_bytes(32)
    service = TeamService(root, browser_mode=browser_mode, engine_factory=engine_factory,
                          execution_boundary=execution_boundary,
                          automation=automation, kernel_root=kernel_root, staging_root=staging_root,
                          client_root=client_root)

    def csrf(cookie):
        return hmac.new(csrf_key, cookie.encode(), hashlib.sha256).hexdigest()

    def checkout_route(path):
        parts = path.split('/')
        if len(parts) in (5, 6) and parts[:4] == ['', 'team', 'v1', 'checkouts']:
            return parts[5] if len(parts) == 6 else ''
        return None

    def peer_error(client):
        if private_only and (not client or not private_address(client.host, allow_loopback=True)):
            return 'private_network_required'
        if allowed_networks and (not client or not allowed_client(client.host, allowed_networks)):
            return 'client_not_allowed'
        return None

    @asynccontextmanager
    async def lifespan(app):
        await service.open()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.team = service

    @app.exception_handler(TeamError)
    async def team_error(request, error):
        return JSONResponse({'error': error.code}, status_code=error.status)

    @app.exception_handler(ValidationError)
    async def validation_error(request, error):
        code = str(error) if str(error) in ERROR_STATUS else 'invalid_request'
        return JSONResponse({'error': code}, status_code=ERROR_STATUS.get(code, 400))

    @app.exception_handler(ControlError)
    async def control_error(request, error):
        return JSONResponse({'error': error.code}, status_code=409)

    @app.exception_handler(StudioError)
    async def file_error(request, error):
        codes = {'invalid_file_name': 400, 'invalid_file_directory': 400, 'unsafe_file': 409,
                 'file_changed': 409, 'file_capacity': 413, 'file_not_found': 404, 'file_io_failed': 503}
        code = str(error) if str(error) in codes else 'operation_failed'
        return JSONResponse({'error': code}, status_code=codes.get(code, 500))

    @app.middleware('http')
    async def boundary(request: Request, call_next):
        response = None
        try:
            denied = peer_error(request.client)
            if denied is not None:
                raise TeamError(denied, 403)
            if (request.headers.getlist('host') != [parsed.netloc]
                    or request.url.scheme != parsed.scheme):
                raise TeamError('invalid_host', 400)
            supplied_origin = request.headers.get('origin')
            if supplied_origin is not None and supplied_origin != origin:
                raise TeamError('origin_denied', 403)
            if request.method not in ('GET', 'HEAD') and supplied_origin != origin:
                raise TeamError('origin_denied', 403)
            if request.query_params:
                raise TeamError('invalid_request')
            length = request.headers.get('content-length')
            if request.url.path.endswith('/cookies'):
                maximum = MAX_COOKIE_BODY
            elif request.url.path == PREFIX + '/configuration/import':
                maximum = MAX_CONFIGURATION_BODY
            elif request.url.path == PREFIX + '/proxies/import':
                maximum = MAX_PROXY_BODY
            elif checkout_route(request.url.path) == 'return':
                maximum = MAX_RETURN_BODY
            elif checkout_route(request.url.path) in ('files', 'identity', 'commit'):
                maximum = MAX_CONFIGURATION_BODY
            else:
                maximum = 8 * 1024 * 1024 if request.url.path.startswith(PREFIX + '/files/') else MAX_BODY
            if length is not None and (not length.isdecimal() or int(length) > maximum):
                raise TeamError('request_too_large', 413)
            if request.url.path.startswith(PREFIX):
                peer = request.client.host if request.client else 'unknown'
                service.rate.check('global', 'all', 1200)
                service.rate.check(peer, 'all', 300)
            response = await call_next(request)
        except TeamError as error:
            response = JSONResponse({'error': error.code}, status_code=error.status)
        except Exception:
            # Neither storage paths nor third-party exceptions are network-visible.
            response = JSONResponse({'error': 'operation_failed'}, status_code=500)
        response.headers.update({
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
            'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; "
                                     "connect-src 'self'; img-src 'self' blob:; font-src 'self'; base-uri 'none'; "
                                     "form-action 'self'; frame-ancestors 'none'",
            'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), clipboard-read=(), clipboard-write=(self)',
        })
        if secure:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    async def body(request, maximum=MAX_BODY):
        if request.headers.get('content-type', '').partition(';')[0].strip() != 'application/json':
            raise TeamError('invalid_request')

        async def read():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > maximum:
                    raise TeamError('request_too_large', 413)
                data.extend(chunk)
            try:
                result = json.loads(data)
            except (ValueError, UnicodeError, RecursionError):
                raise TeamError('invalid_request') from None
            if not isinstance(result, dict):
                raise TeamError('invalid_request')
            return result

        try:
            return await asyncio.wait_for(read(), timeout=10)
        except asyncio.TimeoutError:
            raise TeamError('request_timeout', 408) from None

    async def access(request, *, mutate=False, admin=False):
        cookie = request.cookies.get(cookie_name, '')
        member_store = await service.access(cookie)
        if mutate and not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode('utf-8'), csrf(cookie).encode('ascii')):
            raise TeamError('csrf_denied', 403)
        if admin:
            await service.work(member_store.require_admin)
        return member_store

    def frontend_asset(name):
        item = web_assets.get(name)
        if item is None:
            raise TeamError('not_found', 404)
        path, content_type = item
        return FileResponse(path, media_type=content_type)

    @app.get('/')
    async def console():
        return frontend_asset('index.html')

    @app.get('/assets/{name}')
    async def asset(name: str):
        return frontend_asset('assets/' + name)

    @app.get('/kasm-client.js')
    async def kasm_client():
        return frontend_asset('kasm-client.js')

    @app.get('/kasm-client/{name}')
    async def kasm_client_asset(name: str):
        return frontend_asset('kasm-client/' + name)

    @app.post(PREFIX + '/login')
    async def login(request: Request):
        peer = request.client.host if request.client else 'unknown'
        service.rate.check('global', 'login', 60)
        service.rate.check(peer, 'login', 20)
        data = fields(await body(request), ('name', 'password'))
        if not isinstance(data['name'], str) or not isinstance(data['password'], str) or len(data['password']) > 1024:
            raise TeamError('invalid_credentials', 401)
        try:
            session = await service.work(service.store.login, data['name'], data['password'])
        except ValidationError as error:
            if str(error) == 'login_locked':
                raise TeamError('login_locked', 429) from None
            raise TeamError('invalid_credentials', 401) from None
        previous = await service.work(service.repository.session_id, request.cookies.get(cookie_name, ''))
        if previous:
            await service.work(service.store.logout, previous)
            await service.revoke_connections()
        cookie = await service.work(service.repository.issue_cookie, session['session_id'])
        response = JSONResponse({'member': session['member'], 'expires_at': session['expires_at'], 'csrf': csrf(cookie)})
        response.set_cookie(cookie_name, cookie, max_age=12 * 3600, secure=secure,
                            httponly=True, samesite='strict', path='/')
        return response

    @app.get(PREFIX + '/me')
    async def me(request: Request):
        member_store = await access(request)
        session = await service.work(member_store.resolve)
        return {'member': session['member'], 'expires_at': session['expires_at'],
                'csrf': csrf(request.cookies[cookie_name]), 'workspace_id': service.workspace_id,
                'capabilities': {'browser_mode': service.mode, 'remote_display': service.mode == 'remote',
                                 'control_handover': service.mode == 'remote', 'file_transfer': service.mode == 'remote',
                                 'launch_modes': service.launch_modes(),
                                 'team_automation': service.automation is not None,
                                 'online_maintenance': False, 'secure_transport': secure,
                                 'private_network_only': private_only,
                                 'client_allowlist': bool(allowed_networks),
                                 'key_codes': sorted(KEYS) if service.mode == 'remote' else []}}

    @app.get(PREFIX + '/api-keys')
    async def api_keys(request: Request):
        member_store = await access(request)
        return {'keys': await service.work(service.api_keys.list, member_store.session_id),
                'enabled': service.automation is not None}

    @app.post(PREFIX + '/api-keys')
    async def create_api_key(request: Request):
        member_store = await access(request, mutate=True)
        # Check the role even when the optional network listener is disabled.
        session = await service.work(member_store.resolve)
        if session['member']['role'] not in ('admin', 'operator'):
            raise TeamError('access_denied', 403)
        if service.automation is None:
            raise TeamError('automation_disabled', 409)
        data = await body(request)
        async with service.gate:
            created = await service.work(service.api_keys.create, member_store.session_id, data)
        return {'key': created['key'], 'connection': {'protocolVersion': 1,
                'baseURL': service.automation.base_url, 'token': created['token']}}

    @app.post(PREFIX + '/api-keys/{identifier}/revoke')
    async def revoke_api_key(identifier: str, request: Request):
        member_store = await access(request, mutate=True)
        fields(await body(request), ())
        async with service.gate:
            await service.work(service.api_keys.revoke, member_store.session_id, identifier)
        await service.revoke_connections()
        return {'ok': True}

    @app.post(PREFIX + '/logout')
    async def logout(request: Request):
        member_store = await access(request, mutate=True)
        fields(await body(request), ())
        await service.work(member_store.logout, member_store.session_id)
        await service.revoke_connections()
        response = JSONResponse({'ok': True})
        response.delete_cookie(cookie_name, secure=secure, httponly=True, samesite='strict', path='/')
        return response

    @app.post(PREFIX + '/password')
    async def password(request: Request):
        member_store = await access(request, mutate=True)
        data = fields(await body(request), ('current', 'password'))
        session = await service.work(member_store.resolve)
        await service.work(member_store.change_own_password, session['member']['id'], data['current'], data['password'])
        await service.revoke_connections()
        return {'ok': True}

    @app.get(PREFIX + '/profiles')
    async def profiles(request: Request):
        return await service.snapshot(await access(request))

    @app.post(PREFIX + '/profiles/batch')
    async def batch_profiles(request: Request):
        member_store = await access(request, mutate=True)
        data = fields(await body(request), ('action', 'items'))
        if data['action'] not in ('start', 'headless_start', 'stop', 'archive', 'restore'):
            raise TeamError('invalid_request')
        if not isinstance(data['items'], list) or not 1 <= len(data['items']) <= 50:
            raise TeamError('invalid_request')
        if data['action'] in ('archive', 'restore'):
            await service.work(member_store.require_admin)
        results = []
        for item in data['items']:
            try:
                if data['action'] in ('start', 'headless_start'):
                    fields(item, ('profile_id', 'request_id'))
                    payload = {'request_id': item['request_id']}
                    if data['action'] == 'headless_start':
                        payload['mode'] = 'headless'
                    result = await service.submit(member_store, item['profile_id'], 'start', payload)
                    results.append({'profile_id': item['profile_id'], 'command': result})
                elif data['action'] == 'stop':
                    fields(item, ('profile_id', 'request_id', 'run_id'))
                    result = await service.submit(member_store, item['profile_id'], 'stop', {
                        'request_id': item['request_id'], 'run_id': item['run_id'],
                    })
                    results.append({'profile_id': item['profile_id'], 'command': result})
                else:
                    fields(item, ('profile_id', 'request_id', 'revision'))
                    archived = data['action'] == 'archive'

                    async def update(job, selected=item, value=archived):
                        await service.work(member_store.require_admin)
                        async with service.gate:
                            await service.work(service.repository.ensure_editable, selected['profile_id'])
                            profile = await service.work(
                                member_store.update_profile_checked, {'archived': value},
                                selected['profile_id'], selected['revision'])
                        return {key: profile[key] for key in PROFILE_METADATA}

                    result = await service.submit_job(
                        member_store, 'profile.archive' if archived else 'profile.restore',
                        item['request_id'], {'profile_id': item['profile_id'], 'archived': archived,
                                             'revision': item['revision']},
                        update, profile_id=item['profile_id'])
                    results.append({'profile_id': item['profile_id'], 'task': result})
            except (TeamError, ValidationError) as error:
                code = error.code if isinstance(error, TeamError) else str(error)
                results.append({'profile_id': item.get('profile_id') if isinstance(item, dict) else None,
                                'error': code if code in ERROR_STATUS else 'invalid_request'})
        return JSONResponse({'items': results}, status_code=202)

    @app.get(PREFIX + '/profiles/{profile_id}')
    async def profile_detail(profile_id: str, request: Request):
        member_store = await access(request, admin=True)
        async with service.gate:
            return await service.work(service.read_profile, member_store, profile_id, detail=True)

    @app.get(PREFIX + '/profiles/{profile_id}/remote')
    async def remote_entry(profile_id: str, request: Request):
        return await service.remote_entry(await access(request), profile_id)

    @app.get(PREFIX + '/profiles/{profile_id}/identity')
    async def profile_identity(profile_id: str, request: Request):
        member_store = await access(request, admin=True)
        async with service.gate:
            try:
                profile = await service.work(member_store.get_profile, profile_id)
            except KeyError:
                raise TeamError('not_found', 404) from None
        return {'profile_id': profile['id'], 'revision': profile['revision'],
                'identity': public_identity(profile['identity'])}

    @app.post(PREFIX + '/profiles/{profile_id}/identity')
    async def rebuild_profile_identity(profile_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('action', 'revision'))
        if data['action'] != 'rebuild':
            raise TeamError('invalid_request')
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            if profile_id in service.profile_jobs:
                raise TeamError('profile_busy', 409)
            await service.work(service.repository.ensure_editable, profile_id)
            try:
                profile = await service.work(member_store.reset_identity_checked, profile_id, data['revision'])
            except KeyError:
                raise TeamError('not_found', 404) from None
        return {'profile_id': profile['id'], 'revision': profile['revision'], 'identity_present': False}

    @app.post(PREFIX + '/profiles/{profile_id}/clone')
    async def clone_profile(profile_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('revision',))
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            if profile_id in service.profile_jobs:
                raise TeamError('profile_busy', 409)
            await service.work(service.repository.ensure_editable, profile_id, configuration_only=True)
            try:
                profile = await service.work(member_store.clone_profile_checked, profile_id, data['revision'])
            except KeyError:
                raise TeamError('not_found', 404) from None
        return JSONResponse({key: profile[key] for key in PROFILE_METADATA}, status_code=201)

    @app.post(PREFIX + '/profiles/{profile_id}/delete')
    async def delete_profile(profile_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('request_id', 'revision', 'remove_data'))
        await service.work(service.read_profile, member_store, profile_id, detail=True)

        async def remove(job):
            await service.work(member_store.require_admin)
            async with service.gate:
                await service.work(service.repository.ensure_editable, profile_id)
                try:
                    await service.work(member_store.delete_profile_checked, profile_id, data['revision'],
                                       remove_data=data['remove_data'])
                except KeyError:
                    raise TeamError('not_found', 404) from None
            return {'profile_id': profile_id, 'data_removed': data['remove_data']}

        result = await service.submit_job(
            member_store, 'profile.delete', data['request_id'],
            {'profile_id': profile_id, 'revision': data['revision'], 'remove_data': data['remove_data']},
            remove, profile_id=profile_id)
        return JSONResponse(result, status_code=202)

    @app.get(PREFIX + '/profiles/{profile_id}/cookies')
    async def export_cookies(profile_id: str, request: Request):
        member_store = await access(request, admin=True)
        async with service.gate:
            try:
                await service.work(member_store.get_profile, profile_id)
            except KeyError:
                raise TeamError('not_found', 404) from None
            run = await service.work(service.repository.latest_run, profile_id)
            if not run or run['state'] != 'running' or profile_id not in service.engine.sessions:
                raise TeamError('run_conflict', 409)
            cookies = await service.engine.export_cookie_data(profile_id)
        return {'cookies': cookies}

    @app.post(PREFIX + '/profiles/{profile_id}/cookies')
    async def import_cookies(profile_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request, MAX_COOKIE_BODY), ('cookies',))
        async with service.gate:
            try:
                await service.work(member_store.get_profile, profile_id)
            except KeyError:
                raise TeamError('not_found', 404) from None
            run = await service.work(service.repository.latest_run, profile_id)
            if not run or run['state'] != 'running' or profile_id not in service.engine.sessions:
                raise TeamError('run_conflict', 409)
            count = await service.engine.import_cookie_data(profile_id, data)
        return {'imported': count}

    @app.post(PREFIX + '/profiles')
    async def create_profile(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), (), PROFILE_FIELDS)
        profile = await service.create_profile(member_store, data)
        return {key: profile[key] for key in PROFILE_METADATA}

    @app.post(PREFIX + '/profiles/{profile_id}')
    async def update_profile(profile_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('data', 'revision'))
        fields(data['data'], (), PROFILE_FIELDS)
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            if profile_id in service.profile_jobs:
                raise TeamError('profile_busy', 409)
            await service.work(service.repository.ensure_editable, profile_id)
            profile = await service.work(member_store.update_profile_checked, data['data'], profile_id, data['revision'])
        return {key: profile[key] for key in PROFILE_METADATA}

    def checkout_service():
        if service.checkouts is None:
            raise TeamError('checkout_unavailable', 409)
        return service.checkouts

    def bearer(request):
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme != 'Bearer' or not token:
            raise TeamError('checkout_denied', 401)
        return token

    @asynccontextmanager
    async def transfer(checkout_id):
        # One upload, commit or release per lease at a time; entries are dropped when idle.
        entry = service.transfer_locks.setdefault(checkout_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if not entry[1]:
                service.transfer_locks.pop(checkout_id, None)

    async def receive_stream(request, path, limit):
        chunks, size = request.stream().__aiter__(), 0
        with open(path, 'wb') as output:
            while True:
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), 60)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise TeamError('request_timeout', 408) from None
                size += len(chunk)
                if size > limit:
                    raise TeamError('request_too_large', 413)
                await asyncio.to_thread(output.write, chunk)

    @app.post(PREFIX + '/profiles/{profile_id}/checkout')
    async def checkout_profile(profile_id: str, request: Request):
        member_store = await access(request, mutate=True)
        data = await body(request)
        checkouts = checkout_service()
        async with service.gate:
            if service.closing or service.failed:
                raise TeamError('recovery_required', 503)
            if (service.maintenance_job is not None or profile_id in service.profile_jobs
                    or profile_id in service.engine.sessions):
                raise TeamError('profile_busy', 409)
            result = await service.work(checkouts.create, member_store.session_id, profile_id, data)
        return JSONResponse(result, status_code=201)

    @app.post(PREFIX + '/profiles/{profile_id}/release')
    async def release_profile(profile_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('checkout_id',))
        if not isinstance(data['checkout_id'], str):
            raise TeamError('invalid_request')
        checkouts = checkout_service()
        async with transfer(data['checkout_id']), service.gate:
            return await service.work(checkouts.release, member_store.session_id, profile_id, data['checkout_id'])

    @app.get(PREFIX + '/checkouts/{checkout_id}')
    async def checkout_manifest(checkout_id: str, request: Request):
        return await service.transfer_work(checkout_service().manifest, checkout_id, bearer(request))

    @app.get(PREFIX + '/checkouts/{checkout_id}/kernel')
    async def checkout_kernel(checkout_id: str, request: Request):
        path, record = await service.work(checkout_service().kernel_package, checkout_id, bearer(request))
        return FileResponse(path, media_type='application/zip', headers={
            'Content-Disposition': 'attachment', 'X-Content-SHA256': record['sha256']})

    @app.post(PREFIX + '/checkouts/{checkout_id}/files')
    async def checkout_files(checkout_id: str, request: Request):
        from ..checkouts import stream_files
        data = fields(await body(request, MAX_CONFIGURATION_BODY), ('names',))
        paths = await service.work(checkout_service().files, checkout_id, bearer(request), data['names'])
        return StreamingResponse(stream_files(paths), media_type='application/zip')

    @app.post(PREFIX + '/checkouts/{checkout_id}/identity')
    async def checkout_identity(checkout_id: str, request: Request):
        data = fields(await body(request, MAX_CONFIGURATION_BODY), ('identity',))
        if not isinstance(data['identity'], dict):
            raise TeamError('invalid_request')
        claimed = await service.work(checkout_service().claim_identity, checkout_id, bearer(request), data['identity'])
        return {'claimed': claimed}

    @app.post(PREFIX + '/checkouts/{checkout_id}/heartbeat')
    async def checkout_heartbeat(checkout_id: str, request: Request):
        fields(await body(request), ())
        return await service.work(checkout_service().heartbeat, checkout_id, bearer(request))

    @app.post(PREFIX + '/checkouts/{checkout_id}/cancel')
    async def checkout_cancel(checkout_id: str, request: Request):
        fields(await body(request), ())
        checkouts, token = checkout_service(), bearer(request)
        async with transfer(checkout_id):
            return await service.work(checkouts.cancel, checkout_id, token)

    @app.post(PREFIX + '/checkouts/{checkout_id}/return')
    async def checkout_return(checkout_id: str, request: Request):
        if request.headers.get('content-type') != 'application/zip':
            raise TeamError('invalid_request')
        checkouts, token = checkout_service(), bearer(request)
        await service.work(checkouts.authorize, checkout_id, token)
        async with transfer(checkout_id):
            path = await service.work(checkouts.upload_path, checkout_id)
            try:
                await receive_stream(request, path, MAX_RETURN_BODY)
                received = await service.transfer_work(checkouts.receive, checkout_id, token, path)
            finally:
                await service.work(path.unlink, missing_ok=True)
        return {'received': received}

    @app.post(PREFIX + '/checkouts/{checkout_id}/commit')
    async def checkout_commit(checkout_id: str, request: Request):
        from ..checkouts import CheckoutIncomplete
        data = await body(request, MAX_CONFIGURATION_BODY)
        checkouts, token = checkout_service(), bearer(request)
        async with transfer(checkout_id):
            try:
                return await service.transfer_work(checkouts.commit, checkout_id, token, data)
            except CheckoutIncomplete as error:
                return JSONResponse({'error': 'checkout_incomplete', 'missing': error.missing}, status_code=409)

    @app.post(PREFIX + '/profiles/{profile_id}/{operation}')
    async def operate(profile_id: str, operation: str, request: Request):
        if operation not in ('start', 'stop'):
            raise TeamError('not_found', 404)
        member_store = await access(request, mutate=True)
        command = await service.submit(member_store, profile_id, operation, await body(request))
        return JSONResponse(command, status_code=202)

    @app.get(PREFIX + '/commands/{command_id}')
    async def command(command_id: str, request: Request):
        member_store = await access(request)
        session = await service.work(member_store.resolve)
        result = await service.work(service.repository.command, command_id)
        if session['member']['role'] != 'admin' and result['member_id'] != session['member']['id']:
            raise TeamError('access_denied', 403)
        await service.work(member_store.require_profile, result['profile_id'], 'view')
        return public_command(result)

    @app.get(PREFIX + '/tasks/{task_id}')
    async def task_status(task_id: str, request: Request):
        member_store = await access(request)
        try:
            validate_id(task_id)
        except ValidationError:
            raise TeamError('invalid_request') from None
        session = await service.work(member_store.resolve)
        job = service.jobs.get(task_id)
        if job is None:
            if session['member']['role'] == 'admin':
                raise TeamError('not_found', 404)
            raise TeamError('access_denied', 403)
        if session['member']['role'] != 'admin' and job['member_id'] != session['member']['id']:
            raise TeamError('access_denied', 403)
        return public_job(job)

    @app.get(PREFIX + '/proxies')
    async def proxies(request: Request):
        member_store = await access(request, admin=True)
        return {'proxies': await service.work(member_store.list_proxies)}

    @app.post(PREFIX + '/proxies')
    async def create_proxy(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), (), PROXY_FIELDS)
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            proxy = await service.work(member_store.save_proxy, data)
        return JSONResponse(proxy, status_code=201)

    @app.post(PREFIX + '/proxies/import')
    async def import_proxies(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request, MAX_PROXY_BODY), ('request_id', 'text'))
        if not isinstance(data['text'], str):
            raise TeamError('invalid_request')
        digest = hashlib.sha256(data['text'].encode('utf-8')).hexdigest()

        async def save(job):
            await service.work(member_store.require_admin)
            saved = await service.work(member_store.import_proxies, data['text'])
            return {'imported': len(saved)}

        result = await service.submit_job(member_store, 'proxy.import', data['request_id'],
                                          {'sha256': digest}, save)
        return JSONResponse(result, status_code=202)

    @app.post(PREFIX + '/proxies/parse')
    async def parse_proxy(request: Request):
        from ..store import parse_proxy_line

        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('link',))
        try:
            parsed = await service.work(parse_proxy_line, data['link'])
            await service.work(member_store.require_admin)
            return {'proxy': parsed}
        except (ValueError, TypeError):
            raise TeamError('invalid_proxy_link') from None

    @app.post(PREFIX + '/proxies/{proxy_id}')
    async def update_proxy(proxy_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), (), PROXY_FIELDS)
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            try:
                proxy = await service.work(member_store.save_proxy, data, proxy_id)
            except KeyError:
                raise TeamError('not_found', 404) from None
        return proxy

    @app.post(PREFIX + '/proxies/{proxy_id}/delete')
    async def delete_proxy(proxy_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        fields(await body(request), ())
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            try:
                await service.work(member_store.delete_proxy, proxy_id)
            except KeyError:
                raise TeamError('not_found', 404) from None
        return {'ok': True}

    @app.post(PREFIX + '/proxies/{proxy_id}/probe')
    async def probe_proxy(proxy_id: str, request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('request_id',))
        try:
            await service.work(member_store.get_proxy, proxy_id)
        except KeyError:
            raise TeamError('not_found', 404) from None

        async def probe(job):
            from ..engine import ProxyCheckError

            await service.work(member_store.require_admin)
            try:
                return await service.engine.probe(proxy_id)
            except (ProxyCheckError, ValidationError):
                raise TeamError('proxy_failed') from None

        result = await service.submit_job(member_store, 'proxy.probe', data['request_id'],
                                          {'proxy_id': proxy_id}, probe)
        return JSONResponse(result, status_code=202)

    @app.get(PREFIX + '/activity')
    async def activity(request: Request):
        member_store = await access(request)
        return {'events': await service.work(member_store.list_events, limit=200)}

    @app.get(PREFIX + '/settings')
    async def settings(request: Request):
        member_store = await access(request, admin=True)
        result = await service.work(member_store.get_settings)
        result['automation_enabled'] = False
        return result

    @app.get(PREFIX + '/timezones')
    async def timezones(request: Request):
        from ..timezones import timezone_catalog

        await access(request)
        try:
            return await service.work(timezone_catalog)
        except (OSError, ValueError, KeyError, ModuleNotFoundError):
            raise TeamError('timezone_catalog_unavailable', 503) from None

    @app.post(PREFIX + '/settings')
    async def save_settings(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        allowed = ('max_running', 'executable_path', 'automation_enabled')
        data = fields(await body(request), (), allowed)
        if data.get('automation_enabled') is True:
            raise TeamError('role_permission_denied', 403)
        async with service.gate:
            if service.maintenance_job is not None:
                raise TeamError('service_busy', 409)
            current = await service.work(member_store.get_settings)
            active = await service.work(service.repository.unresolved_runs)
            if active and data.get('executable_path', current['executable_path']) != current['executable_path']:
                raise TeamError('service_busy', 409)
            if active and 'max_running' in data and (type(data['max_running']) is not int
                                                     or data['max_running'] < len(active)):
                raise TeamError('capacity_reached', 409)
            result = await service.work(member_store.save_settings, {**data, 'automation_enabled': False})
        return result

    @app.get(PREFIX + '/configuration')
    async def export_configuration(request: Request):
        member_store = await access(request, admin=True)
        return await service.work(member_store.export_config)

    @app.post(PREFIX + '/configuration/import')
    async def import_configuration(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request, MAX_CONFIGURATION_BODY), ('request_id', 'configuration'))
        if not isinstance(data['configuration'], dict):
            raise TeamError('invalid_request')
        digest = hashlib.sha256(json.dumps(data['configuration'], sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
        if await service.work(service.repository.unresolved_runs):
            raise TeamError('service_busy', 409)

        async def load(job):
            await service.work(member_store.require_admin)
            async with service.gate:
                if await service.work(service.repository.unresolved_runs):
                    raise TeamError('service_busy', 409)
                return await service.work(member_store.import_config, data['configuration'])

        result = await service.submit_job(member_store, 'configuration.import', data['request_id'],
                                          {'sha256': digest}, load, exclusive=True)
        return JSONResponse(result, status_code=202)

    def kernel_inventory():
        from ...multiversion import list_installed

        default = service.store.get_default_kernel()
        installed = [{
            'id': item.relative_path,
            'kernel_id': kernel_id(item),
            'repository': item.repo_name,
            'version': item.version.version,
            'build': item.version.build,
            'prerelease': item.is_prerelease,
            'active': kernel_id(item) == default,
            'sha256': item.sha256,
            'size': item.asset_size,
            'created_at': item.created_at,
        } for item in list_installed()]
        cache = service.kernel_cache.list() if service.kernel_cache is not None else None
        return {'camoufox': installed, 'cache': cache, 'default': default}

    @app.get(PREFIX + '/kernels')
    async def kernels(request: Request):
        member_store = await access(request, admin=True)
        await service.work(member_store.require_admin)
        return await service.work(kernel_inventory)

    def kernel_cache():
        if service.kernel_cache is None:
            raise TeamError('kernel_cache_unavailable', 409)
        return service.kernel_cache

    async def kernel_job(request, kind, operation, *, exclusive):
        from ..kernel_cache import KernelCacheError
        member_store = await access(request, mutate=True, admin=True)
        required = ('request_id',) if kind == 'kernel.catalog' else ('request_id', 'kernel_id')
        data = fields(await body(request), required)
        cache = kernel_cache()

        async def run(job):
            await service.work(member_store.require_admin)
            try:
                if exclusive:
                    async with service.gate:
                        if await service.work(service.repository.unresolved_runs):
                            raise TeamError('service_busy', 409)
                        return await service.work(operation, cache, data)
                return await service.work(operation, cache, data)
            except KernelCacheError as error:
                raise TeamError(str(error), 409) from None
            except OSError:
                raise TeamError('kernel_download_failed', 503) from None

        signature = {key: value for key, value in data.items() if key != 'request_id'}
        result = await service.submit_job(member_store, kind, data['request_id'], signature, run,
                                          exclusive=exclusive)
        return JSONResponse(result, status_code=202)

    @app.post(PREFIX + '/kernels/catalog')
    async def kernel_catalog(request: Request):
        return await kernel_job(request, 'kernel.catalog', lambda cache, _: {'kernels': cache.available()},
                                exclusive=False)

    @app.post(PREFIX + '/kernels/cache')
    async def cache_kernel(request: Request):
        return await kernel_job(request, 'kernel.cache', lambda cache, data: cache.cache(data['kernel_id']),
                                exclusive=True)

    @app.post(PREFIX + '/kernels/remove')
    async def remove_kernel(request: Request):
        def remove(cache, data):
            cache.remove(data['kernel_id'], service.store.kernel_references())
            return {'kernel_id': data['kernel_id']}
        return await kernel_job(request, 'kernel.remove', remove, exclusive=True)

    @app.get(PREFIX + '/kernels/{repository}/{release}/{platform}')
    async def kernel_package(repository: str, release: str, platform: str, request: Request):
        from ..kernel_cache import KernelCacheError
        await access(request)
        try:
            path, record = await service.work(kernel_cache().package, repository + '/' + release, platform)
        except KernelCacheError as error:
            raise TeamError(str(error), 404 if str(error) == 'kernel_not_cached' else 400) from None
        return FileResponse(path, media_type='application/zip', headers={
            'Content-Disposition': 'attachment', 'X-Content-SHA256': record['sha256']})

    @app.post(PREFIX + '/kernels/activate')
    async def activate_kernel(request: Request):
        from ...multiversion import list_installed

        member_store = await access(request, mutate=True, admin=True)
        data = fields(await body(request), ('request_id', 'id'))
        if not isinstance(data['id'], str):
            raise TeamError('invalid_request')
        available = {item.relative_path for item in await service.work(list_installed)}
        if data['id'] not in available:
            raise TeamError('not_found', 404)

        async def activate(job):
            await service.work(member_store.require_admin)
            async with service.gate:
                current = {item.relative_path: item for item in await service.work(list_installed)}
                if data['id'] not in current:
                    raise TeamError('not_found', 404)
                await service.work(service.store.set_default_kernel, kernel_id(current[data['id']]))
            return await service.work(kernel_inventory)

        result = await service.submit_job(member_store, 'kernel.activate', data['request_id'],
                                          {'id': data['id']}, activate, exclusive=True)
        return JSONResponse(result, status_code=202)

    def client_packages():
        from ..client_releases import ClientReleaseError, offered
        if service.client_root is None:
            return {}
        try:
            return offered(service.client_root)
        except ClientReleaseError as error:
            raise TeamError(str(error), 503) from None

    @app.get(PREFIX + '/client/update')
    async def client_update(request: Request):
        from ..client_releases import MINIMUM_PROTOCOL, PROTOCOL
        await access(request)
        packages = await service.work(client_packages)
        return {'protocol': PROTOCOL, 'minimum_protocol': MINIMUM_PROTOCOL, 'packages': list(packages.values())}

    @app.get(PREFIX + '/client/packages/{platform}')
    async def client_package(platform: str, request: Request):
        await access(request)
        record = (await service.work(client_packages)).get(platform)
        if record is None:
            raise TeamError('not_found', 404)
        return FileResponse(service.client_root / record['file'], media_type='application/octet-stream',
                            filename=record['file'], headers={'X-Content-SHA256': record['sha256']})

    @app.get(PREFIX + '/admin')
    async def admin(request: Request):
        member_store = await access(request, admin=True)

        def read():
            members = [{key: member[key] for key in MEMBER_FIELDS} for member in member_store.list_members()]
            for member in members:
                member['grants'] = member_store.list_grants(member['id'])
            groups = member_store.list_member_groups()
            for group in groups:
                group['members'] = member_store.list_group_members(group['id'])
                group['grants'] = member_store.list_group_grants(group['id'])
            return {'members': members, 'groups': groups, 'audit': member_store.list_audit(limit=100)}

        return await service.work(read)

    @app.get(PREFIX + '/health')
    async def health(request: Request):
        member_store = await access(request, admin=True)
        active_tasks = sum(job['state'] in ('accepted', 'running') for job in service.jobs.values())
        maintenance = service.maintenance_job is not None

        def read():
            member_store.require_admin()
            with service.store._connect() as connection:
                active = connection.execute("SELECT COUNT(*) FROM team_runs WHERE state != 'stopped'").fetchone()[0]
                pending = sum(json.loads(row[0])['state'] == 'accepted'
                              for row in connection.execute('SELECT data FROM team_commands'))
                registered = bool(connection.execute(
                    "SELECT 1 FROM workspace_metadata WHERE key = 'runtime_manifest'").fetchone())
                barrier = bool(connection.execute(
                    "SELECT 1 FROM workspace_metadata WHERE key = 'execution_barrier'").fetchone())
            free = shutil.disk_usage(service.root).free
            return {'active_runs': active, 'pending_commands': pending,
                    'active_tasks': active_tasks,
                    'max_running': member_store.get_settings()['max_running'], 'disk_free_bytes': free,
                    'runtime_registered': registered, 'execution_blocked': barrier,
                    'recovery_required': service.failed or service.engine.recovery_required,
                    'closing': service.closing, 'maintenance': maintenance,
                    'remote_display': service.mode == 'remote'}

        try:
            return await service.work(read)
        except OSError:
            raise TeamError('health_unavailable', 503) from None

    @app.post(PREFIX + '/members')
    async def members(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = await body(request)

        def modify():
            actor = member_store.require_admin()['member']['id']
            action = data.get('action')
            if action == 'create':
                fields(data, ('action', 'name', 'display', 'role', 'password'))
                member = member_store.create_member(actor, data['name'], data['display'], data['role'], data['password'])
                return {key: member[key] for key in MEMBER_FIELDS}
            if action == 'active':
                fields(data, ('action', 'id', 'active'))
                member_store.set_member_active(actor, data['id'], data['active'])
            elif action == 'password':
                fields(data, ('action', 'id', 'password'))
                member_store.reset_member_password(actor, data['id'], data['password'])
            elif action == 'grant':
                fields(data, ('action', 'id', 'profile_id', 'permissions'))
                member_store.grant_profile(actor, data['id'], data['profile_id'], data['permissions'])
            elif action == 'revoke':
                fields(data, ('action', 'id', 'profile_id'))
                member_store.revoke_profile(actor, data['id'], data['profile_id'])
            else:
                raise TeamError('invalid_request')
            return {'ok': True}

        async with service.gate:
            result = await service.work(modify)
        await service.revoke_connections()
        return result

    @app.post(PREFIX + '/groups')
    async def groups(request: Request):
        member_store = await access(request, mutate=True, admin=True)
        data = await body(request)

        def modify():
            actor = member_store.require_admin()['member']['id']
            action = data.get('action')
            if action == 'create':
                fields(data, ('action', 'name'))
                return member_store.create_member_group(actor, data['name'])
            if action == 'members':
                fields(data, ('action', 'id', 'revision', 'members'))
                return member_store.set_group_members(actor, data['id'], data['members'], expected_revision=data['revision'])
            if action == 'grants':
                fields(data, ('action', 'id', 'revision', 'changes'))
                return member_store.save_group_grants(actor, data['id'], data['changes'], expected_revision=data['revision'])
            if action == 'delete':
                fields(data, ('action', 'id', 'revision'))
                member_store.delete_member_group(actor, data['id'], expected_revision=data['revision'])
                return {'ok': True}
            raise TeamError('invalid_request')

        async with service.gate:
            result = await service.work(modify)
        await service.revoke_connections()
        return result

    @app.websocket(PREFIX + '/events')
    async def events(socket: WebSocket):
        if (socket.headers.get('origin') != origin or socket.headers.get('host') != parsed.netloc
                or socket.url.scheme != ('wss' if secure else 'ws') or socket.query_params
                or peer_error(socket.client) is not None):
            await socket.close(code=4403)
            return
        cookie = socket.cookies.get(cookie_name, '')
        try:
            member_store = await service.access(cookie)
            if len(service.connections) >= 64 or sum(value == cookie for value in service.connections.values()) >= 4:
                await socket.close(code=4429)
                return
            service.connections[socket] = cookie
            await socket.accept()
            while not service.closing:
                member_store = await service.access(cookie)
                await socket.send_json(await service.snapshot(member_store))
                try:
                    message = await asyncio.wait_for(socket.receive_text(), timeout=1)
                    if message != 'ping':
                        await socket.close(code=4400)
                        return
                    await asyncio.sleep(1)
                except asyncio.TimeoutError:
                    pass
        except (TeamError, ValidationError):
            await socket.close(code=4401)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            service.connections.pop(socket, None)

    async def file_run(run_id, member_store):
        remote = service.remotes.get(run_id)
        if remote is None:
            raise TeamError('run_conflict', 409)
        await remote.watch(member_store)
        await service.work(member_store.require_profile, remote.profile_id, 'operate')
        return remote, service.store.profile_dir(remote.profile_id)

    @app.get(PREFIX + '/files/{run_id}')
    async def list_files(run_id: str, request: Request):
        from .files import listing
        member_store = await access(request)
        async with service.gate:
            remote, directory = await file_run(run_id, member_store)
            result = await service.work(listing, directory)
            await remote.watch(member_store)
        return {'files': result}

    @app.get(PREFIX + '/files/{run_id}/{name}')
    async def get_file(run_id: str, name: str, request: Request):
        from .files import download
        member_store = await access(request)
        async with service.gate:
            remote, directory = await file_run(run_id, member_store)
            data = await service.work(download, directory, name)
            await remote.watch(member_store)
            await service.work(member_store.require_profile, remote.profile_id, 'operate')
        return Response(data, media_type='application/octet-stream',
                        headers={'Content-Disposition': 'attachment'})

    @app.post(PREFIX + '/files/{run_id}/{name}')
    async def put_file(run_id: str, name: str, request: Request):
        from .files import LIMIT, filename, upload
        member_store = await access(request, mutate=True)
        filename(name)
        if request.headers.get('content-type') != 'application/octet-stream':
            raise TeamError('invalid_request')
        await file_run(run_id, member_store)
        async with service.file_workers:
            data = bytearray()

            async def read_file():
                async for block in request.stream():
                    if len(data) + len(block) > LIMIT:
                        raise TeamError('file_capacity', 413)
                    data.extend(block)
            try:
                await asyncio.wait_for(read_file(), 10)
            except asyncio.TimeoutError:
                raise TeamError('request_timeout', 408) from None
            async with service.gate:
                remote, directory = await file_run(run_id, member_store)
                result = await service.work(upload, directory, name, data)
        return result

    def remote_socket_allowed(socket):
        return (socket.headers.get('origin') == origin
                and socket.headers.getlist('host') == [parsed.netloc]
                and socket.url.scheme == ('wss' if secure else 'ws')
                and not socket.query_params and peer_error(socket.client) is None)

    @app.websocket(PREFIX + '/remote/{run_id}/control')
    async def remote_display(run_id: str, socket: WebSocket):
        if not remote_socket_allowed(socket):
            await socket.close(code=4403)
            return
        cookie = socket.cookies.get(cookie_name, '')
        connection_id = str(uuid4())
        remote = service.remotes.get(run_id)
        viewer = None
        try:
            if remote is None:
                raise ControlError('run_conflict')
            member_store = await service.access(cookie)
            await remote.watch(member_store)
            if (len(service.remote_connections) >= 16 or
                    sum(value[0] == cookie for value in service.remote_connections.values()) >= 2):
                await socket.close(code=4429)
                return
            # Reserve before accepting or awaiting the CSRF hello, so parallel
            # incomplete handshakes cannot escape the authenticated-session cap.
            service.remote_connections[socket] = (cookie, remote, connection_id)
            await socket.accept()
            hello = await asyncio.wait_for(socket.receive_json(), 5)
            if (not isinstance(hello, dict) or set(hello) != {'csrf'} or not isinstance(hello['csrf'], str)
                    or not hmac.compare_digest(hello['csrf'].encode(), csrf(cookie).encode())):
                await socket.close(code=4403)
                return
            sending = asyncio.Lock()
            from .display import Viewer
            viewer = Viewer(service, cookie, remote, connection_id, socket)
            service.viewers[connection_id] = viewer

            async def status_message(result):
                async with sending:
                    await asyncio.wait_for(socket.send_json(result), 5)

            async def receive():
                started, count = time.monotonic(), 0
                while not service.closing:
                    message = await socket.receive_json()
                    if time.monotonic() - started >= 1:
                        started, count = time.monotonic(), 0
                    count += 1
                    if count > 80:
                        raise ControlError('rate_limited')
                    member_store = await service.access(cookie)
                    try:
                        result = await remote.handle(member_store, connection_id, message)
                        await status_message(result)
                    except ControlError as error:
                        result = {'error': error.code}
                        if isinstance(message, dict) and message.get('kind') == 'clipboard':
                            if type(message.get('request_id')) is int:
                                result['clipboard_request'] = message['request_id']
                        await status_message(result)

            receiver = asyncio.create_task(receive())
            try:
                while not service.closing:
                    member_store = await service.access(cookie)
                    async with service.gate, remote.gate:
                        await remote.watch(member_store)
                        await remote.expire()
                        status = {**remote.status(connection_id), 'viewer_id': connection_id}
                    async with sending:
                        await asyncio.wait_for(socket.send_json(status), 5)
                    done, _ = await asyncio.wait({receiver}, timeout=.2)
                    if done:
                        receiver.result()
                        break
            finally:
                with CancelScope(shield=True):
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
        except (TeamError, ValidationError, ControlError):
            await service.close_socket(socket, 4401)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            await service.close_socket(socket, 1011)
        finally:
            service.remote_connections.pop(socket, None)
            with CancelScope(shield=True):
                if viewer is not None:
                    await viewer.close()
                    service.viewers.pop(connection_id, None)
                if remote is not None:
                    await remote.disconnect(connection_id)

    from .display import install
    install(app, service, prefix=PREFIX, allowed=remote_socket_allowed, csrf=csrf, cookie_name=cookie_name)
    return app
