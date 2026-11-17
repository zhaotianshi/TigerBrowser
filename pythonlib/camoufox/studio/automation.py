"""Opt-in automation for pages owned by expiring, credential-scoped Studio leases."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hmac
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from .automation_resources import (
    AutomationError, BinaryResult, BodyTransfer, CAPABILITIES, HANDLE_SECONDS,
    MAX_DOWNLOAD, MAX_EVENT_TIMEOUT, Observation, URLFilter, invalid,
)

PREFIX = '/automation/v1'
MAX_REQUEST = 321 * 1024 * 1024
CALL_TIMEOUT = 330
LEASE_SECONDS = 90
_REQUEST_ACCESS = ContextVar('studio_automation_access', default=None)


def options(value, page=None, depth=0):
    if not isinstance(value, dict):
        raise invalid()
    return {OPTION_NAMES.get(key, key): decode(item, page, depth + 1)
            for key, item in value.items() if isinstance(key, str)}


OPTION_NAMES = {
    'waitUntil': 'wait_until', 'noWaitAfter': 'no_wait_after',
    'hasText': 'has_text', 'hasNotText': 'has_not_text', 'hasNot': 'has_not',
    'includeHidden': 'include_hidden', 'clickCount': 'click_count',
    'strict': 'strict', 'timeout': 'timeout', 'trial': 'trial',
}
BUILDERS = {
    'locator': ('locator', 1), 'getByRole': ('get_by_role', 1),
    'getByText': ('get_by_text', 1), 'getByLabel': ('get_by_label', 1),
    'getByPlaceholder': ('get_by_placeholder', 1), 'getByTestId': ('get_by_test_id', 1),
    'filter': ('filter', 0), 'nth': ('nth', 1), 'and': ('and_', 1), 'or': ('or_', 1),
}
LOCATOR_METHODS = {
    'click': ('click', 0), 'dblclick': ('dblclick', 0), 'fill': ('fill', 1),
    'press': ('press', 1), 'hover': ('hover', 0),
    'scrollIntoViewIfNeeded': ('scroll_into_view_if_needed', 0),
    'waitFor': ('wait_for', 0), 'isVisible': ('is_visible', 0),
    'isEnabled': ('is_enabled', 0), 'isChecked': ('is_checked', 0),
    'count': ('count', 0), 'innerText': ('inner_text', 0),
    'textContent': ('text_content', 0), 'getAttribute': ('get_attribute', 1),
    'inputValue': ('input_value', 0), 'check': ('check', 0), 'uncheck': ('uncheck', 0),
    'allTextContents': ('all_text_contents', 0),
}


def decode(value, page=None, depth=0):
    if depth > 16:
        raise invalid()
    if isinstance(value, list):
        return [decode(item, page, depth + 1) for item in value]
    if isinstance(value, dict):
        if '$locator' in value:
            if page is None or set(value) != {'$locator'}:
                raise invalid()
            return locator(page, value['$locator'], depth + 1)
        if '$regex' in value:
            if set(value) - {'$regex', 'flags'} or not isinstance(value['$regex'], str):
                raise invalid()
            flags = value.get('flags', '')
            if not isinstance(flags, str) or set(flags) - set('imsu'):
                raise invalid()
            try:
                return re.compile(value['$regex'], sum(flag for char, flag in (
                    ('i', re.I), ('m', re.M), ('s', re.S)) if char in flags))
            except re.error:
                raise invalid() from None
        return {key: decode(item, page, depth + 1) for key, item in value.items()}
    return value


def arguments(args, count, page=None, depth=0):
    if not isinstance(args, list) or len(args) not in (count, count + 1):
        raise invalid()
    positional = [decode(value, page, depth + 1) for value in args[:count]]
    keyword = options(args[count], page, depth + 1) if len(args) > count else {}
    return positional, keyword


def locator(page, steps, depth=0):
    if depth > 16 or not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise invalid()
    target = page
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'method', 'args'}:
            raise invalid()
        method, args = step['method'], step['args']
        if method in ('first', 'last') and target is not page and args == []:
            target = getattr(target, method)
        elif isinstance(method, str) and method in BUILDERS:
            name, count = BUILDERS[method]
            positional, keyword = arguments(args, count, page, depth)
            target = getattr(target, name)(*positional, **keyword)
        else:
            raise invalid()
    return target


def files(value):
    values = value if isinstance(value, list) else [value]
    if len(values) > 8:
        raise invalid()
    result = []
    for item in values:
        if not isinstance(item, dict) or set(item) != {'$file'}:
            raise invalid()
        entry = item['$file']
        if not isinstance(entry, dict) or set(entry) != {'name', 'mimeType', 'base64'}:
            raise invalid()
        name, mime, encoded = entry['name'], entry['mimeType'], entry['base64']
        if (not isinstance(name, str) or not 1 <= len(name) <= 255
                or any(char in name for char in '/\\\x00') or name in ('.', '..')
                or not isinstance(mime, str) or not 1 <= len(mime) <= 256
                or not isinstance(encoded, str) or len(encoded) > 40 * 1024 * 1024):
            raise invalid()
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise invalid() from None
        result.append({'name': name, 'mimeType': mime, 'buffer': data})
    return result


@dataclass
class Lease:
    profile_id: str
    context: object
    expires: float
    access: object = None
    owner: str = ''
    pages: dict = field(default_factory=dict)
    handles: dict = field(default_factory=dict)
    tasks: set = field(default_factory=set)
    on_close: object = None
    releasing: bool = False
    closed_pages: set = field(default_factory=set)
    cleanup_tasks: set = field(default_factory=set)


class AutomationBridge:
    def __init__(self, engine, *, clock=time.monotonic, lease_seconds=LEASE_SECONDS,
                 access=None, start_profile=None, create_profile=None, restart_profile=None,
                 recycle_profile=None, can_recycle=None, renew_access=True):
        self.engine = engine
        self.access, self.start_profile = access, start_profile
        self.create_profile = create_profile
        self.restart_profile = restart_profile
        self.recycle_profile = recycle_profile
        self.can_recycle = can_recycle
        self.restarts = {}
        self.recycles = {}
        self.recycle_failures = set()
        self.renew_access = renew_access
        self.starts = {}
        self.clock, self.lease_seconds = clock, lease_seconds
        self.discovery_path = engine.store.root / 'automation.json'
        self.server = None
        self.leases = {}
        self.releases = {}
        self.connections = set()
        self.pending_pages = set()
        self.late_cleanup = set()
        self.sweeper = None
        self.token = ''
        self.base_url = ''
        self.authenticate = None
        self.allowed_networks = ()
        self.private_network_only = False
        self.body_budget = {'bytes': 0, 'limit': 2 * MAX_DOWNLOAD}

    @property
    def access(self):
        return _REQUEST_ACCESS.get() or self._access

    @access.setter
    def access(self, value):
        self._access = value

    async def start_network(self, *, host, port, origin, authenticate, ssl=None,
                            allowed_networks=(), private_network_only=False):
        if self.start_profile is None or self.create_profile is None:
            raise ValueError('Network automation requires durable team execution callbacks.')
        self.authenticate = authenticate
        self.allowed_networks, self.private_network_only = allowed_networks, private_network_only
        self.server = await asyncio.start_server(self._connection, host, port, limit=16384, ssl=ssl)
        parsed = urlsplit(origin)
        hostname = '[' + parsed.hostname + ']' if ':' in parsed.hostname else parsed.hostname
        port = self.server.sockets[0].getsockname()[1]
        authority = hostname if port == (443 if parsed.scheme == 'https' else 80) else f'{hostname}:{port}'
        self.base_url = f'{parsed.scheme}://{authority}'
        self.sweeper = asyncio.create_task(self._sweep())

    async def start(self):
        if self.server:
            return
        if self._access is None:
            # The token-only interface for memberless workspaces was removed with studio-host.
            raise ValueError('Local automation requires a member login.')
        self.token = secrets.token_urlsafe(32)
        self.server = await asyncio.start_server(self._connection, '127.0.0.1', 0, limit=16384)
        port = self.server.sockets[0].getsockname()[1]
        self.base_url = f'http://127.0.0.1:{port}'
        try:
            # The workspace itself is private; POSIX also restricts the credential file.
            temporary = self.discovery_path.with_suffix('.json.tmp')
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                json.dump({'protocolVersion': 1, 'baseURL': self.base_url, 'token': self.token,
                           'scope': 'team-member', 'member': self._authorize()['member']['name']}, stream)
            temporary.replace(self.discovery_path)
        except Exception:
            temporary.unlink(missing_ok=True)
            await self.stop()
            raise
        self.sweeper = asyncio.create_task(self._sweep())

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        if self.authenticate is None:
            self.discovery_path.unlink(missing_ok=True)
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
            self.sweeper = None
        await asyncio.gather(*(self.release(key) for key in list(self.leases)))
        await asyncio.gather(*list(self.late_cleanup), return_exceptions=True)
        tasks = list(self.connections)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*self.starts.values(), return_exceptions=True)
        await asyncio.gather(*self.restarts.values(), *self.recycles.values(), return_exceptions=True)
        self.token = self.base_url = ''

    async def _sweep(self):
        while True:
            await asyncio.sleep(5)
            await self.expire()

    async def expire(self):
        if self._access:
            try:
                check = self._access.renew_local_automation if self.renew_access else self._access.resolve
                await asyncio.to_thread(check)
            except Exception:
                self.discovery_path.unlink(missing_ok=True)
                await asyncio.gather(*(self.release(key) for key in list(self.leases)))
                return
        for key, lease in list(self.leases.items()):
            try:
                self._authorize(lease.profile_id, access=lease.access)
            except AutomationError:
                await self.release(key)
            for handle_id, handle in list(lease.handles.items()):
                if handle.get('expires', float('inf')) <= self.clock():
                    await self._release_handle(lease, handle_id, handle)
        await asyncio.gather(*(self.release(key) for key, lease in list(self.leases.items())
                               if lease.expires <= self.clock() and lease.profile_id not in self.recycles))

    def _authorize(self, profile_id=None, *, admin=False, access=None):
        access = access or self.access
        if access is None:
            raise AutomationError('unauthorized', 'Automation authentication is required.', 401)
        from .store import ValidationError
        try:
            session = access.resolve()
            if admin:
                access.require_admin()
            if profile_id is not None:
                access.require_profile(profile_id, 'operate')
            return session
        except ValidationError as error:
            code = str(error) if str(error) in ('session_expired', 'admin_required') else 'access_denied'
            message = ('自动化授权已失效，请检查密钥或成员权限。' if code == 'session_expired' else
                       '创建环境需要管理员权限。' if code == 'admin_required' else '该成员没有此环境的操作权限。')
            raise AutomationError(code, message, 401 if code == 'session_expired' else 403) from None

    def _profiles(self):
        return self.access.list_profiles()

    async def _start(self, profile_id, headless, main_world_eval):
        if self.start_profile is None:
            await self.engine.start(profile_id, headless=headless, main_world_eval=main_world_eval)
            return self.engine.sessions[profile_id]
        task = self.starts.get(profile_id)
        if task is None:
            task = asyncio.create_task(self.start_profile(profile_id, headless, main_world_eval=main_world_eval))
            self.starts[profile_id] = task
            task.add_done_callback(lambda finished: self._start_finished(profile_id, finished))
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.starts.get(profile_id) is task:
                self.starts.pop(profile_id, None)

    def _start_finished(self, profile_id, task):
        if self.starts.get(profile_id) is task:
            self.starts.pop(profile_id, None)
        if not task.cancelled():
            task.exception()

    def _profile(self, profile):
        session = self.engine.sessions.get(profile['id'])
        state = ('stopping' if session.stop.is_set() else
                 'running' if session.context is not None else 'starting') if session else 'stopped'
        return {key: profile[key] for key in ('id', 'name')} | {
            'state': state, 'headless': session.headless if session else profile['headless'],
            'mainWorldEval': session.main_world_eval if session and session.context is not None else None}

    def _idle(self, lease, session):
        from .resources import blank_browser
        if (lease.releasing or lease.pages or lease.handles or lease.tasks or lease.cleanup_tasks
                or session.stop.is_set() or session.error or session.context is None
                or getattr(self.engine, 'recovery_required', False)
                or session.automation_id in self.recycle_failures
                or sum(item.profile_id == lease.profile_id for item in self.leases.values()) != 1
                or (self.can_recycle is not None and not self.can_recycle(lease.profile_id, session))):
            return False
        return blank_browser(session.context)

    async def _recycle(self, key, lease, session):
        self._authorize(lease.profile_id, access=lease.access)
        if self._lease(key, maintenance=True) is not lease or not self._idle(lease, session):
            raise AutomationError('profile_busy', '环境仍有任务或人工标签页，未回收浏览器。', 409)
        try:
            replacement = await asyncio.wait_for(self.recycle_profile(lease.profile_id, session), 180)
        except BaseException as error:
            unchanged = self.engine.sessions.get(lease.profile_id) is session and not session.stop.is_set()
            if not (isinstance(error, AutomationError) and error.code == 'profile_busy' and unchanged
                    and not getattr(self.engine, 'recovery_required', False)):
                self.recycle_failures.intersection_update(item.automation_id for item in self.engine.sessions.values())
                self.recycle_failures.add(session.automation_id)
            raise
        if (replacement is None or replacement is session or replacement.context is None
                or replacement.stop.is_set() or self.engine.sessions.get(lease.profile_id) is not replacement):
            self.recycle_failures.add(session.automation_id)
            raise AutomationError('profile_recycle_unconfirmed', '未能确认浏览器正常重开，请人工检查。', 409)
        await self.release(key)
        return {'recycled': True, 'previousSessionId': session.automation_id,
                'sessionId': replacement.automation_id}

    def _lease(self, key, *, maintenance=False):
        lease = self.leases.get(key)
        if (lease is None or lease.releasing
                or lease.owner != getattr(self.access, 'api_key_id', '')):
            raise AutomationError('lease_not_found', 'The automation lease is no longer available.', 404)
        if lease.expires <= self.clock() and lease.profile_id not in self.recycles:
            asyncio.create_task(self.release(key))
            raise AutomationError('lease_expired', 'The automation lease expired.', 410)
        session = self.engine.sessions.get(lease.profile_id)
        self._authorize(lease.profile_id)
        if lease.profile_id in self.recycles and not maintenance:
            raise AutomationError('profile_busy', '此环境正在维护，请稍后重试。', 409)
        if session is None or session.context is not lease.context or session.stop.is_set():
            asyncio.create_task(self.release(key))
            raise AutomationError('profile_not_running', 'Start this profile in Studio before connecting.', 409)
        return lease

    async def release(self, key):
        task = self.releases.get(key)
        if task is None:
            lease = self.leases.get(key)
            if lease is None:
                return
            lease.releasing = True
            task = asyncio.create_task(self._release(key, lease))
            self.releases[key] = task
            task.add_done_callback(lambda _: self.releases.pop(key, None))
        # HTTP cancellation must not strand a profile in its reserved state.
        await asyncio.shield(task)

    async def _release(self, key, lease):
        # Retain the reservation until page cleanup completes, including concurrent acquisitions.
        tasks = [task for task in lease.tasks if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        lease.context.remove_listener('close', lease.on_close)
        await asyncio.gather(*(self._dispose_handle(handle) for handle in list(lease.handles.values())))
        await asyncio.gather(*(self._close_page(page) for page in list(lease.pages.values()) if page))
        await asyncio.gather(*list(lease.cleanup_tasks), return_exceptions=True)
        lease.handles.clear()
        lease.pages.clear()
        self.leases.pop(key, None)

    @staticmethod
    async def _close_page(page):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(page.close(), 5)

    def _late_page(self, creation):
        if creation.cancelled():
            return
        with contextlib.suppress(Exception):
            task = asyncio.create_task(self._close_page(creation.result()))
            self.late_cleanup.add(task)
            task.add_done_callback(self.late_cleanup.discard)

    @staticmethod
    async def _dispose_handle(handle, excluding=None):
        if handle['type'] == 'event':
            handle['cleanup']()
            if not handle['value'].done():
                handle['value'].set_result(None)
        if handle['type'] == 'observation':
            handle['value'].close()
        if handle.get('transfer'):
            handle['transfer'].close()
        tasks = [task for task in handle.get('operations', ()) if task not in (asyncio.current_task(), excluding)]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await AutomationBridge._discard_download(handle)

    async def _release_handle(self, lease, key, handle):
        lease.handles.pop(key, None)
        task = asyncio.create_task(self._dispose_handle(handle, excluding=asyncio.current_task()))
        lease.cleanup_tasks.add(task)
        task.add_done_callback(lease.cleanup_tasks.discard)
        await asyncio.shield(task)

    @staticmethod
    async def _discard_download(handle):
        value = handle['value']
        if handle['type'] == 'event':
            if handle['event'] != 'download' or not value.done() or value.cancelled():
                return
            value = value.result()
        elif handle['type'] != 'download':
            return
        if value is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(value.cancel(), 5)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(value.delete(), 5)

    def _page_closed(self, lease, page_id):
        lease.pages.pop(page_id, None)
        if len(lease.closed_pages) >= 256:
            lease.closed_pages.pop()
        lease.closed_pages.add(page_id)
        handles = [key for key, handle in lease.handles.items() if handle['pageId'] == page_id]
        for key in handles:
            handle = lease.handles.pop(key)
            if handle['type'] == 'event':
                handle['cleanup']()
                if not handle['value'].done():
                    handle['value'].set_result(None)
            if handle['type'] == 'observation':
                handle['value'].close()
            task = asyncio.create_task(self._dispose_handle(handle))
            lease.cleanup_tasks.add(task)
            task.add_done_callback(lease.cleanup_tasks.discard)

    async def _new_task_page(self, lease):
        # Juggler's context.new_page opens a top-level window. A normal popup
        # without window features uses Firefox's native tabs in the anchor window.
        candidates = [page for page in lease.pages.values() if page] + list(lease.context.pages)
        anchor = next((page for page in candidates if not page.is_closed()), None)
        if anchor is None:
            raise AutomationError('profile_not_running', '环境中没有可用的浏览器窗口，请重新连接。', 409)
        url = 'about:blank#studio-task-' + secrets.token_urlsafe(18)
        page = None
        try:
            # A unique URL keeps simultaneous creations and unrelated popups apart.
            async with lease.context.expect_page(predicate=lambda page: page.url == url, timeout=15000) as pending:
                opened = await anchor.evaluate('''url => {
                    const tab = window.open(url, '_blank');
                    return !!tab;
                }''', arg=url)
                if not opened:
                    raise AutomationError('tab_creation_failed', '无法在当前窗口创建任务标签页。', 502)
            page = await pending.value
            await page.wait_for_load_state('domcontentloaded', timeout=15000)
            # A second goto can race Firefox's initial popup navigation.
            await page.evaluate('''() => {
                window.opener = null;
                history.replaceState(null, '', 'about:blank');
            }''')
            return page
        except BaseException:
            cleanup = [page] if page else [candidate for candidate in lease.context.pages if candidate.url == url]
            await asyncio.gather(*(self._close_page(candidate) for candidate in cleanup))
            raise

    async def dispatch(self, method, route, body):
        self._authorize()
        result = await self._dispatch(method, route, body)
        self._authorize()
        if isinstance(result, BinaryResult):
            key = route.split('/')[4]
            lease = self._lease(key)
            handle = lease.handles.get(body.get('handleId'))
            if handle is None or handle['pageId'] not in lease.pages:
                raise AutomationError('handle_not_found', 'The byte transfer was released.', 404)
        return result

    async def _dispatch(self, method, route, body):
        if method == 'POST' and route == PREFIX + '/profiles':
            from .store import ValidationError
            self._authorize(admin=True)
            try:
                if self.create_profile is not None:
                    profile = await self.create_profile(body)
                else:
                    creation = asyncio.create_task(asyncio.to_thread(self.access.save_profile, body))
                    try:
                        profile = await asyncio.shield(creation)
                    except asyncio.CancelledError:
                        # Finish storage before shutdown can release workspace ownership.
                        await creation
                        raise
            except ValidationError:
                self._authorize(admin=True)
                raise invalid() from None
            return {'profile': self._profile(profile)}
        start_match = re.fullmatch(re.escape(PREFIX) + r'/profiles/([A-Za-z0-9_-]+)/start', route)
        if method == 'POST' and start_match:
            if (not isinstance(body, dict) or set(body) - {'headless', 'mainWorldEval', 'restartIfNeeded'}
                    or any(type(body.get(key, False)) is not bool for key in ('headless', 'mainWorldEval', 'restartIfNeeded'))
                    or (body.get('restartIfNeeded') and body.get('mainWorldEval') is not True)):
                raise invalid()
            headless = body.get('headless', False)
            main_world_eval = body.get('mainWorldEval', False)
            profile_id = start_match.group(1)
            self._authorize(profile_id)
            profile = next((item for item in self._profiles()
                            if item['id'] == profile_id), None)
            if profile is None:
                raise AutomationError('profile_not_found', '所选环境不存在或已归档。', 404)
            session = self.engine.sessions.get(profile_id)
            if profile_id in self.restarts or profile_id in self.recycles:
                raise AutomationError('profile_busy', '此环境正在维护，请稍后重试。', 409)
            if (session is not None and body.get('restartIfNeeded') and main_world_eval
                    and session.main_world_eval is not True):
                if self.restart_profile is None:
                    raise AutomationError('main_world_restart_unsupported', '此 Studio 不支持按需重启运行会话。', 409)
                if session.context is None or session.stop.is_set() or session.headless != headless:
                    raise AutomationError('profile_unavailable', '此环境尚未就绪或运行模式不符。', 409)
                if any(lease.profile_id == profile_id for lease in self.leases.values()):
                    raise AutomationError('profile_leased', '请等待此环境的自动化任务释放连接后再升级运行能力。', 409)
                # Reserve before yielding; other callers cannot lease or replace this run.
                task = asyncio.create_task(self.restart_profile(profile_id, session, headless))
                self.restarts[profile_id] = task
                def finished(result, identifier=profile_id):
                    if self.restarts.get(identifier) is result:
                        self.restarts.pop(identifier, None)
                    if not result.cancelled():
                        result.exception()
                task.add_done_callback(finished)
                session = await asyncio.shield(task)
            if session is None:
                from .engine import public_error
                try:
                    # Session-scoped override: preserve the saved identity, proxy and mode.
                    session = await self._start(profile_id, headless, main_world_eval)
                except AutomationError:
                    raise
                except Exception as error:
                    raise AutomationError('profile_start_failed', public_error(error), 409) from None
            if session is None:
                raise AutomationError('profile_start_failed', '环境启动已停止。', 409)
            if session.stop.is_set():
                raise AutomationError('profile_unavailable', '此环境正在停止，请稍后重试。', 409)
            if session.headless != headless:
                raise AutomationError('profile_mode_mismatch', '环境的运行模式与所选模式不同，请先在 Studio 停止此环境后再连接。', 409)
            if session.context is None:
                try:
                    await asyncio.wait_for(session.ready.wait(), 120)
                except TimeoutError:
                    raise AutomationError('profile_start_timeout', '环境启动超时，请在 Studio 检查启动状态。', 504) from None
            if (self.engine.sessions.get(profile_id) is not session
                    or session.context is None or session.stop.is_set()):
                code = 'main_world_unsupported' if session.error_code == 'main_world_unsupported' else 'profile_start_failed'
                raise AutomationError(code, session.error or '环境启动已停止。', 409)
            if main_world_eval and session.main_world_eval is not True:
                raise AutomationError('profile_main_world_mismatch',
                                      '此环境当前会话未开启主世界执行，请等待现有任务和人工标签完成后，仅停止此环境再按需启动。', 409)
            return {'profile': self._profile(profile)}
        if method == 'GET' and route == PREFIX + '/profiles':
            await self.expire()
            capabilities = dict(CAPABILITIES)
            if self.restart_profile is not None:
                capabilities['mainWorldRestart'] = True
            if self.recycle_profile is not None:
                capabilities['browserMemoryMaintenance'] = True
            return {'protocolVersion': 1, 'capabilities': capabilities, 'profiles': [
                self._profile(profile) | {'leased': any(lease.profile_id == profile['id']
                                                        for lease in self.leases.values())}
                for profile in self._profiles()]}
        if method == 'POST' and route == PREFIX + '/leases':
            if set(body) - {'profileId', 'clientName'} or not isinstance(body.get('profileId'), str):
                raise invalid()
            if 'clientName' in body and (not isinstance(body['clientName'], str) or len(body['clientName']) > 128):
                raise invalid()
            await self.expire()
            self._authorize(body['profileId'])
            if body['profileId'] in self.restarts or body['profileId'] in self.recycles:
                raise AutomationError('profile_busy', '此环境正在维护，请稍后重试。', 409)
            profile = next((item for item in self._profiles()
                            if item['id'] == body['profileId']), None)
            if profile is None or self._profile(profile)['state'] != 'running':
                raise AutomationError('profile_not_running', 'Start this profile in Studio before connecting.', 409)
            if any(lease.profile_id == profile['id'] for lease in self.leases.values()):
                raise AutomationError('profile_leased', 'This profile is already used by an automation client.', 409)
            key = secrets.token_urlsafe(24)
            context = self.engine.sessions[profile['id']].context
            lease = Lease(profile['id'], context, self.clock() + self.lease_seconds,
                          access=self.access, owner=getattr(self.access, 'api_key_id', ''))
            lease.on_close = lambda *_: asyncio.create_task(self.release(key))
            self.leases[key] = lease
            context.on('close', lease.on_close)
            return {'leaseId': key, 'profile': self._profile(profile)}
        match = re.fullmatch(re.escape(PREFIX) + r'/leases/([A-Za-z0-9_-]+)(/heartbeat|/pages|/call|/resources|/recycle)?', route)
        if not match:
            raise AutomationError('not_found', 'Unknown automation endpoint.', 404)
        key, action = match.groups()
        lease = self._lease(key)
        if method == 'GET' and action == '/resources':
            from .resources import profile_memory
            session = self.engine.sessions[lease.profile_id]
            memory = await asyncio.to_thread(profile_memory, self.engine.store, lease.profile_id)
            if self._lease(key) is not lease or self.engine.sessions.get(lease.profile_id) is not session:
                raise AutomationError('session_changed', '环境会话已变化，请重新检查。', 409)
            return {'version': 1, 'sessionId': session.automation_id, **memory,
                    'idle': self._idle(lease, session)}
        if method == 'POST' and action == '/recycle':
            if (not isinstance(body, dict) or set(body) != {'sessionId'}
                    or not isinstance(body['sessionId'], str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', body['sessionId'])):
                raise invalid()
            if self.recycle_profile is None:
                raise AutomationError('browser_recycle_unsupported', '此 Studio 不支持空闲浏览器回收。', 409)
            session = self.engine.sessions[lease.profile_id]
            if session.automation_id != body['sessionId']:
                raise AutomationError('session_changed', '环境会话已变化，未回收替换会话。', 409)
            if session.automation_id in self.recycle_failures:
                raise AutomationError('profile_recycle_unconfirmed', '上次回收状态不明，请先人工检查。', 409)
            if lease.profile_id in self.recycles or not self._idle(lease, session):
                raise AutomationError('profile_busy', '环境仍有任务或人工标签页，未回收浏览器。', 409)
            task = asyncio.create_task(self._recycle(key, lease, session))
            self.recycles[lease.profile_id] = task
            def recycled(finished, identifier=lease.profile_id):
                if self.recycles.get(identifier) is finished:
                    self.recycles.pop(identifier, None)
                if self.leases.get(key) is lease and not lease.releasing:
                    lease.expires = self.clock() + self.lease_seconds
                if not finished.cancelled():
                    finished.exception()
            task.add_done_callback(recycled)
            return await asyncio.shield(task)
        if method == 'DELETE' and action is None:
            await self.release(key)
            return {}
        if method == 'POST' and action == '/heartbeat':
            lease.expires = self.clock() + self.lease_seconds
            return {}
        task = asyncio.current_task()
        lease.tasks.add(task)
        try:
            if method == 'POST' and action == '/pages':
                if len(lease.pages) >= 8 or len(self.pending_pages) >= 64:
                    raise AutomationError('resource_limit', 'A lease supports at most eight pages.', 409)
                # Reserve before awaiting creation so parallel requests cannot bypass the limit.
                page_id = secrets.token_urlsafe(18)
                lease.pages[page_id] = None
                creation = asyncio.create_task(self._new_task_page(lease))
                self.pending_pages.add(creation)
                creation.add_done_callback(self.pending_pages.discard)
                try:
                    page = await asyncio.shield(creation)
                    lease.pages[page_id] = page
                    page.on('close', lambda *_: self._page_closed(lease, page_id))
                except asyncio.CancelledError:
                    # A stalled driver must not block releasing its lease. Close a late result.
                    creation.add_done_callback(self._late_page)
                    lease.pages.pop(page_id, None)
                    raise
                except BaseException:
                    lease.pages.pop(page_id, None)
                    raise
                return {'pageId': page_id, 'url': page.url}
            if method == 'POST' and action == '/call':
                return await self.call(lease, body)
            raise AutomationError('not_found', 'Unknown automation endpoint.', 404)
        finally:
            lease.tasks.discard(task)

    async def call(self, lease, body):
        if set(body) - {'pageId', 'handleId', 'locator', 'method', 'args'}:
            raise invalid()
        method, args = body.get('method'), body.get('args', [])
        if not isinstance(method, str) or not isinstance(args, list):
            raise invalid()
        page_id, handle_id = body.get('pageId'), body.get('handleId')
        if page_id is not None and not isinstance(page_id, str):
            raise invalid()
        page = lease.pages.get(page_id)
        if page_id is not None and page is None:
            if page_id in lease.closed_pages:
                raise AutomationError('page_closed', 'The automation page was closed.', 410)
            raise AutomationError('page_not_found', 'This page does not belong to the lease.', 404)
        if handle_id is not None:
            if not isinstance(handle_id, str) or body.get('locator') is not None:
                raise invalid()
            handle = lease.handles.get(handle_id)
            if handle is None or (page_id is not None and handle['pageId'] != page_id):
                raise AutomationError('handle_not_found', 'The event handle is no longer available.', 404)
            if handle.get('expires', float('inf')) <= self.clock():
                await self._release_handle(lease, handle_id, handle)
                raise AutomationError('handle_expired', 'The browser resource expired.', 410)
            if (method == 'readBinary' or handle['type'] == 'observation') and page_id is None:
                raise invalid()
            operation = asyncio.current_task()
            handle.setdefault('operations', set()).add(operation)
            try:
                value = await self._handle(lease, handle_id, handle, method, args)
            except asyncio.CancelledError:
                if not lease.releasing and handle_id not in lease.handles:
                    if page_id in lease.closed_pages:
                        raise AutomationError('page_closed', 'The automation page was closed.', 410) from None
                    raise AutomationError('handle_not_found', 'The browser resource was released.', 404) from None
                raise
            finally:
                handle['operations'].discard(operation)
        elif page is None:
            if body.get('locator') is not None:
                raise invalid()
            if method == 'addCookies' and len(args) == 1 and isinstance(args[0], list):
                value = await lease.context.add_cookies(args[0])
            elif method == 'cookies' and len(args) <= 1:
                value = await lease.context.cookies(*args)
            else:
                raise invalid()
        elif method == 'expectEvent' and body.get('locator') is None:
            value = self._expect(lease, page_id, page, args)
        elif method == 'observeResponse' and body.get('locator') is None:
            if len(args) != 1:
                raise invalid()
            if len(lease.handles) >= 32:
                raise AutomationError('resource_limit', 'A lease supports at most 32 handles.', 409)
            observation = Observation(page, args[0])
            key = secrets.token_urlsafe(18)
            lease.handles[key] = {'type': 'observation', 'value': observation, 'pageId': page_id,
                                  'expires': self.clock() + MAX_EVENT_TIMEOUT + HANDLE_SECONDS}
            value = {'$handle': key, 'type': 'observation'}
        else:
            steps = body.get('locator')
            target = locator(page, steps) if steps is not None else page
            if method in ('evaluate', 'evaluateAll') and (method != 'evaluateAll' or steps is not None):
                if len(args) not in (1, 2) or not isinstance(args[0], str):
                    raise invalid()
                # Evaluation arguments are application data; do not rename or decode their keys.
                value = await getattr(target, 'evaluate_all' if method == 'evaluateAll' else 'evaluate')(
                    args[0], arg=args[1] if len(args) == 2 else None)
            elif steps is not None and method == 'setInputFiles':
                positional, keyword = arguments(args, 1)
                value = await target.set_input_files(files(positional[0]), **keyword)
            elif steps is not None and method == 'selectOption':
                positional, keyword = arguments(args, 1)
                selection = positional[0]
                if isinstance(selection, dict):
                    keyword.update(selection)
                elif isinstance(selection, list) and selection and isinstance(selection[0], dict):
                    keys = set(selection[0])
                    if len(keys) != 1 or any(set(item) != keys for item in selection):
                        raise invalid()
                    keyword[next(iter(keys))] = [next(iter(item.values())) for item in selection]
                else:
                    keyword['value'] = selection
                if set(keyword) - {'value', 'label', 'index', 'timeout', 'force', 'no_wait_after'}:
                    raise invalid()
                value = await target.select_option(**keyword)
            elif steps is not None and method in LOCATOR_METHODS:
                name, count = LOCATOR_METHODS[method]
                positional, keyword = arguments(args, count, page)
                value = await getattr(target, name)(*positional, **keyword)
            elif steps is None and method == 'mouse.click':
                positional, keyword = arguments(args, 2)
                timeout = keyword.pop('timeout', 30000)
                if (any(type(value) not in (int, float) or not math.isfinite(value) for value in positional)
                        or type(timeout) not in (int, float) or not 0 < timeout <= CALL_TIMEOUT * 1000
                        or set(keyword) - {'button', 'click_count', 'delay'}):
                    raise invalid()
                try:
                    value = await asyncio.wait_for(page.mouse.click(*positional, **keyword), timeout / 1000)
                except BaseException:
                    # A timeout can occur between down and up; release the held button.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(page.mouse.up(button=keyword.get('button', 'left')), 1)
                    raise
            elif steps is None and method in ('goto', 'title', 'content', 'close', 'keyboard.press', 'waitForLoadState'):
                positional, keyword = arguments(args, 1 if method in ('goto', 'keyboard.press', 'waitForLoadState') else 0)
                callable_ = (page.keyboard.press if method == 'keyboard.press' else
                             getattr(page, 'wait_for_load_state' if method == 'waitForLoadState' else method))
                value = await callable_(*positional, **keyword)
                if method == 'goto':
                    value = None
                if method == 'close':
                    lease.pages.pop(page_id, None)
            else:
                raise invalid()
        if isinstance(value, BinaryResult):
            return value
        return {'value': value} | ({'url': page.url, 'closed': page.is_closed()} if page else {})

    def _expect(self, lease, page_id, page, args):
        if len(args) not in (1, 2) or args[0] not in ('download', 'filechooser'):
            raise invalid()
        opts = args[1] if len(args) == 2 else {}
        if (not isinstance(opts, dict) or set(opts) - {'timeout', 'urlFilter'}
                or ('urlFilter' in opts and args[0] != 'download')):
            raise invalid()
        url_filter = URLFilter(opts['urlFilter']) if 'urlFilter' in opts else None
        timeout = opts.get('timeout', 30000)
        if type(timeout) not in (int, float) or not 0 < timeout <= MAX_EVENT_TIMEOUT * 1000:
            raise invalid()
        if len(lease.handles) >= 32:
            raise AutomationError('resource_limit', 'A lease supports at most 32 event handles.', 409)
        key = secrets.token_urlsafe(18)
        future = asyncio.get_running_loop().create_future()
        event = args[0]

        def received(value):
            if url_filter is not None and not url_filter.matches(value.url):
                return
            if not future.done():
                future.set_result(value)
            cleanup()

        def expired():
            if not future.done():
                future.set_result(None)
            cleanup()

        def cleanup():
            page.remove_listener(event, received)
            timer.cancel()

        timer = asyncio.get_running_loop().call_later(timeout / 1000, expired)
        page.on(event, received)
        lease.handles[key] = {'type': 'event', 'event': event, 'value': future,
                              'cleanup': cleanup, 'pageId': page_id,
                              'expires': self.clock() + timeout / 1000 + HANDLE_SECONDS}
        return {'$handle': key, 'type': 'event'}

    async def _handle(self, lease, key, handle, method, args):
        kind, value = handle['type'], handle['value']
        if method == 'delete' and not args:
            await self._release_handle(lease, key, handle)
            return None
        if kind == 'observation' and method in ('result', 'status') and not args:
            return await value.result() if method == 'result' else value.status()
        if kind in ('observation', 'download') and method == 'readBinary' and len(args) <= 1:
            if 'transfer' not in handle:
                handle['transfer'] = BodyTransfer(value, observation=value if kind == 'observation' else None,
                                                   budget=self.body_budget)
            try:
                return await handle['transfer'].read(args[0] if args else {})
            except BaseException:
                if handle['transfer'].closed and lease.handles.get(key) is handle:
                    await self._release_handle(lease, key, handle)
                raise
        if kind == 'event' and method == 'result' and not args:
            result = await asyncio.shield(value)
            if result is None:
                lease.handles.pop(key, None)
                if handle['pageId'] in lease.closed_pages:
                    raise AutomationError('page_closed', 'The automation page was closed.', 410)
                raise AutomationError('operation_timeout', 'The browser event timed out.', 408, 'TimeoutError')
            handle.update(type=handle['event'], value=result)
            return {'$handle': key, 'type': handle['type']} | (
                {'suggestedFilename': result.suggested_filename} if handle['type'] == 'download'
                else {'multiple': result.is_multiple()})
        if kind == 'filechooser' and method == 'setFiles':
            positional, keyword = arguments(args, 1)
            try:
                return await value.set_files(files(positional[0]), **keyword)
            finally:
                lease.handles.pop(key, None)
        if kind == 'download':
            if method == 'read' and len(args) <= 1:
                opts = args[0] if args else {}
                if not isinstance(opts, dict) or set(opts) - {'maxBytes'}:
                    raise invalid()
                limit = opts.get('maxBytes', MAX_DOWNLOAD)
                if type(limit) is not int or not 1 <= limit <= MAX_DOWNLOAD:
                    raise invalid()
                try:
                    path = await value.path()
                    if path is None:
                        raise AutomationError('download_failed', 'The browser download failed.', 502)

                    def read():
                        with Path(path).open('rb') as stream:
                            data = stream.read(limit + 1)
                        if len(data) > limit:
                            raise AutomationError('image_size_limit', 'The download exceeds the requested size limit.', 413)
                        return {'base64': base64.b64encode(data).decode('ascii')}

                    return await asyncio.to_thread(read)
                except BaseException:
                    lease.handles.pop(key, None)
                    await self._discard_download(handle)
                    raise
            if method == 'failure' and not args:
                result = await value.failure()
                return 'Download failed.' if result else None
        raise invalid()

    async def _connection(self, reader, writer):
        task = asyncio.current_task()
        if len(self.connections) >= 256:
            payload = b'{"error":{"code":"service_busy","message":"The local automation service is busy.","name":"Error"}}'
            writer.write((f'HTTP/1.1 503 Busy\r\nContent-Type: application/json\r\n'
                          f'Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n').encode() + payload)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.drain(), 1)
            writer.close()
            return
        self.connections.add(task)
        scope = _REQUEST_ACCESS.set(None)
        try:
            await self._serve(reader, writer)
        finally:
            _REQUEST_ACCESS.reset(scope)
            self.connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, reader, writer):
        operation = disconnected = None
        route = ''
        authenticated = False
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            lines = header.decode('ascii').split('\r\n')
            parts = lines[0].split(' ')
            if len(parts) != 3 or parts[2] != 'HTTP/1.1':
                raise invalid()
            method, route, _ = parts
            headers = {}
            for line in lines[1:-2]:
                key, separator, value = line.partition(':')
                if not separator or key.lower() in headers:
                    raise invalid()
                headers[key.lower()] = value.strip()
            expected_host = urlsplit(self.base_url).netloc
            if 'origin' in headers or headers.get('host') != expected_host:
                raise AutomationError('forbidden', 'Only application clients on the configured host are supported.', 403)
            if '?' in route or '#' in route or not route.startswith(PREFIX + '/'):
                raise invalid()
            authorization = headers.get('authorization', '')
            if self.authenticate is None:
                if not hmac.compare_digest(authorization, 'Bearer ' + self.token):
                    raise AutomationError('unauthorized', 'Automation authentication is required.', 401)
            else:
                from .store import ValidationError
                from .team.transport import allowed_client, private_address
                peer = writer.get_extra_info('peername')
                peer = peer[0] if peer else ''
                if (self.private_network_only and not private_address(peer, allow_loopback=True)
                        or self.allowed_networks and not allowed_client(peer, self.allowed_networks)):
                    raise AutomationError('forbidden', 'The direct client address is not allowed.', 403)
                if not authorization.startswith('Bearer '):
                    raise AutomationError('unauthorized', 'Automation authentication is required.', 401)
                try:
                    _REQUEST_ACCESS.set(await self.authenticate(authorization[7:], peer))
                except ValidationError as error:
                    denied = str(error) == 'access_denied'
                    raise AutomationError('access_denied' if denied else 'session_expired',
                                          'Automation authorization failed.', 403 if denied else 401) from None
            authenticated = True
            if 'transfer-encoding' in headers:
                raise invalid()
            length = headers.get('content-length', '0')
            if not length.isdecimal() or len(length) > 9 or int(length) > MAX_REQUEST:
                raise AutomationError('request_too_large', 'Request body exceeds the 321 MiB limit.', 413)
            if int(length) and headers.get('content-type', '').split(';')[0].strip() != 'application/json':
                raise invalid()
            raw = await asyncio.wait_for(reader.readexactly(int(length)), 30)
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                raise invalid()
            operation = asyncio.create_task(self.dispatch(method, route, body))
            disconnected = asyncio.create_task(reader.read(1))
            done, _ = await asyncio.wait((operation, disconnected), timeout=CALL_TIMEOUT,
                                         return_when=asyncio.FIRST_COMPLETED)
            if disconnected in done:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
                await self._release_route(route, method=method)
                return
            if operation not in done:
                raise asyncio.TimeoutError()
            result = operation.result()
            status = 200
        except asyncio.CancelledError:
            return
        except Exception as error:
            if isinstance(error, AutomationError):
                failure = error
            elif isinstance(error, (asyncio.TimeoutError,)) or type(error).__name__ == 'TimeoutError':
                failure = AutomationError('operation_timeout', 'The browser operation timed out.', 408, 'TimeoutError')
            elif type(error).__name__ == 'TargetClosedError':
                failure = AutomationError('page_closed', 'The automation page was closed.', 410)
            elif isinstance(error, (ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.LimitOverrunError)):
                failure = invalid()
            else:
                failure = AutomationError('browser_operation_failed', 'The browser operation failed.', 502)
            status = failure.status
            result = {'error': {'code': failure.code, 'message': str(failure), 'name': failure.name}}
        finally:
            for pending in (operation, disconnected):
                if pending and not pending.done():
                    pending.cancel()
            await asyncio.gather(*(item for item in (operation, disconnected) if item), return_exceptions=True)
        binary = isinstance(result, BinaryResult)
        metadata = ''
        try:
            if binary:
                encoded = result.data
                metadata = 'X-Studio-Transfer: ' + json.dumps(result.metadata, ensure_ascii=True, allow_nan=False) + '\r\n'
            else:
                encoded = json.dumps(result, ensure_ascii=True, allow_nan=False).encode('utf-8')
            if len(encoded) > 180 * 1024 * 1024:
                raise ValueError('Response size limit')
        except (TypeError, ValueError):
            status = 502
            binary, metadata = False, ''
            encoded = b'{"error":{"code":"invalid_result","message":"The browser result cannot be serialized.","name":"Error"}}'
        try:
            content_type = 'application/octet-stream' if binary else 'application/json'
            writer.write((f'HTTP/1.1 {status} Result\r\nContent-Type: {content_type}\r\n'
                         f'Content-Length: {len(encoded)}\r\nConnection: close\r\n'
                         + metadata + 'Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n\r\n').encode('ascii'))
            writer.write(encoded)
            await asyncio.wait_for(writer.drain(), 30)
        except (ConnectionError, asyncio.TimeoutError):
            if authenticated:
                await self._release_route(route, method=method)

    async def _release_route(self, route, *, method=None):
        # A cancelled renewal is not lease abandonment; normal expiry still applies.
        if method == 'POST' and re.fullmatch(
                re.escape(PREFIX) + r'/leases/[A-Za-z0-9_-]+/heartbeat', route):
            return
        match = re.fullmatch(re.escape(PREFIX) + r'/leases/([A-Za-z0-9_-]+)(?:/heartbeat|/pages|/call)?', route)
        if match:
            lease = self.leases.get(match[1])
            if lease is not None and lease.owner == getattr(self.access, 'api_key_id', ''):
                await self.release(match[1])
