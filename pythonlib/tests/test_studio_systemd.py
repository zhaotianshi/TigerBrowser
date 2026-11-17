"""Fail-closed service credential initialization without touching the host keyring."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from camoufox.studio.team import systemd
from camoufox.studio.secrets import SecretStoreError


@pytest.fixture
def service(tmp_path, monkeypatch):
    credential = tmp_path / 'credentials'
    credential.mkdir()
    (credential / 'keyring-unlock').write_bytes(b'synthetic-unlock-key')
    runtime = tmp_path / 'run'
    runtime.mkdir()
    monkeypatch.setenv('CREDENTIALS_DIRECTORY', str(credential))
    monkeypatch.setenv('RUNTIME_DIRECTORY', str(runtime))
    monkeypatch.setenv('HOME', str(tmp_path / 'state'))
    # Restore the whole environment, including variables changed by the initializer.
    monkeypatch.setattr(systemd.os, 'environ', systemd.os.environ.copy())
    return credential, runtime


@pytest.mark.parametrize('password', [b'synthetic-unlock-key', b'synthetic-unlock-key\n', b'\n'])
def test_unlock_uses_private_bus_and_stdin_then_checks_real_native_storage(service, monkeypatch, password):
    credential, runtime = service
    (credential / 'keyring-unlock').write_bytes(password)
    monkeypatch.setenv('DBUS_SESSION_BUS_ADDRESS', 'unix:path=/desktop/bus')
    monkeypatch.setenv('GNOME_KEYRING_CONTROL', '/desktop/keyring')
    monkeypatch.setenv('KEYRING_PROPERTY_PREFERRED_COLLECTION', '/session')
    monkeypatch.setenv('PYTHON_KEYRING_BACKEND', 'keyring.backends.null.Keyring')
    calls = []
    monkeypatch.setattr(systemd.subprocess, 'run', lambda command, **kw: calls.append((command, kw)))
    systemd.unlock_keyring()
    assert len(calls) == 3
    bus, daemon, probe = calls
    assert bus[0][0] == 'dbus-daemon' and f'--address=unix:path={runtime}/bus' in bus[0]
    assert daemon[0][:3] == ['gnome-keyring-daemon', '--unlock', '--components=secrets']
    assert daemon[1]['input'] == password
    assert 'check_keyring()' in probe[0][-1]
    for command, kwargs in calls:
        assert 'synthetic-unlock-key' not in str(command)
        assert kwargs['timeout'] > 0 and kwargs['check']
        assert kwargs['stdout'] == kwargs['stderr'] == subprocess.DEVNULL
    assert 'CREDENTIALS_DIRECTORY' not in systemd.os.environ
    assert 'GNOME_KEYRING_CONTROL' not in systemd.os.environ
    assert 'KEYRING_PROPERTY_PREFERRED_COLLECTION' not in systemd.os.environ
    assert systemd.os.environ['PYTHON_KEYRING_BACKEND'] == 'keyring.backends.SecretService.Keyring'
    assert Path(systemd.os.environ['XDG_DATA_HOME']) == credential.parent / 'state/.local/share'
    assert (credential / 'keyring-unlock').read_bytes() == password


@pytest.mark.parametrize('password', [b'', b'key\0suffix', b'x' * 4097])
def test_invalid_credential_never_starts_daemons(service, monkeypatch, password):
    (service[0] / 'keyring-unlock').write_bytes(password)
    monkeypatch.setattr(systemd.subprocess, 'run', lambda *a, **kw: pytest.fail('Started with invalid key'))
    with pytest.raises(ValueError, match='credential is invalid'):
        systemd.unlock_keyring()


def test_missing_credential_fails_without_starting(service, monkeypatch):
    (service[0] / 'keyring-unlock').unlink()
    monkeypatch.setattr(systemd.subprocess, 'run', lambda *a, **kw: pytest.fail('Started without key'))
    with pytest.raises(FileNotFoundError):
        systemd.unlock_keyring()


@pytest.mark.parametrize('failure', [0, 1, 2])
def test_each_startup_failure_prevents_exec_and_redacts_diagnostics(service, monkeypatch, capsys, failure):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == failure + 1:
            raise subprocess.TimeoutExpired('private diagnostic', 15)

    monkeypatch.setattr(systemd.subprocess, 'run', run)
    monkeypatch.setattr(systemd.os, 'execv', lambda *a: pytest.fail('Started after credential failure'))
    assert systemd.main() == 1
    assert len(calls) == failure + 1
    output = capsys.readouterr()
    assert 'credential initialization failed' in output.err
    assert 'private diagnostic' not in output.err and 'synthetic-unlock-key' not in output.err


def test_success_execs_the_existing_team_entrypoint_preserving_the_supervisor_pid(monkeypatch):
    calls = []
    monkeypatch.setattr(systemd, 'unlock_keyring', lambda: calls.append('unlocked'))
    monkeypatch.setattr(systemd.sys, 'argv', ['systemd', '--data-dir', '/workspace', '--browser-mode', 'remote'])
    monkeypatch.setattr(systemd.os, 'execv', lambda *args: calls.append(args))
    systemd.main()
    assert calls == ['unlocked', (sys.executable, [sys.executable, '-m', 'camoufox', 'studio-team',
                                                 '--data-dir', '/workspace', '--browser-mode', 'remote'])]


@pytest.mark.parametrize('locked,path', [(True, '/org/freedesktop/secrets/collection/login'),
                                       (False, '/org/freedesktop/secrets/collection/session')])
def test_locked_or_volatile_keyrings_are_rejected_without_a_prompt(monkeypatch, locked, path):
    closed = []
    monkeypatch.setitem(sys.modules, 'secretstorage', SimpleNamespace(
        dbus_init=lambda: SimpleNamespace(close=lambda: closed.append(True)),
        get_collection_by_alias=lambda conn, alias: SimpleNamespace(is_locked=lambda: locked, collection_path=path)))
    monkeypatch.setattr(systemd, 'KeyringSecretStore', lambda *a: pytest.fail('Accessed invalid keyring'))
    with pytest.raises(SecretStoreError, match='not unlocked'):
        systemd.check_keyring()
    assert closed == [True]


@pytest.mark.parametrize('corrupt', [False, True])
def test_probe_round_trips_and_removes_only_its_own_synthetic_secret(monkeypatch, corrupt):
    values = {'existing-user-secret': 'unchanged'}
    monkeypatch.setitem(sys.modules, 'secretstorage', SimpleNamespace(
        dbus_init=lambda: SimpleNamespace(close=lambda: None),
        get_collection_by_alias=lambda conn, alias: SimpleNamespace(
            is_locked=lambda: False, collection_path='/org/freedesktop/secrets/collection/login')))

    def store(namespace):
        assert namespace == 'camoufox-studio-service-check'
        return SimpleNamespace(set=lambda key, value: values.update({key: value}),
                               get=lambda key: 'corrupt' if corrupt else values[key],
                               delete=lambda key: values.pop(key, None))

    monkeypatch.setattr(systemd, 'KeyringSecretStore', store)
    if corrupt:
        with pytest.raises(SecretStoreError, match='read/write check failed'):
            systemd.check_keyring()
    else:
        systemd.check_keyring()
    assert values == {'existing-user-secret': 'unchanged'}
