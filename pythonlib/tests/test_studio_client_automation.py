"""Local automation on a member computer checks out through the member's own server login."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
import requests

from camoufox.studio.client.local import LocalClient
from camoufox.studio.engine import BrowserEngine
from test_studio_automation import Context, request
from test_studio_client_local import until
from test_studio_client_sync import server  # noqa: F401
from test_studio_team_service import workspace  # noqa: F401


def engine_factory(store, state, message, failure):
    # Automation leases listen on the context, as they do on a real Playwright context.
    @asynccontextmanager
    async def browser(**kwargs):
        yield Context()

    return BrowserEngine(store, state, message, failure,
                         prepare=lambda store, profile: {'headless': profile['headless']}, browser_factory=browser)


def login(origin, name, password):
    response = requests.post(origin + '/team/v1/login', headers={'Origin': origin},
                             json={'name': name, 'password': password})
    assert response.status_code == 200, response.text
    return response.cookies['camoufox-team-local']


@pytest.fixture
def local(server, tmp_path_factory):
    store, service, engine, _, first, lease, secrets = server
    origin = engine.transport.origin
    client = LocalClient(tmp_path_factory.mktemp('client-base'), origin, service.workspace_id,
                         lambda *_: None, engine_factory=engine_factory)
    client.save_local_settings({'automation_enabled': True})
    return store, service, client, first, origin


def test_start_checks_out_with_the_member_login_and_returns_after_stop(local, server):
    store, service, client, first, origin = local
    second = next(item for item in store.list_profiles() if item['id'] != first['id'])

    async def scenario():
        await client.configure_automation(login(origin, 'op', 'member-password-1'))
        bridge = client.engine.automation
        discovery = json.loads(bridge.discovery_path.read_text(encoding='utf-8'))
        assert discovery['member'] == 'op' and discovery['baseURL'].startswith('http://127.0.0.1:')
        status, listed = await request(bridge, 'GET', '/profiles')
        assert status == 200 and [item['id'] for item in listed['profiles']] == [first['id']]
        status, denied = await request(bridge, 'POST', f"/profiles/{second['id']}/start", {})
        assert status == 403 and denied['error']['code'] == 'access_denied'
        status, created = await request(bridge, 'POST', '/profiles', {'name': 'Automation'})
        assert status == 403 and created['error']['code'] == 'admin_required'
        status, started = await request(bridge, 'POST', f"/profiles/{first['id']}/start", {})
        assert status == 200 and started['profile']['state'] == 'running', started
        holder = service.repository.active_checkout(first['id'])
        assert holder['member_name'] == 'Operator' and holder['device_id'] == client.device()['device_id']
        status, leased = await request(bridge, 'POST', '/leases', {'profileId': first['id']})
        assert status == 200 and leased['leaseId']
        (client.data_folder(first['id']) / 'cookies.sqlite').write_bytes(b'automation-run')
        await client.stop(first['id'])
        await until(lambda: client.status(first['id'])['lease'] is None)
        await client.shutdown()
        assert not bridge.discovery_path.exists()

    asyncio.run(scenario())
    assert (store.profile_dir(first['id']) / 'cookies.sqlite').read_bytes() == b'automation-run'
    assert service.repository.active_checkout(first['id']) is None


def test_logout_or_an_invalid_login_withdraws_local_automation(local):
    store, service, client, first, origin = local

    async def scenario():
        cookie = login(origin, 'op', 'member-password-1')
        await client.configure_automation(cookie)
        bridge = client.engine.automation
        token = bridge.token
        await client.configure_automation(cookie)
        assert bridge.token == token, 'a repeated cookie notification keeps the published connection'
        await client.configure_automation('')
        assert bridge.server is None and not bridge.discovery_path.exists()
        await client.configure_automation('not-a-session')
        assert bridge.server is None and client.automation_status()['error'] == 'session_expired'
        client.save_local_settings({'automation_enabled': False})
        await client.configure_automation(cookie)
        assert bridge.server is None and client.automation_status()['error'] is None
        await client.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize('enabled', [False, True])
def test_local_main_world_checkout_concurrency_and_actual_lease_state(local, enabled):
    store, service, client, first, origin = local
    launches = []

    def prepare(store, profile, *, main_world_eval=None):
        launches.append(main_world_eval)
        return {'env': {'CAMOU_CONFIG_1': json.dumps({'allowMainWorld': main_world_eval is True})}}

    client.engine.prepare = prepare

    async def scenario():
        try:
            await client.configure_automation(login(origin, 'op', 'member-password-1'))
            bridge, route = client.engine.automation, f"/profiles/{first['id']}/start"
            status, listed = await request(bridge, 'GET', '/profiles')
            assert status == 200 and listed['capabilities']['mainWorldEval'] is True
            assert listed['capabilities']['responseObservation'] is True
            assert listed['capabilities']['responseBodyRead'] is True
            assert listed['capabilities']['downloadBinaryRead'] is True
            assert listed['profiles'][0]['mainWorldEval'] is None
            results = await asyncio.gather(*(request(bridge, 'POST', route, {'mainWorldEval': enabled}) for _ in range(2)))
            assert all(status == 200 and result['profile']['mainWorldEval'] is enabled for status, result in results)
            session = client.engine.sessions[first['id']]
            checkout = service.repository.active_checkout(first['id'])
            assert launches == ([True] if enabled else [None])
            assert checkout['device_id'] == client.device()['device_id']
            status, leased = await request(bridge, 'POST', '/leases', {'profileId': first['id']})
            assert status == 200 and leased['profile']['mainWorldEval'] is enabled
            status, result = await request(bridge, 'POST', route, {'mainWorldEval': True})
            if enabled:
                assert status == 200 and result['profile']['mainWorldEval'] is True
            else:
                assert status == 409 and result['error']['code'] == 'profile_main_world_mismatch'
            assert not session.stop.is_set() and all(not page.closed for page in session.context.pages)
            status, result = await request(bridge, 'POST', route, {})
            assert status == 200 and result['profile']['mainWorldEval'] is enabled
            assert service.repository.active_checkout(first['id'])['id'] == checkout['id']
            assert store.get_profile(first['id'])['identity'] is None
        finally:
            await client.shutdown()

    asyncio.run(scenario())


def test_local_main_world_unsupported_survives_fast_launch_failure(local):
    from camoufox.studio.engine import MainWorldUnsupportedError
    _, _, client, first, origin = local

    def unsupported(store, profile, *, main_world_eval=None):
        assert main_world_eval is True
        raise MainWorldUnsupportedError()

    client.engine.prepare = unsupported

    async def scenario():
        try:
            await client.configure_automation(login(origin, 'op', 'member-password-1'))
            status, result = await request(client.engine.automation, 'POST', f"/profiles/{first['id']}/start",
                                           {'mainWorldEval': True})
            assert status == 409 and result['error']['code'] == 'main_world_unsupported'
            assert not client.engine.sessions
        finally:
            await client.shutdown()

    asyncio.run(scenario())
