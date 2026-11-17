"""Desktop options must describe effective browser behavior, including native modes."""

import sys

import pytest

from camoufox.studio.fingerprint_options import (
    apply_identity_options, apply_network_options, generation_arguments,
    normalize_options, validate_environment,
)


@pytest.mark.parametrize('data', [
    None, [], {'canvas_seed': 3}, {'audio_mode': 'random'},
    {'hardware_concurrency': True}, {'hardware_concurrency': -1},
    {'hardware_concurrency': 257}, {'screen_width': 319}, {'screen_height': 16385},
    {'do_not_track': 'false'}, {'webgl_mode': 'custom'},
    {'webgl_vendor': 'bad\nvalue'}, {'webgpu_mode': 'native'},
])
def test_malformed_or_unsupported_options_fail(data):
    with pytest.raises(ValueError):
        normalize_options(data)


def test_default_options_are_independent_and_idempotent():
    first = normalize_options({})
    assert normalize_options(first) == first
    first['hardware_concurrency'] = 8
    assert normalize_options({})['hardware_concurrency'] == 0


def test_native_modes_remove_all_overrides_including_optional_false_flags():
    config = {
        'navigator.userAgent': 'unchanged', 'audio:seed': 123,
        'AudioContext:sampleRate': 48000, 'AudioContext:outputLatency': 0.02,
        'fonts': ['recorded'], 'voices': [], 'voices:blockIfNotDefined': True,
        'mediaDevices:enabled': True, 'mediaDevices:micros': 2,
        'webGl:vendor': 'recorded', 'webGl2:parameters': {},
        'screen.width': 1920, 'window.devicePixelRatio': 1.0,
    }
    values = normalize_options({key: 'native' for key in (
        'screen_mode', 'fonts_mode', 'webgl_mode', 'audio_mode', 'voices_mode', 'media_mode'
    )})
    prefs = {}
    apply_identity_options(config, prefs, values, 'windows')
    assert set(config) == {'navigator.userAgent', 'navigator.doNotTrack'}
    assert not prefs['webgl.force-enabled']
    assert not prefs['dom.webgpu.enabled']


@pytest.mark.parametrize('key', ['fonts_mode', 'webgl_mode', 'voices_mode', 'media_mode'])
def test_native_os_surfaces_cannot_claim_another_os(key):
    host = {'win32': 'windows', 'darwin': 'macos'}.get(sys.platform, 'linux')
    other = 'macos' if host != 'macos' else 'windows'
    with pytest.raises(ValueError, match='本机'):
        validate_environment(normalize_options({key: 'native'}), other, False)


def test_webrtc_replacement_requires_a_bound_proxy():
    with pytest.raises(ValueError, match='绑定代理'):
        validate_environment(normalize_options({'webrtc_mode': 'proxy'}), 'windows', False)


@pytest.mark.parametrize(('address', 'key', 'ipv6_disabled'), [
    ('198.51.100.4', 'webrtc:ipv4', True), ('2001:db8::4', 'webrtc:ipv6', False),
])
def test_proxy_webrtc_uses_checked_exit_and_clears_old_family(address, key, ipv6_disabled):
    config = {'webrtc:ipv4': 'old', 'webrtc:ipv6': 'old'}
    prefs = {}
    apply_network_options(config, prefs, normalize_options({'webrtc_mode': 'proxy'}), address)
    assert config == {key: address}
    assert prefs['media.peerconnection.enabled']
    assert prefs['network.dns.disableIPv6'] is ipv6_disabled


@pytest.mark.parametrize('address', [None, '', 'invalid', 'user:password@host'])
def test_invalid_proxy_address_is_a_visible_error(address):
    with pytest.raises(ValueError, match='有效出口 IP'):
        apply_network_options({}, {}, normalize_options({'webrtc_mode': 'proxy'}), address)


@pytest.mark.parametrize(('mode', 'enabled'), [('disabled', False), ('native', True)])
def test_native_and_disabled_webrtc_clear_previous_replacement(mode, enabled):
    config, prefs = {'webrtc:ipv4': 'old'}, {'network.dns.disableIPv6': True}
    apply_network_options(config, prefs, normalize_options({'webrtc_mode': mode}), None)
    assert not config
    assert prefs['media.peerconnection.enabled'] is enabled
    assert not prefs['network.dns.disableIPv6']


def test_custom_resolution_must_not_silently_fall_back():
    values = normalize_options({'screen_mode': 'custom', 'screen_width': 1920, 'screen_height': 1080})
    arguments = generation_arguments(values, 'windows')
    bounds = arguments['screen'].as_conditions()
    assert bounds['screen.width'](1920) and not bounds['screen.width'](1921)
    with pytest.raises(ValueError, match='分辨率'):
        apply_identity_options({'screen.width': 1366, 'screen.height': 768}, {}, values, 'windows')


@pytest.mark.parametrize(('width', 'height'), [(1366, 768), (2560, 1440)])
def test_resolution_override_keeps_recorded_chrome_gaps_and_other_device_values(width, height):
    from camoufox.studio.fingerprint_options import apply_screen_options

    config = {'screen.width': 1920, 'screen.height': 1080, 'screen.availWidth': 1920,
              'screen.availHeight': 1040, 'window.outerWidth': 1920, 'window.outerHeight': 1040,
              'window.innerWidth': 1920, 'window.innerHeight': 960, 'window.screenX': 0,
              'window.screenY': 0, 'window.devicePixelRatio': 1, 'screen.colorDepth': 24,
              'audio:seed': 123, 'fonts': ['Recorded font']}
    values = normalize_options({'screen_mode': 'custom', 'screen_width': width, 'screen_height': height})
    apply_screen_options(config, values)
    assert (config['screen.width'], config['screen.height']) == (width, height)
    assert config['screen.availHeight'] == height - 40
    assert config['window.outerHeight'] - config['window.innerHeight'] == 80
    assert config['window.outerWidth'] == width
    assert {key: config[key] for key in ('window.devicePixelRatio', 'screen.colorDepth', 'audio:seed', 'fonts')} == {
        'window.devicePixelRatio': 1, 'screen.colorDepth': 24, 'audio:seed': 123, 'fonts': ['Recorded font']}


def test_gpu_pair_requires_a_record_for_target_os(monkeypatch):
    from camoufox import webgl
    monkeypatch.setattr(webgl, 'firefox_gpus', lambda target: frozenset())
    values = normalize_options({'webgl_mode': 'custom', 'webgl_vendor': 'vendor', 'webgl_renderer': 'gpu'})
    with pytest.raises(ValueError, match='实测记录'):
        generation_arguments(values, 'windows')


def test_incoherent_manual_cpu_is_rejected_instead_of_repaired():
    values = normalize_options({'hardware_concurrency': 2})
    with pytest.raises(ValueError, match='apple-silicon-cores'):
        apply_identity_options({'webGl:renderer': 'Apple M1, or similar'}, {}, values, 'macos')


def test_explicit_cpu_and_dnt_and_webgpu_are_applied():
    values = normalize_options({
        'hardware_concurrency': 12, 'do_not_track': True,
        'webgl_mode': 'native', 'webgpu_mode': 'native',
    })
    config, prefs = {}, {}
    apply_identity_options(config, prefs, values, 'windows')
    assert config['navigator.hardwareConcurrency'] == 12
    assert config['navigator.doNotTrack'] == '1'
    assert prefs['privacy.donottrackheader.enabled'] and prefs['dom.webgpu.enabled']
