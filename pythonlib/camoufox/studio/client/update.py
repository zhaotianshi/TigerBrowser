"""Client updates from the connected server, verified before they run after the client exits."""

import hashlib
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

import requests

from ..client_releases import ClientReleaseError, compatible, validate, version_key
from ..kernel_cache import host_platform
from ..team.transport import exact_origin, session_cookie
from .sync import TIMEOUT, SyncError

BLOCK = 1024 * 1024


def current_version():
    return version('camoufox')


def _session(origin, cookie):
    session = requests.Session()
    session.trust_env = False
    session.headers.update({'Origin': origin,
                            'Cookie': f'{session_cookie(exact_origin(origin).scheme)}={cookie}'})
    return session


def _error(response):
    try:
        return SyncError(response.json().get('error', 'operation_failed'))
    except ValueError:
        return SyncError('operation_failed')


def check(origin, cookie):
    """Whether this build can work with the logged-in server, and its installer for this platform."""
    with _session(origin, cookie) as session:
        try:
            response = session.get(origin + '/team/v1/client/update', timeout=TIMEOUT)
            data = response.json() if response.ok else None
        except (requests.RequestException, ValueError):
            raise SyncError('server_unreachable') from None
    if data is None:
        raise _error(response)
    current = current_version()
    try:
        records = [item for item in data['packages'] if item['platform'] == host_platform()]
        package = validate(records[0]) if len(records) == 1 else None
        if len(records) > 1:
            raise ClientReleaseError('invalid_client_package')
        result = {'version': current, 'compatible': compatible(data['minimum_protocol'], data['protocol']),
                  'package': package,
                  'newer': package is not None and version_key(package['version']) > version_key(current)}
    except (ClientReleaseError, KeyError, TypeError):
        raise SyncError('client_update_invalid') from None
    return result


def download(origin, cookie, record, directory):
    """Keep one verified installer; a wrong size or digest leaves nothing behind."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix='.update-', dir=directory)
    try:
        with os.fdopen(descriptor, 'wb') as stream, _session(origin, cookie) as session:
            try:
                with session.get(f"{origin}/team/v1/client/packages/{record['platform']}",
                                 timeout=TIMEOUT, stream=True) as response:
                    if not response.ok:
                        raise _error(response)
                    size, digest = 0, hashlib.sha256()
                    for block in response.iter_content(BLOCK):
                        size += len(block)
                        if size > record['size']:
                            raise SyncError('client_update_digest_mismatch')
                        digest.update(block)
                        stream.write(block)
            except requests.RequestException:
                raise SyncError('server_unreachable') from None
            if size != record['size'] or digest.hexdigest() != record['sha256']:
                raise SyncError('client_update_digest_mismatch')
            stream.flush()
            os.fsync(stream.fileno())
        target = directory / ('client-update' + Path(record['file']).suffix)
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return target


# Waits for the client to exit, then swaps the bundle so a failed copy keeps the previous app.
MAC_REPLACE = r"""
pid=$1 image=$2 app=$3
while kill -0 "$pid" 2>/dev/null; do sleep 1; done
mount=$(mktemp -d) || exit 1
hdiutil attach -nobrowse -readonly -mountpoint "$mount" "$image" || exit 1
rm -rf "$app.update" "$app.previous"
if ditto "$mount/TigerBrowser.app" "$app.update" && mv "$app" "$app.previous"; then
  if mv "$app.update" "$app"; then rm -rf "$app.previous"; else mv "$app.previous" "$app"; fi
fi
hdiutil detach "$mount"
open "$app"
"""


def packaged_app():
    """The installed application, or None for a source checkout, which updates through Git instead."""
    if not getattr(sys, 'frozen', False):
        return None
    executable = Path(sys.executable).resolve()
    return executable.parents[2] if sys.platform == 'darwin' else executable.parent


def install_command(platform, path, app, pid):
    """Windows installs silently and reopens the client; macOS replaces its bundle; Ubuntu opens the installer."""
    if platform == 'win-x86_64':
        return [str(path), '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART']
    if platform.startswith('mac-'):
        if os.access(app, os.W_OK) and os.access(app.parent, os.W_OK):
            return ['/bin/sh', '-c', MAC_REPLACE, 'sh', str(pid), str(path), str(app)]
        return ['open', str(path)]
    return ['xdg-open', str(path)]


def start_install(path):
    app = packaged_app()
    if app is None:
        raise SyncError('client_update_unsupported')
    options = ({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if sys.platform == 'win32' else {'start_new_session': True})
    subprocess.Popen(install_command(host_platform(), path, app, os.getpid()), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **options)
