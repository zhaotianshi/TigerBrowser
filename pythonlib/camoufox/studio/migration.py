"""Authenticated, destination-bound handovers between stopped execution hosts."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from ..pkgman import ARCH_MAP
from ..utils import resolve_verstr
from . import backup, manifest
from .distribution import browser_executable, default_addon_paths, profile_addon_paths
from .store import ProfileStore, _json, validate_id
from .workspace import WorkspaceLock

RECORD_BYTES = 2 * backup.MANIFEST_BYTES


def _digest(record):
    return hashlib.sha256(_json(record, sort_keys=True).encode()).hexdigest()


def source_runtime(store, *, portable=True):
    """Capture actual source artifacts explicitly, without certifying legacy measurements."""
    manifest.verify(store)
    runtime = manifest.capture(store, create_host=True)
    defaults = {str(Path(path)): Path(path).name for path in default_addon_paths()}
    paths = {path: {'sha256': manifest._tree(path, portable=portable), 'default': name} for path, name in defaults.items()}
    for archived in (False, True):
        for profile in store.list_profiles(archived):
            identity = profile['identity']
            if not identity:
                continue
            saved = identity['config'].get('addons', [])
            resolved = profile_addon_paths(store, identity)
            if len(saved) != len(resolved):
                raise backup.BackupError('runtime_addon_mapping_required')
            for original, actual in zip(saved, resolved):
                paths[original] = {'sha256': manifest._tree(actual, portable=portable), 'default': defaults.get(actual)}
    return {**({'format': 2} if portable else {}), 'runtime': runtime, 'addon_paths': paths}


def _read(path, key, root, kind):
    with tempfile.TemporaryDirectory(prefix='.studio-migration-', dir=root) as temporary:
        plain = backup._decrypt(path, key, temporary, RECORD_BYTES)
        try:
            if plain.stat().st_size > RECORD_BYTES:
                raise backup.BackupError('migration_record_limit')
            record = json.loads(plain.read_bytes())
        except ValueError:
            raise backup.BackupError('invalid_migration_record') from None
    if not isinstance(record, dict) or record.get('format') != 1 or record.get('kind') != kind:
        raise backup.BackupError('invalid_migration_record')
    return record


def _destination(path, root):
    path = Path(path).absolute()
    if path.is_symlink() or not path.parent.is_dir() or path.resolve().is_relative_to(root):
        raise backup.BackupError('invalid_migration_destination')
    return path


def _publish(record, target, key, root):
    target = _destination(target, root)
    if target.exists():
        if _read(target, key, root, record['kind']) == record:
            return
        raise backup.BackupError('migration_destination_exists')
    encoded = _json(record, sort_keys=True).encode()
    if len(encoded) > RECORD_BYTES:
        raise backup.BackupError('migration_record_limit')
    with tempfile.TemporaryDirectory(prefix='.studio-migration-', dir=root) as temporary:
        plain = Path(temporary) / 'record'
        descriptor = os.open(plain, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encoded)
        encrypted = target.parent / ('.studio-encrypted-' + uuid4().hex)
        try:
            backup._encrypt(plain, encrypted, key)
            if _read(encrypted, key, root, record['kind']) != record:
                raise backup.BackupError('backup_verification_failed')
            os.link(encrypted, target)
        finally:
            encrypted.unlink(missing_ok=True)


def _root(root):
    root = Path(root).resolve()
    if not (root / 'studio.sqlite3').is_file():
        raise backup.BackupError('workspace_missing')
    backup._regular(root / 'studio.sqlite3')
    return root


def _quarantined(store):
    with store._connect() as connection:
        backup._clean(connection)
        barrier = json.loads(backup._metadata(connection, 'execution_barrier') or 'null')
        record = json.loads(backup._metadata(connection, 'restore_manifest') or 'null')
        target_id = backup._metadata(connection, 'workspace_id')
    if not record or not barrier or barrier != {'reason': 'restore_quarantine', 'snapshot_id': record['snapshot_id']}:
        raise backup.BackupError('not_quarantined')
    backup._verify_workspace(store, record)
    return record, target_id


def _runtime(store, settings, paths, mode):
    current = manifest.capture(store, create_host=True, executable_path=settings['executable_path'],
                               addon_paths={*default_addon_paths(), *paths.values()})
    current['mode'] = mode
    return current


def prepare(root, request_file, key, *, mode='desktop', executable_path=None, addon_paths=(),
            accept_platform_change=False):
    """Freeze an accepted destination runtime while keeping every browser quarantined."""
    backup._key(key)
    if mode not in ('desktop', 'headless'):
        raise backup.BackupError('invalid_migration_mode')
    root = _root(root)
    request_file = _destination(request_file, root)
    lock = WorkspaceLock(root)
    lock.acquire()
    try:
        store = ProfileStore(root)
        record, target_id = _quarantined(store)
        backup._verify_credentials(store)
        source = record.get('migration_runtime')
        if not source:
            raise backup.BackupError('migration_snapshot_required')
        if source.get('format') not in (None, 2):
            raise backup.BackupError('invalid_migration_record')
        portable = source.get('format') == 2
        overrides = dict(addon_paths)
        if len(overrides) != len(addon_paths) or set(overrides) - source['addon_paths'].keys():
            raise backup.BackupError('invalid_addon_mapping')
        defaults = {Path(path).name: path for path in default_addon_paths()}
        paths = {}
        for original, artifact in source['addon_paths'].items():
            selected = overrides.get(original, defaults.get(artifact['default']))
            if selected is None:
                raise backup.BackupError('runtime_addon_mapping_required')
            paths[original] = str(Path(selected).resolve())
            if manifest._tree(paths[original], portable=portable) != artifact['sha256']:
                raise backup.BackupError('runtime_addon_mismatch')
        settings = store.get_settings()
        settings['executable_path'] = str(browser_executable(executable_path))
        current = _runtime(store, settings, paths, mode)
        original = source['runtime']
        platform_change = current['host']['system'] != original['host']['system']
        if platform_change and accept_platform_change:
            if not portable:
                raise backup.BackupError('migration_snapshot_required')
            if set(defaults) != {item['default'] for item in source['addon_paths'].values() if item['default']}:
                raise backup.BackupError('migration_artifact_mismatch')
            architectures = [ARCH_MAP.get(item['host']['machine'].lower()) for item in (original, current)]
            if None in architectures or architectures[0] != architectures[1]:
                raise backup.BackupError('migration_platform_mismatch')
            versions = {profile['identity'].get('browser_version') for archived in (False, True)
                        for profile in store.list_profiles(archived) if profile['identity']}
            binary = Path(settings['executable_path'])
            try:
                selected_version = resolve_verstr(binary, require_executable_version=True)
            except ValueError:
                raise backup.BackupError('migration_artifact_mismatch') from None
            if versions and versions != {selected_version}:
                raise backup.BackupError('migration_artifact_mismatch')
            fields = ('model', 'packages')
        else:
            if any(current['host'][field] != original['host'][field] for field in ('system', 'machine')):
                raise backup.BackupError('migration_platform_mismatch')
            fields = ('browser', 'addons', 'model', 'packages')
        if any(current[field] != original[field] for field in fields):
            raise backup.BackupError('migration_artifact_mismatch')
        request = {'format': 1, 'kind': 'migration_request', 'snapshot': record, 'target_id': target_id,
                   'runtime': current, 'settings': settings, 'addon_paths': paths,
                   'platform_change': platform_change}
        with store._write() as (connection, _):
            previous = backup._metadata(connection, 'migration_request')
            if previous and json.loads(previous) != request:
                raise backup.BackupError('migration_already_prepared')
            connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('migration_request', ?)",
                               (_json(request),))
            if not previous:
                store._audit(connection, 'migration.prepared', target_kind='snapshot', target_id=record['snapshot_id'])
        _publish(request, request_file, key, store.root)
        return {'snapshot_id': record['snapshot_id'], 'target_id': target_id, 'mode': mode,
                'platform_change': platform_change,
                'changed_runtime_fields': sorted(field for field in current if current[field] != original.get(field))}
    finally:
        lock.release()


def _receipt(request):
    try:
        snapshot = request['snapshot']
        source_id, target_id = validate_id(snapshot['workspace_id']), validate_id(request['target_id'])
        snapshot_id = validate_id(snapshot['snapshot_id'])
        if (not isinstance(snapshot['files'], dict) or not isinstance(request['runtime'], dict)
                or not isinstance(snapshot['migration_runtime'], dict)):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise backup.BackupError('invalid_migration_record') from None
    return {'format': 1, 'kind': 'migration_receipt', 'snapshot_id': snapshot_id,
            'source_id': source_id, 'target_id': target_id, 'request_digest': _digest(request)}


def native_platform_accepted(store, profile):
    """Honor native host changes only for fixed profiles in an activated OS handover."""
    if profile['identity'] is None:
        return False
    with store._connect() as connection:
        request = json.loads(backup._metadata(connection, 'migration_request') or 'null')
        receipt = json.loads(backup._metadata(connection, 'active_restore_receipt') or 'null')
        profiles = backup._metadata(connection, 'migration_native_profiles')
        if profiles is None:
            profiles = backup._metadata(connection, 'migration_requires_attestation')
        profiles = json.loads(profiles or '[]')
        workspace_id = backup._metadata(connection, 'workspace_id')
        barrier = backup._metadata(connection, 'execution_barrier')
    if (barrier or not request or not request.get('platform_change') or not receipt
            or profile['id'] not in profiles or receipt != _receipt(request)
            or receipt['target_id'] != workspace_id):
        return False
    source = request['snapshot']['migration_runtime']
    systems = {'Windows': 'windows', 'Darwin': 'macos', 'Linux': 'linux'}
    return (source.get('format') == 2
            and systems.get(source['runtime']['host']['system']) == profile['os']
            and request['runtime']['host']['system'] == manifest.platform.system())


def retire(root, request_file, receipt_file, key):
    """Commit irreversible source retirement before publishing destination-bound evidence."""
    backup._key(key)
    root = _root(root)
    receipt_file = _destination(receipt_file, root)
    lock = WorkspaceLock(root)
    lock.acquire()
    try:
        store = ProfileStore(root)
        request = _read(request_file, key, store.root, 'migration_request')
        receipt = _receipt(request)
        record = request['snapshot']
        retirement = {'reason': 'retired', 'snapshot_id': receipt['snapshot_id'],
                      'target_id': receipt['target_id'], 'request_digest': receipt['request_digest']}
        with store._connect() as connection:
            backup._clean(connection)
            if backup._metadata(connection, 'workspace_id') != receipt['source_id']:
                raise backup.BackupError('source_workspace_mismatch')
            previous = json.loads(backup._metadata(connection, 'execution_barrier') or 'null')
            if previous and previous != retirement:
                raise backup.BackupError('source_retired')
        backup._verify_workspace(store, record)
        if source_runtime(store, portable=record['migration_runtime'].get('format') == 2) != record['migration_runtime']:
            raise backup.BackupError('runtime_mismatch')
        with store._write() as (connection, _):
            connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('execution_barrier', ?)",
                               (_json(retirement),))
            connection.execute('DELETE FROM team_http_sessions')
            connection.execute('DELETE FROM team_sessions')
            if not previous:
                store._audit(connection, 'migration.source_retired', target_kind='snapshot', target_id=receipt['snapshot_id'])
        _publish(receipt, receipt_file, key, store.root)
        return receipt
    finally:
        lock.release()


def activate(root, receipt_file, key):
    """Activate exactly the prepared destination after authenticated source retirement."""
    backup._key(key)
    root = _root(root)
    lock = WorkspaceLock(root)
    lock.acquire()
    try:
        store = ProfileStore(root)
        receipt = _read(receipt_file, key, store.root, 'migration_receipt')
        with store._connect() as connection:
            backup._clean(connection)
            request = json.loads(backup._metadata(connection, 'migration_request') or 'null')
            previous = json.loads(backup._metadata(connection, 'active_restore_receipt') or 'null')
            barrier = backup._metadata(connection, 'execution_barrier')
            target_id = backup._metadata(connection, 'workspace_id')
        if not request or receipt != _receipt(request) or receipt['target_id'] != target_id:
            raise backup.BackupError('migration_receipt_mismatch')
        if not barrier and previous == receipt:
            return store.root
        record, _ = _quarantined(store)
        if record != request['snapshot']:
            raise backup.BackupError('migration_receipt_mismatch')
        if _runtime(store, request['settings'], request['addon_paths'], request['runtime']['mode']) != request['runtime']:
            raise backup.BackupError('runtime_mismatch')
        backup._verify_credentials(store)
        pending = sorted(profile['id'] for archived in (False, True) for profile in store.list_profiles(archived)
                         if profile['identity'] is not None)
        with store._write() as (connection, _):
            certificates = dict(connection.execute(
                "SELECT key, value FROM workspace_metadata WHERE key LIKE 'remote_attestation:%'"))
            history = {'request_digest': receipt['request_digest'], 'runtime_manifest': record['runtime_manifest'],
                       'remote_attestations': {name: json.loads(value) for name, value in certificates.items()}}
            for name, value in (('runtime_manifest', request['runtime']), ('runtime_addon_paths', request['addon_paths']),
                                ('active_restore_receipt', receipt), ('migration_native_profiles', pending),
                                ('migration_history:' + receipt['snapshot_id'], history)):
                connection.execute('INSERT OR REPLACE INTO workspace_metadata VALUES (?, ?)', (name, _json(value)))
            connection.execute('INSERT OR REPLACE INTO settings (id, data) VALUES (1, ?)', (_json(request['settings']),))
            connection.execute("DELETE FROM workspace_metadata WHERE key LIKE 'remote_attestation:%'")
            connection.execute("DELETE FROM workspace_metadata WHERE key='migration_requires_attestation'")
            connection.execute("DELETE FROM workspace_metadata WHERE key='execution_barrier'")
            connection.execute('DELETE FROM team_http_sessions')
            connection.execute('DELETE FROM team_sessions')
            store._audit(connection, 'migration.activated', target_kind='snapshot', target_id=receipt['snapshot_id'])
        return store.root
    finally:
        lock.release()
