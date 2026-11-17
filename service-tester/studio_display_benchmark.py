"""Measure painted updates and display traffic on a temporary local Studio workspace."""

import argparse
import asyncio
import json
import os
import platform
import sys
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, expect

from camoufox.studio.store import ProfileStore
from camoufox.studio.team.boundary import MARKER, _supervise
from camoufox.studio.team.kasm import prerequisites
from studio_runtime_tests import command, login, running_team


async def measure(surface, seconds):
    return await surface.evaluate('''async (surface, seconds) => {
        const context = surface.getContext('2d', {willReadFrequently: true});
        const started = performance.now();
        let previous = '', changes = 0, last = started;
        const intervals = [];
        await new Promise(resolve => {
            function sample(now) {
                const pixel = Array.from(context.getImageData(500,900,1,1).data).join(',');
                if (previous && pixel !== previous) { changes++; intervals.push(now-last); last=now; }
                previous = pixel;
                if (now-started < seconds*1000) requestAnimationFrame(sample); else resolve();
            }
            requestAnimationFrame(sample);
        });
        intervals.sort((a,b) => a-b);
        return {painted_updates: changes, seconds: (performance.now()-started)/1000,
                interval_p95_ms: intervals.length ? intervals[Math.ceil(intervals.length*.95)-1] : null};
    }''', seconds)


async def benchmark(seconds):
    prerequisites()
    binary = Path(os.environ['CAMOUFOX_STUDIO_BINARY']).resolve(strict=True)
    dist = Path(__file__).resolve().parents[1] / 'pythonlib/camoufox/studio/team/web'
    if not (dist / 'index.html').is_file():
        raise ValueError('Build frontend/studio before measuring its real client.')
    results = {'scope': 'WSL/local loopback when run in WSL; no physical-network certification',
               'system': platform.platform(), 'logical_cpus': os.cpu_count(),
               'display': [1920, 1080], 'samples': []}
    with tempfile.TemporaryDirectory(prefix='studio-display-benchmark-') as root:
        store = ProfileStore(Path(root))
        store.save_settings({'executable_path': str(binary)})
        store.initialize_team(store.begin_team_setup(), 'root', 'Benchmark', 'synthetic-benchmark-password')
        profile = store.save_profile({'name': 'Synthetic display benchmark', 'os': 'linux',
            'fingerprint': {'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080}})
        async with running_team(store, browser_mode='remote') as (service, origin):
            async with httpx.AsyncClient(base_url=origin) as owner, async_playwright() as playwright:
                csrf = await login(owner, 'root', 'synthetic-benchmark-password')
                browser = await playwright.firefox.launch(headless=True)
                try:
                    launched = await command(service, owner, csrf, profile['id'], 'start')
                    native = service.engine.sessions[profile['id']].context.pages[0]
                    await native.goto(origin + '/synthetic-native')
                    await native.evaluate("document.body.style.background='rgb(120,180,220)'")
                    context = await browser.new_context(viewport={'width': 1920, 'height': 1400})
                    try:
                        response = await context.request.post(origin + '/team/v1/login',
                            headers={'Origin': origin}, data={'name': 'root', 'password': 'synthetic-benchmark-password'})
                        assert response.ok
                        client = await context.new_page()
                        traffic = [0]
                        def received(payload):
                            if isinstance(payload, bytes):
                                traffic[0] += len(payload)
                        client.on('websocket', lambda socket: socket.on('framereceived', received))
                        await client.goto(origin + '/#/remote/' + profile['id'])
                        surface = client.locator('canvas[width="1920"][height="1080"]').first
                        await expect(surface).to_be_visible(timeout=15000)
                        await asyncio.sleep(1)
                        for scene in ('static', 'changing'):
                            if scene == 'changing':
                                await native.evaluate('''() => {
                                    let sequence=0;
                                    window.benchmarkTimer=setInterval(() => {
                                        const n=sequence++;
                                        document.body.style.background=`rgb(${(n%16)*15},${(Math.floor(n/16)%16)*15},120)`;
                                    },40);
                                }''')
                            traffic[0] = 0
                            sample = await measure(surface, seconds)
                            sample.update(backend='kasmvnc', scene=scene, display_bytes=traffic[0])
                            sample['painted_fps'] = sample['painted_updates'] / sample['seconds']
                            results['samples'].append(sample)
                    finally:
                        await context.close()
                    await command(service, owner, csrf, profile['id'], 'stop', launched['run_id'])
                finally:
                    await browser.close()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=30, help='Duration of each of two samples (5–300 seconds).')
    args = parser.parse_args()
    if not 5 <= args.seconds <= 300:
        parser.error('--seconds must be between 5 and 300.')
    if os.getpid() != 1 or os.environ.get(MARKER) != '1':
        return _supervise([sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])
    print(json.dumps(asyncio.run(benchmark(args.seconds)), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
