#!/usr/bin/env python3
"""Install the source Studio desktop for the current Ubuntu user."""

import argparse
import os
import platform
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    'python3-venv', 'libegl1', 'libgl1', 'libxkbcommon-x11-0',
    'libxcb-cursor0', 'libxcb-icccm4', 'libxcb-image0', 'libxcb-keysyms1',
    'libxcb-render-util0', 'libxcb-xinerama0', 'libxcb-xkb1',
    'libsecret-1-0', 'gnome-keyring', 'dbus-user-session', 'xwayland',
    'fonts-noto-cjk', 'desktop-file-utils',
)


def validate_platform(release, machine, version):
    try:
        ubuntu = tuple(int(part) for part in release.get('VERSION_ID', '').split('.'))
    except ValueError:
        ubuntu = ()
    if release.get('ID') != 'ubuntu' or ubuntu < (24, 4):
        raise ValueError('Ubuntu Desktop 24.04 or newer is required.')
    if machine.lower() not in ('x86_64', 'amd64', 'aarch64', 'arm64'):
        raise ValueError('Only x64 and arm64 are supported, when upstream wheels and browser assets exist.')
    if version < (3, 12):
        raise ValueError('Python 3.12 or newer is required by the pinned desktop dependencies.')


def desktop_value(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def desktop_argument(value):
    # Exec quoting follows the desktop specification, then the string-value escaping layer.
    value = str(value)
    if any(character in value for character in '\r\n\x00'):
        raise ValueError('Desktop launcher paths cannot contain line breaks or NUL.')
    quoted = ''.join('\\' + character if character in '\\"`$' else character for character in value)
    return desktop_value('"' + quoted.replace('%', '%%') + '"')


def desktop_entry(root):
    command = ' '.join(desktop_argument(part) for part in ('/bin/bash', root / 'start-studio.sh'))
    return ('[Desktop Entry]\nType=Application\nVersion=1.0\n'
            'Name=TigerBrowser\nComment=Manage persistent browser environments\n'
            f'Exec={command}\nPath={desktop_value(root)}\n'
            f'Icon={desktop_value(root / "assets/studio/default128.png")}\n'
            'Terminal=false\nCategories=Network;WebBrowser;\nStartupNotify=true\n')


def run(arguments, *, root=ROOT, environment=None):
    subprocess.run([str(argument) for argument in arguments], cwd=root,
                   env=environment, check=True)


def install(root, *, system_dependencies=True):
    environment = os.environ.copy()
    # An ambient Python installation must not replace this checkout's editable package.
    environment.pop('PYTHONPATH', None)
    environment.pop('PYTHONHOME', None)
    if system_dependencies:
        run(['sudo', 'apt-get', 'update'], root=root, environment=environment)
        run(['sudo', 'apt-get', 'install', '-y', *PACKAGES], root=root, environment=environment)
    venv = root / '.venv-studio'
    python = venv / 'bin/python'
    if venv.exists() and not python.is_file():
        raise ValueError('.venv-studio already exists but is not a Linux virtual environment; choose a fresh checkout.')
    if not python.is_file():
        run([sys.executable, '-m', 'venv', venv], root=root, environment=environment)
    run([python, '-m', 'pip', 'install', '--only-binary=:all:', '-r',
         root / 'pythonlib/studio-requirements.lock'], root=root, environment=environment)
    run([python, '-m', 'pip', 'install', '--no-deps', '-e', './pythonlib[studio]'],
        root=root, environment=environment)
    if system_dependencies:
        run(['sudo', python, '-m', 'playwright', 'install-deps', 'firefox'],
            root=root, environment=environment)
    run([python, '-m', 'camoufox', 'fetch'], root=root, environment=environment)
    # The existing fetch command can report a missing asset without a failing exit status.
    run([python, '-c',
         'from PySide6.QtWebEngineWidgets import QWebEngineView; '
         'from pathlib import Path; '
         'from camoufox.pkgman import camoufox_path, launch_path; '
         'from camoufox.fpgen_model import is_pinned; '
         'from camoufox.addons import confirm_paths; '
         'from camoufox.studio.distribution import default_addon_paths; '
         'assert Path(launch_path(camoufox_path(False))).is_file(), "Camoufox browser is missing"; '
         'assert is_pinned(), "Verified fingerprint model is missing"; '
         'confirm_paths(default_addon_paths())'], root=root, environment=environment)
    data_home = Path(environment.get('XDG_DATA_HOME') or Path.home() / '.local/share').expanduser()
    if not data_home.is_absolute():
        raise ValueError('XDG_DATA_HOME must be an absolute path.')
    applications = data_home / 'applications'
    applications.mkdir(parents=True, exist_ok=True)
    entry = applications / 'tigerbrowser.desktop'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                     suffix='.desktop', dir=applications, delete=False) as output:
        temporary = Path(output.name)
        output.write(desktop_entry(root))
    try:
        run(['desktop-file-validate', temporary], root=root, environment=environment)
        temporary.chmod(0o644)
        temporary.replace(entry)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Installed desktop entry: {entry}')
    print('Open TigerBrowser from Applications in your Ubuntu desktop session.')
    print('Saving proxy credentials requires the desktop session keyring to be unlocked.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-system-deps', action='store_true',
                        help='Skip apt and Playwright system dependencies after they are already installed.')
    args = parser.parse_args(argv)
    try:
        if sys.platform != 'linux':
            raise ValueError('Run this installer on Ubuntu Desktop, not from Windows Python.')
        if os.geteuid() == 0:
            raise ValueError('Run as your desktop user, without sudo; only system dependency commands use sudo.')
        validate_platform(platform.freedesktop_os_release(), platform.machine(), sys.version_info[:2])
        install(ROOT, system_dependencies=not args.skip_system_deps)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Installation failed: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
