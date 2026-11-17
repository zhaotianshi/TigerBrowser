"""Real systemd credential delivery and headless native keyring persistence on Ubuntu."""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_upgrade_reads_effective_systemd_settings_without_starting_the_server(monkeypatch):
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.fail('Run this check as root on Ubuntu 24.04 with systemd installed.')
    import argparse

    spec = importlib.util.spec_from_file_location('studio_server_install', ROOT / 'scripts/install-studio-server.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    # Nobody's primary group is named nogroup on Ubuntu; use its numeric ID for both properties.
    import pwd
    monkeypatch.setattr(installer, 'ACCOUNT', str(pwd.getpwnam('nobody').pw_uid))
    monkeypatch.setattr(installer.Layout, 'python', property(lambda self: Path(sys.executable)))
    with tempfile.TemporaryDirectory(prefix='studio-settings-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        layout = installer.Layout(program=root, state=root / 'state', config=root)
        for path in (layout.workspace, layout.clients):
            path.mkdir(parents=True)
        layout.environment.write_text(
            'STUDIO_ORIGIN="http://127.0.0.1:9876"\nSTUDIO_LISTEN=127.0.0.1\n'
            'STUDIO_BROWSER_MODE=headless\n'
            "STUDIO_EXTRA='--private-network-only --allow-client 127.0.0.1 --automation-listen --automation-port 9877'\n"
            'CAMOUFOX_KASMVNC_TOOLS=/opt/preserved-runtime\n', encoding='utf-8')
        original = layout.environment.read_bytes()
        args = argparse.Namespace()
        assert installer.upgrade_settings(layout, args) == Path('/opt/preserved-runtime')
        assert args.origin == 'http://127.0.0.1:9876'
        assert args.browser_mode == 'headless'
        assert args.automation and args.automation_port == 9877
        assert layout.environment.read_bytes() == original
        assert not list(layout.workspace.iterdir()), 'Reading settings must not initialize a workspace.'


def test_systemd_keyring_persists_across_service_restarts_and_rejects_wrong_keys():
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.fail('Run this check as root on Ubuntu 24.04 with systemd and gnome-keyring installed.')
    import pwd

    spec = importlib.util.spec_from_file_location('studio_server_install', ROOT / 'scripts/install-studio-server.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    account = pwd.getpwnam('nobody')
    with tempfile.TemporaryDirectory(prefix='studio-credentials-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        state = root / 'state'
        state.mkdir(mode=0o700)
        os.chown(state, account.pw_uid, account.pw_gid)
        config = root / 'config'
        config.mkdir(mode=0o700)
        layout = installer.Layout(state=state, config=config)
        installer.provision_keyring(layout)
        encrypted = layout.credential.read_bytes()
        assert layout.credential.stat().st_mode & 0o777 == 0o600

        def run(credential, action):
            name = 'studio-credential-check-' + uuid4().hex
            program = '''
import os, sys
from camoufox.studio.team.systemd import unlock_keyring
from camoufox.studio.secrets import KeyringSecretStore
try:
    unlock_keyring()
except Exception:
    sys.exit(23)
assert 'CREDENTIALS_DIRECTORY' not in os.environ
store = KeyringSecretStore('studio-runtime-check')
if sys.argv[1] == 'write':
    store.set('proxy', 'synthetic-persistent-proxy')
assert store.get('proxy') == 'synthetic-persistent-proxy'
assert KeyringSecretStore('camoufox-studio-service-check').get('startup') is None
print('native-keyring-ok')
'''
            command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', f'--unit={name}',
                       '-p', f'User={account.pw_uid}', '-p', f'Group={account.pw_gid}',
                       '-p', f'RuntimeDirectory={name}', '-p', 'RuntimeDirectoryMode=0700',
                       '-p', 'KillMode=mixed', '-p', 'TimeoutStopSec=15', '-p', 'RuntimeMaxSec=90',
                       '-p', 'UMask=0077', '-p', 'LimitCORE=0',
                       '-p', f'LoadCredentialEncrypted=keyring-unlock:{credential}',
                       f'--setenv=HOME={state}', sys.executable]
            command += (['-m', 'camoufox.studio.team.systemd', '--help'] if action == 'entrypoint'
                        else ['-c', program, action])
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            finally:
                subprocess.run(['systemctl', 'stop', name], capture_output=True, timeout=30)
            assert not Path('/run', name).exists(), 'The stopped service left its credential session behind.'
            return result

        first = run(layout.credential, 'write')
        assert first.returncode == 0, first.stderr
        assert 'native-keyring-ok' in first.stdout
        keyring = state / '.local/share/keyrings/login.keyring'
        assert keyring.is_file()
        assert b'synthetic-persistent-proxy' not in keyring.read_bytes()
        installer.provision_keyring(layout)
        assert layout.credential.read_bytes() == encrypted
        second = run(layout.credential, 'read')
        assert second.returncode == 0, second.stderr
        assert 'native-keyring-ok' in second.stdout
        entrypoint = run(layout.credential, 'entrypoint')
        assert entrypoint.returncode == 0, entrypoint.stderr
        assert '--data-dir' in entrypoint.stdout

        wrong_config = root / 'wrong-config'
        wrong_config.mkdir(mode=0o700)
        wrong = installer.Layout(state=root / 'unused-state', config=wrong_config)
        installer.provision_keyring(wrong)
        rejected = run(wrong.credential, 'read')
        assert rejected.returncode == 23
        assert 'native-keyring-ok' not in rejected.stdout
        # A failed unlock must neither replace nor erase the existing keyring.
        recovered = run(layout.credential, 'read')
        assert recovered.returncode == 0, recovered.stderr
        assert 'native-keyring-ok' in recovered.stdout
