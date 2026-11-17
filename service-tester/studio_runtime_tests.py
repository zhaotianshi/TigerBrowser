"""Opt-in real-browser Studio checks; missing platform prerequisites fail, never skip."""

import asyncio
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest
import uvicorn
from cryptography.cobblestone import Cobblestone128Encryptor
from playwright.async_api import expect
from starlette.responses import HTMLResponse, Response

from camoufox.studio import backup, manifest, migration
from camoufox.studio.engine import BrowserEngine
from camoufox.studio.store import ProfileStore, StoreError
from camoufox.studio.team.host import configuration


@pytest.fixture
def workspace(tmp_path, monkeypatch, request):
    selected = os.environ.get('CAMOUFOX_STUDIO_BINARY')
    if not selected or not Path(selected).is_file():
        pytest.fail('Set CAMOUFOX_STUDIO_BINARY to an installed Camoufox executable.')
    root = tmp_path / 'source'
    root.mkdir(mode=0o700)
    store = ProfileStore(root)
    store.save_settings({'executable_path': str(Path(selected).resolve())})
    admin = store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'synthetic-admin-password')
    member = store.create_member(admin['id'], 'operator', 'Operator', 'operator', 'synthetic-member-password')
    profile = store.save_profile({'name': 'Runtime validation',
                                  'os': 'windows' if os.name == 'nt' else 'linux',
                                  'fingerprint': getattr(request, 'param', {})})
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'operate'])
    monkeypatch.setattr(manifest, 'INSTALL_DIR', tmp_path / 'host-registration')
    return store, admin, member, profile


@asynccontextmanager
async def running_team(store, port=0, browser_mode='headless', **options):
    listener = socket.socket()
    # Match asyncio.create_server's Unix TIME_WAIT handling; Windows does not use this flag.
    if os.name == 'posix':
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', port))
    listener.listen(16)
    listener.setblocking(False)
    origin = f'http://127.0.0.1:{listener.getsockname()[1]}'
    config = configuration(store.root, origin=origin, browser_mode=browser_mode, **options)
    app = config.app

    @app.get('/synthetic-site')
    async def site():
        return HTMLResponse('<title>Studio persistence validation</title><p>Synthetic local site</p>')

    @app.get('/synthetic-native')
    async def native_site():
        return HTMLResponse('<meta charset="utf-8"><link rel="stylesheet" href="/synthetic-native.css">'
                            '<title>中文远程浏览器</title><h1>中文环境控制台</h1>'
                            '<label for="native">输入验证</label><input id="native">')

    @app.get('/synthetic-native.css')
    async def native_styles():
        return Response('@font-face {font-family: "Noto Sans SC"; src: url("/assets/noto-sans-sc.woff2")} '
                        'body {font-family: "Noto Sans SC", sans-serif} '
                        '#native {position:fixed;left:20px;bottom:20px;width:300px;height:50px}',
                        media_type='text/css')

    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            if task.done():
                await task
                pytest.fail('The local team server did not start.')
            if time.monotonic() >= deadline:
                pytest.fail('The local team server startup timed out.')
            await asyncio.sleep(0.01)
        app.state.team.validation_errors = []
        app.state.team.engine.failure = app.state.team.validation_errors.append
        yield app.state.team, origin
    finally:
        server.should_exit = True
        await task
        listener.close()


async def login(client, name, password):
    response = await client.post('/team/v1/login', headers={'Origin': str(client.base_url).rstrip('/')},
                                 json={'name': name, 'password': password})
    assert response.status_code == 200
    return response.json()['csrf']


async def command(service, client, csrf, profile_id, operation, run_id=None):
    data = {'request_id': str(uuid4())}
    if run_id is not None:
        data['run_id'] = run_id
    headers = {'Origin': str(client.base_url).rstrip('/'), 'X-CSRF-Token': csrf}
    result = await client.post(f'/team/v1/profiles/{profile_id}/{operation}', headers=headers, json=data)
    assert result.status_code == 202, result.text
    accepted = result.json()
    # Wait on the service-owned task instead of flooding its deliberately rate-limited HTTP API.
    await asyncio.wait_for(asyncio.shield(service.tasks[accepted['id']]), timeout=60)
    result = await client.get('/team/v1/commands/' + accepted['id'])
    assert result.status_code == 200
    accepted = result.json()
    assert accepted['state'] != 'accepted'
    assert accepted['error'] is None, (accepted['error'], service.validation_errors)
    return accepted


async def site_state(context, url, *, create=False):
    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto(url, wait_until='domcontentloaded')
    if create:
        await context.add_cookies([{'name': 'studio-persistent', 'value': 'synthetic-cookie',
                                   'url': url, 'expires': time.time() + 3600}])
    return await page.evaluate('''async (create) => {
        if (create) localStorage.setItem('studio-state', 'synthetic-storage');
        const database = await new Promise((resolve, reject) => {
            const request = indexedDB.open('studio-runtime', 1);
            request.onupgradeneeded = () => request.result.createObjectStore('values');
            request.onsuccess = () => resolve(request.result);
            request.onerror = () => reject(request.error);
        });
        const transaction = database.transaction('values', create ? 'readwrite' : 'readonly');
        const values = transaction.objectStore('values');
        if (create) values.put('synthetic-indexeddb', 'key');
        const request = values.get('key');
        let value;
        request.onsuccess = () => { value = request.result; };
        await new Promise((resolve, reject) => {
            transaction.oncomplete = resolve;
            transaction.onabort = () => reject(transaction.error);
        });
        database.close();
        return [document.cookie, localStorage.getItem('studio-state'), value];
    }''', create)


@pytest.mark.asyncio
async def test_stopped_server_handover_preserves_site_data_and_member_access(workspace, tmp_path, monkeypatch):
    store, _, _, profile = workspace
    async with running_team(store) as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            csrf = await login(client, 'operator', 'synthetic-member-password')
            started = await command(service, client, csrf, profile['id'], 'start')
            context = service.engine.sessions[profile['id']].context
            initial = await site_state(context, origin + '/synthetic-site', create=True)
            identity = store.get_profile(profile['id'])['identity']
            await command(service, client, csrf, profile['id'], 'stop', started['run_id'])
    key = Cobblestone128Encryptor.generate_key()
    archive, request, receipt = (tmp_path / name for name in ('archive', 'request', 'receipt'))
    backup.create_snapshot(store.root, archive, key, for_migration=True)
    old_host = manifest.INSTALL_DIR
    new_host = tmp_path / 'new-host-registration'
    monkeypatch.setattr(manifest, 'INSTALL_DIR', new_host)
    restored = backup.restore_snapshot(archive, tmp_path / 'restored', key)
    migration.prepare(restored, request, key, executable_path=store.get_settings()['executable_path'])
    monkeypatch.setattr(manifest, 'INSTALL_DIR', old_host)
    migration.retire(store.root, request, receipt, key)
    with pytest.raises(StoreError, match='retired'):
        store.check_execution_ownership(profile['id'])
    monkeypatch.setattr(manifest, 'INSTALL_DIR', new_host)
    migration.activate(restored, receipt, key)
    target = ProfileStore(restored)
    async with running_team(target, port=urlsplit(origin).port) as (service, target_origin):
        assert target_origin == origin
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            csrf = await login(client, 'operator', 'synthetic-member-password')
            started = await command(service, client, csrf, profile['id'], 'start')
            context = service.engine.sessions[profile['id']].context
            assert await site_state(context, origin + '/synthetic-site') == initial
            assert target.get_profile(profile['id'])['identity'] == identity
            await command(service, client, csrf, profile['id'], 'stop', started['run_id'])
    assert not (store.root / 'workspace.lock').exists()
    assert not (restored / 'workspace.lock').exists()


@pytest.mark.asyncio
async def test_shared_execution_revocation_and_encrypted_recovery(workspace, tmp_path, monkeypatch):
    # Loopback acceptance must not inherit a developer's outbound proxy.
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    store, admin, member, profile = workspace
    async with running_team(store) as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as first, \
                httpx.AsyncClient(base_url=origin, trust_env=False) as second:
            admin_csrf = await login(first, 'root', 'synthetic-admin-password')
            member_csrf = await login(second, 'operator', 'synthetic-member-password')
            started = await command(service, second, member_csrf, profile['id'], 'start')
            context = service.engine.sessions[profile['id']].context
            initial = await site_state(context, origin + '/synthetic-site', create=True)
            identity = store.get_profile(profile['id'])['identity']
            for client in (first, second):
                response = await client.get('/team/v1/profiles')
                assert response.json()['profiles'][0]['run']['id'] == started['run_id']
            response = await first.post('/team/v1/members',
                headers={'Origin': origin, 'X-CSRF-Token': admin_csrf},
                json={'action': 'revoke', 'id': member['id'], 'profile_id': profile['id']})
            assert response.status_code == 200
            assert (await second.get('/team/v1/profiles')).status_code == 401
            assert service.engine.sessions[profile['id']].context is context
            assert await site_state(context, origin + '/synthetic-site') == initial
            await command(service, first, admin_csrf, profile['id'], 'stop', started['run_id'])
            restarted = await command(service, first, admin_csrf, profile['id'], 'start')
            context = service.engine.sessions[profile['id']].context
            assert await site_state(context, origin + '/synthetic-site') == initial
            assert store.get_profile(profile['id'])['identity'] == identity
            await command(service, first, admin_csrf, profile['id'], 'stop', restarted['run_id'])
    assert not (store.root / 'workspace.lock').exists()
    manifest.register(store.root)
    key = Cobblestone128Encryptor.generate_key()
    archive = tmp_path / 'stopped.cfxbackup'
    backup.create_snapshot(store.root, archive, key)
    restored = backup.restore_snapshot(archive, tmp_path / 'restore', key)
    copy = ProfileStore(restored)
    with pytest.raises(StoreError, match='quarantined'):
        copy.check_execution_ownership(profile['id'])
    backup.activate_transfer(store.root, restored)
    with pytest.raises(StoreError, match='retired'):
        store.check_execution_ownership(profile['id'])
    assert copy.get_profile(profile['id'])['identity'] == identity
    engine = BrowserEngine(copy, lambda *_: None, lambda *_: None, lambda *_: None)
    try:
        await engine.start(profile['id'], headless=True)
        session = engine.sessions[profile['id']]
        await session.ready.wait()
        assert session.error is None, session.error
        # The synthetic site's server has stopped; preserve its origin without a network request.
        page = session.context.pages[0] if session.context.pages else await session.context.new_page()
        await page.route(origin + '/synthetic-site', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<title>Restored synthetic site</title>'))
        assert await site_state(session.context, origin + '/synthetic-site') == initial
    finally:
        await engine.shutdown()
    assert engine.recovery_required is False


@asynccontextmanager
async def console_client(store, root):
    client = ProfileStore(root)
    client.save_settings({'executable_path': store.get_settings()['executable_path']})
    profile = client.save_profile({'name': 'Console client',
                                   'os': 'windows' if os.name == 'nt' else 'linux'})
    engine = BrowserEngine(client, lambda *_: None, lambda *_: None, lambda *_: None)
    errors = []
    try:
        await engine.start(profile['id'], headless=True)
        session = engine.sessions[profile['id']]
        await session.ready.wait()
        assert session.error is None, session.error
        page = session.context.pages[0] if session.context.pages else await session.context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        yield page
        assert errors == []
    finally:
        await engine.shutdown()
    assert engine.recovery_required is False


async def console_login(page, origin):
    await page.goto(origin)
    await page.locator('.login-form').get_by_label('用户名', exact=True).fill('root')
    await page.locator('.login-form').get_by_label('密码', exact=True).fill('synthetic-admin-password')
    async with page.expect_response(lambda response: response.url == origin + '/team/v1/login'
                                    and response.request.method == 'POST') as login_response:
        await page.get_by_role('button', name='登录控制台', exact=True).click()
    assert (await login_response.value).ok
    await expect(page.locator('.connection-status')).to_have_text('实时同步 · 正常')


@pytest.mark.asyncio
async def test_web_console_recovers_after_service_restart(workspace, tmp_path):
    store, _, _, _ = workspace
    async with console_client(store, tmp_path / 'client') as page:
        async with running_team(store) as (_, origin):
            await console_login(page, origin)
        await expect(page.locator('.connection-status')).not_to_have_text('实时同步 · 正常')
        # Rebind after shutdown, as the real single-worker entry point does; never duplicate IOCP sockets.
        async with running_team(store, port=urlsplit(origin).port):
            await expect(page.locator('.connection-status')).to_have_text('实时同步 · 正常', timeout=15000)
            await page.get_by_role('link', name='团队管理', exact=True).click()
            await page.get_by_role('button', name='成员组 (0)', exact=True).click()
            await page.get_by_role('button', name='创建成员组', exact=True).click()
            await page.locator('.group-form input').fill('After service restart')
            async with page.expect_response(lambda response: response.url == origin + '/team/v1/groups') as response:
                await page.get_by_role('button', name='确认创建', exact=True).click()
            assert (await response.value).status == 200
            await expect(page.locator('.tab-panel').filter(has_text='成员组与组授权')).to_contain_text('After service restart')
            await page.set_viewport_size({'width': 375, 'height': 812})
            assert await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            await page.screenshot(path=str(tmp_path / 'console-mobile.png'), full_page=True)
            await page.get_by_role('button', name='退出', exact=True).click()
            await expect(page.locator('.login-page')).to_be_visible()
            await expect(page.locator('.workspace-main')).to_be_hidden()


@pytest.mark.asyncio
async def test_console_opens_selected_profile_details(workspace, tmp_path):
    store, _, _, profile = workspace
    async with running_team(store) as (_, origin), console_client(store, tmp_path / 'client') as page:
        await console_login(page, origin)
        row = page.locator('.profile-table tbody tr').filter(has_text=profile['name'])
        await row.get_by_role('checkbox').check()
        await page.get_by_role('button', name='详情', exact=True).click()
        modal = page.locator('.modal-container').filter(has_text='环境详情与指纹检查')
        await expect(modal.get_by_text(profile['id'], exact=True)).to_be_visible()
        await expect(modal.get_by_text(profile['name'], exact=True)).to_be_visible()
        await modal.get_by_role('button', name='确定', exact=True).click()
        await row.get_by_role('button', name=profile['name'], exact=True).click()
        await expect(modal.get_by_text(profile['id'], exact=True)).to_be_visible()


@pytest.mark.asyncio
async def test_console_settings_and_timezone_choices_preserve_saved_values(workspace, tmp_path):
    store, _, _, profile = workspace
    proxy = store.save_proxy({'name': 'Synthetic timezone route', 'server': 'socks5://127.0.0.1:1080'})
    store.record_proxy_timezone(proxy['id'], proxy['updated_at'], 'Etc/UTC')
    store.update_profile_checked({'timezone': 'US/Pacific', 'proxy_id': proxy['id']}, profile['id'], profile['revision'])
    async with running_team(store) as (_, origin), console_client(store, tmp_path / 'client') as page:
        await console_login(page, origin)
        await page.get_by_role('link', name='工作区设置', exact=True).click()
        async with page.expect_response(lambda response: response.request.method == 'POST'
                and response.url == origin + '/team/v1/settings') as saved:
            await page.get_by_role('button', name='保存引擎设置', exact=True).click()
        assert (await saved.value).ok
        await page.get_by_role('link', name='浏览器环境', exact=True).click()
        await page.locator('.profile-table tbody tr').filter(has_text=profile['name']).get_by_role('checkbox').check()
        await page.get_by_role('button', name='编辑', exact=True).click()
        await page.get_by_role('button', name='网络与系统', exact=True).click()
        timezone = page.get_by_label('时区 (Timezone)', exact=True)
        await expect(timezone).to_be_enabled()
        await expect(timezone).to_have_value('US/Pacific')
        await expect(timezone.locator('option:checked')).to_contain_text('已保存')
        await expect(page.locator('.proxy-timezone-hint')).to_contain_text('Etc/UTC')
        await page.get_by_role('searchbox', name='搜索时区', exact=True).fill('Shanghai')
        await timezone.select_option('Asia/Shanghai')
        await expect(timezone).to_have_value('Asia/Shanghai')
        await page.screenshot(path=str(tmp_path / 'timezone-choices.png'))
        await page.set_viewport_size({'width': 375, 'height': 812})
        assert await page.locator('.modal-body').evaluate('element => element.scrollWidth <= element.clientWidth')
        await page.screenshot(path=str(tmp_path / 'timezone-choices-mobile.png'))
        await timezone.select_option('')
        async with page.expect_response(lambda response: response.request.method == 'POST'
                and response.url == origin + '/team/v1/profiles/' + profile['id']) as saved:
            await page.get_by_role('button', name='保存修改', exact=True).click()
        assert (await saved.value).ok
        assert store.get_profile(profile['id'])['timezone'] == ''
