"""A display stream inherits the login, run and lifetime of its control connection."""

import asyncio
import json
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import pytest
from anyio import create_task_group, sleep
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from test_studio_team_service import (ORIGIN, PREFIX, app, login, mutation, settle,
                                      workspace)


@pytest.mark.parametrize('stage', ['server_spawn', 'display_report', 'listener_check', 'gateway_bind'])
def test_display_startup_failure_reports_stage_without_child_secrets(tmp_path, monkeypatch, stage):
    from camoufox.exceptions import CannotExecuteXvfb
    from camoufox.studio.engine import StudioError, public_error
    from camoufox.studio.team import kasm

    display = kasm.KasmDisplay(tmp_path, 640, 480, tools=tmp_path)
    display.proc = object()
    failure = CannotExecuteXvfb('secret child stderr')
    failure.reason = 'invalid_response'
    failure.response = json.dumps([stage, 13, None]).encode() + b'\n'
    failure.exit_code = None
    cleaned = []

    def failed_get(self):
        raise failure

    monkeypatch.setattr(kasm.native.NativeDisplay, 'get', failed_get)
    monkeypatch.setattr(display, 'kill', lambda: cleaned.append(True))
    with pytest.raises(StudioError) as raised:
        display.get()
    assert public_error(raised.value) == f'kasmvnc_startup_failed: {stage} (errno=13, exit=unknown)'
    assert cleaned


@pytest.mark.parametrize('response', [b'private-password\n', b'["private-password",13,null]\n',
                                      b'["server_spawn","private-password",null]\n',
                                      b'["server_spawn",true,null]\n',
                                      b'["server_spawn",13,"private-password"]\n',
                                      b'["server_spawn",13,0]' + b' ' * 129])
def test_display_startup_rejects_untrusted_failure_details(tmp_path, monkeypatch, response):
    from camoufox.exceptions import CannotExecuteXvfb
    from camoufox.studio.engine import StudioError, public_error
    from camoufox.studio.team import kasm

    display = kasm.KasmDisplay(tmp_path, 640, 480, tools=tmp_path)
    display.proc = object()
    failure = CannotExecuteXvfb('private-password')
    failure.reason, failure.response, failure.exit_code = 'invalid_response', response, None

    def failed_get(self):
        raise failure

    monkeypatch.setattr(kasm.native.NativeDisplay, 'get', failed_get)
    monkeypatch.setattr(display, 'kill', lambda: None)
    with pytest.raises(StudioError) as raised:
        display.get()
    assert public_error(raised.value) == 'kasmvnc_startup_failed: relay_invalid_response (exit=unknown)'


def test_display_relay_reports_listener_failure_and_reaps_child(monkeypatch):
    from camoufox.studio.team import kasm

    events = []
    process = SimpleNamespace(pid=123, returncode=None)

    def kill():
        events.append('kill')
        process.returncode = -9

    async def wait():
        events.append('wait')
        return process.returncode

    async def spawn(*args, **kwargs):
        os.write(kwargs['pass_fds'][0], b'0\n')
        return process

    def unavailable(pid):
        raise PermissionError(13, 'private-password')

    process.kill, process.wait = kill, wait
    monkeypatch.setattr(kasm.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(kasm.select, 'select', lambda readers, *args: (readers, [], []))
    monkeypatch.setattr(kasm, 'listening_port', unavailable)
    reader, writer = os.pipe()
    try:
        with pytest.raises(PermissionError):
            asyncio.run(kasm.relay_display('/synthetic-server', ['-displayfd', str(writer)]))
        assert os.read(reader, 256) == b'["listener_check",13,null]\n'
        assert events == ['kill', 'wait']
    finally:
        os.close(reader)


def test_display_stop_terminates_namespace_init_before_reaping_monitor(tmp_path, monkeypatch):
    from camoufox.studio.team import kasm

    events = []
    display = kasm.KasmDisplay(tmp_path, 640, 480, tools=tmp_path)
    display.child = 123

    def wait(timeout):
        if display.proc.poll() is None:
            events.append('reap-monitor')
        display.proc.poll = lambda: 0

    def kill_monitor():
        pytest.fail('Killing the bwrap monitor does not prove the namespace stopped.')

    display.proc = SimpleNamespace(poll=lambda: None, kill=kill_monitor, wait=wait)
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal',
        lambda child, number: events.append(('signal', child, number)), raising=False)
    monkeypatch.setattr(kasm.select, 'select',
        lambda readers, writers, errors, timeout: (readers, [], []))
    monkeypatch.setattr(kasm.os, 'close', lambda child: events.append(('close', child)))
    display.kill()
    assert events == [('signal', 123, signal.SIGKILL), 'reap-monitor', ('close', 123)]
    assert display.child is None and display.proc is None


def test_display_stop_keeps_ownership_evidence_when_namespace_exit_is_unproven(tmp_path, monkeypatch):
    from camoufox.studio.engine import StudioError
    from camoufox.studio.team import kasm

    display = kasm.KasmDisplay(tmp_path, 640, 480, tools=tmp_path)
    display.child = 123
    display.private = tmp_path / 'private'
    display.private.mkdir()
    closed = []
    monkeypatch.setattr(signal, 'SIGKILL', 9, raising=False)
    monkeypatch.setattr(signal, 'pidfd_send_signal', lambda *args: None, raising=False)
    monkeypatch.setattr(kasm.select, 'select', lambda *args: ([], [], []))
    monkeypatch.setattr(kasm.os, 'close', closed.append)
    with pytest.raises(StudioError, match='did not stop'):
        display.kill()
    assert display.child == 123 and display.private.is_dir() and not closed


class Native:
    transport = 'kasmvnc'
    width, height = 640, 480

    def __init__(self, options, profile):
        self.options = options
        self.events = []
        self.sent = []
        self.sent_event = asyncio.Event()
        self.released = 0
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(b'RFB 003.008\n')

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def attach(self):
        pass

    async def wait(self):
        await asyncio.Future()

    async def drain(self):
        self.released += 1

    async def input(self, event):
        self.events.append(event)

    @asynccontextmanager
    async def display_stream(self):
        yield self

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.incoming.get()

    async def send(self, message):
        self.sent.append(message)
        self.sent_event.set()


def test_display_coalescing_is_bounded_and_preserves_every_byte():
    from camoufox.studio.team.display import coalesced_stream

    async def run():
        full = asyncio.Event()
        produced = []
        chunks = [bytes([index]) * 1024 for index in range(128)] + [b'large' * 30000]

        async def upstream():
            for index, chunk in enumerate(chunks):
                produced.append(index)
                if index == 64:
                    full.set()
                yield chunk

        async with coalesced_stream(upstream()) as stream:
            await asyncio.wait_for(full.wait(), 5)
            assert len(produced) == 65
            result = [block async for block in stream]
        assert all(0 < len(block) <= 65536 for block in result)
        assert b''.join(result) == b''.join(chunks)
        assert len(result) < len(chunks)

    asyncio.run(run())


def test_display_coalescing_propagates_upstream_failure():
    from camoufox.studio.team.display import coalesced_stream

    async def run():
        async def upstream():
            yield b'previous-frame'
            raise OSError('synthetic stream failure')

        async with coalesced_stream(upstream()) as stream:
            assert await anext(stream) == b'previous-frame'
            with pytest.raises(OSError, match='synthetic stream failure'):
                await anext(stream)

    asyncio.run(run())


def test_display_coalescing_stops_its_pending_reader_on_close():
    from camoufox.studio.team.display import coalesced_stream

    async def run():
        stopped = asyncio.Event()
        waiting = asyncio.Event()

        async def upstream():
            try:
                yield b'frame'
                waiting.set()
                await asyncio.Future()
            finally:
                stopped.set()

        async with coalesced_stream(upstream()) as stream:
            assert await anext(stream) == b'frame'
            await asyncio.wait_for(waiting.wait(), 5)
        assert stopped.is_set()

    asyncio.run(run())


@pytest.fixture
def remote_client(workspace, monkeypatch):
    from camoufox.studio.team import kasm
    monkeypatch.setattr(kasm, 'prerequisites', lambda: Path('/synthetic-pinned-kasm'))
    monkeypatch.setattr(kasm, 'KasmSession', Native)
    with TestClient(app(workspace, browser_mode='remote'), base_url=ORIGIN) as client:
        csrf = login(client)
        profile = workspace[4]['id']
        response = mutation(client, csrf, '/profiles/' + profile + '/start', {
            'request_id': str(uuid4()),
        })
        assert response.status_code == 202, response.text
        assert settle(client, response.json())['state'] == 'succeeded'
        entry = client.get(PREFIX + '/profiles/' + profile + '/remote').json()
        yield client, csrf, entry


def control(client, csrf, entry):
    socket = client.websocket_connect('wss://team.test' + entry['control_path'],
                                      headers={'Origin': ORIGIN})
    return socket


def test_remote_service_requires_kasmvnc_instead_of_falling_back(workspace, monkeypatch):
    from camoufox.studio.engine import StudioError
    from camoufox.studio.team import kasm

    def unavailable():
        raise StudioError('kasmvnc_not_installed')

    monkeypatch.setattr(kasm, 'prerequisites', unavailable)
    with pytest.raises(StudioError, match='kasmvnc_not_installed'):
        with TestClient(app(workspace, browser_mode='remote'), base_url=ORIGIN):
            pass


def test_remote_api_rejects_retired_backend_selection(remote_client):
    client, csrf, entry = remote_client
    assert 'display_backends' not in client.get(PREFIX + '/me').json()['capabilities']
    for backend in ('png', 'kasmvnc'):
        response = mutation(client, csrf, '/profiles/' + entry['profile']['id'] + '/start', {
            'request_id': str(uuid4()), 'display_backend': backend,
        })
        assert response.status_code == 400


def test_kasm_entry_and_capabilities_do_not_disclose_internal_auth(remote_client):
    client, _, entry = remote_client
    capabilities = client.get(PREFIX + '/me').json()['capabilities']
    assert 'display_backends' not in capabilities
    assert 'default_display_backend' not in capabilities
    assert entry['transport'] == 'kasmvnc' and entry['protocol_version'] == 2
    assert 'websocket_path' not in entry
    assert entry['profile']['run']['display_backend'] == 'kasmvnc'
    assert 'viewer_id' not in entry and 'Authorization' not in str(entry)


def test_remote_launch_is_idempotent_without_display_selection(remote_client):
    client, csrf, entry = remote_client
    service = client.app.state.team
    original = service.repository.command(next(iter(service.tasks)))
    replay = mutation(client, csrf, '/profiles/' + entry['profile']['id'] + '/start', {
        'request_id': original['request_id'],
    })
    assert replay.status_code == 202 and replay.json()['id'] == original['id']


def test_kasm_control_requires_a_ready_paired_display(remote_client):
    client, csrf, entry = remote_client
    with control(client, csrf, entry) as owner:
        owner.send_json({'csrf': csrf})
        state = owner.receive_json()
        owner.send_json({'kind': 'claim', 'generation': state['generation'], 'force': False})
        while not (state := owner.receive_json()).get('error'):
            assert not state['mine']
        assert state == {'error': 'display_required'}


def test_kasm_display_waits_for_decoder_and_disconnect_releases_control(remote_client):
    client, csrf, entry = remote_client
    service = client.app.state.team
    run_id = entry['profile']['run']['id']
    native = service.remotes[run_id].native
    with control(client, csrf, entry) as owner:
        owner.send_json({'csrf': csrf})
        state = owner.receive_json()
        with client.websocket_connect('wss://team.test' + entry['display_path'],
                                      headers={'Origin': ORIGIN}) as display:
            display.send_json({'csrf': csrf, 'viewer_id': state['viewer_id']})
            assert display.receive_json() == {'ready': True}
            assert native.incoming.qsize() == 1
            display.send_json({'ready': True})
            assert display.receive_bytes() == b'RFB 003.008\n'
            owner.send_json({'kind': 'claim', 'generation': state['generation'], 'force': False})
            while not (state := owner.receive_json())['mine']:
                pass
            display.send_bytes(b'client-rfb-message')
            client.portal.call(native.sent_event.wait)
        with pytest.raises(WebSocketDisconnect):
            while True:
                owner.receive_json()
    assert native.released >= 2
    assert native.sent == [b'client-rfb-message']
    assert service.remotes[run_id].controller is None
    assert service.repository.latest_run(entry['profile']['id'])['state'] == 'running'


def test_cancelled_display_finishes_upstream_cleanup_and_releases_control(remote_client, monkeypatch):
    client, csrf, entry = remote_client
    service = client.app.state.team
    closed = []

    @asynccontextmanager
    async def stream(native):
        try:
            yield native
        finally:
            await sleep(0)
            closed.append(True)

    monkeypatch.setattr(Native, 'display_stream', stream)
    endpoint = next(route.endpoint for route in client.app.routes if route.name == 'display_stream')
    with control(client, csrf, entry) as owner:
        owner.send_json({'csrf': csrf})
        state = owner.receive_json()

        async def cancel_display():
            incoming = asyncio.Queue()
            incoming.put_nowait({'type': 'websocket.connect'})
            incoming.put_nowait({'type': 'websocket.receive', 'text': json.dumps({
                'csrf': csrf, 'viewer_id': state['viewer_id'],
            })})
            incoming.put_nowait({'type': 'websocket.receive', 'text': '{"ready":true}'})
            frame = asyncio.Event()

            async def send(message):
                if message['type'] == 'websocket.send' and 'bytes' in message:
                    frame.set()

            cookie = '; '.join(name + '=' + value for name, value in client.cookies.items())
            socket = WebSocket({'type': 'websocket', 'scheme': 'wss', 'path': entry['display_path'],
                'query_string': b'', 'server': ('team.test', 443), 'client': ('127.0.0.1', 3000),
                'headers': [(b'host', b'team.test'), (b'origin', ORIGIN.encode()), (b'cookie', cookie.encode())]},
                incoming.get, send)
            async with create_task_group() as group:
                group.start_soon(endpoint, entry['profile']['run']['id'], socket)
                await asyncio.wait_for(frame.wait(), 5)
                viewer = service.viewers[state['viewer_id']]
                await viewer.remote.handle(await service.access(viewer.cookie), viewer.connection_id, {
                    'kind': 'claim', 'generation': state['generation'], 'force': False,
                })
                assert viewer.remote.controller is not None
                group.cancel_scope.cancel()

        client.portal.call(cancel_display)
        assert closed == [True]
        assert not service.display_connections
        assert service.remotes[entry['profile']['run']['id']].controller is None


@pytest.mark.parametrize('hello', [{'csrf': 'wrong'}, {'csrf': 'correct', 'viewer_id': 'missing'}])
def test_kasm_display_rejects_unassociated_or_invalid_csrf(remote_client, hello):
    client, csrf, entry = remote_client
    with client.websocket_connect('wss://team.test' + entry['display_path'],
                                  headers={'Origin': ORIGIN}) as display:
        display.send_json({k: csrf if v == 'correct' else v for k, v in hello.items()})
        with pytest.raises(WebSocketDisconnect):
            display.receive_json()


def test_kasm_viewer_cannot_be_reused_by_another_login(remote_client):
    client, csrf, entry = remote_client
    with control(client, csrf, entry) as owner:
        owner.send_json({'csrf': csrf})
        state = owner.receive_json()
        another_csrf = login(client)
        with client.websocket_connect('wss://team.test' + entry['display_path'],
                                      headers={'Origin': ORIGIN}) as display:
            display.send_json({'csrf': another_csrf, 'viewer_id': state['viewer_id']})
            with pytest.raises(WebSocketDisconnect) as rejected:
                display.receive_json()
            assert rejected.value.code == 4403


def test_kasm_viewer_accepts_only_one_display_and_strict_ready_boolean(remote_client):
    client, csrf, entry = remote_client
    with control(client, csrf, entry) as owner:
        owner.send_json({'csrf': csrf})
        state = owner.receive_json()
        with client.websocket_connect('wss://team.test' + entry['display_path'],
                                      headers={'Origin': ORIGIN}) as display:
            display.send_json({'csrf': csrf, 'viewer_id': state['viewer_id']})
            assert display.receive_json() == {'ready': True}
            with client.websocket_connect('wss://team.test' + entry['display_path'],
                                          headers={'Origin': ORIGIN}) as duplicate:
                duplicate.send_json({'csrf': csrf, 'viewer_id': state['viewer_id']})
                with pytest.raises(WebSocketDisconnect) as rejected:
                    duplicate.receive_json()
                assert rejected.value.code == 4403
            display.send_json({'ready': 1})
            with pytest.raises(WebSocketDisconnect):
                display.receive_bytes()


@pytest.mark.parametrize('suffix', ['/control', '/display'])
def test_kasm_channels_reject_wrong_origin_and_query_credentials(remote_client, suffix):
    client, _, entry = remote_client
    path = PREFIX + '/remote/' + entry['profile']['run']['id'] + suffix
    for target, origin in ((path, 'https://other.test'), (path + '?viewer=anything', ORIGIN)):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('wss://team.test' + target, headers={'Origin': origin}):
                pytest.fail('An untrusted socket origin or query was accepted.')


def test_kasm_revocation_closes_both_channels_and_rejects_reuse(remote_client, workspace):
    client, csrf, entry = remote_client
    service = client.app.state.team
    with control(client, csrf, entry) as owner:
        owner.send_json({'csrf': csrf})
        state = owner.receive_json()
        with client.websocket_connect('wss://team.test' + entry['display_path'],
                                      headers={'Origin': ORIGIN}) as display:
            display.send_json({'csrf': csrf, 'viewer_id': state['viewer_id']})
            assert display.receive_json() == {'ready': True}
            display.send_json({'ready': True})
            display.receive_bytes()
            assert mutation(client, csrf, '/logout', {}).status_code == 200
            with pytest.raises(WebSocketDisconnect):
                display.receive_bytes()
            with pytest.raises(WebSocketDisconnect):
                while True:
                    owner.receive_json()
    assert not service.remote_connections


def test_retired_png_socket_is_not_served(remote_client):
    client, _, entry = remote_client
    path = PREFIX + '/remote/' + entry['profile']['run']['id']
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('wss://team.test' + path, headers={'Origin': ORIGIN}):
            pytest.fail('The retired PNG channel was accepted.')
