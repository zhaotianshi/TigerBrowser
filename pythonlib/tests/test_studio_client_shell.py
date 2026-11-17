"""The desktop shell trusts only its configured server and exposes a path-free local bridge."""

import json
from pathlib import Path
import os
import threading
import time

os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import pytest
from PySide6.QtCore import QCoreApplication

from camoufox.studio.client import shell
from camoufox.studio.client.bridge import Bridge
from camoufox.studio.client.sync import SyncError
from test_studio_client_sync import server  # noqa: F401
from test_studio_team_service import workspace  # noqa: F401

PROFILE = '00000000-0000-4000-8000-000000000001'


@pytest.mark.parametrize('url, expected', [
    ('http://192.168.1.5:8766/#/profiles', True),
    ('http://192.168.1.5:8767/', False),
    ('https://192.168.1.5:8766/', False),
    ('http://user@192.168.1.5:8766/', False),
    ('http://192.168.1.50:8766/', False),
    ('blob:http://192.168.1.5:8766/x', False),
])
def test_only_the_configured_origin_is_trusted(url, expected):
    assert shell.same_origin(url, 'http://192.168.1.5:8766') is expected
    assert shell.same_origin('https://team.test/', 'https://team.test') is True
    assert shell.same_origin('https://team.test:443/', 'https://team.test') is True


def test_saved_servers_round_trip_and_damage_fails_loudly(tmp_path):
    assert shell.load_servers(tmp_path) == []
    shell.save_servers(tmp_path, ['http://192.168.1.5:8766', 'https://team.test'])
    assert shell.load_servers(tmp_path) == ['http://192.168.1.5:8766', 'https://team.test']
    (tmp_path / shell.SERVERS).write_text(json.dumps({'servers': ['http://x/path']}))
    with pytest.raises(ValueError, match='damaged'):
        shell.load_servers(tmp_path)


def test_probe_accepts_only_a_studio_server_at_its_exact_origin(server):
    origin = server[2].transport.origin
    assert shell.probe_server(origin) is None
    assert shell.probe_server(origin.replace('127.0.0.1', 'localhost')) == 'invalid_host'
    assert shell.probe_server('http://127.0.0.1:9') == 'server_unreachable'


class Client:
    def __init__(self, base, origin, workspace_id, notify):
        self.lock = type('Lock', (), {'path': base / workspace_id / 'workspace.lock'})()
        self.notify, self.launched = notify, []
        self.engine = type('Engine', (), {'sessions': {}})()

    async def run(self):
        pass

    async def configure_automation(self, cookie):
        self.cookie = cookie

    async def launch(self, profile_id, checkout, token):
        self.launched.append((profile_id, checkout, token))
        raise SyncError('pending_return')

    def status(self, profile_id):
        return {'browser': 'stopped', 'lease': 'pending_return'}

    def local_states(self):
        return {PROFILE: self.status(PROFILE)}

    def stale_copies(self):
        return []

    def device(self):
        return {'device_id': PROFILE, 'device_name': 'Laptop', 'platform': 'win-x86_64'}

    def data_folder(self, profile_id):
        raise AssertionError('not used')


@pytest.fixture
def bridge(tmp_path):
    QCoreApplication.instance() or QCoreApplication([])
    thread = shell.BrowserThread()
    thread.start()
    thread.ready.wait()
    state = {'allowed': True}
    created = Bridge(tmp_path, 'http://192.168.1.5:8766', thread.loop, allowed=lambda: state['allowed'],
                     client_factory=Client)
    try:
        yield created, state
    finally:
        thread.loop.call_soon_threadsafe(thread.loop.stop)
        thread.wait()


def reply(text):
    return json.loads(text)


def test_bridge_answers_only_the_trusted_page_and_rejects_bad_arguments(bridge):
    created, state = bridge
    assert reply(created.localStates()) == {'error': 'client_not_attached'}
    assert reply(created.attach('not-a-uuid')) == {'error': 'operation_failed'}
    workspace_id = '00000000-0000-4000-8000-0000000000aa'
    device = {'ok': {'device_id': PROFILE, 'device_name': 'Laptop', 'platform': 'win-x86_64'}}
    assert reply(created.attach(workspace_id)) == device
    assert reply(created.attach(workspace_id)) == device
    assert reply(created.attach('00000000-0000-4000-8000-0000000000bb')) == {'error': 'workspace_changed'}
    assert reply(created.pendingReturns()) == {'ok': {'profiles': {PROFILE: {'browser': 'stopped',
                                                                             'lease': 'pending_return'}}, 'stale': []}}
    assert reply(created.launchLocal(PROFILE, json.dumps({'checkout': {}, 'path': 'C:/'}))) == {
        'error': 'invalid_request'}
    assert reply(created.saveLocalSettings('{')) == {'error': 'operation_failed'}
    state['allowed'] = False
    assert reply(created.capabilities()) == {'error': 'origin_denied'}
    assert reply(created.localStates()) == {'error': 'origin_denied'}


def test_failed_background_launch_is_reported_with_its_code(bridge):
    created, _ = bridge
    created.attach('00000000-0000-4000-8000-0000000000aa')
    received, done = [], threading.Event()
    created._notified.disconnect()
    created._notified.connect(lambda profile_id, state: (received.append((profile_id, json.loads(state))), done.set()))
    assert reply(created.launchLocal(PROFILE, '')) == {'ok': True}
    # The loop thread queues the signal; the page receives it on the Qt thread.
    for _ in range(200):
        QCoreApplication.processEvents()
        if done.wait(0.05):
            break
    assert done.is_set()
    assert received == [(PROFILE, {'browser': 'stopped', 'lease': 'pending_return', 'error_code': 'pending_return'})]
    assert created.client.launched == [(PROFILE, None, None)]


def updates(created):
    received = []
    created.updateChanged.connect(lambda state: received.append(json.loads(state)))

    def wait(count):
        # Background results reach the Qt thread as queued signals.
        deadline = time.monotonic() + 10
        while len(received) < count and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.01)
        return received
    return wait


def test_update_requires_a_login_and_waits_for_local_runs_to_stop(bridge, monkeypatch, tmp_path):
    from camoufox.studio.client import bridge as bridge_module

    created, _ = bridge
    package = {'platform': 'win-x86_64', 'file': 'TigerBrowser-9.0.0-win-x86_64.exe'}
    monkeypatch.setattr(bridge_module.update, 'check', lambda origin, cookie: {
        'version': '8.9.0', 'compatible': True, 'package': package, 'newer': True})
    downloaded = tmp_path / 'client-update.exe'
    monkeypatch.setattr(bridge_module.update, 'download', lambda origin, cookie, record, directory: downloaded)
    installed = []
    wait = updates(created)
    assert reply(created.installUpdate()) == {'error': 'session_expired'}
    created.set_session_cookie('login')
    assert reply(created.installUpdate()) == {'error': 'client_update_unsupported'}
    monkeypatch.setattr(bridge_module.update, 'packaged_app', lambda: tmp_path / 'TigerBrowser')
    capabilities = reply(created.capabilities())['ok']
    assert capabilities['version'] and capabilities['protocol'] >= capabilities['minimum_protocol']
    created.attach('00000000-0000-4000-8000-0000000000aa')
    created.client.engine.sessions[PROFILE] = object()
    assert reply(created.installUpdate()) == {'error': 'local_runs_active'}
    created.client.engine.sessions.clear()
    monkeypatch.setattr(QCoreApplication, 'quit', lambda: installed.append('quit'))
    assert reply(created.installUpdate()) == {'ok': True}
    assert wait(2) == [{'state': 'downloading'}, {'state': 'installing'}]
    # The shell starts it only after shutdown has returned local data and released the cache.
    assert installed == ['quit'] and created.pending_install == downloaded


def test_a_failed_update_download_is_reported_and_never_installed(bridge, monkeypatch):
    from camoufox.studio.client import bridge as bridge_module

    created, _ = bridge
    monkeypatch.setattr(bridge_module.update, 'check', lambda origin, cookie: {
        'version': '8.9.0', 'compatible': True, 'package': {'platform': 'win-x86_64'}, 'newer': True})

    def download(*args):
        raise SyncError('client_update_digest_mismatch')

    monkeypatch.setattr(bridge_module.update, 'download', download)
    monkeypatch.setattr(bridge_module.update, 'packaged_app', lambda: Path('TigerBrowser'))
    wait = updates(created)
    created.set_session_cookie('login')
    assert reply(created.installUpdate()) == {'ok': True}
    assert wait(2) == [{'state': 'downloading'}, {'state': 'error', 'error': 'client_update_digest_mismatch'}]
    assert created.pending_install is None
