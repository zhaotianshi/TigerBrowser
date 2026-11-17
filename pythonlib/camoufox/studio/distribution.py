"""Resource and subprocess locations for source and standalone Studio builds."""

import json
import sys
from pathlib import Path

from ..multiversion import get_active_path
from ..pkgman import launch_path


def bundled_path(name: str) -> Path | None:
    if not getattr(sys, 'frozen', False):
        return None
    return Path(__file__).with_name('bundle') / name


def browser_executable(executable_path=None) -> Path:
    """Resolve the same existing artifact for launch, registration and read-only checks."""
    if executable_path:
        return Path(executable_path).expanduser().resolve()
    directory = get_active_path(update_config=False)
    if directory is None:
        raise FileNotFoundError('No installed browser is selected.')
    return Path(launch_path(directory)).resolve()


def default_addon_paths() -> list[str]:
    from ..addons import DefaultAddons, get_addon_path

    bundled = bundled_path('addons')
    return [str(bundled / addon.name) if bundled else get_addon_path(addon.name)
            for addon in DefaultAddons]


def profile_addon_paths(store, identity) -> list[str]:
    """Resolve host paths separately from the saved browser identity."""
    paths = identity['config'].get('addons', [])
    if bundled_path('addons'):
        return default_addon_paths()
    if not paths:
        return []
    with store._connect() as connection:
        row = connection.execute("SELECT value FROM workspace_metadata WHERE key='runtime_addon_paths'").fetchone()
    mapping = json.loads(row[0]) if row else {}
    defaults = {Path(path).name: path for path in default_addon_paths()}
    return [mapping.get(path, defaults.get(path.replace('\\', '/').rsplit('/', 1)[-1], path))
            for path in paths]
