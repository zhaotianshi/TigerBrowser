"""Initialize the installed systemd service's private native credential session."""

import os
import secrets
import subprocess
import sys
from contextlib import closing
from pathlib import Path

from ..secrets import KeyringSecretStore, SecretStoreError


def check_keyring():
    import secretstorage

    with closing(secretstorage.dbus_init()) as connection:
        collection = secretstorage.get_collection_by_alias(connection, 'default')
        if collection.is_locked() or collection.collection_path != '/org/freedesktop/secrets/collection/login':
            raise SecretStoreError('The persistent service login keyring is not unlocked.')
    store = KeyringSecretStore('camoufox-studio-service-check')
    value = secrets.token_urlsafe(32)
    try:
        store.set('startup', value)
        if store.get('startup') != value:
            raise SecretStoreError('The service keyring read/write check failed.')
    finally:
        store.delete('startup')


def unlock_keyring():
    """Read systemd's decrypted credential only into memory and the daemon's stdin."""
    credential_root = os.environ.pop('CREDENTIALS_DIRECTORY')
    runtime = Path(os.environ['RUNTIME_DIRECTORY'])
    if not runtime.is_dir():
        raise ValueError('The systemd runtime directory is missing.')
    with (Path(credential_root) / 'keyring-unlock').open('rb') as stream:
        password = stream.read(4097)
    if not password or len(password) > 4096 or b'\0' in password:
        raise ValueError('The service keyring credential is invalid.')
    # Never reuse a desktop session or a caller-selected in-memory credential collection.
    for name in tuple(os.environ):
        if name.startswith(('DBUS_', 'GNOME_KEYRING_', 'KEYRING_PROPERTY_')):
            os.environ.pop(name)
    os.environ.update({
        'XDG_RUNTIME_DIR': str(runtime),
        'XDG_DATA_HOME': str(Path(os.environ['HOME']) / '.local/share'),
        'PYTHON_KEYRING_BACKEND': 'keyring.backends.SecretService.Keyring',
        'DBUS_SESSION_BUS_ADDRESS': f'unix:path={runtime}/bus',
    })
    # These daemons stay in the service cgroup. exec preserves the supervisor PID,
    # so KillMode=mixed still gives browser shutdown the first and only SIGTERM.
    subprocess.run(['dbus-daemon', '--session', '--fork', '--nopidfile',
                    f'--address={os.environ["DBUS_SESSION_BUS_ADDRESS"]}'],
                   check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(['gnome-keyring-daemon', '--unlock', '--components=secrets',
                    f'--control-directory={runtime}/keyring'],
                   input=password, check=True, timeout=15,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    del password
    subprocess.run([sys.executable, '-c',
                    'from camoufox.studio.team.systemd import check_keyring; check_keyring()'],
                   check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    try:
        unlock_keyring()
    except Exception:
        print('Studio service credential initialization failed. Check the encrypted unlock credential, '
              'service keyring and D-Bus dependencies; see docs/studio-team-service.md. '
              'No credential fallback was used.', file=sys.stderr)
        return 1
    os.execv(sys.executable, [sys.executable, '-m', 'camoufox', 'studio-team', *sys.argv[1:]])


if __name__ == '__main__':
    raise SystemExit(main())
