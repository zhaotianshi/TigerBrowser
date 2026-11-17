"""The client sync engine mirrors a lease over real HTTP and returns only what changed."""

import hashlib
import io
import json
import socket
import threading
import time
import zipfile

import pytest
import uvicorn

from camoufox import multiversion
from camoufox.studio import store as store_module
from camoufox.studio.backup import profile_files
from camoufox.studio.client import sync
from camoufox.studio.client.sync import ClientStore, HttpTransport, SyncEngine, SyncError
from camoufox.studio.kernel_cache import KernelCache, host_platform
from camoufox.studio.kernels import kernel_id
from camoufox.studio.team.service import create_app
from test_studio_checkouts import device
from test_studio_team_service import engine_factory, workspace  # noqa: F401

KERNEL = 'official/152.0.4-beta.31'


class Secrets:
    def __init__(self, values, namespace):
        self.values, self.namespace = values, namespace

    def get(self, key):
        return self.values.get((self.namespace, key))

    def set(self, key, value):
        self.values[(self.namespace, key)] = value

    def delete(self, key):
        self.values.pop((self.namespace, key), None)


def cache_kernel(root):
    """A host package as the server's cache holds it after a verified download."""
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as archive:
        archive.writestr('camoufox-bin', 'kernel')
    payload, platform = data.getvalue(), host_platform()
    directory = KernelCache(root).root / 'official' / KERNEL.split('/', 1)[1]
    directory.mkdir(parents=True)
    (directory / f'{platform}.zip').write_bytes(payload)
    (directory / f'{platform}.json').write_text(json.dumps({
        'kernel_id': KERNEL, 'platform': platform, 'size': len(payload),
        'sha256': hashlib.sha256(payload).hexdigest(), 'version': '152.0.4', 'build': 'beta.31',
        'prerelease': True, 'asset_id': 1, 'asset_size': len(payload),
        'asset_updated_at': '2026-10-01T00:00:00Z', 'created_at': '2026-10-01T00:00:00Z'}))


@pytest.fixture
def server(workspace, tmp_path_factory, monkeypatch):
    store, admin, operator, viewer, first, second = workspace
    secrets = {}
    monkeypatch.setattr(store_module, 'KeyringSecretStore', lambda namespace: Secrets(secrets, namespace))
    store.secret_store = Secrets(secrets, store.secret_store.namespace)
    monkeypatch.setattr(multiversion, 'COMPAT_FLAG', tmp_path_factory.mktemp('compat') / 'flag')
    proxy = store.save_proxy({'name': 'Route', 'server': 'http://proxy.test:8080',
                              'username': 'member', 'password': 'route-secret'})
    store.save_profile({'kernel': KERNEL, 'proxy_id': proxy['id']}, first['id'])
    store.grant_profile(admin['id'], operator['id'], first['id'], ['view', 'operate', 'checkout'])
    directory = store.profile_dir(first['id'])
    (directory / 'storage').mkdir(parents=True)
    (directory / 'cookies.sqlite').write_bytes(b'server-cookies')
    (directory / 'storage' / 'old.bin').write_bytes(b'deleted-on-client')
    (directory / 'cache2').mkdir()
    (directory / 'cache2' / 'entry').write_bytes(b'regenerable')
    kernel_root = tmp_path_factory.mktemp('kernels')
    cache_kernel(kernel_root)
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    application = create_app(store.root, origin=origin, engine_factory=engine_factory,
                             kernel_root=kernel_root, staging_root=tmp_path_factory.mktemp('staging'))
    runner = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=port, log_level='warning'))
    thread = threading.Thread(target=runner.run)
    thread.start()
    deadline = time.monotonic() + 30
    while not runner.started:
        assert thread.is_alive() and time.monotonic() < deadline
        time.sleep(0.05)
    client = ClientStore(tmp_path_factory.mktemp('client'), secret_store=Secrets(secrets, 'client'))
    engine = SyncEngine(client, HttpTransport(origin))
    service = application.state.team

    def lease():
        created = service.checkouts.create(store.login('op', 'member-password-1')['session_id'],
                                           first['id'], device(platform=host_platform()))
        return created['checkout'], created['token']

    try:
        yield store, service, engine, client, first, lease, secrets
    finally:
        runner.should_exit = True
        thread.join(30)


def test_checkout_mirrors_profile_kernel_and_files_then_returns_only_changes(server, monkeypatch):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    assert engine.checkout(checkout, token)['state'] == 'ready'
    directory = client.profile_dir(first['id'])
    assert profile_files(directory) == profile_files(store.profile_dir(first['id']), skip={'cache2'})
    assert client.get_proxy(client.get_profile(first['id'])['proxy_id'], reveal_password=True)['password'] == 'route-secret'
    assert [kernel_id(item) for item in multiversion.list_installed()] == [KERNEL]
    engine.begin_local(first['id'])

    (directory / 'cookies.sqlite').write_bytes(b'client-cookies')
    (directory / 'places.sqlite').write_bytes(b'client-history')
    (directory / 'storage' / 'old.bin').unlink()
    uploads = []
    real_stream = sync.stream_files
    monkeypatch.setattr(sync, 'stream_files', lambda paths: (uploads.append([name for name, _ in paths]),
                                                             real_stream(paths))[1])
    with pytest.raises(SyncError, match='profile_running'):
        engine.return_profile(first['id'])
    engine.finish(first['id'])
    assert engine.return_profile(first['id']) == {'data_revision': 1}
    assert uploads == [['cookies.sqlite', 'places.sqlite']]
    assert profile_files(store.profile_dir(first['id'])) == profile_files(directory)
    assert store.get_profile(first['id'])['data_revision'] == 1
    # The lease token and proxy credentials leave this computer's keyring with the lease.
    assert engine.leases() == {} and not [key for key in secrets if key[0] == 'client']
    with pytest.raises(SyncError, match='not_checked_out'):
        engine.begin_local(first['id'])

    downloads = []
    monkeypatch.setattr(SyncEngine, '_download', lambda self, lease, directory, names: downloads.append(names))
    checkout, token = lease()
    assert engine.checkout(checkout, token)['data_revision'] == 1
    assert downloads == []


def test_interrupted_return_resumes_and_a_lost_commit_response_is_recognized(server, monkeypatch):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    engine.checkout(checkout, token)
    (client.profile_dir(first['id']) / 'cookies.sqlite').write_bytes(b'client-cookies')
    engine.finish(first['id'])
    real_call, failures = engine.transport.call, {'before': 1, 'after': 1}

    def flaky(method, path, token, **options):
        if path.endswith('/commit') and failures['before']:
            failures['before'] -= 1
            raise SyncError('server_unreachable')
        result = real_call(method, path, token, **options)
        if path.endswith('/commit') and failures['after']:
            failures['after'] -= 1
            raise SyncError('server_unreachable')
        return result

    monkeypatch.setattr(engine.transport, 'call', flaky)
    with pytest.raises(SyncError, match='server_unreachable'):
        engine.return_profile(first['id'])
    assert engine.leases()[first['id']]['state'] == 'pending_return'
    # A sent commit may already own this copy on the server, so it cannot change locally again.
    with pytest.raises(SyncError, match='return_in_progress'):
        engine.begin_local(first['id'])
    assert engine.resume() == {first['id']: {'error': 'server_unreachable'}}
    assert store.get_profile(first['id'])['data_revision'] == 1
    assert engine.resume() == {first['id']: {'data_revision': 1}}
    assert engine.leases() == {} and client.get_profile(first['id'])['data_revision'] == 1
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'client-cookies'


def test_forced_release_retires_the_local_copy_without_deleting_it(server):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    engine.checkout(checkout, token)
    (client.profile_dir(first['id']) / 'cookies.sqlite').write_bytes(b'unreturned')
    service.checkouts.release(store.login('root', 'admin-password-1')['session_id'], first['id'], checkout['id'])
    with pytest.raises(SyncError, match='checkout_revoked'):
        engine.heartbeat(first['id'])
    # The browser may still hold the files; they move only once it is no longer running.
    assert engine.resume(running={first['id']}) == {}
    assert (client.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'unreturned'
    with pytest.raises(SyncError, match='not_checked_out'):
        engine.begin_local(first['id'])
    assert engine.resume() == {first['id']: {'error': 'checkout_revoked'}}
    assert (client.root / 'stale' / checkout['id'] / 'cookies.sqlite').read_bytes() == b'unreturned'
    assert set(engine.stale()) == {checkout['id']} and engine.leases() == {}
    assert not [key for key in secrets if key[0] == 'client']
    with pytest.raises(SyncError, match='not_checked_out'):
        engine.begin_local(first['id'])
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'server-cookies'
    checkout, token = lease()
    assert engine.checkout(checkout, token)['state'] == 'ready'
    assert (client.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'server-cookies'


def test_a_copy_that_never_ran_can_be_cancelled_but_a_launched_one_must_return(server):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    engine.checkout(checkout, token)
    engine.cancel(first['id'])
    assert engine.leases() == {} and not [key for key in secrets if key[0] == 'client']
    assert service.repository.active_checkout(first['id']) is None
    checkout, token = lease()
    engine.checkout(checkout, token)
    engine.begin_local(first['id'])
    with pytest.raises(SyncError, match='return_required'):
        engine.cancel(first['id'])


def test_an_unreturned_copy_reopens_offline_and_is_never_overwritten(server):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    engine.checkout(checkout, token)
    engine.finish(first['id'])
    engine.begin_local(first['id'])
    assert engine.leases()[first['id']]['state'] == 'ready'
    engine.finish(first['id'])
    with pytest.raises(SyncError, match='pending_return'):
        engine.checkout({**checkout, 'id': '00000000-0000-4000-8000-000000000000'}, 'x' * 43)
    assert engine.leases()[first['id']]['checkout_id'] == checkout['id']


@pytest.mark.parametrize('state', ['ready', 'pending_return', 'committing'])
def test_redelivered_lease_preserves_local_data_and_return_barrier(server, state):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    engine.checkout(checkout, token)
    engine.begin_local(first['id'])
    directory = client.profile_dir(first['id'])
    (directory / 'cookies.sqlite').write_bytes(b'unreturned-cookies')
    (directory / 'local-only.sqlite').write_bytes(b'unreturned-history')
    if state != 'ready':
        engine.finish(first['id'])
    if state == 'committing':
        pending = engine.leases()[first['id']]
        pending['committing'] = True
        engine._save(pending)
    previous = engine.leases()[first['id']]

    assert engine.checkout(checkout, token) == previous
    assert (directory / 'cookies.sqlite').read_bytes() == b'unreturned-cookies'
    assert (directory / 'local-only.sqlite').read_bytes() == b'unreturned-history'
    if state == 'committing':
        with pytest.raises(SyncError, match='return_in_progress'):
            engine.begin_local(first['id'])
    else:
        with pytest.raises(SyncError, match='return_required'):
            engine.cancel(first['id'])


@pytest.mark.parametrize('close', ['return', 'cancel', 'revoke'])
def test_closing_one_lease_keeps_a_shared_proxy_secret_until_the_last_lease(server, close):
    store, service, engine, client, first, lease, secrets = server
    checkout, token = lease()
    engine.checkout(checkout, token)
    proxy_id = client.get_profile(first['id'])['proxy_id']
    second = store.save_profile({'name': 'Shared route', 'kernel': KERNEL, 'proxy_id': proxy_id})
    created = service.checkouts.create(store.login('root', 'admin-password-1')['session_id'],
                                       second['id'], device(platform=host_platform()))
    engine.checkout(created['checkout'], created['token'])

    if close == 'return':
        engine.finish(first['id'])
        engine.return_profile(first['id'])
    elif close == 'cancel':
        engine.cancel(first['id'])
    else:
        service.checkouts.release(store.login('root', 'admin-password-1')['session_id'], first['id'], checkout['id'])
        with pytest.raises(SyncError, match='checkout_revoked'):
            engine.heartbeat(first['id'])
        engine.resume()

    assert set(engine.leases()) == {second['id']}
    assert client.get_proxy(proxy_id, reveal_password=True)['password'] == 'route-secret'
    assert client.token(checkout['id']) is None
    engine.cancel(second['id'])
    assert engine.leases() == {} and not [key for key in secrets if key[0] == 'client']
