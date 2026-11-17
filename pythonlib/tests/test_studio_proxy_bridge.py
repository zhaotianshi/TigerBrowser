"""Wire-level coverage for the authenticated SOCKS5 browser bridge."""

import asyncio
import ipaddress
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import pytest

from camoufox.studio import proxy_bridge


async def address_bytes(reader, address_type):
    if address_type == 1:
        return await reader.readexactly(4)
    if address_type == 4:
        return await reader.readexactly(16)
    length = await reader.readexactly(1)
    return length + await reader.readexactly(length[0])


async def reply(reader):
    header = await reader.readexactly(4)
    return header + await address_bytes(reader, header[3]) + await reader.readexactly(2)


def request_for(host, port=443):
    try:
        address = ipaddress.ip_address(host)
        payload = bytes([1 if address.version == 4 else 4]) + address.packed
    except ValueError:
        encoded = host.encode('ascii')
        payload = bytes([3, len(encoded)]) + encoded
    return b'\x05\x01\x00' + payload + port.to_bytes(2, 'big')


@asynccontextmanager
async def upstream(*, reject_auth=False, greeting=b'\x05\x02', stall=False, response=None):
    observations = {'auth': [], 'requests': [], 'closed': asyncio.Event()}
    tasks = set()
    writers = set()

    async def handle(reader, writer):
        writers.add(writer)
        try:
            assert await reader.readexactly(3) == b'\x05\x01\x02'
            if stall:
                await reader.read()
                return
            writer.write(greeting)
            await writer.drain()
            if greeting != b'\x05\x02':
                await reader.read()
                return
            version, length = await reader.readexactly(2)
            assert version == 1
            username = await reader.readexactly(length)
            length = (await reader.readexactly(1))[0]
            password = await reader.readexactly(length)
            observations['auth'].append((username, password))
            writer.write(b'\x01\x01' if reject_auth else b'\x01\x00')
            await writer.drain()
            if reject_auth:
                return
            header = await reader.readexactly(4)
            request = header + await address_bytes(reader, header[3]) + await reader.readexactly(2)
            observations['requests'].append(request)
            writer.write(response or b'\x05\x00\x00\x01\x7f\x00\x00\x01\x1f\x90')
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)
            observations['closed'].set()

    def accept(reader, writer):
        task = asyncio.create_task(handle(reader, writer))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    server = await asyncio.start_server(accept, '127.0.0.1', 0)
    port = server.sockets[0].getsockname()[1]
    proxy = {'server': f'socks5://127.0.0.1:{port}', 'username': 'usér', 'password': 'fixture:@'}
    try:
        yield proxy, observations
    finally:
        server.close()
        for writer in tuple(writers):
            writer.close()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await server.wait_closed()


async def client(proxy, request=None):
    endpoint = urlsplit(proxy['server'])
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
    writer.write(b'\x05\x02\x00\x02')
    await writer.drain()
    assert await reader.readexactly(2) == b'\x05\x00'
    writer.write(request or request_for('remote-only.invalid'))
    await writer.drain()
    return reader, writer, await reply(reader)


@pytest.mark.parametrize('proxy', [None, {'server': 'http://localhost:80', 'username': 'u', 'password': 'p'},
                                     {'server': 'https://localhost:443'}, {'server': 'socks5://localhost:1080'}])
def test_proxy_without_socks_auth_is_unchanged(proxy, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('A bridge must not be created')

    monkeypatch.setattr(asyncio, 'start_server', forbidden)

    async def run():
        async with proxy_bridge.browser_proxy(proxy) as result:
            assert result is proxy
    asyncio.run(run())


@pytest.mark.parametrize('host', ['remote-only.invalid', '203.0.113.8', '2001:db8::8'])
def test_authenticated_proxy_forwards_original_address_and_relays(host):
    async def run():
        async with upstream() as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                assert set(local) == {'server'}
                assert urlsplit(local['server']).hostname == '127.0.0.1'
                request = request_for(host)
                reader, writer, response = await client(local, request)
                assert response[:3] == b'\x05\x00\x00'
                assert observations['auth'] == [('usér'.encode(), b'fixture:@')]
                assert observations['requests'] == [request]
                payload = bytes(range(256)) * 1024
                writer.write(payload)
                await writer.drain()
                assert await asyncio.wait_for(reader.readexactly(len(payload)), 5) == payload
                writer.write_eof()
                assert await asyncio.wait_for(reader.read(), 5) == b''
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_upstream_rejected_auth_returns_failure_without_connecting_destination():
    async def run():
        async with upstream(reject_auth=True) as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                reader, writer, response = await client(local)
                assert response[1] == 2
                assert not observations['requests']
                assert await reader.read() == b''
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_upstream_cannot_downgrade_authentication():
    async def run():
        async with upstream(greeting=b'\x05\x00') as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                reader, writer, response = await client(local)
                assert response[1] == 2
                assert not observations['auth']
                assert not observations['requests']
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize('wire_response,expected', [
    (b'\x05\x05\x00\x01' + b'\x00' * 6, 5),
    (b'\x04\x00\x00\x01' + b'\x00' * 6, 1),
    (b'\x05\x00\x01\x01' + b'\x00' * 6, 1),
    (b'\x05\x00\x00\x04' + ipaddress.ip_address('2001:db8::4').packed + b'\x1f\x90', 0),
    (b'\x05\x00\x00\x03\x09localhost\x1f\x90', 0),
])
def test_upstream_replies_are_validated_and_bound_addresses_preserved(wire_response, expected):
    async def run():
        async with upstream(response=wire_response) as (proxy, _):
            async with proxy_bridge.browser_proxy(proxy) as local:
                reader, writer, response = await client(local)
                assert response[1] == expected
                if expected == 0:
                    assert response == wire_response
                else:
                    assert await reader.read() == b''
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_unsupported_client_authentication_is_rejected_without_upstream_connection():
    async def run():
        async with upstream() as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                endpoint = urlsplit(local['server'])
                reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
                writer.write(b'\x05\x01\x02')
                await writer.drain()
                assert await reader.readexactly(2) == b'\x05\xff'
                assert await reader.read() == b''
                assert not observations['auth']
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_cancelled_owner_closes_live_tunnel():
    async def run():
        async with upstream() as (proxy, observations):
            with pytest.raises(asyncio.CancelledError):
                async with proxy_bridge.browser_proxy(proxy) as local:
                    reader, writer, response = await client(local)
                    assert response[1] == 0
                    raise asyncio.CancelledError
            assert await asyncio.wait_for(reader.read(), 2) == b''
            await asyncio.wait_for(observations['closed'].wait(), 2)
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_exit_closes_listener_and_active_streams():
    async def run():
        async with upstream() as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                reader, writer, response = await client(local)
                assert response[1] == 0
                endpoint = urlsplit(local['server'])
            assert await asyncio.wait_for(reader.read(), 2) == b''
            await asyncio.wait_for(observations['closed'].wait(), 2)
            with pytest.raises(OSError):
                await asyncio.open_connection(endpoint.hostname, endpoint.port)
            writer.close()
            await writer.wait_closed()
    asyncio.run(run())


def test_shutdown_closes_accepted_connection_before_handler_starts():
    async def run():
        accepted = asyncio.Queue()
        server = await asyncio.start_server(lambda r, w: accepted.put_nowait((r, w)), '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection('127.0.0.1', port)
        local_reader, local_writer = await accepted.get()
        bridge = proxy_bridge._SocksBridge('127.0.0.1', 1, b'')
        try:
            bridge.accept(local_reader, local_writer)
            await bridge.close()
            assert local_writer.is_closing()
            assert await asyncio.wait_for(reader.read(), 2) == b''
        finally:
            local_writer.close()
            writer.close()
            await asyncio.gather(local_writer.wait_closed(), writer.wait_closed())
            server.close()
            await server.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize('wire_request,code', [
    (b'\x05\x03\x00\x01\x7f\x00\x00\x01\x00\x50', 7),
    (b'\x05\x01\x01\x01\x7f\x00\x00\x01\x00\x50', 1),
    (b'\x05\x01\x00\x09', 8),
    (b'\x05\x01\x00\x03\x00', 8),
])
def test_malformed_or_unsupported_requests_do_not_reach_upstream(wire_request, code):
    async def run():
        async with upstream() as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                reader, writer, response = await client(local, wire_request)
                assert response[1] == code
                assert not observations['auth']
                assert not observations['requests']
                assert await reader.read() == b''
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


def test_client_and_upstream_handshakes_have_deadlines(monkeypatch):
    monkeypatch.setattr(proxy_bridge, 'HANDSHAKE_TIMEOUT', 0.05)

    async def run():
        async with upstream(stall=True) as (proxy, observations):
            async with proxy_bridge.browser_proxy(proxy) as local:
                endpoint = urlsplit(local['server'])
                reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
                assert await asyncio.wait_for(reader.read(), 2) == b''
                writer.close()
                await writer.wait_closed()
                reader, writer, response = await asyncio.wait_for(client(local), 2)
                assert response[1] == 1
                assert await asyncio.wait_for(reader.read(), 2) == b''
                await asyncio.wait_for(observations['closed'].wait(), 2)
                writer.close()
                await writer.wait_closed()
    asyncio.run(run())


@pytest.mark.parametrize('username,password', [('u', ''), ('', 'p'), ('u' * 256, 'p'), ('é' * 128, 'p'), ('u', 'p' * 256)])
def test_rfc_credential_lengths_are_validated_without_exposing_values(username, password):
    async def run():
        with pytest.raises(proxy_bridge.ProxyBridgeError, match='1.*255'):
            async with proxy_bridge.browser_proxy({
                'server': 'socks5://localhost:1080', 'username': username, 'password': password,
            }):
                raise AssertionError('Invalid credentials were accepted')
    asyncio.run(run())
