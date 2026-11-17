"""Stopped encrypted snapshots never create a second executable identity."""

import json
import os
import zipfile
from uuid import uuid4

import pytest
from cryptography.cobblestone import Cobblestone128Encryptor

from camoufox.studio import backup
from camoufox.studio.store import ProfileStore, StoreError, ValidationError
from camoufox.studio.workspace import WorkspaceLock, WorkspaceLockError


@pytest.fixture
def workspace(tmp_path):
    store = ProfileStore(tmp_path / 'original')
    admin = store.initialize_team(store.begin_team_setup(), 'root', 'Admin', 'admin-password')
    profile = store.save_profile({'name': 'Persistent'})
    directory = store.profile_dir(profile['id'])
    directory.mkdir(parents=True)
    (directory / 'cookies.sqlite').write_bytes(b'synthetic-persistent-cookie')
    (directory / 'storage').mkdir()
    (directory / 'storage' / 'site').write_bytes(b'synthetic-local-storage')
    store.set_identity(profile['id'], {'config': {'navigator.platform': 'Win32'}})
    session = store.login('root', 'admin-password')
    return store, admin, profile, session


def snapshot(workspace, tmp_path):
    store, _, _, _ = workspace
    key = Cobblestone128Encryptor.generate_key()
    target = tmp_path / 'snapshot.cfxbackup'
    manifest = backup.create_snapshot(store.root, target, key)
    return key, target, manifest


def test_encrypted_round_trip_preserves_data_but_revokes_capabilities(workspace, tmp_path):
    store, _, profile, session = workspace
    key, target, manifest = snapshot(workspace, tmp_path)
    assert b'synthetic-persistent-cookie' not in target.read_bytes()
    restored = backup.restore_snapshot(target, tmp_path / 'restore', key)
    assert restored == tmp_path / 'restore' / 'workspace'
    copy = ProfileStore(restored)
    assert copy.get_profile(profile['id'])['identity'] == store.get_profile(profile['id'])['identity']
    assert (copy.profile_dir(profile['id']) / 'cookies.sqlite').read_bytes() == b'synthetic-persistent-cookie'
    assert copy.resolve_session(session['session_id']) is None
    assert copy.secret_store.namespace != store.secret_store.namespace
    with pytest.raises(StoreError, match='quarantined'):
        copy.check_execution_ownership(profile['id'])
    assert manifest['format'] == 1 and manifest['snapshot_id']
    assert store.resolve_session(session['session_id']) is not None
    assert not (store.root / 'workspace.lock').exists()


def test_restore_discards_api_keys_with_their_sessions(workspace, tmp_path):
    from camoufox.studio.api_keys import ApiKeys
    store, _, _, session = workspace
    token = ApiKeys(store).create(session['session_id'], {'name': 'Backup worker'})['token']
    key, target, _ = snapshot(workspace, tmp_path)
    copy = ProfileStore(backup.restore_snapshot(target, tmp_path / 'restore', key))
    with copy._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM team_api_keys').fetchone()[0] == 0
        assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(ValueError, match='session_expired'):
        ApiKeys(copy).authenticate(token, '127.0.0.1')
    ApiKeys(store).authenticate(token, '127.0.0.1')


def test_previous_schema_snapshot_is_verified_before_additive_migration(workspace, tmp_path):
    import hashlib
    import sqlite3
    key, archive, manifest = snapshot(workspace, tmp_path)
    plain = backup._decrypt(archive, key, tmp_path, backup.MAX_BYTES)
    with zipfile.ZipFile(plain) as original:
        entries = {name: original.read(name) for name in original.namelist()}
    database = tmp_path / 'historical.sqlite3'
    database.write_bytes(entries['studio.sqlite3'])
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE team_api_keys')
        connection.execute("UPDATE workspace_metadata SET value='4' WHERE key='team_schema_version'")
    payload = database.read_bytes()
    entries['studio.sqlite3'] = payload
    manifest['team_schema'] = 4
    manifest['files']['studio.sqlite3'] = {'size': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
    entries['manifest.json'] = json.dumps(manifest).encode()
    rebuilt = tmp_path / 'historical.zip'
    with zipfile.ZipFile(rebuilt, 'w', compression=zipfile.ZIP_STORED) as output:
        for name, content in entries.items():
            output.writestr(name, content)
    historical = tmp_path / 'historical.cfxbackup'
    backup._encrypt(rebuilt, historical, key)
    restored = ProfileStore(backup.restore_snapshot(historical, tmp_path / 'restore', key))
    assert restored.get_profile(workspace[2]['id'])['identity'] == workspace[0].get_profile(workspace[2]['id'])['identity']
    with restored._connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM team_api_keys').fetchone()[0] == 0
    with pytest.raises(StoreError, match='quarantined'):
        restored.check_execution_ownership(workspace[2]['id'])


@pytest.mark.parametrize('historical_reservation', [False, True])
def test_completed_recovery_receipts_survive_encrypted_backup(workspace, tmp_path, historical_reservation):
    store = workspace[0]
    owner = uuid4().hex
    receipt = store.root / 'recovery' / (owner + '.lock')
    receipt.mkdir(parents=True)
    raw = json.dumps({'version': 2, 'owner': owner, 'execution_owner': str(uuid4()),
                      'boundary': {'kind': 'synthetic-completed-boundary'}}).encode()
    (receipt / 'owner.json').write_bytes(raw)
    if historical_reservation:
        (receipt / 'recovering').mkdir()
    key, archive, manifest = snapshot(workspace, tmp_path)
    assert manifest['format'] == 2
    name = 'recovery/' + owner + '.lock/owner.json'
    assert name in manifest['files']
    restored = backup.restore_snapshot(archive, tmp_path / 'restored-receipts', key)
    assert (restored / name).read_bytes() == raw
    assert (receipt / 'owner.json').read_bytes() == raw
    assert (receipt / 'recovering').exists() == historical_reservation
    assert not (restored / 'workspace.lock').exists()
    backup._verify_workspace(ProfileStore(restored), manifest)


@pytest.mark.parametrize('invalid', ['owner', 'extra', 'oversized', 'reservation_file', 'reservation_data'])
def test_recovery_receipts_cannot_hide_unrecognized_data(workspace, tmp_path, invalid):
    store = workspace[0]
    owner = uuid4().hex
    receipt = store.root / 'recovery' / (owner + '.lock')
    receipt.mkdir(parents=True)
    record = {'version': 2, 'owner': owner, 'execution_owner': str(uuid4()), 'boundary': {}}
    if invalid == 'owner':
        record['owner'] = uuid4().hex
    (receipt / 'owner.json').write_text(json.dumps(record))
    if invalid == 'extra':
        (receipt / 'unrecognized').write_text('must not be silently discarded')
    if invalid == 'oversized':
        (receipt / 'owner.json').write_bytes(b' ' * 16385)
    if invalid == 'reservation_file':
        (receipt / 'recovering').write_bytes(b'')
    if invalid == 'reservation_data':
        (receipt / 'recovering').mkdir()
        (receipt / 'recovering' / 'unrecognized').write_bytes(b'data')
    with pytest.raises(backup.BackupError, match='unsafe_recovery_receipt'):
        snapshot(workspace, tmp_path)


def test_legacy_chain_snapshot_preserves_requirements_without_enabling_the_route(workspace, tmp_path, monkeypatch):
    from camoufox.studio.secrets import KeyringSecretStore

    secrets = {}
    monkeypatch.setattr(KeyringSecretStore, 'get', lambda self, name: secrets.get((self.namespace, name)))
    monkeypatch.setattr(KeyringSecretStore, 'set', lambda self, name, value: secrets.__setitem__((self.namespace, name), value))
    store = workspace[0]
    password, node_secret = 'backup-test-password', 'backup-obfs-test'
    proxy = store.save_proxy({'name': 'Legacy route', 'server': 'socks5://proxy.example:1080',
                              'password': password})
    # Older versions saved chain metadata in SQLite and its credentials in the keyring.
    proxy['node'] = {'core': 'mihomo', 'proxies': [{'type': 'socks5'}]}
    proxy['has_node_secret'] = True
    store.secret_store.set(proxy['id'] + ':node_secret', node_secret)
    with store._write() as (connection, _):
        connection.execute('UPDATE proxies SET data = ? WHERE id = ?', (json.dumps(proxy), proxy['id']))

    key, target, manifest = snapshot(workspace, tmp_path)
    assert manifest['credentials'] == [{'id': proxy['id'], 'fields': ['password', 'node_secret']}]
    plain = backup._decrypt(target, key, tmp_path, backup.MAX_BYTES)
    with zipfile.ZipFile(plain) as archive:
        for entry in archive.namelist():
            content = archive.read(entry)
            assert password.encode() not in content and node_secret.encode() not in content

    restored = backup.restore_snapshot(target, tmp_path / 'extended-restore', key)
    copy = ProfileStore(restored)
    assert copy.get_proxy(proxy['id']) == proxy
    assert copy.secret_store.get(proxy['id']) is None
    assert copy.secret_store.get(proxy['id'] + ':node_secret') is None
    with pytest.raises(ValidationError, match='unsupported node or chain'):
        backup._verify_credentials(copy)


@pytest.mark.parametrize('change', ['wrong_key', 'truncate', 'tamper', 'append'])
def test_authentication_failure_never_publishes_restore(workspace, tmp_path, change):
    key, archive, _ = snapshot(workspace, tmp_path)
    data = archive.read_bytes()
    if change == 'wrong_key':
        key = Cobblestone128Encryptor.generate_key()
    elif change == 'truncate':
        archive.write_bytes(data[:-1])
    elif change == 'append':
        archive.write_bytes(data + b'garbage')
    else:
        archive.write_bytes(data[:100] + bytes([data[100] ^ 1]) + data[101:])
    with pytest.raises(backup.BackupError):
        backup.restore_snapshot(archive, tmp_path / 'restore', key)
    assert not (tmp_path / 'restore').exists()
    assert not list(tmp_path.glob('.studio-*'))


def test_active_workspace_or_unresolved_run_cannot_be_backed_up(workspace, tmp_path):
    store, _, profile, _ = workspace
    key = Cobblestone128Encryptor.generate_key()
    lock = WorkspaceLock(store.root)
    lock.acquire()
    try:
        with pytest.raises(WorkspaceLockError):
            backup.create_snapshot(store.root, tmp_path / 'a.cfxbackup', key)
    finally:
        lock.release()
    run_id = str(uuid4())
    with store._write() as (connection, _):
        connection.execute('INSERT INTO team_runs VALUES (?, ?, ?, ?, ?)',
                           (run_id, profile['id'], 'owner', 'recovery_required', '{}'))
    with pytest.raises(backup.BackupError, match='recovery_required'):
        backup.create_snapshot(store.root, tmp_path / 'a.cfxbackup', key)
    assert not (tmp_path / 'a.cfxbackup').exists()


def test_neither_snapshot_nor_restore_overwrites_existing_data(workspace, tmp_path):
    key, archive, _ = snapshot(workspace, tmp_path)
    saved = archive.read_bytes()
    with pytest.raises(backup.BackupError):
        backup.create_snapshot(workspace[0].root, archive, key)
    assert archive.read_bytes() == saved
    target = tmp_path / 'restore'
    target.mkdir()
    (target / 'sentinel').write_text('user-owned')
    with pytest.raises(backup.BackupError):
        backup.restore_snapshot(archive, target, key)
    assert (target / 'sentinel').read_text() == 'user-owned'


def test_source_paths_and_size_are_strictly_bounded(workspace, tmp_path):
    key = Cobblestone128Encryptor.generate_key()
    with pytest.raises(backup.BackupError, match='size_limit'):
        backup.create_snapshot(workspace[0].root, tmp_path / 'small', key, max_bytes=100)
    assert not (tmp_path / 'small').exists()
    (workspace[0].root / 'unrecognized').write_text('not silently discarded')
    with pytest.raises(backup.BackupError, match='unknown_workspace_file'):
        backup.create_snapshot(workspace[0].root, tmp_path / 'unknown', key)


@pytest.mark.parametrize('name', ['parent.lock', '.parentlock', 'lock'])
def test_native_profile_locks_are_excluded_without_reading_their_targets(workspace, tmp_path, name):
    store, _, profile, _ = workspace
    lock = store.profile_dir(profile['id']) / name
    if os.name == 'posix':
        lock.symlink_to('127.0.1.1:+12345')
    else:
        lock.write_bytes(b'')
    key, archive, record = snapshot(workspace, tmp_path)
    assert 'profiles/' + profile['id'] + '/' + name not in record['files']
    restored = backup.restore_snapshot(archive, tmp_path / 'restore', key)
    assert not (ProfileStore(restored).profile_dir(profile['id']) / name).exists()
    assert os.path.lexists(lock)


@pytest.mark.parametrize('name', ['parent.lock', '.parentlock', 'lock'])
def test_profile_lock_names_cannot_hide_directories(workspace, tmp_path, name):
    store, _, profile, _ = workspace
    (store.profile_dir(profile['id']) / name).mkdir()
    key = Cobblestone128Encryptor.generate_key()
    with pytest.raises(backup.BackupError, match='unsafe_workspace_file'):
        backup.create_snapshot(store.root, tmp_path / 'snapshot', key)


@pytest.mark.parametrize('location', ['cookies.sqlite', 'storage/lock'])
def test_nontransient_profile_entries_remain_strict(workspace, tmp_path, location):
    store, _, profile, _ = workspace
    entry = store.profile_dir(profile['id']) / location
    entry.unlink(missing_ok=True)
    target = tmp_path / 'outside'
    target.write_bytes(b'must-not-be-archived')
    if os.name == 'posix':
        entry.symlink_to(target)
    else:
        os.link(target, entry)
    key = Cobblestone128Encryptor.generate_key()
    with pytest.raises(backup.BackupError, match='unsafe_workspace_file'):
        backup.create_snapshot(store.root, tmp_path / 'snapshot', key)


@pytest.mark.parametrize('name', ['../outside', '/absolute', 'C:/outside', 'profiles/a/../b',
                                  'profiles/a/CON', 'profiles/a/file:stream', 'profiles/a/file.',
                                  'profiles/a/file\\outside'])
def test_archive_paths_are_portable_and_cannot_escape(name):
    with pytest.raises(backup.BackupError):
        backup.checked_name(name)


def test_portable_archive_paths_reject_non_utf8_names():
    with pytest.raises(backup.BackupError, match='invalid_archive_path'):
        backup.checked_name('invalid-\udcff.txt')


def test_archive_metadata_and_digest_are_verified(workspace, tmp_path):
    key, archive, manifest = snapshot(workspace, tmp_path)
    plain = backup._decrypt(archive, key, tmp_path, backup.MAX_BYTES)
    try:
        import zipfile

        with zipfile.ZipFile(plain, 'a') as contents:
            contents.writestr('../escaped', 'host data')
        altered = tmp_path / 'altered'
        backup._encrypt(plain, altered, key)
        with pytest.raises(backup.BackupError):
            backup.restore_snapshot(altered, tmp_path / 'restore', key)
        assert not (tmp_path / 'escaped').exists()
        assert manifest['files']['studio.sqlite3']['sha256']
    finally:
        plain.unlink()


@pytest.mark.parametrize('defect', ['digest', 'size', 'collision', 'symlink', 'compression',
                                  'schema', 'object', 'unknown_profile'])
def test_authenticated_but_invalid_archive_is_rejected(workspace, tmp_path, defect):
    import stat
    import zipfile

    key, archive, record = snapshot(workspace, tmp_path)
    plain = backup._decrypt(archive, key, tmp_path, backup.MAX_BYTES)
    cookie = 'profiles/' + workspace[2]['id'] + '/cookies.sqlite'
    rebuilt = tmp_path / 'rebuilt.zip'
    try:
        with zipfile.ZipFile(plain) as original:
            entries = {name: original.read(name) for name in original.namelist()}
        if defect == 'digest':
            entries[cookie] = b'x' * len(entries[cookie])
        elif defect == 'size':
            record['files'][cookie]['size'] += 1
        elif defect == 'collision':
            entries[cookie.upper()] = entries[cookie]
        elif defect == 'schema':
            record['team_schema'] = 99
        elif defect == 'unknown_profile':
            other = cookie.replace(workspace[2]['id'], str(uuid4()))
            record['files'][other] = record['files'].pop(cookie)
            entries[other] = entries.pop(cookie)
        entries['manifest.json'] = json.dumps([] if defect == 'object' else record).encode()
        method = zipfile.ZIP_DEFLATED if defect == 'compression' else zipfile.ZIP_STORED
        with zipfile.ZipFile(rebuilt, 'w', compression=method) as contents:
            for name, payload in entries.items():
                if defect == 'symlink' and name == cookie:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    contents.writestr(info, payload)
                else:
                    contents.writestr(name, payload)
        invalid = tmp_path / 'invalid.cfxbackup'
        backup._encrypt(rebuilt, invalid, key)
        with pytest.raises(backup.BackupError):
            backup.restore_snapshot(invalid, tmp_path / 'restore', key)
        assert not (tmp_path / 'restore').exists()
    finally:
        plain.unlink()


def test_retired_workspace_cannot_launch_even_without_a_run(workspace):
    store, _, profile, _ = workspace
    with store._write() as (connection, _):
        connection.execute("INSERT INTO workspace_metadata VALUES ('execution_barrier', ?)",
                           (json.dumps({'reason': 'retired'}),))
    with pytest.raises(StoreError, match='retired'):
        store.check_execution_ownership(profile['id'])


def register_fixture(workspace, monkeypatch):
    from camoufox.studio import manifest

    baseline = {'format': 1, 'mode': 'headless', 'browser': 'fixture-artifact'}
    monkeypatch.setattr(manifest, 'capture', lambda *args, **kwargs: baseline)
    manifest.register(workspace[0].root)
    return baseline


def test_transfer_retires_original_before_allowing_the_restored_identity(workspace, tmp_path, monkeypatch):
    register_fixture(workspace, monkeypatch)
    key, archive, _ = snapshot(workspace, tmp_path)
    restored = backup.restore_snapshot(archive, tmp_path / 'restore', key)
    backup.activate_transfer(workspace[0].root, restored)
    assert backup.activate_transfer(workspace[0].root, restored) == restored
    copy = ProfileStore(restored)
    copy.check_execution_ownership(workspace[2]['id'])
    with pytest.raises(StoreError, match='retired'):
        workspace[0].check_execution_ownership(workspace[2]['id'])
    second = backup.restore_snapshot(archive, tmp_path / 'second', key)
    with pytest.raises(backup.BackupError, match='source_retired'):
        backup.activate_transfer(workspace[0].root, second)
    with pytest.raises(StoreError, match='quarantined'):
        ProfileStore(second).check_execution_ownership(workspace[2]['id'])


@pytest.mark.parametrize('change', ['source_config', 'source_default', 'source_data', 'target_data', 'runtime'])
def test_transfer_rejects_stale_data_or_changed_runtime(workspace, tmp_path, monkeypatch, change):
    baseline = register_fixture(workspace, monkeypatch)
    key, archive, _ = snapshot(workspace, tmp_path)
    restored = backup.restore_snapshot(archive, tmp_path / 'restore', key)
    if change == 'source_config':
        workspace[0].save_profile({'name': 'changed'}, workspace[2]['id'])
    elif change == 'source_default':
        workspace[0].set_default_kernel('official/152.0.4-beta.31')
    elif change in ('source_data', 'target_data'):
        root = workspace[0].root if change == 'source_data' else restored
        (root / 'profiles' / workspace[2]['id'] / 'storage/site').write_text('changed')
    else:
        baseline['browser'] = 'changed-artifact'
    with pytest.raises(backup.BackupError):
        backup.activate_transfer(workspace[0].root, restored)
    workspace[0].check_execution_ownership(workspace[2]['id'])
    with pytest.raises(StoreError, match='quarantined'):
        ProfileStore(restored).check_execution_ownership(workspace[2]['id'])


def test_transfer_requires_an_explicit_runtime_registration(workspace, tmp_path):
    key, archive, _ = snapshot(workspace, tmp_path)
    restored = backup.restore_snapshot(archive, tmp_path / 'restore', key)
    with pytest.raises(backup.BackupError, match='runtime_unregistered'):
        backup.activate_transfer(workspace[0].root, restored)


def test_failed_activation_commit_leaves_both_workspaces_fenced_and_can_resume(workspace, tmp_path, monkeypatch):
    from contextlib import contextmanager

    register_fixture(workspace, monkeypatch)
    key, archive, _ = snapshot(workspace, tmp_path)
    restored = backup.restore_snapshot(archive, tmp_path / 'restore', key)
    original_write = ProfileStore._write

    @contextmanager
    def blocked_write(store, **kwargs):
        if store.root == restored:
            raise StoreError('fixture_activation_commit_failure')
        with original_write(store, **kwargs) as transaction:
            yield transaction

    with monkeypatch.context() as context:
        context.setattr(ProfileStore, '_write', blocked_write)
        with pytest.raises(StoreError, match='activation_commit_failure'):
            backup.activate_transfer(workspace[0].root, restored)
    with pytest.raises(StoreError, match='retired'):
        workspace[0].check_execution_ownership(workspace[2]['id'])
    with pytest.raises(StoreError, match='quarantined'):
        ProfileStore(restored).check_execution_ownership(workspace[2]['id'])
    backup.activate_transfer(workspace[0].root, restored)
    ProfileStore(restored).check_execution_ownership(workspace[2]['id'])
