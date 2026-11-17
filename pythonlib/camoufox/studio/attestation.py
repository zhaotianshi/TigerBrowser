"""Explicit same-host native identity stability diagnostics using the Studio collector."""

import asyncio
import copy
import hashlib
import json
import math
import platform
import shutil
from importlib.metadata import version
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import manifest
from .fingerprint_options import SCREEN_OPTIONS
from .store import ProfileStore, StoreError, _json, _now, validate_id
from .workspace import WorkspaceLock

GROUPS = ('navigator', 'screen', 'timezone', 'webgl', 'canvas', 'audio', 'fonts',
          'clientRects', 'emojiCanvas', 'fontAvailability', 'speechVoices')
PROBE = Path(__file__).parent / 'team/probe.js'
TEXT_FIELDS = {
    'navigator': ('userAgent', 'platform', 'oscpu', 'vendor', 'buildID', 'doNotTrack'),
    'timezone': ('timezone',), 'webgl': ('vendor', 'renderer', 'unmaskedVendor', 'unmaskedRenderer'),
    'canvas': ('dataUrlPrefix',),
}
NUMBER_FIELDS = {
    'navigator': ('hardwareConcurrency', 'maxTouchPoints'),
    'screen': ('width', 'height', 'colorDepth', 'pixelDepth', 'devicePixelRatio', 'availWidth',
               'availHeight', 'innerWidth', 'innerHeight', 'outerWidth', 'outerHeight'),
    'timezone': ('offset',), 'webgl': ('maxTextureSize',), 'audio': ('sampleRate',), 'fonts': ('measureWidth',),
}
SCREEN_DIMENSIONS = ('width', 'height', 'availWidth', 'availHeight', 'innerWidth', 'innerHeight',
                     'outerWidth', 'outerHeight')


def stable(sample, *, include_timezone=True, editable_screen=False):
    if not isinstance(sample, dict) or any(not isinstance(sample.get(key), dict) for key in GROUPS):
        raise StoreError('attestation_measurement_failed')
    result = {key: copy.deepcopy(sample[key]) for key in GROUPS}
    for group, fields in TEXT_FIELDS.items():
        if any(not isinstance(result[group].get(key), str) for key in fields):
            raise StoreError('attestation_measurement_failed')
    for group, fields in NUMBER_FIELDS.items():
        if any(type(result[group].get(key)) not in (int, float) or not math.isfinite(result[group][key])
               for key in fields):
            raise StoreError('attestation_measurement_failed')
    if (any(result['screen'][key] <= 0 for key in NUMBER_FIELDS['screen'])
            or result['navigator']['hardwareConcurrency'] <= 0 or result['navigator']['maxTouchPoints'] < 0
            or result['webgl']['maxTextureSize'] <= 0 or not result['webgl']['renderer']
            or result['audio']['sampleRate'] <= 0 or result['fonts']['measureWidth'] <= 0
            or not result['canvas']['dataUrlPrefix'].startswith('data:image/png;base64,')):
        raise StoreError('attestation_measurement_failed')
    for group, key in (('fontAvailability', 'detected'), ('speechVoices', 'names')):
        values = result[group].get(key)
        if (not isinstance(values, list) or any(not isinstance(value, str) for value in values)
                or type(result[group].get('count')) is not int or result[group]['count'] != len(values)):
            raise StoreError('attestation_measurement_failed')
    for key in ('canvas', 'audio', 'fonts', 'clientRects', 'emojiCanvas', 'fontAvailability', 'speechVoices'):
        value = result[key].get('hash')
        if not isinstance(value, str) or value in ('error', 'no-context', 'n/a'):
            raise StoreError('attestation_measurement_failed')
        if not value and key not in ('fontAvailability', 'speechVoices'):
            raise StoreError('attestation_measurement_failed')
    # Wall-clock time and realtime analyser timing are not identity measurements.
    result['timezone'].pop('localTime', None)
    result['timezone'].pop('expected_timezone', None)
    audio = result['audio']
    result['audio'] = {key: audio[key] for key in ('hash', 'sampleRate') if key in audio}
    methods = audio.get('methods')
    if (not isinstance(methods, dict)
            or any(methods.get(key) != audio['hash'] for key in ('getChannelData', 'copyFromChannel'))):
        raise StoreError('attestation_measurement_failed')
    if not include_timezone:
        del result['timezone']
    if editable_screen:
        for key in SCREEN_DIMENSIONS:
            del result['screen'][key]
    return hashlib.sha256(_json(result).encode('utf-8')).hexdigest()


def compare(samples):
    if len(samples) < 2:
        raise StoreError('attestation_measurement_failed')
    digests = [stable(sample) for sample in samples]
    if len(set(digests)) != 1:
        raise StoreError('attestation_drift')
    return {'checks': len(GROUPS), 'digest': digests[0]}


def binding(profile, *, legacy=False, editable_screen=False):
    fields = ('identity', 'os', 'locale', 'fingerprint', 'block_images', 'humanize')
    if legacy:
        # Version 1 hashes used this exact field order.
        fields = ('identity', 'os', 'locale', 'timezone', 'fingerprint', 'proxy_id', 'block_images', 'humanize')
    values = {key: profile.get(key) for key in fields}
    if editable_screen:
        values['fingerprint'] = {key: value for key, value in values['fingerprint'].items()
                                 if key not in SCREEN_OPTIONS}
    return hashlib.sha256(_json(values).encode()).hexdigest()


def runtime(store, *, create_host=False):
    record = manifest.capture(store, create_host=create_host)
    record['mode'] = 'remote'
    record['collector'] = manifest._hash(PROBE)
    if platform.system() == 'Linux':
        record['packages']['python-xlib'] = version('python-xlib')
        tools = {}
        from .team.kasm import prerequisites
        kasm = prerequisites()
        for name in ('bwrap', 'xdotool', 'xauth', 'xclip'):
            path = shutil.which(name)
            if not path:
                raise StoreError('runtime_unavailable')
            tools[name] = manifest._hash(Path(path).resolve())
        for name in ('Xkasmvnc', 'kasmvncpasswd', 'studio-kasmvnc-runtime.json'):
            tools[name] = manifest._hash(kasm / name)
        record['native_tools'] = tools
        record['native_font_directories'] = {
            str(path): manifest._tree(path, allowed_roots=(Path('/usr/share/fontconfig'),))
            for path in (Path('/usr/share/fonts'), Path('/etc/fonts'))
            if path.is_dir()
        }
    else:
        raise StoreError('Remote execution requires Linux with KasmVNC.')
    return record


async def collect(context, *, expected_timezone=None):
    page = await context.new_page()
    try:
        source = PROBE.read_text(encoding='utf-8')
        # A website observes its content realm, not Juggler's cross-compartment wrappers.
        # Run only in a new synthetic document; leave main-world automation off.
        await page.set_content('<!doctype html><html><head><meta charset="utf-8"></head><body>'
                               '<script>' + source.replace('</script', '<\\/script') + '''
            (async () => {
                const output = document.createElement('pre');
                output.id = 'camoufox-native-measurement';
                try {
                    const sample = await CamoufoxProbe.collectFingerprints();
                    const expected = ''' + json.dumps(expected_timezone).replace('<', '\\u003c') + ''';
                    if (expected !== null) {
                        sample.timezone.expected_timezone = new Intl.DateTimeFormat('en-US', {
                            timeZone: expected
                        }).resolvedOptions().timeZone;
                    }
                    output.textContent = JSON.stringify(sample);
                }
                catch { output.textContent = 'null'; }
                document.body.appendChild(output);
            })();</script></body></html>''')
        raw = await page.locator('#camoufox-native-measurement').text_content(timeout=20000)
        return json.loads(raw)
    finally:
        await page.close()


def verify_timezone(sample, expected_timezone, before, after):
    observed = sample['timezone']
    if expected_timezone is not None and observed.get('expected_timezone') != observed['timezone']:
        raise StoreError('attestation_timezone_mismatch')
    try:
        selected = ZoneInfo(expected_timezone or observed['timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise StoreError('attestation_timezone_mismatch') from None
    offsets = {-when.astimezone(selected).utcoffset().total_seconds() / 60 for when in (before, after)}
    if observed['offset'] not in offsets:
        raise StoreError('attestation_timezone_mismatch')


async def measure(context, expected_timezone):
    before = datetime.now(timezone.utc)
    sample = await collect(context, expected_timezone=expected_timezone)
    after = datetime.now(timezone.utc)
    stable(sample)
    verify_timezone(sample, expected_timezone, before, after)
    return sample


def certificate(store, profile_id):
    with store._connect() as connection:
        row = connection.execute('SELECT value FROM workspace_metadata WHERE key=?',
                                 ('remote_attestation:' + validate_id(profile_id),)).fetchone()
    return json.loads(row[0]) if row else None


def check(store, profile, *, required=False):
    expected = certificate(store, profile['id'])
    if expected is None:
        if required:
            raise StoreError('attestation_required')
        return
    if (expected.get('format') not in (1, 2, 3)
            or expected.get('binding') != binding(profile, legacy=expected.get('format') == 1,
                                                 editable_screen=expected.get('format') == 3)
            or expected.get('runtime') != runtime(store)):
        raise StoreError('attestation_runtime_mismatch')
    return expected


async def verify(store, profile, context, *, required=False, expected_timezone=None):
    expected = await asyncio.to_thread(check, store, profile, required=required)
    if expected is None:
        return
    if expected['format'] in (2, 3):
        sample = await measure(context, expected_timezone)
        if expected['format'] == 3:
            config = profile['identity']['config']
            options = profile['fingerprint']
            dimensions = tuple(options['screen_' + axis] if options['screen_mode'] == 'custom'
                               else config.get('screen.' + axis) for axis in ('width', 'height'))
            observed = sample['screen']
            if (dimensions != (observed['width'], observed['height'])
                    or any(not (observed['inner' + axis] <= observed['outer' + axis]
                                <= observed[axis.lower()]) for axis in ('Width', 'Height'))
                    or any(observed['avail' + axis] > observed[axis.lower()] for axis in ('Width', 'Height'))):
                raise StoreError('attestation_screen_mismatch')
        digest = stable(sample, include_timezone=False, editable_screen=expected['format'] == 3)
    else:
        digest = stable(await collect(context))
    if expected.get('digest') != digest:
        raise StoreError('attestation_drift')
    if expected['format'] == 2:
        # Upgrade only after a fresh measurement passes the original complete certificate.
        upgraded = {**expected, 'format': 3, 'binding': binding(profile, editable_screen=True),
                    'digest': stable(sample, include_timezone=False, editable_screen=True)}
        with store._write() as (connection, _):
            key = 'remote_attestation:' + validate_id(profile['id'])
            row = connection.execute('SELECT value FROM workspace_metadata WHERE key=?', (key,)).fetchone()
            current = store._get(connection, 'profiles', profile['id'])
            if row and json.loads(row[0]) == expected and binding(current) == expected['binding']:
                connection.execute('UPDATE workspace_metadata SET value=? WHERE key=?', (_json(upgraded), key))
                store._audit(connection, 'runtime.remote_resolution_enabled', profile['id'])


async def register(root, profile_id, *, boundary):
    from .engine import BrowserEngine
    from .team.backend import select

    root = Path(root).resolve()
    if not (root / 'studio.sqlite3').is_file():
        raise StoreError('workspace_missing')
    native = select()
    lock = WorkspaceLock(root, boundary=boundary, execution_owner=str(uuid4()))
    lock.acquire()
    engine = None
    try:
        store = ProfileStore(root)
        profile = store.get_profile(profile_id)
        if profile['identity'] is None:
            raise StoreError('attestation_identity_missing')
        with store._connect() as connection:
            if connection.execute("SELECT 1 FROM team_runs WHERE state != 'stopped'").fetchone():
                raise StoreError('recovery_required')
            if connection.execute("SELECT 1 FROM workspace_metadata WHERE key='execution_barrier'").fetchone():
                raise StoreError('workspace_quarantined')
        before = await asyncio.to_thread(runtime, store, create_host=True)
        engine = BrowserEngine(store, lambda *_: None, lambda *_: None, lambda *_: None,
                               open_start_pages=False)
        engine.native_factory = native
        samples = []
        for _ in range(2):
            await engine.start(profile_id, headless=False)
            session = engine.sessions[profile_id]
            await asyncio.wait_for(session.ready.wait(), timeout=90)
            if session.error or session.context is None:
                raise StoreError('attestation_launch_failed')
            samples.extend([await measure(session.context, session.launch_timezone),
                            await measure(session.context, session.launch_timezone)])
            await engine.stop(profile_id)
            await session.task
            if engine.recovery_required:
                raise StoreError('recovery_required')
        result = compare(samples)
        if before != await asyncio.to_thread(runtime, store):
            raise StoreError('attestation_runtime_mismatch')
        record = dict(format=3, binding=binding(store.get_profile(profile_id), editable_screen=True), runtime=before,
                      digest=stable(samples[0], include_timezone=False, editable_screen=True),
                      checks=result['checks'], accepted_at=_now())
        with store._write() as (connection, _):
            connection.execute('INSERT OR REPLACE INTO workspace_metadata VALUES (?, ?)',
                               ('remote_attestation:' + validate_id(profile_id), _json(record)))
            store._audit(connection, 'runtime.remote_accepted', profile_id)
        return {'checks': result['checks'], 'starts': 2, 'reads': 4, 'accepted_at': record['accepted_at']}
    finally:
        try:
            if engine is not None:
                await engine.shutdown()
        except BaseException:
            raise
        else:
            lock.release()
