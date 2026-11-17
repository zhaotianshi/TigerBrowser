"""Exact browser build identifiers shared by profiles, identities and distribution."""

import re
from pathlib import Path

from .. import multiversion
from ..pkgman import launch_path


def validate_kernel(value):
    if (not isinstance(value, str) or len(value) > 160 or value and not re.fullmatch(
            r'[a-z0-9][a-z0-9_.-]*/[0-9][A-Za-z0-9_.-]*', value)):
        raise ValueError('invalid_kernel')
    return value


def kernel_id(installed):
    return validate_kernel(f'{installed.repo_name}/{installed.version.full_string}')


def historical_version_matches(release, recorded):
    """Old identities contain either the full build or the browser version alone."""
    return recorded in (release, release.partition('-')[0])


def resolve_kernel(profile):
    identity = profile.get('identity') or {}
    selected = identity.get('kernel') or profile.get('kernel', '')
    validate_kernel(selected)
    installed = multiversion.list_installed()
    if selected:
        candidates = [item for item in installed if kernel_id(item) == selected]
    elif identity.get('browser_version'):
        candidates = [item for item in installed
                      if historical_version_matches(item.version.full_string, identity['browser_version'])]
    else:
        raise ValueError('kernel_required')
    if not candidates:
        raise ValueError('kernel_not_installed')
    if len(candidates) != 1:
        raise ValueError('kernel_ambiguous')
    chosen = candidates[0]
    return Path(launch_path(chosen.path)).resolve(), kernel_id(chosen)


def initialize_default(store):
    """Record the pre-upgrade selection once; later global activation cannot move profiles."""
    with store._connect() as connection:
        if connection.execute("SELECT 1 FROM workspace_metadata WHERE key='execution_barrier'").fetchone():
            return
    selected = store.get_default_kernel()
    if not selected:
        active = multiversion.get_active_path(update_config=False)
        candidates = [item for item in multiversion.list_installed() if item.path == active]
        if len(candidates) != 1:
            return
        selected = kernel_id(candidates[0])
    store.set_default_kernel(selected, initialize=True)
