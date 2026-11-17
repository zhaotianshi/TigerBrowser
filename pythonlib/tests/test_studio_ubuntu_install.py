"""Ubuntu desktop installation plans and freedesktop argument quoting."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('studio_ubuntu_install', ROOT / 'scripts/install-studio-ubuntu.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.mark.parametrize('machine', ['x86_64', 'aarch64'])
def test_supported_ubuntu_architectures(machine):
    installer.validate_platform({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, machine, (3, 12))


@pytest.mark.parametrize('release,machine,version', [
    ({'ID': 'ubuntu', 'VERSION_ID': '22.04'}, 'x86_64', (3, 12)),
    ({'ID': 'debian', 'VERSION_ID': '13'}, 'x86_64', (3, 12)),
    ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, 'i686', (3, 12)),
    ({'ID': 'ubuntu', 'VERSION_ID': '24.04'}, 'x86_64', (3, 10)),
])
def test_unsupported_platforms_fail_before_install(release, machine, version):
    with pytest.raises(ValueError):
        installer.validate_platform(release, machine, version)


def test_exec_argument_escapes_field_codes_and_reserved_characters():
    value = '/home/Name with spaces/$cash/`tick`/"quote"/slash\\/100%'
    encoded = installer.desktop_argument(value)
    # Undo the desktop string layer and the Exec quoting layer, as required by the spec.
    string_value = encoded.replace('\\\\', '\\')
    argument = string_value[1:-1]
    decoded = ''
    index = 0
    while index < len(argument):
        if argument[index] == '\\':
            index += 1
        decoded += argument[index]
        index += 1
    assert decoded.replace('%%', '%') == value
    with pytest.raises(ValueError):
        installer.desktop_argument('/home/name\nExec=unwanted')


def test_install_uses_lock_local_package_and_does_not_touch_local_data(monkeypatch, tmp_path):
    root = tmp_path / 'source with spaces'
    root.mkdir()
    workspace = root / 'studio-data'
    workspace.mkdir()
    preserved = workspace / 'existing-profile'
    preserved.write_text('preserved')
    data_home = tmp_path / 'desktop data'
    monkeypatch.setenv('XDG_DATA_HOME', str(data_home))
    monkeypatch.setenv('PYTHONPATH', '/unrelated/package')
    calls = []
    monkeypatch.setattr(installer, 'run', lambda args, **kwargs: calls.append((list(map(str, args)), kwargs)))
    installer.install(root)
    commands = [command for command, _ in calls]
    assert ['sudo', 'apt-get', 'install', '-y', *installer.PACKAGES] in commands
    assert any(command[-3:] == ['playwright', 'install-deps', 'firefox'] for command in commands)
    assert any(command[-2:] == ['-r', str(root / 'pythonlib/studio-requirements.lock')] for command in commands)
    assert any(command[-3:] == ['--no-deps', '-e', './pythonlib[studio]'] for command in commands)
    assert any(command[-3:] == ['-m', 'camoufox', 'fetch'] for command in commands)
    assert all('PYTHONPATH' not in kwargs['environment'] for _, kwargs in calls)
    assert preserved.read_text() == 'preserved'
    entry = (data_home / 'applications/tigerbrowser.desktop').read_text()
    assert installer.desktop_argument(root / 'start-studio.sh') in entry
    assert '--data-dir' not in entry
    assert 'Terminal=false' in entry


def test_failed_runtime_check_does_not_install_desktop_entry(monkeypatch, tmp_path):
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'desktop'))
    def run(args, **kwargs):
        if len(args) > 2 and args[1] == '-c' and 'launch_path' in args[2]:
            raise installer.subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(installer, 'run', run)
    with pytest.raises(installer.subprocess.CalledProcessError):
        installer.install(tmp_path, system_dependencies=False)
    assert not (tmp_path / 'desktop/applications/tigerbrowser.desktop').exists()


def test_invalid_desktop_entry_preserves_existing_launcher(monkeypatch, tmp_path):
    data_home = tmp_path / 'desktop'
    applications = data_home / 'applications'
    applications.mkdir(parents=True)
    entry = applications / 'tigerbrowser.desktop'
    entry.write_text('previous launcher')
    monkeypatch.setenv('XDG_DATA_HOME', str(data_home))
    def run(args, **kwargs):
        if args[0] == 'desktop-file-validate':
            raise installer.subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(installer, 'run', run)
    with pytest.raises(installer.subprocess.CalledProcessError):
        installer.install(tmp_path, system_dependencies=False)
    assert entry.read_text() == 'previous launcher'
    assert list(applications.iterdir()) == [entry]
