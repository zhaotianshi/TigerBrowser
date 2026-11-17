"""Actual systemd team entrypoint, HTTP proxy credentials and clean service restarts."""

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_nonlogin_service_saves_proxy_credentials_and_reopens_the_same_workspace():
    if sys.platform != 'linux' or os.geteuid() != 0:
        pytest.fail('Run as root on Ubuntu 24.04 with systemd, native keyring and bubblewrap installed.')
    import pwd

    spec = importlib.util.spec_from_file_location('studio_server_install', ROOT / 'scripts/install-studio-server.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    account = pwd.getpwnam('nobody')
    with tempfile.TemporaryDirectory(prefix='studio-service-lifecycle-', dir='/var/tmp') as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        state = root / 'state'
        state.mkdir(mode=0o700)
        os.chown(state, account.pw_uid, account.pw_gid)
        config = root / 'config'
        config.mkdir(mode=0o700)
        layout = installer.Layout(state=state, config=config)
        installer.provision_keyring(layout)
        user = ['runuser', '-u', account.pw_name, '--', 'env', f'HOME={state}']
        # Detach the terminal so getpass consumes the test's stdin rather than /dev/tty.
        initialized = subprocess.run([
            *user, sys.executable, '-m', 'camoufox', 'studio-team-init',
            '--data-dir', str(layout.workspace), '--name', 'root', '--display', 'Administrator',
        ], input='synthetic-admin-password\nsynthetic-admin-password\n',
            capture_output=True, text=True, timeout=30, start_new_session=True)
        assert initialized.returncode == 0, 'The real terminal initialization failed.'
        assert 'synthetic-admin-password' not in initialized.stdout
        proxy_id = None
        for cycle in range(2):
            name = 'studio-lifecycle-' + uuid4().hex
            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1', 0))
                port = reservation.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            command = [
                'systemd-run', '--quiet', '--collect', f'--unit={name}',
                '-p', 'Type=exec', '-p', f'User={account.pw_uid}', '-p', f'Group={account.pw_gid}',
                '-p', f'RuntimeDirectory={name}', '-p', 'RuntimeDirectoryMode=0700',
                '-p', 'KillMode=mixed', '-p', 'TimeoutStopSec=30', '-p', 'RuntimeMaxSec=120',
                '-p', 'UMask=0077', '-p', 'LimitCORE=0',
                '-p', f'LoadCredentialEncrypted=keyring-unlock:{layout.credential}',
                f'--setenv=HOME={state}', sys.executable, '-m', 'camoufox.studio.team.systemd',
                '--data-dir', str(layout.workspace), '--origin', origin, '--browser-mode', 'disabled',
            ]
            try:
                subprocess.run(command, capture_output=True, check=True, timeout=30)
                with httpx.Client(base_url=origin, trust_env=False, timeout=2) as client:
                    deadline = time.monotonic() + 30
                    while True:
                        try:
                            if client.get('/').status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        if time.monotonic() >= deadline:
                            pytest.fail('The actual systemd team service did not become HTTP-ready.')
                        time.sleep(.1)
                    response = client.post('/team/v1/login', headers={'Origin': origin},
                                           json={'name': 'root', 'password': 'synthetic-admin-password'})
                    assert response.status_code == 200
                    headers = {'Origin': origin, 'X-CSRF-Token': response.json()['csrf']}
                    if cycle == 0:
                        saved = client.post('/team/v1/proxies', headers=headers, json={
                            'name': 'Lifecycle proxy', 'server': 'http://127.0.0.1:9',
                            'username': 'synthetic-user', 'password': 'synthetic-proxy-password',
                        })
                        assert saved.status_code == 201
                        proxy_id = saved.json()['id']
                    listed = client.get('/team/v1/proxies')
                    assert listed.status_code == 200
                    assert [proxy['id'] for proxy in listed.json()['proxies']] == [proxy_id]
                    assert listed.json()['proxies'][0]['has_password'] is True
                    assert 'synthetic-proxy-password' not in listed.text
                    # Read through the same native bus under the same account, not a fake store
                    # or the parent's graphical session; no secret is placed in arguments.
                    verified = subprocess.run([
                        *user, f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/{name}/bus',
                        f'XDG_DATA_HOME={state}/.local/share',
                        'PYTHON_KEYRING_BACKEND=keyring.backends.SecretService.Keyring',
                        sys.executable, '-c',
                        'import sys; from camoufox.studio.store import ProfileStore; '
                        "assert ProfileStore(sys.argv[1]).get_proxy(sys.argv[2], reveal_password=True)['password'] == sys.stdin.read()",
                        str(layout.workspace), proxy_id,
                    ], input='synthetic-proxy-password', capture_output=True, text=True, timeout=15)
                    assert verified.returncode == 0, 'The persisted native proxy credential could not be read.'
            finally:
                stopped = subprocess.run(['systemctl', 'stop', name], capture_output=True, timeout=40)
                assert stopped.returncode == 0, 'The transient test service did not stop cleanly.'
            assert not (layout.workspace / 'workspace.lock').exists(), 'Clean stop retained workspace ownership.'
            assert not Path('/run', name).exists(), 'The transient credential session survived service stop.'
        assert b'synthetic-proxy-password' not in (layout.workspace / 'studio.sqlite3').read_bytes()
        assert b'synthetic-proxy-password' not in (state / '.local/share/keyrings/login.keyring').read_bytes()
