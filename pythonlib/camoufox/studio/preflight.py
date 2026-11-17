"""Static host checks without workspace writes, downloads or browser launches."""

import os

from ..addons import confirm_paths
from ..display import has_display
from ..fpgen_model import CUSTOM_MODEL_ENV, is_pinned
from ..pkgman import OS_NAME, Version
from ..utils import validate_config
from .distribution import browser_executable, default_addon_paths
from .secrets import KeyringSecretStore


def _windows_interactive() -> bool:
    """A Windows service's window station is not an interactive desktop."""
    import ctypes
    from ctypes import wintypes

    class UserObjectFlags(ctypes.Structure):
        _fields_ = [('inherit', wintypes.BOOL), ('reserved', wintypes.BOOL),
                    ('flags', wintypes.DWORD)]

    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.GetProcessWindowStation.argtypes = ()
    user32.GetProcessWindowStation.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    station = user32.GetProcessWindowStation()
    flags, needed = UserObjectFlags(), wintypes.DWORD()
    if not station or not user32.GetUserObjectInformationW(
        station, 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed),
    ):
        raise OSError('Cannot inspect the process window station.')
    # UOI_FLAGS=1 returns USEROBJECTFLAGS; WSF_VISIBLE=0x0001.
    return bool(flags.flags & 0x0001)


def check_host(*, mode='headless', executable_path=None, check_credentials=False) -> dict:
    """Report prerequisites only; successful probes do not certify a deployment."""
    if mode not in ('headless', 'desktop', 'remote'):
        raise ValueError('Unknown host mode.')
    checks = []

    def add(identifier, status, message):
        checks.append({'id': identifier, 'status': status, 'message': message})

    supported = OS_NAME in ('win', 'lin')
    add('platform', 'pass' if supported else 'fail',
        '执行主机目标为 Windows 和 Linux；Linux 为优先部署平台。')
    try:
        executable = browser_executable(executable_path)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise FileNotFoundError
        if not Version.from_path(executable.parent).is_supported():
            raise ValueError('Unsupported browser package.')
        validate_config({}, path=executable)
    except Exception:
        add('browser', 'fail', '内核文件、版本或配置 schema 不可用；请安装兼容完整包或检查显式路径。')
    else:
        add('browser', 'pass', '内核文件、支持范围内的版本元数据和配置 schema 可读取；未启动内核。')

    try:
        confirm_paths(default_addon_paths())
    except Exception:
        add('addons', 'fail', '默认扩展未就绪；请先完成 camoufox fetch，检查不会自动下载。')
    else:
        add('addons', 'pass', '默认扩展目录和清单存在。')
    try:
        model_ok = not os.environ.get(CUSTOM_MODEL_ENV) and is_pinned()
    except Exception:
        model_ok = False
    add('model', 'pass' if model_ok else 'fail',
        '固定模型摘要校验通过。' if model_ok else
        '固定模型不可用或设置了自定义模型；请先完成安装，不会自动生成或替换身份。')

    if mode == 'headless':
        add('display', 'pass', '无头模式不需要桌面显示；不等于支持远程人工操作。')
    elif mode == 'desktop' and supported:
        try:
            available = _windows_interactive() if OS_NAME == 'win' else has_display(os.environ)
        except Exception:
            available = False
        add('display', 'warn' if available else 'fail',
            '存在显示配置或可见窗口站；尚未验证显示连接、权限及真实浏览器画面。' if available else
            '未确认交互显示条件；Linux 需要授权显示会话，Windows 有窗口模式需要交互用户会话。')
    elif mode == 'remote':
        try:
            from .team.kasm import prerequisites
            prerequisites()
        except Exception:
            add('native', 'fail', 'Linux KasmVNC 原生远程依赖未就绪；Windows 不提供远程执行。')
        else:
            add('native', 'pass', '原生远程工具和固定依赖存在；未采集画面或启动浏览器。')
        add('remote', 'fail', '静态检查不能认证正式远程就绪；另需专用会话和真实部署验证，studio-attest 仅提供可选诊断测量。')

    if check_credentials:
        try:
            KeyringSecretStore('camoufox-studio-preflight').check_backend()
        except Exception:
            add('credentials', 'fail', '原生凭据后端不可用；检查服务账号及系统钥匙环，禁止明文回退。')
        else:
            add('credentials', 'warn', '原生凭据后端可识别；未读取密码，未证明钥匙环已解锁或原凭据可用。')
    else:
        add('credentials', 'warn', '未探测凭据后端；使用认证代理前请以实际运行账号显式检查。')
    return {
        'protocolVersion': 1, 'platform': {'win': 'windows', 'lin': 'linux'}.get(OS_NAME, 'unsupported'),
        'mode': mode, 'scope': 'static-prerequisites', 'remoteReady': False,
        'ok': not any(item['status'] == 'fail' for item in checks), 'checks': checks,
    }
