"""Keep standalone clients independent of unrelated host dependencies."""

import ast
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement


def test_desktop_extra_includes_shared_transfer_crypto():
    project = Path(__file__).resolve().parents[1] / 'pyproject.toml'
    extras = project.read_text(encoding='utf-8').split('[tool.poetry.extras]', 1)[1]
    studio = ast.literal_eval(re.search(r'^studio = (.+)$', extras, re.MULTILINE).group(1))
    assert 'cryptography' in studio


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
def test_desktop_lock_includes_transfer_crypto_on_every_platform(platform):
    lock = Path(__file__).resolve().parents[1] / 'studio-requirements.lock'
    environment = {'sys_platform': platform, 'platform_python_implementation': 'CPython',
                   'implementation_name': 'cpython'}
    selected = {}
    for line in lock.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate(environment):
            selected[requirement.name] = requirement
    # Checkout imports the shared archive validation module, which also owns encryption.
    for name in ('cryptography', 'cffi', 'pycparser'):
        assert name in selected, f'{platform} desktop lock omits {name}'
        assert any(item.operator == '==' for item in selected[name].specifier)


def test_freezer_environment_excludes_ambient_dll_directories(monkeypatch):
    script = Path(__file__).resolve().parents[2] / 'scripts/package-studio.py'
    monkeypatch.setenv('PATH', 'C:/unrelated-app/bin')
    monkeypatch.setenv('SystemRoot', 'C:/Windows')
    environment = runpy.run_path(str(script))['freezer_environment']()
    assert 'unrelated-app' not in environment['PATH']
    assert str(Path(sys.executable).parent) in environment['PATH']
    assert str(Path('C:/Windows') / 'System32') in environment['PATH']
    assert os.environ['PATH'] == 'C:/unrelated-app/bin'


def test_dependency_licenses_exclude_copying_test_binaries(monkeypatch, tmp_path):
    script = Path(__file__).resolve().parents[2] / 'scripts/package-studio.py'
    stage = runpy.run_path(str(script))['stage_licenses']
    licenses = ('example.dist-info/licenses/LICENSE.APACHE2',
                'example.dist-info/licenses/AUTHORS', 'example/NOTICE.txt', 'example/LICENSE')
    tests = ('PyObjCTest/copying.cpython-312-darwin.so', 'PyObjCTest/copying.py',
             'PyObjCTest/copying.cpython-312-darwin.so.dSYM/Contents/Resources/DWARF/copying.cpython-312-darwin.so')
    files = [Path(name) for name in (*licenses, *tests)]
    source = tmp_path / 'installed'
    for file in files:
        path = source / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'license or test fixture')
    distribution = SimpleNamespace(files=files, metadata={'Name': 'example'}, locate_file=lambda file: source / file)
    monkeypatch.setitem(stage.__globals__, 'distributions', lambda: [distribution])
    runtime = tmp_path / 'runtime'
    stage(runtime)
    copied = runtime / 'licenses/example'
    assert {file.relative_to(copied).as_posix() for file in copied.rglob('*') if file.is_file()} == set(licenses)


@pytest.mark.parametrize('copy_fails', [False, True])
def test_macos_image_owns_and_detaches_its_staging_volume(monkeypatch, tmp_path, copy_fails):
    script = Path(__file__).resolve().parents[2] / 'scripts/package-studio.py'
    package = runpy.run_path(str(script))['macos_image']
    dist = tmp_path / 'app'
    app = dist / 'TigerBrowser.app'
    app.mkdir(parents=True)
    (app / 'signed-payload').write_bytes(b'original')
    name = 'TigerBrowser.dmg'
    attached = None
    links = {}
    checks = []

    def command(args, **kwargs):
        nonlocal attached
        assert kwargs['check']
        if args[:2] == ['hdiutil', 'create']:
            if '-srcfolder' in args:
                raise subprocess.CalledProcessError(1, args, stderr='Resource busy')
            Path(args[-1]).touch()
        elif args[:2] == ['hdiutil', 'attach']:
            attached = Path(args[args.index('-mountpoint') + 1])
            assert attached.resolve().is_relative_to(tmp_path.resolve()) and attached != tmp_path
            assert '-nobrowse' in args and '-noautoopen' in args
        elif args[0] == 'ditto':
            if copy_fails:
                raise subprocess.CalledProcessError(1, args, stderr='Copy failed')
            shutil.copytree(args[-2], args[-1])
        elif args[0] == 'codesign':
            assert (Path(args[-1]) / 'signed-payload').read_bytes() == b'original'
            assert Path(args[-1]).parent == attached
            assert links == {attached / 'Applications': '/Applications'}
            checks.append('signature')
        elif args[:2] == ['hdiutil', 'detach']:
            assert Path(args[2]) == attached
            if (mounted_app := attached / app.name).is_dir():
                (mounted_app / 'signed-payload').unlink()
                mounted_app.rmdir()
            attached = None
            checks.append('detached')
        elif args[:2] == ['hdiutil', 'convert']:
            assert attached is None
            assert '-format' in args and args[args.index('-format') + 1] == 'UDZO'
            (tmp_path / name).write_bytes(b'compressed')
        elif args[:2] == ['hdiutil', 'verify']:
            assert Path(args[-1]).read_bytes() == b'compressed'
            checks.append('image')

    runner = SimpleNamespace(run=command, check_output=lambda *_args, **_kwargs: '1024\tapp')
    monkeypatch.setitem(package.__globals__, 'subprocess', runner)
    monkeypatch.setattr(Path, 'is_mount', lambda path: path == attached)
    monkeypatch.setattr(Path, 'symlink_to', lambda path, target: links.setdefault(path, target))
    if copy_fails:
        with pytest.raises(subprocess.CalledProcessError, match='ditto'):
            package(dist, tmp_path, name)
        assert checks == ['detached']
        assert not (tmp_path / name).exists()
    else:
        package(dist, tmp_path, name)
        assert checks == ['signature', 'detached', 'image']
        assert list(tmp_path.rglob('*.sparseimage')) == []
    assert attached is None
    assert (app / 'signed-payload').read_bytes() == b'original'
