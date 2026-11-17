"""Persistent automation credentials retain live grants and isolate task ownership."""

import asyncio
import json
from functools import partial
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from camoufox.studio.api_keys import ApiKeys
from camoufox.studio.store import ValidationError
from camoufox.studio.team.repository import TeamRepository
from camoufox.studio.team.service import create_app
from test_studio_automation import request
from test_studio_team_automation import engine_factory
from test_studio_team_service import ORIGIN, login, mutation, workspace


def test_keys_store_only_digest_and_resolve_current_grants(workspace):
    store, admin, operator, _, first, second = workspace
    keys = ApiKeys(store)
    session = store.login('op', 'member-password-1')
    created = keys.create(session['session_id'], {'name': 'Worker', 'profile_ids': [first['id']]})
    token = created['token']
    assert len(token) >= 43 and token not in json.dumps(keys.list(session['session_id']))
    with store._connect() as connection:
        for table in ('team_api_keys', 'team_sessions', 'team_audit'):
            assert token not in '\n'.join(str(tuple(row)) for row in connection.execute('SELECT * FROM ' + table))
    access = keys.authenticate(token, '127.0.0.1')
    assert [profile['id'] for profile in access.list_profiles()] == [first['id']]
    with pytest.raises(ValidationError, match='access_denied'):
        access.require_profile(second['id'], 'operate')
    store.revoke_profile(admin['id'], operator['id'], first['id'])
    assert access.list_profiles() == []
    assert keys.authenticate(token, '127.0.0.1').list_profiles() == []
    store.grant_profile(admin['id'], operator['id'], first['id'], ['view', 'operate'])
    keys.authenticate(token, '127.0.0.1').require_profile(first['id'], 'operate')
    store.set_member_active(admin['id'], operator['id'], False)
    store.set_member_active(admin['id'], operator['id'], True)
    with pytest.raises(ValidationError, match='session_expired'):
        keys.authenticate(token, '127.0.0.1')


def test_key_scope_applies_inside_durable_run_reservation(workspace):
    store, admin, _, _, first, second = workspace
    keys = ApiKeys(store)
    session = store.login('root', 'admin-password-1')
    created = keys.create(session['session_id'], {'name': 'Scoped admin', 'profile_ids': [first['id']]})
    access = keys.authenticate(created['token'], '127.0.0.1')
    with pytest.raises(ValidationError, match='access_denied'):
        access.save_profile({'name': 'Outside scope'})
    from uuid import uuid4
    with pytest.raises(ValidationError, match='access_denied'):
        TeamRepository(store).reserve(access.session_id, second['id'], 'start', str(uuid4()),
                                      owner='test', mode='headless')


def test_key_expiry_ip_password_reset_and_viewer_restrictions(workspace):
    store, admin, operator, _, first, _ = workspace
    keys = ApiKeys(store)
    session = store.login('op', 'member-password-1')
    viewer = store.login('reader', 'member-password-2')
    with pytest.raises(ValidationError, match='access_denied'):
        keys.create(viewer['session_id'], {'name': 'Viewer'})
    for changes in ({'profile_ids': []}, {'allowed_ips': ['invalid']}, {'expires_at': 'yesterday'}):
        with pytest.raises(ValidationError):
            keys.create(session['session_id'], {'name': 'Invalid', **changes})
    expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    created = keys.create(session['session_id'], {'name': 'Local', 'allowed_ips': ['127.0.0.0/8'], 'expires_at': expiry})
    with pytest.raises(ValidationError, match='access_denied'):
        keys.authenticate(created['token'], '192.0.2.1')
    keys.authenticate(created['token'], '::ffff:127.0.0.1')
    expired = keys.create(session['session_id'], {'name': 'Expires soon', 'expires_at': expiry})
    expired_access = keys.authenticate(expired['token'], '127.0.0.1')
    with store._write() as (connection, _):
        row = connection.execute('SELECT data FROM team_sessions WHERE id = ?', (expired_access.session_id,)).fetchone()
        record = json.loads(row['data'])
        store._touch_session(connection, record,
                             expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
    with pytest.raises(ValidationError, match='session_expired'):
        keys.authenticate(expired['token'], '127.0.0.1')
    store.reset_member_password(admin['id'], operator['id'], 'changed-member-password')
    with pytest.raises(ValidationError, match='session_expired'):
        keys.authenticate(created['token'], '127.0.0.1')


def test_network_keys_own_leases_and_revoke_only_their_pages(workspace):
    store, _, _, _, first, _ = workspace
    application = create_app(store.root, origin=ORIGIN, browser_mode='headless',
                             engine_factory=engine_factory,
                             automation={'host': '127.0.0.1', 'port': 0, 'origin': 'http://127.0.0.1'})
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = login(client, 'op', 'member-password-1')
        one = mutation(client, csrf, '/api-keys', {'name': 'One'}).json()
        two = mutation(client, csrf, '/api-keys', {'name': 'Two'}).json()
        assert one['connection']['protocolVersion'] == 1
        bridge = application.state.team.automation
        assert not bridge.token and not (store.root / 'automation.json').exists()

        async def call(token, method, route, body=None, extra=None):
            return await request(bridge, method, route, body, token=False,
                                 extra={'Authorization': 'Bearer ' + token, **(extra or {})})

        token, other = one['connection']['token'], two['connection']['token']
        assert client.portal.call(call, token, 'POST', '/profiles/' + first['id'] + '/start', {'headless': True})[0] == 200
        _, lease = client.portal.call(call, token, 'POST', '/leases', {'profileId': first['id']})
        route = '/leases/' + lease['leaseId']
        assert client.portal.call(call, token, 'POST', route + '/pages')[0] == 200
        for action, method in (('', 'DELETE'), ('/heartbeat', 'POST'), ('/pages', 'POST')):
            assert client.portal.call(call, other, method, route + action)[0] == 404
        assert client.portal.call(call, other, 'POST', '/leases', {'profileId': first['id']})[0] == 409
        assert client.portal.call(call, token, 'GET', '/profiles?token=' + token)[0] == 400
        assert client.portal.call(call, token, 'GET', '/profiles', None, {'Origin': ORIGIN})[0] == 403
        assert client.portal.call(call, token, 'GET', '/profiles', None, {'Host': 'wrong.test'})[0] == 403
        context = application.state.team.engine.sessions[first['id']].context
        assert mutation(client, csrf, '/api-keys/' + one['key']['id'] + '/revoke', {}).status_code == 200
        assert context.pages[1].closed and not context.pages[0].closed
        assert client.portal.call(call, token, 'GET', '/profiles')[0] == 401
        assert client.portal.call(call, other, 'POST', '/leases', {'profileId': first['id']})[0] == 200
        assert application.state.team.repository.latest_run(first['id'])['state'] == 'running'


def test_parallel_requests_and_disconnect_cleanup_keep_key_scope(workspace):
    store, _, _, _, first, second = workspace
    application = create_app(store.root, origin=ORIGIN, browser_mode='headless', engine_factory=engine_factory,
                             automation={'host': '127.0.0.1', 'port': 0, 'origin': 'http://127.0.0.1'})
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = login(client)
        tokens = [mutation(client, csrf, '/api-keys', {'name': profile['name'], 'profile_ids': [profile['id']]})
                  .json()['connection']['token'] for profile in (first, second)]
        bridge = application.state.team.automation
        async def calls():
            return await asyncio.gather(*(request(bridge, 'GET', '/profiles', token=False,
                extra={'Authorization': 'Bearer ' + token}) for _ in range(8) for token in tokens))
        results = client.portal.call(calls)
        assert [[profile['id'] for profile in result['profiles']] for status, result in results] == [
            [profile['id']] for _ in range(8) for profile in (first, second)]
        extra = {'Authorization': 'Bearer ' + tokens[0]}
        client.portal.call(partial(request, bridge, 'POST', '/profiles/' + first['id'] + '/start',
                                   {'headless': True}, token=False, extra=extra))
        _, lease = client.portal.call(partial(request, bridge, 'POST', '/leases',
                                               {'profileId': first['id']}, token=False, extra=extra))
        async def foreign_cleanup():
            from camoufox.studio.automation import _REQUEST_ACCESS, PREFIX
            access = application.state.team.api_keys.authenticate(tokens[1], '127.0.0.1')
            scope = _REQUEST_ACCESS.set(access)
            try:
                await bridge._release_route(PREFIX + '/leases/' + lease['leaseId'] + '/call')
            finally:
                _REQUEST_ACCESS.reset(scope)
        client.portal.call(foreign_cleanup)
        assert lease['leaseId'] in bridge.leases
        assert client.portal.call(partial(request, bridge, 'POST', '/profiles', {'name': 'Outside scope'},
                                          token=False, extra=extra))[0] == 403


def test_members_manage_only_their_keys_and_admin_can_revoke_any(workspace):
    store, admin, _, _, _, _ = workspace
    keys = ApiKeys(store)
    administrator = store.login('root', 'admin-password-1')['session_id']
    operator = store.login('op', 'member-password-1')['session_id']
    first = keys.create(administrator, {'name': 'Admin worker'})
    second = keys.create(operator, {'name': 'Operator worker'})
    assert [item['id'] for item in keys.list(operator)] == [second['key']['id']]
    assert {item['id'] for item in keys.list(administrator)} == {first['key']['id'], second['key']['id']}
    with pytest.raises(ValidationError, match='access_denied'):
        keys.revoke(operator, first['key']['id'])
    keys.revoke(administrator, second['key']['id'])
    with pytest.raises(ValidationError, match='session_expired'):
        keys.authenticate(second['token'], '127.0.0.1')
    keys.authenticate(first['token'], '127.0.0.1')


def test_network_configuration_shares_bind_tls_and_network_scopes(workspace):
    from camoufox.studio.team.host import configuration
    config = configuration(workspace[0].root, origin='https://team.test:8766', listen='0.0.0.0',
                           cert='certificate.pem', key='private.pem', browser_mode='headless',
                           automation_listen=True, allow_client=['192.0.2.0/24'], private_network_only=True)
    options = config.app.state.team.automation_configuration
    assert (options['host'], options['port'], options['origin']) == ('0.0.0.0', 8767, 'https://team.test:8766')
    assert (options['cert'], options['key']) == ('certificate.pem', 'private.pem')
    assert options['private_network_only'] and str(options['allowed_networks'][0]) == '192.0.2.0/24'
    with pytest.raises(ValueError, match='separate automation port'):
        configuration(workspace[0].root, automation_listen=True, automation_port=8766)
    with pytest.raises(ValueError, match='enabled browser'):
        configuration(workspace[0].root, automation_listen=True)


def test_management_roles_and_secrets_are_not_retrievable(workspace):
    store = workspace[0]
    with TestClient(create_app(store.root, origin=ORIGIN), base_url=ORIGIN) as client:
        csrf = login(client, 'op', 'member-password-1')
        created = mutation(client, csrf, '/api-keys', {'name': 'Worker'})
        assert created.status_code == 409 and created.json()['error'] == 'automation_disabled'
        assert client.get('/team/v1/api-keys').json() == {'keys': [], 'enabled': False}
        csrf = login(client, 'reader', 'member-password-2')
        assert client.get('/team/v1/api-keys').status_code == 403


def test_https_listener_uses_the_web_certificate_with_normal_validation(workspace, tmp_path):
    import ipaddress
    import ssl
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Studio test')])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                   .not_valid_after(now + timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(private, hashes.SHA256()))
    cert_path, key_path = tmp_path / 'listener-cert.pem', tmp_path / 'listener-key.pem'
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
    application = create_app(workspace[0].root, origin=ORIGIN, browser_mode='headless', engine_factory=engine_factory,
                             automation={'host': '127.0.0.1', 'port': 0, 'origin': 'https://127.0.0.1',
                                         'cert': str(cert_path), 'key': str(key_path)})
    with TestClient(application, base_url=ORIGIN) as client:
        csrf = login(client)
        created = mutation(client, csrf, '/api-keys', {'name': 'TLS worker'}).json()
        trust = ssl.create_default_context(cafile=str(cert_path))
        status, profiles = client.portal.call(partial(request, application.state.team.automation, 'GET', '/profiles',
            token=False, extra={'Authorization': 'Bearer ' + created['connection']['token']}, ssl_context=trust))
        assert status == 200 and len(profiles['profiles']) == 2


def test_v4_upgrade_retains_existing_login_and_adds_key_storage(workspace):
    from camoufox.studio.store import ProfileStore
    store = workspace[0]
    opened = store.login('root', 'admin-password-1')
    with store._write() as (connection, _):
        connection.execute('DROP TABLE team_api_keys')
        connection.execute("UPDATE workspace_metadata SET value='4' WHERE key='team_schema_version'")
    upgraded = ProfileStore(store.root)
    assert upgraded.resolve_session(opened['session_id'])['member'] == opened['member']
    assert ApiKeys(upgraded).list(opened['session_id']) == []
