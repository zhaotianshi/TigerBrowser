"""The server offers verified client installers; clients check, download and verify them before installing."""

import hashlib
import json

import pytest
import requests

from camoufox.studio import client_releases
from camoufox.studio.client import update
from camoufox.studio.client.sync import SyncError
from camoufox.studio.client_releases import (ClientReleaseError, MANIFEST, PROTOCOL, SUFFIXES, describe, offered,
                                             write_manifest)
from camoufox.studio.kernel_cache import PLATFORMS, host_platform
from test_studio_client_automation import login as http_login
from test_studio_client_sync import server  # noqa: F401
from test_studio_team_service import ORIGIN, PREFIX, engine_factory, login, workspace  # noqa: F401


def publish(directory, platform=None, version='9.0.0', payload=b'installer'):
    platform = platform or host_platform()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'TigerBrowser-{version}-{platform}{SUFFIXES[platform]}'
    path.write_bytes(payload)
    return describe(path, platform, version)


def test_installer_suffixes_cover_every_kernel_platform():
    assert set(SUFFIXES) == set(PLATFORMS)


def test_manifest_lists_only_verified_installers(tmp_path):
    records = {platform: publish(tmp_path, platform, payload=platform.encode()) for platform in SUFFIXES}
    assert write_manifest(tmp_path) == records
    assert offered(tmp_path) == records
    (tmp_path / records['win-x86_64']['file']).write_bytes(b'replaced')
    with pytest.raises(ClientReleaseError, match='client_package_digest_mismatch'):
        write_manifest(tmp_path)
    with pytest.raises(ClientReleaseError, match='client_packages_unavailable'):
        offered(tmp_path)


@pytest.mark.parametrize('change', [
    {'file': '../escape.exe'}, {'file': 'client.zip'}, {'platform': 'win-arm64'}, {'version': 'latest'},
    {'sha256': 'A' * 64}, {'size': 0}, {'minimum_protocol': 2, 'protocol': 1}, {'extra': True},
])
def test_damaged_or_unsafe_records_are_rejected(tmp_path, change):
    record = publish(tmp_path)
    (tmp_path / MANIFEST).write_text(json.dumps({'packages': [{**record, **change}]}))
    with pytest.raises(ClientReleaseError, match='client_packages_unavailable'):
        offered(tmp_path)


def test_a_build_this_server_cannot_speak_to_is_never_offered(tmp_path, monkeypatch):
    publish(tmp_path)
    write_manifest(tmp_path)
    monkeypatch.setattr(client_releases, 'PROTOCOL', PROTOCOL + 1)
    monkeypatch.setattr(client_releases, 'MINIMUM_PROTOCOL', PROTOCOL + 1)
    with pytest.raises(ClientReleaseError, match='client_packages_unavailable'):
        offered(tmp_path)
    assert offered(tmp_path / 'absent') == {}


def test_service_offers_installers_to_logged_in_members_only(workspace, tmp_path):
    from starlette.testclient import TestClient
    from camoufox.studio.team.service import TeamService, create_app

    store = workspace[0]
    with pytest.raises(ValueError):
        TeamService(store.root, client_root=store.root / 'clients')
    clients = tmp_path.parent / (tmp_path.name + '-clients')
    record = publish(clients, 'win-x86_64')
    write_manifest(clients)
    application = create_app(store.root, origin=ORIGIN, engine_factory=engine_factory, client_root=clients)
    with TestClient(application, base_url=ORIGIN) as client:
        assert client.get(PREFIX + '/client/update').status_code == 401
        assert client.get(PREFIX + '/client/packages/win-x86_64').status_code == 401
        login(client, 'reader', 'member-password-2')
        update_info = client.get(PREFIX + '/client/update').json()
        assert update_info == {'protocol': PROTOCOL, 'minimum_protocol': client_releases.MINIMUM_PROTOCOL,
                               'packages': [record]}
        assert str(clients) not in json.dumps(update_info)
        download = client.get(PREFIX + '/client/packages/win-x86_64')
        assert download.content == b'installer'
        assert download.headers['x-content-sha256'] == hashlib.sha256(b'installer').hexdigest()
        assert client.get(PREFIX + '/client/packages/mac-arm64').status_code == 404
        assert client.get(PREFIX + '/client/packages/..%2Fmanifest.json').status_code == 404
        (clients / MANIFEST).write_text('{')
        assert client.get(PREFIX + '/client/update').json() == {'error': 'client_packages_unavailable'}
    application = create_app(store.root, origin=ORIGIN, engine_factory=engine_factory)
    with TestClient(application, base_url=ORIGIN) as client:
        login(client)
        assert client.get(PREFIX + '/client/update').json()['packages'] == []


@pytest.fixture
def offering(server, tmp_path_factory, monkeypatch):
    service, engine = server[1], server[2]
    origin = engine.transport.origin
    clients = tmp_path_factory.mktemp('clients')
    record = publish(clients)
    write_manifest(clients)
    service.client_root = clients
    monkeypatch.setattr(update, 'current_version', lambda: '8.9.0')
    return origin, http_login(origin, 'op', 'member-password-1'), clients, record


def test_client_reports_a_newer_compatible_installer_and_verifies_its_download(offering, tmp_path):
    origin, cookie, _, record = offering
    assert update.check(origin, cookie) == {'version': '8.9.0', 'compatible': True, 'package': record,
                                            'newer': True}
    path = update.download(origin, cookie, record, tmp_path / 'updates')
    assert path.read_bytes() == b'installer' and path.suffix == SUFFIXES[host_platform()]
    assert [item.name for item in path.parent.iterdir()] == [path.name]
    with pytest.raises(SyncError, match='session_expired'):
        update.check(origin, 'expired-login')


def test_a_replaced_installer_is_discarded(offering, tmp_path):
    origin, cookie, clients, record = offering
    (clients / record['file']).write_bytes(b'installex')
    with pytest.raises(SyncError, match='client_update_digest_mismatch'):
        update.download(origin, cookie, record, tmp_path / 'updates')
    assert list((tmp_path / 'updates').iterdir()) == []


def test_an_incompatible_server_is_reported(offering, monkeypatch):
    origin, cookie, _, _ = offering
    monkeypatch.setattr(update, 'compatible', lambda minimum, protocol: False)
    assert update.check(origin, cookie)['compatible'] is False


def test_update_endpoint_ignores_system_proxies(offering, monkeypatch):
    origin, cookie, _, _ = offering
    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:9')
    monkeypatch.setattr(requests.sessions, 'get_environ_proxies', lambda *args, **kwargs: pytest.fail('Used a proxy'))
    assert update.check(origin, cookie)['newer'] is True


@pytest.mark.parametrize('platform, command', [
    ('win-x86_64', ['client-update.exe', '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART']),
    ('lin-x86_64', ['xdg-open', 'client-update.exe']),
])
def test_install_commands_follow_each_platform(platform, command, tmp_path):
    assert update.install_command(platform, 'client-update.exe', tmp_path, 1) == command


def test_macos_replaces_a_writable_bundle_after_exit_and_otherwise_opens_the_image(tmp_path, monkeypatch):
    app = tmp_path / 'TigerBrowser.app'
    app.mkdir()
    command = update.install_command('mac-arm64', 'client-update.dmg', app, 42)
    assert command[:2] == ['/bin/sh', '-c'] and command[3:] == ['sh', '42', 'client-update.dmg', str(app)]
    monkeypatch.setattr(update.os, 'access', lambda path, mode: False)
    assert update.install_command('mac-arm64', 'client-update.dmg', app, 42) == ['open', 'client-update.dmg']


def test_a_source_checkout_never_runs_an_installer(monkeypatch, tmp_path):
    monkeypatch.delattr(update.sys, 'frozen', raising=False)
    monkeypatch.setattr(update.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('Ran an installer'))
    with pytest.raises(SyncError, match='client_update_unsupported'):
        update.start_install(tmp_path / 'client-update.exe')


def test_manifest_command_publishes_verified_installers_and_reports_tampering(tmp_path):
    from click.testing import CliRunner
    from camoufox.__main__ import cli

    record = publish(tmp_path, 'lin-x86_64')
    result = CliRunner().invoke(cli, ['studio-client-manifest', str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert record['file'] in result.output and offered(tmp_path) == {'lin-x86_64': record}
    (tmp_path / record['file']).write_bytes(b'tampered')
    result = CliRunner().invoke(cli, ['studio-client-manifest', str(tmp_path)])
    assert result.exit_code != 0 and 'client_package_digest_mismatch' in result.output
