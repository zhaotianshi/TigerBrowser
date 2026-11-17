"""A headless execution host initializes accounts locally without exposing a setup token."""

import os
import subprocess
import sys

from click.testing import CliRunner

from camoufox.__main__ import cli
from camoufox.studio.store import ProfileStore, TEAM_SETUP_CODE_PATH


def initialize(path, password='local-admin-password-1'):
    return CliRunner().invoke(cli, ['studio-team-init', '--data-dir', str(path),
                                   '--name', 'root', '--display', 'Administrator'],
                              input=password + '\n' + password + '\n')


def test_local_initialization_works_without_qt_or_network(tmp_path, monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError('Local initialization must not connect to the network.')

    monkeypatch.setattr(socket, 'create_connection', forbidden)
    root = tmp_path / 'workspace'
    result = initialize(root)
    assert result.exit_code == 0, result.output
    store = ProfileStore(root)
    session = store.login('root', 'local-admin-password-1')
    assert session['member']['role'] == 'admin'
    assert 'local-admin-password-1' not in result.output
    assert not (root / 'workspace.lock').exists()
    assert not (root / TEAM_SETUP_CODE_PATH).exists()
    if os.name != 'nt':
        assert root.stat().st_mode & 0o777 == 0o700


def test_existing_directory_is_never_initialized_or_overwritten(tmp_path):
    original = tmp_path / 'keep'
    original.write_bytes(b'unchanged')
    result = initialize(tmp_path)
    assert result.exit_code != 0 and 'new directory' in result.output
    assert original.read_bytes() == b'unchanged'
    assert not (tmp_path / 'studio.sqlite3').exists()


def initialize_existing(path, password='local-admin-password-1'):
    return CliRunner().invoke(cli, ['studio-team-init', '--existing', '--data-dir', str(path),
                                   '--name', 'root', '--display', 'Administrator'],
                              input=password + '\n' + password + '\n')


def test_existing_memberless_workspace_keeps_its_profiles_and_gains_an_administrator(tmp_path):
    root = tmp_path / 'workspace'
    profile = ProfileStore(root).save_profile({'name': 'Kept environment'})
    result = initialize_existing(root)
    assert result.exit_code == 0, result.output
    store = ProfileStore(root)
    assert store.login('root', 'local-admin-password-1')['member']['role'] == 'admin'
    assert [item['id'] for item in store.list_profiles()] == [profile['id']]
    assert not (root / 'workspace.lock').exists()
    assert not (root / TEAM_SETUP_CODE_PATH).exists()


def test_existing_mode_refuses_team_workspaces_owned_or_missing_databases(tmp_path):
    root = tmp_path / 'workspace'
    assert initialize(root).exit_code == 0
    result = initialize_existing(root, 'another-admin-password-1')
    assert result.exit_code != 0 and 'already has members' in result.output
    assert ProfileStore(root).login('root', 'local-admin-password-1')
    empty = tmp_path / 'empty'
    empty.mkdir()
    assert initialize_existing(empty).exit_code != 0
    assert list(empty.iterdir()) == []
    owned = tmp_path / 'owned'
    ProfileStore(owned)
    from camoufox.studio.workspace import WorkspaceLock
    lock = WorkspaceLock(owned)
    lock.acquire()
    try:
        result = initialize_existing(owned)
        assert result.exit_code != 0 and not ProfileStore(owned).list_members()
    finally:
        lock.release()


def test_invalid_password_does_not_create_a_workspace(tmp_path):
    root = tmp_path / 'workspace'
    result = initialize(root, 'short')
    assert result.exit_code != 0
    assert not root.exists()


def test_cancelled_confirmation_does_not_create_a_workspace(tmp_path):
    root = tmp_path / 'workspace'
    result = CliRunner().invoke(cli, ['studio-team-init', '--data-dir', str(root),
                                     '--name', 'root', '--display', 'Admin'], input='')
    assert result.exit_code != 0 and not root.exists()


def test_invalid_member_fields_do_not_create_a_workspace(tmp_path):
    root = tmp_path / 'workspace'
    result = CliRunner().invoke(cli, ['studio-team-init', '--data-dir', str(root),
                                     '--name', 'invalid/name', '--display', 'Admin'])
    assert result.exit_code != 0 and not root.exists()


def test_initialization_failure_retains_new_data_but_releases_its_lock(tmp_path, monkeypatch):
    from camoufox.studio.store import StoreError

    def fail(*args):
        raise StoreError('private disk diagnostic')

    monkeypatch.setattr(ProfileStore, 'initialize_team', fail)
    root = tmp_path / 'workspace'
    result = initialize(root)
    assert result.exit_code != 0 and 'retained' in result.output
    assert 'private disk diagnostic' not in result.output
    assert (root / 'studio.sqlite3').is_file()
    assert not (root / 'workspace.lock').exists()


def test_setup_command_has_no_gui_or_network_framework_dependency(tmp_path):
    code = '''
import sys
class NoGuiOrServer:
    def find_spec(self, name, *args):
        if name.split('.')[0] in ('PySide6', 'fastapi', 'uvicorn', 'starlette'):
            raise ModuleNotFoundError('not installed', name=name)
sys.meta_path.insert(0, NoGuiOrServer())
from camoufox.__main__ import cli
from click.testing import CliRunner
result = CliRunner().invoke(cli, ['studio-team-init', '--data-dir', sys.argv[1],
                                 '--name', 'root', '--display', 'Admin'],
                           input='local-admin-password-1\\nlocal-admin-password-1\\n')
assert result.exit_code == 0, result.output
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'workspace')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_team_service_cli_defaults_to_local_http_without_tls_or_allowlist(tmp_path, monkeypatch):
    from camoufox.studio.team import host

    captured = {}
    monkeypatch.setattr(host, 'run', lambda root, **options: captured.update(root=root, **options))
    result = CliRunner().invoke(cli, ['studio-team', '--data-dir', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert captured['root'] == tmp_path
    assert captured['origin'] == 'http://127.0.0.1:8766'
    assert captured['listen'] == '127.0.0.1'
    assert captured['cert'] is None and captured['key'] is None
    assert captured['private_network_only'] is False and captured['allow_client'] == ()
