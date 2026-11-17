"""Opt-in Linux native remote checks using the actual isolated browser and display."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
import sys

import httpx
import pytest
from playwright.async_api import Error, async_playwright, expect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from camoufox.studio.access import MemberStore
from camoufox.studio.team.control import ControlError
from studio_runtime_tests import command, login, running_team, site_state, workspace

# Constrain the real generator to a recorded screen within this backend's
# capacity; unrelated random high-resolution devices are not this test's input.
pytestmark = pytest.mark.parametrize('workspace', [{
    'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080,
}], indirect=True)


async def window_bounds(native):
    geometry = await native.command('xdotool', 'search', '--onlyvisible',
        '--limit', '1', '--classname', '^Navigator$', 'getwindowgeometry', '--shell',
        capture=True)
    return dict(line.split('=') for line in geometry.decode('ascii').splitlines())


@pytest.mark.asyncio
async def test_remote_entry_tracks_real_run_without_acquiring_control(workspace):
    store, admin, member, profile = workspace
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'watch', 'operate'])
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner, httpx.AsyncClient(base_url=origin) as viewer:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            viewer_csrf = await login(viewer, 'operator', 'synthetic-member-password')
            path = '/team/v1/profiles/' + profile['id'] + '/remote'
            assert (await viewer.get(path)).json()['reason'] == 'not_running'
            launched = await command(service, owner, csrf, profile['id'], 'start')
            run = service.remotes[launched['run_id']]
            identity = store.get_profile(profile['id'])['identity']
            before = (run.controller, run.generation, run.sequence, run.deadline)
            response = await viewer.get(path)
            assert response.status_code == 200, response.text
            entry = response.json()
            assert entry['available'] is True and entry['reason'] is None
            assert entry['profile']['run']['id'] == launched['run_id']
            assert entry['viewer_path'] == '/#remote=' + profile['id']
            assert entry['control_path'] == '/team/v1/remote/' + launched['run_id'] + '/control'
            assert (run.controller, run.generation, run.sequence, run.deadline) == before
            assert store.get_profile(profile['id'])['identity'] == identity
            assert not service.remote_connections
            cookies = '; '.join(f'{key}={value}' for key, value in viewer.cookies.items())
            async with connect(origin.replace('http://', 'ws://', 1) + entry['control_path'],
                origin=origin, additional_headers={'Cookie': cookies},
                compression=None, max_size=8 * 1024 * 1024) as watching:
                await watching.send(json.dumps({'csrf': viewer_csrf}))
                status = await remote_status(watching, lambda value: value.get('run_id') == run.run_id)
                assert status['mine'] is False and status['controller'] is None
                async with paired_display(origin, cookies, viewer_csrf, run.run_id, status['viewer_id']) as display:
                    assert await asyncio.wait_for(display.recv(), 5) == b'RFB 003.008\n'
            assert (run.controller, run.generation, run.sequence, run.deadline) == before
            assert not service.engine.sessions[profile['id']].stop.is_set()
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])
            ended = (await viewer.get(path)).json()
            assert ended['available'] is False and ended['reason'] == 'not_running'
            assert ended['control_path'] is None


@pytest.mark.asyncio
async def test_last_native_tab_close_allows_restart_with_connected_viewer(workspace):
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            started = await command(service, owner, csrf, profile['id'], 'start')
            run = service.remotes[started['run_id']]
            session = service.engine.sessions[profile['id']]
            identity = store.get_profile(profile['id'])['identity']
            initial = await site_state(session.context, origin + '/synthetic-site', create=True)
            assert len(session.context.pages) == 1
            worker = run.native.input_worker
            native_root, display_root = run.native.root, run.native.display.private
            cookies = '; '.join(f'{key}={value}' for key, value in owner.cookies.items())
            control_path = origin.replace('http://', 'ws://', 1) + '/team/v1/remote/' + run.run_id + '/control'
            async with connect(control_path, origin=origin, additional_headers={'Cookie': cookies},
                               proxy=None, compression=None) as control:
                await control.send(json.dumps({'csrf': csrf}))
                status = await remote_status(control, lambda value: 'viewer_id' in value)
                async with paired_display(origin, cookies, csrf, run.run_id, status['viewer_id']) as display:
                    assert await asyncio.wait_for(display.recv(), 5) == b'RFB 003.008\n'
                    await control.send(json.dumps({
                        'kind': 'claim', 'generation': status['generation'], 'force': False}))
                    await remote_status(control, lambda value: value.get('mine') is True)
                    # Close the browser's final native tab, not the client viewer window.
                    await run.native.command('xdotool', 'key', '--clearmodifiers', 'ctrl+w')
                    await asyncio.wait_for(asyncio.shield(session.task), 20)
                    await asyncio.gather(*tuple(service.monitors))
                    await run.disconnect(status['viewer_id'])
            assert worker.returncode == 0
            assert run.native.display.proc is None
            assert not native_root.exists() and not display_root.exists()
            assert not service.failed and not service.engine.recovery_required
            assert service.repository.latest_run(profile['id'])['state'] == 'stopped'
            health = (await owner.get('/team/v1/health')).json()
            assert health['active_runs'] == 0 and health['recovery_required'] is False
            restarted = await command(service, owner, csrf, profile['id'], 'start')
            assert restarted['run_id'] != started['run_id']
            context = service.engine.sessions[profile['id']].context
            assert await site_state(context, origin + '/synthetic-site') == initial
            assert store.get_profile(profile['id'])['identity'] == identity
            await command(service, owner, csrf, profile['id'], 'stop', restarted['run_id'])
    assert not (store.root / 'workspace.lock').exists()


@pytest.mark.asyncio
async def test_native_attach_fills_saved_display_after_a_small_window(workspace):
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launched = await command(service, owner, csrf, profile['id'], 'start')
            native = service.remotes[launched['run_id']].native
            identity = store.get_profile(profile['id'])['identity']
            # Reproduce a persisted, narrow browser window on a larger private display.
            await native.command('xdotool', 'search', '--onlyvisible', '--limit', '1',
                                 '--classname', '^Navigator$', 'windowsize', '--sync', '640', '600',
                                 'windowmove', '--sync', '20', '30')
            assert int((await window_bounds(native))['WIDTH']) == 640
            await native.attach()
            bounds = await window_bounds(native)
            assert [int(bounds[key]) for key in ('X', 'Y', 'WIDTH', 'HEIGHT')] == [
                0, 0, native.width, native.height]
            assert store.get_profile(profile['id'])['identity'] == identity
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])


@pytest.mark.asyncio
async def test_native_input_reuses_worker_and_confirms_release(workspace):
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launched = await command(service, owner, csrf, profile['id'], 'start')
            run = service.remotes[launched['run_id']]
            native, worker = run.native, run.native.input_worker
            access = MemberStore(store, store.login('root', 'synthetic-admin-password'))
            status = await run.handle(access, 'controller', {'kind': 'claim', 'generation': 0, 'force': False})
            events = [
                *[{'kind': 'pointer', 'x': x, 'y': native.height - 40, 'button': 0, 'down': False}
                  for x in range(50, 150)],
                {'kind': 'key', 'code': 'ControlLeft', 'down': True},
                {'kind': 'pointer', 'x': 149, 'y': native.height - 40, 'button': 1, 'down': True},
            ]
            for sequence, event in enumerate(events, 1):
                await run.handle(access, 'controller', {'kind': 'input', 'generation': status['generation'],
                    'sequence': sequence, 'event': event})
            assert native.input_worker is worker and worker.returncode is None
            probe = ('import json; from Xlib import X, display; d = display.Display(); '
                     'p = d.screen().root.query_pointer(); '
                     'print(json.dumps([p.root_x, p.root_y, bool(p.mask & X.ControlMask), '
                     'bool(p.mask & X.Button1Mask), any(d.query_keymap())])); d.close()')

            async def state():
                return json.loads(await native.command(sys.executable, '-c', probe, capture=True))

            assert await state() == [149, native.height - 40, True, True, True]
            await run.disconnect('controller')
            assert await state() == [149, native.height - 40, False, False, False]
            assert native.input_worker is worker and worker.returncode is None
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])
            assert worker.returncode == 0


@pytest.mark.asyncio
async def test_native_display_input_handover_and_filesystem_isolation(workspace, tmp_path):
    if os.name != 'posix':
        pytest.fail('Native execution validation requires Linux, not a client-only Windows check.')
    store, admin, member, profile = workspace
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'watch', 'operate'])
    sentinel = tmp_path / 'host-private.txt'
    sentinel.write_text('host data must not be readable in the browser', encoding='utf-8')
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as client:
            csrf = await login(client, 'root', 'synthetic-admin-password')
            launched = await command(service, client, csrf, profile['id'], 'start')
            run = service.remotes[launched['run_id']]
            session = service.engine.sessions[profile['id']]
            page = session.context.pages[0]
            await page.goto(origin + '/synthetic-native')
            first = MemberStore(store, store.login('operator', 'synthetic-member-password'))
            second = MemberStore(store, store.login('root', 'synthetic-admin-password'))
            status = await run.handle(first, 'first', {'kind': 'claim', 'generation': 0, 'force': False})
            sequence = 0
            # Native coordinates come from the mapped X11 window, not spoofed JS geometry.
            bounds = await window_bounds(run.native)
            for down in (True, False):
                sequence += 1
                await run.handle(first, 'first', {'kind': 'input', 'generation': status['generation'],
                    'sequence': sequence, 'event': {'kind': 'pointer', 'x': 50,
                        'y': int(bounds['Y']) + int(bounds['HEIGHT']) - 45, 'button': 1, 'down': down}})

            async def key(code, down):
                nonlocal sequence
                sequence += 1
                await run.handle(first, 'first', {'kind': 'input', 'generation': status['generation'],
                    'sequence': sequence, 'event': {'kind': 'key', 'code': code, 'down': down}})

            await key('KeyA', True)
            await key('KeyA', False)
            await expect(page.locator('#native')).to_have_value('a')
            await key('ShiftLeft', True)
            replacement = await run.handle(second, 'second', {
                'kind': 'claim', 'generation': status['generation'], 'force': True})
            with pytest.raises(ControlError):
                await key('KeyB', True)
            await run.handle(second, 'second', {'kind': 'input', 'generation': replacement['generation'],
                'sequence': 1, 'event': {'kind': 'key', 'code': 'KeyB', 'down': True}})
            await run.handle(second, 'second', {'kind': 'input', 'generation': replacement['generation'],
                'sequence': 2, 'event': {'kind': 'key', 'code': 'KeyB', 'down': False}})
            await expect(page.locator('#native')).to_have_value('ab')
            identity = store.get_profile(profile['id'])['identity']
            with pytest.raises(Error, match='NS_ERROR_FILE_NOT_FOUND'):
                await page.goto(sentinel.as_uri())
            assert store.get_profile(profile['id'])['identity'] == identity
            await command(service, client, csrf, profile['id'], 'stop', launched['run_id'])
            assert run.native.display.proc is None


@asynccontextmanager
async def paired_display(origin, cookies, csrf, run_id, viewer_id):
    path = origin.replace('http://', 'ws://', 1) + '/team/v1/remote/' + run_id + '/display'
    async with connect(path, origin=origin, additional_headers={'Cookie': cookies},
                       proxy=None, compression=None, max_size=64 * 1024 * 1024) as display:
        await display.send(json.dumps({'csrf': csrf, 'viewer_id': viewer_id}))
        assert json.loads(await display.recv()) == {'ready': True}
        await display.send(json.dumps({'ready': True}))
        yield display


async def remote_status(socket, predicate):
    async def receive():
        while True:
            message = await socket.recv()
            assert isinstance(message, str)
            status = json.loads(message)
            if predicate(status):
                return status
    return await asyncio.wait_for(receive(), 6)


@pytest.mark.asyncio
async def test_native_long_text_and_explicit_clipboard_export(workspace):
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launched = await command(service, owner, csrf, profile['id'], 'start')
            run = service.remotes[launched['run_id']]
            page = service.engine.sessions[profile['id']].context.pages[0]
            await page.goto(origin + '/synthetic-native')
            cookies = '; '.join(f'{key}={value}' for key, value in owner.cookies.items())
            async with connect(origin.replace('http://', 'ws://', 1) + '/team/v1/remote/' + run.run_id + '/control',
                origin=origin, additional_headers={'Cookie': cookies},
                compression=None, max_size=8 * 1024 * 1024) as socket:
                await socket.send(json.dumps({'csrf': csrf}))
                status = await remote_status(socket, lambda value: 'generation' in value)
                async with paired_display(origin, cookies, csrf, run.run_id, status['viewer_id']):
                    await socket.send(json.dumps({'kind': 'claim', 'generation': status['generation'], 'force': False}))
                    status = await remote_status(socket, lambda value: value.get('mine'))
                    sequence = 0
                    async def dispatch(event):
                        nonlocal sequence
                        sequence += 1
                        await socket.send(json.dumps({'kind': 'input', 'generation': status['generation'],
                                                       'sequence': sequence, 'event': event}, ensure_ascii=False))
                    for down in (True, False):
                        await dispatch({'kind': 'pointer', 'x': 50, 'y': run.native.height - 40,
                                        'button': 1, 'down': down})
                    text = '  https://example.com/?q=' + '中文😀' * 10000 + '  '
                    await dispatch({'kind': 'text', 'text': text})
                    field = page.locator('#native')
                    await expect(field).to_have_value(text)
                    for code, down in [('ControlLeft', True), ('KeyA', True), ('KeyA', False), ('ControlLeft', False)]:
                        await dispatch({'kind': 'key', 'code': code, 'down': down})
                    await expect(field).to_have_js_property('selectionStart', 0)
                    await expect(field).to_have_js_property('selectionEnd', len(text.encode('utf-16-le')) // 2)
                    for request_id, source in enumerate(('primary', 'clipboard'), 1):
                        await socket.send(json.dumps({'kind': 'clipboard', 'generation': status['generation'],
                                                       'source': source, 'request_id': request_id}))
                        result = await remote_status(socket, lambda value: value.get('clipboard_request') == request_id)
                        assert result.get('clipboard') == text
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])


@pytest.mark.asyncio
async def test_remote_websocket_authentication_revocation_and_file_gateway(workspace, tmp_path):
    store, admin, member, profile = workspace
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'watch', 'operate'])
    viewer = store.create_member(admin['id'], 'reader', 'Reader', 'viewer', 'synthetic-viewer-password')
    store.grant_profile(admin['id'], viewer['id'], profile['id'], ['view', 'watch'])
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with (httpx.AsyncClient(base_url=origin) as owner,
                    httpx.AsyncClient(base_url=origin) as operator,
                    httpx.AsyncClient(base_url=origin) as reader):
            owner_csrf = await login(owner, 'root', 'synthetic-admin-password')
            operator_csrf = await login(operator, 'operator', 'synthetic-member-password')
            reader_csrf = await login(reader, 'reader', 'synthetic-viewer-password')
            launched = await command(service, owner, owner_csrf, profile['id'], 'start')
            run = service.remotes[launched['run_id']]
            endpoint = origin.replace('http:', 'ws:') + '/team/v1/remote/' + run.run_id + '/control'

            def connection(client):
                return connect(endpoint, origin=origin, additional_headers={
                    'Cookie': 'camoufox-team-local=' + client.cookies['camoufox-team-local']},
                    proxy=None, compression=None, max_size=8 * 1024 * 1024)

            async with connection(operator), connection(operator):
                with pytest.raises(InvalidStatus) as limited:
                    async with connection(operator):
                        pytest.fail('Unauthenticated handshakes bypassed the per-session connection capacity.')
                assert limited.value.response.status_code == 403

            async with connection(operator) as invalid:
                await invalid.send(json.dumps({'csrf': 'incorrect'}))
                with pytest.raises(ConnectionClosedError) as rejected:
                    await invalid.recv()
                assert rejected.value.rcvd.code == 4403
            async with connection(operator) as controller, connection(reader) as watching:
                await controller.send(json.dumps({'csrf': operator_csrf}))
                await watching.send(json.dumps({'csrf': reader_csrf}))
                status = await remote_status(controller, lambda value: 'generation' in value)
                cookies = 'camoufox-team-local=' + operator.cookies['camoufox-team-local']
                async with paired_display(origin, cookies, operator_csrf, run.run_id, status['viewer_id']):
                    await controller.send(json.dumps({'kind': 'claim', 'generation': status['generation'], 'force': False}))
                    controlled = await remote_status(controller, lambda value: value.get('mine'))
                    await controller.send(json.dumps({'kind': 'input', 'generation': controlled['generation'],
                        'sequence': 1, 'event': {'kind': 'key', 'code': 'Lang1', 'down': True}}))
                    rejected = await remote_status(controller, lambda value: 'error' in value)
                    assert rejected['error'] == 'invalid_input'
                    assert (await remote_status(controller, lambda value: value.get('mine')))['generation'] == controlled['generation']
                    await remote_status(watching, lambda value: value.get('controller') == member['id'])
                    files_url = '/team/v1/files/' + run.run_id
                    headers = {'Origin': origin, 'X-CSRF-Token': operator_csrf,
                               'Content-Type': 'application/octet-stream'}
                    response = await operator.post(files_url + '/中文.txt', headers=headers, content='中文文件'.encode())
                    assert response.status_code == 200, response.text
                    uploaded = response.json()['name']
                    directory = store.profile_dir(profile['id'])
                    assert (directory / 'Uploads' / uploaded).read_bytes() == '中文文件'.encode()
                    denied = await reader.get(files_url)
                    assert denied.status_code == 403
                    (directory / 'Downloads' / '中文下载.txt').write_bytes('中文下载'.encode())
                    assert (await operator.get(files_url)).json()['files'] == [{'name': '中文下载.txt', 'size': 12}]
                    response = await operator.get(files_url + '/中文下载.txt')
                    assert response.status_code == 200 and response.content == '中文下载'.encode()
                    sentinel = tmp_path / 'not-shared.txt'
                    sentinel.write_bytes(b'not shared')
                    (directory / 'Downloads' / 'linked.txt').symlink_to(sentinel)
                    assert (await operator.get(files_url + '/linked.txt')).json() == {'error': 'unsafe_file'}
                    assert (await operator.get(files_url + '/missing.txt')).status_code == 404

                    store.revoke_profile(admin['id'], member['id'], profile['id'])
                    await service.revoke_connections()
                    assert run.controller is None
                    assert controlled['generation'] < run.generation
                    with pytest.raises(ConnectionClosedError) as revoked:
                        await remote_status(controller, lambda value: False)
                    assert revoked.value.rcvd.code == 4401
                    expired = await operator.get(files_url)
                    assert expired.status_code == 401 and expired.json() == {'error': 'session_expired'}
                    assert not service.engine.sessions[profile['id']].stop.is_set()
                    assert not (await remote_status(watching, lambda value: value.get('controller') is None))['mine']
            await command(service, owner, owner_csrf, profile['id'], 'stop', launched['run_id'])


@pytest.mark.asyncio
async def test_native_bundle_resolves_declared_chinese_font_and_covers_han_glyphs(workspace):
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launched = await command(service, owner, csrf, profile['id'], 'start')
            run = service.remotes[launched['run_id']]
            identity = store.get_profile(profile['id'])['identity']
            assert 'Noto Sans CJK SC' in identity['config']['fonts']
            environment = {**run.native.env, 'FONTCONFIG_FILE': run.native.options['env']['FONTCONFIG_FILE']}
            process = await asyncio.create_subprocess_exec(
                'fc-match', '-f', '%{family}\n%{charset}', 'Noto Sans CJK SC',
                env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, error = await asyncio.wait_for(process.communicate(), 10)
            assert process.returncode == 0, error.decode()
            family, charset = output.decode().split('\n', 1)
            assert 'Noto Sans CJK SC' in family
            ranges = [tuple(int(value, 16) for value in interval.split('-')) for interval in charset.split()]
            for character in '中文显示':
                assert any(start <= ord(character) <= (end[0] if end else start)
                           for start, *end in ranges), character
            assert store.get_profile(profile['id'])['identity'] == identity
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_worker', ['display', 'input'])
async def test_two_native_environments_isolate_clipboards_files_and_worker_failure(workspace, failed_worker):
    store, _, _, first = workspace
    second = store.save_profile({'name': 'Independent native environment', 'os': 'linux',
                                'fingerprint': first['fingerprint']})
    access = MemberStore(store, store.login('root', 'synthetic-admin-password'))
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            runs, sessions = [], []
            for profile, text in ((first, '甲'), (second, '乙')):
                started = await command(service, owner, csrf, profile['id'], 'start')
                run = service.remotes[started['run_id']]
                session = service.engine.sessions[profile['id']]
                page = session.context.pages[0]
                await page.goto(origin + '/synthetic-native')
                bounds = await window_bounds(run.native)
                await run.native.input({'kind': 'pointer', 'x': 50,
                    'y': int(bounds['Y']) + int(bounds['HEIGHT']) - 45, 'button': 1, 'down': True})
                await run.native.input({'kind': 'pointer', 'x': 50,
                    'y': int(bounds['Y']) + int(bounds['HEIGHT']) - 45, 'button': 1, 'down': False})
                status = await run.handle(access, profile['id'], {'kind': 'claim', 'generation': 0, 'force': False})
                await run.handle(access, profile['id'], {'kind': 'input', 'generation': status['generation'],
                    'sequence': 1, 'event': {'kind': 'text', 'text': text}})
                await expect(page.locator('#native')).to_have_value(text)
                runs.append(run)
                sessions.append(session)
            assert runs[0].native.env['DISPLAY'] != runs[1].native.env['DISPLAY']
            for sequence, (code, down) in enumerate((('ControlLeft', True), ('KeyV', True),
                                                    ('KeyV', False), ('ControlLeft', False)), start=2):
                await runs[0].handle(access, first['id'], {'kind': 'input', 'generation': runs[0].generation,
                    'sequence': sequence, 'event': {'kind': 'key', 'code': code, 'down': down}})
            await expect(sessions[0].context.pages[0].locator('#native')).to_have_value('甲甲')
            await expect(sessions[1].context.pages[0].locator('#native')).to_have_value('乙')
            wrong_env = {**runs[0].native.env, 'DISPLAY': runs[1].native.env['DISPLAY']}
            process = await asyncio.create_subprocess_exec(
                *runs[0].native.worker_arguments(['xdotool', 'getwindowfocus']), env=wrong_env,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            assert await asyncio.wait_for(process.wait(), 5) != 0
            secret = store.profile_dir(second['id']) / 'Uploads/isolated.txt'
            secret.write_bytes(b'private to second environment')
            with pytest.raises(Error, match='NS_ERROR_FILE_NOT_FOUND'):
                await sessions[0].context.pages[0].goto(secret.as_uri())
            if failed_worker == 'display':
                runs[0].native.display.proc.kill()
            else:
                runs[0].native.input_worker.kill()
            await asyncio.wait_for(asyncio.shield(sessions[0].task), 15)
            assert first['id'] not in service.engine.sessions
            assert runs[0].native.root is None
            assert not sessions[1].stop.is_set()
            assert runs[1].native.display.proc.poll() is None
            await expect(sessions[1].context.pages[0].locator('#native')).to_have_value('乙')
            await command(service, owner, csrf, second['id'], 'stop', runs[1].run_id)
