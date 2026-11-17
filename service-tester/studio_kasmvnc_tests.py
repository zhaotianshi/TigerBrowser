"""Real KasmVNC display isolation, protocol and browser decoding checks."""

import asyncio
import json
import struct
import sys

import httpx
import pytest
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from camoufox.studio.access import MemberStore
from studio_runtime_tests import command, login, running_team, workspace


from camoufox.studio.team.kasm import (KasmDisplay as ProductionDisplay,
                                       KasmSession as ProductionSession, prerequisites)


class KasmDisplay(ProductionDisplay):
    accounts = (*ProductionDisplay.accounts, ('writer', ('-r', '-w')))


class RFBClient:
    def __init__(self, socket):
        self.socket = socket
        self.pending = b''

    async def read(self, count):
        while len(self.pending) < count:
            data = await asyncio.wait_for(self.socket.recv(), 5)
            assert isinstance(data, bytes)
            self.pending += data
        value, self.pending = self.pending[:count], self.pending[count:]
        return value

    async def initialize(self):
        assert await self.read(12) == b'RFB 003.008\n'
        await self.socket.send(b'RFB 003.008\n')
        count = (await self.read(1))[0]
        assert await self.read(count) == b'\x01'
        await self.socket.send(b'\x01')
        assert await self.read(4) == b'\0' * 4
        await self.socket.send(b'\x01')
        width, height, _, name_length = struct.unpack('!HH16sI', await self.read(24))
        assert 0 < name_length < 4096
        await self.read(name_length)
        return width, height

    async def barrier(self):
        # A BlockBefore fence confirms that earlier RFB messages were processed.
        await self.socket.send(struct.pack('!BBHii', 2, 0, 2, -312, -1998))
        await self.socket.send(struct.pack('!B3xIB', 248, 0x80000001, 5) + b'probe')
        while True:
            assert await self.read(1) == b'\xf8'
            flags, count = struct.unpack('!3xIB', await self.read(8))
            payload = await self.read(count)
            if not flags & 0x80000000 and payload == b'probe':
                return
            if flags & 0x80000000:
                await self.socket.send(struct.pack('!B3xIB', 248, flags & 3, count) + payload)


class KasmSession(ProductionSession):
    def create_display(self):
        return KasmDisplay(self.root, self.width, self.height, tools=prerequisites())


@pytest.fixture
def kasm(monkeypatch):
    from camoufox.studio.team import kasm
    tools = prerequisites()
    monkeypatch.setattr(kasm, 'KasmSession', KasmSession)
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize('workspace', [{'screen_mode': 'custom', 'screen_width': 1920,
                                      'screen_height': 1080}], indirect=True)
async def test_kasm_private_listener_authentication_and_read_only_protocol(workspace, kasm):
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launched = await command(service, owner, csrf, profile['id'], 'start')
            session = service.remotes[launched['run_id']].native
            display = session.display
            gateway = display.socket
            async with session.display_stream() as upstream:
                assert await RFBClient(upstream).initialize() == (1920, 1080)
            assert gateway.stat().st_mode & 0o777 == 0o600
            for user, expected in ((None, 401), ('viewer', 401), ('owner', 200)):
                async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(gateway)),
                                              base_url='http://localhost') as client:
                    response = await client.get('/api/get_sessions',
                        headers=display.authorization(user) if user else {})
                    assert response.status_code == expected
            with pytest.raises(InvalidStatus):
                async with unix_connect(str(gateway), uri='ws://localhost/websockify',
                                        origin=origin, subprotocols=['binary']):
                    pytest.fail('An unauthenticated display connection was accepted.')
            await session.input({'kind': 'pointer', 'x': 123, 'y': 456, 'button': 0, 'down': False})
            await session.publish_selection(b'synthetic-private-selection')
            config = str(display.tools / 'kasmvncconfig')
            for setting in ('AcceptPointerEvents', 'AcceptKeyEvents', 'AcceptCutText',
                            'SendCutText', 'AcceptSetDesktopSize'):
                # kasmvncconfig exits zero even when an override was refused.
                assert (await session.command(config, '-get', setting, capture=True)).strip() == b'0'
                await session.command(config, '-set', setting + '=1')
                assert (await session.command(config, '-get', setting, capture=True)).strip() == b'0'
            for user in ('viewer', 'writer'):
                async with unix_connect(str(gateway), uri='ws://localhost/websockify',
                    origin=origin, additional_headers=display.authorization(user),
                    subprotocols=['binary'], compression=None, max_size=8 * 1024 * 1024) as socket:
                    client = RFBClient(socket)
                    assert await client.initialize() == (1920, 1080)
                    clipboard = b'forbidden-display-clipboard'
                    resize = struct.pack('!BBHHBBIHHHHI', 251, 0, 800, 600, 1, 0, 0, 0, 0, 800, 600, 0)
                    await socket.send(struct.pack('!BBHI', 4, 1, 0, ord('a'))
                                      + struct.pack('!BHHHhh', 5, 1, 900, 900, 0, 0)
                                      + bytes([180, 1, 10]) + b'text/plain'
                                      + struct.pack('!I', len(clipboard)) + clipboard + resize)
                    await client.barrier()
                    assert (await session.command(config, '-get', 'FrameRate', capture=True)).strip() == b'24'
                    probe = ('import json; from Xlib import display; d=display.Display(); '
                             'p=d.screen().root.query_pointer(); '
                             'print(json.dumps([p.root_x,p.root_y,p.mask,any(d.query_keymap()),'
                             'd.screen().width_in_pixels,d.screen().height_in_pixels]));d.close()')
                    state = json.loads(await session.command(sys.executable, '-c', probe, capture=True))
                    assert state == [123, 456, 0, False, 1920, 1080]
                    assert await session.clipboard('clipboard') == 'synthetic-private-selection'
            async with session.display_stream() as socket:
                await RFBClient(socket).initialize()
                await socket.send(bytes([181]) + struct.pack('!H', 1) + b'x')
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(socket.recv(), 5)
            assert display.proc.poll() is None
            async with session.display_stream() as socket:
                assert await RFBClient(socket).initialize() == (1920, 1080)
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])
            assert not gateway.exists() and display.proc is None and not display.credentials


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_worker', ['display', 'input'])
@pytest.mark.parametrize('workspace', [{'screen_mode': 'custom', 'screen_width': 1920,
                                      'screen_height': 1080}], indirect=True)
async def test_two_kasm_displays_preserve_native_isolation(workspace, kasm, failed_worker):
    store, _, _, first = workspace
    second = store.save_profile({'name': 'Second isolated display', 'os': 'linux',
        'fingerprint': {'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080}})
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launches = [await command(service, owner, csrf, profile['id'], 'start')
                        for profile in (first, second)]
            runs = [service.remotes[item['run_id']] for item in launches]
            sockets = [run.native.display.socket for run in runs]
            assert sockets[0] != sockets[1]
            for index, run in enumerate(runs):
                await run.native.publish_selection(f'synthetic-{index}'.encode())
                assert await run.native.clipboard('clipboard') == f'synthetic-{index}'
            for index, run in enumerate(runs):
                assert await run.native.clipboard('clipboard') == f'synthetic-{index}'
            with pytest.raises(InvalidStatus):
                async with unix_connect(str(sockets[1]), uri='ws://localhost/websockify',
                    origin=origin, subprotocols=['binary'],
                    additional_headers=runs[0].native.display.authorization('viewer')):
                    pytest.fail('Credentials crossed display boundaries.')
            failed = runs[0].native
            process = failed.display.proc if failed_worker == 'display' else failed.input_worker
            process.kill()
            await asyncio.wait_for(asyncio.shield(service.engine.sessions[first['id']].task), 15)
            assert not sockets[0].exists() and sockets[1].is_socket()
            access = MemberStore(store, store.login('root', 'synthetic-admin-password'))
            status = await runs[1].handle(access, 'controller',
                {'kind': 'claim', 'generation': 0, 'force': False})
            await runs[1].handle(access, 'controller', {'kind': 'input',
                'generation': status['generation'], 'sequence': 1,
                'event': {'kind': 'pointer', 'x': 50, 'y': 50, 'button': 0, 'down': False}})
            assert await runs[1].native.clipboard('clipboard') == 'synthetic-1'
            assert not service.engine.sessions[second['id']].stop.is_set()
            await command(service, owner, csrf, second['id'], 'stop', launches[1]['run_id'])
            assert not sockets[1].exists()
