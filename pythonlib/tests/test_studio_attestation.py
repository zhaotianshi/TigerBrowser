"""Native attestation fails on drift, missing measurements and unaccepted baselines."""

import copy
import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from camoufox.studio import attestation
from camoufox.studio.store import StoreError


def test_manual_proxy_and_timezone_edits_do_not_change_device_binding(tmp_path):
    from camoufox.studio.store import ProfileStore

    store = ProfileStore(tmp_path)
    first = store.save_proxy({'name': 'First route', 'server': 'http://127.0.0.1:8081'})
    second = store.save_proxy({'name': 'Replacement route', 'server': 'http://127.0.0.1:8082'})
    profile = store.save_profile({'name': 'Existing environment', 'proxy_id': first['id']})
    store.set_identity(profile['id'], {'config': {'navigator.platform': 'Win32'}, 'browser_version': '152.0.4'})
    before = store.get_profile(profile['id'])
    after = store.update_profile_checked({'proxy_id': second['id'], 'timezone': 'Asia/Singapore'},
                                         before['id'], before['revision'])
    assert after['identity'] == before['identity']
    assert attestation.binding(after) == attestation.binding(before)


def test_resolution_edit_preserves_fixed_identity_and_acceptance_binding(tmp_path):
    from camoufox.studio.store import ProfileStore

    store = ProfileStore(tmp_path)
    profile = store.save_profile({'name': 'Resizable environment'})
    identity = {'config': {'navigator.platform': 'Win32', 'screen.width': 1920,
                          'screen.height': 1080, 'audio:seed': 123}, 'browser_version': '152.0.4'}
    store.set_identity(profile['id'], identity)
    before = store.get_profile(profile['id'])
    after = store.update_profile_checked({'fingerprint': {**before['fingerprint'],
        'screen_width': 1366, 'screen_height': 768}}, before['id'], before['revision'])
    assert after['identity'] == identity
    assert attestation.binding(after, editable_screen=True) == attestation.binding(before, editable_screen=True)


@pytest.mark.parametrize('changed', ['resolution', 'identity', 'depth', 'dpr', 'wrong_size', 'missing_screen'])
def test_editable_resolution_checks_selected_size_and_keeps_display_device_checks(accepted, monkeypatch, sample, changed):
    from camoufox.studio.store import _json

    store, profile, actual_runtime, _ = accepted
    record = {'format': 3, 'binding': attestation.binding(profile, editable_screen=True),
              'runtime': actual_runtime, 'digest': attestation.stable(sample, include_timezone=False,
                  editable_screen=True), 'checks': 11}
    with store._write() as (connection, _):
        connection.execute('UPDATE workspace_metadata SET value=? WHERE key=?',
                           (_json(record), 'remote_attestation:' + profile['id']))
    profile = store.update_profile_checked({'fingerprint': {**profile['fingerprint'],
        'screen_width': 1366, 'screen_height': 768}}, profile['id'], profile['revision'])
    sample['timezone']['expected_timezone'] = 'Asia/Singapore'
    for key in ('width', 'availWidth', 'innerWidth', 'outerWidth'):
        sample['screen'][key] = 1366
    for key in ('height', 'availHeight', 'innerHeight', 'outerHeight'):
        sample['screen'][key] = 768
    if changed == 'identity':
        profile['identity']['config']['navigator.platform'] = 'different-device'
    elif changed == 'depth':
        sample['screen']['colorDepth'] = 30
    elif changed == 'dpr':
        sample['screen']['devicePixelRatio'] = 2
    elif changed == 'wrong_size':
        sample['screen']['width'] = 1920
    elif changed == 'missing_screen':
        del sample['screen']['height']

    async def collect(*args, **kwargs):
        return sample

    monkeypatch.setattr(attestation, 'collect', collect)
    if changed == 'resolution':
        asyncio.run(attestation.verify(store, profile, object(), required=True, expected_timezone='Asia/Singapore'))
        assert attestation.certificate(store, profile['id']) == record
    else:
        with pytest.raises(StoreError):
            asyncio.run(attestation.verify(store, profile, object(), required=True, expected_timezone='Asia/Singapore'))


@pytest.fixture
def accepted(tmp_path, monkeypatch, sample):
    from camoufox.studio.store import ProfileStore, _json

    store = ProfileStore(tmp_path)
    profile = store.save_profile({'name': 'Measured environment'})
    store.set_identity(profile['id'], {'config': {'navigator.platform': 'Win32', 'screen.width': 1920,
                                                'screen.height': 1080}, 'browser_version': '152.0.4'})
    profile = store.get_profile(profile['id'])
    actual_runtime = {'host_id': 'synthetic-host', 'browser': 'synthetic-browser'}
    record = {'format': 2, 'binding': attestation.binding(profile), 'runtime': actual_runtime,
              'digest': attestation.stable(sample, include_timezone=False), 'checks': 11,
              'accepted_at': '2026-07-01T00:00:00+00:00'}
    with store._write() as (connection, _):
        connection.execute('INSERT INTO workspace_metadata VALUES (?, ?)',
                           ('remote_attestation:' + profile['id'], _json(record)))
    monkeypatch.setattr(attestation, 'runtime', lambda *args, **kwargs: actual_runtime)
    instant = datetime(2026, 7, 1, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(*args):
            return instant

    monkeypatch.setattr(attestation, 'datetime', Clock)
    return store, profile, actual_runtime, record


def test_proxy_replacement_accepts_fresh_geography_without_reaccepting_devices(accepted, monkeypatch, sample):
    store, profile, _, record = accepted
    proxy = store.save_proxy({'name': 'Manually selected route', 'server': 'http://127.0.0.1:8082'})
    profile = store.update_profile_checked({'proxy_id': proxy['id']}, profile['id'], profile['revision'])
    sample['timezone'] = {'timezone': 'America/New_York', 'expected_timezone': 'America/New_York', 'offset': 240}

    async def collect(context, *, expected_timezone=None):
        assert expected_timezone == 'America/New_York'
        return sample

    monkeypatch.setattr(attestation, 'collect', collect)
    asyncio.run(attestation.verify(store, profile, object(), required=True, expected_timezone='America/New_York'))
    upgraded = attestation.certificate(store, profile['id'])
    assert upgraded['format'] == 3
    assert upgraded['accepted_at'] == record['accepted_at']
    assert upgraded['runtime'] == record['runtime']
    # The successful old-format launch enables subsequent edits without maintenance.
    profile = store.update_profile_checked({'fingerprint': {**profile['fingerprint'],
        'screen_width': 1366, 'screen_height': 768}}, profile['id'], profile['revision'])
    for key in ('width', 'availWidth', 'innerWidth', 'outerWidth'):
        sample['screen'][key] = 1366
    for key in ('height', 'availHeight', 'innerHeight', 'outerHeight'):
        sample['screen'][key] = 768
    asyncio.run(attestation.verify(store, profile, object(), required=True, expected_timezone='America/New_York'))
    assert attestation.certificate(store, profile['id']) == upgraded


@pytest.mark.parametrize('changed', ['identity', 'runtime', 'canvas', 'timezone_name', 'timezone_offset', 'missing_timezone'])
def test_proxy_policy_still_rejects_device_runtime_and_timezone_failures(accepted, monkeypatch, sample, changed):
    store, profile, actual_runtime, _ = accepted
    sample['timezone']['expected_timezone'] = 'Asia/Singapore'
    if changed == 'identity':
        profile['identity']['config']['navigator.platform'] = 'different-device'
    elif changed == 'runtime':
        actual_runtime['browser'] = 'different-browser'
    elif changed == 'canvas':
        sample['canvas']['hash'] = 'different-canvas'
    elif changed == 'timezone_name':
        sample['timezone']['timezone'] = 'America/New_York'
    elif changed == 'timezone_offset':
        sample['timezone']['offset'] = 0
    elif changed == 'missing_timezone':
        del sample['timezone']

    async def collect(*args, **kwargs):
        return sample

    monkeypatch.setattr(attestation, 'collect', collect)
    with pytest.raises(StoreError):
        asyncio.run(attestation.verify(store, profile, object(), required=True, expected_timezone='Asia/Singapore'))


def test_legacy_certificates_keep_their_original_binding_and_measurement(accepted, monkeypatch, sample):
    from camoufox.studio.store import _json

    store, profile, actual_runtime, _ = accepted
    record = {'format': 1, 'binding': attestation.binding(profile, legacy=True), 'runtime': actual_runtime,
              'digest': attestation.stable(sample), 'checks': 11}
    with store._write() as (connection, _):
        connection.execute('UPDATE workspace_metadata SET value=? WHERE key=?',
                           (_json(record), 'remote_attestation:' + profile['id']))

    async def collect(*args, **kwargs):
        return sample

    monkeypatch.setattr(attestation, 'collect', collect)
    asyncio.run(attestation.verify(store, profile, object(), required=True))
    profile['timezone'] = 'America/New_York'
    with pytest.raises(StoreError, match='attestation_runtime_mismatch'):
        attestation.check(store, profile, required=True)


def test_timezone_aliases_and_dst_changes_use_the_selected_zone():
    instant = datetime(2026, 7, 1, tzinfo=timezone.utc)
    attestation.verify_timezone({'timezone': {'timezone': 'UTC', 'expected_timezone': 'UTC', 'offset': 0}},
                                'Etc/UTC', instant, instant)
    before = datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc)
    after = datetime(2026, 11, 1, 6, 1, tzinfo=timezone.utc)
    for offset in (240, 300):
        attestation.verify_timezone({'timezone': {'timezone': 'America/New_York',
            'expected_timezone': 'America/New_York', 'offset': offset}}, 'America/New_York', before, after)


@pytest.fixture
def sample():
    return {'navigator': {'platform': 'Linux x86_64', 'userAgent': 'Synthetic test', 'oscpu': '', 'vendor': '',
                          'buildID': '', 'doNotTrack': '', 'hardwareConcurrency': 4, 'maxTouchPoints': 0},
            'screen': dict(width=1920, height=1080, availWidth=1920, availHeight=1080,
                           colorDepth=24, pixelDepth=24, devicePixelRatio=1, innerWidth=1920,
                           innerHeight=1080, outerWidth=1920, outerHeight=1080),
            'timezone': {'timezone': 'Asia/Singapore', 'offset': -480},
            'webgl': {'renderer': 'Synthetic fixture', 'vendor': '', 'unmaskedRenderer': '',
                      'unmaskedVendor': '', 'maxTextureSize': 4096},
            'canvas': {'hash': '11111111', 'dataUrlPrefix': 'data:image/png;base64,'},
            'audio': {'hash': '22222222', 'sampleRate': 44100,
                      'methods': {'analyserFloat': 'runtime', 'getChannelData': '22222222', 'copyFromChannel': '22222222'}},
            'fonts': {'hash': '3.0000', 'measureWidth': 3.0}, 'clientRects': {'hash': '40.000016.0000'},
            'emojiCanvas': {'hash': '33333333'}, 'fontAvailability': {'detected': ['Arial'], 'count': 1, 'hash': 'Arial'},
            'speechVoices': {'names': ['Fixture'], 'count': 1, 'hash': 'Fixture'}}


def test_all_existing_collector_groups_are_bound_to_the_fingerprint(sample):
    assert attestation.stable(sample) == attestation.stable(copy.deepcopy(sample))
    for group in sample:
        changed = copy.deepcopy(sample)
        changed[group]['changed'] = True
        if group != 'audio':
            assert attestation.stable(changed) != attestation.stable(sample)


@pytest.mark.parametrize('group', ['canvas', 'audio', 'fonts', 'emojiCanvas', 'fontAvailability', 'speechVoices'])
def test_failed_or_missing_measurements_do_not_become_a_successful_certificate(sample, group):
    sample[group]['hash'] = 'error'
    with pytest.raises(StoreError, match='attestation_measurement_failed'):
        attestation.stable(sample)


def test_missing_surface_and_changed_restart_are_loud_failures(sample):
    incomplete = copy.deepcopy(sample)
    del incomplete['webgl']
    with pytest.raises(StoreError, match='attestation_measurement_failed'):
        attestation.stable(incomplete)
    changed = copy.deepcopy(sample)
    changed['canvas']['hash'] = 'different'
    with pytest.raises(StoreError, match='attestation_drift'):
        attestation.compare([sample, sample, changed, changed])


def test_realtime_analyser_timing_is_not_mistaken_for_the_offline_identity_hash(sample):
    changed = copy.deepcopy(sample)
    changed['audio']['methods']['analyserFloat'] = 'later-timing'
    assert attestation.stable(sample) == attestation.stable(changed)
    assert attestation.compare([sample, changed])['checks'] == 11


def test_clock_changes_are_not_identity_changes(sample):
    changed = copy.deepcopy(sample)
    sample['timezone']['localTime'] = '01:00:00'
    changed['timezone']['localTime'] = '02:00:00'
    assert attestation.stable(sample) == attestation.stable(changed)


def test_packaged_collector_is_built_from_the_existing_source():
    source = Path(__file__).resolve().parents[2] / 'frontend/studio/src/diagnostics/collectors.ts'
    digest = hashlib.sha256(source.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
    assert attestation.PROBE.read_text(encoding='utf-8').splitlines()[0] == '// Collector source SHA256: ' + digest


@pytest.mark.parametrize('group', ['navigator', 'screen', 'timezone', 'webgl'])
def test_empty_objects_are_not_valid_measurements(sample, group):
    sample[group] = {}
    with pytest.raises(StoreError, match='attestation_measurement_failed'):
        attestation.stable(sample)


def test_disagreeing_offline_audio_reads_are_not_accepted(sample):
    sample['audio']['methods']['copyFromChannel'] = 'different'
    with pytest.raises(StoreError, match='attestation_measurement_failed'):
        attestation.stable(sample)
