"""Exercise the shipped Kasm decoder in both browsers with the service's strict CSP."""

import asyncio
import base64
import hashlib
import json
import socket
import struct
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, Response
from playwright.async_api import async_playwright, expect


WEB = Path(__file__).resolve().parents[1] / 'pythonlib/camoufox/studio/team/web'
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' blob:; font-src 'self'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)
HARNESS = """
import {createKasmDisplay} from '/kasm-client.js';
window.state = {json: [], closed: 0, violations: [], peers: 0};
document.addEventListener('securitypolicyviolation', e => state.violations.push(e.violatedDirective));
window.RTCPeerConnection = class { constructor() { state.peers++; throw Error('Unexpected WebRTC'); } };
const socket = new WebSocket(`ws://${location.host}/display`);
socket.onclose = () => state.closed++;
socket.onmessage = async event => {
    if (typeof event.data !== 'string') return;
    const message = JSON.parse(event.data);
    state.json.push(message);
    if (message.ready) {
        window.display = await createKasmDisplay(document.querySelector('#display'), socket);
        const image = document.createElement('canvas');
        image.width = 32;
        image.height = 16;
        const context = image.getContext('2d');
        context.fillStyle = '#e02040'; context.fillRect(0, 0, 32, 16);
        const jpeg = image.toDataURL('image/jpeg');
        context.fillStyle = '#2040e0'; context.fillRect(0, 0, 32, 16);
        const webp = image.toDataURL('image/webp');
        socket.send(JSON.stringify({ready: true, jpeg, webp}));
    }
};
window.acknowledge = name => socket.send(JSON.stringify({ack: name}));
window.stopDisplay = () => { display.disconnect(); display.disconnect(); };
"""


def _compact_length(length):
    result = bytearray()
    for index in range(3):
        part = length & 0x7f
        length >>= 7
        result.append(part | (0x80 if length and index < 2 else 0))
        if not length:
            return bytes(result)
    raise AssertionError('Test image too large')


def _image_frame(data_url, control):
    data = base64.b64decode(data_url.partition(',')[2], validate=True)
    return (
        struct.pack('>BBH', 0, 0, 2)
        + struct.pack('>HHHHi', 0, 0, 32, 16, 7)
        + bytes([control]) + _compact_length(len(data)) + data
        + struct.pack('>HHHHi', 0, 0, 0, 0, -224)
    )


def test_shipped_kasm_client_provenance():
    metadata = json.loads((WEB / 'kasm-client/PROVENANCE.json').read_text())
    assert metadata['client']['revision'] == '475ecfa5356579ef222983c7ce4619a7576a3bce'
    assert metadata['kasmvnc']['version'] == '1.5.0'
    assert 'app/ui.js' not in metadata['sources']
    assert not any('output/' in name or '/qoi/' in name for name in metadata['sources'])
    for name, digest in metadata['assets'].items():
        assert hashlib.sha256((WEB / 'kasm-client' / name).read_bytes()).hexdigest() == digest
    source_map = json.loads((WEB / 'kasm-client/rfb.js.map').read_text())
    assert len(source_map['sources']) == len(source_map['sourcesContent'])
    assert all('Users/' not in name and 'Users\\' not in name for name in source_map['sources'])
    assert any('export default class RFB' in contents for contents in source_map['sourcesContent'])


@pytest.mark.asyncio
@pytest.mark.parametrize('client_browser', ['firefox', 'chromium'])
async def test_kasm_client_decodes_jpeg_webp_with_strict_csp_and_preserves_control_events(client_browser):
    app = FastAPI()
    completed = asyncio.get_running_loop().create_future()
    received = []

    @app.middleware('http')
    async def policy(request, call_next):
        response = await call_next(request)
        response.headers['Content-Security-Policy'] = CSP
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/')
    async def index():
        return Response(
            '<!doctype html><html><head><link rel="stylesheet" href="/test.css"></head>'
            '<body><div id="display"></div><script type="module" src="/test.js"></script></body></html>',
            media_type='text/html',
        )

    @app.get('/test.css')
    async def stylesheet():
        return Response('#display {width:320px;height:160px}', media_type='text/css')

    @app.get('/test.js')
    async def javascript():
        return Response(HARNESS, media_type='text/javascript')

    @app.get('/kasm-client.js')
    async def adapter():
        return FileResponse(WEB / 'kasm-client.js', media_type='text/javascript')

    @app.get('/kasm-client/rfb.js')
    async def decoder():
        return FileResponse(WEB / 'kasm-client/rfb.js', media_type='text/javascript')

    @app.websocket('/display')
    async def display(websocket: WebSocket):
        try:
            await websocket.accept()
            await websocket.send_json({'ready': True})
            ready = await websocket.receive_json()
            assert ready['ready'] is True
            await websocket.send_bytes(b'RFB 003.008\n')
            assert await websocket.receive_bytes() == b'RFB 003.008\n'
            await websocket.send_bytes(b'\x01\x01')
            assert await websocket.receive_bytes() == b'\x01'
            await websocket.send_bytes(bytes(4))
            assert await websocket.receive_bytes() == b'\x01'
            name = b'Studio decoder test'
            await websocket.send_bytes(
                struct.pack('>HHBBBBHHHBBB3xI', 32, 16, 32, 24, 0, 1, 255, 255, 255, 0, 8, 16, len(name)) + name
            )
            while True:
                packet = await websocket.receive_bytes()
                received.append(packet)
                if packet[0] == 3:
                    break
            await websocket.send_bytes(
                struct.pack('>BBHHHHHi', 0, 0, 1, 0, 0, 2, 2, -239)
                + bytes([255, 255, 255, 0]) * 4 + bytes([0xc0, 0xc0])
            )
            for kind, control in [('jpeg', 0x90), ('webp', 0xb0)]:
                await websocket.send_bytes(_image_frame(ready[kind], control))
                await websocket.send_json({'frame': kind})
                while True:
                    event = await websocket.receive()
                    if event.get('text') is not None:
                        assert json.loads(event['text']) == {'ack': kind}
                        break
                    received.append(event['bytes'])
            while True:
                event = await websocket.receive()
                if event['type'] == 'websocket.disconnect':
                    break
                received.append(event['bytes'])
            completed.set_result(None)
        except BaseException as error:
            if not completed.done():
                completed.set_exception(error)

    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off', ws='websockets-sansio'))
    running = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with async_playwright() as playwright:
            browser = await getattr(playwright, client_browser).launch()
            try:
                page = await browser.new_page()
                await page.add_init_script('''
                    window.imageFrames = {decoded: 0, closed: 0};
                    const track = image => {
                        imageFrames.decoded++;
                        const close = image.close;
                        let closed = false;
                        image.close = function(...args) {
                            if (!closed) { imageFrames.closed++; closed = true; }
                            return close.apply(this, args);
                        };
                        return image;
                    };
                    const bitmap = window.createImageBitmap;
                    window.createImageBitmap = async (...args) => track(await bitmap(...args));
                    if (typeof ImageDecoder !== 'undefined') {
                        const decode = ImageDecoder.prototype.decode;
                        ImageDecoder.prototype.decode = async function(...args) {
                            const result = await decode.apply(this, args);
                            track(result.image);
                            return result;
                        };
                    }
                ''')
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}/')
                canvas = page.locator('#display canvas')
                await expect(canvas).to_have_js_property('width', 32)
                await expect(canvas).to_have_js_property('height', 16)
                await canvas.hover()
                for kind, color in [('jpeg', [224, 32, 64]), ('webp', [32, 64, 224])]:
                    await canvas.evaluate('''async (canvas, wanted) => {
                        const end = performance.now() + 5000;
                        while (performance.now() < end) {
                            const pixel = canvas.getContext('2d').getImageData(16, 8, 1, 1).data;
                            if (wanted.every((value, index) => Math.abs(value - pixel[index]) <= 4)) return;
                            await new Promise(requestAnimationFrame);
                        }
                        throw Error('Decoded frame did not reach the canvas');
                    }''', color)
                    await canvas.dispatch_event('mousedown', {'button': 0, 'buttons': 1})
                    await canvas.dispatch_event('mousemove', {'clientX': 20, 'clientY': 20})
                    await canvas.dispatch_event('wheel', {'deltaY': 50})
                    await page.keyboard.press('A')
                    await page.set_viewport_size({'width': 900, 'height': 600})
                    await page.evaluate('(kind) => acknowledge(kind)', kind)
                assert await canvas.evaluate('canvas => [canvas.width, canvas.height]') == [32, 16]
                assert await page.evaluate('state.peers') == 0
                assert await page.evaluate('state.violations') == []
                assert await page.evaluate('state.json') == [{'ready': True}, {'frame': 'jpeg'}, {'frame': 'webp'}]
                await page.evaluate('stopDisplay()')
                await expect(canvas).to_have_count(0)
                await asyncio.wait_for(completed, 5)
                assert await page.evaluate('state.closed') == 1
                frames = await page.evaluate('imageFrames')
                assert frames['decoded'] > 0
                assert frames['closed'] == frames['decoded'], frames
                assert errors == []
                assert all(packet[0] in {0, 2, 3, 184, 185} for packet in received)
                encodings = next(packet for packet in received if packet[0] == 2)
                values = struct.unpack(f'>{(len(encodings) - 4) // 4}i', encodings[4:])
                assert -1886 not in values  # QOI is not negotiated.
                assert await page.evaluate('''async () => {
                    const {createKasmDisplay} = await import('/kasm-client.js');
                    class ClosingSocket extends EventTarget {
                        constructor() {
                            super(); this.readyState = WebSocket.OPEN; this.protocol = '';
                            this.binaryType = 'arraybuffer'; this.onopen = null;
                            this.onclose = null; this.onmessage = null; this.onerror = null;
                        }
                        send() { throw Error('No RFB greeting was released'); }
                        close() {
                            if (this.readyState === WebSocket.CLOSED) return;
                            this.readyState = WebSocket.CLOSED;
                            this.dispatchEvent(new CloseEvent('close'));
                        }
                    }
                    const target = document.createElement('div');
                    const existing = document.createElement('span');
                    target.appendChild(existing); document.body.appendChild(target);
                    const closing = new ClosingSocket();
                    const opening = createKasmDisplay(target, closing);
                    closing.close();
                    let rejected = false;
                    try { await opening; } catch (error) {
                        rejected = error.message.includes('closed during initialization');
                    }
                    const cleaned = target.children.length === 1 && target.firstChild === existing;
                    target.remove();
                    return rejected && cleaned && document.querySelectorAll('canvas').length === 0;
                }''')
            finally:
                await browser.close()
    finally:
        server.should_exit = True
        await running
        listener.close()
