"""Real-browser, same-host check-out acceptance over loopback HTTP; not cross-host certification."""

import asyncio
import os
import sqlite3
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import pytest

from camoufox import multiversion
from camoufox.pkgman import launch_path
from camoufox.studio.client.local import LocalClient
from camoufox.studio.kernels import kernel_id
from studio_runtime_tests import command, login, running_team, site_state, workspace  # noqa: F401


@pytest.fixture
def checkout_workspace(workspace):
    store, admin, member, profile = workspace
    selected = Path(os.environ['CAMOUFOX_STUDIO_BINARY']).resolve()
    installed = [item for item in multiversion.list_installed()
                 if Path(launch_path(item.path)).resolve() == selected]
    assert len(installed) == 1, 'The selected browser must be one registered exact kernel.'
    store.save_profile({'kernel': kernel_id(installed[0])}, profile['id'])
    store.grant_profile(admin['id'], member['id'], profile['id'], ['view', 'operate', 'checkout'])
    return store, admin, member, profile


def headers(origin, csrf):
    return {'Origin': origin, 'X-CSRF-Token': csrf}


async def until(predicate):
    deadline = time.monotonic() + 60
    while not predicate():
        assert time.monotonic() < deadline, 'The local browser or check-out did not settle.'
        await asyncio.sleep(0.05)


@asynccontextmanager
async def local_run(service, origin, root, client, csrf, profile_id):
    local = LocalClient(root, origin, service.workspace_id, lambda *_: None)
    created = None
    try:
        response = await client.post(f'/team/v1/profiles/{profile_id}/checkout',
            headers=headers(origin, csrf), json={'request_id': str(uuid4()), **local.device()})
        assert response.status_code == 201
        created = response.json()
        await local.launch(profile_id, created['checkout'], created['token'], headless=True)
        session = local.engine.sessions[profile_id]
        await asyncio.wait_for(session.ready.wait(), timeout=90)
        assert session.error is None, session.error
        yield local, created
    finally:
        try:
            await local.shutdown()
        finally:
            # Only this test-created lease key in its random cache namespace is touched.
            if created is not None:
                local.store.token(created['checkout']['id'], delete=True)
    assert not local.engine.recovery_required and not local.lock.path.exists()


async def seed(service, client, csrf, profile_id, origin):
    started = await command(service, client, csrf, profile_id, 'start')
    initial = await site_state(service.engine.sessions[profile_id].context,
                               origin + '/synthetic-site', create=True)
    await command(service, client, csrf, profile_id, 'stop', started['run_id'])
    return initial


@pytest.mark.asyncio
async def test_real_checkout_return_preserves_site_data_and_exclusive_ownership(checkout_workspace, tmp_path):
    store, _, _, profile = checkout_workspace
    profile_id = profile['id']
    async with running_team(store, staging_root=tmp_path / 'staging') as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
            csrf = await login(client, 'operator', 'synthetic-member-password')
            initial = await seed(service, client, csrf, profile_id, origin)
            identity = store.get_profile(profile_id)['identity']
            async with local_run(service, origin, tmp_path / 'client', client, csrf, profile_id) as (local, created):
                context = local.engine.sessions[profile_id].context
                assert await site_state(context, origin + '/synthetic-site') == initial
                for operation, data in (
                    ('start', {'request_id': str(uuid4())}),
                    ('checkout', {'request_id': str(uuid4()), **local.device()}),
                ):
                    denied = await client.post(f'/team/v1/profiles/{profile_id}/{operation}',
                        headers=headers(origin, csrf), json=data)
                    assert denied.status_code == 409
                    assert denied.json()['error'] == 'profile_checked_out'
                await context.add_cookies([{'name': 'client-returned', 'value': 'synthetic-local-cookie',
                    'url': origin, 'expires': time.time() + 3600}])
                await context.pages[0].evaluate("localStorage.setItem('studio-state', 'returned-storage')")
                updated = await site_state(context, origin + '/synthetic-site')
                await local.stop(profile_id)
                await until(lambda: not local.sync.leases())
                assert profile_id not in local.engine.sessions
                assert local.store.token(created['checkout']['id']) is None
            restarted = await command(service, client, csrf, profile_id, 'start')
            assert await site_state(service.engine.sessions[profile_id].context,
                                     origin + '/synthetic-site') == updated
            assert store.get_profile(profile_id)['identity'] == identity
            await command(service, client, csrf, profile_id, 'stop', restarted['run_id'])


@pytest.mark.asyncio
@pytest.mark.parametrize('force', [False, True], ids=['withdraw-grant', 'force-release'])
async def test_real_revocation_stops_the_client_and_preserves_the_authoritative_copy(checkout_workspace, tmp_path, force):
    store, _, member, profile = checkout_workspace
    profile_id = profile['id']
    async with running_team(store, staging_root=tmp_path / 'staging') as (service, origin):
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as client, \
                httpx.AsyncClient(base_url=origin, trust_env=False) as admin:
            csrf = await login(client, 'operator', 'synthetic-member-password')
            admin_csrf = await login(admin, 'root', 'synthetic-admin-password')
            initial = await seed(service, client, csrf, profile_id, origin)
            identity = store.get_profile(profile_id)['identity']
            async with local_run(service, origin, tmp_path / 'client', client, csrf, profile_id) as (local, created):
                context = local.engine.sessions[profile_id].context
                assert await site_state(context, origin + '/synthetic-site') == initial
                await context.add_cookies([{'name': 'client-revocation', 'value': 'synthetic-unreturned',
                    'url': origin, 'expires': time.time() + 3600}])
                updated = await site_state(context, origin + '/synthetic-site')
                checkout_id = created['checkout']['id']
                path, data = (f'/team/v1/profiles/{profile_id}/release', {'checkout_id': checkout_id}) if force else (
                    '/team/v1/members', {'action': 'revoke', 'id': member['id'], 'profile_id': profile_id})
                response = await admin.post(path, headers=headers(origin, admin_csrf), json=data)
                assert response.status_code == 200
                monitor = asyncio.create_task(local.run())
                try:
                    await until(lambda: not local.sync.leases())
                finally:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                assert profile_id not in local.engine.sessions and not local.engine.recovery_required
                assert local.store.token(checkout_id) is None
                if force:
                    stale = local.store.root / 'stale' / checkout_id
                    assert not local.store.profile_dir(profile_id).exists()
                    assert local.stale_copies()[0]['checkout_id'] == checkout_id
                    # Inspect the stopped stale copy without making its identity executable again.
                    with sqlite3.connect((stale / 'cookies.sqlite').as_uri() + '?mode=ro', uri=True) as connection:
                        assert connection.execute('SELECT value FROM moz_cookies WHERE name = ?',
                            ('client-revocation',)).fetchall() == [('synthetic-unreturned',)]
                    denied = await admin.post(f'/team/v1/checkouts/{checkout_id}/heartbeat',
                        headers={'Origin': origin, 'Authorization': 'Bearer ' + created['token']}, json={})
                    assert denied.json()['error'] == 'checkout_revoked' and denied.status_code != 200
                else:
                    assert local.stale_copies() == []
            restarted = await command(service, admin, admin_csrf, profile_id, 'start')
            assert await site_state(service.engine.sessions[profile_id].context,
                                     origin + '/synthetic-site') == (initial if force else updated)
            assert store.get_profile(profile_id)['identity'] == identity
            await command(service, admin, admin_csrf, profile_id, 'stop', restarted['run_id'])


@pytest.mark.asyncio
async def test_real_return_resumes_after_the_server_stops_and_restarts(checkout_workspace, tmp_path):
    store, _, _, profile = checkout_workspace
    profile_id = profile['id']
    async with AsyncExitStack() as local_lifetime:
        async with running_team(store, staging_root=tmp_path / 'staging') as (service, origin):
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                csrf = await login(client, 'operator', 'synthetic-member-password')
                initial = await seed(service, client, csrf, profile_id, origin)
                identity = store.get_profile(profile_id)['identity']
                local, created = await local_lifetime.enter_async_context(
                    local_run(service, origin, tmp_path / 'client', client, csrf, profile_id))
                context = local.engine.sessions[profile_id].context
                assert await site_state(context, origin + '/synthetic-site') == initial
                await context.pages[0].evaluate("localStorage.setItem('studio-state', 'offline-return')")
                updated = await site_state(context, origin + '/synthetic-site')
        # The actual TCP listener is gone, but its durable lease still belongs to this cache.
        assert profile_id in local.engine.sessions
        await local.stop(profile_id)
        await until(lambda: local.errors.get(profile_id) == 'server_unreachable')
        assert profile_id not in local.engine.sessions
        assert local.sync.leases()[profile_id]['state'] == 'pending_return'
        assert local.store.profile_dir(profile_id).is_dir()
        assert store.get_profile(profile_id)['data_revision'] == 0
        async with running_team(store, port=urlsplit(origin).port,
                                staging_root=tmp_path / 'staging') as (service, restored_origin):
            assert restored_origin == origin
            async with httpx.AsyncClient(base_url=origin, trust_env=False) as client:
                csrf = await login(client, 'operator', 'synthetic-member-password')
                denied = await client.post(f'/team/v1/profiles/{profile_id}/start',
                    headers=headers(origin, csrf), json={'request_id': str(uuid4())})
                assert denied.status_code == 409 and denied.json()['error'] == 'profile_checked_out'
                monitor = asyncio.create_task(local.run())
                try:
                    await until(lambda: not local.sync.leases())
                finally:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
                assert local.store.token(created['checkout']['id']) is None
                assert store.get_profile(profile_id)['data_revision'] == 1
                restarted = await command(service, client, csrf, profile_id, 'start')
                assert await site_state(service.engine.sessions[profile_id].context,
                                         origin + '/synthetic-site') == updated
                assert store.get_profile(profile_id)['identity'] == identity
                await command(service, client, csrf, profile_id, 'stop', restarted['run_id'])
