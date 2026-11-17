"""Check-out synchronization between a team server and this computer's cache workspace, without Qt."""

import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests

from ... import multiversion
from ..backup import BLOCK, BackupError, checked_name
from ..checkouts import stream_files, synced_files, synced_name, write_entry
from ..kernel_cache import host_platform
from ..kernels import kernel_id
from ..store import ProfileStore, ValidationError, _json, _now, validate_id

LEASE = 'client_checkout:'
STALE = 'client_stale:'
BATCH = 2000
# The read timeout applies per block, so large transfers are bounded by stalls, not size.
TIMEOUT = (30, 120)


class SyncError(RuntimeError):
    """A fixed code; the server's own error code when it gave one."""

    def __init__(self, code, missing=None):
        super().__init__(code)
        self.code, self.missing = code, missing


class ClientStore(ProfileStore):
    """A cache of checked-out profiles; identities are claimed in the server's ledger."""

    claimer = None

    def uses_team_kernels(self):
        return True

    def claim_identity(self, profile_id, identity):
        if self.claimer is None or not self.claimer(profile_id, identity):
            return False
        self.set_identity(profile_id, identity)
        return True

    def set_data_revision(self, profile_id, value):
        with self._write() as (connection, _):
            profile = self._get(connection, 'profiles', profile_id)
            self._profile_state(connection, profile, {'data_revision': value}, 'profile.returned')

    def bind_origin(self, origin):
        """Each cache belongs to one server address, so lease tokens are never sent to another server."""
        with self._write() as (connection, _):
            row = connection.execute("SELECT value FROM workspace_metadata WHERE key = 'client_origin'").fetchone()
            if row is None:
                connection.execute("INSERT INTO workspace_metadata VALUES ('client_origin', ?)", (origin,))
            elif row[0] != origin:
                raise SyncError('workspace_origin_mismatch')

    def metadata(self, prefix):
        with self._connect() as connection:
            return {row['key'][len(prefix):]: json.loads(row['value']) for row in connection.execute(
                'SELECT key, value FROM workspace_metadata WHERE substr(key, 1, ?) = ?', (len(prefix), prefix))}

    def save_metadata(self, key, value):
        with self._write() as (connection, _):
            if value is None:
                connection.execute('DELETE FROM workspace_metadata WHERE key = ?', (key,))
            else:
                connection.execute('INSERT OR REPLACE INTO workspace_metadata VALUES (?, ?)', (key, _json(value)))

    def token(self, checkout_id, value=None, *, delete=False):
        key = 'checkout/' + validate_id(checkout_id)
        if delete:
            return self._secret('delete', key)
        if value is not None:
            return self._secret('set', key, value)
        return self._secret('get', key)


class HttpTransport:
    """Lease-token requests that send the server's exact Origin and ignore system proxies."""

    def __init__(self, origin):
        self.origin = origin
        self.session = requests.Session()
        self.session.trust_env = False

    def call(self, method, path, token, *, json=None, body=None, content_type=None, download=None):
        headers = {'Origin': self.origin, 'Authorization': 'Bearer ' + token}
        if content_type:
            headers['Content-Type'] = content_type
        try:
            response = self.session.request(method, self.origin + '/team/v1' + path, headers=headers,
                                            json=json, data=body, timeout=TIMEOUT, stream=download is not None)
            if response.ok and download is not None:
                for block in response.iter_content(BLOCK):
                    download.write(block)
                return response.headers
            data = response.json() if response.content else {}
        except (requests.RequestException, ValueError):
            raise SyncError('server_unreachable') from None
        if not response.ok:
            raise SyncError(data.get('error', 'operation_failed'), data.get('missing'))
        return data


def _files(directory):
    try:
        return synced_files(directory)
    except ValidationError as error:
        raise SyncError(str(error)) from None


class SyncEngine:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport
        store.claimer = self._claim

    def leases(self):
        return self.store.metadata(LEASE)

    def stale(self):
        return self.store.metadata(STALE)

    def _lease(self, profile_id):
        lease = self.leases().get(validate_id(profile_id))
        if lease is None:
            raise SyncError('not_checked_out')
        return lease

    def _save(self, lease):
        self.store.save_metadata(LEASE + lease['profile_id'], lease)

    def _call(self, lease, method, path, **options):
        token = self.store.token(lease['checkout_id'])
        if token is None:
            raise SyncError('checkout_denied')
        try:
            return self.transport.call(method, '/checkouts/' + lease['checkout_id'] + path, token, **options)
        except SyncError as error:
            if error.code == 'checkout_revoked':
                # A running browser still holds the files; resume() moves them aside after it stops.
                lease['state'] = 'revoked'
                self._save(lease)
            raise

    def checkout(self, checkout, token):
        """Mirror a lease: profile, proxy, kernel and only the site files that differ locally."""
        profile_id, checkout_id = validate_id(checkout['profile_id']), validate_id(checkout['id'])
        if not isinstance(token, str) or len(token) != 43:
            raise SyncError('checkout_denied')
        previous = self.leases().get(profile_id)
        if previous is not None and previous['checkout_id'] != checkout_id:
            if previous['state'] == 'revoked':
                self._retire(previous)
            else:
                self._settle(previous)
        self.store.token(checkout_id, token)
        if previous is not None and previous['checkout_id'] == checkout_id:
            # Retried delivery must not replace locally modified data or reset a sent return.
            return self._sync(previous) if previous['state'] == 'syncing' else previous
        lease = {'profile_id': profile_id, 'checkout_id': checkout_id, 'state': 'syncing'}
        self._save(lease)
        return self._sync(lease)

    def _settle(self, lease):
        """A new lease means the server ended the old one; learn how before touching its data."""
        try:
            self._call(lease, 'POST', '/heartbeat', json={})
        except SyncError as error:
            if error.code == 'checkout_revoked':
                self._retire(lease)
                return
            if error.code == 'checkout_closed' and lease.get('committing'):
                # Only a committed return closes a lease; its response was lost.
                self._returned(lease)
                return
            raise
        raise SyncError('pending_return')

    def _sync(self, lease):
        profile_id = lease['profile_id']
        manifest = self._call(lease, 'GET', '')
        if manifest['profile']['id'] != profile_id or manifest['checkout']['id'] != lease['checkout_id']:
            raise SyncError('checkout_denied')
        self.store.mirror_profile(manifest['profile'], manifest['proxy'])
        self._install_kernel(lease, manifest['kernel'])
        directory = self.store.profile_dir(profile_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        local = _files(directory)
        for name in set(local) - set(manifest['files']):
            (directory / checked_name(name)).unlink()
        wanted = sorted(name for name, item in manifest['files'].items() if local.get(name) != item)
        for start in range(0, len(wanted), BATCH):
            self._download(lease, directory, wanted[start:start + BATCH])
        if _files(directory) != manifest['files']:
            raise SyncError('download_incomplete')
        lease.update(state='ready', base=manifest['files'], data_revision=manifest['profile']['data_revision'])
        self._save(lease)
        return lease

    def _download(self, lease, directory, names):
        with tempfile.TemporaryFile(dir=self.store.root) as archive:
            self._call(lease, 'POST', '/files', json={'names': names}, download=archive)
            archive.seek(0)
            try:
                with zipfile.ZipFile(archive) as source:
                    entries = source.infolist()
                    if sorted(entry.filename for entry in entries) != names:
                        raise SyncError('download_incomplete')
                    for entry in entries:
                        name = checked_name(entry.filename)
                        if not synced_name(name) or entry.compress_type != zipfile.ZIP_STORED:
                            raise SyncError('download_incomplete')
                        write_entry(source, entry, directory / name)
            except (zipfile.BadZipFile, BackupError, ValidationError, OSError, EOFError):
                raise SyncError('download_incomplete') from None

    def _install_kernel(self, lease, record):
        if record is None or any(kernel_id(item) == record['kernel_id'] for item in multiversion.list_installed()):
            return
        if record['platform'] != host_platform():
            raise SyncError('kernel_platform_mismatch')
        descriptor, name = tempfile.mkstemp(prefix='.kernel-', dir=self.store.root)
        path = Path(name)
        try:
            with os.fdopen(descriptor, 'w+b') as archive:
                self._call(lease, 'GET', '/kernel', download=archive)
                archive.seek(0)
                digest = hashlib.sha256()
                for block in iter(lambda: archive.read(BLOCK), b''):
                    digest.update(block)
            if digest.hexdigest() != record['sha256']:
                raise SyncError('kernel_digest_mismatch')
            metadata = {key: record[key] for key in ('version', 'build', 'prerelease', 'asset_id', 'asset_size',
                                                     'asset_updated_at', 'sha256', 'created_at')}
            multiversion.install_archive(path, record['kernel_id'].split('/', 1)[0], metadata)
        finally:
            path.unlink(missing_ok=True)

    def begin_local(self, profile_id):
        """A synced lease may reopen offline until a commit was sent, which may already hold this copy."""
        lease = self._lease(profile_id)
        if lease['state'] not in ('ready', 'pending_return'):
            raise SyncError('not_checked_out')
        if lease.get('committing'):
            raise SyncError('return_in_progress')
        if lease['state'] != 'ready' or not lease.get('launched'):
            lease.update(state='ready', launched=True)
            self._save(lease)

    def cancel(self, profile_id):
        """Give up a lease whose copy never ran here, so the server's data stays authoritative."""
        lease = self._lease(profile_id)
        if lease.get('launched') or lease['state'] not in ('syncing', 'ready'):
            raise SyncError('return_required')
        self._call(lease, 'POST', '/cancel', json={})
        self._close(lease)

    def _claim(self, profile_id, identity):
        return self._call(self._lease(profile_id), 'POST', '/identity', json={'identity': identity})['claimed']

    def heartbeat(self, profile_id):
        return self._call(self._lease(profile_id), 'POST', '/heartbeat', json={})

    def finish(self, profile_id):
        """The browser stopped cleanly; the data must reach the server before the lease can end."""
        lease = self._lease(profile_id)
        if lease['state'] != 'ready':
            raise SyncError('not_checked_out')
        lease['state'] = 'pending_return'
        self._save(lease)

    def return_profile(self, profile_id):
        lease = self._lease(profile_id)
        if lease['state'] != 'pending_return':
            raise SyncError('profile_running' if lease['state'] == 'ready' else 'not_checked_out')
        directory = self.store.profile_dir(profile_id)
        local = _files(directory)
        changed = sorted(name for name, item in local.items() if lease['base'].get(name) != item)
        body = {'files': local, 'data_revision': lease['data_revision']}
        if not lease.get('committing'):
            lease['committing'] = True
            self._save(lease)
        try:
            for _ in range(3):
                if changed:
                    self._call(lease, 'POST', '/return', content_type='application/zip',
                               body=stream_files([(name, directory / name) for name in changed]))
                try:
                    self._call(lease, 'POST', '/commit', json=body)
                    break
                except SyncError as error:
                    if error.code != 'checkout_incomplete' or not error.missing:
                        raise
                    changed = [name for name in error.missing if name in local]
            else:
                raise SyncError('checkout_incomplete')
        except SyncError as error:
            if error.code != 'checkout_closed':
                raise
        return self._returned(lease)

    def _returned(self, lease):
        revision = lease['data_revision'] + 1
        self.store.set_data_revision(lease['profile_id'], revision)
        self._close(lease)
        return {'data_revision': revision}

    def resume_profile(self, profile_id):
        """Continue whatever a stopped profile's lease still needs: download, return or retirement."""
        lease = self._lease(profile_id)
        if lease['state'] == 'revoked':
            self._retire(lease)
            raise SyncError('checkout_revoked')
        if lease['state'] == 'pending_return':
            return self.return_profile(profile_id)
        if lease['state'] == 'syncing':
            return self._sync(lease)
        return lease

    def resume(self, running=()):
        """Resume every stopped profile that is not simply ready; failures stay recorded for the next attempt."""
        results = {}
        for profile_id, lease in self.leases().items():
            if profile_id in running or lease['state'] == 'ready':
                continue
            try:
                results[profile_id] = self.resume_profile(profile_id)
            except SyncError as error:
                results[profile_id] = {'error': error.code}
        return results

    def _close(self, lease):
        # Different profiles may finish concurrently while sharing one cached proxy.
        with self.store._lock:
            try:
                proxy_id = self.store.get_profile(lease['profile_id'])['proxy_id']
            except KeyError:
                proxy_id = None
            remaining = set(self.leases()) - {lease['profile_id']}
            if proxy_id and not any(profile['id'] in remaining and profile['proxy_id'] == proxy_id
                                    for profile in self.store.list_profiles()):
                self.store.clear_proxy_secrets(proxy_id)
            self.store.token(lease['checkout_id'], delete=True)
            self.store.save_metadata(LEASE + lease['profile_id'], None)

    def _retire(self, lease):
        """A forced release disables this copy; its data is kept aside for an administrator."""
        directory = self.store.profile_dir(lease['profile_id'])
        if os.path.lexists(directory):
            target = self.store.root / 'stale' / lease['checkout_id']
            target.parent.mkdir(exist_ok=True, mode=0o700)
            shutil.move(directory, target)
        self.store.save_metadata(STALE + lease['checkout_id'], {
            'profile_id': lease['profile_id'], 'revoked_at': _now()})
        self._close(lease)
