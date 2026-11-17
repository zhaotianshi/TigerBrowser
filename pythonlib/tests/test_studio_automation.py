"""Authenticated HTTP, ownership and event ordering contracts for Studio clients."""

import asyncio
import base64
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from contextlib import asynccontextmanager

import pytest

from camoufox.studio.automation import AutomationBridge, AutomationError, PREFIX, files
from camoufox.studio.engine import Session
from camoufox.studio.store import ProfileStore


class Emitter:
    def __init__(self):
        self.listeners = {}

    def on(self, name, callback):
        self.listeners.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        if callback in self.listeners.get(name, []):
            self.listeners[name].remove(callback)

    def emit(self, name, value=None):
        for callback in list(self.listeners.get(name, [])):
            callback(value)


class Page(Emitter):
    def __init__(self, context=None):
        super().__init__()
        self.url = 'about:blank'
        self.closed = False
        self.started = asyncio.Event()
        self.data = None
        self.context = context

    async def close(self):
        self.closed = True
        self.emit('close')

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.url = url

    async def wait_for_load_state(self, state, **kwargs):
        pass

    async def evaluate(self, expression, *, arg=None):
        if 'history.replaceState(' in expression:
            self.url = 'about:blank'
        if 'window.open(' in expression:
            page = await self.context.new_tab(arg)
            self.context.emit('page', page)
            return True
        if expression == 'error':
            raise RuntimeError('secret-token credential@host')
        if expression == 'wait':
            self.started.set()
            await asyncio.Event().wait()
        self.data = arg
        return arg


class Context(Emitter):
    def __init__(self):
        super().__init__()
        self.pages = [Page(self)]
        self.windows = 1

    async def new_page(self):
        self.windows += 1
        return await self.new_tab('about:blank')

    async def new_tab(self, url):
        page = Page(self)
        page.url = url
        self.pages.append(page)
        return page

    @asynccontextmanager
    async def expect_page(self, *, predicate, timeout):
        future = asyncio.get_running_loop().create_future()
        def received(page):
            if predicate(page) and not future.done():
                future.set_result(page)
        self.on('page', received)
        try:
            yield SimpleNamespace(value=future)
            await asyncio.wait_for(asyncio.shield(future), timeout / 1000)
        finally:
            self.remove_listener('page', received)


class Member:
    """An administrator login with every grant, standing in for MemberStore."""

    def __init__(self, store):
        self.store = store

    def resolve(self):
        return {'member': {'name': 'admin', 'role': 'admin'}}

    def require_admin(self):
        pass

    def require_profile(self, profile_id, permission):
        pass

    def renew_local_automation(self):
        pass

    def list_profiles(self):
        return self.store.list_profiles()

    def save_profile(self, data):
        return self.store.save_profile(data)


def harness(tmp_path):
    context = Context()
    profile = dict(id='profile', name='Example', headless=False)
    store = SimpleNamespace(root=tmp_path, list_profiles=lambda: [profile])
    engine = SimpleNamespace(store=store, sessions={'profile': Session(context=context)})
    now = [100]
    return AutomationBridge(engine, clock=lambda: now[0], access=Member(store)), context, now


async def request(bridge, method, path, body=None, *, token=True, extra=None, raw=None, ssl_context=None):
    import ssl
    from urllib.parse import urlsplit
    endpoint = urlsplit(bridge.base_url)
    transport = (ssl_context or ssl.create_default_context()) if endpoint.scheme == 'https' else None
    reader, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port, ssl=transport)
    payload = raw if raw is not None else json.dumps(body or {}).encode()
    headers = {'Host': endpoint.netloc, 'Content-Type': 'application/json',
               'Content-Length': str(len(payload))}
    if token:
        headers['Authorization'] = 'Bearer ' + bridge.token
    headers.update(extra or {})
    header = f'{method} {PREFIX}{path} HTTP/1.1\r\n'
    header += ''.join(f'{key}: {value}\r\n' for key, value in headers.items()) + '\r\n'
    writer.write(header.encode() + payload)
    await writer.drain()
    response = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    await writer.wait_closed()
    response_header, response_body = response.split(b'\r\n\r\n', 1)
    return int(response_header.split(b' ')[1]), json.loads(response_body)


async def acquire(bridge):
    result = await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
    key = result['leaseId']
    page = await bridge.dispatch('POST', PREFIX + '/leases/' + key + '/pages', {})
    return key, page['pageId']


def test_http_auth_origin_host_and_json_validation(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        await bridge.start()
        try:
            discovery = json.loads((tmp_path / 'automation.json').read_text())
            assert discovery == {'protocolVersion': 1, 'baseURL': bridge.base_url, 'token': bridge.token,
                                 'scope': 'team-member', 'member': 'admin'}
            assert len(bridge.token) >= 32
            assert (await request(bridge, 'GET', '/profiles', token=False))[0] == 401
            for extra in ({'Origin': 'https://example.org'}, {'Host': 'example.org'},
                          {'Origin': 'null'}):
                assert (await request(bridge, 'GET', '/profiles', extra=extra))[0] == 403
            for raw in (b'{', b'[]'):
                assert (await request(bridge, 'POST', '/leases', raw=raw))[0] == 400
            assert (await request(bridge, 'POST', '/leases', extra={'Content-Length': '999999999'}))[0] == 413
            assert (await request(bridge, 'POST', '/leases', extra={'Transfer-Encoding': 'chunked'}))[0] == 400
            status, result = await request(bridge, 'GET', '/profiles')
            assert status == 200 and result['profiles'][0]['leased'] is False
            assert set(result['profiles'][0]) == {'id', 'name', 'headless', 'state', 'leased', 'mainWorldEval'}
            assert result['capabilities']['mainWorldEval'] is True
            assert result['capabilities']['responseObservation'] is True
            assert result['capabilities']['responseBodyRead'] is True
            assert result['capabilities']['downloadBinaryRead'] is True
            assert result['capabilities']['binaryTransferVersion'] == 1
        finally:
            await bridge.stop()
        assert not (tmp_path / 'automation.json').exists()
        assert not context.pages[0].closed
    asyncio.run(run())


def test_loopback_bridge_requires_a_member_login(tmp_path):
    async def run():
        store = SimpleNamespace(root=tmp_path, list_profiles=lambda: [])
        bridge = AutomationBridge(SimpleNamespace(store=store, sessions={}))
        with pytest.raises(ValueError, match='member login'):
            await bridge.start()
        assert bridge.server is None and not (tmp_path / 'automation.json').exists()
        with pytest.raises(AutomationError) as denied:
            await bridge.dispatch('GET', PREFIX + '/profiles', {})
        assert denied.value.code == 'unauthorized'
    asyncio.run(run())


def test_native_mouse_click_uses_only_leased_page_and_bounds_wait(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        calls = []
        releases = []
        async def click(x, y, **kwargs):
            calls.append((x, y, kwargs))
        async def blocked(*args, **kwargs):
            await asyncio.Event().wait()
        async def release(**kwargs):
            releases.append(kwargs)
        await bridge.start()
        try:
            lease, page_id = await acquire(bridge)
            context.pages[-1].mouse = SimpleNamespace(click=click, up=release)
            route = '/leases/' + lease + '/call'
            body = {'pageId': page_id, 'method': 'mouse.click',
                    'args': [21, 33, {'delay': 80, 'timeout': 1000}]}
            status, result = await request(bridge, 'POST', route, body)
            assert status == 200 and result['value'] is None
            assert calls == [(21, 33, {'delay': 80})]
            status, result = await request(bridge, 'POST', route, body | {'pageId': 'foreign'})
            assert status == 404 and result['error']['code'] == 'page_not_found'
            assert len(calls) == 1
            context.pages[-1].mouse.click = blocked
            body['args'] = [21, 33, {'timeout': 10}]
            status, result = await request(bridge, 'POST', route, body)
            assert status == 408 and result['error']['code'] == 'operation_timeout'
            assert releases == [{'button': 'left'}]
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_http_create_profile_persists_configuration_without_launch(tmp_path):
    async def run():
        store = ProfileStore(tmp_path)
        proxy = store.save_proxy({'name': 'Automation proxy', 'server': 'http://127.0.0.1:8080'})
        engine = SimpleNamespace(store=store, sessions={})
        bridge = AutomationBridge(engine, access=Member(store))
        await bridge.start()
        try:
            data = {'name': 'Automation profile', 'headless': True, 'locale': 'zh-CN',
                    'proxy_id': proxy['id'], 'tags': ['automation'],
                    'start_urls': ['https://example.com'], 'fingerprint': {'screen_mode': 'identity'}}
            status, result = await request(bridge, 'POST', '/profiles', data)
            assert status == 200
            profile = result['profile']
            assert set(profile) == {'id', 'name', 'state', 'headless', 'mainWorldEval'}
            assert profile['mainWorldEval'] is None
            assert profile == {'id': profile['id'], 'name': data['name'], 'state': 'stopped',
                               'headless': True, 'mainWorldEval': None}
            saved = ProfileStore(tmp_path).get_profile(profile['id'])
            for key in ('name', 'headless', 'locale', 'proxy_id', 'tags', 'start_urls'):
                assert saved[key] == data[key]
            assert saved['fingerprint']['screen_mode'] == 'identity'
            assert saved['identity'] is None and saved['revision'] == 1
            assert (await request(bridge, 'GET', '/profiles'))[1]['profiles'] == [profile | {'leased': False}]
            status, second = await request(bridge, 'POST', '/profiles', data)
            assert status == 200 and second['profile']['id'] != profile['id']
            assert not engine.sessions and not bridge.leases
        finally:
            await bridge.stop()
    asyncio.run(run())


@pytest.mark.parametrize('data', [
    {}, {'name': ''}, {'name': 'Invalid', 'unexpected': True},
    {'name': 'Invalid', 'headless': 'true'}, {'name': 'Invalid', 'os': 'android'},
    {'name': 'Invalid', 'proxy_id': '11111111-1111-4111-8111-111111111111'},
    {'name': 'Invalid', 'start_urls': ['file:///etc/passwd']},
    {'name': 'Invalid', 'identity': {'config': {'navigator.userAgent': 'arbitrary'}}},
])
def test_http_create_profile_rejects_invalid_configuration_without_writes(tmp_path, data):
    async def run():
        store = ProfileStore(tmp_path)
        bridge = AutomationBridge(SimpleNamespace(store=store, sessions={}), access=Member(store))
        await bridge.start()
        try:
            status, result = await request(bridge, 'POST', '/profiles', data)
            assert status == 400 and result['error']['code'] == 'invalid_request'
            assert not store.list_profiles() and not store.list_profiles(archived=True)
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_cancelled_profile_creation_waits_for_storage_without_blocking_loop(tmp_path, monkeypatch):
    async def run():
        store = ProfileStore(tmp_path)
        bridge = AutomationBridge(SimpleNamespace(store=store, sessions={}), access=Member(store))
        started, finish = threading.Event(), threading.Event()
        original = store.save_profile

        def save(data):
            started.set()
            assert finish.wait(5), 'The creation worker was not released.'
            return original(data)

        monkeypatch.setattr(store, 'save_profile', save)
        pending = asyncio.create_task(bridge.dispatch('POST', PREFIX + '/profiles', {'name': 'Cancelled creation'}))
        try:
            assert await asyncio.to_thread(started.wait, 5), 'Profile creation did not reach storage.'
            pending.cancel()
            await asyncio.sleep(0)
            assert not pending.done()
            assert (await bridge.dispatch('GET', PREFIX + '/profiles', {}))['profiles'] == []
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert len(store.list_profiles()) == 1
        finally:
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)
            await bridge.stop()
    asyncio.run(run())


def test_exclusive_expiring_leases_close_only_owned_pages(tmp_path):
    async def run():
        bridge, context, now = harness(tmp_path)
        values = await asyncio.gather(*(bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
                                        for _ in range(2)), return_exceptions=True)
        assert sum(isinstance(item, AutomationError) for item in values) == 1
        key = next(item['leaseId'] for item in values if isinstance(item, dict))
        page = await bridge.dispatch('POST', PREFIX + '/leases/' + key + '/pages', {})
        assert page['pageId'] and len(context.pages) == 2
        now[0] += 80
        await bridge.dispatch('POST', PREFIX + '/leases/' + key + '/heartbeat', {})
        now[0] += 80
        await bridge.expire()
        assert key in bridge.leases
        now[0] += 11
        await bridge.expire()
        assert not bridge.leases
        assert not context.pages[0].closed and context.pages[1].closed
        assert not context.listeners['close']
        with pytest.raises(AutomationError, match='no longer available'):
            await bridge.dispatch('POST', PREFIX + '/leases/' + key + '/heartbeat', {})
    asyncio.run(run())


def test_task_pages_share_existing_window_and_match_only_their_unique_popup(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        original = context.pages[0]
        original.url = 'https://example.org/untouched'
        lease = await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
        key = lease['leaseId']
        pages = await asyncio.gather(*(bridge.dispatch('POST', PREFIX + '/leases/' + key + '/pages', {}) for _ in range(3)))
        assert context.windows == 1
        assert len({p['pageId'] for p in pages}) == 3
        assert all(p['url'] == 'about:blank' for p in pages)
        assert len(set(bridge.leases[key].pages.values())) == 3
        assert original.url == 'https://example.org/untouched'
        await bridge.release(key)
        assert not original.closed
        assert all(page.closed for page in context.pages[1:])
    asyncio.run(run())


def test_task_tab_canonicalizes_marker_without_interrupting_initial_navigation(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        create = context.new_tab
        async def opening(url):
            page = await create(url)
            async def interrupted(*args, **kwargs):
                raise RuntimeError('Navigation to about:blank is interrupted by another navigation')
            page.goto = interrupted
            return page
        context.new_tab = opening
        key, page_id = await acquire(bridge)
        assert bridge.leases[key].pages[page_id].url == 'about:blank'
        assert context.windows == 1
        await bridge.stop()
        assert not context.pages[0].closed
    asyncio.run(run())


def test_blocked_task_tab_does_not_fall_back_to_new_window_or_take_manual_page(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        async def blocked(*args, **kwargs):
            return False
        context.pages[0].evaluate = blocked
        lease = await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
        with pytest.raises(AutomationError) as failure:
            await bridge.dispatch('POST', PREFIX + '/leases/' + lease['leaseId'] + '/pages', {})
        assert failure.value.code == 'tab_creation_failed'
        assert context.windows == 1 and len(context.pages) == 1
        assert not context.pages[0].closed
        assert not bridge.leases[lease['leaseId']].pages
        await bridge.stop()
    asyncio.run(run())


def test_cancelled_release_finishes_cleanup_and_unreserves_profile(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, _ = await acquire(bridge)
        closing, finish = asyncio.Event(), asyncio.Event()
        page = context.pages[-1]
        original = page.close

        async def close():
            closing.set()
            await finish.wait()
            await original()

        page.close = close
        request = asyncio.create_task(bridge.dispatch('DELETE', PREFIX + '/leases/' + key, {}))
        await asyncio.wait_for(closing.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        with pytest.raises(AutomationError, match='already used'):
            await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
        cleanup = asyncio.create_task(bridge.release(key))
        finish.set()
        await asyncio.wait_for(cleanup, 2)
        assert not bridge.leases and page.closed and not context.pages[0].closed
        await acquire(bridge)
        await bridge.stop()
    asyncio.run(run())


def test_release_does_not_wait_for_stalled_page_creation_and_closes_late_page(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        lease = await bridge.dispatch('POST', PREFIX + '/leases', {'profileId': 'profile'})
        key = lease['leaseId']
        entered, finish = asyncio.Event(), asyncio.Event()
        original = context.new_tab

        async def new_page(url):
            entered.set()
            await finish.wait()
            return await original(url)

        context.new_tab = new_page
        pending = asyncio.create_task(bridge.dispatch('POST', PREFIX + '/leases/' + key + '/pages', {}))
        await entered.wait()
        try:
            await asyncio.wait_for(bridge.release(key), 0.2)
            assert key not in bridge.leases
        finally:
            finish.set()
            await asyncio.gather(pending, return_exceptions=True)
            await bridge.stop()
        async def closed():
            while len(context.pages) < 2 or not context.pages[-1].closed:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(closed(), 2)
        assert not context.pages[0].closed
    asyncio.run(run())


def test_event_registration_precedes_trigger_and_release_removes_listeners(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        lease, page = bridge.leases[key], context.pages[-1]
        chooser = SimpleNamespace(is_multiple=lambda: True)
        waiter = await bridge.call(lease, {'pageId': page_id, 'method': 'expectEvent', 'args': ['filechooser']})
        assert len(page.listeners['filechooser']) == 1
        page.emit('filechooser', chooser)
        result = await bridge.call(lease, {'pageId': page_id, 'handleId': waiter['value']['$handle'], 'method': 'result'})
        assert result['value']['type'] == 'filechooser' and result['value']['multiple']
        await bridge.call(lease, {'pageId': page_id, 'method': 'expectEvent', 'args': ['download']})
        await bridge.release(key)
        assert not page.listeners['download'] and not page.listeners['filechooser']
        assert page.closed and not context.pages[0].closed
    asyncio.run(run())


def test_timeout_and_browser_failures_are_sanitized(tmp_path):
    async def run():
        bridge, _, _ = harness(tmp_path)
        await bridge.start()
        try:
            key, page_id = await acquire(bridge)
            route = '/leases/' + key + '/call'
            status, result = await request(bridge, 'POST', route,
                                            {'pageId': page_id, 'method': 'evaluate', 'args': ['error']})
            assert status == 502 and 'secret' not in json.dumps(result)
            _, waiter = await request(bridge, 'POST', route, {'pageId': page_id, 'method': 'expectEvent',
                                                             'args': ['download', {'timeout': 1}]})
            status, result = await request(bridge, 'POST', route, {'pageId': page_id, 'method': 'result',
                                                                 'handleId': waiter['value']['$handle']})
            assert status == 408 and result['error']['name'] == 'TimeoutError'
            assert (await request(bridge, 'POST', route, {'pageId': page_id, 'method': '__getattribute__'}))[0] == 400
            assert (await request(bridge, 'POST', route, {'pageId': 'manual', 'method': 'close'}))[0] == 404
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_disconnect_releases_lease_and_cancels_pending_operation(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        await bridge.start()
        try:
            key, page_id = await acquire(bridge)
            host, port = bridge.base_url.removeprefix('http://').split(':')
            _, writer = await asyncio.open_connection(host, int(port))
            data = json.dumps({'pageId': page_id, 'method': 'evaluate', 'args': ['wait']}).encode()
            writer.write((f'POST {PREFIX}/leases/{key}/call HTTP/1.1\r\nHost: {host}:{port}\r\n'
                          f'Authorization: Bearer {bridge.token}\r\nContent-Type: application/json\r\n'
                          f'Content-Length: {len(data)}\r\n\r\n').encode() + data)
            await writer.drain()
            await asyncio.wait_for(context.pages[-1].started.wait(), 2)
            writer.close()
            await writer.wait_closed()
            async def released():
                while bridge.leases:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(released(), 2)
            assert context.pages[-1].closed and not context.pages[0].closed
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_cancelled_heartbeat_preserves_lease_until_normal_expiry(tmp_path):
    async def run():
        bridge, context, now = harness(tmp_path)
        entered, checked = asyncio.Event(), asyncio.Event()
        dispatch, release_route = bridge.dispatch, bridge._release_route
        async def delayed(method, route, body):
            if route.endswith('/heartbeat'):
                entered.set()
                await asyncio.Event().wait()
            return await dispatch(method, route, body)
        async def inspect_release(route, **kwargs):
            await release_route(route, **kwargs)
            checked.set()
        bridge.dispatch, bridge._release_route = delayed, inspect_release
        await bridge.start()
        try:
            key, _ = await acquire(bridge)
            host, port = bridge.base_url.removeprefix('http://').split(':')
            _, writer = await asyncio.open_connection(host, int(port))
            writer.write((f'POST {PREFIX}/leases/{key}/heartbeat HTTP/1.1\r\nHost: {host}:{port}\r\n'
                          f'Authorization: Bearer {bridge.token}\r\nContent-Type: application/json\r\n'
                          'Content-Length: 2\r\n\r\n{}').encode())
            await writer.drain()
            await asyncio.wait_for(entered.wait(), 2)
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(checked.wait(), 2)
            assert key in bridge.leases and not context.pages[-1].closed
            now[0] += bridge.lease_seconds + 1
            await bridge.expire()
            assert key not in bridge.leases and context.pages[-1].closed
            assert not context.pages[0].closed
        finally:
            await bridge.stop()
    asyncio.run(run())


def test_closed_browser_invalidates_lease(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, _ = await acquire(bridge)
        context.emit('close')
        await asyncio.sleep(0)
        assert bridge.leases[key].releasing
        with pytest.raises(AutomationError):
            bridge._lease(key)
        await bridge.stop()
    asyncio.run(run())


def test_manual_page_close_cleans_handles_without_closing_the_environment(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        lease = bridge.leases[key]
        waiter = await bridge.call(lease, {'pageId': page_id, 'method': 'expectEvent', 'args': ['download']})
        result = asyncio.create_task(bridge.call(lease, {'pageId': page_id,
                                    'handleId': waiter['value']['$handle'], 'method': 'result'}))
        await asyncio.sleep(0)
        await context.pages[-1].close()
        with pytest.raises(AutomationError) as error:
            await result
        assert error.value.code == 'page_closed'
        assert not lease.pages and not lease.handles
        assert not context.pages[0].closed
        assert bridge._lease(key) is lease
        with pytest.raises(AutomationError) as error:
            await bridge.call(lease, {'pageId': page_id, 'method': 'title'})
        assert error.value.code == 'page_closed'
        await bridge.stop()
    asyncio.run(run())


def test_evaluate_keeps_application_keys_and_uploads_reject_local_paths(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        data = {'waitUntil': 'unchanged', '$regex': 'ordinary data', '$locator': []}
        result = await bridge.call(bridge.leases[key], {'pageId': page_id, 'method': 'evaluate', 'args': ['x=>x', data]})
        assert result['value'] == data
        await bridge.stop()
    asyncio.run(run())
    for value in ('C:/private.txt', {'$file': {'name': '../private.txt', 'mimeType': 'text/plain', 'base64': ''}},
                  {'$file': {'name': 'x.txt', 'mimeType': 'text/plain', 'base64': '***'}}):
        with pytest.raises(AutomationError):
            files(value)
    assert files({'$file': {'name': 'x.txt', 'mimeType': 'text/plain', 'base64': 'aGk='}})[0]['buffer'] == b'hi'


def test_download_waits_for_completion_and_enforces_limit(tmp_path):
    async def run():
        bridge, context, _ = harness(tmp_path)
        key, page_id = await acquire(bridge)
        lease, page = bridge.leases[key], context.pages[-1]
        complete = asyncio.Event()
        path = tmp_path / 'download.bin'
        path.write_bytes(b'contents')

        class Download:
            suggested_filename = 'result.bin'

            async def path(self):
                await complete.wait()
                return path

            async def cancel(self):
                complete.set()

            async def delete(self):
                path.unlink(missing_ok=True)

        waiter = await bridge.call(lease, {'pageId': page_id, 'method': 'expectEvent', 'args': ['download']})
        page.emit('download', Download())
        result = await bridge.call(lease, {'handleId': waiter['value']['$handle'], 'method': 'result'})
        handle = result['value']['$handle']
        pending = asyncio.create_task(bridge.call(lease, {'handleId': handle, 'method': 'read', 'args': [{'maxBytes': 8}]}))
        await asyncio.sleep(0)
        assert not pending.done()
        complete.set()
        assert base64.b64decode((await pending)['value']['base64']) == b'contents'
        with pytest.raises(AutomationError, match='size limit'):
            await bridge.call(lease, {'handleId': handle, 'method': 'read', 'args': [{'maxBytes': 1}]})
        await bridge.release(key)
        assert not path.exists()
    asyncio.run(run())
