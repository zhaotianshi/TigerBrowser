"""Member-owned automation credentials with live authorization and immutable scope."""

import hashlib
import json
import secrets
from datetime import datetime, timezone
from uuid import uuid4

from .access import MemberStore
from .store import ValidationError, _json, _now, validate_id
from .team.transport import allowed_client, client_allowlist


class ApiKeyAccess(MemberStore):
    def __init__(self, store, session):
        super().__init__(store, session)
        self.api_key_id = session['api_key_id']

    def require_admin(self):
        session = super().require_admin()
        if session['profile_ids'] is not None:
            raise ValidationError('access_denied')
        return session

    def require_profile(self, profile_id, permission):
        super().require_profile(profile_id, permission)
        scope = self.resolve()['profile_ids']
        if scope is not None and profile_id not in scope:
            raise ValidationError('access_denied')

    def list_profiles(self, archived=False):
        profiles = super().list_profiles(archived)
        scope = self.resolve()['profile_ids']
        return profiles if scope is None else [profile for profile in profiles if profile['id'] in scope]


class ApiKeys:
    def __init__(self, store):
        self.store = store

    def _actor(self, connection, session_id):
        session = self.store._resolve_session(connection, session_id)
        if session is None:
            raise ValidationError('session_expired')
        if session['member']['role'] not in ('admin', 'operator') or session.get('api_key_id'):
            raise ValidationError('access_denied')
        return session

    @staticmethod
    def _public(key, session):
        return {**key, 'revoked_at': key['revoked_at'] or session['revoked_at']}

    def list(self, session_id):
        with self.store._write() as (connection, _):
            actor = self._actor(connection, session_id)['member']
            rows = connection.execute(
                'SELECT k.data, s.data AS session FROM team_api_keys k '
                'JOIN team_sessions s ON s.id = k.session_id '
                'WHERE ? OR k.member_id = ? ORDER BY k.rowid DESC',
                (actor['role'] == 'admin', actor['id']))
            return [self._public(json.loads(row['data']), json.loads(row['session'])) for row in rows]

    def create(self, session_id, data):
        if not isinstance(data, dict) or set(data) - {'name', 'profile_ids', 'allowed_ips', 'expires_at'}:
            raise ValidationError('invalid_request')
        name = data.get('name')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 120
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ValidationError('invalid_request')
        scope = data.get('profile_ids')
        if scope is not None:
            if not isinstance(scope, list) or not 1 <= len(scope) <= 10000:
                raise ValidationError('invalid_request')
            for identifier in scope:
                validate_id(identifier)
            scope = sorted(set(scope))
        values = data.get('allowed_ips', [])
        if not isinstance(values, list) or len(values) > 64:
            raise ValidationError('invalid_request')
        try:
            networks = client_allowlist(values)
        except ValueError:
            raise ValidationError('invalid_request') from None
        expiry = data.get('expires_at')
        if expiry is not None:
            try:
                timestamp = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                if timestamp.tzinfo is None or timestamp <= datetime.now(timezone.utc):
                    raise ValueError
                expiry = timestamp.astimezone(timezone.utc).isoformat(timespec='microseconds')
            except (ValueError, TypeError, AttributeError):
                raise ValidationError('invalid_request') from None
        with self.store._write() as (connection, _):
            session = self._actor(connection, session_id)
            member = session['member']
            if scope is not None:
                for identifier in scope:
                    if connection.execute('SELECT 1 FROM profiles WHERE id = ?', (identifier,)).fetchone() is None:
                        raise ValidationError('invalid_request')
                    if member['role'] != 'admin' and 'operate' not in session['permissions'].get(identifier, ()):
                        raise ValidationError('access_denied')
            token = secrets.token_urlsafe(32)
            identifier, access_id, now = str(uuid4()), str(uuid4()), _now()
            key = {'id': identifier, 'member_id': member['id'], 'member_name': member['name'],
                   'name': name.strip(), 'profile_ids': scope, 'allowed_ips': [str(item) for item in networks],
                   'created_at': now, 'expires_at': expiry, 'last_used_at': None, 'revoked_at': None}
            access = {'id': access_id, 'member_id': member['id'], 'permission_version': member['permission_version'],
                      'created_at': now, 'expires_at': expiry, 'revoked_at': None,
                      'api_key_id': identifier, 'profile_ids': scope}
            connection.execute('INSERT INTO team_sessions VALUES (?, ?, ?)', (access_id, member['id'], _json(access)))
            connection.execute('INSERT INTO team_api_keys VALUES (?, ?, ?, ?, ?)',
                               (identifier, member['id'], hashlib.sha256(token.encode()).hexdigest(), access_id, _json(key)))
            self.store._audit(connection, 'api_key.created', actor_id=member['id'], actor_name=member['name'],
                              target_kind='api_key', target_id=identifier)
            return {'key': key, 'token': token}

    def authenticate(self, token, peer):
        if not isinstance(token, str) or len(token) != 43 or not token.isascii():
            raise ValidationError('session_expired')
        with self.store._write() as (connection, _):
            row = connection.execute('SELECT data, session_id FROM team_api_keys WHERE token_hash = ?',
                                     (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row is None:
                raise ValidationError('session_expired')
            key = json.loads(row['data'])
            session = self.store._resolve_session(connection, row['session_id'])
            if key['revoked_at'] or session is None:
                raise ValidationError('session_expired')
            if key['allowed_ips'] and not allowed_client(peer, client_allowlist(key['allowed_ips'])):
                raise ValidationError('access_denied')
            key['last_used_at'] = _now()
            connection.execute('UPDATE team_api_keys SET data = ? WHERE id = ?', (_json(key), key['id']))
        return ApiKeyAccess(self.store, session)

    def revoke(self, session_id, identifier):
        validate_id(identifier)
        with self.store._write() as (connection, _):
            actor = self._actor(connection, session_id)['member']
            row = connection.execute('SELECT data, session_id FROM team_api_keys WHERE id = ?', (identifier,)).fetchone()
            if row is None:
                raise ValidationError('access_denied')
            key = json.loads(row['data'])
            if actor['role'] != 'admin' and key['member_id'] != actor['id']:
                raise ValidationError('access_denied')
            if key['revoked_at'] is None:
                key['revoked_at'] = _now()
                connection.execute('UPDATE team_api_keys SET data = ? WHERE id = ?', (_json(key), identifier))
                session = json.loads(connection.execute('SELECT data FROM team_sessions WHERE id = ?',
                                                       (row['session_id'],)).fetchone()['data'])
                self.store._touch_session(connection, session, revoked_at=key['revoked_at'])
                self.store._audit(connection, 'api_key.revoked', actor_id=actor['id'], actor_name=actor['name'],
                                  target_kind='api_key', target_id=identifier)
