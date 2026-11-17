"""Local runs sync a lease before the browser starts and return it after the browser stops."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from camoufox.studio.client.local import LocalClient
from camoufox.studio.client.sync import SyncError
from camoufox.studio.engine import StudioError
from camoufox.studio.workspace import WorkspaceLockError
from test_studio_client_sync import server  # noqa: F401
from test_studio_team_service import engine_factory, workspace  # noqa: F401


async def until(condition, timeout=30):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError('condition not reached')


@pytest.fixture
def local(server, tmp_path_factory):
    store, service, engine, _, first, lease, secrets = server
    base = tmp_path_factory.mktemp('client-base')
    events = []
    client = LocalClient(base, engine.transport.origin, service.workspace_id,
                         lambda profile_id, state: events.append((profile_id, state)), engine_factory=engine_factory)
    return store, service, client, first, lease, base, events


def test_launch_syncs_then_stop_returns_the_changed_data(local):
    store, service, client, first, lease, base, events = local
    checkout, token = lease()

    async def scenario():
        await client.launch(first['id'], checkout, token)
        await until(lambda: client.status(first['id'])['browser'] == 'running')
        assert client.status(first['id'])['lease'] == 'ready'
        with pytest.raises(StudioError):
            await client.launch(first['id'])
        (client.data_folder(first['id']) / 'cookies.sqlite').write_bytes(b'local-run')
        await client.stop(first['id'])
        await until(lambda: client.status(first['id'])['lease'] is None)
        await client.shutdown()

    asyncio.run(scenario())
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'local-run'
    assert store.get_profile(first['id'])['data_revision'] == 1
    assert service.repository.active_checkout(first['id']) is None
    assert not (base / 'servers' / service.workspace_id / 'workspace.lock').exists()


def test_a_forced_release_stops_the_local_browser_and_retires_its_copy(local, monkeypatch):
    store, service, client, first, lease, base, events = local
    monkeypatch.setattr('camoufox.studio.client.local.HEARTBEAT', 0.05)
    checkout, token = lease()

    async def scenario():
        await client.launch(first['id'], checkout, token)
        await until(lambda: client.status(first['id'])['browser'] == 'running')
        (client.data_folder(first['id']) / 'cookies.sqlite').write_bytes(b'unreturned')
        service.checkouts.release(store.login('root', 'admin-password-1')['session_id'], first['id'], checkout['id'])
        heartbeat = asyncio.create_task(client.run())
        await until(lambda: client.status(first['id']) == {
            'browser': 'stopped', 'error': None, 'lease': None, 'sync_error': 'checkout_revoked',
            'recovery_required': False})
        heartbeat.cancel()
        await client.shutdown()

    asyncio.run(scenario())
    assert [copy['checkout_id'] for copy in client.stale_copies()] == [checkout['id']]
    assert (client.store.root / 'stale' / checkout['id'] / 'cookies.sqlite').read_bytes() == b'unreturned'
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'server-cookies'


def test_one_process_per_cache_and_one_server_per_cache(local, server):
    store, service, client, first, lease, base, events = local
    with pytest.raises(WorkspaceLockError):
        LocalClient(base, client.sync.transport.origin, service.workspace_id, lambda *_: None)
    device = client.device()
    assert device['device_name'] and device == client.device()
    asyncio.run(client.shutdown())
    reopened = LocalClient(base, client.sync.transport.origin, service.workspace_id, lambda *_: None)
    assert reopened.device() == device
    asyncio.run(reopened.shutdown())
    with pytest.raises(SyncError, match='workspace_origin_mismatch'):
        LocalClient(base, 'https://other.test', service.workspace_id, lambda *_: None)


def test_a_profile_not_checked_out_here_cannot_start_or_open_its_folder(local):
    store, service, client, first, lease, base, events = local

    async def scenario():
        with pytest.raises(SyncError, match='not_checked_out'):
            await client.launch(first['id'])
        await client.shutdown()

    asyncio.run(scenario())
    assert client.status(first['id'])['sync_error'] == 'not_checked_out'
    with pytest.raises(SyncError, match='not_checked_out'):
        client.data_folder(first['id'])
    with pytest.raises(SyncError, match='invalid_request'):
        client.save_local_settings({'executable_path': '/tmp/browser'})
    assert client.save_local_settings({'max_running': 3}) == {
        'automation_enabled': False, 'max_running': 3}


@pytest.mark.parametrize('operation', ['checkout', 'cancel'])
@pytest.mark.parametrize('failure', [None, 'before', 'after'])
@pytest.mark.parametrize('cancel_caller', [False, True])
def test_shutdown_owns_foreground_sync_until_it_finishes(local, monkeypatch, operation, failure, cancel_caller):
    store, service, client, first, lease, base, events = local
    checkout, token = lease()
    if operation == 'cancel':
        client.sync.checkout(checkout, token)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = getattr(client.sync, operation)
    unlocked_writes = []

    def delayed(*args):
        entered.set()
        assert release.wait(30), 'foreground synchronization was not released'
        try:
            unlocked_writes.append(not client.lock.path.exists())
            if failure == 'before':
                raise SyncError('server_unreachable')
            result = original(*args)
            unlocked_writes.append(not client.lock.path.exists())
            if failure == 'after':
                raise SyncError('server_unreachable')
            return result
        finally:
            finished.set()

    monkeypatch.setattr(client.sync, operation, delayed)

    async def scenario():
        action = asyncio.create_task(client.launch(first['id'], checkout, token) if operation == 'checkout'
                                     else client.cancel(first['id']))
        shutdown = None
        try:
            await until(entered.is_set)
            if cancel_caller:
                action.cancel()
                await asyncio.gather(action, return_exceptions=True)
            shutdown = asyncio.create_task(client.shutdown())
            await until(lambda: getattr(client, '_closing', False) or shutdown.done())
            assert not shutdown.done() and client.lock.path.exists()
            with pytest.raises(WorkspaceLockError):
                LocalClient(base, client.origin, service.workspace_id, lambda *_: None)
            for call, args in ((client.launch, (first['id'],)), (client.cancel, (first['id'],)),
                               (client.configure_automation, ('synthetic-cookie',)),
                               (client._automation_start, (first['id'], True)),
                               (client._automation_create, ({'name': 'Rejected during shutdown'},))):
                with pytest.raises(StudioError):
                    await call(*args)
            release.set()
            results = await asyncio.gather(action, return_exceptions=True)
            if not cancel_caller:
                if failure:
                    assert isinstance(results[0], SyncError)
                else:
                    assert results == [None]
            await shutdown
            assert finished.is_set() and not client._tasks
        finally:
            release.set()
            await asyncio.gather(action, return_exceptions=True)
            if shutdown is None:
                shutdown = asyncio.create_task(client.shutdown())
            await asyncio.gather(shutdown, return_exceptions=True)

    asyncio.run(scenario())
    assert unlocked_writes and not any(unlocked_writes)
    assert not client.lock.path.exists()


def test_shutdown_finishes_an_accepted_automation_checkout(local):
    store, service, client, first, lease, base, events = local
    entered, release = threading.Event(), threading.Event()

    def checkout(profile_id, device):
        entered.set()
        assert release.wait(30), 'automation checkout was not released'
        created, token = lease()
        return {'checkout': created, 'token': token}

    client.engine.automation.access = SimpleNamespace(checkout=checkout)

    async def scenario():
        action = asyncio.create_task(client._automation_start(first['id'], True))
        shutdown = None
        try:
            await until(entered.is_set)
            shutdown = asyncio.create_task(client.shutdown())
            await until(lambda: getattr(client, '_closing', False) or shutdown.done())
            assert not shutdown.done() and client.lock.path.exists()
            release.set()
            await action
            await shutdown
        finally:
            release.set()
            await asyncio.gather(action, return_exceptions=True)
            if shutdown is None:
                shutdown = asyncio.create_task(client.shutdown())
            await asyncio.gather(shutdown, return_exceptions=True)

    asyncio.run(scenario())
    assert client.sync.leases() == {}
    assert service.repository.active_checkout(first['id']) is None
    assert not client.lock.path.exists()


def test_cancelled_shutdown_waiter_does_not_cancel_or_duplicate_cleanup(local, monkeypatch):
    store, service, client, first, lease, base, events = local
    entered, release = threading.Event(), threading.Event()
    resume = client.sync.resume
    calls = []

    def delayed_resume():
        calls.append('resume')
        entered.set()
        assert release.wait(30), 'shutdown resume was not released'
        assert client.lock.path.exists()
        return resume()

    monkeypatch.setattr(client.sync, 'resume', delayed_resume)

    async def scenario():
        first_shutdown = asyncio.create_task(client.shutdown())
        second_shutdown = None
        try:
            await until(entered.is_set)
            first_shutdown.cancel()
            await asyncio.gather(first_shutdown, return_exceptions=True)
            second_shutdown = asyncio.create_task(client.shutdown())
            await asyncio.sleep(0)
            assert not second_shutdown.done() and client.lock.path.exists()
            release.set()
            await second_shutdown
            await client.shutdown()
        finally:
            release.set()
            await asyncio.gather(first_shutdown, return_exceptions=True)
            if second_shutdown is not None:
                await asyncio.gather(second_shutdown, return_exceptions=True)

    asyncio.run(scenario())
    assert calls == ['resume']
    assert not client.lock.path.exists()


def test_heartbeat_stops_scheduling_work_when_shutdown_begins(local, monkeypatch):
    store, service, client, first, lease, base, events = local
    attempts = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed_leases():
            entered.set()
            await release.wait()
            return {first['id']: {'state': 'syncing'}}

        async def resume(profile_id):
            attempts.append(profile_id)

        monkeypatch.setattr(client, '_leases', delayed_leases)
        monkeypatch.setattr(client, '_resume', resume)
        heartbeat = asyncio.create_task(client.run())
        try:
            await entered.wait()
            await client.shutdown()
            release.set()
            await until(lambda: heartbeat.done() or attempts)
            assert not attempts and heartbeat.done()
        finally:
            release.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize('source', ['stop', 'heartbeat'])
def test_shutdown_waits_for_an_inflight_return_before_releasing_the_cache(local, monkeypatch, source):
    store, service, client, first, lease, base, events = local
    checkout, token = lease()
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    return_profile = client.sync.return_profile
    calls = []

    def delayed_return(profile_id):
        calls.append(profile_id)
        entered.set()
        assert release.wait(30), 'return was not released'
        try:
            return return_profile(profile_id)
        finally:
            finished.set()

    monkeypatch.setattr(client.sync, 'return_profile', delayed_return)

    async def scenario():
        heartbeat = shutdown = None
        try:
            if source == 'stop':
                await client.launch(first['id'], checkout, token)
                await until(lambda: client.status(first['id'])['browser'] == 'running')
                (client.data_folder(first['id']) / 'cookies.sqlite').write_bytes(b'shutdown-return')
                await client.stop(first['id'])
            else:
                client.sync.checkout(checkout, token)
                (client.data_folder(first['id']) / 'cookies.sqlite').write_bytes(b'shutdown-return')
                client.sync.finish(first['id'])
                heartbeat = asyncio.create_task(client.run())
            await until(entered.is_set)
            assert client._tasks, 'inflight return must remain owned during shutdown'
            return_task = next(iter(client._tasks))
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            shutdown = asyncio.create_task(client.shutdown())
            await until(lambda: client.engine.closing)
            assert not return_task.cancelled()
            assert not shutdown.done() and client.lock.path.exists()
            release.set()
            await shutdown
            assert finished.is_set() and not client._tasks
        finally:
            release.set()
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
            if shutdown is None:
                shutdown = asyncio.create_task(client.shutdown())
            await asyncio.gather(shutdown, return_exceptions=True)

    asyncio.run(scenario())
    assert calls == [first['id']]
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'shutdown-return'
    assert service.repository.active_checkout(first['id']) is None
    assert not client.lock.path.exists()


def test_resume_keeps_revoked_data_in_place_when_browser_cleanup_is_uncertain(local):
    store, service, client, first, lease, base, events = local
    checkout, token = lease()
    client.sync.checkout(checkout, token)
    directory = client.data_folder(first['id'])
    (directory / 'cookies.sqlite').write_bytes(b'possibly-open')
    service.checkouts.release(store.login('root', 'admin-password-1')['session_id'], first['id'], checkout['id'])
    with pytest.raises(SyncError, match='checkout_revoked'):
        client.sync.heartbeat(first['id'])
    client.engine.recovery_required = True
    try:
        asyncio.run(client._resume(first['id']))
        assert (directory / 'cookies.sqlite').read_bytes() == b'possibly-open'
        assert client.sync.leases()[first['id']]['state'] == 'revoked'
        assert client.stale_copies() == []
        assert client.lock.path.exists()
    finally:
        # This fixture never started a browser; release only the test-owned lock.
        client.lock.release()


@pytest.mark.parametrize('completion', ['during_shutdown', 'before_shutdown'])
def test_shutdown_waits_for_a_failing_heartbeat_and_still_returns_the_profile(local, monkeypatch, completion):
    store, service, client, first, lease, base, events = local
    checkout, token = lease()
    entered, release = threading.Event(), threading.Event()

    def delayed_heartbeat(profile_id):
        entered.set()
        assert release.wait(30), 'heartbeat was not released'
        raise SyncError('server_unreachable')

    monkeypatch.setattr(client.sync, 'heartbeat', delayed_heartbeat)

    async def scenario():
        unhandled = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context['message']))
        await client.launch(first['id'], checkout, token)
        await until(lambda: client.status(first['id'])['browser'] == 'running')
        heartbeat = asyncio.create_task(client.run())
        shutdown = None
        try:
            await until(entered.is_set)
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if completion == 'before_shutdown':
                release.set()
                await until(lambda: not client._tasks)
            shutdown = asyncio.create_task(client.shutdown())
            if completion == 'during_shutdown':
                await until(lambda: client.engine.closing)
                assert not shutdown.done() and client.lock.path.exists()
            release.set()
            await shutdown
            assert not client._tasks
            assert not unhandled
        finally:
            release.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if shutdown is None:
                shutdown = asyncio.create_task(client.shutdown())
            await asyncio.gather(shutdown, return_exceptions=True)
            loop.set_exception_handler(None)

    asyncio.run(scenario())
    assert service.repository.active_checkout(first['id']) is None
    assert not client.lock.path.exists()
