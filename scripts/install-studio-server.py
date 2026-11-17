#!/usr/bin/env python3
"""Install or upgrade a Studio team server on Ubuntu 24.04 as the camoufox-studio systemd service."""

import argparse
import ipaddress
import json
import os
import platform
import secrets
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'camoufox'
SERVICE = 'camoufox-studio'
# Every mode needs kernel containment and native credentials; remote mode also needs display tools.
PACKAGES = ('python3-venv', 'bubblewrap', 'xauth', 'xdotool', 'xclip', 'fontconfig',
            'dbus-daemon', 'gnome-keyring')
CHECK_NAMESPACE = ('import subprocess; from camoufox.studio.team.native import namespace; '
                   "subprocess.run([*namespace(), '--', '/usr/bin/true'], check=True)")
CACHE_KERNEL = '''import sys
from camoufox import multiversion
from camoufox.studio.kernel_cache import KernelCache
from camoufox.studio.kernels import kernel_id
active = multiversion.get_active_path(update_config=False)
[installed] = [item for item in multiversion.list_installed() if item.path == active]
print(KernelCache(sys.argv[1]).cache(kernel_id(installed))['kernel_id'])
'''


@dataclass(frozen=True)
class Layout:
    program: Path = Path('/opt/camoufox-studio')
    state: Path = Path('/var/lib/camoufox-studio')
    config: Path = Path('/etc/camoufox-studio')
    unit: Path = Path(f'/etc/systemd/system/{SERVICE}.service')
    build_cache: Path = Path('/var/cache/camoufox-studio')

    @property
    def python(self):
        return self.program / '.venv/bin/python'

    @property
    def environment(self):
        return self.config / 'studio.env'

    @property
    def credential(self):
        return self.config / 'keyring-unlock.cred'

    # Packages and staging stay outside the workspace; staging shares its filesystem for atomic returns.
    @property
    def workspace(self):
        return self.state / 'workspace'

    @property
    def staging(self):
        return self.state / 'staging'

    @property
    def kernels(self):
        return self.state / 'kernels'

    @property
    def clients(self):
        return self.state / 'clients'


def validate_platform(release, machine, version):
    if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '24.04':
        raise ValueError('The server installer supports Ubuntu 24.04.')
    if machine != 'x86_64':
        raise ValueError('The server installer supports x86_64, the platform of the pinned remote display runtime.')
    if version < (3, 10):
        raise ValueError('Python 3.10 or newer is required.')


def plain(value, option):
    # Unbraced $STUDIO_EXTRA is split at whitespace by systemd.
    if not value or any(character.isspace() or character in '"\'\\$%;' for character in str(value)):
        raise ValueError(f'{option} cannot contain whitespace, quotes, backslashes or $ % ;.')
    return str(value)


def extra_options(args):
    if bool(args.cert) != bool(args.key):
        raise ValueError('Pass both --cert and --key for an HTTPS origin.')
    if (urlsplit(args.origin).scheme == 'https') != bool(args.cert):
        raise ValueError('An https:// origin needs --cert and --key; an http:// origin uses neither.')
    options = ['--private-network-only'] if args.private_network_only else []
    for client in args.allow_client:
        options += ['--allow-client', str(ipaddress.ip_network(client, strict=False))]
    if args.automation:
        options.append('--automation-listen')
    if args.cert:
        options += ['--cert', plain(Path(args.cert).resolve(), '--cert'), '--key', plain(Path(args.key).resolve(), '--key')]
    return options


def environment_file(args, kasmvnc):
    lines = ['# TigerBrowser team server; see docs/studio-team-service.md#ubuntu-服务器安装.',
             '# After editing: sudo systemctl restart camoufox-studio',
             f'STUDIO_ORIGIN={plain(args.origin, "--origin")}', f'STUDIO_LISTEN={args.listen}',
             f'STUDIO_BROWSER_MODE={args.browser_mode}', f'STUDIO_EXTRA={" ".join(extra_options(args))}']
    if kasmvnc:
        lines.append(f'CAMOUFOX_KASMVNC_TOOLS={kasmvnc}')
    return '\n'.join(lines) + '\n'


def service_arguments(layout):
    return list(map(str, (
        '--data-dir', layout.workspace,
        '--origin', '${STUDIO_ORIGIN}', '--listen', '${STUDIO_LISTEN}', '--browser-mode', '${STUDIO_BROWSER_MODE}',
        '--kernel-root', layout.kernels, '--staging-root', layout.staging, '--client-root', layout.clients,
        '$STUDIO_EXTRA')))


def unit_file(layout):
    command = ' '.join([str(layout.python), '-m', 'camoufox.studio.team.systemd', *service_arguments(layout)])
    return f'''[Unit]
Description=TigerBrowser team server
Documentation=https://github.com/zhaotianshi/TigerBrowser/blob/main/docs/studio-team-service.md
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User={ACCOUNT}
Group={ACCOUNT}
EnvironmentFile={layout.environment}
Environment=HOME={layout.state}
RuntimeDirectory={SERVICE}
RuntimeDirectoryMode=0700
LoadCredentialEncrypted=keyring-unlock:{layout.credential}
UMask=0077
LimitCORE=0
WorkingDirectory={layout.state}
ExecStart={command}
# Unresolved runs after a crash need studio-recover; an automatic restart would only fail again.
Restart=no
# Only the supervisor receives SIGTERM, so browsers close and ownership is released before anything is killed.
KillMode=mixed
TimeoutStopSec=300

[Install]
WantedBy=multi-user.target
'''


def run(arguments, **options):
    subprocess.run([str(argument) for argument in arguments], check=True, **options)


def service_active():
    return subprocess.run(['systemctl', 'is-active', '--quiet', SERVICE]).returncode == 0


def account_exists():
    import pwd
    try:
        pwd.getpwnam(ACCOUNT)
    except KeyError:
        return False
    return True


def as_service(layout, *arguments):
    return ['runuser', '-u', ACCOUNT, '--', 'env', f'HOME={layout.state}', *arguments]


def place(text, target, mode, group='root'):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / target.name
        source.write_text(text, encoding='utf-8', newline='\n')
        run(['install', '-m', mode, '-o', 'root', '-g', group, source, target])


def provision_keyring(layout):
    if layout.credential.exists():
        return
    # Never replace the unlock key of an existing keyring: that would strand its secrets.
    if (layout.state / '.local/share/keyrings').exists():
        raise ValueError('Existing service keyrings have no managed unlock credential. '
                         'Restore keyring-unlock.cred or follow the credential adoption instructions.')
    try:
        encrypted = subprocess.run(
            ['systemd-creds', 'encrypt', '--with-key=host', '--name=keyring-unlock', '-', '-'],
            input=secrets.token_urlsafe(48).encode('ascii'), capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Could not encrypt the service keyring credential with systemd-creds.') from None
    place(encrypted.stdout.decode('ascii'), layout.credential, '0600')


def kasmvnc_prefix(layout):
    version = json.loads((ROOT / 'scripts/data/studio-kasmvnc.json').read_text(encoding='utf-8'))['version']
    return layout.program / ('kasmvnc-' + version.replace('+', '-'))


def build_frontend(invoking_user):
    if (ROOT / 'pythonlib/camoufox/studio/team/web/.vite/manifest.json').is_file():
        return
    # Build as the checkout's owner so node_modules does not become root-owned.
    user = ['runuser', '-u', invoking_user, '--'] if invoking_user else []
    for step in (['ci'], ['run', 'build']):
        run([*user, 'npm', '--prefix', ROOT / 'frontend/studio', *step])


def upgrade_settings(layout, args):
    # Let systemd expand the same arguments as ExecStart and reuse the service's
    # parser, including quoted EnvironmentFile values and STUDIO_EXTRA options.
    probe = '''import json, os, sys
from camoufox.__main__ import studio_team_cmd
with studio_team_cmd.make_context('studio-team', sys.argv[1:]) as context:
    print(json.dumps({'settings': context.params,
                      'kasmvnc': os.environ.get('CAMOUFOX_KASMVNC_TOOLS')}, default=str))
'''
    try:
        result = subprocess.run([
            'systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--expand-environment=yes',
            '--property=Type=exec', '--property=RuntimeMaxSec=25',
            f'--property=User={ACCOUNT}', f'--property=Group={ACCOUNT}',
            f'--property=EnvironmentFile={layout.environment}', f'--property=Environment=HOME={layout.state}',
            str(layout.python), '-c', probe, *service_arguments(layout),
        ], capture_output=True, text=True, check=True, timeout=30)
        saved = json.loads(result.stdout)
        for name in ('origin', 'listen', 'browser_mode', 'cert', 'key', 'automation_port'):
            setattr(args, name, saved['settings'][name])
        args.automation = saved['settings']['automation_listen']
        return Path(saved['kasmvnc']) if saved['kasmvnc'] else None
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise ValueError(f'Could not validate preserved settings in {layout.environment}; '
                         'check the service arguments and certificate paths before upgrading.') from None


def install(layout, args, *, invoking_user=None):
    if service_active():
        raise ValueError(f'Stop the running service first: sudo systemctl stop {SERVICE}')
    environment = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME')}
    run(['apt-get', 'update'], env=environment)
    run(['apt-get', 'install', '-y', *PACKAGES], env=environment)
    if not account_exists():
        run(['useradd', '--system', '--user-group', '--home-dir', layout.state, '--shell', '/usr/sbin/nologin',
             ACCOUNT])
    run(['install', '-d', '-m', '0755', layout.program])
    run(['install', '-d', '-m', '0700', '-o', ACCOUNT, '-g', ACCOUNT,
         layout.state, layout.kernels, layout.staging, layout.clients])
    run(['install', '-d', '-m', '0750', '-o', 'root', '-g', ACCOUNT, layout.config])
    provision_keyring(layout)
    build_frontend(invoking_user)
    if not layout.python.is_file():
        run([sys.executable, '-m', 'venv', layout.program / '.venv'], env=environment)
    python = layout.python
    run([python, '-m', 'pip', 'install', '-r', ROOT / 'pythonlib/team-requirements.lock'], env=environment)
    run([python, '-m', 'pip', 'install', '--no-deps', '--force-reinstall', ROOT / 'pythonlib'], env=environment)
    kept = layout.environment.exists()
    kasmvnc = upgrade_settings(layout, args) if kept else None
    run([python, '-c', 'import sys; from camoufox.studio.team.transport import exact_origin; exact_origin(sys.argv[1])',
         args.origin], env=environment)
    run([python, ROOT / 'scripts/pin-fpgen-model.py'], env=environment)
    run([python, '-m', 'playwright', 'install-deps', 'firefox'], env=environment)
    service = {'env': environment, 'cwd': layout.state}
    run(as_service(layout, python, '-c', CHECK_NAMESPACE), **service)
    run(as_service(layout, python, '-m', 'camoufox', 'fetch'), **service)
    run(as_service(layout, python, '-c', CACHE_KERNEL, layout.kernels), **service)
    if args.browser_mode == 'remote':
        if kept and kasmvnc is None:
            raise ValueError('The preserved remote settings need CAMOUFOX_KASMVNC_TOOLS in studio.env.')
        kasmvnc = kasmvnc or kasmvnc_prefix(layout)
        if not kasmvnc.exists():
            if kept:
                raise ValueError('The preserved CAMOUFOX_KASMVNC_TOOLS path is missing; '
                                 'restore the configured runtime before upgrading.')
            run([python, ROOT / 'scripts/install-studio-kasmvnc.py', '--install-system-deps', '--prefix', kasmvnc,
                 '--cache-dir', layout.build_cache / 'kasmvnc'], env=environment)
        run(as_service(layout, f'CAMOUFOX_KASMVNC_TOOLS={kasmvnc}', python, '-c',
                       'from camoufox.studio.team.kasm import prerequisites; prerequisites()'), **service)
    for path in filter(None, (args.cert, args.key)):
        run(as_service(layout, 'test', '-r', Path(path).resolve()), **service)
    if not layout.workspace.exists():
        # The terminal prompts for the administrator password; it never appears in arguments or files.
        run(as_service(layout, python, '-m', 'camoufox', 'studio-team-init', '--data-dir', layout.workspace,
                       '--name', args.admin_name, '--display', args.admin_display), **service)
    # An upgrade keeps the administrator's settings; edit the file to change them.
    if not kept:
        place(environment_file(args, kasmvnc), layout.environment, '0640', ACCOUNT)
    place(unit_file(layout), layout.unit, '0644')
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', SERVICE])
    return kept


def summary(layout, args, kept):
    parsed = urlsplit(args.origin)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    lines = [f'Kept the existing settings in {layout.environment}' if kept
             else f'TigerBrowser configured for {args.origin}',
             'Service start requested; confirm startup and sign in before accepting the deployment.',
             f'Status: systemctl status {SERVICE}', f'Settings: {layout.environment}',
             f'Logs: journalctl -u {SERVICE}',
             'Allow only your LAN through the firewall, for example:',
             f'  sudo ufw allow from 192.168.1.0/24 to any port {port} proto tcp']
    if args.automation:
        lines.append(f'  sudo ufw allow from 192.168.1.0/24 to any port {getattr(args, "automation_port", 8767)} proto tcp')
    lines.append(f'After a crash: sudo -u {ACCOUNT} {layout.python} -m camoufox studio-recover --data-dir '
                 f'{layout.workspace}')
    if urlsplit(args.origin).scheme == 'http':
        lines.append('HTTP sends passwords, check-outs, proxy passwords and client installers without encryption.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True, help='Exact address members open, e.g. http://192.168.1.10:8766.')
    parser.add_argument('--listen', default='127.0.0.1', help='Listener IP; use 0.0.0.0 to accept other computers.')
    parser.add_argument('--browser-mode', choices=('disabled', 'headless', 'remote'), default='remote')
    parser.add_argument('--private-network-only', action='store_true')
    parser.add_argument('--allow-client', action='append', default=[], metavar='IP_OR_CIDR')
    parser.add_argument('--automation', action='store_true', help='Also serve member API keys on port 8767.')
    parser.add_argument('--cert', help='TLS certificate chain for an https:// origin, readable by camoufox.')
    parser.add_argument('--key', help='TLS private key for an https:// origin, readable by camoufox.')
    parser.add_argument('--admin-name', default='admin')
    parser.add_argument('--admin-display', default='Administrator')
    args = parser.parse_args(argv)
    try:
        if sys.platform != 'linux':
            raise ValueError('Run this installer on the Ubuntu server.')
        if os.geteuid() != 0:
            raise ValueError('Run as root, for example with sudo.')
        validate_platform(platform.freedesktop_os_release(), platform.machine(), sys.version_info[:2])
        ipaddress.ip_address(args.listen)
        environment_file(args, None)
        layout = Layout()
        kept = install(layout, args, invoking_user=os.environ.get('SUDO_USER'))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Installation failed: {error}\n')
    print(summary(layout, args, kept))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
