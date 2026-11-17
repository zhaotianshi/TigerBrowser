"""Stopped backup and migration runtime records; legacy identities are never rewritten."""

import hashlib
import json
import os
import platform
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from ..fpgen_model import CUSTOM_MODEL_ENV, PIN, _hash, is_pinned
from ..pkgman import INSTALL_DIR
from .distribution import browser_executable, default_addon_paths, profile_addon_paths
from .store import StoreError, _json, _now
from .workspace import WorkspaceLock


def _tree(directory, *, allowed_roots=(), portable=False):
    directory = Path(directory).resolve()
    roots = (directory, *(Path(root).resolve() for root in allowed_roots))
    if not directory.is_dir():
        raise StoreError('runtime_unavailable')
    digest = hashlib.sha256()
    count = 0
    ordering = (lambda path: path.relative_to(directory).as_posix()) if portable else None
    for path in sorted(directory.rglob('*'), key=ordering):
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) for root in roots):
            raise StoreError('runtime_path_escape')
        if resolved.is_file():
            digest.update(_json([path.relative_to(directory).as_posix(), _hash(resolved)]).encode())
            count += 1
        elif not resolved.is_dir():
            raise StoreError('runtime_unavailable')
    if not count:
        raise StoreError('runtime_unavailable')
    return digest.hexdigest()


def _host_id(*, create=False):
    path = INSTALL_DIR / 'studio-host-id'
    if create and not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                stream.write(str(uuid4()))
                stream.flush()
                os.fsync(stream.fileno())
    if path.is_symlink() or not path.is_file() or path.stat().st_size != 36:
        raise StoreError('runtime_host_unregistered')
    from .store import validate_id
    return validate_id(path.read_text(encoding='ascii'))


def capture(store, *, create_host=False, executable_path=None, addon_paths=None):
    """Record actual launch artifacts, not the page's spoofed device values."""
    if os.environ.get(CUSTOM_MODEL_ENV) or not is_pinned():
        raise StoreError('runtime_model_mismatch')
    try:
        selected = store.get_settings()['executable_path'] if executable_path is None else executable_path
        binary = browser_executable(selected)
    except OSError:
        raise StoreError('runtime_unavailable') from None
    if not binary.is_file():
        raise StoreError('runtime_unavailable')
    addons = set(default_addon_paths() if addon_paths is None else addon_paths)
    if addon_paths is None:
        for archived in (False, True):
            for profile in store.list_profiles(archived):
                if profile['identity']:
                    addons.update(profile_addon_paths(store, profile['identity']))
    native_fonts = {}
    if platform.system() == 'Windows':
        for name, location in (
                ('system', Path(os.environ['WINDIR']) / 'Fonts'),
                ('user', Path(os.environ['LOCALAPPDATA']) / 'Microsoft/Windows/Fonts')):
            if location.is_dir():
                native_fonts[name] = _tree(location)
    return {'format': 1, 'mode': 'headless', 'host_id': _host_id(create=create_host),
            'host': {'system': platform.system(), 'machine': platform.machine(),
                     'release': platform.release(), 'version': platform.version()},
            'browser': _tree(binary.parent), 'addons': sorted(_tree(Path(path)) for path in addons),
            'native_font_directories': native_fonts, 'model': PIN['sha256'],
            'packages': {name: version(name) for name in ('camoufox', 'fpgen', 'playwright')}}


def registered(store):
    with store._connect() as connection:
        row = connection.execute("SELECT value FROM workspace_metadata WHERE key = 'runtime_manifest'").fetchone()
        return json.loads(row[0]) if row else None


def verify(store, *, headless=True):
    expected = registered(store)
    if expected is None:
        return
    current = capture(store)
    if expected.get('mode') == 'desktop':
        current['mode'] = 'desktop'
    elif expected.get('mode') != 'headless' or not headless:
        raise StoreError('runtime_mismatch')
    if expected != current:
        raise StoreError('runtime_mismatch')


def register(root):
    """Record stopped artifacts for backup and migration without gating routine launches."""
    from .store import ProfileStore

    root = Path(root).resolve()
    if not (root / 'studio.sqlite3').is_file():
        raise StoreError('workspace_missing')
    lock = WorkspaceLock(root)
    lock.acquire()
    try:
        store = ProfileStore(root)
        with store._connect() as connection:
            if connection.execute("SELECT 1 FROM team_runs WHERE state != 'stopped'").fetchone():
                raise StoreError('recovery_required')
            if connection.execute("SELECT 1 FROM workspace_metadata WHERE key = 'execution_barrier'").fetchone():
                raise StoreError('workspace_quarantined')
        current = capture(store, create_host=True)
        with store._write() as (connection, _):
            connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('runtime_manifest', ?)",
                               (_json(current),))
            store._audit(connection, 'runtime.registered')
        return {'mode': current['mode'], 'registered_at': _now()}
    finally:
        lock.release()
