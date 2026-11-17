"""Ubuntu server installation plans, the generated systemd unit and its environment file."""

import argparse
import importlib.util
import json
import subprocess
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('studio_server_install', ROOT / 'scripts/install-studio-server.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def options(**changes):
    values = {'origin': 'http://192.168.1.10:8766', 'listen': '0.0.0.0', 'browser_mode': 'remote',
              'private_network_only': True, 'allow_client': [], 'automation': False, 'cert': None, 'key': None,
              'admin_name': 'admin', 'admin_display': 'Administrator'}
    return argparse.Namespace(**{**values, **changes})


def layout(tmp_path):
    return installer.Layout(program=tmp_path / 'opt', state=tmp_path / 'state', config=tmp_path / 'etc',
                            unit=tmp_path / 'camoufox-studio.service', build_cache=tmp_path / 'cache')


@pytest.mark.parametrize('release, machine, version', [
    ({'ID': 'ubuntu', 'VERSION_ID': '22.04'}, 'x86_64', (3, 12)),
    ({'ID': 'debian', 'VERSION_ID': '13'}, 'x86_64', (3, 12)),
    ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, 'aarch64', (3, 12)),
    ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, 'x86_64', (3, 9)),
])
def test_unsupported_hosts_fail_before_install(release, machine, version):
    with pytest.raises(ValueError):
        installer.validate_platform(release, machine, version)
    installer.validate_platform({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, 'x86_64', (3, 12))


def test_unit_requires_explicit_recovery_and_keeps_user_namespaces(tmp_path):
    unit = installer.unit_file(layout(tmp_path))
    assert 'User=camoufox' in unit and 'Restart=no' in unit and 'KillMode=mixed' in unit
    assert 'Type=exec\n' in unit
    assert f'EnvironmentFile={tmp_path / "etc/studio.env"}' in unit
    for hardening in ('RestrictNamespaces', 'PrivateUsers'):
        assert hardening not in unit
    command = next(line for line in unit.splitlines() if line.startswith('ExecStart='))
    for option in ('--kernel-root', '--staging-root', '--client-root'):
        assert option in command
    assert '--origin ${STUDIO_ORIGIN}' in command and command.endswith('$STUDIO_EXTRA')


def test_service_provisions_an_encrypted_keyring_without_a_login_session(tmp_path):
    paths = layout(tmp_path)
    unit = installer.unit_file(paths)
    assert {'dbus-daemon', 'gnome-keyring'} <= set(installer.PACKAGES)
    assert f'LoadCredentialEncrypted=keyring-unlock:{paths.config / "keyring-unlock.cred"}' in unit
    assert 'RuntimeDirectory=camoufox-studio' in unit
    assert 'RuntimeDirectoryMode=0700' in unit
    assert f'Environment=HOME={paths.state}' in unit
    assert 'UMask=0077' in unit
    assert '-m camoufox.studio.team.systemd ' in unit
    assert 'LimitCORE=0' in unit


def test_environment_records_scope_and_rejects_values_systemd_would_split(tmp_path):
    text = installer.environment_file(options(allow_client=['192.168.1.0/24', '10.0.0.5'], automation=True),
                                      PurePosixPath('/opt/camoufox-studio/kasmvnc-1.5.0-studio.2'))
    assert 'STUDIO_ORIGIN=http://192.168.1.10:8766\n' in text
    assert ('STUDIO_EXTRA=--private-network-only --allow-client 192.168.1.0/24 --allow-client 10.0.0.5/32 '
            '--automation-listen\n') in text
    assert 'CAMOUFOX_KASMVNC_TOOLS=/opt/camoufox-studio/kasmvnc-1.5.0-studio.2\n' in text
    for bad in (options(origin='http://host:8766 --listen'), options(allow_client=['not-an-ip']),
                options(origin='https://team.test:8766'), options(cert='/tls/chain.pem')):
        with pytest.raises(ValueError):
            installer.environment_file(bad, None)


def plan(tmp_path, monkeypatch, *, workspace=False, environment=False, active=False):
    paths = layout(tmp_path)
    if workspace:
        paths.workspace.mkdir(parents=True)
    if environment:
        paths.config.mkdir(parents=True)
        paths.environment.write_text('STUDIO_ORIGIN=http://192.168.1.20:8766\n')
    commands = []
    monkeypatch.setattr(installer, 'run', lambda arguments, **_: commands.append([str(item) for item in arguments]))
    monkeypatch.setattr(installer, 'service_active', lambda: active)
    monkeypatch.setattr(installer, 'account_exists', lambda: False)
    monkeypatch.setattr(installer, 'build_frontend', lambda user: commands.append(['build-frontend', str(user)]))
    monkeypatch.setattr(installer, 'provision_keyring',
                        lambda paths: commands.append(['provision-keyring', str(paths.credential)]))
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=json.dumps({
        'settings': {'origin': 'http://192.168.1.20:8766', 'listen': '0.0.0.0', 'browser_mode': 'disabled',
                     'cert': None, 'key': None, 'automation_listen': False, 'automation_port': 8767},
        'kasmvnc': None})))
    return paths, commands


def test_fresh_install_runs_downloads_and_setup_as_the_service_account(tmp_path, monkeypatch):
    paths, commands = plan(tmp_path, monkeypatch)
    assert installer.install(paths, options(), invoking_user='maintainer') is False
    # runuser -u camoufox -- env HOME=<state> <command>
    service = [command[6:] for command in commands if command[:5] == ['runuser', '-u', 'camoufox', '--', 'env']]
    assert [str(paths.python), '-m', 'camoufox', 'fetch'] in service
    init = next(command for command in service if 'studio-team-init' in command)
    assert init[init.index('--data-dir') + 1] == str(paths.workspace)
    assert not any('password' in item.lower() for command in commands for item in command)
    assert ['build-frontend', 'maintainer'] in commands
    kasm = next(command for command in commands if command[1].endswith('install-studio-kasmvnc.py'))
    assert kasm[0] == str(paths.python) and str(installer.kasmvnc_prefix(paths)) in kasm
    assert commands[-1] == ['systemctl', 'enable', '--now', 'camoufox-studio']
    assert ['provision-keyring', str(paths.credential)] in commands
    created = next(command for command in commands if command[:3] == ['install', '-d', '-m'] and '0700' in command)
    for directory in (paths.workspace.parent, paths.kernels, paths.staging, paths.clients):
        assert str(directory) in created


def test_upgrade_keeps_the_workspace_and_settings_and_refuses_a_running_service(tmp_path, monkeypatch):
    paths, commands = plan(tmp_path, monkeypatch, workspace=True, environment=True)
    assert installer.install(paths, options(browser_mode='disabled'), invoking_user=None) is True
    assert not any('studio-team-init' in command for command in commands)
    assert not any(command[1].endswith('install-studio-kasmvnc.py') for command in commands if len(command) > 1)
    assert not any(str(paths.environment) in command for command in commands)
    assert paths.environment.read_text() == 'STUDIO_ORIGIN=http://192.168.1.20:8766\n'
    paths, commands = plan(tmp_path / 'running', monkeypatch, active=True)
    with pytest.raises(ValueError, match='Stop the running service'):
        installer.install(paths, options())
    assert commands == []


def test_summary_suggests_a_lan_firewall_rule_and_explicit_recovery(tmp_path):
    text = installer.summary(layout(tmp_path), options(automation=True), False)
    assert 'ufw allow from 192.168.1.0/24 to any port 8766 proto tcp' in text
    assert 'port 8767' in text and 'studio-recover --data-dir' in text and 'without encryption' in text
    assert 'is serving' not in text
    assert 'systemctl status' in text


def test_upgrade_checks_preserved_remote_runtime_and_tls_not_cli_defaults(tmp_path, monkeypatch):
    paths, commands = plan(tmp_path, monkeypatch, workspace=True, environment=True)
    stored = {'origin': 'https://team.example:9443', 'listen': '::', 'browser_mode': 'remote',
              'cert': '/tls/server.crt', 'key': '/tls/server.key', 'automation_listen': True,
              'automation_port': 9444}
    runtime = tmp_path / 'existing-kasmvnc'
    runtime.mkdir()
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: SimpleNamespace(
        stdout=json.dumps({'settings': stored, 'kasmvnc': str(runtime)})))
    args = options(browser_mode='disabled')
    assert installer.install(paths, args) is True
    assert any(f'CAMOUFOX_KASMVNC_TOOLS={runtime}' in command for command in commands)
    assert any(command[-3:] == ['test', '-r', str(Path('/tls/server.crt').resolve())] for command in commands)
    assert any(command[-3:] == ['test', '-r', str(Path('/tls/server.key').resolve())] for command in commands)
    assert not any(command[1].endswith('install-studio-kasmvnc.py') for command in commands if len(command) > 1)
    text = installer.summary(paths, args, True)
    assert 'port 9443' in text and 'port 9444' in text
    assert 'without encryption' not in text


def test_preserved_settings_use_systemd_expansion_and_the_existing_cli(tmp_path, monkeypatch):
    paths = layout(tmp_path)
    calls = []

    def read(command, **kwargs):
        calls.append(command)
        assert kwargs == {'capture_output': True, 'text': True, 'check': True, 'timeout': 30}
        return SimpleNamespace(stdout=json.dumps({'settings': {
            'origin': 'http://server:8766', 'listen': '::', 'browser_mode': 'headless',
            'cert': None, 'key': None, 'automation_listen': True, 'automation_port': 9000}, 'kasmvnc': None}))

    monkeypatch.setattr(installer.subprocess, 'run', read)
    args = options()
    assert installer.upgrade_settings(paths, args) is None
    command, = calls
    assert '--expand-environment=yes' in command
    assert f'--property=EnvironmentFile={paths.environment}' in command
    assert '--property=User=camoufox' in command
    assert '--property=RuntimeMaxSec=25' in command
    assert command[-len(installer.service_arguments(paths)):] == installer.service_arguments(paths)
    assert 'studio_team_cmd.make_context' in command[command.index('-c') + 1]
    assert args.origin == 'http://server:8766' and args.automation_port == 9000


@pytest.mark.parametrize('configured', [False, True])
def test_upgrade_rejects_missing_preserved_remote_runtime_without_rebuilding_or_starting(
        tmp_path, monkeypatch, configured):
    paths, commands = plan(tmp_path, monkeypatch, workspace=True, environment=True)
    runtime = tmp_path / 'missing-kasmvnc'
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: SimpleNamespace(stdout=json.dumps({
        'settings': {'origin': 'http://server:8766', 'listen': '127.0.0.1', 'browser_mode': 'remote',
                     'cert': None, 'key': None, 'automation_listen': False, 'automation_port': 8767},
        'kasmvnc': str(runtime) if configured else None})))
    original = paths.environment.read_bytes()
    with pytest.raises(ValueError, match='CAMOUFOX_KASMVNC_TOOLS'):
        installer.install(paths, options(browser_mode='disabled'))
    assert not any('install-studio-kasmvnc.py' in item for command in commands for item in command)
    assert not any(command[0] == 'systemctl' for command in commands)
    assert not runtime.exists()
    assert paths.environment.read_bytes() == original
    assert list(paths.workspace.iterdir()) == []


def test_invalid_preserved_settings_fail_without_leaking_diagnostics(tmp_path, monkeypatch):
    paths, commands = plan(tmp_path, monkeypatch, workspace=True, environment=True)

    def reject(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr='private diagnostic')

    monkeypatch.setattr(installer.subprocess, 'run', reject)
    with pytest.raises(ValueError, match='preserved settings') as error:
        installer.install(paths, options())
    assert 'private diagnostic' not in str(error.value)
    assert not any('enable' in command for command in commands)


def test_unlock_key_is_generated_in_memory_and_only_ciphertext_is_installed(tmp_path, monkeypatch):
    paths = layout(tmp_path)
    encrypted = b'encrypted-systemd-credential\n'
    calls = []

    def encrypt(command, **kwargs):
        assert command == ['systemd-creds', 'encrypt', '--with-key=host', '--name=keyring-unlock', '-', '-']
        assert len(kwargs['input']) == 64
        assert kwargs['input'].decode() not in str(command)
        assert kwargs['capture_output'] and kwargs['check'] and kwargs['timeout'] == 30
        calls.append('encrypted')
        return SimpleNamespace(stdout=encrypted)

    monkeypatch.setattr(installer.subprocess, 'run', encrypt)
    monkeypatch.setattr(installer, 'place', lambda *args: calls.append(args))
    installer.provision_keyring(paths)
    assert calls == ['encrypted', (encrypted.decode(), paths.credential, '0600')]
    assert not paths.state.exists()


def test_upgrade_never_replaces_an_existing_unlock_key(tmp_path, monkeypatch):
    paths = layout(tmp_path)
    paths.config.mkdir()
    paths.credential.write_bytes(b'existing encrypted credential')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: pytest.fail('Replaced unlock key'))
    installer.provision_keyring(paths)
    assert paths.credential.read_bytes() == b'existing encrypted credential'


def test_existing_keyring_without_unlock_key_requires_explicit_adoption(tmp_path, monkeypatch):
    paths = layout(tmp_path)
    keyrings = paths.state / '.local/share/keyrings'
    keyrings.mkdir(parents=True)
    original = keyrings / 'login.keyring'
    original.write_bytes(b'existing encrypted keyring')
    monkeypatch.setattr(installer.subprocess, 'run', lambda *a, **kw: pytest.fail('Generated a replacement'))
    with pytest.raises(ValueError, match='Existing service keyrings'):
        installer.provision_keyring(paths)
    assert original.read_bytes() == b'existing encrypted keyring'


def test_encryption_failure_is_redacted_and_does_not_publish_a_credential(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr=b'private diagnostic')

    monkeypatch.setattr(installer.subprocess, 'run', fail)
    monkeypatch.setattr(installer, 'place', lambda *a: pytest.fail('Published failed encryption'))
    with pytest.raises(ValueError, match='Could not encrypt') as error:
        installer.provision_keyring(layout(tmp_path))
    assert 'private diagnostic' not in str(error.value)
