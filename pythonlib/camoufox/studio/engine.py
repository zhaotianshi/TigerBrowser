"""Persistent browser sessions, kept on one asyncio loop outside the GUI thread."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlsplit

import requests

from ..async_api import AsyncCamoufox
from ..ip import Proxy, proxy_exit_geo
from ..utils import (
    get_env_vars,
    _camou_config_blob,
    _load_properties,
    get_pref_env_vars,
    get_target_os,
    launch_options,
    resolve_verstr,
    validate_config,
)
from .proxy_bridge import ProxyBridgeError, browser_proxy
from .distribution import browser_executable, bundled_path, default_addon_paths, profile_addon_paths
from .fingerprint_options import (
    apply_identity_options, apply_network_options, apply_screen_options, generation_arguments,
    normalize_options, validate_environment,
)
from .store import ValidationError, StoreError


class StudioError(RuntimeError):
    """A credential-free error that can be displayed directly."""


class ProxyCheckError(StudioError):
    """The original proxy failed its mandatory exit check."""


class MainWorldUnsupportedError(StudioError):
    def __init__(self):
        super().__init__('此环境的实际内核未声明支持主世界执行，请管理员核对该内核的 properties.json；环境未启动。')


def public_error(error: Exception) -> str:
    if isinstance(error, (StudioError, ProxyBridgeError)):
        return str(error)
    # Playwright and requests exceptions can contain authenticated proxy URLs.
    return f"操作失败（{type(error).__name__}）。请检查内核、代理及文件权限后重试。"


def check_proxy(proxy: dict) -> dict:
    started = time.monotonic()
    address = Proxy(
        server=proxy['server'],
        username=quote(proxy.get('username') or '', safe=''),
        password=quote(proxy.get('password') or '', safe=''),
    ).as_string()
    # requests uses local DNS for socks5, whereas this probe must use the proxy.
    if address.startswith('socks5://'):
        address = 'socks5h://' + address[len('socks5://'):]
    try:
        with requests.Session() as session:
            session.trust_env = False
            ip, timezone = proxy_exit_geo(address, session=session)
    except Exception as error:
        cause = error.__cause__ or error
        if isinstance(cause, requests.Timeout):
            reason = '代理连接或出口检查超时，请检查网络和代理可用性。'
        elif isinstance(cause, requests.HTTPError) and cause.response is not None:
            status = cause.response.status_code
            reason = ('出口检测服务限流（HTTP 429），请稍后再试。' if status == 429 else
                      f'出口检测服务返回 HTTP {status}，不一定是代理认证失败。')
        elif isinstance(cause, requests.ConnectionError):
            reason = '代理连接失败或被中断，请检查代理地址及认证。'
        else:
            reason = '未取得有效的出口 IP 和时区，请检查代理与检测服务。'
        raise ProxyCheckError(f'代理检查失败：{reason}此次启动已中止。') from error
    return {'ip': ip, 'timezone': timezone, 'latency_ms': round((time.monotonic() - started) * 1000)}


def _cookie_key(cookie: dict) -> tuple:
    if cookie.get('url'):
        url = urlsplit(cookie['url'])
        domain, path = url.hostname, url.path.rpartition('/')[0] + '/'
    else:
        domain, path = cookie['domain'], cookie['path']
    return cookie['name'], domain.lstrip('.').encode('idna').decode('ascii').lower(), path


def _new_identity(store, profile: dict, fingerprint: dict, executable: Path, version: str, kernel=None) -> dict:
    """Draw a recorded-device configuration not previously claimed in this workspace."""
    from ..addons import DefaultAddons, confirm_paths
    from ..fpgen_model import CUSTOM_MODEL_ENV, is_pinned

    addons = default_addon_paths()
    try:
        confirm_paths(addons)
    except Exception as error:
        raise StudioError('默认扩展尚未就绪，请先运行 python -m camoufox fetch。') from error
    if os.environ.get(CUSTOM_MODEL_ENV) or not is_pinned():
        raise StudioError('Studio 需要已校验的指纹模型，请移除 FPGEN_MODEL_URL 后运行 python -m camoufox fetch。')
    try:
        arguments = generation_arguments(fingerprint, profile['os'])
    except ValueError as error:
        raise StudioError(str(error)) from error
    for _ in range(8):
        config: dict[str, Any] = {}
        options = launch_options(
            config=config, os=profile['os'], locale=profile['locale'],
            executable_path=executable, headless=profile['headless'],
            block_webrtc=fingerprint['webrtc_mode'] == 'disabled', pin_cpu_cores=False,
            addons=addons, exclude_addons=list(DefaultAddons), **arguments,
        )
        identity = {
            'browser_version': version,
            'config': config,
            'options': {key: value for key, value in options.items()
                        if key not in ('env', 'executable_path', 'proxy', 'headless')},
        }
        if kernel:
            identity['kernel'] = kernel
        try:
            apply_identity_options(config, options['firefox_user_prefs'], fingerprint, profile['os'])
        except ValueError as error:
            raise StudioError(str(error)) from error
        validate_config(config, path=executable)
        # The claim and identity write are atomic; a duplicate never replaces a saved identity.
        if store.claim_identity(profile['id'], identity):
            return identity
    raise StudioError('这些限制下连续生成了已有指纹组合，请调整参数后重新生成。')


def prepare_launch(store, profile: dict, *, main_world_eval: bool | None = None) -> dict:
    """Freeze generated device data; rebuild only host environment and routing."""
    try:
        fingerprint = normalize_options(profile.get('fingerprint', {}))
        validate_environment(fingerprint, profile['os'], bool(profile['proxy_id']),
                             native_platform_accepted=store.native_platform_accepted(profile))
    except ValueError as error:
        raise StudioError(str(error)) from error
    configured = store.get_settings()['executable_path']
    kernel = None
    try:
        pinned = profile.get('kernel') or (profile.get('identity') or {}).get('kernel')
        if not store.uses_team_kernels() and (configured or not pinned):
            executable = browser_executable(configured)
        else:
            from .kernels import resolve_kernel
            executable, kernel = resolve_kernel(profile)
    except Exception as error:
        raise StudioError('环境的指定内核缺失或无法唯一确定。请由管理员安装并选择此环境的精确内核版本。') from error
    if not executable.is_file():
        raise StudioError('设置中的 Camoufox 可执行文件不存在。')
    version = resolve_verstr(executable)
    identity = profile['identity']
    if main_world_eval is True or (identity or {}).get('config', {}).get('allowMainWorld') is True:
        # validate_config tolerates unknown properties; an explicit capability must not.
        try:
            supported = _load_properties(path=executable).get('allowMainWorld') == 'bool'
        except (OSError, ValueError, KeyError, TypeError):
            raise MainWorldUnsupportedError() from None
        if not supported:
            raise MainWorldUnsupportedError()
    proxy = None
    exit_ip = None
    timezone = profile['timezone']
    if profile['proxy_id']:
        saved = store.get_proxy(profile['proxy_id'], reveal_password=True)
        proxy = {key: saved[key] for key in ('server', 'username', 'password') if saved.get(key)}
        result = check_proxy(proxy)
        exit_ip = result.get('ip')
        timezone = timezone or result['timezone']

    if identity is not None and identity['browser_version'] != version:
        raise StudioError('内核版本与此 Profile 的固定指纹不一致。请选择原内核，或确认重置指纹后再启动。')
    if identity is None:
        identity = _new_identity(store, profile, fingerprint, executable, version, kernel)

    config = copy.deepcopy(identity['config'])
    if main_world_eval is True:
        config['allowMainWorld'] = True
    # Legacy chrome.css turns tabs into window drag regions. Keep stock browser UI.
    config['disableTheming'] = True
    if config.get('addons') or bundled_path('addons'):
        from ..addons import confirm_paths
        config['addons'] = profile_addon_paths(store, identity)
        confirm_paths(config['addons'])
    options = copy.deepcopy(identity['options'])
    prefs = options['firefox_user_prefs']
    try:
        apply_screen_options(config, fingerprint)
        apply_identity_options(config, prefs, fingerprint, profile['os'])
    except ValueError as error:
        raise StudioError(str(error)) from error
    config['humanize'] = profile['humanize']
    if timezone:
        config['timezone'] = timezone
    else:
        config.pop('timezone', None)
    prefs['permissions.default.image'] = 2 if profile['block_images'] else 1
    try:
        apply_network_options(config, prefs, fingerprint, exit_ip)
    except ValueError as error:
        raise StudioError(str(error)) from error
    validate_config(config, path=executable)
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(('CAMOU_CONFIG', 'CAMOU_PREFS'))}
    environment.update(get_env_vars(config, get_target_os(config), path=executable))
    environment.update(get_pref_env_vars(prefs))
    directory = store.profile_dir(profile['id'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    options.update(
        executable_path=str(executable), user_data_dir=str(directory),
        env=environment, headless=profile['headless'], timeout=45000,
    )
    if proxy:
        options['proxy'] = proxy
    return options


@dataclass
class Session:
    headless: bool = False
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    context: Any = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    remote: Any = None
    error_code: str | None = None
    launch_timezone: str | None = None
    main_world_eval: bool | None = None
    automation_id: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    run_id: str | None = None


class _SessionResources(AsyncExitStack):
    def __init__(self, engine):
        super().__init__()
        self.engine = engine

    async def __aexit__(self, *exc):
        try:
            return await super().__aexit__(*exc)
        except BaseException:
            # Failed teardown cannot establish that the profile is safe to reopen.
            self.engine.recovery_required = True
            raise


class BrowserEngine:
    def __init__(self, store, state: Callable, message: Callable, failure: Callable,
                 *, prepare=prepare_launch, browser_factory=AsyncCamoufox,
                 authorize_start=None, open_start_pages=True):
        self.store = store
        if prepare is prepare_launch and store.list_members():
            from .kernels import initialize_default
            initialize_default(store)
        self.state = state
        self.message = message
        self.failure = failure
        self.prepare = prepare
        self.browser_factory = browser_factory
        self.authorize_start = authorize_start
        self.native_factory = None
        self.open_start_pages = open_start_pages
        self.sessions: dict[str, Session] = {}
        self.closing = False
        self.recovery_required = False
        self._preparing = asyncio.Lock()
        from .automation import AutomationBridge
        self.automation = AutomationBridge(self)
        self._automation_lock = asyncio.Lock()

    async def configure_automation(self, enabled: bool) -> None:
        async with self._automation_lock:
            if enabled and not self.closing:
                await self.automation.start()
            else:
                await self.automation.stop()

    async def start(self, profile_id: str, *, headless: bool | None = None,
                    run_id: str | None = None, owner: str | None = None,
                    native: bool | None = None, main_world_eval: bool | None = None) -> None:
        if self.closing:
            raise StudioError('管理器正在退出。')
        if self.recovery_required:
            raise StudioError('会话资源清理失败，请停止管理器并检查残留进程后恢复工作区。')
        if profile_id in self.sessions:
            raise StudioError('此 Profile 已在运行或启动中。')
        profile = self.store.get_profile(profile_id)
        if profile['archived']:
            raise StudioError('请先恢复已归档的 Profile。')
        if len(self.sessions) >= self.store.get_settings()['max_running']:
            raise StudioError('已达到并发上限，请停止一个 Profile 或调整设置。')
        if headless is not None:
            profile = {**profile, 'headless': headless}
        session = Session(headless=profile['headless'], run_id=run_id)
        self.sessions[profile_id] = session
        self.state(profile_id, {'state': 'starting', 'headless': session.headless})
        session.task = asyncio.create_task(self._run(
            profile, session, run_id=run_id, owner=owner,
            native=self.native_factory is not None if native is None else native,
            main_world_eval=main_world_eval,
        ))

    async def _run(self, profile: dict, session: Session, *, run_id=None, owner=None, native=False,
                   main_world_eval=None) -> None:
        profile_id = profile['id']
        error = None
        try:
            try:
                await asyncio.to_thread(self.store.check_execution_ownership, profile_id, run_id=run_id, owner=owner)
            except StoreError as exception:
                raise StudioError(str(exception)) from None
            if self.authorize_start is not None:
                await self.authorize_start(profile_id)
            if native and self.native_factory is None:
                raise StudioError('当前执行主机不支持原生远程模式。')
            async with _SessionResources(self) as resources:
                routing = {}
                if main_world_eval is True:
                    routing['main_world_eval'] = True
                # Generation in a worker must finish before another fpgen call can begin.
                async with self._preparing:
                    if session.stop.is_set():
                        return
                    options = await asyncio.to_thread(self.prepare, self.store, profile, **routing)
                if session.stop.is_set():
                    return
                if self.authorize_start is not None:
                    await self.authorize_start(profile_id)
                if session.stop.is_set():
                    return
                proxy = await resources.enter_async_context(browser_proxy(options.get('proxy')))
                if native:
                    session.launch_timezone = json.loads(_camou_config_blob(options) or '{}').get('timezone')
                    session.remote = await resources.enter_async_context(self.native_factory(
                        options, self.store.get_profile(profile_id)))
                    options = session.remote.options
                effective_main_world = json.loads(_camou_config_blob(options) or '{}').get('allowMainWorld') is True
                context = await resources.enter_async_context(self.browser_factory(
                    from_options={**options, **({'proxy': proxy} if proxy else {})},
                    persistent_context=True,
                ))
                if session.remote:
                    await session.remote.attach()
                if session.stop.is_set():
                    return
                session.context = context
                session.main_world_eval = effective_main_world
                context.on('close', lambda *_: session.stop.set())
                if not session.stop.is_set():
                    self.store.mark_launched(profile_id)
                    self.store.record_event('browser.start', profile_id)
                    self.state(profile_id, {'state': 'running', 'headless': session.headless})
                    session.ready.set()
                navigation = asyncio.create_task(self._open_pages(
                    context, profile['start_urls'] if self.open_start_pages else []))
                display_watch = asyncio.create_task(session.remote.wait()) if session.remote else None
                stopped = asyncio.create_task(session.stop.wait())
                try:
                    async def wait():
                        if display_watch:
                            done, _ = await asyncio.wait((stopped, display_watch), return_when=asyncio.FIRST_COMPLETED)
                            if display_watch in done:
                                await display_watch
                        else:
                            await stopped
                    await wait()
                finally:
                    navigation.cancel()
                    stopped.cancel()
                    if display_watch:
                        display_watch.cancel()
                    await asyncio.gather(navigation, stopped, *([display_watch] if display_watch else []),
                                         return_exceptions=True)
                    self.state(profile_id, {'state': 'stopping', 'headless': session.headless})
        except Exception as exception:
            if isinstance(exception, (ProxyCheckError, ProxyBridgeError, ValidationError)):
                session.error_code = 'proxy_failed'
            elif isinstance(exception, MainWorldUnsupportedError):
                session.error_code = 'main_world_unsupported'
            error = public_error(exception)
            self.failure(error)
        finally:
            session.error = error
            session.ready.set()
            self.sessions.pop(profile_id, None)
            self.store.record_event('browser.error' if error else 'browser.stop', profile_id)
            self.state(profile_id, {'state': 'error', 'error': error} if error else {'state': 'stopped'})

    async def _open_pages(self, context, urls: list[str]) -> None:
        for index, url in enumerate(urls):
            try:
                page = context.pages[0] if index == 0 and context.pages else await context.new_page()
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception:
                self.message('某个启动页未能加载，会话仍在运行。请检查网络；无头会话可停止后切换为有窗口模式检查。')

    async def stop(self, profile_id: str) -> None:
        session = self.sessions.get(profile_id)
        if session:
            self.state(profile_id, {'state': 'stopping', 'headless': session.headless})
            session.stop.set()

    async def stop_all(self) -> None:
        for profile_id in list(self.sessions):
            await self.stop(profile_id)

    async def shutdown(self) -> None:
        self.closing = True
        try:
            await self.configure_automation(False)
        finally:
            tasks = [session.task for session in self.sessions.values()]
            await self.stop_all()
            if tasks:
                await asyncio.gather(*tasks)
        if self.recovery_required:
            raise StudioError('会话资源清理失败，工作区仍被锁定。请检查残留浏览器、显示和代理进程。')

    async def probe(self, proxy_id: str) -> dict:
        proxy = self.store.get_proxy(proxy_id, reveal_password=True)
        result = await asyncio.to_thread(check_proxy, proxy)
        self.store.record_proxy_timezone(proxy_id, proxy['updated_at'], result['timezone'])
        self.store.record_event('proxy.check', detail='success')
        self.message(f"代理可用 · {result['ip']} · {result['timezone']} · {result['latency_ms']} ms")
        return result

    def _context(self, profile_id: str):
        session = self.sessions.get(profile_id)
        if not session or session.context is None or session.stop.is_set():
            raise StudioError('请先启动此 Profile。')
        return session.context

    @staticmethod
    def validate_cookies(cookies) -> list[dict]:
        if isinstance(cookies, dict):
            cookies = cookies.get('cookies')
        if not isinstance(cookies, list) or any(
            not isinstance(cookie, dict) or not {'name', 'value'} <= cookie.keys()
            or not all(isinstance(cookie[key], str) for key in ('name', 'value'))
            or not ('url' in cookie or {'domain', 'path'} <= cookie.keys())
            for cookie in cookies
        ):
            raise StudioError('请选择 Playwright Cookie JSON 数组，或含 cookies 的 storage_state 文件。')
        return cookies

    async def import_cookie_data(self, profile_id: str, cookies) -> int:
        context = self._context(profile_id)
        cookies = self.validate_cookies(cookies)
        await context.add_cookies(cookies)
        accepted = {_cookie_key(cookie): cookie['value'] for cookie in await context.cookies()}
        for key, cookie in {_cookie_key(cookie): cookie for cookie in cookies}.items():
            expires = cookie.get('expires', -1)
            expired = expires != -1 and expires <= time.time()
            if (expired and key in accepted) or (not expired and accepted.get(key) != cookie['value']):
                self.store.record_event('cookies.reject', profile_id)
                raise StudioError('浏览器未接受部分 Cookie。请检查域名、过期时间及 SameSite/Secure 设置；已接受的 Cookie 已保留。')
        self.store.record_event('cookies.import', profile_id)
        self.message(f'已导入 {len(cookies)} 条 Cookie。')
        return len(cookies)

    async def export_cookie_data(self, profile_id: str) -> list[dict]:
        cookies = await self._context(profile_id).cookies()
        self.store.record_event('cookies.export', profile_id)
        self.message(f'已导出 {len(cookies)} 条 Cookie。')
        return cookies

    async def import_cookies(self, profile_id: str, path: str) -> None:
        file = Path(path)
        if file.stat().st_size > 10 * 1024 * 1024:
            raise StudioError('Cookie 文件不能超过 10 MB。')
        try:
            cookies = json.loads(file.read_text(encoding='utf-8-sig'))
        except (ValueError, UnicodeError) as error:
            raise StudioError('Cookie 文件必须为 UTF-8 JSON。') from error
        await self.import_cookie_data(profile_id, cookies)

    async def export_cookies(self, profile_id: str, path: str) -> None:
        cookies = await self.export_cookie_data(profile_id)
        # Explicit user export. Restrict new files on POSIX; never audit contents.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(cookies, stream, ensure_ascii=False, indent=2)
