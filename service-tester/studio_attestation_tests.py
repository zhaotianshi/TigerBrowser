"""Opt-in native measurement diagnostics; run inside the same kernel boundary as the host."""

import asyncio
import copy
import json

import pytest

from camoufox.studio import attestation
from camoufox.studio.engine import BrowserEngine
from camoufox.studio.team.boundary import enter
from camoufox.studio.team.kasm import KasmSession
from studio_runtime_tests import workspace

pytestmark = pytest.mark.parametrize('workspace', [{
    'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080,
}], indirect=True)


async def launch(engine, profile_id):
    await engine.start(profile_id, headless=False)
    session = engine.sessions[profile_id]
    await asyncio.wait_for(session.ready.wait(), 90)
    return session


@pytest.mark.asyncio
async def test_native_measurement_is_diagnostic_and_never_blocks_restart(workspace):
    store, _, _, profile = workspace
    engine = BrowserEngine(store, lambda *_: None, lambda *_: None, lambda *_: None,
                           open_start_pages=False)
    engine.native_factory = KasmSession
    try:
        first = await launch(engine, profile['id'])
        assert first.error is None, first.error
    finally:
        await engine.shutdown()
    identity = copy.deepcopy(store.get_profile(profile['id'])['identity'])
    result = await attestation.register(store.root, profile['id'], boundary=enter())
    assert result['checks'] == 11 and result['starts'] == 2 and result['reads'] == 4
    assert store.get_profile(profile['id'])['identity'] == identity
    assert not (store.root / 'workspace.lock').exists()

    with store._write() as (connection, _):
        key = 'remote_attestation:' + profile['id']
        record = json.loads(connection.execute('SELECT value FROM workspace_metadata WHERE key=?', (key,)).fetchone()[0])
        record['runtime']['collector'] = 'mismatching-synthetic-runtime'
        connection.execute('UPDATE workspace_metadata SET value=? WHERE key=?', (json.dumps(record), key))
    restarted = BrowserEngine(store, lambda *_: None, lambda *_: None, lambda *_: None,
                              open_start_pages=False)
    restarted.native_factory = KasmSession
    try:
        session = await launch(restarted, profile['id'])
        assert session.error is None and session.context is not None
    finally:
        await restarted.shutdown()
    assert store.get_profile(profile['id'])['identity'] == identity
