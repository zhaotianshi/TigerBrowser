"""Profile isolation, fixed identity, routing and session teardown contracts."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from camoufox.studio import engine


class Store:
    def __init__(self, root):
        self.root = root
        self.events = []
        self.settings = {'max_running': 2, 'executable_path': str(root / 'camoufox.exe')}
        Path(self.settings['executable_path']).touch()
        self.profiles = {
            key: dict(id=key, name=key, archived=False, os='windows', locale='en-US',
                      timezone='', proxy_id=None, identity=None, headless=True,
                      humanize=True, block_images=False, start_urls=[])
            for key in ('a', 'b', 'c')
        }
        self.proxy = dict(server='http://127.0.0.1:8080', username='operator', password='private-value')

    def get_profile(self, key):
        return copy.deepcopy(self.profiles[key])

    def list_profiles(self):
        return [copy.deepcopy(p) for p in self.profiles.values() if not p['archived']]

    def get_settings(self):
        return self.settings.copy()

    def list_members(self):
        return []

    def uses_team_kernels(self):
        return bool(self.list_members())

    def check_execution_ownership(self, profile_id, *, run_id=None, owner=None):
        assert run_id is None and owner is None

    def check_runtime(self, *, headless):
        assert type(headless) is bool

    def native_platform_accepted(self, profile):
        return False

    def get_proxy(self, key, reveal_password=False):
        assert reveal_password
        return self.proxy.copy()

    def profile_dir(self, key):
        return self.root / 'profiles' / key

    def set_identity(self, key, identity):
        self.profiles[key]['identity'] = copy.deepcopy(identity)

    def claim_identity(self, key, identity):
        self.set_identity(key, identity)
        return True

    def mark_launched(self, key):
        pass

    def record_event(self, action, profile_id=None, detail=''):
        self.events.append((action, profile_id, detail))


class Context:
    def __init__(self):
        self.callback = None
        self.closed = False
        self.pages = []
        self.saved_cookies = []

    def on(self, event, callback):
        assert event == 'close'
        self.callback = callback

    async def add_cookies(self, cookies):
        self.saved_cookies = cookies

    async def cookies(self):
        return self.saved_cookies


class Harness:
    def __init__(self, store, prepare=None, launch_error=False):
        self.contexts = []
        self.states = {}
        self.messages = []
        self.errors = []
        self.changed = asyncio.Event()

        def state(key, value):
            self.states[key] = value
            self.changed.set()

        @asynccontextmanager
        async def factory(**options):
            assert options['persistent_context']
            if launch_error:
                raise RuntimeError('credentials:private-value')
            context = Context()
            self.contexts.append(context)
            try:
                yield context
            finally:
                context.closed = True

        self.engine = engine.BrowserEngine(
            store, state, self.messages.append, self.errors.append,
            prepare=prepare or (lambda store, profile: {'user_data_dir': str(store.profile_dir(profile['id']))}),
            browser_factory=factory,
        )
        from test_studio_automation import Member
        self.engine.automation.access = Member(store)

    async def wait(self, key, state):
        async def until():
            while self.states.get(key, {}).get('state') != state:
                self.changed.clear()
                await self.changed.wait()
        await asyncio.wait_for(until(), 5)


def test_proxy_check_keeps_typed_failure_and_redacts_credentials(monkeypatch):
    def unavailable(*args, **kwargs):
        raise requests.ConnectionError('socks5://operator:private-value@127.0.0.1:8080')

    monkeypatch.setattr(engine, 'proxy_exit_geo', unavailable)
    with pytest.raises(engine.ProxyCheckError) as failure:
        engine.check_proxy({'server': 'socks5://127.0.0.1:8080', 'password': 'private-value'})
    assert 'private-value' not in str(failure.value)


def test_parallel_sessions_and_duplicate_start(tmp_path):
    async def run():
        harness = Harness(Store(tmp_path))
        await harness.engine.start('a')
        await harness.engine.start('b')
        with pytest.raises(engine.StudioError, match='已在运行'):
            await harness.engine.start('a')
        with pytest.raises(engine.StudioError, match='并发上限'):
            await harness.engine.start('c')
        await harness.wait('a', 'running')
        await harness.wait('b', 'running')
        assert len(harness.contexts) == 2
        await harness.engine.shutdown()
        assert all(context.closed for context in harness.contexts)
        assert not harness.engine.sessions
        assert all(state['state'] == 'stopped' for state in harness.states.values())
    asyncio.run(run())


def test_native_restart_does_not_use_old_measurements_as_launch_authorization(tmp_path, monkeypatch):
    from camoufox.studio import attestation
    from camoufox.studio.store import StoreError

    def stale(*args, **kwargs):
        raise StoreError('attestation_runtime_mismatch')

    async def stale_after_launch(*args, **kwargs):
        raise StoreError('attestation_drift')

    async def run():
        class Remote:
            def __init__(self, options):
                self.options = options
                self.closed = asyncio.Event()

            async def attach(self):
                pass

            async def wait(self):
                await self.closed.wait()

        @asynccontextmanager
        async def native(options, profile):
            assert profile['id'] == 'a'
            remote = Remote(options)
            try:
                yield remote
            finally:
                remote.closed.set()

        monkeypatch.setattr(attestation, 'check', stale)
        monkeypatch.setattr(attestation, 'verify', stale_after_launch)
        harness = Harness(Store(tmp_path))
        harness.engine.native_factory = native
        await harness.engine.start('a')
        await harness.wait('a', 'running')
        assert harness.errors == []
        await harness.engine.shutdown()

    asyncio.run(run())


def test_automation_start_waits_and_reuses_one_headed_session(tmp_path):
    async def run():
        harness = Harness(Store(tmp_path))
        bridge = harness.engine.automation
        route = '/automation/v1/profiles/a/start'
        try:
            results = await asyncio.gather(*(bridge.dispatch('POST', route, {}) for _ in range(3)))
            assert all(result['profile']['state'] == 'running' for result in results)
            assert len(harness.contexts) == 1
            assert harness.engine.sessions['a'].headless is False
            assert harness.engine.store.get_profile('a')['headless'] is True
            await bridge.dispatch('POST', route, {})
            assert len(harness.contexts) == 1
            await bridge.stop()
            assert not harness.contexts[0].closed
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('headless', [False, True])
def test_automation_start_accepts_explicit_mode_without_changing_saved_profile(tmp_path, headless):
    async def run():
        harness = Harness(Store(tmp_path))
        original = harness.engine.store.get_profile('a')
        bridge = harness.engine.automation
        try:
            result = await bridge.dispatch('POST', '/automation/v1/profiles/a/start', {'headless': headless})
            assert result['profile']['headless'] is headless
            assert harness.engine.store.get_profile('a') == original
            await bridge.dispatch('POST', '/automation/v1/profiles/a/start', {'headless': headless})
            assert len(harness.contexts) == 1
            from camoufox.studio.automation import AutomationError
            with pytest.raises(AutomationError) as mismatch:
                await bridge.dispatch('POST', '/automation/v1/profiles/a/start', {'headless': not headless})
            assert mismatch.value.code == 'profile_mode_mismatch'
            assert not harness.contexts[0].closed
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('body', [{'headless': 'true'}, {'headless': 1}, {'headless': None}, {'unknown': True}])
def test_automation_start_rejects_invalid_mode_before_launch(tmp_path, body):
    from camoufox.studio.automation import AutomationError
    async def run():
        harness = Harness(Store(tmp_path))
        with pytest.raises(AutomationError) as error:
            await harness.engine.automation.dispatch('POST', '/automation/v1/profiles/a/start', body)
        assert error.value.code == 'invalid_request'
        assert not harness.contexts
        await harness.engine.shutdown()
    asyncio.run(run())


def test_automation_start_reports_launch_failure_and_rejects_invalid_profiles(tmp_path):
    from camoufox.studio.automation import AutomationError

    async def run():
        harness = Harness(Store(tmp_path), launch_error=True)
        bridge = harness.engine.automation
        with pytest.raises(AutomationError) as failed:
            await bridge.dispatch('POST', '/automation/v1/profiles/a/start', {})
        assert failed.value.code == 'profile_start_failed'
        assert 'private-value' not in str(failed.value)
        assert not harness.engine.sessions
        harness.engine.store.profiles['a']['archived'] = True
        for key in ('a', 'missing'):
            with pytest.raises(AutomationError) as missing:
                await bridge.dispatch('POST', f'/automation/v1/profiles/{key}/start', {})
            assert missing.value.code == 'profile_not_found'
        await harness.engine.shutdown()
    asyncio.run(run())


def test_automation_start_preserves_headless_and_stopping_sessions_and_limit(tmp_path):
    from camoufox.studio.automation import AutomationError

    async def run():
        harness = Harness(Store(tmp_path))
        bridge = harness.engine.automation
        try:
            await harness.engine.start('a')
            await harness.wait('a', 'running')
            with pytest.raises(AutomationError) as headless:
                await bridge.dispatch('POST', '/automation/v1/profiles/a/start', {})
            assert headless.value.code == 'profile_mode_mismatch'
            assert len(harness.contexts) == 1 and not harness.contexts[0].closed
            await bridge.dispatch('POST', '/automation/v1/profiles/b/start', {})
            with pytest.raises(AutomationError) as limit:
                await bridge.dispatch('POST', '/automation/v1/profiles/c/start', {})
            assert limit.value.code == 'profile_start_failed'
            assert '并发上限' in str(limit.value)
            await harness.engine.stop('b')
            with pytest.raises(AutomationError) as stopping:
                await bridge.dispatch('POST', '/automation/v1/profiles/b/start', {})
            assert stopping.value.code == 'profile_unavailable'
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('saved_headless, override', [(False, True), (True, False), (True, None), (False, None)])
def test_launch_mode_is_session_scoped(prepared, saved_headless, override):
    store, draws = prepared
    store.profiles['a']['headless'] = saved_headless
    engine.prepare_launch(store, store.get_profile('a'))
    saved = store.get_profile('a')
    launches = []

    def prepare(store, profile):
        options = engine.prepare_launch(store, profile)
        launches.append(options)
        return options

    async def run():
        harness = Harness(store, prepare=prepare)
        expected = saved_headless if override is None else override
        try:
            await harness.engine.start('a', headless=override)
            assert harness.states['a'] == {'state': 'starting', 'headless': expected}
            await harness.wait('a', 'running')
            assert harness.states['a'] == {'state': 'running', 'headless': expected}
            assert launches[0]['headless'] is expected
            assert launches[0]['user_data_dir'] == str(store.profile_dir('a'))
            await harness.engine.stop('a')
            assert harness.states['a'] == {'state': 'stopping', 'headless': expected}
            await harness.wait('a', 'stopped')
            await harness.engine.start('a')
            await harness.wait('a', 'running')
            assert launches[1]['headless'] is saved_headless
            assert store.get_profile('a') == saved
            assert len(draws) == 1
        finally:
            await harness.engine.shutdown()
        assert all(context.closed for context in harness.contexts)

    asyncio.run(run())


def test_manual_browser_close_releases_slot(tmp_path):
    async def run():
        harness = Harness(Store(tmp_path))
        await harness.engine.start('a')
        await harness.wait('a', 'running')
        harness.contexts[0].callback()
        await harness.wait('a', 'stopped')
        assert harness.contexts[0].closed
        await harness.engine.start('a')
        await harness.engine.shutdown()
    asyncio.run(run())


def test_stop_before_prepare_does_not_launch(tmp_path):
    async def run():
        harness = Harness(Store(tmp_path))
        await harness.engine.start('a')
        await harness.engine.stop('a')
        await harness.engine.shutdown()
        assert not harness.contexts
        assert harness.states['a']['state'] == 'stopped'
    asyncio.run(run())


def test_launch_failure_is_visible_and_redacted(tmp_path):
    async def run():
        store = Store(tmp_path)
        harness = Harness(store, launch_error=True)
        await harness.engine.start('a')
        await harness.wait('a', 'error')
        assert not harness.engine.sessions
        assert 'private-value' not in str(harness.states) + str(harness.errors) + str(store.events)
        assert harness.errors
    asyncio.run(run())


def test_proxy_failure_never_launches_direct(tmp_path):
    def fail(*args):
        raise engine.StudioError('代理检查失败')

    async def run():
        harness = Harness(Store(tmp_path), prepare=fail)
        await harness.engine.start('a')
        await harness.wait('a', 'error')
        assert not harness.contexts
    asyncio.run(run())


def test_archived_profile_and_shutdown_reject_start(tmp_path):
    async def run():
        store = Store(tmp_path)
        store.profiles['a']['archived'] = True
        harness = Harness(store)
        with pytest.raises(engine.StudioError, match='恢复'):
            await harness.engine.start('a')
        await harness.engine.shutdown()
        with pytest.raises(engine.StudioError, match='退出'):
            await harness.engine.start('b')
    asyncio.run(run())


def test_cookies_roundtrip_requires_running_context(tmp_path):
    async def run():
        store = Store(tmp_path)
        harness = Harness(store)
        path = tmp_path / 'cookies.json'
        cookies = [{'name': 'session', 'value': 'private-value', 'domain': 'example.com', 'path': '/'}]
        path.write_text(json.dumps({'cookies': cookies}), encoding='utf-8')
        with pytest.raises(engine.StudioError, match='启动'):
            await harness.engine.import_cookies('a', str(path))
        await harness.engine.start('a')
        await harness.wait('a', 'running')
        await harness.engine.import_cookies('a', str(path))
        await harness.engine.export_cookies('a', str(path))
        assert json.loads(path.read_text()) == cookies
        assert 'private-value' not in str(store.events)
        path.write_text('[{"name":"x"}]', encoding='utf-8')
        with pytest.raises(engine.StudioError, match='JSON'):
            await harness.engine.import_cookies('a', str(path))
        await harness.engine.shutdown()
    asyncio.run(run())


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = Store(tmp_path)
    draws = []
    from camoufox import addons, fpgen_model
    monkeypatch.setattr(addons, 'ADDONS_DIR', tmp_path / 'addons')
    for addon in addons.DefaultAddons:
        directory = Path(addons.get_addon_path(addon.name))
        directory.mkdir(parents=True)
        (directory / 'manifest.json').write_text('{}')
    monkeypatch.setattr(fpgen_model, 'is_pinned', lambda: True)

    def draw(**options):
        draws.append(options)
        options['config'].update({'navigator.userAgent': 'recorded', 'audio:seed': 1234})
        return {'firefox_user_prefs': {}, 'args': [], 'env': {'PRIVATE_ENV': 'private-value'}}

    monkeypatch.setattr(engine, 'resolve_verstr', lambda path: '152.0.4')
    monkeypatch.setattr(engine, 'launch_options', draw)
    monkeypatch.setattr(engine, 'validate_config', lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, 'get_target_os', lambda config: 'win')
    monkeypatch.setattr(engine, 'get_env_vars', lambda config, *args, **kwargs: {'CAMOU_CONFIG_1': json.dumps(config)})
    monkeypatch.setattr(engine, 'get_pref_env_vars', lambda prefs: {})
    return store, draws


def test_fixed_identity_excludes_credentials_and_host_environment(prepared, monkeypatch):
    store, draws = prepared
    monkeypatch.setenv('CAMOU_CONFIG_99', 'stale')
    first = engine.prepare_launch(store, store.get_profile('a'))
    second = engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 1
    assert first == second
    assert first['user_data_dir'] == str(store.root / 'profiles' / 'a')
    assert 'CAMOU_CONFIG_99' not in first['env']
    assert 'private-value' not in json.dumps(store.profiles['a']['identity'])
    assert first['firefox_user_prefs']['media.peerconnection.enabled'] is False


def test_team_profiles_select_and_record_distinct_builds_without_global_path(prepared, monkeypatch, tmp_path):
    from camoufox import multiversion
    from camoufox.pkgman import Version
    from camoufox.studio import kernels
    store, _ = prepared
    monkeypatch.setattr(store, 'list_members', lambda: [{'role': 'admin'}])
    installed = []
    for key, build in (('a', 'beta.31'), ('b', 'beta.32')):
        directory = tmp_path / build
        directory.mkdir()
        (directory / 'browser').touch()
        item = multiversion.InstalledVersion('official', Version(build, '152.0.4'), directory)
        installed.append(item)
        store.profiles[key]['kernel'] = kernels.kernel_id(item)
    monkeypatch.setattr(multiversion, 'list_installed', lambda: installed)
    monkeypatch.setattr(kernels, 'launch_path', lambda path: str(path / 'browser'))
    store.settings['executable_path'] = str(tmp_path / 'unrelated-global-browser')
    for key, item in zip(('a', 'b'), installed):
        options = engine.prepare_launch(store, store.get_profile(key))
        assert options['executable_path'] == str(item.path / 'browser')
        assert store.get_profile(key)['identity']['kernel'] == kernels.kernel_id(item)


def test_team_upgrade_preserves_historical_full_version_identity(prepared, monkeypatch, tmp_path):
    from camoufox import multiversion, utils
    from camoufox.pkgman import Version
    from camoufox.studio import kernels
    store, draws = prepared
    directory = tmp_path / 'beta.31'
    directory.mkdir()
    executable = directory / 'browser'
    executable.touch()
    (directory / 'application.ini').write_text('[App]\nVersion=152.0.4-beta.31\n')
    item = multiversion.InstalledVersion('official', Version('beta.31', '152.0.4'), directory)
    monkeypatch.setattr(engine, 'resolve_verstr', utils.resolve_verstr)
    store.settings['executable_path'] = str(executable)
    before = engine.prepare_launch(store, store.get_profile('a'))
    identity = store.get_profile('a')['identity']
    assert identity['browser_version'] == item.version.full_string and 'kernel' not in identity
    monkeypatch.setattr(store, 'list_members', lambda: [{'role': 'admin'}])
    monkeypatch.setattr(multiversion, 'list_installed', lambda: [item])
    monkeypatch.setattr(kernels, 'launch_path', lambda path: str(path / 'browser'))
    store.settings['executable_path'] = str(tmp_path / 'unrelated-global-browser')
    assert engine.prepare_launch(store, store.get_profile('a')) == before
    assert store.get_profile('a')['identity'] == identity
    assert len(draws) == 1


def test_launch_does_not_use_the_migration_runtime_manifest_as_permission(prepared):
    from camoufox.studio.store import StoreError

    store, _ = prepared

    def stale_runtime(*, headless):
        raise StoreError('runtime_mismatch')

    store.check_runtime = stale_runtime
    options = engine.prepare_launch(store, store.get_profile('a'))
    assert options['user_data_dir'] == str(store.root / 'profiles' / 'a')


def test_new_profile_uses_native_browser_tabs(prepared):
    store, _ = prepared
    options = engine.prepare_launch(store, store.get_profile('a'))
    assert json.loads(options['env']['CAMOU_CONFIG_1'])['disableTheming'] is True


def test_screen_edits_apply_at_launch_without_redrawing_the_saved_device(prepared):
    store, draws = prepared
    engine.prepare_launch(store, store.get_profile('a'))
    store.profiles['a']['identity']['config'].update({'screen.width': 1366, 'screen.height': 800,
        'screen.availWidth': 1366, 'screen.availHeight': 752, 'window.outerWidth': 1295, 'window.outerHeight': 752})
    identity = copy.deepcopy(store.profiles['a']['identity'])
    store.profiles['a']['fingerprint'] = {'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080}
    options = engine.prepare_launch(store, store.get_profile('a'))
    config = json.loads(options['env']['CAMOU_CONFIG_1'])
    assert (config['screen.width'], config['screen.height']) == (1920, 1080)
    assert store.get_profile('a')['identity'] == identity
    assert len(draws) == 1


@pytest.mark.parametrize('legacy_theme', [None, False, True])
def test_existing_profile_restores_native_tabs_without_resetting_identity(prepared, legacy_theme):
    store, draws = prepared
    engine.prepare_launch(store, store.get_profile('a'))
    config = store.profiles['a']['identity']['config']
    config.pop('disableTheming', None)
    if legacy_theme is not None:
        config['disableTheming'] = legacy_theme
    identity = copy.deepcopy(store.profiles['a']['identity'])

    options = engine.prepare_launch(store, store.get_profile('a'))

    assert json.loads(options['env']['CAMOU_CONFIG_1'])['disableTheming'] is True
    assert store.get_profile('a')['identity'] == identity
    assert len(draws) == 1


def test_proxy_geo_is_fresh_while_device_identity_stays_fixed(prepared, monkeypatch):
    store, draws = prepared
    store.profiles['a']['proxy_id'] = 'proxy'
    zones = iter(('Asia/Tokyo', 'Europe/Paris'))
    monkeypatch.setattr(engine, 'check_proxy', lambda proxy: {'timezone': next(zones)})
    first = engine.prepare_launch(store, store.get_profile('a'))
    second = engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 1
    assert first['proxy'] == second['proxy'] == store.proxy
    assert json.loads(first['env']['CAMOU_CONFIG_1'])['timezone'] == 'Asia/Tokyo'
    assert json.loads(second['env']['CAMOU_CONFIG_1'])['timezone'] == 'Europe/Paris'
    assert 'timezone' not in store.profiles['a']['identity']['config']
    assert 'private-value' not in json.dumps(store.profiles['a']['identity'])


def test_binary_version_change_requires_explicit_identity_reset(prepared, monkeypatch):
    store, _ = prepared
    engine.prepare_launch(store, store.get_profile('a'))
    monkeypatch.setattr(engine, 'resolve_verstr', lambda path: '155.0')
    with pytest.raises(engine.StudioError, match='版本'):
        engine.prepare_launch(store, store.get_profile('a'))


def test_portable_profile_rebases_addons_without_redrawing_identity(prepared, monkeypatch, tmp_path):
    from camoufox.studio import distribution

    store, draws = prepared
    engine.prepare_launch(store, store.get_profile('a'))
    store.profiles['a']['identity']['config']['addons'] = ['C:/old-location/UBO']
    identity = copy.deepcopy(store.profiles['a']['identity'])
    bundle = tmp_path / 'moved package' / 'bundle'
    addon = bundle / 'addons' / 'UBO'
    addon.mkdir(parents=True)
    (addon / 'manifest.json').write_text('{}')
    monkeypatch.setattr(engine, 'bundled_path', lambda name: bundle / name)
    monkeypatch.setattr(distribution, 'bundled_path', lambda name: bundle / name)

    options = engine.prepare_launch(store, store.get_profile('a'))

    assert options['executable_path'] == str(Path(store.settings['executable_path']).resolve())
    assert json.loads(options['env']['CAMOU_CONFIG_1'])['addons'] == [str(addon)]
    assert store.get_profile('a')['identity'] == identity
    assert len(draws) == 1


def test_proxy_credentials_encoded_and_dns_resolved_remotely():
    with patch.object(engine, 'proxy_exit_geo', return_value=('203.0.113.2', 'Etc/UTC')) as probe:
        engine.check_proxy({'server': 'socks5://localhost:1080', 'username': 'user@x', 'password': 'p:/@'})
    assert probe.call_args.args == ('socks5h://user%40x:p%3A%2F%40@localhost:1080',)
    assert not probe.call_args.kwargs['session'].trust_env


def test_proxy_check_ignores_system_proxy_and_netrc(monkeypatch):
    observed = []
    monkeypatch.setattr(requests.sessions, 'get_environ_proxies', lambda *args, **kwargs: {
        'http://ip-api.com': 'http://ambient.invalid:8080', 'all': 'http://ambient.invalid:8080',
    })
    monkeypatch.setattr(requests.sessions, 'get_netrc_auth', lambda *args, **kwargs: ('ambient', 'secret'))

    def send(adapter, request, **kwargs):
        observed.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"status":"success","query":"203.0.113.2","timezone":"Etc/UTC"}'
        return response

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    result = engine.check_proxy({'server': 'socks5://127.0.0.1:1080'})
    assert result['ip'] == '203.0.113.2'
    assert len(observed) == 1
    request, options = observed[0]
    assert options['proxies'] == {
        'http': 'socks5h://127.0.0.1:1080', 'https': 'socks5h://127.0.0.1:1080',
    }
    assert 'Authorization' not in request.headers


@pytest.mark.parametrize('failure, reason', [
    (requests.ReadTimeout('socks5://private-credential@host'), '超时'),
    (requests.ConnectionError('socks5://private-credential@host'), '连接'),
])
def test_proxy_check_explains_transport_failure_without_secrets(monkeypatch, failure, reason):
    from camoufox.exceptions import InvalidIP

    def fail(*args, **kwargs):
        raise InvalidIP('private-credential') from failure

    monkeypatch.setattr(engine, 'proxy_exit_geo', fail)
    with pytest.raises(engine.StudioError) as error:
        engine.check_proxy({'server': 'socks5://localhost:1080'})
    assert reason in str(error.value)
    assert 'private-credential' not in str(error.value)


def test_proxy_check_distinguishes_provider_rate_limit(monkeypatch):
    from camoufox.exceptions import InvalidIP

    def fail(*args, **kwargs):
        response = requests.Response()
        response.status_code = 429
        raise InvalidIP('private-credential') from requests.HTTPError(response=response)

    monkeypatch.setattr(engine, 'proxy_exit_geo', fail)
    with pytest.raises(engine.StudioError, match='限流'):
        engine.check_proxy({'server': 'socks5://localhost:1080'})


def test_missing_addon_fails_before_unbounded_implicit_download(prepared):
    from camoufox import addons
    store, draws = prepared
    Path(addons.get_addon_path('UBO'), 'manifest.json').unlink()
    with pytest.raises(engine.StudioError, match='fetch'):
        engine.prepare_launch(store, store.get_profile('a'))
    assert not draws


def test_missing_model_fails_before_implicit_download(prepared, monkeypatch):
    from camoufox import fpgen_model
    store, draws = prepared
    monkeypatch.setattr(fpgen_model, 'is_pinned', lambda: False)
    with pytest.raises(engine.StudioError, match='fetch'):
        engine.prepare_launch(store, store.get_profile('a'))
    assert not draws


def test_explicit_hardware_and_dnt_persist_with_identity(prepared):
    store, draws = prepared
    store.profiles['a']['fingerprint'] = {'hardware_concurrency': 12, 'do_not_track': True}
    first = engine.prepare_launch(store, store.get_profile('a'))
    second = engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 1
    assert first == second
    config = store.profiles['a']['identity']['config']
    assert config['navigator.hardwareConcurrency'] == 12
    assert config['navigator.doNotTrack'] == '1'
    assert first['firefox_user_prefs']['privacy.donottrackheader.enabled']


def test_webrtc_exit_changes_do_not_enter_saved_identity(prepared, monkeypatch):
    store, draws = prepared
    store.profiles['a'].update(proxy_id='proxy', fingerprint={'webrtc_mode': 'proxy'})
    addresses = iter(('198.51.100.2', '2001:db8::2'))
    monkeypatch.setattr(engine, 'check_proxy', lambda _: {'ip': next(addresses), 'timezone': 'Etc/UTC'})
    first = engine.prepare_launch(store, store.get_profile('a'))
    second = engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 1
    assert json.loads(first['env']['CAMOU_CONFIG_1'])['webrtc:ipv4'] == '198.51.100.2'
    assert json.loads(second['env']['CAMOU_CONFIG_1'])['webrtc:ipv6'] == '2001:db8::2'
    assert not any(key.startswith('webrtc:') for key in store.profiles['a']['identity']['config'])


def test_unbound_webrtc_replacement_is_rejected_before_generation(prepared):
    store, draws = prepared
    store.profiles['a']['fingerprint'] = {'webrtc_mode': 'proxy'}
    with pytest.raises(engine.StudioError, match='绑定代理'):
        engine.prepare_launch(store, store.get_profile('a'))
    assert not draws


def test_duplicate_identity_is_redrawn_before_browser_launch(prepared, monkeypatch):
    store, draws = prepared
    claimed = []

    def claim(key, identity):
        claimed.append(copy.deepcopy(identity))
        if len(claimed) == 1:
            return False
        store.set_identity(key, identity)
        return True

    monkeypatch.setattr(store, 'claim_identity', claim)
    engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 2
    assert store.get_profile('a')['identity'] == claimed[-1]
    engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 2


def test_exhausted_identity_combinations_fail_loudly(prepared, monkeypatch):
    store, draws = prepared
    monkeypatch.setattr(store, 'claim_identity', lambda *args: False)
    with pytest.raises(engine.StudioError, match='已有指纹组合'):
        engine.prepare_launch(store, store.get_profile('a'))
    assert len(draws) == 8
    assert store.get_profile('a')['identity'] is None


def test_proxy_probe_records_only_timezone_metadata_in_the_shared_store(tmp_path, monkeypatch):
    from camoufox.studio.store import ProfileStore

    async def run():
        store = ProfileStore(tmp_path)
        proxy = store.save_proxy({'name': 'Probe metadata', 'server': 'socks5://127.0.0.1:1080'})
        result = {'ip': '198.51.100.10', 'timezone': 'Etc/UTC', 'latency_ms': 10}
        monkeypatch.setattr(engine, 'check_proxy', lambda _: result)
        assert await Harness(store).engine.probe(proxy['id']) == result
        hint = store.get_proxy(proxy['id'])['last_probe']
        assert set(hint) == {'timezone', 'checked_at'} and hint['timezone'] == 'Etc/UTC'
    asyncio.run(run())


def test_browser_failure_finishes_with_the_real_audit_store(tmp_path):
    from camoufox.studio.store import ProfileStore

    async def run():
        store = ProfileStore(tmp_path)
        profile = store.save_profile({'name': 'Failure cleanup'})
        harness = Harness(store, launch_error=True)
        await harness.engine.start(profile['id'])
        await harness.engine.sessions[profile['id']].task
        assert harness.states[profile['id']]['state'] == 'error'
        assert not harness.engine.sessions
        assert store.list_events(limit=1)[0]['action'] == 'browser.error'
        assert 'private-value' not in json.dumps(store.list_events())
    asyncio.run(run())


def test_cookie_import_reports_native_rejection(tmp_path):
    async def run():
        harness = Harness(Store(tmp_path))
        await harness.engine.start('a')
        await harness.wait('a', 'running')
        context = harness.contexts[0]

        async def rejected(cookies):
            pass

        context.add_cookies = rejected
        path = tmp_path / 'cookies.json'
        path.write_text(json.dumps([{'name': 's', 'value': 'private-value', 'url': 'http://example.com'}]))
        try:
            with pytest.raises(engine.StudioError, match='Cookie'):
                await harness.engine.import_cookies('a', str(path))
            assert not any('已导入' in message for message in harness.messages)
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


def test_clean_teardown_after_session_error_allows_reuse(tmp_path, monkeypatch):
    async def run():
        store = Store(tmp_path)
        harness = Harness(store)
        original = store.mark_launched

        def fail(_):
            raise RuntimeError('private launch detail')

        monkeypatch.setattr(store, 'mark_launched', fail)
        await harness.engine.start('a')
        await harness.wait('a', 'error')
        assert harness.contexts[0].closed
        assert not harness.engine.recovery_required
        assert 'private launch detail' not in harness.errors[0]
        monkeypatch.setattr(store, 'mark_launched', original)
        await harness.engine.start('a')
        await harness.wait('a', 'running')
        await harness.engine.shutdown()
        assert all(context.closed for context in harness.contexts)
    asyncio.run(run())


def test_bridge_shutdown_failure_still_closes_browsers(tmp_path, monkeypatch):
    async def run():
        harness = Harness(Store(tmp_path))
        await harness.engine.start('a')
        await harness.wait('a', 'running')

        async def fail(_):
            raise RuntimeError('bridge shutdown failed')

        monkeypatch.setattr(harness.engine, 'configure_automation', fail)
        with pytest.raises(RuntimeError, match='bridge shutdown failed'):
            await harness.engine.shutdown()
        assert harness.contexts[0].closed
        assert not harness.engine.sessions
    asyncio.run(run())


def test_failed_resource_exit_runs_remaining_cleanup_and_prevents_restart(tmp_path, monkeypatch):
    cleanup = []

    @asynccontextmanager
    async def route(*args):
        try:
            yield None
        finally:
            cleanup.append('proxy')

    @asynccontextmanager
    async def browser(**options):
        try:
            yield Context()
        finally:
            cleanup.append('browser')
            raise RuntimeError('private teardown detail')

    monkeypatch.setattr(engine, 'browser_proxy', route)

    async def run():
        errors = []
        instance = engine.BrowserEngine(Store(tmp_path), lambda *_: None, lambda *_: None,
                                        errors.append, prepare=lambda *_: {}, browser_factory=browser)
        await instance.start('a')
        session = instance.sessions['a']
        await asyncio.wait_for(session.ready.wait(), 5)
        await instance.stop('a')
        await asyncio.wait_for(session.task, 5)
        assert cleanup == ['browser', 'proxy']
        assert instance.recovery_required
        assert 'private teardown detail' not in errors[0]
        with pytest.raises(engine.StudioError, match='清理'):
            await instance.start('a')
        with pytest.raises(engine.StudioError, match='清理'):
            await instance.shutdown()
    asyncio.run(run())
