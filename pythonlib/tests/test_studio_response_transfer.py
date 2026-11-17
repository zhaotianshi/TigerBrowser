"""Passive observation and byte transfer preserve page ownership and exact content."""

import asyncio
import base64
import hashlib
import json

import httpx
import pytest

from camoufox.studio.automation import AutomationError, PREFIX
from test_studio_automation import acquire, harness


class Request:
    def __init__(self, url='https://files.example/image?signature=private', method='GET', previous=None):
        self.url, self.method, self.redirected_from = url, method, previous


class Response:
    def __init__(self, request, data=b'original\x00bytes', status=200, headers=None):
        self.request, self.status, self.data = request, status, data
        self.headers = headers or {'content-type': 'image/png', 'content-length': str(len(data))}
        self.reads = 0

    async def body(self):
        self.reads += 1
        return self.data


async def call(bridge, key, page, method, args=None, handle=None):
    body = {'pageId': page, 'method': method, 'args': args or []}
    if handle:
        body['handleId'] = handle
    return await bridge.dispatch('POST', f'{PREFIX}/leases/{key}/call', body)


async def observe(bridge, key, page, **opts):
    result = await call(bridge, key, page, 'observeResponse', [
        {'origin': 'https://files.example', 'method': 'GET', 'urlPrefix': 'https://files.example/image', **opts}])
    return result['value']['$handle']


def test_observation_registered_before_action_and_reads_exact_binary(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        page = context.pages[-1]
        try:
            handle = await observe(bridge, key, page_id)
            assert page.listeners['request'] and page.listeners['response']
            unrelated = Request('https://files.example.evil/image')
            page.emit('request', unrelated)
            assert (await call(bridge, key, page_id, 'status', handle=handle))['value']['state'] == 'waiting'
            req = Request()
            response = Response(req)
            page.emit('request', req)
            page.emit('response', response)
            pending = asyncio.create_task(call(bridge, key, page_id, 'result', handle=handle))
            await asyncio.sleep(0)
            assert not pending.done()
            page.emit('requestfinished', req)
            result = (await pending)['value']
            assert result['state'] == 'complete' and result['status'] == 200
            assert result['requestId'] and result['responseId']
            assert 'private' not in str(result)
            assert not any(page.listeners.get(name) for name in ('request', 'response', 'requestfinished', 'requestfailed'))
            chunks = []
            offset = 0
            while True:
                chunk = await call(bridge, key, page_id, 'readBinary',
                                   [{'offset': offset, 'maxBytes': 100, 'chunkBytes': 4}], handle)
                chunks.append(chunk.data)
                assert chunk.metadata['offset'] == offset
                offset += len(chunk.data)
                if chunk.metadata['complete']:
                    break
            assert b''.join(chunks) == response.data
            assert chunk.metadata['totalBytes'] == offset
            assert chunk.metadata['sha256'] == hashlib.sha256(response.data).hexdigest()
            assert response.reads == 1
            await call(bridge, key, page_id, 'delete', handle=handle)
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, page_id, 'status', handle=handle)
            assert error.value.code == 'handle_not_found'
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_observation_redirects_attachment_failure_and_page_scope(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        page = context.pages[-1]
        other = await bridge.dispatch('POST', f'{PREFIX}/leases/{key}/pages', {})
        try:
            handle = await observe(bridge, key, page_id)
            context.pages[-1].emit('request', Request())
            assert (await call(bridge, key, page_id, 'status', handle=handle))['value']['state'] == 'waiting'
            first = Request()
            page.emit('request', first)
            page.emit('response', Response(first, status=302, headers={'location': '/final'}))
            page.emit('requestfinished', first)
            last = Request('https://files.example/final', previous=first)
            page.emit('request', last)
            page.emit('response', Response(last, headers={'content-disposition': 'attachment; filename="private.png"'}))
            result = (await call(bridge, key, page_id, 'result', handle=handle))['value']
            assert result['state'] == 'attachment'
            assert len(result['redirects']) == 1
            assert result['redirects'][0]['requestId'] == result['redirectedFrom']
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, page_id, 'readBinary', [{}], handle)
            assert error.value.code == 'response_body_unavailable'
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, other['pageId'], 'status', handle=handle)
            assert error.value.code == 'handle_not_found'
            failed = await observe(bridge, key, page_id)
            req = Request()
            page.emit('request', req)
            page.emit('requestfailed', req)
            assert (await call(bridge, key, page_id, 'result', handle=failed))['value']['state'] == 'failed'
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_observation_cancel_timeout_and_size_failure_clean_resources(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        page = context.pages[-1]
        try:
            handle = await observe(bridge, key, page_id)
            pending = asyncio.create_task(call(bridge, key, page_id, 'result', handle=handle))
            await asyncio.sleep(0)
            await call(bridge, key, page_id, 'delete', handle=handle)
            with pytest.raises(AutomationError) as error:
                await pending
            assert error.value.code == 'handle_not_found'
            timed = await observe(bridge, key, page_id, timeout=1)
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, page_id, 'result', handle=timed)
            assert error.value.code == 'operation_timeout'
            assert not any(page.listeners.get(name) for name in ('request', 'response', 'requestfinished', 'requestfailed'))
            large = await observe(bridge, key, page_id)
            req = Request()
            response = Response(req, b'12345')
            page.emit('request', req)
            page.emit('response', response)
            page.emit('requestfinished', req)
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, page_id, 'readBinary', [{'maxBytes': 4}], large)
            assert error.value.status == 413
            assert response.reads == 0
        finally:
            await bridge.stop()
    asyncio.run(run())


class Download:
    suggested_filename = 'original.bin'

    def __init__(self, path):
        self.filename = path
        self.ready = asyncio.Event()
        self.started = asyncio.Event()
        self.deleted = False

    async def path(self):
        self.started.set()
        await self.ready.wait()
        return self.filename

    async def cancel(self):
        self.ready.set()

    async def delete(self):
        self.filename.unlink(missing_ok=True)
        self.deleted = True


async def download_handle(bridge, key, page_id, page, path):
    waiter = await call(bridge, key, page_id, 'expectEvent', ['download'])
    download = Download(path)
    page.emit('download', download)
    handle = waiter['value']['$handle']
    await call(bridge, key, page_id, 'result', handle=handle)
    return handle, download


def test_binary_http_auth_integrity_offsets_and_legacy_compatibility(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        await bridge.start()
        try:
            key, page_id = await acquire(bridge)
            data = bytes(range(256)) * 8193
            path = tmp_path / 'original.bin'
            path.write_bytes(data)
            handle, download = await download_handle(bridge, key, page_id, context.pages[-1], path)
            download.ready.set()
            legacy = await call(bridge, key, page_id, 'read', [{'maxBytes': len(data)}], handle)
            assert base64.b64decode(legacy['value']['base64']) == data
            async with httpx.AsyncClient(base_url=bridge.base_url, trust_env=False) as client:
                route = f'{PREFIX}/leases/{key}/call'
                body = {'pageId': page_id, 'handleId': handle, 'method': 'readBinary',
                        'args': [{'offset': 0, 'maxBytes': len(data), 'chunkBytes': 1024 * 1024}]}
                denied = await client.post(route, json=body)
                assert denied.status_code == 401
                headers = {'Authorization': 'Bearer ' + bridge.token}
                denied = await client.post(route, headers=headers | {'Origin': 'https://other.example'}, json=body)
                assert denied.status_code == 403
                chunks = []
                offset = 0
                while True:
                    body['args'][0]['offset'] = offset
                    response = await client.post(route, headers=headers, json=body)
                    assert response.status_code == 200, response.text
                    assert response.headers['content-type'] == 'application/octet-stream'
                    assert response.headers['cache-control'] == 'no-store'
                    metadata = json.loads(response.headers['x-studio-transfer'])
                    assert int(response.headers['content-length']) == metadata['readBytes'] == len(response.content)
                    assert metadata['chunkSha256'] == hashlib.sha256(response.content).hexdigest()
                    assert metadata['offset'] == offset and metadata['totalBytes'] == len(data)
                    chunks.append(response.content)
                    offset += len(response.content)
                    if metadata['complete']:
                        assert metadata['sha256'] == hashlib.sha256(data).hexdigest()
                        break
                assert b''.join(chunks) == data
                body['args'][0]['offset'] = 1
                rejected = await client.post(route, headers=headers, json=body)
                assert rejected.status_code == 409 and rejected.json()['error']['code'] == 'transfer_offset'
                await call(bridge, key, page_id, 'delete', handle=handle)
                rejected = await client.post(route, headers=headers, json=body)
                assert rejected.status_code == 404 and download.deleted
        finally:
            await bridge.stop()
    asyncio.run(run())


@pytest.mark.parametrize('action', ['delete', 'page_close', 'disconnect', 'expire'])
def test_pending_binary_read_cleans_download_and_listeners(tmp_path, action):
    async def run():
        bridge, context, now = harness(tmp_path)
        await bridge.start()
        try:
            key, page_id = await acquire(bridge)
            page = context.pages[-1]
            await observe(bridge, key, page_id)
            path = tmp_path / 'pending.bin'
            path.write_bytes(b'pending')
            handle, download = await download_handle(bridge, key, page_id, page, path)
            async with httpx.AsyncClient(base_url=bridge.base_url, trust_env=False) as client:
                pending = asyncio.create_task(client.post(f'{PREFIX}/leases/{key}/call',
                    headers={'Authorization': 'Bearer ' + bridge.token},
                    json={'pageId': page_id, 'handleId': handle, 'method': 'readBinary', 'args': [{}]}))
                await asyncio.wait_for(download.started.wait(), 2)
                if action == 'delete':
                    await call(bridge, key, page_id, 'delete', handle=handle)
                    result = await pending
                    assert result.status_code == 404
                elif action == 'page_close':
                    await page.close()
                    result = await pending
                    assert result.status_code == 410
                elif action == 'expire':
                    now[0] += 700
                    bridge.leases[key].expires = now[0] + 90
                    await bridge.expire()
                    assert (await pending).status_code == 404
                    assert not bridge.leases[key].handles
                else:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                if action == 'delete':
                    await bridge.release(key)
                async def cleaned():
                    while not download.deleted or (action == 'disconnect' and key in bridge.leases):
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(cleaned(), 2)
                assert not path.exists()
                assert not any(page.listeners.get(name) for name in ('request', 'response', 'requestfinished', 'requestfailed'))
                assert not context.pages[0].closed
        finally:
            await bridge.stop()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['short', 'unknown_large', 'unavailable', 'timeout', 'budget'])
def test_response_body_limits_errors_and_budget_cleanup(tmp_path, mode):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        try:
            handle = await observe(bridge, key, page_id)
            req = Request()
            headers = {'content-length': '10'} if mode == 'short' else {'content-type': 'image/png'}
            response = Response(req, b'12345', headers=headers)
            if mode == 'unavailable':
                async def unavailable():
                    raise RuntimeError('private signed URL')
                response.body = unavailable
            if mode == 'timeout':
                async def stalled():
                    await asyncio.Event().wait()
                response.body = stalled
            if mode == 'budget':
                bridge.body_budget['limit'] = 1
            page = context.pages[-1]
            page.emit('request', req)
            page.emit('response', response)
            page.emit('requestfinished', req)
            expected = {'short': 'body_incomplete', 'unknown_large': 'image_size_limit',
                        'unavailable': 'response_body_unavailable', 'budget': 'resource_limit'}
            if mode == 'timeout':
                with pytest.raises(asyncio.TimeoutError):
                    await call(bridge, key, page_id, 'readBinary', [{'timeout': 1}], handle)
            else:
                with pytest.raises(AutomationError) as error:
                    await call(bridge, key, page_id, 'readBinary',
                               [{'maxBytes': 4 if mode == 'unknown_large' else 20}], handle)
                assert error.value.code == expected[mode]
                assert 'private' not in str(error.value)
            assert bridge.body_budget['bytes'] == 0
            assert handle not in bridge.leases[key].handles
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_binary_handles_reject_another_owner_lease_page_and_restarted_session(tmp_path):
    async def run():
        from camoufox.studio.automation import Lease, _REQUEST_ACCESS
        from test_studio_automation import Member
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        try:
            handle = await observe(bridge, key, page_id)
            other_member = Member(bridge.engine.store)
            other_member.api_key_id = 'other-key'
            token = _REQUEST_ACCESS.set(other_member)
            try:
                with pytest.raises(AutomationError) as error:
                    await call(bridge, key, page_id, 'status', handle=handle)
                assert error.value.code == 'lease_not_found'
            finally:
                _REQUEST_ACCESS.reset(token)
            bridge.leases['other'] = Lease('profile', context, 190, access=bridge.access, pages={'other-page': context.pages[-1]})
            try:
                with pytest.raises(AutomationError) as error:
                    await call(bridge, 'other', 'other-page', 'status', handle=handle)
                assert error.value.code == 'handle_not_found'
            finally:
                bridge.leases.pop('other')
            bridge.engine.sessions['profile'].context = object()
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, page_id, 'status', handle=handle)
            assert error.value.code == 'profile_not_running' and error.value.status == 409
        finally:
            await bridge.stop()
    asyncio.run(run())


@pytest.mark.parametrize('options', [
    {'origin': None}, {'origin': 'https://files.example/path'}, {'method': 'get'},
    {'method': None}, {'url': 'https://other.example/image'}, {'url': 'https://files.example/image'},
    {'timeout': float('nan')}, {'timeout': True}, {'intercept': True},
])
def test_invalid_observers_never_attach_listeners(tmp_path, options):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        try:
            with pytest.raises(AutomationError) as error:
                await observe(bridge, key, page_id, **options)
            assert error.value.code == 'invalid_request'
            assert not bridge.leases[key].handles
            assert not context.pages[-1].listeners.get('request')
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_download_filter_rejects_wrong_origin_and_waits_for_matching_event(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        try:
            registered = await call(bridge, key, page_id, 'expectEvent', ['download', {
                'urlFilter': {'origin': 'https://files.example', 'urlPrefix': 'https://files.example/image'}}])
            handle = registered['value']['$handle']
            wrong = Download(tmp_path / 'wrong.bin')
            wrong.url = 'https://files.example.evil/image'
            context.pages[-1].emit('download', wrong)
            assert not bridge.leases[key].handles[handle]['value'].done()
            wanted = Download(tmp_path / 'wanted.bin')
            wanted.url = 'https://files.example/image?signature=private'
            context.pages[-1].emit('download', wanted)
            result = (await call(bridge, key, page_id, 'result', handle=handle))['value']
            assert result['type'] == 'download' and 'private' not in str(result)
            await call(bridge, key, page_id, 'delete', handle=handle)
            assert wanted.deleted and not wrong.deleted
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_parallel_reads_and_revocation_during_body_read_never_return_bytes(tmp_path):
    async def run():
        from camoufox.studio.store import ValidationError
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        ready, finish = asyncio.Event(), asyncio.Event()
        try:
            handle = await observe(bridge, key, page_id)
            req = Request()
            response = Response(req)
            async def body():
                ready.set()
                await finish.wait()
                return response.data
            response.body = body
            page = context.pages[-1]
            page.emit('request', req)
            page.emit('response', response)
            page.emit('requestfinished', req)
            pending = asyncio.create_task(call(bridge, key, page_id, 'readBinary', [{}], handle))
            await ready.wait()
            with pytest.raises(AutomationError) as error:
                await call(bridge, key, page_id, 'readBinary', [{}], handle)
            assert error.value.code == 'transfer_busy'
            def revoked():
                raise ValidationError('session_expired')
            bridge.access.resolve = revoked
            finish.set()
            with pytest.raises(AutomationError) as error:
                await pending
            assert error.value.status == 401
            await bridge.expire()
            assert not bridge.leases and bridge.body_budget['bytes'] == 0
        finally:
            finish.set()
            await bridge.stop()
    asyncio.run(run())


def test_sweeper_cancellation_retains_inflight_artifact_cleanup(tmp_path):
    async def run():
        bridge, context, now = harness(tmp_path)
        key, page_id = await acquire(bridge)
        handle, download = await download_handle(bridge, key, page_id, context.pages[-1], tmp_path / 'expired.bin')
        deleting, finish = asyncio.Event(), asyncio.Event()
        async def delete():
            deleting.set()
            await finish.wait()
            download.deleted = True
        download.delete = delete
        now[0] += 700
        bridge.leases[key].expires = now[0] + 90
        sweep = asyncio.create_task(bridge.expire())
        await asyncio.wait_for(deleting.wait(), 2)
        sweep.cancel()
        await asyncio.gather(sweep, return_exceptions=True)
        assert handle not in bridge.leases[key].handles
        try:
            assert bridge.leases[key].cleanup_tasks
        finally:
            finish.set()
            await bridge.stop()
        assert download.deleted
    asyncio.run(run())
