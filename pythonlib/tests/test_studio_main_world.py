"""Main-world execution is an opt-in launch requirement, never a saved identity edit."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from camoufox.studio import engine
from camoufox.studio.automation import AutomationError, PREFIX
from test_studio_engine import Harness, prepared  # noqa: F401


@pytest.fixture
def supported(prepared):
    store, draws = prepared
    (store.root / 'properties.json').write_text(
        json.dumps([{'property': 'allowMainWorld', 'type': 'bool'}]), encoding='utf-8')
    return store, draws


@pytest.mark.parametrize('requested', [None, False, True])
@pytest.mark.parametrize('saved', [None, False, True])
def test_prepare_main_world_is_session_only(supported, requested, saved):
    store, draws = supported
    engine.prepare_launch(store, store.get_profile('a'))
    if saved is not None:
        store.profiles['a']['identity']['config']['allowMainWorld'] = saved
    original = store.get_profile('a')
    options = engine.prepare_launch(store, original, main_world_eval=requested)
    config = json.loads(engine._camou_config_blob(options))
    assert config.get('allowMainWorld', False) is (requested is True or saved is True)
    assert store.get_profile('a') == original
    assert len(draws) == 1
    assert options['user_data_dir'] == str(store.profile_dir('a'))


@pytest.mark.parametrize('schema', [[], [{'property': 'allowMainWorld', 'type': 'str'}]])
def test_unsupported_kernel_fails_before_identity_generation(prepared, schema):
    store, draws = prepared
    (store.root / 'properties.json').write_text(json.dumps(schema), encoding='utf-8')
    with pytest.raises(engine.StudioError, match='主世界'):
        engine.prepare_launch(store, store.get_profile('a'), main_world_eval=True)
    assert store.get_profile('a')['identity'] is None and not draws


@pytest.mark.parametrize('body', [
    {'mainWorldEval': 'true'}, {'mainWorldEval': 1}, {'mainWorldEval': 0},
    {'mainWorldEval': None}, {'mainWorldEval': []}, {'mainWorldEval': {}},
    {'mainWorldEval': True, 'config': {'allowMainWorld': True}},
])
def test_main_world_input_is_strict_and_never_launches(supported, body):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        try:
            with pytest.raises(AutomationError) as caught:
                await harness.engine.automation.dispatch('POST', PREFIX + '/profiles/a/start', body)
            assert caught.value.code == 'invalid_request'
            assert not harness.engine.sessions and not harness.contexts
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('requested', [False, True])
def test_automation_reports_actual_session_and_never_restarts(supported, requested):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        bridge = harness.engine.automation
        route = PREFIX + '/profiles/a/start'
        try:
            listing = await bridge.dispatch('GET', PREFIX + '/profiles', {})
            assert listing['protocolVersion'] == 1
            assert listing['capabilities']['mainWorldEval'] is True
            assert all(item['mainWorldEval'] is None for item in listing['profiles'])
            started = await bridge.dispatch('POST', route, {'mainWorldEval': requested})
            assert started['profile']['mainWorldEval'] is requested
            session = harness.engine.sessions['a']
            assert session.main_world_eval is requested
            if not requested:
                with pytest.raises(AutomationError) as caught:
                    await bridge.dispatch('POST', route, {'mainWorldEval': True})
                assert caught.value.code == 'profile_main_world_mismatch' and caught.value.status == 409
            for body in ({}, {'mainWorldEval': False}):
                reused = await bridge.dispatch('POST', route, body)
                assert reused['profile']['mainWorldEval'] is requested
            assert harness.engine.sessions['a'] is session and len(harness.contexts) == 1
            assert not session.stop.is_set() and not harness.contexts[0].closed
            assert (await bridge.dispatch('GET', PREFIX + '/profiles', {}))['profiles'][0]['mainWorldEval'] is requested
            await harness.engine.stop('a')
            await session.task
            assert (await bridge.dispatch('GET', PREFIX + '/profiles', {}))['profiles'][0]['mainWorldEval'] is None
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


def test_unsupported_error_is_stable_and_does_not_start_browser(prepared):
    store, draws = prepared
    (store.root / 'properties.json').write_text('[]', encoding='utf-8')

    async def run():
        harness = Harness(store, prepare=engine.prepare_launch)
        try:
            with pytest.raises(AutomationError) as caught:
                await harness.engine.automation.dispatch('POST', PREFIX + '/profiles/a/start', {'mainWorldEval': True})
            assert caught.value.code == 'main_world_unsupported' and caught.value.status == 409
            assert not harness.contexts and not harness.engine.sessions and not draws
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('first_requires', [False, True])
def test_concurrent_launch_checks_each_callers_requirement(supported, first_requires):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        bridge = harness.engine.automation
        route = PREFIX + '/profiles/a/start'
        try:
            results = await asyncio.gather(
                bridge.dispatch('POST', route, {'mainWorldEval': first_requires}),
                bridge.dispatch('POST', route, {'mainWorldEval': not first_requires}),
                return_exceptions=True)
            assert results[0]['profile']['mainWorldEval'] is first_requires
            if first_requires:
                assert results[1]['profile']['mainWorldEval'] is True
            else:
                assert isinstance(results[1], AutomationError)
                assert results[1].code == 'profile_main_world_mismatch'
            assert len(harness.contexts) == 1 and not harness.contexts[0].closed
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('schema', [None, '{', '{}', '[{"property":"allowMainWorld"}]'])
def test_missing_or_unreadable_schema_cannot_claim_main_world(prepared, schema):
    store, draws = prepared
    if schema is not None:
        (store.root / 'properties.json').write_text(schema, encoding='utf-8')
    with pytest.raises(engine.StudioError, match='主世界'):
        engine.prepare_launch(store, store.get_profile('a'), main_world_eval=True)
    assert not draws and store.get_profile('a')['identity'] is None


@pytest.mark.parametrize('macos', [False, True])
def test_capability_uses_selected_kernel_not_global_executable(supported, monkeypatch, macos):
    from camoufox.studio import kernels
    store, draws = supported
    directory = store.root / 'selected' / ('Contents/MacOS' if macos else 'bin')
    directory.mkdir(parents=True)
    executable = directory / 'camoufox'
    executable.touch()
    properties = directory.parent / 'Resources' if macos else directory
    properties.mkdir(exist_ok=True)
    schema = properties / 'properties.json'
    schema.write_text('[]', encoding='utf-8')
    monkeypatch.setattr(store, 'list_members', lambda: [{'role': 'admin'}])
    monkeypatch.setattr(kernels, 'resolve_kernel', lambda profile: (executable, 'official/test-build'))
    with pytest.raises(engine.StudioError, match='主世界'):
        engine.prepare_launch(store, store.get_profile('a'), main_world_eval=True)
    assert not draws
    schema.write_text('[{"property":"allowMainWorld","type":"bool"}]', encoding='utf-8')
    (store.root / 'properties.json').write_text('[]', encoding='utf-8')
    options = engine.prepare_launch(store, store.get_profile('a'), main_world_eval=True)
    assert options['executable_path'] == str(executable)
    assert json.loads(engine._camou_config_blob(options))['allowMainWorld'] is True
    assert 'allowMainWorld' not in store.get_profile('a')['identity']['config']


def test_reuse_reports_legacy_enabled_identity_without_a_new_request(supported):
    store, _ = supported
    engine.prepare_launch(store, store.get_profile('a'))
    store.profiles['a']['identity']['config']['allowMainWorld'] = True
    original = store.get_profile('a')

    async def run():
        harness = Harness(store, prepare=engine.prepare_launch)
        try:
            for body in ({}, {'mainWorldEval': True}, {'mainWorldEval': False}):
                result = await harness.engine.automation.dispatch('POST', PREFIX + '/profiles/a/start', body)
                assert result['profile']['mainWorldEval'] is True
            assert len(harness.contexts) == 1 and store.get_profile('a') == original
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('first_requires', [False, True])
def test_shared_start_callback_validates_waiters_after_preparation(supported, first_requires):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        bridge = harness.engine.automation
        entered, release = asyncio.Event(), asyncio.Event()
        callbacks = []

        async def start(profile_id, headless, *, main_world_eval):
            callbacks.append(main_world_eval)
            entered.set()
            await release.wait()
            await harness.engine.start(profile_id, headless=headless, main_world_eval=main_world_eval)
            return harness.engine.sessions[profile_id]

        bridge.start_profile = start
        route = PREFIX + '/profiles/a/start'
        first = asyncio.create_task(bridge.dispatch('POST', route, {'mainWorldEval': first_requires}))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            second = asyncio.create_task(bridge.dispatch('POST', route, {'mainWorldEval': not first_requires}))
            release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert results[0]['profile']['mainWorldEval'] is first_requires
            if first_requires:
                assert results[1]['profile']['mainWorldEval'] is True
            else:
                assert isinstance(results[1], AutomationError) and results[1].code == 'profile_main_world_mismatch'
            assert callbacks == [first_requires] and len(harness.contexts) == 1
        finally:
            release.set()
            await first
            await harness.engine.shutdown()
    asyncio.run(run())


def test_pending_browser_is_not_reported_as_enabled(supported):
    from contextlib import asynccontextmanager
    from test_studio_automation import Context

    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        entered, release = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def browser(**kwargs):
            entered.set()
            await release.wait()
            yield Context()

        harness.engine.browser_factory = browser
        try:
            await harness.engine.start('a', main_world_eval=True)
            await asyncio.wait_for(entered.wait(), 5)
            listing = await harness.engine.automation.dispatch('GET', PREFIX + '/profiles', {})
            assert listing['profiles'][0]['state'] == 'starting'
            assert listing['profiles'][0]['mainWorldEval'] is None
            release.set()
            await harness.wait('a', 'running')
            assert harness.engine.sessions['a'].main_world_eval is True
        finally:
            release.set()
            await harness.engine.shutdown()
    asyncio.run(run())


def test_requirement_is_isolated_between_profiles_and_later_launches(supported):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        bridge = harness.engine.automation
        try:
            first, other = await asyncio.gather(
                bridge.dispatch('POST', PREFIX + '/profiles/a/start', {'mainWorldEval': True}),
                bridge.dispatch('POST', PREFIX + '/profiles/b/start', {}))
            assert first['profile']['mainWorldEval'] is True
            assert other['profile']['mainWorldEval'] is False
            session = harness.engine.sessions['a']
            saved = harness.engine.store.get_profile('a')
            await harness.engine.stop('a')
            await session.task
            restarted = await bridge.dispatch('POST', PREFIX + '/profiles/a/start', {})
            assert restarted['profile']['mainWorldEval'] is False
            assert harness.engine.store.get_profile('a') == saved
            assert not harness.engine.sessions['b'].stop.is_set()
        finally:
            await harness.engine.shutdown()
    asyncio.run(run())


def test_explicit_main_world_restart_is_exclusive_and_session_only(supported):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        bridge = harness.engine.automation
        stopped = []

        async def restart(profile_id, expected, headless):
            assert harness.engine.sessions[profile_id] is expected
            stopped.append(profile_id)
            await harness.engine.stop(profile_id)
            await expected.task
            await harness.engine.start(profile_id, headless=headless, main_world_eval=True)
            return harness.engine.sessions[profile_id]

        bridge.restart_profile = restart
        route = PREFIX + '/profiles/a/start'
        body = {'mainWorldEval': True, 'restartIfNeeded': True}
        try:
            await bridge.dispatch('POST', route, {})
            await bridge.dispatch('POST', PREFIX + '/profiles/b/start', {})
            original = harness.engine.store.get_profile('a')
            other = harness.engine.sessions['b']
            bridge.leases['busy'] = SimpleNamespace(profile_id='a')
            with pytest.raises(AutomationError) as caught:
                await bridge.dispatch('POST', route, body)
            assert caught.value.code == 'profile_leased' and not stopped
            bridge.leases.pop('busy')
            result = await bridge.dispatch('POST', route, body)
            assert result['profile']['mainWorldEval'] is True and stopped == ['a']
            assert harness.engine.store.get_profile('a') == original
            assert harness.engine.sessions['b'] is other and not other.stop.is_set()
            await bridge.dispatch('POST', route, body)
            assert stopped == ['a'], 'An enabled session is reused'
        finally:
            bridge.leases.pop('busy', None)
            await harness.engine.shutdown()
    asyncio.run(run())


def test_cancelled_upgrade_remains_reserved_until_the_same_restart_finishes(supported):
    async def run():
        harness = Harness(supported[0], prepare=engine.prepare_launch)
        bridge = harness.engine.automation
        entered, finish = asyncio.Event(), asyncio.Event()

        async def restart(profile_id, expected, headless):
            entered.set()
            await finish.wait()
            await harness.engine.stop(profile_id)
            await expected.task
            await harness.engine.start(profile_id, headless=headless, main_world_eval=True)
            session = harness.engine.sessions[profile_id]
            await session.ready.wait()
            return session

        bridge.restart_profile = restart
        try:
            await bridge.dispatch('POST', PREFIX + '/profiles/a/start', {})
            caller = asyncio.create_task(bridge.dispatch('POST', PREFIX + '/profiles/a/start',
                {'mainWorldEval': True, 'restartIfNeeded': True}))
            await entered.wait()
            upgrading = bridge.restarts['a']
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            with pytest.raises(AutomationError) as caught:
                await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'a'})
            assert caught.value.code == 'profile_busy'
            finish.set()
            session = await upgrading
            assert session.main_world_eval is True and len(harness.contexts) == 2
        finally:
            finish.set()
            await harness.engine.shutdown()
    asyncio.run(run())
