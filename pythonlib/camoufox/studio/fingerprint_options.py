"""Validated desktop controls mapped to supported Camoufox fingerprint surfaces."""

from __future__ import annotations

import ipaddress
import sys

from .. import coherence
from ..fingerprints import Screen, clamp_window_dimensions, clamp_window_position


DEFAULTS = {
    'screen_mode': 'identity', 'screen_width': 1920, 'screen_height': 1080,
    'fonts_mode': 'identity', 'webrtc_mode': 'disabled',
    'webgl_mode': 'identity', 'webgl_vendor': '', 'webgl_renderer': '',
    'webgpu_mode': 'disabled', 'audio_mode': 'identity',
    'voices_mode': 'identity', 'media_mode': 'identity',
    'hardware_concurrency': 0, 'do_not_track': False,
}
NEW_PROFILE_DEFAULTS = {**DEFAULTS, 'screen_mode': 'custom'}
SCREEN_OPTIONS = ('screen_mode', 'screen_width', 'screen_height')
MODES = {
    'screen_mode': ('identity', 'native', 'custom'),
    'fonts_mode': ('identity', 'native'),
    'webrtc_mode': ('disabled', 'native', 'proxy'),
    'webgl_mode': ('identity', 'native', 'custom'),
    'webgpu_mode': ('disabled', 'native'),
    'audio_mode': ('identity', 'native'),
    'voices_mode': ('identity', 'native'),
    'media_mode': ('identity', 'native'),
}


def normalize_options(data: dict) -> dict:
    """Supply defaults without accepting ignored fields or coerced types."""
    if not isinstance(data, dict) or data.keys() - DEFAULTS.keys():
        raise ValueError('指纹配置包含不支持的字段。')
    values = {**DEFAULTS, **data}
    for key, choices in MODES.items():
        if not isinstance(values[key], str) or values[key] not in choices:
            raise ValueError(f'指纹选项 {key} 不受支持。')
    for key, minimum, maximum in (
        ('screen_width', 320, 16384), ('screen_height', 240, 16384),
        ('hardware_concurrency', 0, 256),
    ):
        if type(values[key]) is not int or not minimum <= values[key] <= maximum:
            raise ValueError(f'{key} 必须是 {minimum}–{maximum} 之间的整数。')
    for key in ('webgl_vendor', 'webgl_renderer'):
        value = values[key]
        if (not isinstance(value, str) or len(value) > 512
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise ValueError('WebGL 厂商和渲染器必须为不超过 512 个字符的文本。')
        values[key] = value.strip()
    if values['webgl_mode'] == 'custom' and not all(
        values[key] for key in ('webgl_vendor', 'webgl_renderer')
    ):
        raise ValueError('自定义 WebGL 需要同时填写厂商和渲染器。')
    if type(values['do_not_track']) is not bool:
        raise ValueError('Do Not Track 必须是布尔值。')
    if values['webgpu_mode'] == 'native' and values['webgl_mode'] != 'native':
        raise ValueError('真实 WebGPU 必须与真实 WebGL 一起使用，避免报告不同的显卡。')
    return values


def validate_environment(values: dict, target_os: str, has_proxy: bool, *, native_platform_accepted=False) -> None:
    host_os = {'win32': 'windows', 'darwin': 'macos'}.get(sys.platform, 'linux')
    native = [key for key in ('fonts_mode', 'webgl_mode', 'voices_mode', 'media_mode')
              if values[key] == 'native']
    if native and target_os != host_os and not native_platform_accepted:
        raise ValueError('使用真实字体、WebGL、语音或媒体设备时，环境操作系统必须与本机一致。')
    if values['webrtc_mode'] == 'proxy' and not has_proxy:
        raise ValueError('WebRTC 替换模式需要先绑定代理，以获取真实出口地址。')


def generation_arguments(values: dict, target_os: str) -> dict:
    """Constrain the existing generator instead of inventing device records."""
    arguments = {}
    if values['screen_mode'] == 'custom':
        arguments['screen'] = Screen(
            min_width=values['screen_width'], max_width=values['screen_width'],
            min_height=values['screen_height'], max_height=values['screen_height'],
        )
    if values['webgl_mode'] == 'custom':
        from ..webgl import firefox_gpus

        pair = (values['webgl_vendor'], values['webgl_renderer'])
        os_key = {'windows': 'win', 'macos': 'mac', 'linux': 'lin'}[target_os]
        if pair not in firefox_gpus(os_key):
            raise ValueError('该 WebGL 厂商与渲染器组合没有对应操作系统的 Firefox 实测记录。')
        arguments['webgl_config'] = pair
    return arguments


def _remove(config: dict, *prefixes: str) -> None:
    for key in list(config):
        if key.startswith(prefixes):
            del config[key]


def apply_screen_options(config: dict, values: dict) -> None:
    """Resize a saved screen using its recorded taskbar and window chrome gaps."""
    if values['screen_mode'] != 'custom':
        return
    for axis in ('Width', 'Height'):
        screen_key = 'screen.' + axis.lower()
        previous = config.get(screen_key)
        selected = values['screen_' + axis.lower()]
        if type(previous) is not int or previous <= 0:
            raise ValueError('固定身份缺少屏幕尺寸，请先生成设备身份。')
        for key in ('screen.avail' + axis, 'window.outer' + axis, 'window.inner' + axis):
            if key in config:
                config[key] = max(1, config[key] + selected - previous)
        config[screen_key] = selected
    clamp_window_dimensions(config)
    clamp_window_position(config)


def apply_identity_options(config: dict, prefs: dict, values: dict, target_os: str) -> None:
    """Remove overrides for native modes and verify explicit device constraints."""
    if values['screen_mode'] == 'custom':
        if (config.get('screen.width'), config.get('screen.height')) != (
            values['screen_width'], values['screen_height']
        ):
            raise ValueError('指纹模型无法生成该分辨率，请选择其他尺寸。')
    if values['hardware_concurrency']:
        config['navigator.hardwareConcurrency'] = values['hardware_concurrency']
    if values['screen_mode'] == 'native':
        _remove(config, 'screen.', 'window.')
    if values['fonts_mode'] == 'native':
        config.pop('fonts', None)
    if values['webgl_mode'] == 'native':
        _remove(config, 'webGl:', 'webGl2:')
        prefs['webgl.enable-webgl2'] = True
        prefs['webgl.force-enabled'] = False
    if values['audio_mode'] == 'native':
        _remove(config, 'AudioContext:', 'audio:')
    if values['voices_mode'] == 'native':
        _remove(config, 'voices')
    if values['media_mode'] == 'native':
        _remove(config, 'mediaDevices:')
    prefs['dom.webgpu.enabled'] = values['webgpu_mode'] == 'native'
    prefs['privacy.donottrackheader.enabled'] = values['do_not_track']
    config['navigator.doNotTrack'] = '1' if values['do_not_track'] else 'unspecified'
    # Validate after manual edits: repairing them would silently change the user's choice.
    os_key = {'windows': 'win', 'macos': 'mac', 'linux': 'lin'}[target_os]
    violations = coherence.validate(config, os_key)
    if violations:
        rules = ', '.join(item.rule for item in violations)
        raise ValueError(f'这些参数无法组成一致的设备身份（{rules}），请调整分辨率、显卡或 CPU 核数。')


def apply_network_options(config: dict, prefs: dict, values: dict, exit_ip: str | None) -> None:
    """Refresh proxy-dependent data without changing the saved device identity."""
    mode = values['webrtc_mode']
    _remove(config, 'webrtc:')
    prefs['media.peerconnection.enabled'] = mode != 'disabled'
    prefs['network.dns.disableIPv6'] = False
    if mode == 'proxy':
        try:
            address = ipaddress.ip_address(exit_ip)
        except ValueError:
            raise ValueError('代理检查未返回有效出口 IP，无法替换 WebRTC 地址。') from None
        config[f'webrtc:ipv{address.version}'] = str(address)
        prefs['network.dns.disableIPv6'] = address.version == 4
