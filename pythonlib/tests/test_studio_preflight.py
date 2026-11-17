"""Read-only host prerequisites, not an assertion of remote-team readiness."""

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from camoufox.studio import distribution, preflight


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, 'OS_NAME', 'lin')
    binary = tmp_path / 'camoufox-bin'
    binary.write_bytes(b'fixture')
    binary.chmod(0o700)
    monkeypatch.setattr(distribution, 'get_active_path', lambda **kwargs: tmp_path)
    monkeypatch.setattr(distribution, 'bundled_path', lambda _: None)
    monkeypatch.setattr(distribution, 'launch_path', lambda root: str(binary))
    monkeypatch.setattr(preflight.Version, 'from_path', lambda _: SimpleNamespace(is_supported=lambda: True))
    monkeypatch.setattr(preflight, 'validate_config', lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, 'confirm_paths', lambda _: None)
    monkeypatch.setattr(preflight, 'is_pinned', lambda: True)
    monkeypatch.delenv('FPGEN_MODEL_URL', raising=False)
    return binary


def checks(report):
    return {item['id']: item for item in report['checks']}


@pytest.mark.parametrize('os_name', ['win', 'lin'])
def test_headless_prerequisites_on_supported_hosts(installed, monkeypatch, os_name):
    monkeypatch.setattr(preflight, 'OS_NAME', os_name)
    report = preflight.check_host(mode='headless')
    assert report['ok']
    assert report['mode'] == 'headless'
    assert checks(report)['display']['status'] == 'pass'
    assert checks(report)['credentials']['status'] == 'warn'
    assert report['remoteReady'] is False
    assert str(installed) not in json.dumps(report)


@pytest.mark.parametrize('os_name', ['win', 'lin'])
def test_remote_mode_never_claims_local_prerequisites_are_team_support(installed, monkeypatch, os_name):
    monkeypatch.setattr(preflight, 'OS_NAME', os_name)
    report = preflight.check_host(mode='remote')
    assert not report['ok']
    assert checks(report)['remote']['status'] == 'fail'
    assert report['remoteReady'] is False
    assert '真实部署验证' in checks(report)['remote']['message']
    assert '可选诊断' in checks(report)['remote']['message']


def test_remote_prerequisite_failure_is_redacted(installed, monkeypatch):
    from camoufox.studio.team import kasm

    def fail():
        raise RuntimeError('private-native-path')

    monkeypatch.setattr(kasm, 'prerequisites', fail)
    report = preflight.check_host(mode='remote')
    assert checks(report)['native']['status'] == 'fail'
    assert 'private-native-path' not in json.dumps(report)


def test_native_prerequisites_do_not_certify_remote_identity(installed, monkeypatch):
    from camoufox.studio.team import kasm
    monkeypatch.setattr(kasm, 'prerequisites', lambda: None)
    report = preflight.check_host(mode='remote')
    assert checks(report)['native']['status'] == 'pass'
    assert report['remoteReady'] is False and not report['ok']


@pytest.mark.parametrize('configured', [False, True])
def test_linux_desktop_configuration_is_not_proof_of_display_access(installed, monkeypatch, configured):
    monkeypatch.setattr(preflight, 'OS_NAME', 'lin')
    monkeypatch.setattr(preflight, 'has_display', lambda _: configured)
    report = preflight.check_host(mode='desktop')
    assert report['ok'] is configured
    assert checks(report)['display']['status'] == ('warn' if configured else 'fail')


@pytest.mark.parametrize('interactive', [False, True])
def test_windows_services_are_not_assumed_to_have_a_visible_desktop(installed, monkeypatch, interactive):
    monkeypatch.setattr(preflight, 'OS_NAME', 'win')
    monkeypatch.setattr(preflight, '_windows_interactive', lambda: interactive)
    report = preflight.check_host(mode='desktop')
    assert report['ok'] is interactive
    assert checks(report)['display']['status'] == ('warn' if interactive else 'fail')


@pytest.mark.parametrize('custom_model', [False, True])
def test_wrong_or_custom_model_fails_without_generating_identity(installed, monkeypatch, custom_model):
    if custom_model:
        monkeypatch.setenv('FPGEN_MODEL_URL', 'https://private.invalid/secret')
    else:
        monkeypatch.setattr(preflight, 'is_pinned', lambda: False)
    report = preflight.check_host()
    assert not report['ok']
    assert checks(report)['model']['status'] == 'fail'
    assert 'private.invalid' not in json.dumps(report)


def test_missing_browser_never_selects_or_downloads_a_replacement(installed, monkeypatch):
    calls = []
    monkeypatch.setattr(distribution, 'get_active_path', lambda **kw: calls.append(kw) or None)
    report = preflight.check_host()
    assert not report['ok']
    assert checks(report)['browser']['status'] == 'fail'
    assert calls == [{'update_config': False}]


def test_explicit_binary_does_not_probe_or_change_active_selection(installed, monkeypatch):
    monkeypatch.setattr(distribution, 'get_active_path', lambda **_: pytest.fail('Selected another binary'))
    assert preflight.check_host(executable_path=installed)['ok']


def test_unsupported_team_host_is_not_reported_as_ready(installed, monkeypatch):
    monkeypatch.setattr(preflight, 'OS_NAME', 'mac')
    report = preflight.check_host(mode='desktop')
    assert not report['ok']
    assert checks(report)['platform']['status'] == 'fail'


@pytest.mark.parametrize('probe,check_id', [('confirm_paths', 'addons'), ('is_pinned', 'model'),
                                         ('validate_config', 'browser')])
def test_probe_errors_are_reported_without_private_details(installed, monkeypatch, probe, check_id):
    def fail(*args, **kwargs):
        raise RuntimeError('private-credential-and-filesystem-detail')
    monkeypatch.setattr(preflight, probe, fail)
    report = preflight.check_host()
    assert not report['ok']
    assert checks(report)[check_id]['status'] == 'fail'
    assert 'private-credential' not in json.dumps(report)


def test_credential_check_is_explicit_and_does_not_read_passwords(installed, monkeypatch):
    calls = []
    monkeypatch.setattr(preflight.KeyringSecretStore, 'check_backend', lambda _: calls.append('check'))
    assert preflight.check_host()['ok']
    assert not calls
    report = preflight.check_host(check_credentials=True)
    assert report['ok']
    assert calls == ['check']
    assert checks(report)['credentials']['status'] == 'warn'


def test_credential_backend_failure_is_fatal_when_requested(installed, monkeypatch):
    def fail(_):
        raise RuntimeError('private backend detail')
    monkeypatch.setattr(preflight.KeyringSecretStore, 'check_backend', fail)
    report = preflight.check_host(check_credentials=True)
    assert not report['ok']
    assert checks(report)['credentials']['status'] == 'fail'
    assert 'private backend detail' not in json.dumps(report)


def test_invalid_mode_is_rejected_before_inspection(monkeypatch):
    monkeypatch.setattr(distribution, 'get_active_path', lambda **_: pytest.fail('Read runtime'))
    with pytest.raises(ValueError, match='mode'):
        preflight.check_host(mode='unknown')


def test_cli_returns_structured_failure_without_initializing_workspace(tmp_path, monkeypatch):
    from click.testing import CliRunner
    from camoufox.__main__ import cli
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ['studio-check', '--executable-path', str(tmp_path / 'missing')])
    assert result.exit_code == 1
    assert json.loads(result.output)['ok'] is False
    assert not list(tmp_path.iterdir())


def test_preflight_uses_no_qt_or_implicit_network(tmp_path):
    code = '''
import importlib.abc, json, socket, sys
from pathlib import Path
class NoQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith('PySide6'):
            raise AssertionError('Qt import')
sys.meta_path.insert(0, NoQt())
attempts = []
def no_network(*args, **kwargs):
    attempts.append(True)
    raise AssertionError('Network attempt')
socket.socket.connect = no_network
from camoufox.studio.preflight import check_host
report = check_host(executable_path=Path(sys.argv[1]))
assert not report['ok']
assert not attempts
assert not any(name.startswith('PySide6') for name in sys.modules)
print(json.dumps(report))
'''
    result = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'missing')],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['remoteReady'] is False


def test_cli_report_decodes_as_utf8_on_legacy_windows_pipes(tmp_path):
    result = subprocess.run(
        [sys.executable, '-m', 'camoufox', 'studio-check', '--executable-path', str(tmp_path / 'missing')],
        capture_output=True, timeout=30, env={**os.environ, 'PYTHONIOENCODING': 'gbk'},
    )
    assert result.returncode == 1
    assert json.loads(result.stdout.decode('utf-8'))['ok'] is False


@pytest.mark.parametrize('station,api_ok,visible', [(1, True, True), (1, True, False),
                                                 (0, True, False), (1, False, False)])
def test_windows_station_probe_checks_handles_and_api_result(monkeypatch, station, api_ok, visible):
    import ctypes
    from ctypes import wintypes

    def flags(handle, index, output, size, needed):
        assert handle == 1
        assert index == 1
        assert size == ctypes.sizeof(wintypes.BOOL) * 2 + ctypes.sizeof(wintypes.DWORD)
        output._obj.flags = 1 if visible else 0
        return api_ok

    library = SimpleNamespace(GetProcessWindowStation=lambda: station, GetUserObjectInformationW=flags)
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: library, raising=False)
    if not station or not api_ok:
        with pytest.raises(OSError):
            preflight._windows_interactive()
    else:
        assert preflight._windows_interactive() is visible


def test_backend_check_reuses_native_allowlist_without_password_operations(monkeypatch):
    class WinVaultKeyring:
        priority = 5

        def get_password(self, *args):
            pytest.fail('Read a credential')

        set_password = delete_password = get_password

    WinVaultKeyring.__module__ = 'keyring.backends.Windows'
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_keyring=lambda: WinVaultKeyring()))
    preflight.KeyringSecretStore('check').check_backend()
    monkeypatch.setitem(sys.modules, 'keyring', SimpleNamespace(get_keyring=lambda: object()))
    from camoufox.studio.secrets import SecretStoreError
    with pytest.raises(SecretStoreError):
        preflight.KeyringSecretStore('check').check_backend()
