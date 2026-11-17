"""Input ownership is independent of browser ownership and fenced at native dispatch."""

import asyncio
from types import SimpleNamespace

import pytest

from camoufox.studio.access import MemberStore
from camoufox.studio.engine import StudioError
from camoufox.studio.store import ProfileStore, ValidationError
from camoufox.studio.team.control import ControlError, RemoteRun
from camoufox.studio.team.service import TeamService
from camoufox.studio.team.native import validate_event


def test_long_native_text_preserves_unicode_and_rejects_oversized_bytes():
    text = '  https://example.com/?q=' + '中文😀' * 10000 + '\nsecond line  '
    validate_event({'kind': 'text', 'text': text}, 1920, 1080)
    with pytest.raises(StudioError):
        validate_event({'kind': 'text', 'text': '中' * 400000}, 1920, 1080)


@pytest.mark.asyncio
async def test_clipboard_requires_current_controller_and_rechecks_revocation(remote):
    run, (admin, operator, viewer), events, member = remote
    async def clipboard(source):
        assert source == 'primary'
        return '选中文字😀\n' * 1000
    run.native.clipboard = clipboard
    status = await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    message = {'kind': 'clipboard', 'generation': status['generation'], 'source': 'primary', 'request_id': 1}
    with pytest.raises(ControlError) as denied:
        await run.handle(admin, 'second', message)
    assert denied.value.code == 'control_required'
    with pytest.raises(ValidationError):
        await run.handle(viewer, 'reader', message)
    result = await run.handle(operator, 'first', message)
    assert result['clipboard'] == '选中文字😀\n' * 1000
    assert result['clipboard_request'] == 1
    async def revoked(source):
        run.service.store.revoke_profile(admin.resolve()['member']['id'], member['id'], run.profile_id)
        return 'must not leave the service'
    run.native.clipboard = revoked
    with pytest.raises(ValidationError):
        await run.handle(operator, 'first', message)


@pytest.mark.asyncio
async def test_clipboard_does_not_return_text_after_control_expiry(remote):
    run, (_, operator, _), _, _ = remote
    async def clipboard(source):
        run.deadline = 0
        return 'expired control text'
    run.native.clipboard = clipboard
    status = await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    with pytest.raises(ControlError) as rejected:
        await run.handle(operator, 'first', {'kind': 'clipboard', 'generation': status['generation'],
                                           'source': 'clipboard', 'request_id': 1})
    assert rejected.value.code == 'control_changed'


@pytest.fixture
def remote(tmp_path):
    store = ProfileStore(tmp_path)
    admin = store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'admin-password-1')
    operator = store.create_member(admin['id'], 'op', 'Operator', 'operator', 'member-password-1')
    viewer = store.create_member(admin['id'], 'reader', 'Reader', 'viewer', 'member-password-2')
    profile = store.save_profile({'name': 'Controlled environment'})
    store.grant_profile(admin['id'], operator['id'], profile['id'], ['view', 'watch', 'operate'])
    store.grant_profile(admin['id'], viewer['id'], profile['id'], ['view', 'watch'])
    accesses = [MemberStore(store, store.login(name, password)) for name, password in
                [('root', 'admin-password-1'), ('op', 'member-password-1'), ('reader', 'member-password-2')]]
    events = []

    class Native:
        width, height = 1920, 1080

        async def input(self, event):
            events.append(event)

        async def drain(self):
            events.append('drain')

    service = TeamService(tmp_path, browser_mode='headless')
    service.store = store
    from camoufox.studio.team.repository import TeamRepository
    service.repository = TeamRepository(store)
    from uuid import uuid4
    command, _ = service.repository.reserve(accesses[0].session_id, profile['id'], 'start', str(uuid4()),
                                            owner=service.owner, mode='headless')
    service.repository.update_run(command['run_id'], service.owner, 'running')
    native = Native()
    session = SimpleNamespace(remote=native, stop=asyncio.Event())
    service.engine = SimpleNamespace(sessions={profile['id']: session})
    return RemoteRun(service, profile['id'], command['run_id'], native), accesses, events, operator


@pytest.mark.asyncio
async def test_viewer_can_watch_but_cannot_claim_or_input(remote):
    run, (_, _, viewer), events, _ = remote
    await run.watch(viewer)
    with pytest.raises(ValidationError, match='access_denied'):
        await run.handle(viewer, 'reader', {'kind': 'claim', 'generation': 0, 'force': False})
    assert events == []


@pytest.mark.asyncio
async def test_handover_drains_before_grant_and_fences_old_input(remote):
    run, (admin, operator, _), events, _ = remote
    first = await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    await run.handle(operator, 'first', {'kind': 'input', 'generation': first['generation'], 'sequence': 1,
                                        'event': {'kind': 'key', 'code': 'ShiftLeft', 'down': True}})
    second = await run.handle(admin, 'second', {'kind': 'claim', 'generation': first['generation'], 'force': True})
    assert second['generation'] > first['generation']
    assert events == ['drain', {'kind': 'key', 'code': 'ShiftLeft', 'down': True}, 'drain', 'drain']
    with pytest.raises(ControlError) as error:
        await run.handle(operator, 'first', {'kind': 'input', 'generation': first['generation'], 'sequence': 2,
                                            'event': {'kind': 'key', 'code': 'KeyA', 'down': True}})
    assert error.value.code == 'control_changed'
    assert len(events) == 4


@pytest.mark.asyncio
async def test_busy_connection_cannot_steal_and_sequence_cannot_replay(remote):
    run, (admin, operator, _), events, _ = remote
    status = await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    with pytest.raises(ControlError) as busy:
        await run.handle(admin, 'second', {'kind': 'claim', 'generation': status['generation'], 'force': False})
    assert busy.value.code == 'control_busy'
    message = {'kind': 'input', 'generation': status['generation'], 'sequence': 1,
               'event': {'kind': 'wheel', 'up': True}}
    await run.handle(operator, 'first', message)
    with pytest.raises(ControlError) as replay:
        await run.handle(operator, 'first', message)
    assert replay.value.code == 'input_sequence'
    assert events == ['drain', message['event']]


@pytest.mark.asyncio
async def test_revocation_and_disconnect_release_inputs_not_browser(remote):
    run, (admin, operator, _), events, member = remote
    await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    run.service.store.revoke_profile(admin.resolve()['member']['id'], member['id'], run.profile_id)
    async with run.gate:
        await run.expire()
    assert run.controller is None and events == ['drain', 'drain']
    assert not run.service.engine.sessions[run.profile_id].stop.is_set()
    await run.handle(admin, 'second', {'kind': 'claim', 'generation': run.generation, 'force': False})
    await run.disconnect('first')
    assert run.controller[0] == 'second'
    await run.disconnect('second')
    assert run.controller is None


@pytest.mark.asyncio
@pytest.mark.parametrize('replacement', [False, True])
async def test_disconnect_after_browser_cleanup_does_not_fence_service(remote, replacement):
    run, (_, operator, _), events, _ = remote
    await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    session = run.service.engine.sessions.pop(run.profile_id)
    session.stop.set()
    if replacement:
        run.service.engine.sessions[run.profile_id] = SimpleNamespace(
            remote=object(), stop=asyncio.Event())

    async def closed_worker():
        raise StudioError('Native input worker is unavailable.')

    async def stop(profile_id):
        pytest.fail('Disconnecting an ended run must not stop another run.')

    run.native.drain = closed_worker
    run.service.engine.stop = stop
    await run.disconnect('first')
    assert run.controller is None and events == ['drain']
    assert not run.service.failed


@pytest.mark.asyncio
@pytest.mark.parametrize('race', [False, True])
@pytest.mark.parametrize('cleanup_failed', [False, True])
async def test_disconnect_waits_for_browser_cleanup(remote, race, cleanup_failed):
    run, (_, operator, _), events, _ = remote
    await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    session = run.service.engine.sessions[run.profile_id]
    entered, finish = asyncio.Event(), asyncio.Event()
    run.service.engine.recovery_required = False

    async def cleanup():
        entered.set()
        await finish.wait()
        run.service.engine.recovery_required = cleanup_failed
        run.service.engine.sessions.pop(run.profile_id)

    session.task = asyncio.create_task(cleanup())
    if not race:
        session.stop.set()

    async def closed_worker():
        assert race, 'A stopping run must await cleanup instead of using its input worker.'
        session.stop.set()
        raise StudioError('Native input worker is unavailable.')

    async def stop(profile_id):
        pytest.fail('Normal browser shutdown already owns resource cleanup.')

    run.native.drain = closed_worker
    run.service.engine.stop = stop
    disconnected = asyncio.create_task(run.disconnect('first'))
    try:
        await entered.wait()
        await asyncio.sleep(0)
        assert not disconnected.done()
    finally:
        finish.set()
        await asyncio.gather(session.task, disconnected, return_exceptions=True)
    await disconnected
    assert run.controller is None and events == ['drain']
    assert not run.service.failed
    assert run.service.engine.recovery_required is cleanup_failed


@pytest.mark.asyncio
async def test_handover_cannot_grant_control_after_browser_cleanup(remote):
    run, (admin, operator, _), _, _ = remote
    await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    session = run.service.engine.sessions[run.profile_id]

    async def cleanup():
        run.service.engine.sessions.pop(run.profile_id)

    async def closing_worker():
        session.stop.set()
        session.task = asyncio.create_task(cleanup())
        raise StudioError('Native input worker is unavailable.')

    async def stop(profile_id):
        pytest.fail('The closed browser must not be marked as a failed input release.')

    run.native.drain = closing_worker
    run.service.engine.stop = stop
    with pytest.raises(ControlError) as rejected:
        await run.handle(admin, 'second', {
            'kind': 'claim', 'generation': run.generation, 'force': True})
    assert rejected.value.code == 'run_conflict'
    assert run.controller is None and not run.service.failed


@pytest.mark.asyncio
async def test_handover_waits_for_inflight_native_input(remote):
    run, (admin, operator, _), events, _ = remote
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(event):
        entered.set()
        await release.wait()
        events.append('old-input-finished')

    run.native.input = slow
    await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    first = asyncio.create_task(run.handle(operator, 'first', {
        'kind': 'input', 'generation': run.generation, 'sequence': 1,
        'event': {'kind': 'key', 'code': 'KeyS', 'down': True}}))
    await entered.wait()
    transfer = asyncio.create_task(run.handle(admin, 'second', {
        'kind': 'claim', 'generation': run.generation, 'force': True}))
    release.set()
    await asyncio.gather(first, transfer)
    assert events == ['drain', 'old-input-finished', 'drain', 'drain']


@pytest.mark.asyncio
async def test_handover_cannot_grant_control_without_native_release_confirmation(remote):
    run, (admin, operator, _), _, _ = remote
    first = await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})

    async def failed_release():
        raise StudioError('Native input was not acknowledged.')

    async def stop(profile_id):
        run.service.engine.sessions[profile_id].stop.set()

    run.native.drain = failed_release
    run.service.engine.stop = stop
    with pytest.raises(StudioError, match='not acknowledged'):
        await run.handle(admin, 'second', {'kind': 'claim', 'generation': first['generation'], 'force': True})
    assert run.controller is None and run.generation > first['generation']
    assert run.service.failed and run.service.engine.sessions[run.profile_id].stop.is_set()
    with pytest.raises(ControlError) as rejected:
        await run.handle(admin, 'second', {'kind': 'claim', 'generation': run.generation, 'force': True})
    assert rejected.value.code == 'run_conflict'


@pytest.mark.asyncio
@pytest.mark.parametrize('event', [
    {'kind': 'key', 'code': 'Unidentified', 'down': True},
    {'kind': 'key', 'code': 'Lang1', 'down': False},
    {'kind': 'pointer', 'x': -1, 'y': 0, 'button': 1, 'down': True},
])
async def test_invalid_client_input_does_not_close_display_or_release_control(remote, event):
    run, (_, operator, _), events, _ = remote
    dispatch = run.native.input

    async def validated(value):
        validate_event(value, run.native.width, run.native.height)
        await dispatch(value)

    run.native.input = validated
    status = await run.handle(operator, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
    with pytest.raises(ControlError) as rejected:
        await run.handle(operator, 'first', {'kind': 'input', 'generation': status['generation'],
            'sequence': 1, 'event': event})
    assert rejected.value.code == 'invalid_input'
    assert run.controller[0] == 'first'
    assert run.generation == status['generation']
    assert events == ['drain']
    await run.handle(operator, 'first', {'kind': 'input', 'generation': status['generation'],
        'sequence': 2, 'event': {'kind': 'key', 'code': 'KeyS', 'down': True}})
    assert events[-1]['code'] == 'KeyS'
    assert not run.service.engine.sessions[run.profile_id].stop.is_set()
