"""Pair incremental displays with the existing authenticated control lease."""

import asyncio
import hmac
import time
from contextlib import AsyncExitStack, asynccontextmanager

from anyio import CancelScope
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..store import ValidationError
from .control import ControlError


@asynccontextmanager
async def coalesced_stream(upstream):
    buffer = bytearray()
    changed = asyncio.Condition()

    async def receive():
        started, total = time.monotonic(), 0
        try:
            async for block in upstream:
                if not isinstance(block, bytes) or len(block) > 64 * 1024 * 1024:
                    raise ControlError('invalid_request')
                now = time.monotonic()
                if now - started >= 1:
                    started, total = now, 0
                total += len(block)
                if total > 128 * 1024 * 1024:
                    raise ControlError('rate_limited')
                offset = 0
                while offset < len(block):
                    async with changed:
                        await changed.wait_for(lambda: len(buffer) < 65536)
                        end = min(len(block), offset + 65536 - len(buffer))
                        buffer.extend(memoryview(block)[offset:end])
                        offset = end
                        changed.notify_all()
        finally:
            async with changed:
                changed.notify_all()

    producer = asyncio.create_task(receive())

    async def blocks():
        while True:
            async with changed:
                await changed.wait_for(lambda: buffer or producer.done())
                if not buffer:
                    producer.result()
                    return
                block = bytes(buffer)
                buffer.clear()
                changed.notify_all()
            yield block

    stream = blocks()
    try:
        yield stream
    finally:
        with CancelScope(shield=True):
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            await stream.aclose()


class Viewer:
    def __init__(self, service, cookie, remote, connection_id, control):
        self.service, self.cookie, self.remote = service, cookie, remote
        self.connection_id, self.control = connection_id, control
        self.display = None
        self.closed = False
        self.ready = False
        self.transfers = set()

    async def close(self, code=1000):
        if self.closed:
            return
        self.closed = True
        for task in self.transfers:
            if task is not asyncio.current_task():
                task.cancel()
        # ASGI cancellation must still close the paired channel and release held input.
        with CancelScope(shield=True):
            sockets = [self.control] + ([self.display] if self.display is not None else [])
            await asyncio.gather(*(self.service.close_socket(socket, code) for socket in sockets))
            await self.remote.disconnect(self.connection_id)


def install(app, service, *, prefix, allowed, csrf, cookie_name):
    from .service import TeamError

    @app.websocket(prefix + '/remote/{run_id}/display')
    async def display_stream(run_id: str, socket: WebSocket):
        if not allowed(socket):
            await socket.close(code=4403)
            return
        cookie = socket.cookies.get(cookie_name, '')
        viewer = None
        upstreams = AsyncExitStack()
        try:
            access = await service.access(cookie)
            if (len(service.display_connections) >= 16 or
                    sum(value == cookie for value in service.display_connections.values()) >= 2):
                await socket.close(code=4429)
                return
            service.display_connections[socket] = cookie
            await socket.accept()
            hello = await asyncio.wait_for(socket.receive_json(), 5)
            if (not isinstance(hello, dict) or set(hello) != {'csrf', 'viewer_id'}
                    or not isinstance(hello['csrf'], str) or not isinstance(hello['viewer_id'], str)
                    or not hmac.compare_digest(hello['csrf'].encode(), csrf(cookie).encode())):
                await service.close_socket(socket, 4403)
                return
            candidate = service.viewers.get(hello['viewer_id'])
            if (candidate is None or candidate.closed or candidate.cookie != cookie
                    or candidate.remote.run_id != run_id or candidate.display is not None):
                await service.close_socket(socket, 4403)
                return
            await candidate.remote.watch(access)
            # Associate without an intervening await, so two hellos cannot share one viewer.
            if candidate.closed or candidate.display is not None:
                await service.close_socket(socket, 4403)
                return
            viewer = candidate
            viewer.display = socket
            upstream = await upstreams.enter_async_context(viewer.remote.native.display_stream())
            await asyncio.wait_for(socket.send_json({'ready': True}), 5)
            ready = await asyncio.wait_for(socket.receive_json(), 5)
            if not isinstance(ready, dict) or set(ready) != {'ready'} or ready['ready'] is not True:
                raise ControlError('invalid_request')
            viewer.ready = True

            async def outgoing():
                # RFB is a byte stream; tiny upstream frames must not each trigger database reads.
                async with coalesced_stream(upstream) as blocks:
                    async for block in blocks:
                        await viewer.remote.watch(await service.access(cookie))
                        if viewer.closed:
                            return
                        await asyncio.wait_for(socket.send_bytes(block), 5)

            async def incoming():
                started, count, total = time.monotonic(), 0, 0
                while not viewer.closed:
                    block = await socket.receive_bytes()
                    if len(block) > 1024 * 1024:
                        raise ControlError('invalid_request')
                    now = time.monotonic()
                    if now - started >= 1:
                        started, count, total = now, 0, 0
                    count, total = count + 1, total + len(block)
                    if count > 160 or total > 2 * 1024 * 1024:
                        raise ControlError('rate_limited')
                    await viewer.remote.watch(await service.access(cookie))
                    if viewer.closed:
                        return
                    await asyncio.wait_for(upstream.send(block), 5)

            viewer.transfers = {asyncio.create_task(outgoing()), asyncio.create_task(incoming())}
            try:
                done, _ = await asyncio.wait(viewer.transfers, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if not task.cancelled():
                        task.result()
            finally:
                with CancelScope(shield=True):
                    for task in viewer.transfers:
                        task.cancel()
                    await asyncio.gather(*viewer.transfers, return_exceptions=True)
        except (TeamError, ValidationError, ControlError):
            await service.close_socket(socket, 4401)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            await service.close_socket(socket, 1011)
        finally:
            service.display_connections.pop(socket, None)
            with CancelScope(shield=True):
                try:
                    await upstreams.aclose()
                finally:
                    if viewer is not None:
                        await viewer.close()
