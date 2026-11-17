"""The desktop window: the server's own Vue console in a persistent web view, plus the local bridge."""

import asyncio
import hashlib
import json
import os
import sys
import threading
from pathlib import Path

import requests
from PySide6.QtCore import QFile, QIODevice, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEnginePermission, QWebEngineProfile, QWebEngineScript,
                                     QWebEngineSettings)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (QApplication, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QLabel, QMainWindow,
                               QMessageBox, QVBoxLayout)

from ..team.transport import exact_origin, session_cookie
from . import update
from .bridge import Bridge
from .local import default_base

SERVERS = 'servers.json'
PROBE_TIMEOUT = (5, 10)


class BrowserThread(QThread):
    """One asyncio loop owns the browser engine and synchronization, outside the GUI thread."""

    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()

    def run(self):
        asyncio.set_event_loop(self.loop)
        self.ready.set()
        try:
            self.loop.run_forever()
        finally:
            remaining = asyncio.all_tasks(self.loop)
            for task in remaining:
                task.cancel()
            if remaining:
                self.loop.run_until_complete(asyncio.gather(*remaining, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.run_until_complete(self.loop.shutdown_default_executor())
            self.loop.close()


def same_origin(url, origin):
    """Compare a page URL with the configured origin by scheme, host and effective port."""
    parsed = exact_origin(origin)
    default = 443 if parsed.scheme == 'https' else 80
    url = QUrl(url)
    return (url.scheme() == parsed.scheme and url.host() == parsed.hostname and not url.userInfo()
            and url.port(default) == (parsed.port or default))


def load_servers(base):
    try:
        data = json.loads((Path(base) / SERVERS).read_text(encoding='utf-8'))
        servers = [item for item in data['servers'] if isinstance(item, str)]
        for item in servers:
            exact_origin(item)
        return servers
    except FileNotFoundError:
        return []
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('The saved server list is damaged; remove servers.json to enter the addresses again.') from None


def save_servers(base, servers):
    path = Path(base) / SERVERS
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'servers': servers}), encoding='utf-8')
    os.replace(temporary, path)


def probe_server(origin):
    """A Studio team server answers /me with a session error before login, from its exact origin."""
    exact_origin(origin)
    with requests.Session() as session:
        session.trust_env = False
        try:
            response = session.get(origin + '/team/v1/me', headers={'Origin': origin}, timeout=PROBE_TIMEOUT)
            data = response.json()
        except (requests.RequestException, ValueError):
            return 'server_unreachable'
    if response.status_code == 200 or data.get('error') == 'session_expired':
        return None
    return data.get('error') or 'not_a_studio_server'


class ServerDialog(QDialog):
    PROBLEMS = {'server_unreachable': '无法连接该地址，请检查服务器是否运行及网络。',
                'invalid_host': '地址必须与服务器启动时的 --origin 完全一致。',
                'private_network_required': '服务器只接受私有网络地址的连接。',
                'client_not_allowed': '本机 IP 不在服务器的白名单中。'}

    def __init__(self, servers):
        super().__init__()
        self.setWindowTitle('连接 TigerBrowser 服务器')
        self.origin = None
        self.choice = QComboBox(editable=True)
        self.choice.addItems(servers)
        self.choice.setCurrentText(servers[0] if servers else 'http://192.168.1.10:8766')
        self.problem = QLabel()
        self.problem.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.connect_server)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('服务器地址（与服务器的 --origin 相同）'))
        layout.addWidget(self.choice)
        layout.addWidget(self.problem)
        layout.addWidget(buttons)

    def connect_server(self):
        origin = self.choice.currentText().strip()
        try:
            exact_origin(origin)
        except ValueError:
            self.problem.setText('请填写 http:// 或 https:// 开头、不含路径的地址，例如 http://192.168.1.10:8766。')
            return
        problem = probe_server(origin)
        if problem:
            self.problem.setText(self.PROBLEMS.get(problem, '该地址不是可用的 TigerBrowser 服务器。'))
            return
        self.origin = origin
        self.accept()


class Page(QWebEnginePage):
    """Only the configured server's pages load inside the client; other links open in the system browser."""

    def __init__(self, profile, origin, window_factory, parent=None):
        super().__init__(profile, parent)
        self.origin, self.window_factory = origin, window_factory
        self.permissionRequested.connect(self._permission)

    def acceptNavigationRequest(self, url, kind, main_frame):
        if same_origin(url, self.origin) or url.scheme() in ('blob', 'data', 'about') and not main_frame:
            return True
        if kind == QWebEnginePage.NavigationTypeLinkClicked and url.scheme() in ('http', 'https'):
            QDesktopServices.openUrl(url)
        return False

    def createWindow(self, kind):
        # Remote operation windows from window.open open as top-level client windows.
        return self.window_factory().view.page()

    def _permission(self, permission):
        if (permission.permissionType() == QWebEnginePermission.PermissionType.ClipboardReadWrite
                and same_origin(permission.origin(), self.origin)):
            permission.grant()
        else:
            permission.deny()


class Window(QMainWindow):
    closing = Signal()

    def __init__(self, profile, origin, windows, *, bridge=None):
        super().__init__()
        self.windows, self.bridge = windows, bridge
        self.view = QWebEngineView(self)
        self.view.setPage(Page(profile, origin, lambda: open_window(profile, origin, windows), self.view))
        self.view.titleChanged.connect(self.setWindowTitle)
        if bridge is not None:
            self.channel = QWebChannel(self)
            self.channel.registerObject('studio', bridge)
            self.view.page().setWebChannel(self.channel, QWebEngineScript.MainWorld)
        self.setCentralWidget(self.view)
        self.resize(1400, 900)

    def closeEvent(self, event):
        if self.bridge is not None and self.bridge.running():
            answer = QMessageBox.question(
                self, '仍有本机运行的环境',
                '退出会关闭本机运行的浏览器，并把数据签回服务器。签回失败的环境保留为待签回，下次启动时继续。是否退出？')
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.windows.discard(self)
        if self.bridge is not None:
            self.closing.emit()
        event.accept()


def open_window(profile, origin, windows, *, bridge=None):
    window = Window(profile, origin, windows, bridge=bridge)
    windows.add(window)
    window.show()
    return window


def channel_script():
    """The page-side WebChannel library, injected before the console's own scripts run."""
    source = QFile(':/qtwebchannel/qwebchannel.js')
    if not source.open(QIODevice.ReadOnly):
        raise RuntimeError('Qt WebChannel resources are missing from this build.')
    script = QWebEngineScript()
    script.setName('qwebchannel')
    script.setSourceCode(bytes(source.readAll()).decode('utf-8'))
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(QWebEngineScript.MainWorld)
    script.setRunsOnSubFrames(False)
    return script


def web_profile(base, origin, application):
    """Login cookies persist per server until they expire; nothing else is shared between servers."""
    key = hashlib.sha256(origin.encode()).hexdigest()[:24]
    profile = QWebEngineProfile('server-' + key, application)
    profile.setPersistentStoragePath(str(Path(base) / 'web' / key))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
    settings = profile.settings()
    settings.setAttribute(QWebEngineSettings.JavascriptCanOpenWindows, True)
    settings.setAttribute(QWebEngineSettings.JavascriptCanAccessClipboard, True)
    settings.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, False)
    profile.scripts().insert(channel_script())

    def download(request):
        page = request.page()
        if page is None or not same_origin(page.url(), origin):
            request.cancel()
            return
        target, _ = QFileDialog.getSaveFileName(None, '保存文件', str(Path.home() / request.downloadFileName()))
        if not target:
            request.cancel()
            return
        request.setDownloadDirectory(str(Path(target).parent))
        request.setDownloadFileName(Path(target).name)
        request.accept()

    profile.downloadRequested.connect(download)
    return profile


def watch_session(profile, origin, bridge):
    """Follow the server's HttpOnly login cookie, which pages cannot read, for local automation."""
    parsed = exact_origin(origin)
    name = session_cookie(parsed.scheme).encode()

    def ours(cookie):
        return bytes(cookie.name()) == name and cookie.domain() == parsed.hostname

    store = profile.cookieStore()
    store.cookieAdded.connect(lambda cookie: ours(cookie) and bridge.set_session_cookie(bytes(cookie.value()).decode()))
    store.cookieRemoved.connect(lambda cookie: ours(cookie) and bridge.set_session_cookie(''))
    store.loadAllCookies()


def main(base=None):
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName('TigerBrowser')
    base = Path(base or default_base()).expanduser().resolve()
    try:
        servers = load_servers(base)
    except ValueError as error:
        QMessageBox.warning(None, '服务器列表不可用', str(error))
        return 1
    dialog = ServerDialog(servers)
    if not dialog.exec():
        return 1
    origin = dialog.origin
    save_servers(base, [origin, *[item for item in servers if item != origin]])
    thread = BrowserThread()
    thread.start()
    thread.ready.wait()
    windows = set()
    main_window = None
    bridge = Bridge(base, origin, thread.loop, allowed=lambda: main_window is not None
                    and same_origin(main_window.view.page().url(), origin))
    profile = web_profile(base, origin, application)
    watch_session(profile, origin, bridge)
    main_window = open_window(profile, origin, windows, bridge=bridge)
    main_window.closing.connect(application.quit)
    main_window.view.load(QUrl(origin + '/'))
    try:
        return application.exec()
    finally:
        for window in list(windows):
            window.close()
        try:
            asyncio.run_coroutine_threadsafe(bridge.shutdown(), thread.loop).result()
        except Exception as error:
            QMessageBox.warning(None, '本机运行未能完全停止', f'{error}\n本机缓存保持锁定，请检查残留浏览器进程后再启动。')
            # A locked cache may still have browsers or returns in progress; never replace the program under them.
            bridge.pending_install = None
        finally:
            thread.loop.call_soon_threadsafe(thread.loop.stop)
            thread.wait()
        if bridge.pending_install is not None:
            try:
                update.start_install(bridge.pending_install)
            except (OSError, update.SyncError):
                QMessageBox.warning(None, '无法启动安装程序', '请从“客户端下载”页手动下载并安装新版本。')
