"""Check-out leases move one profile's site data exclusively to a member computer and back."""

import hashlib
import io
import json
import threading
import zipfile
from uuid import uuid4

import pytest
from starlette.testclient import TestClient

from camoufox.studio import backup, checkouts
from camoufox.studio.checkouts import CheckoutIncomplete, Checkouts
from camoufox.studio.store import ProfileStore, StoreError, ValidationError
from camoufox.studio.team.repository import TeamRepository
from camoufox.studio.team.service import create_app
from test_studio_team_service import ORIGIN, PREFIX, engine_factory, login, mutation, workspace  # noqa: F401


def device(**changes):
    return {'request_id': str(uuid4()), 'device_id': str(uuid4()), 'device_name': 'Laptop',
            'platform': 'win-x86_64', **changes}


@pytest.fixture
def team(workspace, tmp_path_factory):
    store, admin, operator, viewer, first, second = workspace
    store.grant_profile(admin['id'], operator['id'], first['id'], ['view', 'operate', 'checkout'])
    sessions = {name: store.login(name, password)['session_id'] for name, password in (
        ('root', 'admin-password-1'), ('op', 'member-password-1'), ('reader', 'member-password-2'))}
    directory = store.profile_dir(first['id'])
    (directory / 'storage').mkdir(parents=True)
    (directory / 'cookies.sqlite').write_bytes(b'server-cookies')
    (directory / 'storage' / 'old.bin').write_bytes(b'deleted-on-client')
    (directory / 'cache2').mkdir()
    (directory / 'cache2' / 'entry').write_bytes(b'regenerable')
    leases = Checkouts(store, tmp_path_factory.mktemp('staging'))
    return store, leases, sessions, first, second, admin, operator


def manifest(files):
    return {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}


def archive(files):
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_STORED) as output:
        for name, payload in files.items():
            output.writestr(name, payload)
    return data.getvalue()


def returned(leases, lease, token, changed, complete):
    path = leases.upload_path(lease['id'])
    path.write_bytes(archive(changed))
    leases.receive(lease['id'], token, path)
    path.unlink()
    return leases.commit(lease['id'], token, {'files': manifest(complete), 'data_revision': 0})


def test_checkout_requires_its_own_grant_and_role_cap(team):
    store, leases, sessions, first, second, admin, operator = team
    with pytest.raises(ValidationError, match='access_denied'):
        leases.create(sessions['op'], second['id'], device())
    with pytest.raises(ValidationError, match='access_denied'):
        leases.create(sessions['reader'], first['id'], device())
    with pytest.raises(ValidationError, match='role_permission_denied'):
        store.grant_profile(admin['id'], store.list_members()[2]['id'], first['id'], ['view', 'checkout'])
    created = leases.create(store.login('op', 'member-password-1')['session_id'], first['id'], device())
    assert created['checkout']['state'] == 'checked_out' and len(created['token']) == 43
    with store._connect() as connection:
        assert created['token'] not in json.dumps([dict(row) for row in connection.execute('SELECT * FROM team_checkouts')])


@pytest.mark.parametrize('historical_version', ['152.0.4', '152.0.4-beta.31'])
def test_historical_checkout_distributes_resolved_build_without_changing_identity(team, tmp_path, monkeypatch,
                                                                                historical_version):
    from camoufox import multiversion
    from camoufox.pkgman import Version
    from camoufox.studio import kernels
    from camoufox.studio.kernel_cache import KernelCache, host_platform
    from test_studio_client_sync import KERNEL, cache_kernel
    store, leases, sessions, first, *_ = team
    identity = {'browser_version': historical_version, 'config': {'navigator.platform': 'Win32'}}
    store.set_identity(first['id'], identity)
    original = store.get_profile(first['id'])
    cache_kernel(tmp_path / 'packages')
    leases.kernel_cache = KernelCache(tmp_path / 'packages')
    installed = [multiversion.InstalledVersion('official', Version('beta.31', '152.0.4'), tmp_path / 'browser')]
    monkeypatch.setattr(multiversion, 'list_installed', lambda: installed)
    monkeypatch.setattr(kernels, 'launch_path', lambda path: str(path / 'camoufox-bin'))
    created = leases.create(sessions['op'], first['id'], device(platform=host_platform()))
    listing = leases.manifest(created['checkout']['id'], created['token'])
    assert listing['kernel']['kernel_id'] == KERNEL
    assert listing['kernel']['platform'] == host_platform()
    assert listing['profile']['identity'] == identity and listing['profile']['kernel'] == ''
    path, record = leases.kernel_package(created['checkout']['id'], created['token'])
    assert path.is_file() and record == listing['kernel']
    assert store.get_profile(first['id']) == original
    installed.clear()
    with pytest.raises(ValidationError, match='kernel_not_cached'):
        leases.kernel_package(created['checkout']['id'], created['token'])
    installed.extend([
        multiversion.InstalledVersion('official', Version('beta.31', '152.0.4'), tmp_path / name)
        for name in ('revision-one', 'revision-two')])
    with pytest.raises(ValidationError, match='kernel_not_cached'):
        leases.kernel_package(created['checkout']['id'], created['token'])


def test_concurrent_checkout_and_remote_start_admit_exactly_one(team):
    store, leases, sessions, first, *_ = team
    repository = TeamRepository(store)
    for _ in range(10):
        barrier, results = threading.Barrier(2), []

        def checkout():
            barrier.wait()
            try:
                results.append(('checkout', leases.create(sessions['op'], first['id'], device())))
            except ValidationError as error:
                results.append(('checkout', str(error)))

        def start():
            barrier.wait()
            try:
                results.append(('start', repository.reserve(sessions['op'], first['id'], 'start', str(uuid4()),
                                                             owner='owner', mode='headless')))
            except ValidationError as error:
                results.append(('start', str(error)))

        threads = [threading.Thread(target=checkout), threading.Thread(target=start)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(isinstance(value, str) for _, value in results) == [False, True]
        assert {value for _, value in results if isinstance(value, str)} <= {'profile_busy', 'profile_checked_out'}
        with store._write() as (connection, _):
            connection.execute('DELETE FROM team_commands')
            connection.execute('DELETE FROM team_runs')
            connection.execute('DELETE FROM team_checkouts')


def test_every_gate_rejects_an_active_lease_except_configuration_copy(team, tmp_path_factory):
    store, leases, sessions, first, *_ = team
    target = tmp_path_factory.mktemp('snapshots') / 'snapshot.bin'
    leases.create(sessions['op'], first['id'], device())
    repository = TeamRepository(store)
    with pytest.raises(ValidationError, match='profile_checked_out'):
        repository.reserve(sessions['root'], first['id'], 'start', str(uuid4()), owner='owner', mode='headless')
    with pytest.raises(ValidationError, match='profile_checked_out'):
        repository.ensure_editable(first['id'])
    repository.ensure_editable(first['id'], configuration_only=True)
    assert store.clone_profile(first['id'])['name'].endswith('(copy)')
    with pytest.raises(StoreError):
        store.check_execution_ownership(first['id'])
    with pytest.raises(ValidationError, match='profile_checked_out'):
        store.delete_profile(first['id'])
    with pytest.raises(backup.BackupError, match='checkout_active'):
        backup.create_snapshot(store.root, target, bytes(16))
    with pytest.raises(ValidationError, match='profile_checked_out'):
        leases.create(sessions['root'], first['id'], device())


def test_active_run_blocks_checkout(team):
    store, leases, sessions, first, *_ = team
    TeamRepository(store).reserve(sessions['op'], first['id'], 'start', str(uuid4()), owner='owner', mode='headless')
    with pytest.raises(ValidationError, match='profile_busy'):
        leases.create(sessions['op'], first['id'], device())


def test_return_replaces_data_atomically_and_increments_data_revision(team):
    store, leases, sessions, first, *_ = team
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    listing = leases.manifest(lease['id'], token)
    assert set(listing['files']) == {'cookies.sqlite', 'storage/old.bin'}
    assert listing['profile']['data_revision'] == 0
    complete = {'cookies.sqlite': b'server-cookies', 'places.sqlite': b'client-history'}
    with pytest.raises(CheckoutIncomplete) as error:
        leases.commit(lease['id'], token, {'files': manifest(complete), 'data_revision': 0})
    assert error.value.missing == ['places.sqlite']
    assert returned(leases, lease, token, {'places.sqlite': b'client-history'}, complete) == {'data_revision': 1}
    directory = store.profile_dir(first['id'])
    assert backup.profile_files(directory) == manifest(complete)
    assert store.get_profile(first['id'])['data_revision'] == 1
    with pytest.raises(ValidationError, match='checkout_closed'):
        leases.heartbeat(lease['id'], token)
    assert list(leases.staging.iterdir()) == []
    TeamRepository(store).ensure_editable(first['id'])


def test_commit_rejects_unsafe_names_stale_revisions_and_wrong_tokens(team):
    store, leases, sessions, first, *_ = team
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    with pytest.raises(ValidationError, match='checkout_denied'):
        leases.heartbeat(lease['id'], 'x' * 43)
    for files in ({'../escape': b'x'}, {'a': b'1', 'A': b'2'}, {'cache2/entry': b'x'}, {'parent.lock': b''}):
        with pytest.raises(ValidationError, match='invalid_request'):
            leases.commit(lease['id'], token, {'files': manifest(files), 'data_revision': 0})
    with pytest.raises(ValidationError, match='checkout_conflict'):
        leases.commit(lease['id'], token, {'files': {}, 'data_revision': 7})
    path = leases.upload_path(lease['id'])
    path.write_bytes(archive({'../escape': b'x'}))
    with pytest.raises(ValidationError, match='invalid_archive_entry'):
        leases.receive(lease['id'], token, path)
    assert not (leases.staging.parent / 'escape').exists()


@pytest.mark.parametrize('step', ['before_record', 'after_record', 'after_swap'])
def test_interrupted_commit_leaves_complete_old_or_new_data(team, monkeypatch, step):
    store, leases, sessions, first, *_ = team
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    complete = {'cookies.sqlite': b'client-cookies'}
    real_apply, real_rename = Checkouts._apply, checkouts.os.rename

    class Crash(Exception):
        pass

    if step == 'before_record':
        monkeypatch.setattr(checkouts, 'profile_files', lambda *_: (_ for _ in ()).throw(Crash()))
    elif step == 'after_record':
        monkeypatch.setattr(Checkouts, '_apply', lambda *_: (_ for _ in ()).throw(Crash()))
    else:
        calls = []

        def rename(source, target):
            calls.append(source)
            if len(calls) == 2:
                raise Crash()
            return real_rename(source, target)
        monkeypatch.setattr(checkouts.os, 'rename', rename)
    with pytest.raises(Crash):
        returned(leases, lease, token, complete, complete)
    monkeypatch.undo()
    assert Checkouts._apply is real_apply
    with pytest.raises(StoreError):
        store.check_execution_ownership(first['id'])
    restarted = Checkouts(store, leases.staging)
    restarted.recover()
    directory = store.profile_dir(first['id'])
    if step == 'before_record':
        assert (directory / 'cookies.sqlite').read_bytes() == b'server-cookies'
        assert store.get_profile(first['id'])['data_revision'] == 0
        assert restarted.authorize(lease['id'], token)['state'] == 'returning'
    else:
        assert backup.profile_files(directory) == manifest(complete)
        assert store.get_profile(first['id'])['data_revision'] == 1
        assert list(leases.staging.iterdir()) == []


def test_force_release_rejects_the_old_client_and_keeps_server_data(team):
    store, leases, sessions, first, *_ = team
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    with pytest.raises(ValidationError, match='admin_required'):
        leases.release(sessions['op'], first['id'], lease['id'])
    assert leases.release(sessions['root'], first['id'], lease['id'])['state'] == 'revoked'
    for call in (lambda: leases.heartbeat(lease['id'], token),
                 lambda: leases.commit(lease['id'], token, {'files': {}, 'data_revision': 0})):
        with pytest.raises(ValidationError, match='checkout_revoked'):
            call()
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'server-cookies'
    TeamRepository(store).ensure_editable(first['id'])


def test_cancel_before_any_return_ends_the_lease_without_changing_data(team):
    store, leases, sessions, first, *_ = team
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    assert leases.cancel(lease['id'], token)['state'] == 'cancelled'
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'server-cookies'
    assert store.get_profile(first['id'])['data_revision'] == 0
    with pytest.raises(ValidationError, match='checkout_closed'):
        leases.heartbeat(lease['id'], token)
    TeamRepository(store).ensure_editable(first['id'])
    created = leases.create(sessions['op'], first['id'], device())
    path = leases.upload_path(created['checkout']['id'])
    path.write_bytes(archive({'cookies.sqlite': b'client'}))
    leases.receive(created['checkout']['id'], created['token'], path)
    # Once data is on its way back, only a commit or a forced release can end the lease.
    with pytest.raises(ValidationError, match='checkout_closed'):
        leases.cancel(created['checkout']['id'], created['token'])


def test_withdrawn_grant_requests_return_and_still_accepts_the_data(team):
    store, leases, sessions, first, second, admin, operator = team
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    assert leases.heartbeat(lease['id'], token)['release_requested'] is False
    store.grant_profile(admin['id'], operator['id'], first['id'], ['view', 'operate'])
    assert leases.heartbeat(lease['id'], token)['release_requested'] is True
    with pytest.raises(ValidationError, match='access_denied'):
        leases.manifest(lease['id'], token)
    complete = {'cookies.sqlite': b'client-cookies'}
    assert returned(leases, lease, token, complete, complete) == {'data_revision': 1}


def test_identity_claims_use_the_server_ledger(team):
    store, leases, sessions, first, second, *_ = team
    store.save_profile({'kernel': 'official/152.0.4-beta.31'}, first['id'])
    store.save_profile({'kernel': 'official/152.0.4-beta.31'}, second['id'])
    identity = {'kernel': 'official/152.0.4-beta.31', 'browser_version': '152.0.4',
                'config': {'navigator.platform': 'Win32'}}
    assert store.claim_identity(second['id'], identity)
    created = leases.create(sessions['op'], first['id'], device())
    lease, token = created['checkout'], created['token']
    assert leases.claim_identity(lease['id'], token, identity) is False
    with pytest.raises(ValidationError, match='invalid_kernel'):
        leases.claim_identity(lease['id'], token, {**identity, 'kernel': 'official/1-beta.1'})
    fresh = {**identity, 'config': {'navigator.platform': 'MacIntel'}}
    assert leases.claim_identity(lease['id'], token, fresh) is True
    with pytest.raises(ValidationError, match='identity_present'):
        leases.claim_identity(lease['id'], token, fresh)


def test_http_round_trip_streams_files_and_marks_the_listing(team):
    store, leases, sessions, first, second, admin, operator = team
    store.grant_profile(admin['id'], operator['id'], first['id'], ['view', 'watch', 'operate', 'checkout'])
    application = create_app(store.root, origin=ORIGIN, engine_factory=engine_factory, staging_root=leases.staging)
    with TestClient(application, base_url=ORIGIN) as client:
        application.state.team.mode = 'remote'
        csrf = login(client, 'op', 'member-password-1')
        response = mutation(client, csrf, '/profiles/' + first['id'] + '/checkout', device())
        assert response.status_code == 201, response.text
        lease, token = response.json()['checkout'], response.json()['token']
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + token}
        listed = client.get(PREFIX + '/profiles').json()['profiles']
        assert next(item for item in listed if item['id'] == first['id'])['checked_out']['mine'] is True
        assert client.get(PREFIX + '/profiles/' + first['id'] + '/remote').json()['reason'] == 'checked_out'
        assert client.get(PREFIX + '/checkouts/' + lease['id']).status_code == 401
        listing = client.get(PREFIX + '/checkouts/' + lease['id'], headers=headers).json()
        names = sorted(listing['files'])
        download = client.post(PREFIX + '/checkouts/' + lease['id'] + '/files', headers=headers, json={'names': names})
        with zipfile.ZipFile(io.BytesIO(download.content)) as received:
            assert {name: received.read(name) for name in received.namelist()} == {
                'cookies.sqlite': b'server-cookies', 'storage/old.bin': b'deleted-on-client'}
        complete = {'cookies.sqlite': b'client-cookies'}
        upload = client.post(PREFIX + '/checkouts/' + lease['id'] + '/return',
                             headers={**headers, 'Content-Type': 'application/zip'}, content=archive(complete))
        assert upload.status_code == 200, upload.text
        commit = client.post(PREFIX + '/checkouts/' + lease['id'] + '/commit', headers=headers,
                             json={'files': manifest(complete), 'data_revision': 0})
        assert commit.json() == {'data_revision': 1}
        listed = client.get(PREFIX + '/profiles').json()['profiles']
        assert next(item for item in listed if item['id'] == first['id'])['checked_out'] is None
