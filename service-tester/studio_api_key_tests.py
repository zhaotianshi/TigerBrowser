"""Run the documented Node client against a real authenticated browser service."""

import asyncio
import json
import os
import re
import shutil
from pathlib import Path

import httpx
import pytest

from camoufox.studio.client.local import LocalClient
from studio_checkout_tests import checkout_workspace, seed, until  # noqa: F401
from studio_runtime_tests import command, login, running_team, workspace  # noqa: F401


async def documented_client(tmp_path, connection_data, profile_id, context):
    node = shutil.which('node')
    assert node is not None, 'Install Node.js 22.15+ to run the documented client.'
    document = Path(__file__).resolve().parents[1] / 'docs/studio-integration.zh-CN.md'
    script, = re.findall(r'```javascript\n(.*?)\n```', document.read_text(encoding='utf-8'), re.DOTALL)
    program = tmp_path / 'studio-client.mjs'
    program.write_text(script, encoding='utf-8')
    connection = tmp_path / 'connection.json'
    connection.touch(mode=0o600)
    connection.write_text(json.dumps(connection_data), encoding='utf-8')
    original_pages = tuple(context.pages)
    try:
        # Keep the published sample unchanged without depending on an external website.
        await context.route('https://example.com/', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<title>Studio Node acceptance</title>'))
        process = await asyncio.create_subprocess_exec(
            node, str(program), str(connection), profile_id, 'headless',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
            assert process.returncode == 0, stderr.decode(errors='replace')
            assert b'Studio Node acceptance' in stdout
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        assert tuple(context.pages) == original_pages
        assert all(not page.is_closed() for page in original_pages)
    finally:
        connection.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_documented_node_client_and_key_revocation(workspace, tmp_path):
    store, _, _, profile = workspace
    port = int(os.environ.get('CAMOUFOX_STUDIO_AUTOMATION_PORT', '8767'))
    async with running_team(store, automation_listen=True, automation_port=port) as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            csrf = await login(client, 'operator', 'synthetic-member-password')
            headers = {'Origin': origin, 'X-CSRF-Token': csrf}
            response = await client.post('/team/v1/api-keys', headers=headers,
                                         json={'name': 'Synthetic Node client', 'profile_ids': [profile['id']]})
            assert response.status_code == 200
            created = response.json()
            started = await command(service, client, csrf, profile['id'], 'start')
            context = service.engine.sessions[profile['id']].context
            await documented_client(tmp_path, created['connection'], profile['id'], context)
            assert not service.automation.leases
            revoked = await client.post('/team/v1/api-keys/' + created['key']['id'] + '/revoke',
                                        headers=headers, json={})
            assert revoked.status_code == 200
            async with httpx.AsyncClient(base_url=created['connection']['baseURL'], trust_env=False) as api:
                rejected = await api.get('/automation/v1/profiles', headers={
                    'Authorization': 'Bearer ' + created['connection']['token']})
                assert rejected.status_code == 401
            assert service.engine.sessions[profile['id']].context is context
            await command(service, client, csrf, profile['id'], 'stop', started['run_id'])
    assert not (store.root / 'workspace.lock').exists()


@pytest.mark.asyncio
async def test_documented_node_client_uses_local_checkout_and_returns_after_stop(checkout_workspace, tmp_path):
    store, _, _, profile = checkout_workspace
    profile_id = profile['id']
    async with running_team(store, staging_root=tmp_path / 'staging') as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            csrf = await login(client, 'operator', 'synthetic-member-password')
            await seed(service, client, csrf, profile_id, origin)
            local = LocalClient(tmp_path / 'client', origin, service.workspace_id, lambda *_: None)
            checkout_id = None
            try:
                local.save_local_settings({'automation_enabled': True})
                await local.configure_automation(client.cookies['camoufox-team-local'])
                bridge = local.engine.automation
                connection = json.loads(bridge.discovery_path.read_text(encoding='utf-8'))
                async with httpx.AsyncClient(base_url=connection['baseURL'], trust_env=False,
                                             timeout=90) as api:
                    started = await api.post(f'/automation/v1/profiles/{profile_id}/start',
                        headers={'Authorization': 'Bearer ' + connection['token']}, json={'headless': True})
                    assert started.status_code == 200
                checkout_id = service.repository.active_checkout(profile_id)['id']
                context = local.engine.sessions[profile_id].context
                await documented_client(tmp_path, connection, profile_id, context)
                assert not bridge.leases
                assert profile_id not in service.engine.sessions
                await local.stop(profile_id)
                await until(lambda: local.status(profile_id)['lease'] is None)
                assert service.repository.active_checkout(profile_id) is None
                await local.configure_automation('')
                assert not bridge.discovery_path.exists() and bridge.server is None
            finally:
                try:
                    await local.shutdown()
                finally:
                    if checkout_id is not None:
                        local.store.token(checkout_id, delete=True)
            assert not local.lock.path.exists()
