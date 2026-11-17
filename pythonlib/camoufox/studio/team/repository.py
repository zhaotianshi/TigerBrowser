"""HTTP capabilities and execution reservations in the existing workspace database."""

import hashlib
import json
import secrets
from uuid import uuid4

from ..store import ValidationError, _json, _now, validate_id

ACTIVE_STATES = ('starting', 'running', 'stopping', 'recovery_required')


class TeamRepository:
    def __init__(self, store):
        self.store = store

    def issue_cookie(self, session_id):
        token = secrets.token_hex(32)
        with self.store._write() as (connection, _):
            self.actor(connection, session_id)
            connection.execute('INSERT INTO team_http_sessions VALUES (?, ?)',
                               (hashlib.sha256(token.encode()).hexdigest(), session_id))
        return token

    def session_id(self, token):
        if not isinstance(token, str) or len(token) != 64:
            return None
        with self.store._connect() as connection:
            row = connection.execute('SELECT session_id FROM team_http_sessions WHERE token_hash = ?',
                                     (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            return row['session_id'] if row else None

    def actor(self, connection, session_id, profile_id=None, *, admin=False):
        session = self.store._resolve_session(connection, session_id)
        if session is None:
            raise ValidationError('session_expired')
        member = session['member']
        if admin and member['role'] != 'admin':
            raise ValidationError('admin_required')
        if profile_id is not None:
            validate_id(profile_id)
            if session.get('profile_ids') is not None and profile_id not in session['profile_ids']:
                raise ValidationError('access_denied')
            if member['role'] != 'admin' and 'operate' not in self.store._member_permissions(
                    connection, member['id']).get(profile_id, ()):
                raise ValidationError('access_denied')
        return member

    def unresolved_runs(self):
        with self.store._connect() as connection:
            return [json.loads(row['data']) for row in connection.execute(
                "SELECT data FROM team_runs WHERE state != 'stopped'")]

    def latest_run(self, profile_id):
        with self.store._connect() as connection:
            row = connection.execute('SELECT data FROM team_runs WHERE profile_id = ? ORDER BY rowid DESC LIMIT 1',
                                     (profile_id,)).fetchone()
            return json.loads(row['data']) if row else None

    def active_checkout(self, profile_id):
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT data FROM team_checkouts WHERE profile_id = ? AND state IN ('checked_out', 'returning')",
                (profile_id,)).fetchone()
            return json.loads(row['data']) if row else None

    def ensure_editable(self, profile_id, *, configuration_only=False):
        run = self.latest_run(profile_id)
        if run and run['state'] in ACTIVE_STATES:
            raise ValidationError('profile_busy')
        # Copying configuration never touches the checked-out site data.
        if not configuration_only and self.active_checkout(profile_id):
            raise ValidationError('profile_checked_out')

    def require_starting(self, run_id, owner):
        with self.store._connect() as connection:
            row = connection.execute('SELECT state FROM team_runs WHERE id = ? AND owner = ?',
                                     (run_id, owner)).fetchone()
            if row is None or row['state'] != 'starting':
                raise ValidationError('start_cancelled')

    def reserve(self, session_id, profile_id, operation, request_id, *, owner, mode, run_id=None, capacity=None,
                main_world_eval=None):
        validate_id(request_id)
        if operation not in ('start', 'stop') or (operation == 'start' and mode not in ('headless', 'desktop', 'remote')):
            raise ValidationError('invalid_request')
        selected = 'kasmvnc' if mode == 'remote' and operation == 'start' else None
        # Authorization, idempotency and ownership are decided together, before any browser side effect.
        with self.store._write() as (connection, _):
            member = self.actor(connection, session_id, profile_id)
            previous = connection.execute('SELECT data FROM team_commands WHERE member_id = ? AND request_id = ?',
                                          (member['id'], request_id)).fetchone()
            if previous:
                command = json.loads(previous['data'])
                signature = {'profile_id': profile_id, 'operation': operation,
                             'mode': mode if operation == 'start' else command['signature']['mode'],
                             'run_id': run_id}
                previous_signature = dict(command['signature'])
                if operation == 'start' and main_world_eval is True:
                    signature['main_world_eval'] = True
                if operation == 'start' and mode == 'remote':
                    signature['display_backend'] = selected
                if previous_signature != signature:
                    raise ValidationError('request_conflict')
                return command, False
            profile = self.store._get(connection, 'profiles', profile_id)
            row = connection.execute("SELECT data FROM team_runs WHERE profile_id = ? AND state != 'stopped'",
                                     (profile_id,)).fetchone()
            run = json.loads(row['data']) if row else None
            if operation == 'start':
                if run:
                    raise ValidationError('profile_busy')
                if connection.execute(
                        "SELECT 1 FROM team_checkouts WHERE profile_id = ? AND state IN ('checked_out', 'returning')",
                        (profile_id,)).fetchone():
                    raise ValidationError('profile_checked_out')
                if profile['archived']:
                    raise ValidationError('profile_archived')
                configured_capacity = self.store._get_settings(connection)['max_running']
                capacity = min(configured_capacity, capacity) if capacity is not None else configured_capacity
                count = connection.execute("SELECT COUNT(*) FROM team_runs WHERE state != 'stopped'").fetchone()[0]
                if count >= capacity:
                    raise ValidationError('capacity_reached')
                run = {'id': str(uuid4()), 'profile_id': profile_id, 'owner': owner, 'state': 'starting',
                       'mode': mode, 'created_at': _now(), 'updated_at': _now(), 'error': None}
                if selected is not None:
                    run['display_backend'] = selected
                connection.execute('INSERT INTO team_runs VALUES (?, ?, ?, ?, ?)',
                                   (run['id'], profile_id, owner, run['state'], _json(run)))
            elif not run or run['id'] != run_id or run['owner'] != owner:
                raise ValidationError('run_conflict')
            elif run['state'] == 'recovery_required':
                raise ValidationError('recovery_required')
            else:
                mode = run['mode']
                run.update(state='stopping', updated_at=_now())
                connection.execute('UPDATE team_runs SET state = ?, data = ? WHERE id = ?',
                                   (run['state'], _json(run), run['id']))
            signature = {'profile_id': profile_id, 'operation': operation, 'mode': mode, 'run_id': run_id}
            if operation == 'start' and main_world_eval is True:
                signature['main_world_eval'] = True
            if selected is not None:
                signature['display_backend'] = selected
            command = {'id': str(uuid4()), 'member_id': member['id'], 'request_id': request_id,
                       'profile_id': profile_id, 'run_id': run['id'], 'operation': operation,
                       'signature': signature, 'state': 'accepted', 'error': None,
                       'created_at': _now(), 'updated_at': _now()}
            connection.execute('INSERT INTO team_commands VALUES (?, ?, ?, ?, ?, ?)',
                               (command['id'], member['id'], request_id, profile_id, run['id'], _json(command)))
            self.store._audit(connection, 'run.' + operation + '_requested', actor_id=member['id'],
                              actor_name=member['name'], target_kind='run', target_id=run['id'])
            return command, True

    def command(self, command_id):
        validate_id(command_id)
        with self.store._connect() as connection:
            row = connection.execute('SELECT data FROM team_commands WHERE id = ?', (command_id,)).fetchone()
            if row is None:
                raise ValidationError('access_denied')
            return json.loads(row['data'])

    def finish_command(self, command_id, *, error=None):
        with self.store._write() as (connection, _):
            row = connection.execute('SELECT data FROM team_commands WHERE id = ?', (command_id,)).fetchone()
            command = json.loads(row['data'])
            command.update(state='failed' if error else 'succeeded', error=error, updated_at=_now())
            connection.execute('UPDATE team_commands SET data = ? WHERE id = ?', (_json(command), command_id))
            self.store._audit(connection, 'run.command_finished', actor_id=command['member_id'],
                              target_kind='run', target_id=command['run_id'],
                              result='error' if error else 'ok', detail=error or '')

    def update_run(self, run_id, owner, state, *, error=None):
        if state not in (*ACTIVE_STATES, 'stopped'):
            raise ValidationError('invalid_request')
        with self.store._write() as (connection, _):
            row = connection.execute('SELECT data FROM team_runs WHERE id = ? AND owner = ?',
                                     (run_id, owner)).fetchone()
            if row is None:
                raise ValidationError('run_conflict')
            run = json.loads(row['data'])
            if run['state'] in ('stopped', 'recovery_required'):
                return
            if state == 'running' and run['state'] != 'starting':
                return
            run.update(state=state, error=error, updated_at=_now())
            connection.execute('UPDATE team_runs SET state = ?, data = ? WHERE id = ?',
                               (state, _json(run), run_id))
            self.store._audit(connection, 'run.' + state, target_kind='run', target_id=run_id,
                              result='error' if error else 'ok', detail=error or '')
