"""Real headed Camoufox, loopback HTTP artifacts and the public byte-transfer API."""

import asyncio
import base64
import contextlib
import hashlib
import json
import shutil
from pathlib import Path

import httpx
import pytest

from camoufox.studio.access import MemberStore
from camoufox.studio.automation import AutomationBridge, PREFIX
from camoufox.studio.engine import BrowserEngine
from camoufox.studio.workspace import WorkspaceLock
from studio_runtime_tests import workspace  # noqa: F401


IMAGE = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLttAAAAABJRU5ErkJggg==')


@contextlib.asynccontextmanager
async def artifact_site():
    release = asyncio.Event()
    requested = asyncio.Event()
    connections = set()
    counts = {}

    async def serve(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            path = header.split(b' ')[1].decode('ascii')
            counts[path] = counts.get(path, 0) + 1
            if path == '/':
                body = ('<title>Local original-byte validation</title>'
                        '<button id="image" onclick="fetch(\'/image\')">Image</button>'
                        '<button id="redirect" onclick="fetch(\'/redirect\')">Redirect</button>'
                        '<button id="slow" onclick="fetch(\'/slow\')">Slow</button>'
                        '<button id="broken" onclick="fetch(\'/broken\').catch(()=>{})">Broken</button>'
                        '<a id="download" href="/attachment">Download</a>').encode()
                writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
            elif path == '/redirect':
                writer.write(b'HTTP/1.1 302 Found\r\nLocation: /image\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            else:
                disposition = 'Content-Disposition: attachment; filename="original.png"\r\n' if path == '/attachment' else ''
                writer.write((f'HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: {len(IMAGE)}\r\n'
                              f'{disposition}Cache-Control: no-store\r\nConnection: close\r\n\r\n').encode())
                if path == '/slow':
                    writer.write(IMAGE[:10])
                    await writer.drain()
                    requested.set()
                    await release.wait()
                    writer.write(IMAGE[10:])
                else:
                    writer.write(IMAGE[:10] if path == '/broken' else IMAGE)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            connections.discard(task)

    server = await asyncio.start_server(serve, '127.0.0.1', 0)
    try:
        yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}', requested, release, counts
    finally:
        release.set()
        server.close()
        await server.wait_closed()
        for task in list(connections):
            task.cancel()
        await asyncio.gather(*list(connections), return_exceptions=True)


@pytest.mark.asyncio
async def test_real_response_and_download_bytes_cleanup_and_failure(workspace, tmp_path, monkeypatch):
    store, _, _, profile = workspace
    # Timing assertions exclude the optional randomized mouse-motion simulation.
    store.save_profile({'humanize': False}, profile['id'])
    lock = WorkspaceLock(store.root)
    lock.acquire()
    engine = BrowserEngine(store, lambda *_: None, lambda *_: None, lambda *_: None)
    access = MemberStore(store, store.open_local_automation_session('operator'))
    bridge = AutomationBridge(engine, access=access)
    try:
        await bridge.start()
        async with httpx.AsyncClient(base_url=bridge.base_url, trust_env=False, timeout=130,
                                    headers={'Authorization': 'Bearer ' + bridge.token}) as client:
            async def request(method, route, body=None):
                response = await client.request(method, PREFIX + route, json=body)
                assert response.status_code == 200, response.text
                return response.json()

            await request('POST', '/profiles/' + profile['id'] + '/start', {'headless': False})
            lease = (await request('POST', '/leases', {'profileId': profile['id']}))['leaseId']
            route = '/leases/' + lease
            page_id = (await request('POST', route + '/pages', {}))['pageId']
            page_two = (await request('POST', route + '/pages', {}))['pageId']

            async def call(method, args=None, handle=None, page=page_id, locator=None):
                body = {'pageId': page, 'method': method, 'args': args or []}
                if handle:
                    body['handleId'] = handle
                if locator:
                    body['locator'] = [{'method': 'locator', 'args': [locator]}]
                return (await request('POST', route + '/call', body))['value']

            async def bytes_for(handle, page=page_id):
                chunks, offset = [], 0
                while True:
                    response = await client.post(PREFIX + route + '/call', json={
                        'pageId': page, 'handleId': handle, 'method': 'readBinary',
                        'args': [{'offset': offset, 'chunkBytes': 17, 'maxBytes': 4096}]})
                    assert response.status_code == 200, response.text
                    meta = json.loads(response.headers['x-studio-transfer'])
                    assert meta['offset'] == offset and meta['readBytes'] == len(response.content)
                    assert meta['chunkSha256'] == hashlib.sha256(response.content).hexdigest()
                    chunks.append(response.content)
                    offset += len(response.content)
                    if meta['complete']:
                        assert offset == meta['totalBytes'] == len(IMAGE)
                        assert meta['sha256'] == hashlib.sha256(IMAGE).hexdigest()
                        break
                assert b''.join(chunks) == IMAGE

            async with artifact_site() as (origin, requested, release, counts):
                await call('goto', [origin])
                await call('goto', [origin], page=page_two)

                async def observe(path, page=page_id):
                    return (await call('observeResponse', [{'origin': origin, 'url': origin + path,
                                                           'method': 'GET', 'timeout': 5000}], page=page))['$handle']

                for target in ('image', 'redirect'):
                    handle = await observe('/' + target)
                    other = await observe('/' + target, page_two)
                    await call('click', locator='#' + target)
                    response = await client.post(PREFIX + route + '/call', json={
                        'pageId': page_id, 'handleId': handle, 'method': 'result'})
                    assert response.status_code == 200, (target, counts, bridge.leases[lease].handles[handle]['value'].status())
                    result = response.json()['value']
                    assert result['state'] == 'complete' and result['mimeType'] == 'image/png'
                    assert len(result['redirects']) == (1 if target == 'redirect' else 0)
                    assert (await call('status', handle=other, page=page_two))['state'] == 'waiting'
                    await bytes_for(handle)
                    await call('delete', handle=handle)
                    await call('delete', handle=other, page=page_two)

                concurrent = [await observe('/image', page) for page in (page_id, page_two)]
                await asyncio.gather(*(call('goto', [origin + '/image'], page=page) for page in (page_id, page_two)))
                results = await asyncio.gather(*(call('result', handle=handle, page=page)
                    for handle, page in zip(concurrent, (page_id, page_two))))
                assert results[0]['requestId'] != results[1]['requestId']
                for handle, page in zip(concurrent, (page_id, page_two)):
                    await bytes_for(handle, page)
                    await call('delete', handle=handle, page=page)
                    await call('goto', [origin], page=page)

                observation = await observe('/attachment')
                download = (await call('expectEvent', ['download', {'timeout': 5000}]))['$handle']
                await call('click', locator='#download')
                assert (await call('result', handle=download))['type'] == 'download'
                result = await call('result', handle=observation)
                assert result['state'] in ('attachment', 'failed', 'unavailable')
                legacy = await call('read', [{'maxBytes': 4096}], handle=download)
                assert base64.b64decode(legacy['base64']) == IMAGE
                await bytes_for(download)
                await call('delete', handle=download)
                await call('delete', handle=observation)
                assert counts['/attachment'] == 1

                delayed = await observe('/slow')
                await call('click', locator='#slow')
                await asyncio.wait_for(requested.wait(), 5)
                assert (await call('status', handle=delayed))['state'] in ('requested', 'responded')
                release.set()
                assert (await call('result', handle=delayed))['state'] == 'complete'
                await bytes_for(delayed)
                await call('delete', handle=delayed)

                limited = await observe('/image')
                await call('click', locator='#image')
                await call('result', handle=limited)
                response = await client.post(PREFIX + route + '/call', json={
                    'pageId': page_id, 'handleId': limited, 'method': 'readBinary',
                    'args': [{'maxBytes': len(IMAGE) - 1}]})
                assert response.status_code == 413

                requested.clear()
                release.clear()
                cancelled = await observe('/slow')
                await call('click', locator='#slow')
                await asyncio.wait_for(requested.wait(), 5)
                await call('delete', handle=cancelled)
                release.set()
                response = await client.post(PREFIX + route + '/call', json={
                    'pageId': page_id, 'handleId': cancelled, 'method': 'status'})
                assert response.status_code == 404

                broken = await observe('/broken')
                await call('click', locator='#broken')
                result = await call('result', handle=broken)
                assert result['state'] == 'failed'
                response = await client.post(PREFIX + route + '/call', json={
                    'pageId': page_id, 'handleId': broken, 'method': 'readBinary', 'args': [{}]})
                assert response.status_code == 409 and response.json()['error']['code'] == 'response_body_unavailable'
                assert not bridge.leases[lease].handles
                assert bridge.body_budget['bytes'] == 0
                await request('DELETE', route)
                node = shutil.which('node')
                assert node, 'Install Node.js 22+ to validate the shipped artifact client.'
                connection = tmp_path / 'automation.json'
                connection.touch(mode=0o600)
                connection.write_text(json.dumps({'protocolVersion': 1, 'baseURL': bridge.base_url,
                                                   'token': bridge.token}), encoding='utf-8')
                try:
                    for mode in ('response', 'download', 'auto', 'legacy'):
                        if mode == 'legacy':
                            monkeypatch.setattr('camoufox.studio.automation.CAPABILITIES', {'mainWorldEval': True})
                        target = 'image' if mode == 'response' else 'attachment'
                        job = tmp_path / 'job.json'
                        job.write_text(json.dumps({'pageURL': origin, 'selector': '#image' if mode == 'response' else '#download',
                            'mode': 'auto' if mode == 'legacy' else mode,
                            'request': {'origin': origin, 'method': 'GET', 'url': origin + '/' + target},
                            'downloadFilter': {'origin': origin, 'url': origin + '/attachment'}}), encoding='utf-8')
                        output = tmp_path / (mode + '.png')
                        program = Path(__file__).resolve().parents[1] / 'scripts/studio-artifact-client.mjs'
                        process = await asyncio.create_subprocess_exec(node, str(program), str(connection),
                            profile['id'], str(job), str(output), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                        try:
                            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
                            assert process.returncode == 0, stderr.decode(errors='replace')
                        finally:
                            if process.returncode is None:
                                process.kill()
                                await process.wait()
                        report = json.loads(stdout)
                        assert output.read_bytes() == IMAGE
                        assert report['bytes'] == len(IMAGE) and report['sha256'] == hashlib.sha256(IMAGE).hexdigest()
                        assert not bridge.leases
                finally:
                    connection.unlink(missing_ok=True)
            assert len(engine.sessions[profile['id']].context.pages) == 1
    finally:
        await bridge.stop()
        await engine.shutdown()
        lock.release()
