"""Build the desktop client installer for this platform with the installed public runtimes."""

import argparse
import os
import shutil
import subprocess
import sys
from importlib.metadata import distributions, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = '/opt/tigerbrowser-client'
# Qt WebEngine, keyring and the Playwright 1.62 Ubuntu 24.04 Firefox lists; the client has no other system needs.
DEB_DEPENDS = (
    'libegl1', 'libgl1', 'libopengl0', 'libxkbcommon-x11-0', 'libxcb-cursor0', 'libxcb-icccm4', 'libxcb-image0',
    'libxcb-keysyms1', 'libxcb-render-util0', 'libxcb-xinerama0', 'libxcb-xkb1', 'libnss3', 'libxtst6',
    'libxkbfile1', 'libxshmfence1', 'libsecret-1-0', 'gnome-keyring', 'dbus-user-session', 'fonts-noto-cjk',
    'libasound2t64', 'libatk1.0-0t64', 'libavcodec60', 'libcairo-gobject2', 'libcairo2', 'libdbus-1-3',
    'libfontconfig1', 'libfreetype6', 'libgdk-pixbuf-2.0-0', 'libglib2.0-0t64', 'libgtk-3-0t64',
    'libpango-1.0-0', 'libpangocairo-1.0-0', 'libx11-6', 'libx11-xcb1', 'libxcb-shm0', 'libxcb1',
    'libxcomposite1', 'libxcursor1', 'libxdamage1', 'libxext6', 'libxfixes3', 'libxi6', 'libxrandr2',
    'libxrender1',
)


def freezer_environment():
    environment = os.environ.copy()
    windows = Path(os.environ['SystemRoot'])
    # An ambient Poppler ICU can shadow Windows ICU with incompatible exports.
    environment['PATH'] = os.pathsep.join(map(str, (
        Path(sys.executable).parent, Path(sys.base_prefix), windows / 'System32', windows,
    )))
    return environment


def check_executables(app: Path):
    """Verify that double-click cannot allocate a console while worker pipes remain usable."""
    import pefile

    for relative, subsystem in (('TigerBrowser.exe', 2), ('_internal/TigerBrowserRuntime.exe', 3)):
        with pefile.PE(str(app / relative), fast_load=True) as executable:
            if executable.OPTIONAL_HEADER.Subsystem != subsystem:
                raise RuntimeError(f'{relative} has the wrong Windows subsystem; expected {subsystem}.')


def stage_runtime(runtime: Path):
    """Public runtime files only: client caches, profiles and credentials stay in user data directories."""
    from camoufox.addons import ADDONS_DIR, DefaultAddons, confirm_paths, get_addon_path
    from camoufox.fpgen_model import ensure_fpgen_model, is_pinned

    confirm_paths([get_addon_path(addon.name) for addon in DefaultAddons])
    if not is_pinned():
        raise ValueError('Install the verified model with python -m camoufox fetch first.')
    ensure_fpgen_model()
    bundle = runtime / 'bundle'
    for addon in DefaultAddons:
        shutil.copytree(ADDONS_DIR / addon.name, bundle / 'addons' / addon.name)
    shutil.copy2(ROOT / 'docs/studio-portable.txt', runtime / 'README.txt')
    shutil.copy2(ROOT / 'LICENSE', runtime / 'LICENSE')
    stage_licenses(runtime)


def stage_licenses(runtime: Path):
    for distribution in distributions():
        for file in distribution.files or ():
            license_directory = any(part.lower() in ('license', 'licenses') for part in file.parts[:-1])
            license_file = (file.name.lower().startswith(('license', 'licence', 'copying', 'notice'))
                            and file.suffix.lower() in ('', '.txt', '.md', '.rst'))
            if license_directory or license_file:
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    target = runtime / 'licenses' / distribution.metadata['Name'] / str(file)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)


def windows_installer(app: Path, output: Path, name: str, client_version: str, iscc: Path):
    check_executables(app)
    subprocess.run([str(app / '_internal/TigerBrowserRuntime.exe'), '--check-runtime'],
                   env=freezer_environment(), creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    subprocess.run([str(iscc), '/Q', f'/DAppVersion={client_version}', f'/DSourceDir={app}',
                    f'/DOutputDir={output}', f'/DOutputName={Path(name).stem}',
                    f'/DIconFile={ROOT / "assets/studio/firefox.ico"}', str(ROOT / 'scripts/studio-windows.iss')],
                   check=True)


def macos_image(dist: Path, output: Path, name: str):
    app = dist / 'TigerBrowser.app'
    subprocess.run([str(app / 'Contents/MacOS/TigerBrowser'), '--check-runtime'], check=True)
    image = output / 'image'
    image.mkdir()
    shutil.move(app, image / app.name)
    (image / 'Applications').symlink_to('/Applications')
    subprocess.run(['hdiutil', 'create', '-volname', 'TigerBrowser', '-srcfolder', str(image),
                    '-format', 'UDZO', str(output / name)], check=True)


def debian_package(app: Path, output: Path, name: str, client_version: str):
    subprocess.run([str(app / 'TigerBrowser'), '--check-runtime'], check=True)
    root = output / 'deb'
    shutil.copytree(app, root / INSTALL.lstrip('/'), symlinks=True)
    icon = root / 'usr/share/icons/hicolor/128x128/apps/tigerbrowser.png'
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / 'assets/studio/default128.png', icon)
    entry = root / 'usr/share/applications/tigerbrowser.desktop'
    entry.parent.mkdir(parents=True)
    entry.write_text('[Desktop Entry]\nType=Application\nVersion=1.0\nName=TigerBrowser\n'
                     'Comment=Desktop client for a TigerBrowser team server\n'
                     f'Exec={INSTALL}/TigerBrowser\nIcon=tigerbrowser\nTerminal=false\n'
                     'Categories=Network;WebBrowser;\nStartupNotify=true\n', encoding='utf-8')
    control = root / 'DEBIAN/control'
    control.parent.mkdir()
    control.write_text(
        f'Package: tigerbrowser-client\nVersion: {client_version}\nArchitecture: amd64\n'
        'Maintainer: TigerBrowser <zhaotianshi@users.noreply.github.com>\n'
        'Section: web\nPriority: optional\nHomepage: https://github.com/zhaotianshi/TigerBrowser\n'
        f'Depends: {", ".join(DEB_DEPENDS)}\n'
        'Description: TigerBrowser desktop client\n'
        ' Connects to a TigerBrowser team server and runs checked-out browser\n'
        ' environments on this computer. Browser kernels come from the server.\n', encoding='utf-8')
    for path in (root, *root.rglob('*')):
        if not path.is_symlink():
            path.chmod(0o755 if path.is_dir() or os.access(path, os.X_OK) else 0o644)
    subprocess.run(['desktop-file-validate', str(entry)], check=True)
    subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(root), str(output / name)], check=True)


def inno_compiler(configured):
    candidates = [configured] if configured else [
        shutil.which('ISCC'), Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Inno Setup 6/ISCC.exe']
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise ValueError('Install Inno Setup 6.7.3 or pass --iscc PATH; see docs/studio.md#client-packages.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('dist/studio-client'))
    parser.add_argument('--iscc', type=Path, help='Inno Setup compiler, for Windows builds.')
    args = parser.parse_args()
    from camoufox.studio.client_releases import SUFFIXES, describe
    from camoufox.studio.kernel_cache import host_platform

    platform = host_platform()
    if platform not in SUFFIXES:
        parser.error('Build on Windows x64, macOS arm64 or x86_64, or Ubuntu 24.04 x64.')
    output = args.output.resolve()
    if output.exists():
        parser.error('Output already exists; choose a new --output directory.')
    client_version = version('camoufox')
    try:
        iscc = inno_compiler(args.iscc) if platform == 'win-x86_64' else None
        runtime = output / 'runtime'
        stage_runtime(runtime)
    except ValueError as error:
        parser.error(str(error))
    if platform.startswith('mac-'):
        subprocess.run(['sips', '-s', 'format', 'icns', str(ROOT / 'assets/studio/default128.png'),
                        '--out', str(runtime / 'TigerBrowser.icns')], check=True, stdout=subprocess.DEVNULL)
    dist = output / 'app'
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', str(ROOT / 'scripts/studio.spec'),
        '--distpath', str(dist), '--workpath', str(ROOT / '.ci-work/studio-package'), '--noconfirm', '--clean',
        '--', '--runtime', str(runtime), '--version', client_version,
    ], cwd=ROOT, env=freezer_environment() if platform == 'win-x86_64' else None, check=True)
    name = f'TigerBrowser-{client_version}-{platform}{SUFFIXES[platform]}'
    if platform == 'win-x86_64':
        windows_installer(dist / 'TigerBrowser', output, name, client_version, iscc)
    elif platform.startswith('mac-'):
        macos_image(dist, output, name)
    else:
        debian_package(dist / 'TigerBrowser', output, name, client_version)
    describe(output / name, platform, client_version)
    print(f'Installer: {output / name}')
    print(f'Record: {output / (name + ".json")}')


if __name__ == '__main__':
    main()
