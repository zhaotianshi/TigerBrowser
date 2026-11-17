"""Exclusive check-out leases that move one profile's site data to a member computer and back."""

import hashlib
import json
import os
import secrets
import shutil
import tempfile
import zipfile
from pathlib import Path
from uuid import uuid4

from ..fpgen_model import _hash
from . import backup
from .backup import BLOCK, LOCK_NAMES, MAX_BYTES, MAX_FILES, BackupError, checked_name, profile_files
from .kernel_cache import PLATFORMS
from .kernels import resolve_kernel
from .store import PROFILE_FIELDS, StoreError, ValidationError, _identity_digest, _json, _now, validate_id

ACTIVE = ('checked_out', 'returning')
# Regenerable caches stay behind; each move behaves like a cache clear.
SKIPPED = frozenset({'cache2', 'startupCache', 'thumbnails', 'crashes', 'minidumps'})
MAX_FILE = 4 * 1024**3
COMMIT = 'checkout_commit:'


class CheckoutIncomplete(ValidationError):
    def __init__(self, missing):
        super().__init__('checkout_incomplete')
        self.missing = missing


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def synced_name(name):
    top = name.split('/', 1)[0]
    return top not in SKIPPED and name not in LOCK_NAMES


def profile_file(directory, name):
    """Resolve a requested name to a regular file without following any link component."""
    if not isinstance(name, str) or not synced_name(checked_name(name)):
        raise BackupError('invalid_archive_path')
    path = Path(directory)
    backup._directory(path)
    for part in name.split('/')[:-1]:
        path = path / part
        backup._directory(path)
    path = path / name.rsplit('/', 1)[-1]
    backup._regular(path)
    return path


def synced_files(directory):
    if not os.path.lexists(directory):
        return {}
    try:
        return profile_files(directory, skip=SKIPPED)
    except BackupError:
        raise ValidationError('unsafe_profile_data') from None


class _Sink:
    """A tell-only stream: ZipFile then writes data descriptors instead of seeking back."""

    def __init__(self):
        self.chunks, self.offset = [], 0

    def write(self, data):
        self.chunks.append(bytes(data))
        self.offset += len(data)
        return len(data)

    def tell(self):
        return self.offset

    def flush(self):
        pass

    def drain(self):
        chunks, self.chunks = self.chunks, []
        return b''.join(chunks)


def stream_files(paths):
    """Yield a ZIP_STORED archive of (name, path) pairs without a temporary copy."""
    sink = _Sink()
    with zipfile.ZipFile(sink, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, path in paths:
            with open(path, 'rb') as source, archive.open(zipfile.ZipInfo(name), 'w', force_zip64=True) as target:
                for block in iter(lambda: source.read(BLOCK), b''):
                    target.write(block)
                    if sink.offset:
                        yield sink.drain()
    if sink.chunks:
        yield sink.drain()


def write_entry(archive, entry, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.part-', dir=target.parent)
    digest, size = hashlib.sha256(), 0
    try:
        with os.fdopen(descriptor, 'wb') as output, archive.open(entry) as source:
            for block in iter(lambda: source.read(BLOCK), b''):
                size += len(block)
                if size > entry.file_size:
                    raise ValidationError('request_too_large')
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return {'size': size, 'sha256': digest.hexdigest()}


class Checkouts:
    def __init__(self, store, staging, kernel_cache=None):
        self.store = store
        self.kernel_cache = kernel_cache
        self.staging = Path(staging).expanduser().resolve()
        if self.staging.is_relative_to(store.root) or store.root.is_relative_to(self.staging):
            raise ValueError('Keep check-out staging outside the workspace.')
        self.staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        profiles = store.root / 'profiles'
        profiles.mkdir(exist_ok=True, mode=0o700)
        if os.stat(self.staging).st_dev != os.stat(profiles).st_dev:
            raise ValueError('Check-out staging must share the workspace filesystem for atomic commits.')

    def _work(self, checkout_id):
        return self.staging / validate_id(checkout_id)

    def _allowed(self, connection, member, profile_id):
        if member['role'] == 'admin':
            try:
                self.store._get(connection, 'profiles', profile_id)
            except KeyError:
                raise ValidationError('not_found') from None
        elif 'checkout' not in self.store._member_permissions(connection, member['id']).get(profile_id, ()):
            raise ValidationError('access_denied')

    def _live(self, connection, lease):
        """The lease holder must still be allowed to receive profile data."""
        row = connection.execute('SELECT data FROM team_members WHERE id = ?', (lease['member_id'],)).fetchone()
        member = json.loads(row['data']) if row else None
        if member is None or not member['active']:
            raise ValidationError('access_denied')
        self._allowed(connection, member, lease['profile_id'])

    @staticmethod
    def _save(connection, lease):
        connection.execute('UPDATE team_checkouts SET state = ?, data = ? WHERE id = ?',
                           (lease['state'], _json(lease), lease['id']))

    @staticmethod
    def public(lease):
        return {key: lease[key] for key in ('id', 'profile_id', 'member_id', 'member_name', 'device_id',
                                            'device_name', 'platform', 'state', 'created_at', 'heartbeat_at')}

    def active(self, connection, profile_id):
        row = connection.execute("SELECT data FROM team_checkouts WHERE profile_id = ? AND state IN (?, ?)",
                                 (profile_id, *ACTIVE)).fetchone()
        return json.loads(row['data']) if row else None

    def create(self, session_id, profile_id, data):
        if not isinstance(data, dict) or set(data) != {'request_id', 'device_id', 'device_name', 'platform'}:
            raise ValidationError('invalid_request')
        validate_id(data['request_id'])
        validate_id(data['device_id'])
        name = data['device_name']
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 128
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or data['platform'] not in PLATFORMS):
            raise ValidationError('invalid_request')
        validate_id(profile_id)
        token = secrets.token_urlsafe(32)
        with self.store._write() as (connection, _):
            session = self.store._resolve_session(connection, session_id)
            if session is None:
                raise ValidationError('session_expired')
            if session.get('api_key_id'):
                raise ValidationError('access_denied')
            member = session['member']
            self._allowed(connection, member, profile_id)
            profile = self.store._get(connection, 'profiles', profile_id)
            if profile['archived']:
                raise ValidationError('profile_archived')
            if connection.execute("SELECT 1 FROM team_runs WHERE profile_id = ? AND state != 'stopped'",
                                  (profile_id,)).fetchone():
                raise ValidationError('profile_busy')
            lease = self.active(connection, profile_id)
            if lease is not None:
                # A retried request from the same device rotates the token it never received.
                if (lease['state'] != 'checked_out' or (lease['member_id'], lease['device_id'], lease['request_id'])
                        != (member['id'], data['device_id'], data['request_id'])):
                    raise ValidationError('profile_checked_out')
                connection.execute('UPDATE team_checkouts SET token_hash = ? WHERE id = ?', (_digest(token), lease['id']))
                return {'checkout': self.public(lease), 'token': token}
            now = _now()
            lease = {'id': str(uuid4()), 'profile_id': profile_id, 'member_id': member['id'],
                     'member_name': member['display'], 'device_id': data['device_id'],
                     'device_name': name.strip(), 'platform': data['platform'], 'request_id': data['request_id'],
                     'state': 'checked_out', 'base_revision': profile['data_revision'],
                     'created_at': now, 'heartbeat_at': now}
            connection.execute('INSERT INTO team_checkouts VALUES (?, ?, ?, ?, ?, ?, ?)',
                               (lease['id'], profile_id, member['id'], lease['device_id'], lease['state'],
                                _digest(token), _json(lease)))
            self.store._audit(connection, 'checkout.created', actor_id=member['id'], actor_name=member['name'],
                              target_kind='checkout', target_id=lease['id'])
            return {'checkout': self.public(lease), 'token': token}

    def _lease(self, connection, checkout_id, token, states=ACTIVE):
        validate_id(checkout_id)
        row = connection.execute('SELECT state, token_hash, data FROM team_checkouts WHERE id = ?',
                                 (checkout_id,)).fetchone()
        if (row is None or not isinstance(token, str) or len(token) != 43
                or not secrets.compare_digest(row['token_hash'], _digest(token))):
            raise ValidationError('checkout_denied')
        if row['state'] == 'revoked':
            raise ValidationError('checkout_revoked')
        if row['state'] not in states:
            raise ValidationError('checkout_closed')
        return json.loads(row['data'])

    def authorize(self, checkout_id, token):
        with self.store._connect() as connection:
            return self._lease(connection, checkout_id, token)

    def manifest(self, checkout_id, token):
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token, ('checked_out',))
            self._live(connection, lease)
            profile = self.store._get(connection, 'profiles', lease['profile_id'])
        proxy = self.store.get_proxy(profile['proxy_id'], reveal_password=True) if profile['proxy_id'] else None
        item = {key: profile[key] for key in PROFILE_FIELDS}
        item.update({key: profile[key] for key in ('id', 'revision', 'data_revision', 'identity')})
        try:
            kernel = self._package(lease, profile)[1]
        except ValidationError:
            kernel = None
        return {'checkout': self.public(lease), 'profile': item, 'proxy': proxy, 'kernel': kernel,
                'files': synced_files(self.store.profile_dir(profile['id']))}

    def _package(self, lease, profile):
        from .kernel_cache import KernelCacheError
        kernel = (profile['identity'] or {}).get('kernel') or profile['kernel']
        if not kernel and profile['identity']:
            try:
                _, kernel = resolve_kernel(profile)
            except ValueError:
                raise ValidationError('kernel_not_cached') from None
        if not kernel or self.kernel_cache is None:
            raise ValidationError('kernel_not_cached')
        try:
            return self.kernel_cache.package(kernel, lease['platform'])
        except KernelCacheError:
            raise ValidationError('kernel_not_cached') from None

    def kernel_package(self, checkout_id, token):
        """The exact build the leased profile needs, for the platform its lease declared."""
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token, ('checked_out',))
            self._live(connection, lease)
            profile = self.store._get(connection, 'profiles', lease['profile_id'])
        return self._package(lease, profile)

    def files(self, checkout_id, token, names):
        if not isinstance(names, list) or len(names) > MAX_FILES or len(set(names)) != len(names):
            raise ValidationError('invalid_request')
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token, ('checked_out',))
            self._live(connection, lease)
        directory = self.store.profile_dir(lease['profile_id'])
        try:
            return [(name, profile_file(directory, name)) for name in names]
        except (BackupError, OSError):
            raise ValidationError('file_not_found') from None

    def claim_identity(self, checkout_id, token, identity):
        """The one fingerprint ledger stays on the server, whichever host generated the identity."""
        digest = _identity_digest(identity)
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token, ('checked_out',))
            self._live(connection, lease)
            profile = self.store._get(connection, 'profiles', lease['profile_id'])
            if profile['identity'] is not None:
                raise ValidationError('identity_present')
            if not profile['kernel'] or identity.get('kernel') != profile['kernel']:
                raise ValidationError('invalid_kernel')
            if not self.store._remember_identity(connection, digest):
                return False
            self.store._profile_state(connection, profile, {'identity': identity}, 'profile.identity_updated')
            return True

    def heartbeat(self, checkout_id, token):
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token)
            lease['heartbeat_at'] = _now()
            self._save(connection, lease)
            try:
                self._live(connection, lease)
                requested = False
            except (ValidationError, KeyError):
                requested = True
        return {'state': lease['state'], 'release_requested': requested}

    def cancel(self, checkout_id, token):
        """The holder gives up a lease before returning anything; the server keeps its data unchanged."""
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token, ('checked_out',))
            lease.update(state='cancelled', cancelled_at=_now())
            self._save(connection, lease)
            self.store._audit(connection, 'checkout.cancelled', actor_id=lease['member_id'],
                              target_kind='checkout', target_id=checkout_id)
        self._discard(checkout_id)
        return self.public(lease)

    def upload_path(self, checkout_id):
        work = self._work(checkout_id)
        work.mkdir(exist_ok=True, mode=0o700)
        return work / f'upload-{uuid4().hex}.zip'

    def receive(self, checkout_id, token, archive):
        """Stage changed files. A holder whose grant was withdrawn may still return; a forced release may not."""
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token)
            if lease['state'] == 'checked_out':
                lease['state'] = 'returning'
                self._save(connection, lease)
        target = self._work(checkout_id) / 'files'
        received = {}
        try:
            with zipfile.ZipFile(archive) as source:
                entries = source.infolist()
                if len(entries) > MAX_FILES:
                    raise ValidationError('file_limit')
                for entry in entries:
                    name = checked_name(entry.filename)
                    if (not synced_name(name) or entry.is_dir() or entry.flag_bits & 1
                            or entry.compress_type != zipfile.ZIP_STORED or entry.file_size > MAX_FILE):
                        raise ValidationError('invalid_archive_entry')
                    received[name] = write_entry(source, entry, target / name)
        except (zipfile.BadZipFile, BackupError, OSError, EOFError):
            raise ValidationError('invalid_archive_entry') from None
        # A forced release during the upload makes the staged copy unusable.
        self.authorize(checkout_id, token)
        return received

    def commit(self, checkout_id, token, data):
        if not isinstance(data, dict) or set(data) != {'files', 'data_revision'}:
            raise ValidationError('invalid_request')
        files = data['files']
        if not isinstance(files, dict) or len(files) > MAX_FILES or type(data['data_revision']) is not int:
            raise ValidationError('invalid_request')
        total = 0
        for name, item in files.items():
            try:
                checked_name(name)
            except BackupError:
                raise ValidationError('invalid_request') from None
            if (not synced_name(name) or not isinstance(item, dict) or set(item) != {'size', 'sha256'}
                    or type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE
                    or not isinstance(item['sha256'], str) or len(item['sha256']) != 64
                    or item['sha256'].strip('0123456789abcdef')):
                raise ValidationError('invalid_request')
            total += item['size']
        if total > MAX_BYTES:
            raise ValidationError('size_limit')
        if len({name.casefold() for name in files}) != len(files):
            raise ValidationError('invalid_request')
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token)
            profile = self.store._get(connection, 'profiles', lease['profile_id'])
            if profile['data_revision'] != data['data_revision'] or lease['base_revision'] != data['data_revision']:
                raise ValidationError('checkout_conflict')
            if lease['state'] == 'checked_out':
                lease['state'] = 'returning'
                self._save(connection, lease)
        work = self._work(checkout_id)
        work.mkdir(exist_ok=True, mode=0o700)
        assembled, staged = work / 'assembled', work / 'files'
        current = self.store.profile_dir(lease['profile_id'])
        if os.path.lexists(assembled):
            shutil.rmtree(assembled)
        assembled.mkdir(mode=0o700)
        missing, moved = [], []
        for name, item in sorted(files.items()):
            destination = assembled / name
            try:
                source = profile_file(staged, name)
                upload = source.stat().st_size == item['size']
            except (BackupError, OSError):
                upload = False
            try:
                if not upload:
                    source = profile_file(current, name)
                    if source.stat().st_size != item['size']:
                        raise BackupError('size_mismatch')
            except (BackupError, OSError):
                missing.append(name)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Current files must survive until the commit record exists, so they are copied.
            if upload:
                os.replace(source, destination)
                moved.append(name)
            else:
                shutil.copyfile(source, destination)
        if not missing:
            actual = profile_files(assembled)
            missing = sorted(name for name, item in files.items() if actual.get(name) != item)
        if missing:
            # Keep verified uploads so the client resends only what is listed.
            for name in moved:
                if name not in missing:
                    (staged / name).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(assembled / name, staged / name)
            shutil.rmtree(assembled)
            raise CheckoutIncomplete(missing)
        with self.store._write() as (connection, _):
            lease = self._lease(connection, checkout_id, token)
            record = {'profile_id': lease['profile_id'], 'had_previous': os.path.lexists(current)}
            connection.execute('INSERT INTO workspace_metadata VALUES (?, ?)', (COMMIT + checkout_id, _json(record)))
        self._apply(checkout_id, record)
        return {'data_revision': profile['data_revision'] + 1}

    def _apply(self, checkout_id, record):
        """Roll a durably recorded commit forward; safe to repeat after a crash at any step."""
        work = self._work(checkout_id)
        assembled, previous = work / 'assembled', work / 'previous'
        current = self.store.profile_dir(record['profile_id'])
        if os.path.lexists(assembled):
            if record['had_previous'] and not os.path.lexists(previous):
                os.rename(current, previous)
            os.rename(assembled, current)
        elif not os.path.lexists(current):
            raise StoreError('A recorded check-out commit lost its assembled data; inspect the staging directory.')
        with self.store._write() as (connection, _):
            row = connection.execute('SELECT data FROM team_checkouts WHERE id = ?', (checkout_id,)).fetchone()
            lease = json.loads(row['data'])
            profile = self.store._get(connection, 'profiles', record['profile_id'])
            profile['data_revision'] += 1
            connection.execute('UPDATE profiles SET data = ? WHERE id = ?', (_json(profile), profile['id']))
            lease.update(state='returned', returned_at=_now())
            self._save(connection, lease)
            connection.execute('DELETE FROM workspace_metadata WHERE key = ?', (COMMIT + checkout_id,))
            self.store._audit(connection, 'checkout.returned', actor_id=lease['member_id'],
                              target_kind='checkout', target_id=checkout_id)
        self._discard(checkout_id)

    def _discard(self, checkout_id):
        # Leftovers are disposable staging copies; recover() removes them on the next start.
        shutil.rmtree(self._work(checkout_id), ignore_errors=True)

    def recover(self):
        """Finish interrupted commits, then drop staging that no active lease can use."""
        with self.store._connect() as connection:
            records = [(row['key'][len(COMMIT):], json.loads(row['value'])) for row in connection.execute(
                'SELECT key, value FROM workspace_metadata WHERE key LIKE ?', (COMMIT + '%',))]
        for checkout_id, record in records:
            self._apply(checkout_id, record)
        with self.store._connect() as connection:
            active = {row['id'] for row in connection.execute(
                'SELECT id FROM team_checkouts WHERE state IN (?, ?)', ACTIVE)}
        for entry in self.staging.iterdir():
            if entry.name not in active:
                shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()

    def release(self, session_id, profile_id, checkout_id):
        """Administrator force release: the server keeps its last returned data."""
        validate_id(profile_id)
        with self.store._write() as (connection, _):
            session = self.store._resolve_session(connection, session_id)
            if session is None:
                raise ValidationError('session_expired')
            if session['member']['role'] != 'admin' or session.get('api_key_id'):
                raise ValidationError('admin_required')
            row = connection.execute('SELECT data FROM team_checkouts WHERE id = ? AND profile_id = ?',
                                     (validate_id(checkout_id), profile_id)).fetchone()
            if row is None:
                raise ValidationError('not_found')
            lease = json.loads(row['data'])
            if lease['state'] not in ACTIVE:
                raise ValidationError('checkout_closed')
            if connection.execute('SELECT 1 FROM workspace_metadata WHERE key = ?', (COMMIT + checkout_id,)).fetchone():
                raise ValidationError('checkout_committing')
            lease.update(state='revoked', revoked_at=_now())
            self._save(connection, lease)
            self.store._audit(connection, 'checkout.revoked', actor_id=session['member']['id'],
                              actor_name=session['member']['name'], target_kind='checkout', target_id=checkout_id)
        self._discard(checkout_id)
        return self.public(lease)
