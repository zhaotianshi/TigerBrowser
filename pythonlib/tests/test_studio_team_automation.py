"""API-key automation shares member grants, maintenance gates and durable execution."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient
import pytest

from camoufox.studio.engine import BrowserEngine
from camoufox.studio.team.service import create_app
from test_studio_automation import Context, request as automation_request
from test_studio_team_service import ORIGIN, login, mutation, workspace


def engine_factory(store, state, message, failure):
    @asynccontextmanager
    async def browser(**kwargs):
        yield Context()

    return BrowserEngine(store, state, message, failure,
                         prepare=lambda store, profile: {'headless': profile['headless']},
                         browser_factory=browser)


def network_app(store):
    return create_app(store.root, origin=ORIGIN, browser_mode='headless', engine_factory=engine_factory,
                      automation={'host': '127.0.0.1', 'port': 0, 'origin': 'http://127.0.0.1'})


def credential(client, member='op'):
    csrf = login(client, member, 'admin-password-1' if member == 'root' else 'member-password-1')
    created = mutation(client, csrf, '/api-keys', {'name': 'Integration worker'})
    assert created.status_code == 200, created.text
    return created.json()['connection']['token']


async def request(bridge, token, method, route, body=None):
    return await automation_request(bridge, method, route, body, token=False,
                                    extra={'Authorization': 'Bearer ' + token})


def test_team_key_bridge_uses_member_grants_and_durable_starts(workspace):
    member = 'op'
    store, admin, operator, viewer, first, second = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client, member)
        bridge = application.state.team.automation
        assert not (store.root / 'automation.json').exists()
        status, result = client.portal.call(request, bridge, token, 'GET', '/profiles')
        assert status == 200 and [p['id'] for p in result['profiles']] == [first['id']]
        status, result = client.portal.call(request, bridge, token, 'POST', '/profiles/' + second['id'] + '/start', {'headless': True})
        assert status == 403 and result['error']['code'] == 'access_denied'
        status, _ = client.portal.call(request, bridge, token, 'POST', '/profiles/' + first['id'] + '/start', {'headless': True})
        assert status == 200
        assert application.state.team.repository.latest_run(first['id'])['state'] == 'running'
        status, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        assert status == 200
        key = lease['leaseId']
        status, page = client.portal.call(request, bridge, token, 'POST', '/leases/' + key + '/pages', {})
        assert status == 200
        context = application.state.team.engine.sessions[first['id']].context
        assert len(context.pages) == 2
        assert client.portal.call(request, bridge, token, 'DELETE', '/leases/' + key)[0] == 200
        assert not context.pages[0].closed and context.pages[1].closed
        assert application.state.team.repository.latest_run(first['id'])['state'] == 'running'
    assert not (store.root / 'automation.json').exists()
    assert not (store.root / 'workspace.lock').exists()


def test_idle_memory_recycle_stops_one_exact_durable_run_and_keeps_settings(workspace):
    store, _, _, _, first, second = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client)
        service = application.state.team
        bridge = service.automation
        original = store.get_profile(first['id'])
        assert client.portal.call(request, bridge, token, 'POST', '/profiles/' + first['id'] + '/start', {'headless': True})[0] == 200
        before_run = service.repository.latest_run(first['id'])
        before_session = service.engine.sessions[first['id']]
        status, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        assert status == 200
        path = '/leases/' + lease['leaseId']
        status, sample = client.portal.call(request, bridge, token, 'GET', path + '/resources')
        assert status == 200 and sample['idle'] is True
        status, result = client.portal.call(request, bridge, token, 'POST', path + '/recycle', {'sessionId': sample['sessionId']})
        assert status == 200, result
        assert result['recycled'] and result['previousSessionId'] == sample['sessionId']
        assert service.engine.sessions[first['id']] is not before_session
        assert service.engine.sessions[first['id']].headless is True
        assert service.engine.sessions[first['id']].main_world_eval == before_session.main_world_eval
        assert service.repository.latest_run(first['id'])['id'] != before_run['id']
        assert service.repository.latest_run(first['id'])['state'] == 'running'
        assert {k: v for k, v in store.get_profile(first['id']).items() if k not in ('last_launch_at', 'updated_at')} == {
            k: v for k, v in original.items() if k not in ('last_launch_at', 'updated_at')}
        assert not bridge.leases
        assert not service.engine.sessions.get(second['id'])


@pytest.mark.parametrize('controller', [False, True])
def test_memory_recycle_fences_management_and_rechecks_manual_control_before_stop(workspace, controller):
    from types import SimpleNamespace
    from uuid import uuid4
    store, _, _, _, first, _ = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client)
        csrf = login(client, 'op', 'member-password-1')
        service, bridge = application.state.team, application.state.team.automation
        client.portal.call(request, bridge, token, 'POST', '/profiles/' + first['id'] + '/start', {'headless': True})
        run = service.repository.latest_run(first['id'])
        expected = service.engine.sessions[first['id']]
        _, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        path = '/leases/' + lease['leaseId']
        _, sample = client.portal.call(request, bridge, token, 'GET', path + '/resources')
        entered, finish = asyncio.Event(), asyncio.Event()
        original = service._execute
        async def execute(access, command, **kwargs):
            if kwargs.get('require_idle'):
                entered.set()
                await finish.wait()
            return await original(access, command, **kwargs)
        service._execute = execute
        operation = client.portal.start_task_soon(request, bridge, token, 'POST', path + '/recycle',
                                                {'sessionId': sample['sessionId']})
        try:
            client.portal.call(entered.wait)
            refused = mutation(client, csrf, '/profiles/' + first['id'] + '/stop',
                               {'request_id': str(uuid4()), 'run_id': run['id']})
            assert refused.status_code == 409 and refused.json()['error'] == 'profile_busy'
            assert client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})[0] == 409
            if controller:
                service.remotes[run['id']] = SimpleNamespace(controller=object())
        finally:
            client.portal.call(finish.set)
        status, result = operation.result(timeout=5)
        if controller:
            assert status == 409 and result['error']['code'] == 'profile_busy'
            assert service.engine.sessions[first['id']] is expected
            assert service.repository.latest_run(first['id'])['id'] == run['id']
            service.remotes.pop(run['id'])
            assert client.portal.call(request, bridge, token, 'GET', path + '/resources')[0] == 200
        else:
            assert status == 200, result
            assert service.engine.sessions[first['id']] is not expected
        assert not service.profile_jobs


def test_cancelled_recycle_callback_keeps_profile_lock_until_durable_work_and_fence_end(workspace):
    from uuid import uuid4
    from camoufox.studio.automation import AutomationError
    store, _, _, _, first, _ = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client)
        csrf = login(client, 'op', 'member-password-1')
        service, bridge = application.state.team, application.state.team.automation
        client.portal.call(request, bridge, token, 'POST', '/profiles/' + first['id'] + '/start', {'headless': True})
        run = service.repository.latest_run(first['id'])
        _, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        path = '/leases/' + lease['leaseId']
        _, sample = client.portal.call(request, bridge, token, 'GET', path + '/resources')
        entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        execute, recycle = service._execute, bridge.recycle_profile
        async def blocked(access, command, **kwargs):
            if kwargs.get('require_idle'):
                entered.set()
                await finish.wait()
            return await execute(access, command, **kwargs)
        async def timed_out(profile_id, expected):
            task = asyncio.create_task(recycle(profile_id, expected))
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            pending = service.profile_jobs[profile_id]['task']
            cancelled.set()
            await asyncio.shield(pending)
            assert profile_id in service.profile_jobs
            raise AutomationError('profile_recycle_unconfirmed', 'The callback deadline elapsed.', 409)
        service._execute, bridge.recycle_profile = blocked, timed_out
        operation = client.portal.start_task_soon(request, bridge, token, 'POST', path + '/recycle',
                                                {'sessionId': sample['sessionId']})
        try:
            client.portal.call(cancelled.wait)
            assert first['id'] in service.profile_jobs
            response = mutation(client, csrf, '/profiles/' + first['id'] + '/stop',
                                {'request_id': str(uuid4()), 'run_id': run['id']})
            assert response.status_code == 409 and response.json()['error'] == 'profile_busy'
        finally:
            client.portal.call(finish.set)
        status, result = operation.result(timeout=5)
        assert status == 409 and result['error']['code'] == 'profile_recycle_unconfirmed'
        assert first['id'] not in service.engine.sessions
        assert service.repository.latest_run(first['id'])['state'] == 'stopped'
        assert not service.profile_jobs


@pytest.mark.parametrize('member', ['root', 'op'])
def test_team_profile_creation_preserves_admin_and_maintenance_boundaries(workspace, member):
    store, _, _, _, _, _ = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client, member)
        service = application.state.team
        before = {profile['id'] for profile in store.list_profiles()}
        status, result = client.portal.call(request, service.automation, token, 'POST', '/profiles',
                                            {'name': 'Created by local automation', 'headless': True})
        if member == 'op':
            assert status == 403 and result['error']['code'] == 'admin_required'
            assert {profile['id'] for profile in store.list_profiles()} == before
        else:
            assert status == 200 and result['profile']['id'] not in before
            profile_id = result['profile']['id']
            saved = store.get_profile(profile_id)
            assert saved['identity'] is None and saved['headless'] is True
            assert not service.repository.unresolved_runs() and not service.engine.sessions
            assert client.portal.call(request, service.automation, token, 'POST',
                                      '/profiles/' + profile_id + '/start', {'headless': True})[0] == 200
            status, lease = client.portal.call(request, service.automation, token, 'POST', '/leases',
                                               {'profileId': profile_id})
            assert status == 200
            assert client.portal.call(request, service.automation, token, 'DELETE', '/leases/' + lease['leaseId'])[0] == 200
            assert service.repository.latest_run(profile_id)['state'] == 'running'
            csrf = login(client)
            created = mutation(client, csrf, '/profiles', {'name': 'Created by team API'})
            assert created.status_code == 200 and created.json()['id'] not in before
            service.maintenance_job = object()
            try:
                status, result = client.portal.call(request, service.automation, token, 'POST', '/profiles',
                                                    {'name': 'Blocked by maintenance'})
                assert status == 409 and result['error']['code'] == 'service_busy'
                assert mutation(client, csrf, '/profiles', {'name': 'Also blocked'}).status_code == 409
                assert len(store.list_profiles()) == len(before) + 2
            finally:
                service.maintenance_job = None


def test_revoked_admin_key_cannot_create_profiles(workspace):
    member = 'root'
    store, admin, _, _, _, _ = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client, member)
        before = store.list_profiles()
        store.reset_member_password(admin['id'], admin['id'], 'updated-admin-password')
        status, result = client.portal.call(request, application.state.team.automation, token, 'POST', '/profiles',
                                            {'name': 'Rejected after revocation'})
        assert status == 401 and result['error']['code'] == 'session_expired'
        assert store.list_profiles() == before


def test_permission_revocation_closes_task_pages_and_keeps_browser(workspace):
    member = 'op'
    store, admin, operator, viewer, first, second = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client, member)
        bridge = application.state.team.automation
        client.portal.call(request, bridge, token, 'POST', '/profiles/' + first['id'] + '/start', {'headless': True})
        _, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        client.portal.call(request, bridge, token, 'POST', '/leases/' + lease['leaseId'] + '/pages', {})
        context = application.state.team.engine.sessions[first['id']].context
        store.revoke_profile(admin['id'], operator['id'], first['id'])
        client.portal.call(application.state.team.revoke_connections)
        assert context.pages[1].closed and not context.pages[0].closed
        assert not (store.root / 'automation.json').exists()
        status, result = client.portal.call(request, bridge, token, 'GET', '/profiles')
        assert status == 200 and result['profiles'] == []
        assert not bridge.leases


def test_concurrent_starts_reserve_one_run_and_mode_changes_are_explicit(workspace):
    member = 'op'
    store, _, _, _, first, _ = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client, member)
        bridge = application.state.team.automation
        async def start_twice():
            return await asyncio.gather(*(request(bridge, token, 'POST', '/profiles/' + first['id'] + '/start',
                                                 {'headless': True}) for _ in range(2)))
        assert [result[0] for result in client.portal.call(start_twice)] == [200, 200]
        assert len(application.state.team.repository.unresolved_runs()) == 1
        status, result = client.portal.call(request, bridge, token, 'POST', '/profiles/' + first['id'] + '/start', {})
        assert status == 409 and result['error']['code'] == 'profile_mode_mismatch'
        _, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        assert client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})[0] == 409
        assert client.portal.call(request, bridge, token, 'DELETE', '/leases/' + lease['leaseId'])[0] == 200


@pytest.mark.parametrize('enabled', [False, True])
def test_main_world_team_start_lease_and_reuse_preserve_run_ownership(workspace, enabled):
    store, _, _, _, first, _ = workspace
    application = network_app(store)
    launches = []

    def prepare(store, profile, *, main_world_eval=None):
        launches.append(main_world_eval)
        return {'env': {'CAMOU_CONFIG_1': json.dumps({'allowMainWorld': main_world_eval is True})}}

    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client)
        service = application.state.team
        service.engine.prepare = prepare
        bridge, route = service.automation, '/profiles/' + first['id'] + '/start'
        status, listing = client.portal.call(request, bridge, token, 'GET', '/profiles')
        assert status == 200 and listing['capabilities']['mainWorldEval'] is True
        assert listing['capabilities']['responseObservation'] is True
        assert listing['capabilities']['responseBodyRead'] is True
        assert listing['capabilities']['downloadBinaryRead'] is True
        assert listing['capabilities']['mainWorldRestart'] is True
        assert listing['capabilities']['browserMemoryMaintenance'] is True
        assert listing['profiles'][0]['mainWorldEval'] is None

        async def start_twice():
            return await asyncio.gather(*(request(bridge, token, 'POST', route,
                                                 {'headless': True, 'mainWorldEval': enabled}) for _ in range(2)))

        results = client.portal.call(start_twice)
        assert all(status == 200 and result['profile']['mainWorldEval'] is enabled for status, result in results)
        session = service.engine.sessions[first['id']]
        run = service.repository.latest_run(first['id'])
        assert run['state'] == 'running' and len(service.repository.unresolved_runs()) == 1
        assert launches == ([True] if enabled else [None])
        status, leased = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        assert status == 200 and leased['profile']['mainWorldEval'] is enabled
        client.portal.call(request, bridge, token, 'POST', '/leases/' + leased['leaseId'] + '/pages', {})
        status, result = client.portal.call(request, bridge, token, 'POST', route,
                                            {'headless': True, 'mainWorldEval': True})
        if enabled:
            assert status == 200 and result['profile']['mainWorldEval'] is True
        else:
            assert status == 409 and result['error']['code'] == 'profile_main_world_mismatch'
        assert not session.stop.is_set() and all(not page.closed for page in session.context.pages)
        status, reused = client.portal.call(request, bridge, token, 'POST', route, {'headless': True})
        assert status == 200 and reused['profile']['mainWorldEval'] is enabled
        assert service.repository.latest_run(first['id'])['id'] == run['id']
        assert store.get_profile(first['id'])['identity'] is None


def test_team_main_world_upgrade_replaces_only_an_unleased_durable_run(workspace):
    store, _, _, _, first, _ = workspace
    application = network_app(store)
    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client)
        service = application.state.team
        service.engine.prepare = lambda store, profile, main_world_eval=None: {
            'env': {'CAMOU_CONFIG_1': json.dumps({'allowMainWorld': main_world_eval is True})}}
        bridge, route = service.automation, '/profiles/' + first['id'] + '/start'
        assert client.portal.call(request, bridge, token, 'POST', route, {'headless': True})[0] == 200
        old_run = service.repository.latest_run(first['id'])
        _, lease = client.portal.call(request, bridge, token, 'POST', '/leases', {'profileId': first['id']})
        body = {'headless': True, 'mainWorldEval': True, 'restartIfNeeded': True}
        status, result = client.portal.call(request, bridge, token, 'POST', route, body)
        assert status == 409 and result['error']['code'] == 'profile_leased'
        assert service.repository.latest_run(first['id'])['id'] == old_run['id']
        assert client.portal.call(request, bridge, token, 'DELETE', '/leases/' + lease['leaseId'])[0] == 200
        status, result = client.portal.call(request, bridge, token, 'POST', route, body)
        assert status == 200 and result['profile']['mainWorldEval'] is True
        new_run = service.repository.latest_run(first['id'])
        assert new_run['id'] != old_run['id'] and new_run['state'] == 'running'
        assert len(service.repository.unresolved_runs()) == 1
        assert store.get_profile(first['id'])['identity'] is None
        assert client.portal.call(request, bridge, token, 'POST', route, body)[0] == 200
        assert service.repository.latest_run(first['id'])['id'] == new_run['id']


def test_team_unsupported_main_world_preserves_stopped_run_and_safe_error(workspace):
    from camoufox.studio.engine import MainWorldUnsupportedError
    store, _, _, _, first, _ = workspace
    application = network_app(store)

    def unsupported(store, profile, *, main_world_eval=None):
        assert main_world_eval is True
        raise MainWorldUnsupportedError() from RuntimeError('private-credential')

    with TestClient(application, base_url=ORIGIN) as client:
        token = credential(client)
        service = application.state.team
        service.engine.prepare = unsupported
        status, result = client.portal.call(request, service.automation, token, 'POST',
                                            '/profiles/' + first['id'] + '/start',
                                            {'headless': True, 'mainWorldEval': True})
        assert status == 409 and result['error']['code'] == 'main_world_unsupported'
        assert 'private-credential' not in json.dumps(result)
        assert not service.engine.sessions and not service.repository.unresolved_runs()
        assert service.repository.latest_run(first['id'])['error'] == 'main_world_unsupported'


@pytest.mark.parametrize('enabled', [False, True])
def test_main_world_requirement_is_part_of_durable_command_identity(workspace, enabled):
    from uuid import uuid4
    from camoufox.studio.store import ValidationError
    from camoufox.studio.team.repository import TeamRepository

    store, _, _, _, first, _ = workspace
    session = store.login('root', 'admin-password-1')
    repository = TeamRepository(store)
    request_id = str(uuid4())

    def reserve(requirement):
        return repository.reserve(session['session_id'], first['id'], 'start', request_id,
                                  owner='test-owner', mode='headless', main_world_eval=requirement)

    command, created = reserve(enabled)
    assert created and command['signature'].get('main_world_eval', False) is enabled
    replay, created = reserve(enabled)
    assert not created and replay == command
    with pytest.raises(ValidationError, match='request_conflict'):
        reserve(not enabled)
    assert repository.latest_run(first['id'])['id'] == command['run_id']


def test_local_session_renewal_never_revives_expiry_or_password_revocation(workspace):
    store, admin, operator, _, _, _ = workspace
    session = store.open_local_automation_session('op')
    def deadline(hours):
        return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    def expires(value):
        with store._write() as (connection, _):
            row = connection.execute('SELECT data FROM team_sessions WHERE id=?', (session['session_id'],)).fetchone()
            record = json.loads(row['data'])
            store._touch_session(connection, record, expires_at=value)
    expires(deadline(0.5))
    store.renew_local_automation_session(session['session_id'])
    assert store.resolve_session(session['session_id'])['expires_at'] > deadline(11)
    expires(deadline(-1))
    with pytest.raises(Exception, match='session_expired'):
        store.renew_local_automation_session(session['session_id'])
    session = store.open_local_automation_session('op')
    store.reset_member_password(admin['id'], operator['id'], 'new-member-password')
    with pytest.raises(Exception, match='session_expired'):
        store.renew_local_automation_session(session['session_id'])
