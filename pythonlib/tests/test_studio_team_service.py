"""Network member boundaries and persistent execution reservations."""

import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
from contextlib import asynccontextmanager, nullcontext
from uuid import uuid4

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from camoufox.studio.engine import BrowserEngine, StudioError
from camoufox.studio.store import PROFILE_FIELDS, ProfileStore, ValidationError
from camoufox.studio.team.control import RemoteRun
from camoufox.studio.team.service import TeamError, create_app
from camoufox.studio.workspace import WorkspaceLock, WorkspaceLockError

ORIGIN = 'https://team.test'
PREFIX = '/team/v1'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    from camoufox import multiversion
    monkeypatch.setattr(multiversion, 'BROWSERS_DIR', tmp_path / 'browser-cache')
    store = ProfileStore(tmp_path)
    code = store.begin_team_setup()
    admin = store.initialize_team(code, 'root', 'Admin', 'admin-password-1')
    operator = store.create_member(admin['id'], 'op', 'Operator', 'operator', 'member-password-1')
    viewer = store.create_member(admin['id'], 'reader', 'Reader', 'viewer', 'member-password-2')
    first = store.save_profile({'name': '<img src=x onerror=alert(1)>', 'notes': 'private note'})
    second = store.save_profile({'name': 'Other environment'})
    store.grant_profile(admin['id'], operator['id'], first['id'], ['view', 'operate'])
    store.grant_profile(admin['id'], viewer['id'], first['id'], ['view', 'watch'])
    return store, admin, operator, viewer, first, second


def engine_factory(store, state, message, failure):
    class Context:
        pages = []

        def on(self, event, callback):
            self.callback = callback

    @asynccontextmanager
    async def browser(**kwargs):
        assert kwargs['persistent_context']
        yield Context()

    return BrowserEngine(store, state, message, failure,
                         prepare=lambda store, profile: {'headless': profile['headless']},
                         browser_factory=browser)


def app(workspace, **kwargs):
    return create_app(workspace[0].root, origin=ORIGIN, engine_factory=engine_factory, **kwargs)


def login(client, name='root', password='admin-password-1'):
    response = client.post(PREFIX + '/login', headers={'Origin': ORIGIN},
                           json={'name': name, 'password': password})
    assert response.status_code == 200, response.text
    return response.json()['csrf']


def mutation(client, csrf, path, body):
    return client.post(PREFIX + path, headers={'Origin': ORIGIN, 'X-CSRF-Token': csrf}, json=body)


def settle(client, command):
    client.portal.call(lambda: client.app.state.team.tasks[command['id']])
    return client.get(PREFIX + '/commands/' + command['id']).json()


def test_proxy_parser_is_admin_only_and_has_no_storage_side_effect(workspace):
    link = 'socks5://demo:example%40password@proxy.example:1080'
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        result = mutation(client, csrf, '/proxies/parse', {'link': link})
        assert result.status_code == 200
        assert result.json()['proxy'] == dict(name='socks5://proxy.example:1080',
            server='socks5://proxy.example:1080', username='demo', password='example@password')
        assert not client.get(PREFIX + '/proxies').json()['proxies']
        assert mutation(client, csrf, '/proxies/parse', {'link': 'unsupported://proxy.example:1080'}).status_code == 400
        csrf = login(client, 'reader', 'member-password-2')
        assert mutation(client, csrf, '/proxies/parse', {'link': link}).status_code == 403


def test_timezone_choices_require_login_and_exclude_legacy_aliases(workspace):
    from zoneinfo import ZoneInfo

    with TestClient(app(workspace), base_url=ORIGIN) as client:
        assert client.get(PREFIX + '/timezones').status_code == 401
        login(client, 'reader', 'member-password-2')
        response = client.get(PREFIX + '/timezones')
        assert response.status_code == 200
        catalog = response.json()
        assert catalog['version']
        names = [item['name'] for item in catalog['timezones']]
        assert names == sorted(set(names))
        assert 'Asia/Shanghai' in names and 'Etc/UTC' in names and 'US/Pacific' not in names
        for item in catalog['timezones']:
            assert ZoneInfo(item['name']).key == item['name']
            assert item['region'] == item['name'].partition('/')[0]


@pytest.mark.parametrize('path, target, code', [
    ('/timezones', 'camoufox.studio.timezones.timezone_catalog', 'timezone_catalog_unavailable'),
])
def test_unavailable_host_choices_fail_without_disclosing_host_paths(workspace, monkeypatch, path, target, code):
    def unavailable():
        raise OSError('/private/host/path')

    monkeypatch.setattr(target, unavailable)
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        response = client.get(PREFIX + path)
        assert response.status_code == 503 and response.json()['error'] == code
        assert 'private' not in response.text


def test_remote_login_cookie_is_unpredictable_hashed_and_not_a_local_session(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        cookie = client.cookies['__Host-camoufox-team']
        assert len(cookie) == 64 and len(csrf) == 64
        with sqlite3.connect(workspace[0].database_path) as connection:
            token_hash, session_id = connection.execute(
                'SELECT token_hash, session_id FROM team_http_sessions').fetchone()
        assert token_hash == hashlib.sha256(cookie.encode()).hexdigest()
        assert cookie != session_id
        with pytest.raises(ValidationError):
            workspace[0].resolve_session(cookie)
        assert cookie not in client.get(PREFIX + '/me').text


def test_secure_cookie_flags_and_security_headers(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        response = client.post(PREFIX + '/login', headers={'Origin': ORIGIN},
                               json={'name': 'root', 'password': 'admin-password-1'})
        cookie = response.headers['set-cookie'].lower()
        assert 'secure' in cookie and 'httponly' in cookie and 'samesite=strict' in cookie
        assert 'domain=' not in cookie and 'path=/' in cookie
        page = client.get('/')
        assert page.headers['cache-control'] == 'no-store'
        assert "frame-ancestors 'none'" in page.headers['content-security-policy']
        assert page.headers['x-content-type-options'] == 'nosniff'


def test_console_chinese_font_is_local_and_allowed_by_csp(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        font = client.get('/assets/noto-sans-sc.woff2')
        assert font.status_code == 200
        assert font.content[:4] == b'wOF2'
        assert 'font/woff2' in font.headers['content-type']
        assert "font-src 'self'" in client.get('/').headers['content-security-policy']
        css = re.search(r'href="\./(assets/[^\"]+\.css)"', client.get('/').text).group(1)
        assert 'Noto Sans SC' in client.get('/' + css).text


def test_console_serves_vite_modules_and_rejects_legacy_sources(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        assert 'type="module"' in client.get('/').text
        module = re.search(r'src="\./(assets/[^\"]+\.js)"', client.get('/').text).group(1)
        response = client.get('/' + module)
        assert response.status_code == 200
        assert 'text/javascript' in response.headers['content-type']
        assert response.headers['x-content-type-options'] == 'nosniff'
        for name in ('team.js', 'team.css', 'remote.js', 'remote-input.js', 'remote-input.d.ts'):
            response = client.get('/assets/' + name)
            assert response.status_code == 404


@pytest.fixture
def frontend(tmp_path):
    web = tmp_path / 'frontend'
    public = Path(__file__).resolve().parents[2] / 'frontend/studio/public'
    shutil.copytree(public, web)
    (web / '.vite').mkdir()
    (web / 'index.html').write_text('<html><script src="/assets/index-abc.js"></script></html>')
    (web / 'assets/index-abc.js').write_text('export const loaded = true')
    (web / 'assets/index-abc.css').write_text('body { color: white }')
    (web / '.vite/manifest.json').write_text(json.dumps({
        'index.html': {'file': 'assets/index-abc.js', 'css': ['assets/index-abc.css'], 'isEntry': True}}))
    return web


def test_team_service_requires_packaged_frontend(workspace, tmp_path, monkeypatch):
    monkeypatch.setattr('camoufox.studio.team.service.ASSETS', tmp_path / 'missing-frontend')
    with pytest.raises(ValueError, match='frontend'):
        app(workspace)


def test_built_frontend_serves_only_manifest_assets(workspace, frontend, monkeypatch):
    web = frontend
    (web / 'assets/private.json').write_text('{"not_a_public_asset":true}')
    monkeypatch.setattr('camoufox.studio.team.service.ASSETS', web)
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        assert '/assets/index-abc.js' in client.get('/').text
        assert client.get('/assets/index-abc.js').status_code == 200
        assert 'text/javascript' in client.get('/assets/index-abc.js').headers['content-type']
        assert client.get('/assets/index-abc.css').status_code == 200
        assert client.get('/assets/private.json').status_code == 404
        assert client.get('/assets/team.js').status_code == 404
        assert client.get('/.vite/manifest.json').status_code == 404
        assert client.get('/kasm-client/rfb.js.map').status_code == 200
        assert client.get('/kasm-client/LICENSE.txt').status_code == 200
        assert client.get('/kasm-client/unlisted.js').status_code == 404


@pytest.mark.parametrize('asset', ['../private.js', 'assets/missing.js', 'assets/private.json'])
def test_built_frontend_rejects_invalid_or_missing_manifest_assets(workspace, frontend, monkeypatch, asset):
    web = frontend
    (web / 'assets/private.json').write_text('{}')
    (web / '.vite/manifest.json').write_text(json.dumps({'index.html': {'file': asset}}))
    with pytest.raises(ValueError, match='frontend'):
        monkeypatch.setattr('camoufox.studio.team.service.ASSETS', web)
        app(workspace)


@pytest.mark.parametrize('missing', ['kasm-client/rfb.js', 'kasm-client/LICENSE.txt', 'assets/noto-sans-sc.woff2'])
def test_frontend_requires_its_decoder_license_and_font(workspace, frontend, monkeypatch, missing):
    (frontend / missing).unlink()
    monkeypatch.setattr('camoufox.studio.team.service.ASSETS', frontend)
    with pytest.raises(ValueError, match='frontend'):
        app(workspace)


@pytest.mark.parametrize('headers', [{}, {'Origin': 'https://evil.test'},
                                     {'Origin': 'https://team.test.evil.test'}])
def test_login_requires_exact_origin(workspace, headers):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        assert client.post(PREFIX + '/login', headers=headers,
                           json={'name': 'root', 'password': 'admin-password-1'}).status_code == 403


def test_csrf_and_hostname_cannot_be_bypassed(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        assert client.post(PREFIX + '/logout', headers={'Origin': ORIGIN}, json={}).status_code == 403
        assert mutation(client, 'wrong', '/logout', {}).status_code == 403
        assert client.get(PREFIX + '/me', headers={'Host': 'evil.test'}).status_code == 400
        assert mutation(client, csrf, '/logout', {}).status_code == 200
        assert client.get(PREFIX + '/me').status_code == 401


def test_viewer_sees_only_redacted_granted_metadata(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client, 'reader', 'member-password-2')
        response = client.get(PREFIX + '/profiles')
        assert [p['id'] for p in response.json()['profiles']] == [workspace[4]['id']]
        for forbidden in ('private note', 'identity', 'proxy_id', 'password_hash', str(workspace[0].root)):
            assert forbidden not in response.text
        assert client.get(PREFIX + '/admin').status_code == 403
        assert mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start',
                        {'request_id': str(uuid4())}).status_code == 403


def test_profile_details_round_trip_editable_fields_without_internal_identity(workspace):
    store, _, _, _, profile, _ = workspace
    store.update_profile_checked({
        'locale': 'zh-CN', 'timezone': 'Asia/Shanghai', 'tags': ['测试'],
        'start_urls': ['https://example.com/'], 'humanize': False,
    }, profile['id'], profile['revision'])
    saved = store.set_identity(profile['id'], {'config': {'navigator.userAgent': 'saved-identity'},
                                              'env': {'PRIVATE_PATH': str(store.root)}})
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        response = client.get(PREFIX + '/profiles/' + profile['id'])
        assert response.status_code == 200, response.text
        detail = response.json()
        assert {key: detail[key] for key in PROFILE_FIELDS} == {key: saved[key] for key in PROFILE_FIELDS}
        assert detail['revision'] == saved['revision'] and detail['identity_present'] is True
        assert detail['last_launch_at'] is None and detail['run'] is None
        assert set(detail['permissions']) == {'view', 'watch', 'operate', 'manage', 'checkout'}
        for forbidden in ('saved-identity', 'PRIVATE_PATH', str(store.root), '"identity":'):
            assert forbidden not in response.text
        assert store.get_profile(profile['id']) == saved
        assert client.get(PREFIX + '/profiles/' + str(uuid4())).status_code == 404
        assert client.get(PREFIX + '/profiles/not-a-uuid').status_code == 400


@pytest.mark.parametrize('name,password', [('op', 'member-password-1'), ('reader', 'member-password-2')])
def test_profile_details_are_admin_only(workspace, name, password):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        path = PREFIX + '/profiles/' + workspace[4]['id']
        assert client.get(path).status_code == 401
        login(client, name, password)
        response = client.get(path)
        assert response.status_code == 403 and response.json() == {'error': 'admin_required'}


def test_profile_list_includes_role_redacted_desktop_summaries(workspace):
    store, _, _, _, profile, _ = workspace
    proxy = store.save_proxy({'name': 'Singapore exit', 'server': 'http://proxy.test:8080'})
    profile = store.update_profile_checked({
        'locale': 'zh-CN', 'proxy_id': proxy['id'], 'notes': 'admin search only',
    }, profile['id'], profile['revision'])
    store.mark_launched(profile['id'])
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client, 'reader', 'member-password-2')
        item = client.get(PREFIX + '/profiles').json()['profiles'][0]
        assert item['locale'] == 'zh-CN' and item['last_launch_at'] is not None
        assert item['proxy'] == {'name': 'Singapore exit'}
        assert 'notes' not in item and 'proxy.test' not in json.dumps(item)
        login(client)
        item = next(p for p in client.get(PREFIX + '/profiles').json()['profiles']
                    if p['id'] == profile['id'])
        assert item['proxy'] == {
            'id': proxy['id'], 'name': 'Singapore exit', 'server': 'http://proxy.test:8080',
        }


def test_identity_clone_and_delete_contracts_enforce_revision_and_stopped_state(workspace):
    store, _, _, _, profile, other = workspace
    profile = store.set_identity(profile['id'], {
        'browser_version': '152.0.4-beta.31',
        'config': {'navigator.userAgent': 'saved-identity', 'addons': ['C:/private/addon']},
        'options': {'firefox_user_prefs': {}},
    })
    directory = store.profile_dir(profile['id'])
    directory.mkdir(parents=True)
    (directory / 'cookies.sqlite').write_bytes(b'session')
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        identity = client.get(PREFIX + '/profiles/' + profile['id'] + '/identity')
        assert identity.status_code == 200
        assert identity.json()['identity']['config']['navigator.userAgent'] == 'saved-identity'
        assert 'private/addon' not in identity.text and 'options' not in identity.text
        assert identity.json()['revision'] == profile['revision']

        stale = mutation(client, csrf, '/profiles/' + profile['id'] + '/identity', {
            'action': 'rebuild', 'revision': profile['revision'] + 1,
        })
        assert stale.status_code == 409 and stale.json() == {'error': 'revision_conflict'}

        clone = mutation(client, csrf, '/profiles/' + profile['id'] + '/clone', {
            'revision': profile['revision'],
        })
        assert clone.status_code == 201, clone.text
        cloned = store.get_profile(clone.json()['id'])
        assert cloned['identity'] is None and not store.profile_dir(cloned['id']).exists()
        assert (directory / 'cookies.sqlite').read_bytes() == b'session'

        rebuilt = mutation(client, csrf, '/profiles/' + profile['id'] + '/identity', {
            'action': 'rebuild', 'revision': profile['revision'],
        })
        assert rebuilt.status_code == 200 and rebuilt.json()['identity_present'] is False
        current = store.get_profile(profile['id'])
        assert current['identity'] is None and current['revision'] == profile['revision'] + 1

        invalid = mutation(client, csrf, '/profiles/' + other['id'] + '/delete', {
            'request_id': str(uuid4()), 'revision': other['revision'], 'remove_data': 'yes',
        })
        assert invalid.status_code == 202
        client.portal.call(lambda: client.app.state.team.jobs[invalid.json()['id']]['task'])
        invalid_result = client.get(PREFIX + '/tasks/' + invalid.json()['id']).json()
        assert invalid_result['state'] == 'failed' and invalid_result['error'] == 'invalid_request'
        assert store.get_profile(other['id'])['id'] == other['id']

        deleted = mutation(client, csrf, '/profiles/' + cloned['id'] + '/delete', {
            'request_id': str(uuid4()), 'revision': cloned['revision'], 'remove_data': False,
        })
        assert deleted.status_code == 202, deleted.text
        task = deleted.json()
        client.portal.call(lambda: client.app.state.team.jobs[task['id']]['task'])
        result = client.get(PREFIX + '/tasks/' + task['id']).json()
        assert result['state'] == 'succeeded' and result['result']['profile_id'] == cloned['id']
        with pytest.raises(KeyError):
            store.get_profile(cloned['id'])


def test_remote_service_allows_explicit_headless_run_without_native_session(workspace):
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        service = client.app.state.team
        service.mode = 'remote'

        def unexpected_native(*args, **kwargs):
            raise AssertionError('A headless run must not create a native remote session.')

        service.engine.native_factory = unexpected_native
        csrf = login(client)
        assert client.get(PREFIX + '/me').json()['capabilities']['launch_modes'] == ['remote', 'headless']
        response = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start', {
            'request_id': str(uuid4()), 'mode': 'headless',
        })
        assert response.status_code == 202, response.text
        command = settle(client, response.json())
        assert command['state'] == 'succeeded'
        run = next(p for p in client.get(PREFIX + '/profiles').json()['profiles']
                   if p['id'] == workspace[4]['id'])['run']
        assert run['mode'] == 'headless'
        stopped = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/stop', {
            'request_id': str(uuid4()), 'run_id': run['id'],
        })
        assert settle(client, stopped.json())['state'] == 'succeeded'


@pytest.mark.parametrize(('change', 'status', 'error'), [
    ({'max_running': 4}, 200, None),
    ({'max_running': 2}, 200, None),
    ({'max_running': 1}, 409, 'capacity_reached'),
    ({'executable_path': 'changed-browser'}, 409, 'service_busy'),
])
def test_settings_compare_engine_values_while_profiles_are_running(workspace, change, status, error):
    store = workspace[0]
    store.save_settings({'max_running': 2})
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        csrf = login(client)
        for profile in workspace[4:]:
            response = mutation(client, csrf, '/profiles/' + profile['id'] + '/start',
                                {'request_id': str(uuid4())})
            assert response.status_code == 202, response.text
            assert settle(client, response.json())['state'] == 'succeeded'
        before = client.get(PREFIX + '/settings').json()
        payload = {key: before[key] for key in ('max_running', 'executable_path')}
        response = mutation(client, csrf, '/settings', {**payload, **change})
        assert response.status_code == status, response.text
        if error:
            assert response.json() == {'error': error}
            assert client.get(PREFIX + '/settings').json() == before
        else:
            assert response.json() == {**before, **change}
            assert client.get(PREFIX + '/settings').json() == response.json()
        assert len(client.app.state.team.engine.sessions) == 2
        assert client.get(PREFIX + '/health').json()['active_runs'] == 2


def test_proxy_activity_settings_and_configuration_resources(workspace, monkeypatch):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        created = mutation(client, csrf, '/proxies', {
            'name': 'Managed proxy', 'server': 'http://proxy.test:3128',
        })
        assert created.status_code == 201, created.text
        proxy = created.json()
        assert proxy['has_password'] is False and 'password' not in proxy
        assert client.get(PREFIX + '/proxies').json()['proxies'][0]['id'] == proxy['id']

        settings = client.get(PREFIX + '/settings')
        assert settings.status_code == 200 and settings.json()['max_running'] == 5
        saved = mutation(client, csrf, '/settings', {'max_running': 7})
        assert saved.status_code == 200 and saved.json()['max_running'] == 7
        denied = mutation(client, csrf, '/settings', {'automation_enabled': True})
        assert denied.status_code == 403

        events = client.get(PREFIX + '/activity')
        assert events.status_code == 200 and any(e['action'] == 'proxy.created' for e in events.json()['events'])

        exported = client.get(PREFIX + '/configuration')
        assert exported.status_code == 200
        assert '"identity":' not in exported.text and '"password":' not in exported.text
        payload = exported.json()
        payload['profiles'] = []
        imported = mutation(client, csrf, '/configuration/import', {
            'request_id': str(uuid4()), 'configuration': payload,
        })
        assert imported.status_code == 202, imported.text
        task = imported.json()
        client.portal.call(lambda: client.app.state.team.jobs[task['id']]['task'])
        assert client.get(PREFIX + '/tasks/' + task['id']).json()['state'] == 'succeeded'

        login(client, 'reader', 'member-password-2')
        for path in ('/proxies', '/settings', '/configuration'):
            assert client.get(PREFIX + path).status_code == 403


def test_cookie_resource_reuses_engine_validation_and_never_uses_server_paths(workspace):
    cookies = []

    def cookie_engine(store, state, message, failure):
        class Context:
            pages = []

            def on(self, event, callback):
                self.callback = callback

            async def add_cookies(self, values):
                cookies[:] = values

            async def cookies(self):
                return list(cookies)

        @asynccontextmanager
        async def browser(**kwargs):
            yield Context()

        return BrowserEngine(store, state, message, failure,
                             prepare=lambda store, profile: {'headless': True},
                             browser_factory=browser)

    application = create_app(workspace[0].root, origin=ORIGIN, browser_mode='headless',
                             engine_factory=cookie_engine)
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = login(client)
        started = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start', {
            'request_id': str(uuid4()),
        }).json()
        settle(client, started)
        payload = {'cookies': [{
            'name': 'sid', 'value': 'secret-cookie', 'domain': 'example.test', 'path': '/',
        }]}
        imported = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/cookies', payload)
        assert imported.status_code == 200 and imported.json() == {'imported': 1}
        exported = client.get(PREFIX + '/profiles/' + workspace[4]['id'] + '/cookies')
        assert exported.status_code == 200 and exported.json() == payload
        assert 'secret-cookie' not in json.dumps(workspace[0].list_events())


def test_batch_commands_keep_individual_ids_runs_and_results(workspace):
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        csrf = login(client)
        requested = {profile['id']: str(uuid4()) for profile in workspace[4:]}
        response = mutation(client, csrf, '/profiles/batch', {
            'action': 'headless_start',
            'items': [{'profile_id': profile_id, 'request_id': request_id}
                      for profile_id, request_id in requested.items()],
        })
        assert response.status_code == 202, response.text
        items = response.json()['items']
        assert {item['profile_id'] for item in items} == set(requested)
        commands = [settle(client, item['command']) for item in items]
        assert all(command['state'] == 'succeeded' for command in commands)
        assert len({command['run_id'] for command in commands}) == 2

        stopped = mutation(client, csrf, '/profiles/batch', {
            'action': 'stop',
            'items': [{'profile_id': command['profile_id'], 'request_id': str(uuid4()),
                       'run_id': command['run_id']} for command in commands],
        })
        assert stopped.status_code == 202
        assert all(settle(client, item['command'])['state'] == 'succeeded'
                   for item in stopped.json()['items'])


def test_proxy_probe_and_task_idempotency_are_bounded(workspace, monkeypatch):

    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        proxy = mutation(client, csrf, '/proxies', {
            'name': 'Probe', 'server': 'http://proxy.test:3128',
        }).json()

        async def probe(proxy_id):
            assert proxy_id == proxy['id']
            return {'ip': '203.0.113.5', 'timezone': 'Asia/Singapore', 'latency_ms': 12}

        monkeypatch.setattr(client.app.state.team.engine, 'probe', probe)
        request_id = str(uuid4())
        first = mutation(client, csrf, '/proxies/' + proxy['id'] + '/probe', {'request_id': request_id})
        again = mutation(client, csrf, '/proxies/' + proxy['id'] + '/probe', {'request_id': request_id})
        assert first.status_code == again.status_code == 202
        assert first.json()['id'] == again.json()['id']
        client.portal.call(lambda: client.app.state.team.jobs[first.json()['id']]['task'])
        assert client.get(PREFIX + '/tasks/' + first.json()['id']).json()['result']['latency_ms'] == 12


def test_proxy_probe_failure_reports_actionable_code_without_diagnostics(workspace, monkeypatch):
    from camoufox.studio.engine import ProxyCheckError
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        proxy = mutation(client, csrf, '/proxies', {'name':'Probe', 'server':'http://proxy.test:3128'}).json()
        async def fail(proxy_id):
            raise ProxyCheckError('private-diagnostic')
        monkeypatch.setattr(client.app.state.team.engine, 'probe', fail)
        task = mutation(client, csrf, '/proxies/'+proxy['id']+'/probe', {'request_id':str(uuid4())}).json()
        client.portal.call(lambda: client.app.state.team.jobs[task['id']]['task'])
        response = client.get(PREFIX+'/tasks/'+task['id'])
        assert response.json()['error'] == 'proxy_failed'
        assert 'private-diagnostic' not in response.text


def test_task_request_id_cannot_cross_operation_kinds(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        cookie = client.cookies['__Host-camoufox-team']

        async def exercise():
            service = client.app.state.team
            access = await service.access(cookie)

            async def complete(job):
                return {'ok': True}

            request_id = str(uuid4())
            first = await service.submit_job(access, 'first', request_id, {'same': True}, complete)
            await service.jobs[first['id']]['task']
            with pytest.raises(TeamError) as raised:
                await service.submit_job(access, 'second', request_id, {'same': True}, complete)
            return raised.value.code

        assert client.portal.call(exercise) == 'request_conflict'


def test_kernel_inventory_is_admin_only_and_does_not_expose_absolute_paths(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client, 'reader', 'member-password-2')
        assert client.get(PREFIX + '/kernels').status_code == 403
        login(client)
        response = client.get(PREFIX + '/kernels')
        assert response.status_code == 200
        assert set(response.json()) == {'camoufox', 'cache', 'default'}
        assert response.json()['cache'] is None
        assert str(workspace[0].root) not in response.text


def test_remote_entry_is_authorized_redacted_and_does_not_start_browser(workspace):
    store, _, _, _, profile, hidden = workspace
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        path = PREFIX + '/profiles/' + profile['id'] + '/remote'
        assert client.get(path).status_code == 401
        login(client, 'reader', 'member-password-2')
        before = store.list_events()
        response = client.get(path)
        assert response.status_code == 200, response.text
        entry = response.json()
        assert entry['profile'] == client.get(PREFIX + '/profiles').json()['profiles'][0]
        assert entry['available'] is False and entry['reason'] == 'remote_disabled'
        assert entry['viewer_path'] == '/#remote=' + profile['id']
        assert entry['control_path'] is None and entry['profile']['run'] is None
        assert not client.app.state.team.engine.sessions and not client.app.state.team.tasks
        assert store.list_events() == before
        for forbidden in ('private note', 'identity', 'proxy_id', str(store.root)):
            assert forbidden not in response.text
        for denied_id in (hidden['id'], str(uuid4())):
            denied = client.get(PREFIX + '/profiles/' + denied_id + '/remote')
            assert denied.status_code == 403 and denied.json() == {'error': 'access_denied'}
        login(client, 'op', 'member-password-1')
        assert client.get(path).json()['reason'] == 'watch_required'
        login(client)
        assert client.get(PREFIX + '/profiles/' + str(uuid4()) + '/remote').status_code == 404
        workspace[0].revoke_profile(workspace[1]['id'], workspace[3]['id'], profile['id'])
        login(client, 'reader', 'member-password-2')
        assert client.get(path).status_code == 403


@pytest.mark.parametrize('state,reason', [
    (None, 'not_running'), ('starting', 'starting'), ('stopping', 'stopping'),
    ('stopped', 'not_running'), ('recovery_required', 'recovery_required'),
    ('running', 'runtime_unavailable'),
])
def test_remote_entry_reports_durable_state_without_claiming_missing_runtime(workspace, state, reason):
    shutdown = (pytest.raises(StudioError, match='Unresolved execution records remain')
                if state == 'recovery_required' else nullcontext())
    with shutdown, TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        service = client.app.state.team
        service.mode = 'remote'
        run_id = None
        if state is not None:
            session = workspace[0].login('root', 'admin-password-1')
            command, _ = service.repository.reserve(session['session_id'], workspace[4]['id'], 'start',
                str(uuid4()), owner=service.owner, mode='remote')
            run_id = command['run_id']
            service.repository.update_run(run_id, service.owner, state)
        try:
            response = client.get(PREFIX + '/profiles/' + workspace[4]['id'] + '/remote')
            assert response.status_code == 200, response.text
            entry = response.json()
            assert entry['available'] is False and entry['reason'] == reason
            assert entry['control_path'] is None
            if run_id:
                assert entry['profile']['run']['id'] == run_id
                assert 'owner' not in entry['profile']['run']
        finally:
            if run_id and state != 'recovery_required':
                service.repository.update_run(run_id, service.owner, 'stopped')


@pytest.mark.parametrize('mode,flag,reason', [
    ('headless', None, 'not_remote'), ('remote', 'closing', 'service_closing'),
    ('remote', 'failed', 'recovery_required'), ('remote', None, 'runtime_unavailable'),
])
def test_remote_entry_checks_service_barriers_and_actual_native_session(workspace, mode, flag, reason):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        service = client.app.state.team
        service.mode = 'remote'
        profile_id = workspace[4]['id']
        session = workspace[0].login('root', 'admin-password-1')
        command, _ = service.repository.reserve(session['session_id'], profile_id, 'start',
            str(uuid4()), owner=service.owner, mode=mode)
        run_id = command['run_id']
        service.repository.update_run(run_id, service.owner, 'running')
        service.remotes[run_id] = RemoteRun(service, profile_id, run_id, object())
        if flag:
            setattr(service, flag, True)
        try:
            response = client.get(PREFIX + '/profiles/' + profile_id + '/remote')
            assert response.status_code == 200, response.text
            assert response.json()['reason'] == reason
            assert response.json()['available'] is False and response.json()['control_path'] is None
        finally:
            if flag:
                setattr(service, flag, False)
            service.remotes.pop(run_id)
            service.repository.update_run(run_id, service.owner, 'stopped')


def test_health_is_admin_only_and_does_not_register_or_disclose_the_runtime(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client, 'reader', 'member-password-2')
        assert client.get(PREFIX + '/health').status_code == 403
        login(client)
        response = client.get(PREFIX + '/health')
        assert response.status_code == 200
        report = response.json()
        assert report['active_runs'] == 0 and report['pending_commands'] == 0
        assert report['active_tasks'] == 0 and report['maintenance'] is False
        assert report['max_running'] == 5 and report['runtime_registered'] is False
        assert type(report['disk_free_bytes']) is int and report['disk_free_bytes'] > 0
        assert 'host_id' not in report and str(workspace[0].root) not in response.text



def test_metadata_does_not_claim_native_remote_display_is_available(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        capabilities = client.get(PREFIX + '/me').json()['capabilities']
        assert capabilities['remote_display'] is False and capabilities['browser_mode'] == 'disabled'
        assert client.get(PREFIX + '/display/' + workspace[4]['id']).status_code == 404
        assert client.get('/automation/v1/profiles').status_code == 404
        assert client.get('/docs').status_code == 404


@pytest.mark.parametrize('name,password', [('missing', 'wrong'), ('root', 'wrong')])
def test_invalid_login_is_generic(workspace, name, password):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        response = client.post(PREFIX + '/login', headers={'Origin': ORIGIN},
                               json={'name': name, 'password': password})
        assert response.status_code == 401 and response.json() == {'error': 'invalid_credentials'}


def test_body_limits_and_unknown_fields(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        assert client.post(PREFIX + '/login', headers={'Origin': ORIGIN, 'Content-Type': 'application/json'},
                           content=b'x' * 65537).status_code == 413
        csrf = login(client)
        assert mutation(client, csrf, '/profiles', {'name': 'Env', 'identity': {}}).status_code == 400
        assert mutation(client, csrf, '/members', {'action': '__getattribute__'}).status_code == 400


def test_existing_websocket_is_closed_after_permission_revocation(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client, 'op', 'member-password-1')
        with client.websocket_connect('wss://team.test' + PREFIX + '/events', headers={'Origin': ORIGIN}) as socket:
            snapshot = socket.receive_json()
            assert [p['id'] for p in snapshot['profiles']] == [workspace[4]['id']]
            workspace[0].revoke_profile(workspace[1]['id'], workspace[2]['id'], workspace[4]['id'])
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
            assert error.value.code == 4401
        assert client.get(PREFIX + '/profiles').status_code == 401


def test_websocket_rejects_wrong_origin_and_query_tokens(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        for path, origin in ((PREFIX + '/events', 'https://evil.test'),
                             (PREFIX + '/events?token=anything', ORIGIN)):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect('wss://team.test' + path, headers={'Origin': origin}):
                    pytest.fail('accepted an unauthorized subscription')


def test_service_and_desktop_are_mutually_exclusive(workspace):
    with TestClient(app(workspace), base_url=ORIGIN):
        with pytest.raises(WorkspaceLockError):
            WorkspaceLock(workspace[0].root).acquire()
        assert not (workspace[0].root / 'automation.json').exists()
    assert not (workspace[0].root / 'workspace.lock').exists()


def test_requires_preexisting_locally_initialized_team(tmp_path):
    with pytest.raises(Exception, match='existing'):
        with TestClient(create_app(tmp_path, origin=ORIGIN)):
            pytest.fail('initialized a remote workspace')
    store = ProfileStore(tmp_path)
    with pytest.raises(Exception, match='administrator'):
        with TestClient(create_app(store.root, origin=ORIGIN)):
            pytest.fail('allowed remote administrator setup')
    assert not (store.root / 'workspace.lock').exists()


def test_admin_can_manage_groups_without_exporting_hashes(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        group = mutation(client, csrf, '/groups', {'action': 'create', 'name': 'Shared'}).json()
        group = mutation(client, csrf, '/groups', {
            'action': 'members', 'id': group['id'], 'revision': group['revision'],
            'members': [workspace[2]['id']]}).json()
        group = mutation(client, csrf, '/groups', {
            'action': 'grants', 'id': group['id'], 'revision': group['revision'],
            'changes': {workspace[5]['id']: ['view', 'operate']}}).json()
        response = client.get(PREFIX + '/admin')
        assert 'Shared' in response.text and 'password_' not in response.text
        assert response.json()['groups'][0]['revision'] == group['revision']
        assert mutation(client, csrf, '/groups', {
            'action': 'members', 'id': group['id'], 'revision': 1, 'members': []}).status_code == 409


def test_explicit_headless_execution_is_unique_and_request_ids_are_durable(workspace):
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        csrf = login(client, 'op', 'member-password-1')
        path = '/profiles/' + workspace[4]['id'] + '/start'
        body = {'request_id': str(uuid4())}
        first = mutation(client, csrf, path, body)
        assert first.status_code == 202, first.text
        command = settle(client, first.json())
        assert command['state'] == 'succeeded'
        assert mutation(client, csrf, path, body).json()['id'] == command['id']
        assert mutation(client, csrf, path, {'request_id': str(uuid4())}).status_code == 409
        profile = client.get(PREFIX + '/profiles').json()['profiles'][0]
        assert profile['run']['state'] == 'running'
        stop = mutation(client, csrf, '/profiles/' + profile['id'] + '/stop', {
            'request_id': str(uuid4()), 'run_id': profile['run']['id']})
        assert settle(client, stop.json())['state'] == 'succeeded'
        assert client.get(PREFIX + '/profiles').json()['profiles'][0]['run']['state'] == 'stopped'
        assert mutation(client, csrf, path, body).json()['id'] == command['id']


def test_stale_stop_and_edit_of_active_profile_are_rejected(workspace):
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        csrf = login(client)
        start = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start',
                         {'request_id': str(uuid4())}).json()
        settle(client, start)
        assert mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/stop', {
            'request_id': str(uuid4()), 'run_id': str(uuid4())}).status_code == 409
        assert mutation(client, csrf, '/profiles/' + workspace[4]['id'], {
            'revision': workspace[4]['revision'], 'data': {'name': 'Changed'}}).status_code == 409


def test_disabled_execution_fails_without_claiming_a_run(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        csrf = login(client)
        response = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start',
                            {'request_id': str(uuid4())})
        assert response.status_code == 503 and response.json()['error'] == 'execution_disabled'
        with sqlite3.connect(workspace[0].database_path) as connection:
            assert connection.execute('SELECT COUNT(*) FROM team_runs').fetchone()[0] == 0


def test_orphaned_run_blocks_reopening_even_if_workspace_marker_is_missing(workspace):
    from camoufox.studio.team.repository import TeamRepository

    repository = TeamRepository(workspace[0])
    session = workspace[0].login('root', 'admin-password-1')
    repository.reserve(session['session_id'], workspace[4]['id'], 'start', str(uuid4()),
                       owner='previous-process', mode='headless')
    with pytest.raises(Exception, match='recovery'):
        with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN):
            pytest.fail('automatically recovered an unproven browser')
    assert (workspace[0].root / 'workspace.lock').exists()


def test_stop_reserved_before_start_dispatch_prevents_the_late_launch(workspace):
    from camoufox.studio.access import MemberStore
    from camoufox.studio.team.service import TeamService

    async def run():
        service = TeamService(workspace[0].root, browser_mode='headless', engine_factory=engine_factory)
        await service.open()
        session = service.store.login('root', 'admin-password-1')
        access = MemberStore(service.store, session)
        start, _ = service.repository.reserve(session['session_id'], workspace[4]['id'], 'start',
                                              str(uuid4()), owner=service.owner, mode='headless')
        stop, _ = service.repository.reserve(session['session_id'], workspace[4]['id'], 'stop',
                                             str(uuid4()), owner=service.owner, mode='headless', run_id=start['run_id'])
        await service._execute(access, stop)
        await service._execute(access, start)
        try:
            assert not service.engine.sessions
            assert service.repository.command(start['id'])['state'] == 'failed'
            assert service.repository.latest_run(workspace[4]['id'])['state'] == 'stopped'
        finally:
            await service.close()
    asyncio.run(run())


def test_cookie_rotation_revokes_the_previous_remote_login(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        first = client.cookies['__Host-camoufox-team']
        login(client)
        assert client.cookies['__Host-camoufox-team'] != first
        assert client.get(PREFIX + '/me', headers={'Cookie': '__Host-camoufox-team=' + first}).status_code == 401
        assert client.get(PREFIX + '/me').status_code == 200


def test_query_credentials_are_rejected_even_on_profile_list(workspace):
    with TestClient(app(workspace), base_url=ORIGIN) as client:
        login(client)
        assert client.get(PREFIX + '/profiles?token=anything').status_code == 400


def test_server_configuration_pins_one_worker_and_rejects_proxy_header_trust(workspace, monkeypatch):
    from camoufox.studio.team.host import configuration
    monkeypatch.setenv('WEB_CONCURRENCY', '8')
    config = configuration(workspace[0].root)
    assert config.host == '127.0.0.1' and config.port == 8766
    assert config.ssl_certfile is None and config.ssl_keyfile is None
    assert config.workers == 1 and config.proxy_headers is False and config.access_log is False
    assert config.ws_max_size == 6 * 1024 * 1024 + 1024 and config.limit_concurrency == 64
    assert config.ws_max_queue == 2 and config.ws_per_message_deflate is False


def test_plain_http_needs_no_certificate_or_network_allowlist(workspace):
    from camoufox.studio.team.host import configuration
    origin = 'http://team.example:8766'
    config = configuration(workspace[0].root, origin=origin, listen='0.0.0.0')
    with TestClient(config.app, base_url=origin, client=('203.0.113.20', 50000)) as client:
        response = client.post(PREFIX + '/login', headers={'Origin': origin},
                               json={'name': 'root', 'password': 'admin-password-1'})
        assert response.status_code == 200
        cookie = response.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie and '; secure' not in cookie
        capabilities = client.get(PREFIX + '/me').json()['capabilities']
        assert capabilities['secure_transport'] is False
        assert capabilities['private_network_only'] is False
        assert capabilities['client_allowlist'] is False
        assert client.post(PREFIX + '/logout', headers={'Origin': 'http://192.168.10.6:8766'},
                           json={}).status_code == 403
        assert client.post(PREFIX + '/logout', headers={'Origin': origin}, json={}).status_code == 403


def test_https_still_requires_both_certificate_files(workspace, tmp_path):
    from camoufox.studio.team.host import configuration
    cert = tmp_path / 'cert.pem'
    key = tmp_path / 'key.pem'
    cert.write_text('certificate')
    key.write_text('key')
    with pytest.raises(ValueError, match='certificate and private key'):
        configuration(workspace[0].root, origin='https://team.example:8766')
    with pytest.raises(ValueError, match='HTTP origin'):
        configuration(workspace[0].root, cert=cert, key=key)
    config = configuration(workspace[0].root, origin='https://team.example:8766', cert=cert, key=key)
    assert config.ssl_certfile == str(cert) and config.ssl_keyfile == str(key)


def test_private_network_switch_and_client_allowlist_filter_http_and_websockets(workspace):
    origin = 'http://team.example:8766'
    def network_login(client):
        response = client.post(PREFIX + '/login', headers={'Origin': origin},
                               json={'name': 'root', 'password': 'admin-password-1'})
        assert response.status_code == 200

    def private():
        return create_app(workspace[0].root, origin=origin, private_network_only=True)

    with TestClient(private(), base_url=origin, client=('192.168.10.20', 50000)) as client:
        network_login(client)
        assert client.get(PREFIX + '/me').json()['capabilities']['private_network_only'] is True
    with TestClient(private(), base_url=origin, client=('203.0.113.20', 50000)) as client:
        assert client.get('/').status_code == 403

    def restricted():
        return create_app(workspace[0].root, origin=origin,
                          allow_client=('203.0.113.0/24', '2001:db8::5'))

    with TestClient(restricted(), base_url=origin, client=('203.0.113.20', 50000)) as client:
        network_login(client)
        assert client.get(PREFIX + '/me').json()['capabilities']['client_allowlist'] is True
    with TestClient(restricted(), base_url=origin, client=('198.51.100.20', 50000)) as client:
        assert client.get('/').status_code == 403
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(PREFIX + '/events', headers={'Origin': origin}):
                pass


@pytest.mark.parametrize('entry', ['', 'team.example', '192.168.1.1/not-a-prefix'])
def test_client_allowlist_rejects_non_ip_entries(workspace, entry):
    with pytest.raises(ValueError, match='allowlist'):
        create_app(workspace[0].root, origin='http://team.example:8766', allow_client=(entry,))


def test_queued_command_rechecks_permissions_before_browser_side_effect(workspace):
    from camoufox.studio.access import MemberStore
    from camoufox.studio.team.service import TeamService

    async def run():
        service = TeamService(workspace[0].root, browser_mode='headless', engine_factory=engine_factory)
        await service.open()
        session = service.store.login('op', 'member-password-1')
        access = MemberStore(service.store, session)
        command, _ = service.repository.reserve(session['session_id'], workspace[4]['id'], 'start',
                                                str(uuid4()), owner=service.owner, mode='headless')
        service.store.revoke_profile(workspace[1]['id'], workspace[2]['id'], workspace[4]['id'])
        await service._execute(access, command)
        assert not service.engine.sessions
        assert service.repository.command(command['id'])['state'] == 'failed'
        assert service.repository.latest_run(workspace[4]['id'])['state'] == 'stopped'
        await service.close()
    asyncio.run(run())


def test_revocation_during_launch_preparation_never_opens_a_browser(workspace):
    from threading import Event
    from camoufox.studio.access import MemberStore
    from camoufox.studio.team.service import TeamService

    entered, release = Event(), Event()
    launches = []

    def prepare(*args):
        entered.set()
        assert release.wait(5), 'Preparation was not released.'
        return {}

    def factory(store, state, message, failure):
        @asynccontextmanager
        async def browser(**kwargs):
            launches.append(kwargs)
            yield object()
        return BrowserEngine(store, state, message, failure, prepare=prepare, browser_factory=browser)

    async def run():
        service = TeamService(workspace[0].root, browser_mode='headless', engine_factory=factory)
        await service.open()
        session = service.store.login('op', 'member-password-1')
        access = MemberStore(service.store, session)
        command = await service.submit(access, workspace[4]['id'], 'start', {'request_id': str(uuid4())})
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            await service.work(service.store.logout, session['session_id'])
            await service.revoke_connections()
        finally:
            release.set()
        await service.tasks[command['id']]
        assert not launches
        assert service.repository.command(command['id'])['state'] == 'failed'
        assert service.repository.latest_run(workspace[4]['id'])['state'] == 'stopped'
        await service.close()
    asyncio.run(run())


def test_cleanup_failure_retains_run_and_workspace_ownership(workspace):
    def failed_engine(store, state, message, failure):
        @asynccontextmanager
        async def browser(**kwargs):
            class Context:
                pages = []
                def on(self, *args):
                    pass
            yield Context()
            raise RuntimeError('sensitive cleanup detail')
        return BrowserEngine(store, state, message, failure,
                             prepare=lambda *_: {}, browser_factory=browser)

    service_app = create_app(workspace[0].root, origin=ORIGIN,
                             browser_mode='headless', engine_factory=failed_engine)
    with pytest.raises(StudioError):
        with TestClient(service_app, base_url=ORIGIN) as client:
            csrf = login(client)
            command = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start',
                               {'request_id': str(uuid4())}).json()
            settle(client, command)
    assert (workspace[0].root / 'workspace.lock').exists()
    with sqlite3.connect(workspace[0].database_path) as connection:
        state, data = connection.execute('SELECT state, data FROM team_runs').fetchone()
    assert state == 'recovery_required' and 'sensitive cleanup detail' not in data


def test_parallel_reservations_only_accept_one_launch(workspace):
    from concurrent.futures import ThreadPoolExecutor
    from camoufox.studio.team.repository import TeamRepository

    session = workspace[0].login('root', 'admin-password-1')
    repository = TeamRepository(workspace[0])
    def reserve():
        try:
            return repository.reserve(session['session_id'], workspace[4]['id'], 'start', str(uuid4()),
                                      owner='owner', mode='headless')[0]
        except ValidationError as error:
            assert str(error) == 'profile_busy'
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve(), range(8)))
    assert sum(result is not None for result in results) == 1


def test_request_id_cannot_be_reused_for_a_different_command(workspace):
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        csrf = login(client)
        request_id = str(uuid4())
        started = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/start',
                           {'request_id': request_id}).json()
        settle(client, started)
        response = mutation(client, csrf, '/profiles/' + workspace[4]['id'] + '/stop',
                            {'request_id': request_id, 'run_id': started['run_id']})
        assert response.status_code == 409 and response.json()['error'] == 'request_conflict'


def test_desktop_engine_cannot_ignore_a_durable_team_reservation(workspace):
    from camoufox.studio.team.repository import TeamRepository

    repository = TeamRepository(workspace[0])
    session = workspace[0].login('root', 'admin-password-1')
    repository.reserve(session['session_id'], workspace[4]['id'], 'start', str(uuid4()),
                       owner='unproven-owner', mode='headless')
    async def run():
        engine = engine_factory(workspace[0], lambda *_: None, lambda *_: None, lambda *_: None)
        try:
            await engine.start(workspace[4]['id'])
            session = engine.sessions[workspace[4]['id']]
            await session.ready.wait()
            assert 'ownership' in session.error and session.context is None
            assert not engine.sessions
        finally:
            await engine.shutdown()
    asyncio.run(run())


def test_failed_status_channel_close_does_not_skip_browser_shutdown(workspace):
    from camoufox.studio.access import MemberStore
    from camoufox.studio.team.service import TeamService
    from starlette.websockets import WebSocketState

    class Socket:
        application_state = WebSocketState.CONNECTED

        async def close(self, **kwargs):
            raise RuntimeError('channel failed')

    async def run():
        service = TeamService(workspace[0].root, browser_mode='headless', engine_factory=engine_factory)
        await service.open()
        session = service.store.login('root', 'admin-password-1')
        access = MemberStore(service.store, session)
        command = await service.submit(access, workspace[4]['id'], 'start', {'request_id': str(uuid4())})
        await service.tasks[command['id']]
        service.connections[Socket()] = 'cookie'
        try:
            with pytest.raises(Exception):
                await service.close()
            assert not service.engine.sessions
            assert service.repository.latest_run(workspace[4]['id'])['state'] == 'stopped'
            assert service.lock.path.exists()
        finally:
            await service.engine.shutdown()
            await asyncio.gather(*service.monitors)
    asyncio.run(run())


def test_partial_legacy_settings_use_the_shared_capacity_default(workspace):
    from camoufox.studio.team.repository import TeamRepository

    with sqlite3.connect(workspace[0].database_path) as connection:
        connection.execute('INSERT INTO settings VALUES (1, ?)', ('{"executable_path":""}',))
    session = workspace[0].login('root', 'admin-password-1')
    command, created = TeamRepository(workspace[0]).reserve(
        session['session_id'], workspace[4]['id'], 'start', str(uuid4()), owner='owner', mode='headless')
    assert created and command['state'] == 'accepted'


def test_two_different_profiles_can_run_under_the_same_service_owner(workspace):
    with TestClient(app(workspace, browser_mode='headless'), base_url=ORIGIN) as client:
        csrf = login(client)
        for profile in workspace[4:]:
            command = mutation(client, csrf, '/profiles/' + profile['id'] + '/start',
                               {'request_id': str(uuid4())}).json()
            assert settle(client, command)['state'] == 'succeeded'
        assert len(client.app.state.team.engine.sessions) == 2


def test_capacity_counts_starting_reservations_not_only_live_contexts(workspace):
    from camoufox.studio.team.repository import TeamRepository

    workspace[0].save_settings({'max_running': 1})
    session = workspace[0].login('root', 'admin-password-1')
    repository = TeamRepository(workspace[0])
    repository.reserve(session['session_id'], workspace[4]['id'], 'start', str(uuid4()),
                       owner='owner', mode='headless')
    with pytest.raises(ValidationError, match='capacity_reached'):
        repository.reserve(session['session_id'], workspace[5]['id'], 'start', str(uuid4()),
                           owner='owner', mode='headless')


def test_remote_host_does_not_import_qt(workspace):
    import subprocess
    import sys

    result = subprocess.run([sys.executable, '-c',
                             'import sys; import camoufox.studio.team.host; '
                             'assert not any(n.startswith("PySide6") for n in sys.modules)'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
