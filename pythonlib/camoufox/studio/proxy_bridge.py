"""Session-scoped SOCKS5 authentication for Playwright's unauthenticated client.

The TCP CONNECT and username/password exchanges follow RFC 1928 and RFC 1929.
Destination addresses are forwarded verbatim, so DNS remains at the upstream.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from .store import normalize_proxy_server

HANDSHAKE_TIMEOUT = 10.0
CLOSE_TIMEOUT = 2.0
MAX_CONNECTIONS = 256
_FAILURE_ADDRESS = b'\x00\x01\x00\x00\x00\x00\x00\x00'


class ProxyBridgeError(RuntimeError):
    """A sanitized configuration or listener error, without credential values."""


class _Rejected(Exception):
    def __init__(self, reply: int | None):
        self.reply = reply


async def _address(reader: asyncio.StreamReader, kind: int) -> bytes:
    if kind == 1:
        return await reader.readexactly(4)
    if kind == 4:
        return await reader.readexactly(16)
    if kind == 3:
        size = await reader.readexactly(1)
        if not size[0]:
            raise _Rejected(8)
        name = await reader.readexactly(size[0])
        if any(char <= 32 or char >= 127 for char in name):
            raise _Rejected(8)
        return size + name
    raise _Rejected(8)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), CLOSE_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        writer.transport.abort()


class _Connection:
    def __init__(self, bridge, reader, writer):
        self.bridge = bridge
        self.reader = reader
        self.writer = writer
        self.upstream_reader = None
        self.upstream_writer = None
        self.negotiated = False
        self.established = False

    async def handshake(self):
        version, count = await self.reader.readexactly(2)
        if version != 5 or count == 0:
            raise _Rejected(None)
        methods = await self.reader.readexactly(count)
        if 0 not in methods:
            self.writer.write(b'\x05\xff')
            await self.writer.drain()
            raise _Rejected(None)
        self.writer.write(b'\x05\x00')
        await self.writer.drain()
        self.negotiated = True
        header = await self.reader.readexactly(4)
        if header[0] != 5 or header[2] != 0:
            raise _Rejected(1)
        if header[1] != 1:
            raise _Rejected(7)
        destination = await _address(self.reader, header[3])
        port = await self.reader.readexactly(2)
        if port == b'\x00\x00':
            raise _Rejected(1)

        # Only the configured proxy is ever opened locally, never the destination.
        self.upstream_reader, self.upstream_writer = await asyncio.open_connection(
            self.bridge.host, self.bridge.port,
        )
        upstream = self.upstream_writer
        upstream.write(b'\x05\x01\x02')
        await upstream.drain()
        if await self.upstream_reader.readexactly(2) != b'\x05\x02':
            raise _Rejected(2)
        upstream.write(self.bridge.authentication)
        await upstream.drain()
        if await self.upstream_reader.readexactly(2) != b'\x01\x00':
            raise _Rejected(2)
        upstream.write(header + destination + port)
        await upstream.drain()
        response = await self.upstream_reader.readexactly(4)
        if response[0] != 5 or response[2] != 0 or response[1] > 8:
            raise _Rejected(1)
        bound = await _address(self.upstream_reader, response[3])
        bound_port = await self.upstream_reader.readexactly(2)
        if response[1] != 0:
            raise _Rejected(response[1])
        self.writer.write(response + bound + bound_port)
        self.established = True
        await self.writer.drain()

    @staticmethod
    async def pump(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
            await writer.drain()

    async def relay(self):
        tasks = [
            asyncio.create_task(self.pump(self.reader, self.upstream_writer)),
            asyncio.create_task(self.pump(self.upstream_reader, self.writer)),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self):
        failure = None
        try:
            await asyncio.wait_for(self.handshake(), HANDSHAKE_TIMEOUT)
            await self.relay()
        except _Rejected as error:
            failure = error.reply
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            failure = 1
        finally:
            if failure is not None and self.negotiated and not self.established:
                with suppress(OSError, asyncio.TimeoutError):
                    self.writer.write(bytes([5, failure]) + _FAILURE_ADDRESS)
                    await asyncio.wait_for(self.writer.drain(), CLOSE_TIMEOUT)
            writers = [self.writer]
            if self.upstream_writer is not None:
                writers.append(self.upstream_writer)
            await asyncio.gather(*(_close(writer) for writer in writers))


class _SocksBridge:
    def __init__(self, host, port, authentication):
        self.host = host
        self.port = port
        self.authentication = authentication
        self.server = None
        self.tasks = {}
        self.closing = False

    def accept(self, reader, writer):
        if self.closing or len(self.tasks) >= MAX_CONNECTIONS:
            writer.close()
            return
        connection = _Connection(self, reader, writer)
        task = asyncio.create_task(connection.run())
        self.tasks[task] = connection
        task.add_done_callback(self.completed)

    def completed(self, task):
        self.tasks.pop(task, None)
        if not task.cancelled():
            task.exception()

    async def start(self) -> dict:
        try:
            self.server = await asyncio.start_server(self.accept, '127.0.0.1', 0)
        except OSError:
            raise ProxyBridgeError('Could not open the local SOCKS5 authentication bridge.') from None
        port = self.server.sockets[0].getsockname()[1]
        return {'server': f'socks5://127.0.0.1:{port}'}

    async def close(self):
        self.closing = True
        if self.server is not None:
            self.server.close()
        tasks = tuple(self.tasks)
        writers = []
        for connection in self.tasks.values():
            writers.append(connection.writer)
            if connection.upstream_writer is not None:
                writers.append(connection.upstream_writer)
        # A task cancelled before its first step never reaches its finally block.
        for writer in writers:
            writer.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(_close(writer) for writer in writers))
        if self.server is not None:
            await self.server.wait_closed()
        self.authentication = b''


@asynccontextmanager
async def browser_proxy(proxy: dict | None):
    """Adapt authenticated SOCKS5 for one browser session, closing every relay on exit."""
    if not proxy or urlsplit(proxy['server']).scheme != 'socks5' or not (
        proxy.get('username') or proxy.get('password')
    ):
        yield proxy
        return
    try:
        username = proxy.get('username', '').encode('utf-8')
        password = proxy.get('password', '').encode('utf-8')
    except (AttributeError, UnicodeError):
        raise ProxyBridgeError('SOCKS5 credentials must be valid UTF-8 text.') from None
    if not 1 <= len(username) <= 255 or not 1 <= len(password) <= 255:
        raise ProxyBridgeError('SOCKS5 username and password must each contain 1 to 255 UTF-8 bytes.')
    endpoint = urlsplit(normalize_proxy_server(proxy['server']))
    authentication = bytes([1, len(username)]) + username + bytes([len(password)]) + password
    bridge = _SocksBridge(endpoint.hostname, endpoint.port, authentication)
    try:
        yield await bridge.start()
    finally:
        await bridge.close()
