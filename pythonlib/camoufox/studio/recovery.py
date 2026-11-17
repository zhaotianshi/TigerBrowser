"""Explicit local recovery after a kernel boundary proves every old child exited."""

import json
import os
from pathlib import Path
from uuid import UUID

from .store import ProfileStore, StoreError, _json, _now
from .team.boundary import stopped


def parse_record(raw):
    """Validate the same ownership evidence in active markers and retained receipts."""
    try:
        if len(raw) > 16384:
            raise ValueError
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError
        if record.get('version') != 2 or not isinstance(record.get('boundary'), dict):
            raise StoreError('recovery_boundary_missing')
        if (not isinstance(record.get('execution_owner'), str)
                or str(UUID(record['execution_owner'])) != record['execution_owner']
                or not isinstance(record.get('owner'), str) or len(record['owner']) != 32
                or any(char not in '0123456789abcdef' for char in record['owner'])):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise StoreError('recovery_record_invalid') from None
    return record


def _record(root):
    root = Path(root).resolve()
    marker = root / 'workspace.lock'
    source = marker / 'owner.json'
    if (marker.is_symlink() or not marker.is_dir() or source.is_symlink()
            or not source.is_file() or source.stat().st_size > 16384):
        raise StoreError('recovery_record_invalid')
    raw = source.read_bytes()
    record = parse_record(raw)
    identity = marker.stat()
    return root, marker, raw, record, (identity.st_dev, identity.st_ino)


def inspect(root):
    _, _, _, record, _ = _record(root)
    return {'recoverable': stopped(record['boundary']), 'boundary': record['boundary']['kind']}


def apply(root):
    root, marker, raw, record, identity = _record(root)
    reservation = marker / 'recovering'
    try:
        reservation.mkdir(mode=0o700)
    except FileExistsError:
        raise StoreError('recovery_busy') from None
    try:
        current = marker.stat()
        if (identity != (current.st_dev, current.st_ino) or
                (marker / 'owner.json').read_bytes() != raw or
                set(path.name for path in marker.iterdir()) != {'owner.json', 'recovering'}):
            raise StoreError('recovery_record_changed')
        if not stopped(record['boundary']):
            raise StoreError('owner_boundary_alive')
        receipts = root / 'recovery'
        if receipts.is_symlink():
            raise StoreError('recovery_record_invalid')
        receipts.mkdir(mode=0o700, exist_ok=True)
        receipt = receipts / (record['owner'] + '.lock')
        if receipt.exists() or receipt.is_symlink():
            raise StoreError('recovery_receipt_exists')
        store = ProfileStore(root)
        recovered = 0
        with store._write() as (connection, _):
            if connection.execute("SELECT 1 FROM workspace_metadata WHERE key='execution_barrier'").fetchone():
                raise StoreError('workspace_quarantined')
            runs = connection.execute("SELECT id, owner, data FROM team_runs WHERE state != 'stopped'").fetchall()
            if any(row['owner'] != record['execution_owner'] for row in runs):
                raise StoreError('recovery_owner_mismatch')
            for row in runs:
                run = json.loads(row['data'])
                run.update(state='stopped', error='manager_crashed', updated_at=_now())
                connection.execute('UPDATE team_runs SET state=?, data=? WHERE id=?',
                                   ('stopped', _json(run), row['id']))
                recovered += 1
            for row in connection.execute('SELECT id, data FROM team_commands').fetchall():
                command = json.loads(row['data'])
                if command['state'] == 'accepted':
                    command.update(state='failed', error='result_unknown', updated_at=_now())
                    connection.execute('UPDATE team_commands SET data=? WHERE id=?', (_json(command), row['id']))
            connection.execute('DELETE FROM team_http_sessions')
            # Long-lived API grants are independent of crashed browser/login sessions.
            for row in connection.execute('SELECT DISTINCT member_id FROM team_sessions').fetchall():
                store._revoke_sessions(connection, row['member_id'], preserve_api_keys=True)
            store._audit(connection, 'workspace.recovered')
        # Keep the durable evidence; never delete native browser locks or replay commands.
        os.rename(marker, receipt)
        return {'recovered_runs': recovered, 'receipt': receipt.name}
    finally:
        if reservation.exists():
            reservation.rmdir()
