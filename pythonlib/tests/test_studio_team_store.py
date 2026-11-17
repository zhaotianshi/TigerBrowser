"""S1 team storage: members, rate-limited logins, sessions, grants and redacted audit."""

import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from camoufox.studio.store import (
    ProfileStore,
    StoreError,
    TEAM_SETUP_CODE_PATH,
    TEAM_LOGIN_LOCK_SECONDS,
    ValidationError,
)


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path)


def _write_setup_code(store, code='local-setup-code'):
    descriptor = os.open(store.root / TEAM_SETUP_CODE_PATH,
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
        stream.write(code)


def _initialize(store, code='local-setup-code'):
    _write_setup_code(store, code)
    return store.initialize_team(code, 'root', 'Root Admin', 'admin-password-1')


def test_first_admin_requires_one_time_local_setup_code(store):
    with pytest.raises(StoreError, match='setup code'):
        store.initialize_team('missing', 'root', 'Root Admin', 'admin-password-1')
    _write_setup_code(store)
    with pytest.raises(ValidationError, match='invalid_setup_code'):
        store.initialize_team('wrong', 'root', 'Root Admin', 'admin-password-1')
    assert not (store.root / TEAM_SETUP_CODE_PATH).exists()
    _write_setup_code(store)
    member = store.initialize_team('local-setup-code', 'root', 'Root Admin', 'admin-password-1')
    assert member['role'] == 'admin' and member['active'] and member['permission_version'] == 1
    assert not (store.root / TEAM_SETUP_CODE_PATH).exists()
    with pytest.raises(ValidationError, match='team_already_initialized'):
        store.initialize_team('local-setup-code', 'other', 'Other', 'admin-password-2')


def test_unknown_member_login_still_pays_the_password_hash_cost(store, monkeypatch):
    import camoufox.studio.store as module

    calls = []
    original = module._hash_password

    def hashed(*args):
        calls.append(args[2])
        return original(*args)

    monkeypatch.setattr(module, '_hash_password', hashed)
    with pytest.raises(ValidationError, match='invalid_credentials'):
        store.login('unknown', 'member-password-1')
    assert calls == [module.TEAM_PASSWORD_TEMPLATE]


def test_setup_code_file_is_owner_only(store):
    store.begin_team_setup()
    if os.name != 'nt':
        assert (store.root / TEAM_SETUP_CODE_PATH).stat().st_mode & 0o777 == 0o600


def test_member_name_rules_and_uniqueness(store):
    admin = _initialize(store)
    for name in ('has space', 'Case' * 30, 'admin!', ''):
        with pytest.raises(ValidationError):
            store.create_member(admin['id'], name, 'Display', 'operator', 'member-password-1')
    member = store.create_member(admin['id'], 'Alice', 'Alice A', 'operator', 'member-password-1')
    assert member['role'] == 'operator' and member['active']
    with pytest.raises(ValidationError, match='member_name_in_use'):
        store.create_member(admin['id'], 'Alice', 'Other', 'viewer', 'member-password-2')
    # Names are case-sensitive byte sequences; a different name is a different member.
    store.create_member(admin['id'], 'alice', 'Lower', 'viewer', 'member-password-2')
    with pytest.raises(ValidationError, match='member_name_in_use'):
        store.create_member(admin['id'], 'alice', 'Again', 'viewer', 'member-password-2')
    store.set_member_active(admin['id'], member['id'], False)
    with pytest.raises(ValidationError, match='member_name_in_use'):
        store.create_member(admin['id'], 'Alice', 'Reuse', 'viewer', 'member-password-3')


def test_role_and_password_validation(store):
    admin = _initialize(store)
    with pytest.raises(ValidationError, match='administrator'):
        store.create_member(admin['id'], 'second', 'Second', 'admin', 'member-password-1')
    for password in ('short', 'x' * 1025, 'has\nline'):
        with pytest.raises(ValidationError):
            store.create_member(admin['id'], 'bob', 'Bob', 'operator', password)
    with pytest.raises(ValidationError):
        store.change_own_password(admin['id'], 'admin-password-1', 'short')


def test_non_admin_cannot_administer(store):
    admin = _initialize(store)
    operator = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    with pytest.raises(ValidationError, match='admin_required'):
        store.create_member(operator['id'], 'x', 'X', 'viewer', 'member-password-2')
    with pytest.raises(ValidationError, match='admin_required'):
        store.set_member_active(operator['id'], admin['id'], False)
    with pytest.raises(ValidationError, match='admin_required'):
        store.reset_member_password(operator['id'], admin['id'], 'member-password-3')
    with pytest.raises(ValidationError, match='admin_required'):
        store.grant_profile(operator['id'], operator['id'], profile['id'], ['view'])
    with pytest.raises(ValidationError, match='admin_required'):
        store.revoke_profile(operator['id'], operator['id'], profile['id'])


def test_successful_login_returns_redacted_member_and_session(store):
    admin = _initialize(store)
    store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    opened = store.login('op', 'member-password-1')
    assert set(opened['member']) == {'id', 'name', 'display', 'role', 'permission_version'}
    resolved = store.resolve_session(opened['session_id'])
    assert resolved['member']['id'] == opened['member']['id']
    assert resolved['permissions'] == {}
    store.logout(opened['session_id'])
    assert store.resolve_session(opened['session_id']) is None
    assert store.resolve_session(opened['session_id']) is None


def test_expired_session_revokes_itself(store):
    admin = _initialize(store)
    opened = store.login('root', 'admin-password-1')
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec='microseconds')
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute('SELECT data FROM team_sessions').fetchone()
        import json
        session = json.loads(row[0])
        session['expires_at'] = past
        connection.execute('UPDATE team_sessions SET data = ?', (json.dumps(session),))
    assert store.resolve_session(opened['session_id']) is None
    with sqlite3.connect(store.database_path) as connection:
        import json
        assert json.loads(connection.execute('SELECT data FROM team_sessions').fetchone()[0])['revoked_at']


def test_disabled_member_sessions_and_grants_stop_applying(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'operate'])
    opened = store.login('op', 'member-password-1')
    store.set_member_active(admin['id'], member['id'], False)
    assert store.resolve_session(opened['session_id']) is None
    with pytest.raises(ValidationError, match='invalid_credentials'):
        store.login('op', 'member-password-1')
    with pytest.raises(ValidationError, match='member_disabled'):
        store.check_profile_permission(member['id'], profile['id'], 'view')
    store.set_member_active(admin['id'], member['id'], True)
    store.check_profile_permission(member['id'], profile['id'], 'view')


def test_admin_cannot_disable_self(store):
    admin = _initialize(store)
    with pytest.raises(ValidationError, match='cannot_change_self'):
        store.set_member_active(admin['id'], admin['id'], False)


def test_failed_logins_lock_after_limit_and_recovery(store, monkeypatch):
    admin = _initialize(store)
    store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    for _ in range(4):
        with pytest.raises(ValidationError, match='invalid_credentials'):
            store.login('op', 'wrong-password')
    store.login('op', 'member-password-1')
    for _ in range(4):
        with pytest.raises(ValidationError, match='invalid_credentials'):
            store.login('op', 'wrong-password')
    # The fifth failure starts the lock; it still reports a generic failure.
    with pytest.raises(ValidationError, match='invalid_credentials'):
        store.login('op', 'wrong-password')
    with pytest.raises(ValidationError, match='login_locked'):
        store.login('op', 'member-password-1')
    with pytest.raises(ValidationError, match='login_locked'):
        store.login('op', 'wrong-password')
    real = time.time
    monkeypatch.setattr('camoufox.studio.store.time.time',
                        lambda: real() + TEAM_LOGIN_LOCK_SECONDS + 1)
    assert store.login('op', 'member-password-1')['member']['name'] == 'op'
    assert store.login('root', 'admin-password-1')['member']['name'] == 'root'


def test_login_attempts_older_than_the_window_expire(store, monkeypatch):
    admin = _initialize(store)
    store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    for _ in range(4):
        with pytest.raises(ValidationError, match='invalid_credentials'):
            store.login('op', 'wrong-password')
    # The lockout begins with the fifth failure inside the window.
    with pytest.raises(ValidationError, match='invalid_credentials'):
        store.login('op', 'wrong-password')
    past = (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat(timespec='microseconds')
    with sqlite3.connect(store.database_path) as connection:
        connection.execute('UPDATE team_login_attempts SET failed_at = ?', (past,))
        connection.execute('DELETE FROM team_login_limits')
    # Old attempts no longer count, so one more failure is not a lockout yet.
    with pytest.raises(ValidationError, match='invalid_credentials'):
        store.login('op', 'wrong-password')
    store.login('op', 'member-password-1')


def test_grant_replace_revoke_and_version_bumps(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view'])
    opened = store.login('op', 'member-password-1')
    with pytest.raises(ValidationError, match='access_denied'):
        store.check_profile_permission(member['id'], profile['id'], 'operate')
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'operate', 'watch'])
    assert store.resolve_session(opened['session_id']) is None
    store.check_profile_permission(member['id'], profile['id'], 'operate')
    assert [grant['permissions'] for grant in store.list_grants(member['id'])] == [
        ['view', 'operate', 'watch']]
    store.revoke_profile(admin['id'], member['id'], profile['id'])
    assert store.list_grants(member['id']) == []
    with pytest.raises(ValidationError, match='access_denied'):
        store.check_profile_permission(member['id'], profile['id'], 'view')


def test_grant_validation(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    viewer = store.create_member(admin['id'], 'vi', 'Vi', 'viewer', 'member-password-2')
    profile = store.save_profile({'name': 'Env'})
    for permissions in ([], ['view', 'view'], ['admin'], 'view', [None]):
        with pytest.raises(ValidationError):
            store.grant_profile(admin['id'], member['id'], profile['id'], permissions)
    with pytest.raises(ValidationError, match='does not exist'):
        store.grant_profile(admin['id'], member['id'], 'c8b0c63a-6a3e-4d2b-9f2c-9f2f36b6a001', ['view'])
    with pytest.raises(ValidationError, match='grant_not_applicable'):
        store.grant_profile(admin['id'], admin['id'], profile['id'], ['view'])
    store.grant_profile(admin['id'], viewer['id'], profile['id'], ['watch'])
    store.check_profile_permission(viewer['id'], profile['id'], 'watch')


def test_permission_check_hides_unknown_profiles(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view'])
    with pytest.raises(ValidationError, match='access_denied'):
        store.check_profile_permission(member['id'], profile['id'], 'operate')
    with pytest.raises(ValidationError, match='access_denied'):
        store.check_profile_permission(member['id'], 'c8b0c63a-6a3e-4d2b-9f2c-9f2f36b6a001', 'view')
    with pytest.raises(ValidationError, match='Unknown permission'):
        store.check_profile_permission(member['id'], profile['id'], 'admin')


def test_visible_profiles_follow_role_and_grants(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    first = store.save_profile({'name': 'One'})
    second = store.save_profile({'name': 'Two'})
    assert store.visible_profiles(member['id']) == []
    store.grant_profile(admin['id'], member['id'], second['id'], ['view'])
    assert [profile['name'] for profile in store.visible_profiles(member['id'])] == ['Two']
    assert {profile['name'] for profile in store.visible_profiles(admin['id'])} == {'One', 'Two'}


def test_password_resets_revoke_sessions(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    opened = store.login('op', 'member-password-1')
    store.reset_member_password(admin['id'], member['id'], 'member-password-2')
    assert store.resolve_session(opened['session_id']) is None
    with pytest.raises(ValidationError, match='invalid_credentials'):
        store.login('op', 'member-password-1')
    store.login('op', 'member-password-2')
    own = store.login('op', 'member-password-2')
    with pytest.raises(ValidationError, match='invalid_current_password'):
        store.change_own_password(member['id'], 'wrong', 'member-password-3')
    store.change_own_password(member['id'], 'member-password-2', 'member-password-3')
    assert store.resolve_session(own['session_id']) is None
    store.login('op', 'member-password-3')


def test_profile_revision_and_checked_updates(store):
    profile = store.save_profile({'name': 'Env'})
    assert profile['revision'] == 1
    updated = store.update_profile_checked({'notes': 'seen'}, profile['id'], 1)
    assert updated['revision'] == 2 and updated['notes'] == 'seen'
    with pytest.raises(ValidationError, match='revision_conflict'):
        store.update_profile_checked({'notes': 'stale'}, profile['id'], 1)
    with pytest.raises(ValidationError):
        store.update_profile_checked({'notes': 'bad'}, profile['id'], 0)
    store.save_profile({'tags': ['x']}, profile['id'])
    assert store.get_profile(profile['id'])['revision'] == 3


def test_legacy_profile_without_revision_defaults_to_one(store):
    profile = store.save_profile({'name': 'Env'})
    with sqlite3.connect(store.database_path) as connection:
        import json
        row = connection.execute('SELECT data FROM profiles WHERE id = ?', (profile['id'],)).fetchone()
        data = json.loads(row[0])
        data.pop('revision')
        connection.execute('UPDATE profiles SET data = ? WHERE id = ?', (json.dumps(data), profile['id']))
    assert store.get_profile(profile['id'])['revision'] == 1
    reopened = ProfileStore(store.root)
    updated = reopened.update_profile_checked({'notes': 'edited'}, profile['id'], 1)
    assert updated['revision'] == 2 and updated['notes'] == 'edited'


def test_audit_redaction_and_whitelist(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    store.grant_profile(admin['id'], member['id'],
                        store.save_profile({'name': 'Env'})['id'], ['view'])
    store.login('op', 'member-password-1')
    entries = store.list_audit()
    actions = [entry['action'] for entry in entries]
    assert {'team.initialized', 'member.created', 'grant.updated', 'session.opened'} <= set(actions)
    assert all(entry['result'] == 'ok' for entry in entries)
    assert all('\n' not in entry['actor_name'] + entry['detail'] for entry in entries)
    with pytest.raises(ValidationError):
        store.record_audit('bad action with spaces')
    with pytest.raises(ValidationError):
        store.record_audit('member.disabled', result='failed: secret')
    with pytest.raises(ValidationError):
        store.record_audit('member.disabled', detail='contains secret token')


def test_export_never_contains_team_data(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    store.grant_profile(admin['id'], member['id'], store.save_profile({'name': 'Env'})['id'], ['view'])
    store.login('op', 'member-password-1')
    exported = store.export_config()
    assert exported['version'] == 2 and set(exported) == {'schema', 'version', 'profiles', 'proxies'}
    import json
    assert 'member-password-1' not in json.dumps(exported)
    for kinds in ({'members'}, {'sessions'}, {'grants'}, {'audit'}, {'profiles', 'members'}, set(), {'unknown'}):
        with pytest.raises(ValidationError):
            store.export_config(kinds)


def test_import_accepts_v1_and_v2_exports(store):
    profile = store.save_profile({'name': 'Env'})
    exported = store.export_config()
    assert store.import_config(exported) == {'profiles': 1, 'proxies': 0}
    legacy = dict(exported, version=1)
    assert store.import_config(legacy) == {'profiles': 1, 'proxies': 0}
    with pytest.raises(ValidationError, match='schema or version'):
        store.import_config(dict(exported, version=3))


def test_deleting_profile_removes_its_grants(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view'])
    store.delete_profile(profile['id'])
    assert store.list_grants(member['id']) == []
    with pytest.raises(ValidationError, match='access_denied'):
        store.check_profile_permission(member['id'], profile['id'], 'view')


def test_archived_profiles_disappear_from_team_views(store):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'op', 'Op', 'operator', 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view'])
    store.archive_profile(profile['id'])
    assert store.visible_profiles(member['id']) == []
    assert store.visible_profiles(admin['id']) == []
    assert store.list_grants(member['id'])


def test_existing_v1_workspace_upgrades_and_stays_desktop_compatible(tmp_path):
    database = tmp_path / 'studio.sqlite3'
    with sqlite3.connect(database) as connection:
        connection.execute('PRAGMA user_version = 1')
        connection.executescript('''
            CREATE TABLE proxies (id TEXT PRIMARY KEY, data TEXT NOT NULL, has_password INTEGER NOT NULL);
            CREATE TABLE profiles (id TEXT PRIMARY KEY, proxy_id TEXT REFERENCES proxies(id),
                archived INTEGER NOT NULL, data TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                action TEXT NOT NULL, profile_id TEXT, detail TEXT NOT NULL);
            CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT);
            CREATE TABLE workspace_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE identity_history (digest TEXT PRIMARY KEY, created_at TEXT NOT NULL);
        ''')
    from camoufox.studio.store import SCHEMA_VERSION
    store = ProfileStore(tmp_path)
    _write_setup_code(store)
    admin = store.initialize_team('local-setup-code', 'root', 'Root', 'admin-password-1')
    store.login('root', 'admin-password-1')
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT value FROM workspace_metadata WHERE key = 'team_schema_version'"
        ).fetchone()[0] == '6'
    assert admin['role'] == 'admin'


def test_future_team_schema_is_rejected(tmp_path):
    store = ProfileStore(tmp_path)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "INSERT OR REPLACE INTO workspace_metadata (key, value) "
            "VALUES ('team_schema_version', '99')")
    with pytest.raises(StoreError, match='team database version'):
        ProfileStore(tmp_path)


def test_session_member_index_and_eager_revocation(store):
    admin = _initialize(store)
    opened = store.login('root', 'admin-password-1')
    with store._connect() as connection:
        row = connection.execute('SELECT member_id FROM team_sessions WHERE id = ?',
                                 (opened['session_id'],)).fetchone()
    assert row['member_id'] == admin['id']
    store.reset_member_password(admin['id'], admin['id'], 'replacement-password-1')
    with store._connect() as connection:
        import json
        row = connection.execute('SELECT data FROM team_sessions WHERE id = ?',
                                 (opened['session_id'],)).fetchone()
        assert json.loads(row['data'])['revoked_at'] is not None


@pytest.mark.parametrize('role,permission', [('viewer', 'operate'), ('viewer', 'manage'),
                                           ('operator', 'manage')])
def test_grants_cannot_exceed_role_cap(store, role, permission):
    admin = _initialize(store)
    member = store.create_member(admin['id'], 'member', 'Member', role, 'member-password-1')
    profile = store.save_profile({'name': 'Env'})
    with pytest.raises(ValidationError, match='role_permission_denied'):
        store.grant_profile(admin['id'], member['id'], profile['id'], ['view', permission])


def test_lockout_deadline_survives_clock_reference_reset(store, monkeypatch):
    _initialize(store)
    clock = 1800000000.0
    monkeypatch.setattr('camoufox.studio.store.time.time', lambda: clock)
    monkeypatch.setattr('camoufox.studio.store.time.monotonic', lambda: 1000000.0)
    for _ in range(5):
        with pytest.raises(ValidationError, match='invalid_credentials'):
            store.login('root', 'wrong-password')
    monkeypatch.setattr('camoufox.studio.store.time.monotonic', lambda: 0.0)
    with store._connect() as connection:
        deadline = connection.execute('SELECT locked_until FROM team_login_limits').fetchone()[0]
    assert deadline == clock + TEAM_LOGIN_LOCK_SECONDS
    with pytest.raises(ValidationError, match='login_locked'):
        store.login('root', 'admin-password-1')
    clock += TEAM_LOGIN_LOCK_SECONDS + 1
    assert store.login('root', 'admin-password-1')['member']['role'] == 'admin'


def test_v1_team_migration_repairs_indexes_without_changing_identity(store, monkeypatch):
    import json
    admin = _initialize(store)
    opened = store.login('root', 'admin-password-1')
    profile = store.save_profile({'name': 'Existing'})
    store.set_identity(profile['id'], {'config': {'navigator.platform': 'Win32'}})
    before = store.get_profile(profile['id'])
    with store._write() as (connection, _):
        connection.execute("UPDATE workspace_metadata SET value = '1' WHERE key = 'team_schema_version'")
        connection.execute('UPDATE team_sessions SET member_id = id')
        connection.execute('INSERT INTO team_login_limits VALUES (?, 5, ?)', ('root', 1000900))
        legacy = dict(before)
        legacy.pop('revision')
        connection.execute('UPDATE profiles SET data = ? WHERE id = ?',
                           (json.dumps(legacy), profile['id']))
    monkeypatch.setattr('camoufox.studio.store.time.time', lambda: 1800000000.0)
    migrated = ProfileStore(store.root)
    assert migrated.get_profile(profile['id']) == before
    assert migrated.resolve_session(opened['session_id']) is None
    with migrated._connect() as connection:
        row = connection.execute('SELECT member_id, data FROM team_sessions').fetchone()
        assert row['member_id'] == admin['id'] and json.loads(row['data'])['revoked_at']
        assert connection.execute('SELECT locked_until FROM team_login_limits').fetchone()[0] == 1800000900.0


def test_unfinished_setup_capability_is_rotated(store):
    first = store.begin_team_setup()
    second = store.begin_team_setup()
    assert first != second
    with pytest.raises(ValidationError, match='invalid_setup_code'):
        store.initialize_team(first, 'root', 'Root', 'admin-password-1')
    assert not store.list_members()
