"""Idle, owned browser recycling never discards another task or manual tab."""
import asyncio
from types import SimpleNamespace

import pytest

from camoufox.studio.automation import AutomationError, PREFIX
from camoufox.studio.engine import Session
from test_studio_automation import harness, Context, Page


async def leased(tmp_path):
    bridge, context, _ = harness(tmp_path)
    starts = []
    async def recycle(profile_id, expected):
        starts.append(expected)
        replacement = Session(context=Context(), headless=expected.headless,
                              main_world_eval=expected.main_world_eval)
        bridge.engine.sessions[profile_id] = replacement
        return replacement
    bridge.recycle_profile = recycle
    lease = (await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'}))['leaseId']
    return bridge, context, lease, starts


@pytest.mark.asyncio
async def test_resources_are_versioned_owned_and_report_unavailable_memory_as_null(tmp_path):
    bridge, _, lease, _ = await leased(tmp_path)
    response = await bridge.dispatch('GET', PREFIX + '/leases/' + lease + '/resources', {})
    assert response['version'] == 1 and response['sessionId']
    assert response['rssBytes'] is None and response['processCount'] is None
    assert response['idle'] is True
    bridge.access.api_key_id = 'another-key'
    with pytest.raises(AutomationError, match='lease'):
        await bridge.dispatch('GET', PREFIX + '/leases/' + lease + '/resources', {})


@pytest.mark.asyncio
async def test_recycle_fences_exact_session_and_releases_only_owned_lease(tmp_path):
    bridge, context, lease, starts = await leased(tmp_path)
    path = PREFIX + '/leases/' + lease
    before = await bridge.dispatch('GET', path + '/resources', {})
    with pytest.raises(AutomationError) as wrong:
        await bridge.dispatch('POST', path + '/recycle', {'sessionId': 'stale'})
    assert wrong.value.code == 'session_changed' and not starts
    response = await bridge.dispatch('POST', path + '/recycle', {'sessionId': before['sessionId']})
    assert response['recycled'] is True and response['previousSessionId'] == before['sessionId']
    assert response['sessionId'] != before['sessionId']
    assert len(starts) == 1 and not bridge.leases
    assert not context.pages[0].closed
    with pytest.raises(AutomationError):
        await bridge.dispatch('POST', path + '/recycle', {'sessionId': before['sessionId']})
    assert len(starts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['owned_page', 'manual_page', 'extra_blank', 'handle', 'task', 'cleanup', 'other_lease'])
async def test_recycle_refuses_every_busy_or_unknown_page_boundary(tmp_path, busy):
    bridge, context, lease, starts = await leased(tmp_path)
    path = PREFIX + '/leases/' + lease
    before = await bridge.dispatch('GET', path + '/resources', {})
    current = bridge.leases[lease]
    if busy == 'owned_page':
        current.pages['page'] = Page(context)
    elif busy == 'manual_page':
        context.pages[0].url = 'https://example.com/work'
    elif busy == 'extra_blank':
        context.pages.append(Page(context))
    elif busy == 'handle':
        current.handles['handle'] = object()
    elif busy == 'task':
        current.tasks.add(object())
    elif busy == 'cleanup':
        current.cleanup_tasks.add(object())
    else:
        bridge.leases['other'] = SimpleNamespace(profile_id='profile')
    assert (await bridge.dispatch('GET', path + '/resources', {}))['idle'] is False
    with pytest.raises(AutomationError) as failure:
        await bridge.dispatch('POST', path + '/recycle', {'sessionId': before['sessionId']})
    assert failure.value.code == 'profile_busy' and not starts


@pytest.mark.asyncio
async def test_cancelled_recycle_keeps_one_task_and_rejects_reacquisition(tmp_path):
    bridge, _, lease, _ = await leased(tmp_path)
    path = PREFIX + '/leases/' + lease
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []
    async def recycle(profile_id, expected):
        calls.append(expected)
        entered.set()
        await finish.wait()
        bridge.engine.sessions[profile_id] = Session(context=Context())
        return bridge.engine.sessions[profile_id]
    bridge.recycle_profile = recycle
    before = await bridge.dispatch('GET', path + '/resources', {})
    task = asyncio.create_task(bridge.dispatch('POST', path + '/recycle', {'sessionId': before['sessionId']}))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(AutomationError) as conflict:
        await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
    assert conflict.value.code == 'profile_busy'
    assert len(calls) == 1
    finish.set()
    await asyncio.gather(*bridge.recycles.values())
    assert len(calls) == 1 and not bridge.leases


@pytest.mark.asyncio
async def test_retained_recycle_lease_does_not_expire_and_definite_rejection_can_resume(tmp_path):
    bridge, context, now = harness(tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def reject_late(profile_id, expected):
        entered.set()
        await finish.wait()
        raise AutomationError('profile_busy', 'Manual work started.', 409)
    bridge.recycle_profile = reject_late
    lease = (await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'}))['leaseId']
    path = PREFIX + '/leases/' + lease
    sample = await bridge.dispatch('GET', path + '/resources', {})
    task = asyncio.create_task(bridge.dispatch('POST', path + '/recycle', {'sessionId': sample['sessionId']}))
    await entered.wait()
    now[0] += 200
    await bridge.expire()
    assert lease in bridge.leases
    with pytest.raises(AutomationError) as busy:
        await bridge.dispatch('GET', path + '/resources', {})
    assert busy.value.code == 'profile_busy' and lease in bridge.leases
    finish.set()
    with pytest.raises(AutomationError) as failure:
        await task
    assert failure.value.code == 'profile_busy'
    await asyncio.sleep(0)
    assert (await bridge.dispatch('GET', path + '/resources', {}))['sessionId'] == sample['sessionId']
    assert not context.pages[0].closed


@pytest.mark.asyncio
async def test_uncertain_recycle_is_not_repeated_on_the_same_session(tmp_path):
    bridge, _, lease, _ = await leased(tmp_path)
    calls = []
    async def uncertain(profile_id, expected):
        calls.append(expected)
        raise RuntimeError('uncertain stop result')
    bridge.recycle_profile = uncertain
    path = PREFIX + '/leases/' + lease
    sample = await bridge.dispatch('GET', path + '/resources', {})
    with pytest.raises(RuntimeError):
        await bridge.dispatch('POST', path + '/recycle', {'sessionId': sample['sessionId']})
    await asyncio.sleep(0)
    assert (await bridge.dispatch('GET', path + '/resources', {}))['idle'] is False
    with pytest.raises(AutomationError) as failure:
        await bridge.dispatch('POST', path + '/recycle', {'sessionId': sample['sessionId']})
    assert failure.value.code == 'profile_recycle_unconfirmed' and len(calls) == 1


def test_memory_sampling_is_exact_owned_process_tree_and_never_returns_args(tmp_path, monkeypatch):
    import psutil
    from camoufox.studio.resources import profile_memory
    path = tmp_path / 'profile'
    class Process:
        def __init__(self, name, args, rss, children=()):
            self.title, self.args, self.rss, self.descendants = name, args, rss, children
        def name(self): return self.title
        def cmdline(self): return self.args
        def memory_info(self): return SimpleNamespace(rss=self.rss)
        def children(self, recursive=False): return self.descendants
        def create_time(self): return 10
        def is_running(self): return True
    child = Process('Web Content', ['private-url'], 32)
    browser = Process('camoufox-bin', ['camoufox-bin', '-profile', str(path)], 64, [child])
    unrelated = Process('camoufox-bin', ['camoufox-bin', '-profile', str(tmp_path / 'another')], 100)
    parent = Process('python', [], 1, [browser, child, unrelated])
    monkeypatch.setattr(psutil, 'Process', lambda: parent)
    store = SimpleNamespace(profile_dir=lambda _: path)
    assert profile_memory(store, 'profile') == {'rssBytes': 96, 'processCount': 2}
    parent.descendants = [unrelated]
    assert profile_memory(store, 'profile') == {'rssBytes': None, 'processCount': None}
    parent.descendants = [browser, browser]
    assert profile_memory(store, 'profile') == {'rssBytes': None, 'processCount': None}


@pytest.mark.asyncio
@pytest.mark.parametrize('leased_run', [False, True])
async def test_optional_main_world_restart_contract_remains_explicit_and_lease_fenced(tmp_path, leased_run):
    bridge, _, _ = harness(tmp_path)
    bridge.engine.sessions['profile'].main_world_eval = False
    restarted = []
    async def restart(profile_id, expected, headless):
        restarted.append(expected)
        session = Session(context=Context(), headless=headless, main_world_eval=True)
        bridge.engine.sessions[profile_id] = session
        return session
    bridge.restart_profile = restart
    listing = await bridge.dispatch('GET', PREFIX + '/profiles', {})
    assert listing['capabilities']['mainWorldRestart'] is True
    if leased_run:
        await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
        with pytest.raises(AutomationError) as failure:
            await bridge.dispatch('POST', PREFIX + '/profiles/profile/start',
                                  {'mainWorldEval': True, 'restartIfNeeded': True})
        assert failure.value.code == 'profile_leased' and not restarted
    else:
        response = await bridge.dispatch('POST', PREFIX + '/profiles/profile/start',
                                        {'mainWorldEval': True, 'restartIfNeeded': True})
        assert response['profile']['mainWorldEval'] is True and len(restarted) == 1
