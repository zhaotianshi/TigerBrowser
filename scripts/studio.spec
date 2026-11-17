"""Studio desktop client for the build platform, with runtime files staged by package-studio.py."""

import argparse
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

parser = argparse.ArgumentParser()
parser.add_argument('--runtime', type=Path, required=True)
parser.add_argument('--version', required=True)
options = parser.parse_args()
root = Path(SPECPATH).parent
runtime = options.runtime
datas = [(str(runtime / 'bundle/addons'), 'camoufox/studio/bundle/addons'),
         (str(runtime / 'licenses'), 'licenses'), (str(runtime / 'LICENSE'), '.'), (str(runtime / 'README.txt'), '.')]
for package in ('camoufox', 'fpgen', 'language_tags', 'tzdata'):
    datas += collect_data_files(package)
for package in ('camoufox', 'keyring'):
    datas += copy_metadata(package)
analysis = Analysis(
    [str(root / 'scripts' / 'studio-launcher.py')],
    pathex=[str(root / 'pythonlib')],
    binaries=[],
    datas=datas,
    hiddenimports=collect_submodules('fpgen') + collect_submodules('keyring.backends')
                  + collect_submodules('ua_parser_builtins'),
    excludes=['tkinter', 'pytest', 'IPython'],
)
archive = PYZ(analysis.pure)
exe = EXE(
    archive, analysis.scripts, [], exclude_binaries=True, name='TigerBrowser',
    console=False, upx=False,
    icon=str(root / 'assets/studio/firefox.ico') if sys.platform == 'win32' else None,
)
if sys.platform == 'win32':
    worker = EXE(
        archive, analysis.scripts, [], exclude_binaries=True, name='TigerBrowserRuntime',
        console=True, contents_directory='.', upx=False,
    )
    COLLECT(
        exe, [('_internal/TigerBrowserRuntime.exe', worker.name, 'EXECUTABLE')],
        analysis.binaries, analysis.datas, name='TigerBrowser', upx=False,
    )
else:
    collected = COLLECT(exe, analysis.binaries, analysis.datas, name='TigerBrowser', upx=False)
    if sys.platform == 'darwin':
        BUNDLE(
            collected, name='TigerBrowser.app', icon=str(runtime / 'TigerBrowser.icns'),
            bundle_identifier='io.github.zhaotianshi.tigerbrowser', version=options.version,
            info_plist={'CFBundleDisplayName': 'TigerBrowser', 'NSHighResolutionCapable': True},
        )
