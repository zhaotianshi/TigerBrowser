"""Recovery requires a stopped kernel boundary, not stale PIDs or expired member leases."""

import json
from uuid import uuid4

import pytest

from camoufox.studio import recovery
from camoufox.studio.store import ProfileStore, StoreError, ValidationError
from camoufox.studio.workspace import WorkspaceLock


@pytest.fixture
def crashed(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path)
    admin = store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'synthetic-admin-password')
    profile = store.save_profile({'name': 'Persistent'})
    store.set_identity(profile['id'], {'config': {'navigator.platform': 'Linux x86_64'}})
    owner = str(uuid4())
    boundary = {'kind': 'fixture', 'host_id': str(uuid4())}
    lock = WorkspaceLock(tmp_path, boundary=boundary, execution_owner=owner)
    lock.acquire()
    from camoufox.studio.team.repository import TeamRepository
    repository = TeamRepository(store)
    session = store.login('root', 'synthetic-admin-password')
    cookie = repository.issue_cookie(session['session_id'])
    command, _ = repository.reserve(session['session_id'], profile['id'], 'start', str(uuid4()),
                                    owner=owner, mode='headless')
    monkeypatch.setattr(recovery, 'stopped', lambda record: True)
    return store, lock, profile, repository, command, cookie


def test_inspection_is_read_only_and_application_revokes_old_capabilities(crashed):
    store, lock, profile, repository, command, cookie = crashed
    identity = store.get_profile(profile['id'])['identity']
    record = (lock.path / 'owner.json').read_bytes()
    assert recovery.inspect(store.root)['recoverable'] is True
    assert (lock.path / 'owner.json').read_bytes() == record
    assert repository.latest_run(profile['id'])['state'] == 'starting'
    result = recovery.apply(store.root)
    assert result['recovered_runs'] == 1
    assert repository.latest_run(profile['id'])['state'] == 'stopped'
    assert repository.command(command['id'])['error'] == 'result_unknown'
    assert repository.session_id(cookie) is None
    assert store.get_profile(profile['id'])['identity'] == identity
    assert not lock.path.exists()
    receipt = store.root / 'recovery' / result['receipt']
    assert (receipt / 'owner.json').read_bytes() == record
    replacement = WorkspaceLock(store.root)
    replacement.acquire()
    replacement.release()


def test_live_or_unprovable_boundary_cannot_modify_the_workspace(crashed, monkeypatch):
    store, lock, profile, repository, _, _ = crashed
    monkeypatch.setattr(recovery, 'stopped', lambda record: False)
    assert recovery.inspect(store.root)['recoverable'] is False
    with pytest.raises(StoreError, match='owner_boundary_alive'):
        recovery.apply(store.root)
    assert (lock.path / 'owner.json').exists()
    assert repository.latest_run(profile['id'])['state'] == 'starting'
    assert not (lock.path / 'recovering').exists()


@pytest.mark.parametrize('already_revoked', [False, True])
def test_recovery_preserves_api_keys_without_reviving_revoked_grants(crashed, already_revoked):
    from camoufox.studio.api_keys import ApiKeys

    store, lock, profile, repository, _, cookie = crashed
    session = store.login('root', 'synthetic-admin-password')
    keys = ApiKeys(store)
    created = keys.create(session['session_id'], {'name': 'Recovery worker', 'profile_ids': [profile['id']]})
    if already_revoked:
        keys.revoke(session['session_id'], created['key']['id'])
    with store._connect() as connection:
        original = [tuple(row) for row in connection.execute('SELECT * FROM team_api_keys')]
        original_session = tuple(connection.execute(
            'SELECT * FROM team_sessions WHERE id = (SELECT session_id FROM team_api_keys WHERE id = ?)',
            (created['key']['id'],),
        ).fetchone())
    recovery.apply(store.root)
    assert not lock.path.exists()
    assert repository.session_id(cookie) is None
    assert store.resolve_session(session['session_id']) is None
    with store._connect() as connection:
        assert [tuple(row) for row in connection.execute('SELECT * FROM team_api_keys')] == original
        assert tuple(connection.execute(
            'SELECT * FROM team_sessions WHERE id = (SELECT session_id FROM team_api_keys WHERE id = ?)',
            (created['key']['id'],),
        ).fetchone()) == original_session
        assert connection.execute('PRAGMA foreign_key_check').fetchone() is None
    if already_revoked:
        with pytest.raises(ValidationError, match='session_expired'):
            keys.authenticate(created['token'], '127.0.0.1')
    else:
        authenticated = keys.authenticate(created['token'], '127.0.0.1')
        assert authenticated is not None
    fresh = store.login('root', 'synthetic-admin-password')
    listed = keys.list(fresh['session_id'])
    assert len(listed) == 1 and listed[0]['id'] == created['key']['id']
    assert (listed[0]['revoked_at'] is not None) is already_revoked


def test_recovery_never_clears_unrelated_durable_ownership(crashed):
    store, lock, profile, repository, _, _ = crashed
    with store._write() as (connection, _):
        connection.execute("UPDATE team_runs SET owner = ?", (str(uuid4()),))
    with pytest.raises(StoreError, match='recovery_owner_mismatch'):
        recovery.apply(store.root)
    assert (lock.path / 'owner.json').exists()
    assert repository.latest_run(profile['id'])['state'] == 'starting'


def test_legacy_crash_marker_requires_manual_investigation(tmp_path):
    lock = WorkspaceLock(tmp_path)
    lock.acquire()
    with pytest.raises(StoreError, match='recovery_boundary_missing'):
        recovery.inspect(tmp_path)
    lock.release()


def test_restore_or_retirement_barrier_is_not_removed_by_crash_recovery(crashed):
    store, lock, _, _, _, _ = crashed
    with store._write() as (connection, _):
        connection.execute("INSERT INTO workspace_metadata VALUES ('execution_barrier', ?)",
                           (json.dumps({'reason': 'retired'}),))
    with pytest.raises(StoreError, match='workspace_quarantined'):
        recovery.apply(store.root)
    assert (lock.path / 'owner.json').exists()
