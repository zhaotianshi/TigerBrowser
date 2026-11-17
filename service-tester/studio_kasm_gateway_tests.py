"""Real Vue display, authorization and native input over KasmVNC."""

import asyncio
import os

import httpx
import psutil
import pytest
from playwright.async_api import async_playwright, expect

from camoufox.studio.team.kasm import prerequisites
from studio_runtime_tests import command, login, running_team, site_state, workspace


async def decoded_color(canvas, expected):
    async def painted():
        while True:
            color = await canvas.evaluate('''surface => {
                const canvas = surface;
                return Array.from(canvas.getContext('2d').getImageData(500,900,1,1).data);
            }''')
            if len(color) == 4 and all(abs(a - b) < 10 for a, b in zip(color, [*expected, 255])):
                return
            await asyncio.sleep(.05)
    await asyncio.wait_for(painted(), 10)


async def fitted_viewport(canvas):
    await canvas.evaluate('''async canvas => {
        const deadline = performance.now() + 3000;
        while (performance.now() < deadline) {
            const rect = canvas.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0 && rect.width <= innerWidth && rect.height <= innerHeight) return;
            await new Promise(requestAnimationFrame);
        }
        throw Error('Fit mode cropped the display instead of scaling it');
    }''')


@pytest.mark.asyncio
@pytest.mark.parametrize('workspace', [{'screen_mode': 'custom', 'screen_width': 1920,
                                      'screen_height': 1080}], indirect=True)
async def test_console_display_native_text_and_reconnect(workspace, tmp_path):
    prerequisites()
    store, _, _, profile = workspace
    selector = 'canvas[width="1920"][height="1080"]'
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            capabilities = (await owner.get('/team/v1/me')).json()['capabilities']
            assert capabilities['remote_display'] is True
            launched = await command(service, owner, csrf, profile['id'], 'start')
            assert service.repository.latest_run(profile['id'])['display_backend'] == 'kasmvnc'
            native = service.engine.sessions[profile['id']]
            page = native.context.pages[0]
            await page.goto(origin + '/synthetic-native')
            await page.evaluate("document.body.style.background = 'rgb(228,244,236)'")
            identity = store.get_profile(profile['id'])['identity']
            async with async_playwright() as playwright:
                browser = await playwright.firefox.launch(headless=True, env={
                    **os.environ, 'MOZ_HEADLESS_WIDTH': '1920', 'MOZ_HEADLESS_HEIGHT': '1400',
                })
                try:
                    context = await browser.new_context(viewport={'width': 1920, 'height': 1400},
                                                        screen={'width': 1920, 'height': 1400})
                    response = await context.request.post(origin + '/team/v1/login',
                        headers={'Origin': origin}, data={'name': 'root', 'password': 'synthetic-admin-password'})
                    assert response.ok
                    client = await context.new_page()
                    errors = []
                    client.on('pageerror', lambda error: errors.append(str(error)))
                    client.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    await client.goto(origin + '/#remote=' + profile['id'])
                    canvas = client.locator(selector).first
                    try:
                        await expect(canvas).to_be_visible(timeout=15000)
                    except AssertionError as error:
                        raise AssertionError(f'KasmVNC display failed: {errors}') from error

                    await decoded_color(canvas, [228,244,236])
                    await client.set_viewport_size({'width': 1280, 'height': 900})
                    await client.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                    scale = client.locator('.scale-select')
                    await scale.select_option('fit')
                    await fitted_viewport(canvas)
                    await decoded_color(canvas, [228,244,236])
                    await scale.select_option('actual')
                    await client.set_viewport_size({'width': 1920, 'height': 1400})
                    spectator = await context.new_page()
                    spectator.on('pageerror', lambda error: errors.append(str(error)))
                    await spectator.goto(origin + '/#remote=' + profile['id'])
                    other_canvas = spectator.locator(selector).first
                    await expect(other_canvas).to_be_visible(timeout=15000)
                    await decoded_color(other_canvas, [228,244,236])
                    await page.evaluate("document.body.style.background = 'rgb(196,220,248)'")
                    await asyncio.gather(decoded_color(canvas, [196,220,248]),
                                         decoded_color(other_canvas, [196,220,248]))
                    await client.bring_to_front()
                    claim = client.get_by_role('button', name='申请控制', exact=True)
                    await claim.click()
                    active = client.get_by_text('你正在控制', exact=False)
                    await expect(active.first).to_contain_text('你正在控制', timeout=5000)
                    box = await canvas.bounding_box()
                    await client.mouse.click(box['x'] + box['width'] * 150 / 1920,
                                             box['y'] + box['height'] * 1035 / 1080)
                    await expect(page.locator('#native')).to_be_focused()
                    text = '中文远程输入验证 Studio'
                    field = client.locator('textarea[placeholder^="粘贴链接"]')
                    await field.fill(text)
                    await client.get_by_role('button', name='发送', exact=True).click()
                    await expect(page.locator('#native')).to_have_value(text)

                    await client.get_by_role('button', name='文件交换', exact=True).click()
                    async with client.expect_response(lambda response:
                            response.request.method == 'POST' and
                            response.url.startswith(origin + '/team/v1/files/')) as upload:
                        await client.locator('input[type="file"]').set_input_files({
                            'name': '中文文件.txt', 'mimeType': 'text/plain', 'buffer': '中文内容'.encode(),
                        })
                    assert (await upload.value).ok
                    directory = store.profile_dir(profile['id'])
                    assert [path.read_bytes() for path in (directory / 'Uploads').iterdir()] == ['中文内容'.encode()]
                    await client.get_by_role('button', name='文件交换', exact=True).click()

                    await client.get_by_role('button', name='全屏', exact=True).click()
                    bounds = await client.locator('.remote-viewport').evaluate('''element => {
                        const rect = element.getBoundingClientRect();
                        return {fullscreen: document.fullscreenElement === element,
                            actual: [rect.x, rect.y, rect.width, rect.height],
                            expected: [0, 0, innerWidth, innerHeight]};
                    }''')
                    assert bounds['fullscreen'] and bounds['actual'] == bounds['expected']
                    assert await canvas.evaluate('canvas => [canvas.width, canvas.height]') == [1920, 1080]
                    display = service.remotes[launched['run_id']].native
                    assert (await display.command('xdotool', 'getdisplaygeometry', capture=True)).strip() == b'1920 1080'
                    await client.screenshot(path=str(tmp_path / 'vue-kasm-fullscreen.png'))
                    await client.evaluate('document.exitFullscreen()')
                    await client.locator('.toggle-btn').click()
                    await client.locator('.scale-select').select_option('fit')
                    for width, height in ((375, 812), (812, 375)):
                        await client.set_viewport_size({'width': width, 'height': height})
                        await expect(client.locator('.remote-viewport')).to_have_js_property('clientWidth', width)
                        await fitted_viewport(canvas)
                        assert (await canvas.bounding_box())['width'] <= width
                        tools = await client.locator('.tools-overlay').bounding_box()
                        assert 0 <= tools['x'] and tools['x'] + tools['width'] <= width
                        await client.screenshot(path=str(tmp_path / f'vue-kasm-{width}.png'))
                    assert store.get_profile(profile['id'])['identity'] == identity
                    await client.close()
                    async def disconnected():
                        while service.remotes[launched['run_id']].controller is not None:
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(disconnected(), 5)
                    assert not native.stop.is_set()
                    client = await context.new_page()
                    client.on('pageerror', lambda error: errors.append(str(error)))
                    await client.goto(origin + '/#remote=' + profile['id'])
                    await expect(client.locator(selector).first).to_be_visible(timeout=10000)
                    assert service.remotes[launched['run_id']].controller is None
                    assert not errors, errors
                    await context.close()
                finally:
                    await browser.close()
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('workspace', [{'screen_mode': 'custom', 'screen_width': 1920,
                                      'screen_height': 1080}], indirect=True)
async def test_kasm_restarts_preserve_identity_and_site_data(workspace):
    prerequisites()
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            initial = identity = None
            for _ in range(3):
                launched = await command(service, owner, csrf, profile['id'], 'start')
                native = service.engine.sessions[profile['id']]
                state = await site_state(native.context, origin + '/synthetic-site',
                                         create=initial is None)
                if initial is None:
                    initial = state
                    identity = store.get_profile(profile['id'])['identity']
                assert state == initial
                assert store.get_profile(profile['id'])['identity'] == identity
                assert service.repository.latest_run(profile['id'])['display_backend'] == 'kasmvnc'
                display = service.remotes[launched['run_id']].native.display
                processes = psutil.Process(display.proc.pid).children(recursive=True)
                assert any(process.name() == 'Xkasmvnc' for process in processes)
                await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])
                _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=5)
                assert not alive, [(process.pid, process.name(), process.status()) for process in alive]


@pytest.mark.asyncio
@pytest.mark.parametrize('client_browser', ['firefox', 'chromium'])
@pytest.mark.parametrize('workspace', [{'screen_mode': 'custom', 'screen_width': 1920,
                                      'screen_height': 1080}], indirect=True)
async def test_console_kasm_continuous_pointer_motion_keeps_both_channels_open(workspace, client_browser):
    prerequisites()
    store, _, _, profile = workspace
    async with running_team(store, browser_mode='remote') as (service, origin):
        async with httpx.AsyncClient(base_url=origin) as owner:
            csrf = await login(owner, 'root', 'synthetic-admin-password')
            launched = await command(service, owner, csrf, profile['id'], 'start')
            native = service.engine.sessions[profile['id']]
            page = native.context.pages[0]
            await page.goto(origin + '/synthetic-native')
            await page.evaluate('''() => {
                window.motionCount = 0;
                document.addEventListener('mousemove', event => {
                    window.motionCount++;
                    document.body.style.background = window.motionCount % 2 ? 'rgb(228,244,236)' : 'rgb(196,220,248)';
                    document.body.style.cursor = event.clientX < 800 ? 'text' : 'crosshair';
                });
            }''')
            async with async_playwright() as playwright:
                browser = await getattr(playwright, client_browser).launch(headless=True)
                try:
                    context = await browser.new_context(viewport={'width': 1920, 'height': 1400})
                    response = await context.request.post(origin + '/team/v1/login', headers={'Origin': origin},
                        data={'name': 'root', 'password': 'synthetic-admin-password'})
                    assert response.ok
                    client = await context.new_page()
                    errors, closed = [], []
                    client.on('pageerror', lambda error: errors.append(str(error)))
                    client.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    client.on('websocket', lambda socket: socket.on('close', lambda: closed.append(socket.url)))
                    await client.goto(origin + '/#remote=' + profile['id'])
                    canvas = client.locator('canvas[width="1920"][height="1080"]').first
                    await expect(canvas).to_be_visible(timeout=15000)
                    claim = client.get_by_role('button', name='申请控制', exact=True)
                    await claim.click()
                    active = client.get_by_text('你正在控制', exact=False).first
                    await expect(active).to_contain_text('你正在控制', timeout=5000)
                    box = await canvas.bounding_box()
                    deadline = asyncio.get_running_loop().time() + 60
                    step = 0
                    while asyncio.get_running_loop().time() < deadline:
                        await client.mouse.move(box['x'] + 300 + step % 20 * 60,
                                                box['y'] + 300 + step % 10 * 40, steps=3)
                        step += 1
                        assert not closed and not errors, {'closed': closed, 'errors': errors,
                            'engine_errors': [str(error) for error in service.validation_errors]}
                        await asyncio.sleep(.02)
                    await expect(active).to_contain_text('你正在控制')
                    assert await page.evaluate('window.motionCount') >= 100
                    # Both active and idle phases exceed the former 20s Ping + 20s timeout.
                    await asyncio.sleep(45)
                    assert not closed and not errors, {'closed': closed, 'errors': errors}
                    await page.evaluate("document.body.style.background = 'rgb(164,210,242)'")
                    await decoded_color(canvas, [164,210,242])
                    assert not native.stop.is_set()
                finally:
                    await browser.close()
            await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])
