"""Authenticated stopped-workspace snapshots with bounded, quarantined restoration."""

import hashlib
import json
import os
import platform
import re
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import ExitStack, closing
from itertools import chain
from pathlib import Path
from uuid import uuid4

from cryptography.cobblestone import Cobblestone128Decryptor, Cobblestone128Encryptor
from cryptography.exceptions import InvalidTag

from ..fpgen_model import _hash
from .store import ProfileStore, SCHEMA_VERSION, TEAM_SCHEMA_VERSION, StoreError, _json, _now, validate_id
from .workspace import WorkspaceLock

MAGIC = b'Camoufox-Studio-Backup\x00\x01'
MAX_BYTES = 32 * 1024**3
MAX_FILES = 100_000
MANIFEST_BYTES = 16 * 1024**2
BLOCK = 1024**2
_TRANSIENT = {'workspace.lock', 'studio.sqlite3-wal', 'studio.sqlite3-shm',
              'automation.json', 'automation.json.tmp', 'team_setup.code'}
LOCK_NAMES = frozenset({'parent.lock', '.parentlock', 'lock'})
_CORE_TABLES = ('profiles', 'proxies', 'settings', 'identity_history', 'team_members',
                'team_groups', 'team_group_members', 'team_grants', 'team_group_grants')


class BackupError(ValueError):
    """A snapshot cannot be safely published or used."""


def checked_name(name):
    """Use one portable namespace, including when Linux archives are read on Windows."""
    if not isinstance(name, str) or len(name) > 1024:
        raise BackupError('invalid_archive_path')
    for part in name.split('/'):
        if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF
                       or char in '\\:*?"<>|\x7f' for char in part)
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
            raise BackupError('invalid_archive_path')
    return name


def _regular(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise BackupError('unsafe_workspace_file')
    return info


def _directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise BackupError('unsafe_workspace_file')


def profile_files(directory, *, skip=frozenset(), max_bytes=MAX_BYTES, max_files=MAX_FILES):
    """Hash one profile's portable regular files; skipped top-level names are never inspected."""
    directory = Path(directory)
    _directory(directory)
    result, folded, total = {}, set(), 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        top = Path(parent) == directory
        if top:
            dirs[:] = [name for name in dirs if name not in skip]
            files = [name for name in files if name not in skip]
        for name in [*dirs, *files]:
            path = Path(parent) / name
            relative = checked_name(path.relative_to(directory).as_posix())
            info = path.lstat()
            if relative.casefold() in folded:
                raise BackupError('archive_name_collision')
            folded.add(relative.casefold())
            if top and name in LOCK_NAMES:
                # Firefox's Unix lock can be a dangling PID symlink; never read or archive its target.
                if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                    continue
                raise BackupError('unsafe_workspace_file')
            if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
                    & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise BackupError('unsafe_workspace_file')
            if stat.S_ISDIR(info.st_mode):
                continue
            _regular(path)
            total += info.st_size
            if total > max_bytes:
                raise BackupError('size_limit')
            result[relative] = {'size': info.st_size, 'sha256': _hash(path)}
            if len(result) >= max_files:
                raise BackupError('file_limit')
    return result


def _profiles(root, ids, *, max_bytes=MAX_BYTES):
    result, total = {}, 0
    directory = root / 'profiles'
    if not os.path.lexists(directory):
        return result
    _directory(directory)
    for entry in directory.iterdir():
        if checked_name(entry.name) not in ids:
            raise BackupError('unknown_profile_directory')
        files = profile_files(entry, max_bytes=max_bytes - total, max_files=MAX_FILES - len(result))
        total += sum(item['size'] for item in files.values())
        result.update({f'profiles/{entry.name}/{name}': item for name, item in files.items()})
    return result


def _recovery(root):
    from .recovery import parse_record

    directory = root / 'recovery'
    if not os.path.lexists(directory):
        return {}
    result = {}
    try:
        for folder in chain([directory], directory.iterdir()):
            info = folder.lstat()
            if (not stat.S_ISDIR(info.st_mode) or folder.is_symlink()
                    or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise ValueError
            if folder == directory:
                continue
            entries = {path.name for path in folder.iterdir()}
            if (not re.fullmatch(r'[0-9a-f]{32}\.lock', folder.name)
                    or entries not in ({'owner.json'}, {'owner.json', 'recovering'})):
                raise ValueError
            if 'recovering' in entries:
                reservation = folder / 'recovering'
                info = reservation.lstat()
                if (not stat.S_ISDIR(info.st_mode) or reservation.is_symlink()
                        or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                        or any(reservation.iterdir())):
                    raise ValueError
            path = folder / 'owner.json'
            info = _regular(path)
            if info.st_size > 16384 or parse_record(path.read_bytes())['owner'] != folder.name[:-5]:
                raise ValueError
            result[path.relative_to(root).as_posix()] = {'size': info.st_size, 'sha256': _hash(path)}
            if len(result) >= MAX_FILES:
                raise ValueError
    except (OSError, ValueError, StoreError):
        raise BackupError('unsafe_recovery_receipt') from None
    return result


def _workspace_files(root, ids, max_bytes):
    files = {**_profiles(root, ids, max_bytes=max_bytes), **_recovery(root)}
    if len(files) >= MAX_FILES:
        raise BackupError('file_limit')
    return files


def _core_digest(connection):
    digest = hashlib.sha256()
    for table in _CORE_TABLES:
        rows = []
        for row in connection.execute('SELECT * FROM ' + table):
            values = list(row)
            if table == 'proxies':
                data = json.loads(values[1])
                # Re-entering an excluded native credential changes its timestamp, not the routing contract.
                data.pop('updated_at', None)
                values[1] = _json(data, sort_keys=True)
            rows.append(_json(values))
        rows.sort()
        digest.update(_json([table, rows]).encode())
    default = _metadata(connection, 'default_kernel')
    if default is not None:
        digest.update(_json(['default_kernel', default]).encode())
    return digest.hexdigest()


def _metadata(connection, key):
    row = connection.execute('SELECT value FROM workspace_metadata WHERE key = ?', (key,)).fetchone()
    return row[0] if row else None


def _clean(connection):
    if connection.execute("SELECT 1 FROM team_runs WHERE state != 'stopped'").fetchone():
        raise BackupError('recovery_required')
    # A snapshot would capture data that the lease holder is about to replace.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'team_checkouts'").fetchone() and (
            connection.execute("SELECT 1 FROM team_checkouts WHERE state IN ('checked_out', 'returning')").fetchone()
            or connection.execute("SELECT 1 FROM workspace_metadata WHERE key LIKE 'checkout_commit:%'").fetchone()):
        raise BackupError('checkout_active')
    if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise BackupError('database_integrity')
    if connection.execute('PRAGMA foreign_key_check').fetchone():
        raise BackupError('database_integrity')
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchone():
        raise BackupError('unsupported_database_schema')


def _verify_workspace(store, record):
    with store._connect() as connection:
        _clean(connection)
        if _core_digest(connection) != record['core_digest']:
            raise BackupError('workspace_changed')
        ids = {row[0] for row in connection.execute('SELECT id FROM profiles')}
    expected = {name: item for name, item in record['files'].items() if name != 'studio.sqlite3'}
    if _workspace_files(store.root, ids, MAX_BYTES) != expected:
        raise BackupError('workspace_changed')


def _verify_credentials(store):
    for proxy in store.list_proxies():
        store.get_proxy(proxy['id'], reveal_password=True)


def _key(key):
    if not isinstance(key, bytes) or len(key) != 16:
        raise BackupError('invalid_backup_key')


def _encrypt(plain, target, key):
    _key(key)
    encryptor = Cobblestone128Encryptor(key, context=MAGIC)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output, Path(plain).open('rb') as source:
        output.write(MAGIC)
        for block in iter(lambda: source.read(BLOCK), b''):
            output.write(encryptor.update(block))
        output.write(encryptor.finalize())
        output.flush()
        os.fsync(output.fileno())


def _decrypt(archive, key, parent, max_bytes):
    _key(key)
    archive = Path(archive)
    _regular(archive)
    if archive.stat().st_size > max_bytes + max_bytes // 100 + MANIFEST_BYTES:
        raise BackupError('size_limit')
    descriptor, name = tempfile.mkstemp(prefix='.studio-plain-', dir=parent)
    target = Path(name)
    try:
        decryptor = Cobblestone128Decryptor(key, context=MAGIC)
        with os.fdopen(descriptor, 'wb') as output, archive.open('rb') as source:
            if source.read(len(MAGIC)) != MAGIC:
                raise BackupError('unsupported_backup_format')
            count = 0
            for block in iter(lambda: source.read(BLOCK), b''):
                plain = decryptor.update(block)
                count += len(plain)
                if count > max_bytes + MANIFEST_BYTES:
                    raise BackupError('size_limit')
                output.write(plain)
            final = decryptor.finalize()
            if count + len(final) > max_bytes + MANIFEST_BYTES:
                raise BackupError('size_limit')
            output.write(final)
        return target
    except BaseException as error:
        target.unlink(missing_ok=True)
        if isinstance(error, InvalidTag):
            raise BackupError('backup_authentication_failed') from None
        raise


def create_snapshot(root, target, key, *, max_bytes=MAX_BYTES, for_migration=False):
    """Hold the common workspace lock for the whole stopped snapshot and verification."""
    _key(key)
    if Path(target).is_symlink():
        raise BackupError('invalid_backup_destination')
    root, target = Path(root).resolve(), Path(target).resolve()
    if not (root / 'studio.sqlite3').is_file():
        raise BackupError('workspace_missing')
    if target.exists() or target.is_relative_to(root) or not target.parent.is_dir():
        raise BackupError('invalid_backup_destination')
    lock = WorkspaceLock(root)
    lock.acquire()
    try:
        _regular(root / 'studio.sqlite3')
        store = ProfileStore(root)
        for path in root.iterdir():
            if path.name not in {'studio.sqlite3', 'profiles', 'recovery', *_TRANSIENT}:
                raise BackupError('unknown_workspace_file')
        _regular(store.database_path)
        with store._connect() as connection:
            _clean(connection)
            migration_runtime = None
            if for_migration:
                from .migration import source_runtime
                if _metadata(connection, 'execution_barrier'):
                    raise BackupError('workspace_quarantined')
                migration_runtime = source_runtime(store)
            ids = {row[0] for row in connection.execute('SELECT id FROM profiles')}
            files = _workspace_files(root, ids, max_bytes)
            if sum(item['size'] for item in files.values()) > max_bytes:
                raise BackupError('size_limit')
            manifest = {'format': 2 if any(name.startswith('recovery/') for name in files) else 1,
                        'snapshot_id': str(uuid4()), 'created_at': _now(),
                        'workspace_id': _metadata(connection, 'workspace_id'),
                        'schema': SCHEMA_VERSION, 'team_schema': TEAM_SCHEMA_VERSION,
                        'host': {'system': platform.system(), 'machine': platform.machine()},
                        'core_digest': _core_digest(connection), 'files': files,
                        'runtime_manifest': json.loads(_metadata(connection, 'runtime_manifest') or 'null'),
                        'migration_runtime': migration_runtime,
                        'credentials': [{'id': row[0], 'fields': [field for field in
                                        ('password', 'short_id', 'public_key', 'node_secret')
                                        if json.loads(row[1]).get('has_' + field)]}
                                        for row in connection.execute('SELECT id, data FROM proxies')]}
            # Plaintext staging inherits the existing sensitive workspace's boundary, not the backup destination's.
            with tempfile.TemporaryDirectory(prefix='.studio-snapshot-', dir=root) as temporary:
                staging = Path(temporary)
                database = staging / 'studio.sqlite3'
                with closing(sqlite3.connect(database)) as snapshot:
                    connection.backup(snapshot)
                    snapshot.execute('PRAGMA foreign_keys = ON')
                    for table in ('team_api_keys', 'team_http_sessions', 'team_sessions', 'team_commands', 'team_runs',
                                  'team_checkouts', 'team_login_attempts', 'team_login_limits'):
                        snapshot.execute('DELETE FROM ' + table)
                    snapshot.commit()
                os.chmod(database, 0o600)
                files['studio.sqlite3'] = {'size': database.stat().st_size, 'sha256': _hash(database)}
                if sum(item['size'] for item in files.values()) > max_bytes:
                    raise BackupError('size_limit')
                plain = staging / 'snapshot.zip'
                with zipfile.ZipFile(plain, 'w', compression=zipfile.ZIP_STORED) as archive:
                    for name in sorted(files):
                        path = database if name == 'studio.sqlite3' else root / name
                        _regular(path)
                        archive.write(path, name)
                        if _hash(path) != files[name]['sha256']:
                            raise BackupError('workspace_changed')
                    encoded = _json(manifest)
                    if len(encoded.encode()) > MANIFEST_BYTES:
                        raise BackupError('manifest_limit')
                    archive.writestr('manifest.json', encoded)
                if files != {**_workspace_files(root, ids, max_bytes), 'studio.sqlite3': files['studio.sqlite3']}:
                    raise BackupError('workspace_changed')
                encrypted = target.parent / ('.studio-encrypted-' + uuid4().hex)
                try:
                    _encrypt(plain, encrypted, key)
                    verified = _decrypt(encrypted, key, staging, max_bytes)
                    try:
                        if _hash(verified) != _hash(plain):
                            raise BackupError('backup_verification_failed')
                    finally:
                        verified.unlink()
                    # Publish a complete file without ever overwriting an existing destination.
                    os.link(encrypted, target)
                finally:
                    encrypted.unlink(missing_ok=True)
        store.record_audit('backup.created', target_kind='snapshot', target_id=manifest['snapshot_id'])
        return manifest
    finally:
        lock.release()


def _unpack(plain, staging, max_bytes):
    with zipfile.ZipFile(plain) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES + 1:
            raise BackupError('file_limit')
        names, folded = {}, set()
        for entry in entries:
            name = checked_name(entry.filename)
            mode = entry.external_attr >> 16
            if (name.casefold() in folded or entry.is_dir() or entry.flag_bits & 1
                    or entry.compress_type != zipfile.ZIP_STORED
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                raise BackupError('invalid_archive_entry')
            names[name] = entry
            folded.add(name.casefold())
        if 'manifest.json' not in names or names['manifest.json'].file_size > MANIFEST_BYTES:
            raise BackupError('invalid_manifest')
        manifest = json.loads(archive.read('manifest.json'))
        if not isinstance(manifest, dict):
            raise BackupError('invalid_manifest')
        if (type(manifest.get('format')) is not int or manifest.get('format') not in (1, 2) or manifest.get('schema') != SCHEMA_VERSION
                or manifest.get('team_schema') not in (4, 5, TEAM_SCHEMA_VERSION)
                or not isinstance(manifest.get('files'), dict)
                or set(names) != {*manifest['files'], 'manifest.json'}
                or 'studio.sqlite3' not in manifest['files']):
            raise BackupError('unsupported_snapshot_schema')
        validate_id(manifest['snapshot_id'])
        validate_id(manifest['workspace_id'])
        total = 0
        for name, item in manifest['files'].items():
            checked_name(name)
            if not isinstance(item, dict):
                raise BackupError('invalid_manifest')
            if name != 'studio.sqlite3':
                parts = name.split('/')
                if parts[0] == 'recovery' and manifest['format'] == 2:
                    if not re.fullmatch(r'recovery/[0-9a-f]{32}\.lock/owner\.json', name):
                        raise BackupError('invalid_archive_path')
                elif len(parts) >= 3 and parts[0] == 'profiles':
                    validate_id(parts[1])
                else:
                    raise BackupError('invalid_archive_path')
            entry = names[name]
            if (type(item.get('size')) is not int or item['size'] < 0
                    or item['size'] != entry.file_size
                    or not isinstance(item.get('sha256'), str)
                    or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
                raise BackupError('invalid_manifest')
            total += item['size']
            if total > max_bytes:
                raise BackupError('size_limit')
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as output, archive.open(entry) as source:
                count = 0
                for block in iter(lambda: source.read(BLOCK), b''):
                    count += len(block)
                    if count > item['size']:
                        raise BackupError('size_limit')
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if count != item['size'] or _hash(target) != item['sha256']:
                raise BackupError('snapshot_digest_mismatch')
        _recovery(staging)
        return manifest


def restore_snapshot(archive, destination, key, *, max_bytes=MAX_BYTES):
    """Publish an authenticated workspace atomically inside a new restore container."""
    _key(key)
    if Path(destination).is_symlink():
        raise BackupError('invalid_restore_destination')
    destination = Path(destination).resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise BackupError('invalid_restore_destination')
    destination.mkdir(mode=0o700)
    published = False
    try:
        with tempfile.TemporaryDirectory(prefix='.studio-restore-', dir=destination) as temporary:
            temporary = Path(temporary)
            plain = _decrypt(archive, key, temporary, max_bytes)
            staging = temporary / 'workspace'
            staging.mkdir(mode=0o700)
            manifest = _unpack(plain, staging, max_bytes)
            with closing(sqlite3.connect(staging / 'studio.sqlite3')) as connection:
                if (connection.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
                        or _metadata(connection, 'team_schema_version') != str(manifest['team_schema'])
                        or _metadata(connection, 'workspace_id') != manifest['workspace_id']
                        or _core_digest(connection) != manifest['core_digest']):
                    raise BackupError('database_manifest_mismatch')
                _clean(connection)
                ids = {row[0] for row in connection.execute('SELECT id FROM profiles')}
                if any(name.startswith('profiles/') and name.split('/')[1] not in ids
                       for name in manifest['files']):
                    raise BackupError('unknown_profile_directory')
                if manifest['team_schema'] >= 5:
                    connection.execute('DELETE FROM team_api_keys')
                if manifest['team_schema'] >= 6:
                    connection.execute('DELETE FROM team_checkouts')
                for table in ('team_http_sessions', 'team_sessions', 'team_commands', 'team_runs',
                              'team_login_attempts', 'team_login_limits'):
                    connection.execute('DELETE FROM ' + table)
                connection.execute("DELETE FROM workspace_metadata WHERE key IN ('migration_request', 'active_restore_receipt')")
                connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('credential_namespace', ?)",
                                   ('camoufox-studio/' + uuid4().hex,))
                connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('workspace_id', ?)",
                                   (str(uuid4()),))
                connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('restore_manifest', ?)",
                                   (_json(manifest),))
                connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('execution_barrier', ?)",
                                   (_json({'reason': 'restore_quarantine', 'snapshot_id': manifest['snapshot_id']}),))
                connection.commit()
            store = ProfileStore(staging)
            store.record_audit('backup.restored', target_kind='snapshot', target_id=manifest['snapshot_id'])
            os.rename(staging, destination / 'workspace')
            published = True
        return destination / 'workspace'
    except (InvalidTag, zipfile.BadZipFile, sqlite3.DatabaseError, KeyError, TypeError,
            ValueError) as error:
        if isinstance(error, BackupError):
            raise
        raise BackupError('invalid_snapshot') from error
    finally:
        if not published:
            destination.rmdir()


def activate_transfer(source, restored):
    """Retire the exact stopped source before removing a clone's quarantine barrier."""
    from . import manifest as runtime

    source, restored = Path(source).resolve(), Path(restored).resolve()
    if source == restored or any(not (root / 'studio.sqlite3').is_file() for root in (source, restored)):
        raise BackupError('invalid_transfer_workspace')
    with ExitStack() as stack:
        for root in sorted((source, restored)):
            lock = WorkspaceLock(root)
            lock.acquire()
            stack.callback(lock.release)
        original, copy = ProfileStore(source), ProfileStore(restored)
        with original._connect() as origin, copy._connect() as target:
            _clean(origin)
            _clean(target)
            encoded = _metadata(target, 'restore_manifest')
            if not encoded:
                raise BackupError('not_a_restored_workspace')
            record = json.loads(encoded)
            barrier = _metadata(target, 'execution_barrier')
            if not record.get('runtime_manifest'):
                raise BackupError('runtime_unregistered')
            target_id = _metadata(target, 'workspace_id')
            retirement = {'reason': 'retired', 'snapshot_id': record['snapshot_id'], 'target_id': target_id}
            previous = _metadata(origin, 'execution_barrier')
            if previous and json.loads(previous) != retirement:
                raise BackupError('source_retired')
            if not barrier:
                receipt = _metadata(target, 'active_restore_receipt')
                if previous and receipt and json.loads(receipt) == retirement:
                    return restored
                raise BackupError('not_quarantined')
            if json.loads(barrier).get('reason') != 'restore_quarantine':
                raise BackupError('not_quarantined')
            if _metadata(origin, 'workspace_id') != record['workspace_id']:
                raise BackupError('source_workspace_mismatch')
            for store in (original, copy):
                _verify_workspace(store, record)
                if runtime.registered(store) != record['runtime_manifest']:
                    raise BackupError('runtime_mismatch')
                try:
                    if runtime.capture(store) != record['runtime_manifest']:
                        raise BackupError('runtime_mismatch')
                except StoreError as error:
                    raise BackupError(str(error)) from None
            _verify_credentials(copy)
        # A crash between these commits leaves both workspaces fenced, never both executable.
        with original._write() as (connection, _):
            connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('execution_barrier', ?)",
                               (_json(retirement),))
            connection.execute('DELETE FROM team_http_sessions')
            connection.execute('DELETE FROM team_sessions')
            original._audit(connection, 'backup.source_retired', target_kind='snapshot',
                            target_id=record['snapshot_id'])
        with copy._write() as (connection, _):
            connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('active_restore_receipt', ?)",
                               (_json(retirement),))
            connection.execute("DELETE FROM workspace_metadata WHERE key = 'execution_barrier'")
            connection.execute('DELETE FROM team_http_sessions')
            connection.execute('DELETE FROM team_sessions')
            copy._audit(connection, 'backup.activated', target_kind='snapshot', target_id=record['snapshot_id'])
        return restored
