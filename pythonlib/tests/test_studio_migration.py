"""Stopped server handovers preserve identities and fence the retired source."""

import copy
import json
import os
from contextlib import contextmanager
from uuid import uuid4

import pytest
from cryptography.cobblestone import Cobblestone128Encryptor

from camoufox.studio import backup, distribution, manifest
from camoufox.studio.store import ProfileStore, StoreError


@pytest.fixture
def handover(tmp_path, monkeypatch):
    from camoufox.studio import migration
    from camoufox import multiversion
    from camoufox.pkgman import Version
    from camoufox.studio import kernels

    hosts = {}
    for name in ('old', 'new'):
        root = tmp_path / name
        root.mkdir()
        browser = root / 'browser'
        browser.mkdir()
        (browser / 'camoufox').write_bytes(b'identical-browser')
        (browser / 'application.ini').write_text('[App]\nVersion=152.0.4\n')
        addon = root / 'UBO'
        addon.mkdir()
        (addon / 'manifest.json').write_text('{}')
        fonts = root / 'windows/Fonts'
        fonts.mkdir(parents=True)
        (fonts / 'synthetic.ttf').write_bytes(b'identical-native-font')
        hosts[name] = {'root': root, 'binary': browser / 'camoufox', 'addon': addon}
    selected = [hosts['old']]
    monkeypatch.setattr(multiversion, 'list_installed', lambda: [multiversion.InstalledVersion(
        'official', Version('beta.31', '152.0.4'), selected[0]['binary'].parent)])
    monkeypatch.setattr(kernels, 'launch_path', lambda path: str(path / 'camoufox'))

    def host(name):
        selected[0] = hosts[name]
        monkeypatch.setattr(manifest, 'INSTALL_DIR', hosts[name]['root'] / 'cache')
        monkeypatch.setenv('WINDIR', str(hosts[name]['root'] / 'windows'))
        monkeypatch.setenv('LOCALAPPDATA', str(hosts[name]['root'] / 'local'))

    monkeypatch.setattr(manifest, 'is_pinned', lambda: True)
    monkeypatch.setattr(manifest.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(manifest, 'default_addon_paths', lambda: [str(selected[0]['addon'])])
    monkeypatch.setattr(distribution, 'default_addon_paths', lambda: [str(selected[0]['addon'])])
    monkeypatch.setattr(migration, 'default_addon_paths', lambda: [str(selected[0]['addon'])])
    host('old')
    store = ProfileStore(hosts['old']['root'] / 'workspace')
    store.save_settings({'executable_path': str(hosts['old']['binary'])})
    store.initialize_team(store.begin_team_setup(), 'admin', 'Admin', 'synthetic-password')
    profile = store.save_profile({'name': 'Migrating environment'})
    identity = {'browser_version': '152.0.4', 'config': {'addons': [str(hosts['old']['addon'])],
                'navigator.platform': 'Win32', 'audio:seed': 1234}, 'options': {}}
    store.set_identity(profile['id'], identity)
    directory = store.profile_dir(profile['id'])
    directory.mkdir(parents=True)
    (directory / 'cookies.sqlite').write_bytes(b'synthetic-cookie-data')
    key = Cobblestone128Encryptor.generate_key()
    archive = tmp_path / 'snapshot'
    record = backup.create_snapshot(store.root, archive, key, for_migration=True)
    host('new')
    restored = backup.restore_snapshot(archive, hosts['new']['root'] / 'restore', key)
    request, receipt = tmp_path / 'request', tmp_path / 'receipt'
    migration.prepare(restored, request, key, executable_path=hosts['new']['binary'])
    return migration, store, ProfileStore(restored), profile['id'], key, request, receipt, host, hosts, record


def retire(handover):
    migration, original, _, _, key, request, receipt, host, _, _ = handover
    host('old')
    migration.retire(original.root, request, receipt, key)
    host('new')


def test_new_host_activation_preserves_identity_data_and_member_login(handover):
    migration, original, restored, profile_id, key, request, receipt, host, hosts, _ = handover
    identity = copy.deepcopy(original.get_profile(profile_id)['identity'])
    old_login = original.login('admin', 'synthetic-password')
    with pytest.raises(StoreError, match='quarantined'):
        restored.check_execution_ownership(profile_id)
    retire(handover)
    assert original.resolve_session(old_login['session_id']) is None
    with pytest.raises(StoreError, match='retired'):
        original.check_execution_ownership(profile_id)
    assert migration.activate(restored.root, receipt, key) == restored.root
    assert migration.activate(restored.root, receipt, key) == restored.root
    restored.check_execution_ownership(profile_id)
    restored.check_runtime(headless=False)
    restored.check_runtime(headless=True)
    assert restored.get_profile(profile_id)['identity'] == identity
    assert (restored.profile_dir(profile_id) / 'cookies.sqlite').read_bytes() == b'synthetic-cookie-data'
    assert restored.login('admin', 'synthetic-password')['member']['name'] == 'admin'
    assert restored.get_settings()['executable_path'] == str(hosts['new']['binary'])
    assert distribution.profile_addon_paths(restored, identity) == [str(hosts['new']['addon'])]
    host('old')
    with pytest.raises(StoreError):
        restored.check_runtime(headless=False)


def test_a_receipt_cannot_activate_a_second_restore(handover, tmp_path):
    migration, original, restored, _, key, request, receipt, host, hosts, _ = handover
    second = backup.restore_snapshot(tmp_path / 'snapshot', tmp_path / 'second', key)
    second_request = tmp_path / 'second-request'
    migration.prepare(second, second_request, key, executable_path=hosts['new']['binary'])
    retire(handover)
    with pytest.raises(backup.BackupError, match='migration_receipt_mismatch'):
        migration.activate(second, receipt, key)
    host('old')
    with pytest.raises(backup.BackupError, match='source_retired'):
        migration.retire(original.root, second_request, tmp_path / 'second-receipt', key)
    host('new')
    migration.activate(restored.root, receipt, key)


@pytest.mark.parametrize('change', ['source_data', 'source_settings', 'source_runtime', 'target_data', 'target_runtime'])
def test_changed_inputs_never_complete_a_handover(handover, change):
    migration, original, restored, profile_id, key, request, receipt, host, hosts, _ = handover
    if change.startswith('source'):
        if change == 'source_data':
            (original.profile_dir(profile_id) / 'cookies.sqlite').write_bytes(b'newer-data')
        elif change == 'source_settings':
            original.save_settings({'max_running': 7})
        else:
            hosts['old']['binary'].write_bytes(b'changed-source-browser')
        host('old')
        with pytest.raises(backup.BackupError, match='runtime_mismatch' if change == 'source_runtime' else 'workspace_changed'):
            migration.retire(original.root, request, receipt, key)
        original.check_execution_ownership(profile_id)
        assert not receipt.exists()
    else:
        retire(handover)
        if change == 'target_data':
            (restored.profile_dir(profile_id) / 'cookies.sqlite').write_bytes(b'changed-copy')
        else:
            hosts['new']['binary'].write_bytes(b'changed-browser')
        with pytest.raises(backup.BackupError):
            migration.activate(restored.root, receipt, key)
    with pytest.raises(StoreError, match='quarantined'):
        restored.check_execution_ownership(profile_id)


def test_receipt_publication_failure_can_resume_only_the_same_handover(handover, monkeypatch):
    migration, original, restored, _, key, request, receipt, host, _, _ = handover
    host('old')
    with monkeypatch.context() as context:
        context.setattr(migration, '_publish', lambda *args, **kwargs: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            migration.retire(original.root, request, receipt, key)
    with pytest.raises(StoreError, match='retired'):
        original.check_execution_ownership(handover[3])
    migration.retire(original.root, request, receipt, key)
    host('new')
    migration.activate(restored.root, receipt, key)


def test_failed_target_commit_leaves_both_ends_fenced(handover, monkeypatch):
    migration, original, restored, profile_id, key, _, receipt, _, _, _ = handover
    retire(handover)
    write = ProfileStore._write

    @contextmanager
    def fail(store, **kwargs):
        if store.root == restored.root:
            raise StoreError('activation_commit_failure')
        with write(store, **kwargs) as transaction:
            yield transaction

    with monkeypatch.context() as context:
        context.setattr(ProfileStore, '_write', fail)
        with pytest.raises(StoreError, match='activation_commit_failure'):
            migration.activate(restored.root, receipt, key)
    for store, reason in ((original, 'retired'), (restored, 'quarantined')):
        with pytest.raises(StoreError, match=reason):
            store.check_execution_ownership(profile_id)
    migration.activate(restored.root, receipt, key)


def test_forged_retirement_receipt_keeps_the_target_quarantined(handover):
    migration, _, restored, profile_id, key, _, receipt, _, _, _ = handover
    retire(handover)
    data = receipt.read_bytes()
    receipt.write_bytes(data[:-1])
    with pytest.raises(backup.BackupError, match='authentication_failed'):
        migration.activate(restored.root, receipt, key)
    with pytest.raises(StoreError, match='quarantined'):
        restored.check_execution_ownership(profile_id)


def test_migration_activation_does_not_add_a_native_reacceptance_gate(handover):
    from camoufox.studio import attestation

    migration, _, restored, profile_id, key, _, receipt, _, _, _ = handover
    with restored._write() as (connection, _):
        connection.execute('INSERT INTO workspace_metadata VALUES (?, ?)',
                           ('remote_attestation:' + profile_id, json.dumps({'format': 1, 'digest': 'old-host'})))
    retire(handover)
    migration.activate(restored.root, receipt, key)
    assert attestation.certificate(restored, profile_id) is None
    assert attestation.check(restored, restored.get_profile(profile_id)) is None
    with restored._connect() as connection:
        migrated = connection.execute(
            "SELECT value FROM workspace_metadata WHERE key='migration_native_profiles'").fetchone()
        obsolete = connection.execute(
            "SELECT 1 FROM workspace_metadata WHERE key='migration_requires_attestation'").fetchone()
    assert json.loads(migrated[0]) == [profile_id]
    assert obsolete is None


def test_new_profiles_keep_their_existing_preview_attestation_policy(handover):
    from camoufox.studio import attestation

    migration, _, restored, _, key, _, receipt, _, _, _ = handover
    retire(handover)
    migration.activate(restored.root, receipt, key)
    profile = restored.save_profile({'name': 'Created after migration'})
    assert attestation.check(restored, profile) is None
    with pytest.raises(StoreError, match='attestation_required'):
        attestation.check(restored, profile, required=True)


def test_unresolved_source_run_cannot_be_retired(handover):
    migration, original, _, profile_id, key, request, receipt, host, _, _ = handover
    with original._write() as (connection, _):
        connection.execute('INSERT INTO team_runs VALUES (?, ?, ?, ?, ?)',
                           (str(uuid4()), profile_id, 'old-owner', 'recovery_required', '{}'))
    host('old')
    with pytest.raises(backup.BackupError, match='recovery_required'):
        migration.retire(original.root, request, receipt, key)
    assert not receipt.exists()


@pytest.mark.parametrize('change', ['browser', 'addon', 'platform'])
def test_incompatible_destination_is_rejected_before_retiring_source(handover, tmp_path, monkeypatch, change):
    migration, original, restored, profile_id, key, _, _, _, hosts, _ = handover
    if change == 'browser':
        hosts['new']['binary'].write_bytes(b'wrong-version-or-content')
    elif change == 'addon':
        (hosts['new']['addon'] / 'manifest.json').write_text('{"changed":true}')
    else:
        monkeypatch.setattr(manifest.platform, 'machine', lambda: 'incompatible-architecture')
    with pytest.raises(backup.BackupError, match='mismatch'):
        migration.prepare(restored.root, tmp_path / 'new-request', key, executable_path=hosts['new']['binary'])
    original.check_execution_ownership(profile_id)
    with pytest.raises(StoreError, match='quarantined'):
        restored.check_execution_ownership(profile_id)
    assert not (tmp_path / 'new-request').exists()


@pytest.mark.parametrize('change', ['accepted', 'architecture', 'version', 'version_metadata', 'same_platform'])
def test_platform_change_requires_matching_architecture_and_saved_browser_version(
        handover, tmp_path, monkeypatch, change):
    migration, original, _, profile_id, key, _, _, host, hosts, _ = handover
    restored = backup.restore_snapshot(tmp_path / 'snapshot', tmp_path / 'platform-restore', key)
    hosts['new']['binary'].write_bytes(b'platform-specific-browser')
    if change != 'same_platform':
        monkeypatch.setattr(manifest.platform, 'system', lambda: 'Darwin')
    if change == 'accepted':
        capture = manifest.capture
        def native_order(*args, **kwargs):
            result = capture(*args, **kwargs)
            if result['host']['system'] == 'Darwin':
                result['addons'] = ['different-native-path-order']
            return result
        monkeypatch.setattr(manifest, 'capture', native_order)
    if change == 'architecture':
        monkeypatch.setattr(manifest.platform, 'machine', lambda: 'incompatible-architecture')
    if change == 'version':
        (hosts['new']['binary'].parent / 'application.ini').write_text('[App]\nVersion=153.0.0\n')
    if change == 'version_metadata':
        from camoufox import utils
        (hosts['new']['binary'].parent / 'application.ini').write_text('[App]\n')
        monkeypatch.setattr(utils, 'installed_verstr', lambda: '152.0.4')
    request, receipt = tmp_path / 'platform-request', tmp_path / 'platform-receipt'
    if change != 'accepted':
        with pytest.raises(backup.BackupError, match='mismatch'):
            migration.prepare(restored, request, key, executable_path=hosts['new']['binary'],
                              accept_platform_change=True)
        assert not request.exists()
        original.check_execution_ownership(profile_id)
        return
    with pytest.raises(backup.BackupError, match='migration_platform_mismatch'):
        migration.prepare(restored, request, key, executable_path=hosts['new']['binary'])
    result = migration.prepare(restored, request, key, executable_path=hosts['new']['binary'],
                               accept_platform_change=True)
    assert result['platform_change'] is True
    with monkeypatch.context() as source:
        source.setattr(manifest.platform, 'system', lambda: 'Linux')
        host('old')
        migration.retire(original.root, request, receipt, key)
    host('new')
    migration.activate(restored, receipt, key)
    target = ProfileStore(restored)
    assert target.get_profile(profile_id)['identity'] == original.get_profile(profile_id)['identity']
    assert (target.profile_dir(profile_id) / 'cookies.sqlite').read_bytes() == b'synthetic-cookie-data'
    with pytest.raises(StoreError, match='retired'):
        original.check_execution_ownership(profile_id)


def test_native_secret_must_be_reentered_before_preparing_migration(handover, tmp_path, monkeypatch):
    from camoufox.studio.secrets import KeyringSecretStore, SecretStoreError

    migration, original, _, profile_id, key, _, _, host, hosts, _ = handover
    secrets = {}
    monkeypatch.setattr(KeyringSecretStore, 'get', lambda self, name: secrets.get((self.namespace, name)))
    monkeypatch.setattr(KeyringSecretStore, 'set', lambda self, name, value: secrets.__setitem__((self.namespace, name), value))
    host('old')
    proxy = original.save_proxy({'name': 'Route', 'server': 'http://127.0.0.1:8080', 'password': 'synthetic-secret'})
    original.save_profile({'proxy_id': proxy['id']}, profile_id)
    archive = tmp_path / 'with-secret'
    backup.create_snapshot(original.root, archive, key, for_migration=True)
    host('new')
    restored = backup.restore_snapshot(archive, tmp_path / 'secret-restore', key)
    request = tmp_path / 'secret-request'
    with pytest.raises(SecretStoreError, match='missing'):
        migration.prepare(restored, request, key, executable_path=hosts['new']['binary'])
    assert not request.exists()
    target = ProfileStore(restored)
    target.save_proxy({'password': 'synthetic-secret'}, proxy['id'])
    migration.prepare(restored, request, key, executable_path=hosts['new']['binary'])
    assert request.exists()


@pytest.mark.parametrize('invalid', [None, 'unactivated', 'receipt', 'new_profile', 'other_os', 'new_identity'])
def test_native_modes_follow_only_an_accepted_source_platform(handover, tmp_path, monkeypatch, invalid):
    from camoufox.studio import engine

    migration, original, _, profile_id, key, _, _, host, hosts, _ = handover
    host('old')
    monkeypatch.setattr(manifest.platform, 'system', lambda: 'Windows')
    identity = original.get_profile(profile_id)['identity']
    original.save_profile({'os': 'windows', 'fingerprint': {'fonts_mode': 'native'}}, profile_id)
    identity['options'] = {'firefox_user_prefs': {}, 'args': []}
    original.set_identity(profile_id, identity)
    backup.create_snapshot(original.root, tmp_path / 'native-snapshot', key, for_migration=True)
    host('new')
    monkeypatch.setattr(manifest.platform, 'system', lambda: 'Linux')
    restored = backup.restore_snapshot(tmp_path / 'native-snapshot', tmp_path / 'native-restore', key)
    request, receipt = tmp_path / 'native-request', tmp_path / 'native-receipt'
    migration.prepare(restored, request, key, executable_path=hosts['new']['binary'], accept_platform_change=True)
    with monkeypatch.context() as source:
        source.setattr(manifest.platform, 'system', lambda: 'Windows')
        host('old')
        migration.retire(original.root, request, receipt, key)
    host('new')
    target = ProfileStore(restored)
    if invalid != 'unactivated':
        migration.activate(restored, receipt, key)
    if invalid == 'receipt':
        with target._write() as (connection, _):
            connection.execute("UPDATE workspace_metadata SET value='{}' WHERE key='active_restore_receipt'")
    if invalid == 'new_profile':
        profile_id = target.clone_profile(profile_id)['id']
        target.set_identity(profile_id, identity)
    if invalid == 'other_os':
        target.save_profile({'os': 'macos'}, profile_id)
        target.set_identity(profile_id, identity)
    if invalid == 'new_identity':
        target.set_identity(profile_id, None)
    profile = target.get_profile(profile_id)
    saved = copy.deepcopy(profile)
    monkeypatch.setattr(engine, 'validate_config', lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, 'resolve_verstr', lambda *_: '152.0.4')
    monkeypatch.setattr(engine, 'get_env_vars', lambda config, *args, **kwargs: {'CAMOU_CONFIG_1': json.dumps(config)})
    monkeypatch.setattr(engine, 'get_pref_env_vars', lambda *_: {})
    monkeypatch.setattr('camoufox.studio.fingerprint_options.sys.platform', 'linux')
    if invalid:
        with pytest.raises(engine.StudioError):
            engine.prepare_launch(target, profile)
    else:
        launch = engine.prepare_launch(target, profile)
        assert 'fonts' not in json.loads(launch['env']['CAMOU_CONFIG_1'])
        assert target.get_profile(profile_id) == saved
        assert target.get_profile(profile_id)['identity'] == identity


@pytest.mark.parametrize('platform_change', [False, True])
def test_legacy_migration_records_remain_limited_to_same_platform(handover, tmp_path, monkeypatch, platform_change):
    migration, original, _, profile_id, key, _, _, host, hosts, _ = handover
    host('old')
    capture = migration.source_runtime
    with monkeypatch.context() as legacy:
        legacy.setattr(migration, 'source_runtime', lambda store: capture(store, portable=False))
        record = backup.create_snapshot(original.root, tmp_path / 'legacy-snapshot', key, for_migration=True)
    assert 'format' not in record['migration_runtime']
    host('new')
    restored = backup.restore_snapshot(tmp_path / 'legacy-snapshot', tmp_path / 'legacy-restore', key)
    request, receipt = tmp_path / 'legacy-request', tmp_path / 'legacy-receipt'
    if platform_change:
        monkeypatch.setattr(manifest.platform, 'system', lambda: 'Darwin')
        with pytest.raises(backup.BackupError, match='migration_snapshot_required'):
            migration.prepare(restored, request, key, executable_path=hosts['new']['binary'], accept_platform_change=True)
        assert not request.exists()
        original.check_execution_ownership(profile_id)
        return
    migration.prepare(restored, request, key, executable_path=hosts['new']['binary'])
    host('old')
    migration.retire(original.root, request, receipt, key)
    host('new')
    migration.activate(restored, receipt, key)
    ProfileStore(restored).check_execution_ownership(profile_id)


def test_maintenance_cli_completes_the_three_server_handover_steps(handover, tmp_path):
    from click.testing import CliRunner
    from camoufox.__main__ import cli

    _, original, restored, profile_id, key, _, _, host, hosts, _ = handover
    vault, transfers = tmp_path / 'vault', tmp_path / 'transfers'
    vault.mkdir()
    transfers.mkdir()
    key_file = vault / 'key'
    descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(key)
    request, receipt = transfers / 'request', transfers / 'receipt'
    runner = CliRunner()
    result = runner.invoke(cli, ['studio-migration-prepare', '--restored', str(restored.root),
        '--output', str(request), '--key-file', str(key_file), '--executable-path', str(hosts['new']['binary']),
        '--confirm-current-runtime'])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['state'] == 'quarantined'
    host('old')
    result = runner.invoke(cli, ['studio-migration-retire', '--source', str(original.root),
        '--request', str(request), '--output', str(receipt), '--key-file', str(key_file), '--retire-source'])
    assert result.exit_code == 0, result.output
    host('new')
    result = runner.invoke(cli, ['studio-migration-activate', '--restored', str(restored.root),
        '--receipt', str(receipt), '--key-file', str(key_file)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)['state'] == 'active'
    restored.check_execution_ownership(profile_id)
    assert key.hex() not in result.output


def test_an_activated_environment_can_migrate_again(handover, tmp_path):
    migration, original, restored, profile_id, key, _, receipt, host, hosts, _ = handover
    retire(handover)
    migration.activate(restored.root, receipt, key)
    third = tmp_path / 'third'
    third.mkdir()
    browser, addon = third / 'browser', third / 'UBO'
    browser.mkdir()
    addon.mkdir()
    (browser / 'camoufox').write_bytes(hosts['new']['binary'].read_bytes())
    (browser / 'application.ini').write_bytes((hosts['new']['binary'].parent / 'application.ini').read_bytes())
    (addon / 'manifest.json').write_bytes((hosts['new']['addon'] / 'manifest.json').read_bytes())
    hosts['third'] = {'root': third, 'binary': browser / 'camoufox', 'addon': addon}
    archive = tmp_path / 'second-snapshot'
    backup.create_snapshot(restored.root, archive, key, for_migration=True)
    host('third')
    second = backup.restore_snapshot(archive, third / 'restore', key)
    request, receipt = tmp_path / 'next-request', tmp_path / 'next-receipt'
    migration.prepare(second, request, key, executable_path=hosts['third']['binary'])
    host('new')
    migration.retire(restored.root, request, receipt, key)
    host('third')
    migration.activate(second, receipt, key)
    target = ProfileStore(second)
    target.check_runtime(headless=False)
    target.check_execution_ownership(profile_id)
    assert target.get_profile(profile_id)['identity'] == original.get_profile(profile_id)['identity']
    assert distribution.profile_addon_paths(target, target.get_profile(profile_id)['identity']) == [str(addon)]
    for store in (original, restored):
        with pytest.raises(StoreError, match='retired'):
            store.check_execution_ownership(profile_id)


def test_custom_addons_need_explicit_identical_path_mappings(handover, tmp_path):
    migration, original, _, profile_id, key, _, _, host, hosts, _ = handover
    source_addon, target_addon = tmp_path / 'old-custom', tmp_path / 'new-custom'
    for path in (source_addon, target_addon):
        path.mkdir()
        (path / 'manifest.json').write_text('{"name":"Synthetic custom extension"}')
    host('old')
    identity = original.get_profile(profile_id)['identity']
    identity['config']['addons'].append(str(source_addon))
    original.set_identity(profile_id, identity)
    archive = tmp_path / 'custom-snapshot'
    backup.create_snapshot(original.root, archive, key, for_migration=True)
    host('new')
    restored = backup.restore_snapshot(archive, tmp_path / 'custom-restore', key)
    request = tmp_path / 'custom-request'
    with pytest.raises(backup.BackupError, match='runtime_addon_mapping_required'):
        migration.prepare(restored, request, key, executable_path=hosts['new']['binary'])
    migration.prepare(restored, request, key, executable_path=hosts['new']['binary'],
                      addon_paths=((str(source_addon), str(target_addon)),))
    host('old')
    receipt = tmp_path / 'custom-receipt'
    migration.retire(original.root, request, receipt, key)
    host('new')
    migration.activate(restored, receipt, key)
    target = ProfileStore(restored)
    assert distribution.profile_addon_paths(target, identity) == [str(hosts['new']['addon']), str(target_addon)]
    assert target.get_profile(profile_id)['identity'] == identity
    target.check_runtime(headless=False)


def test_headless_migration_cannot_be_silently_launched_with_windows(handover, tmp_path):
    migration, original, _, profile_id, key, _, _, host, hosts, _ = handover
    restored = backup.restore_snapshot(tmp_path / 'snapshot', tmp_path / 'headless-restore', key)
    request, receipt = tmp_path / 'headless-request', tmp_path / 'headless-receipt'
    migration.prepare(restored, request, key, mode='headless', executable_path=hosts['new']['binary'])
    host('old')
    migration.retire(original.root, request, receipt, key)
    host('new')
    migration.activate(restored, receipt, key)
    target = ProfileStore(restored)
    target.check_execution_ownership(profile_id)
    target.check_runtime(headless=True)
    with pytest.raises(StoreError, match='runtime_mismatch'):
        target.check_runtime(headless=False)
