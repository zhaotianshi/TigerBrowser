"""Private Linux native displays and filesystem namespaces for the shared engine."""

import asyncio
import json
import os
import secrets
import shlex
import shutil
import struct
import sys
import tempfile
from pathlib import Path

from ...virtdisplay import VirtualDisplay
from ...utils import _camou_config_blob
from ..engine import StudioError

TOOLS = ('xauth', 'xdotool', 'bwrap', 'xclip')
MAX_TEXT_BYTES = 1024 * 1024
MAX_INPUT_MESSAGE_BYTES = 6 * MAX_TEXT_BYTES + 1024
MAX_NATIVE_INPUT_BYTES = 1024
KEYS = {
    'Enter': 'Return', 'Escape': 'Escape', 'Tab': 'Tab', 'Backspace': 'BackSpace',
    'Delete': 'Delete', 'ArrowLeft': 'Left', 'ArrowRight': 'Right',
    'ArrowUp': 'Up', 'ArrowDown': 'Down', 'Home': 'Home', 'End': 'End',
    'PageUp': 'Prior', 'PageDown': 'Next', 'ShiftLeft': 'Shift_L',
    'ShiftRight': 'Shift_R', 'ControlLeft': 'Control_L', 'ControlRight': 'Control_R',
    'AltLeft': 'Alt_L', 'AltRight': 'Alt_R', 'MetaLeft': 'Super_L', 'MetaRight': 'Super_R',
    'Space': 'space', 'Minus': 'minus', 'Equal': 'equal', 'Comma': 'comma',
    'Period': 'period', 'Slash': 'slash', 'Semicolon': 'semicolon', 'Quote': 'apostrophe',
    'BracketLeft': 'bracketleft', 'BracketRight': 'bracketright', 'Backslash': 'backslash',
    'Backquote': 'grave',
    **{f'Key{letter}': letter.lower() for letter in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'},
    **{f'Digit{number}': str(number) for number in range(10)},
    **{f'F{number}': f'F{number}' for number in range(1, 13)},
}


def validate_event(event, width, height):
    kind = event.get('kind') if isinstance(event, dict) else None
    if kind == 'pointer' and set(event) == {'kind', 'x', 'y', 'button', 'down'}:
        x, y, button, down = (event[key] for key in ('x', 'y', 'button', 'down'))
        valid = (type(x) is int and type(y) is int and 0 <= x < width and 0 <= y < height
                 and type(button) is int and button in range(4) and type(down) is bool)
    elif kind == 'key' and set(event) == {'kind', 'code', 'down'}:
        valid = isinstance(event['code'], str) and event['code'] in KEYS and type(event['down']) is bool
    elif kind == 'wheel' and set(event) == {'kind', 'up'}:
        valid = type(event['up']) is bool
    elif kind == 'text' and set(event) == {'kind', 'text'}:
        value = event['text']
        valid = (isinstance(value, str) and 0 < len(value) <= MAX_TEXT_BYTES
                 and not any(0xD800 <= ord(character) <= 0xDFFF for character in value)
                 and len(value.encode('utf-8')) <= MAX_TEXT_BYTES and '\0' not in value)
    else:
        valid = False
    if not valid:
        raise StudioError('Invalid native input event.')


def prerequisites():
    if os.name != 'posix' or not Path('/proc/sys/kernel').is_dir():
        raise StudioError('Native remote execution currently requires Linux.')
    if any(not shutil.which(tool) for tool in TOOLS):
        raise StudioError('Install bubblewrap, xauth, xdotool and xclip before remote execution.')
    from importlib.metadata import PackageNotFoundError, version
    try:
        pinned = version('python-xlib') == '0.33'
    except PackageNotFoundError:
        pinned = False
    if not pinned:
        raise StudioError('Install the pinned team input and text dependencies before remote execution.')


def namespace():
    return [shutil.which('bwrap'), '--unshare-user', '--unshare-pid', '--unshare-ipc',
            '--unshare-uts', '--die-with-parent', '--new-session', '--cap-drop', 'ALL',
            '--ro-bind', '/usr', '/usr', '--symlink', 'usr/bin', '/bin',
            '--symlink', 'usr/sbin', '/sbin',
            '--symlink', 'usr/lib', '/lib', '--symlink', 'usr/lib64', '/lib64',
            '--proc', '/proc', '--dev', '/dev']


def executable_wrapper(path, arguments, *, stdout=None):
    redirect = ' > ' + shlex.quote(str(stdout)) if stdout is not None else ''
    path.write_text('#!/bin/sh\nexec ' + shlex.join(arguments) + ' "$@"' + redirect + '\n', encoding='utf-8')
    path.chmod(0o700)
    return str(path)


def python_mounts():
    # Preserve the interpreter's configured prefix as well as its resolved path:
    # managed Python installations can link a version alias inside a hidden home.
    return list(dict.fromkeys((Path(sys.prefix), Path(sys.base_prefix),
                              Path(sys._base_executable).parent.parent,
                              Path(__file__).resolve().parents[3])))


class NativeDisplay(VirtualDisplay):
    def __init__(self, root, width, height):
        super().__init__(screen=f'{width}x{height}x24')
        self.root = root

    @property
    def xvfb_args(self):
        return ('-screen', '0', self.screen,
                '-auth', str(self.root / 'xauth'), '-nolisten', 'tcp', '-noreset',
                '-extension', 'COMPOSITE')

    def kill(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        # X server allocation is atomic; remove only this child's claimed display after reaping it.
        if self._display is not None:
            for path in (self.root / f'display-tmp/.X{self._display}-lock',
                         self.root / f'display-tmp/.X11-unix/X{self._display}'):
                path.unlink(missing_ok=True)
        self.proc = None


class NativeSession:
    """Shared namespace, input and browser lifecycle for the KasmVNC session."""

    def __init__(self, options, profile):
        self.options = options
        self.profile = profile
        self.root = None
        self.display = None
        self.env = None
        self.input_worker = None
        self.input_gate = asyncio.Lock()
        self.input_failed = False
        self.selection = None

    def worker_arguments(self, arguments):
        # LibX11 and Python Xlib choose different socket transports. Hide every
        # host filesystem socket so neither can fall back to WSLg or another display.
        mounts = python_mounts()
        base = namespace() + ['--tmpfs', '/tmp', '--tmpfs', '/home', '--dir', '/home/worker',
            '--chdir', '/', '--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache',
            '--ro-bind', str(self.root), str(self.root),
            '--ro-bind', str(self.root / f'display-tmp/.X11-unix/X{self.display._display}'),
            f'/tmp/.X11-unix/X{self.display._display}']
        covered = [Path('/usr')]
        for path in mounts:
            if not any(path == parent or parent in path.parents for parent in covered):
                base += ['--ro-bind', str(path), str(path)]
                covered.append(path)
        return base + ['--', *arguments]

    async def command(self, *arguments, data=None, capture=False, maximum=None):
        if self.env is not None:
            arguments = self.worker_arguments(arguments)
        process = await asyncio.create_subprocess_exec(
            *arguments, stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=self.env, start_new_session=True)
        async def bounded_read():
            output = bytearray()
            while block := await process.stdout.read(65536):
                if len(output) + len(block) > maximum:
                    raise StudioError('clipboard_capacity')
                output.extend(block)
            await process.wait()
            return bytes(output), None
        try:
            output, _ = await asyncio.wait_for(
                bounded_read() if maximum is not None else process.communicate(data), 5)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise StudioError('Native display operation failed.')
        return output

    async def clipboard(self, source):
        if source not in ('primary', 'clipboard'):
            raise StudioError('invalid_request')
        try:
            if source == 'primary':
                await self.drain()
                # A new private owner distinguishes a fresh Ctrl+C result from
                # stale clipboard text, including a previous copy by Firefox.
                await self.publish_selection(secrets.token_hex(16).encode('ascii'))
                try:
                    await self.input_request({'kind': 'shortcut', 'code': 'KeyC'})
                    await asyncio.wait_for(self.selection.wait(), 5)
                    if self.selection.returncode:
                        raise StudioError('clipboard_unavailable')
                finally:
                    await self.clear_selection()
                source = 'clipboard'
            data = await self.command(shutil.which('xclip'), '-selection', source,
                                      '-out', '-target', 'UTF8_STRING', capture=True,
                                      maximum=MAX_TEXT_BYTES)
            text = data.decode('utf-8')
        except (asyncio.TimeoutError, UnicodeError):
            raise StudioError('clipboard_unavailable') from None
        except StudioError as error:
            if str(error) == 'clipboard_capacity':
                raise
            raise StudioError('clipboard_unavailable') from None
        if not text or '\0' in text:
            raise StudioError('clipboard_unavailable')
        return text

    async def __aenter__(self):
        prerequisites()
        self.root = Path(tempfile.mkdtemp(prefix='camoufox-native-'))
        try:
            # FamilyWild accepts only our random cookie, including the namespace's different hostname.
            cookie = secrets.token_hex(16)
            await self.command(shutil.which('xauth'), '-f', str(self.root / 'xauth'),
                               'add', ':0', 'MIT-MAGIC-COOKIE-1', cookie)
            authority = self.root / 'xauth'
            record = authority.read_bytes()
            number_at = 4 + struct.unpack('!H', record[2:4])[0]
            number_end = number_at + 2 + struct.unpack('!H', record[number_at:number_at + 2])[0]
            authority.write_bytes(b'\xff\xff' + record[2:number_at] + b'\0\0' + record[number_end:])
            authority.chmod(0o600)
            # Prepared options include authorized screen edits; client viewport sizes do not.
            config = json.loads(_camou_config_blob(self.options))
            self.width, self.height = config['screen.width'], config['screen.height']
            if not (isinstance(self.width, int) and isinstance(self.height, int)
                    and 640 <= self.width <= 4096 and 480 <= self.height <= 2160):
                raise StudioError('The saved screen is outside native remote capacity.')
            self.display = self.create_display()
            startup = asyncio.create_task(asyncio.to_thread(self.display.get))
            try:
                display = await asyncio.shield(startup)
            except asyncio.CancelledError:
                await startup
                raise
            # Python Xlib matches FamilyLocal exactly; keep the wildcard entry
            # for the browser namespace and add the same cookie for local workers.
            number = str(self.display._display).encode('ascii')
            authority.write_bytes(authority.read_bytes() + record[:number_at]
                                  + struct.pack('!H', len(number)) + number + record[number_end:])
            # The server atomically owns this display's abstract socket; Python
            # Xlib uses the corresponding private filesystem socket mounted below.
            self.env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'DISPLAY': 'unix/' + display,
                        'XAUTHORITY': str(authority), 'HOME': '/home/worker',
                        'XDG_CACHE_HOME': '/home/worker/.cache'}
            self.input_worker = await asyncio.create_subprocess_exec(
                *self.worker_arguments([sys.executable, '-m', 'camoufox.studio.team.input']),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=self.env, start_new_session=True)
            if await asyncio.wait_for(self.input_worker.stdout.readexactly(1), 5) != b'R':
                raise StudioError('Native input worker failed to start.')
            if not self.display.socket.is_socket():
                raise StudioError('kasmvnc_unavailable')
            self.options = self.sandbox(self.options)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    def sandbox(self, options):
        def checked(path):
            path = Path(path).absolute()
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise StudioError('unsafe_native_path')
            return path

        executable = checked(options['executable_path'])
        directory = checked(options['user_data_dir'])
        workspace = directory.parent.parent
        try:
            addons = json.loads(_camou_config_blob(options)).get('addons', [])
            if not isinstance(addons, list) or any(not isinstance(path, str) for path in addons):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise StudioError('invalid_native_config') from None
        assets = [checked(path) for path in (
            executable.parent, *addons)]
        if any(path == workspace or path in workspace.parents or workspace in path.parents
               or not path.is_dir() for path in assets):
            raise StudioError('unsafe_native_path')
        downloads = directory / 'Downloads'
        uploads = directory / 'Uploads'
        checked(downloads)
        checked(uploads)
        downloads.mkdir(mode=0o700, exist_ok=True)
        uploads.mkdir(mode=0o700, exist_ok=True)
        arguments = namespace() + ['--tmpfs', '/tmp',
                     '--tmpfs', '/home', '--dir', '/home/browser', '--chdir', '/home/browser']
        for path in ('/etc/ld.so.cache', '/etc/fonts', '/etc/ssl/certs', '/etc/resolv.conf',
                     '/etc/hosts', '/etc/localtime'):
            if Path(path).exists():
                arguments += ['--ro-bind', path, path]
        for path in assets:
            arguments += ['--ro-bind', str(path), str(path)]
        fontconfig = options['env'].get('FONTCONFIG_FILE')
        if fontconfig:
            checked(fontconfig)
            arguments += ['--ro-bind', fontconfig, fontconfig]
        arguments += ['--bind', str(directory), str(directory),
                      '--ro-bind', str(self.root / 'xauth'), '/run/studio-xauth',
                      '--ro-bind', str(self.root / f'display-tmp/.X11-unix/X{self.display._display}'),
                      f'/tmp/.X11-unix/X{self.display._display}', '--', str(executable)]
        wrapper = executable_wrapper(self.root / 'browser', arguments)
        environment = {key: value for key, value in options['env'].items()
                       if key.startswith(('CAMOU_CONFIG', 'CAMOU_PREFS', 'FONTCONFIG'))}
        environment.update({'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': '/home/browser',
                            'XDG_CACHE_HOME': '/home/browser/.cache', 'XDG_CONFIG_HOME': '/home/browser/.config',
                            'DISPLAY': self.env['DISPLAY'], 'XAUTHORITY': '/run/studio-xauth',
                            'MOZ_ENABLE_WAYLAND': '0'})
        prefs = dict(options['firefox_user_prefs'])
        prefs.update({'browser.download.folderList': 2, 'browser.download.dir': str(downloads),
                      'browser.download.useDownloadDir': True})
        return {**options, 'executable_path': wrapper, 'env': environment,
                'firefox_user_prefs': prefs, 'downloads_path': str(downloads), 'headless': False}

    async def attach(self):
        # Without a window manager, persisted Firefox geometry can leave most of
        # the private screen empty. Size only this browser to its saved display.
        geometry = await self.command(
            shutil.which('xdotool'), 'search', '--sync', '--onlyvisible',
            '--limit', '1', '--classname', '^Navigator$',
            'windowmove', '--sync', '%1', '0', '0',
            'windowsize', '--sync', '%1', str(self.width), str(self.height),
            'windowfocus', '--sync', '%1', 'getwindowgeometry', '--shell', '%1', capture=True)
        bounds = dict(line.split('=') for line in geometry.decode('ascii').splitlines())
        if [int(bounds[key]) for key in ('X', 'Y', 'WIDTH', 'HEIGHT')] != [0, 0, self.width, self.height]:
            raise StudioError('Native browser window did not fill its saved display.')

    async def input(self, event):
        validate_event(event, self.width, self.height)
        if event['kind'] == 'text':
            await self.paste(event['text'].encode('utf-8'))
        else:
            await self.input_request(event)

    async def input_request(self, message):
        # One acknowledged request at a time keeps the pipe from retaining old
        # controller input behind RemoteRun's release and revocation barrier.
        async with self.input_gate:
            worker = self.input_worker
            if self.input_failed or worker is None or worker.returncode is not None:
                raise StudioError('Native input worker is unavailable.')

            async def exchange():
                data = json.dumps(message, separators=(',', ':')).encode('ascii') + b'\n'
                if len(data) > MAX_NATIVE_INPUT_BYTES:
                    raise StudioError('Native input exceeds capacity.')
                worker.stdin.write(data)
                await worker.stdin.drain()
                if await worker.stdout.readexactly(1) != b'A':
                    raise StudioError('Native input was not acknowledged.')

            try:
                await asyncio.wait_for(exchange(), 5)
            except BaseException:
                self.input_failed = True
                worker.stdin.close()
                if worker.returncode is None:
                    worker.kill()
                await worker.wait()
                raise

    async def paste(self, data):
        # Unicode keycode remapping races GTK's lookup. Publish explicit text
        # through the native selection protocol before dispatching the paste key.
        await self.drain()
        await self.publish_selection(data)
        await self.input_request({'kind': 'shortcut', 'code': 'KeyV'})
        if self.selection.returncode is not None:
            raise StudioError('Native text selection failed.')

    async def publish_selection(self, data):
        await self.clear_selection()
        worker = await asyncio.create_subprocess_exec(
            *self.worker_arguments([sys.executable, '-m', 'camoufox.studio.team.paste']),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=self.env, start_new_session=True)
        try:
            worker.stdin.write(struct.pack('!I', len(data)) + data)
            await worker.stdin.drain()
            if await asyncio.wait_for(worker.stdout.readexactly(1), 5) != b'R':
                raise StudioError('Native text selection failed.')
            # GTK may request the value again after the first transfer. Keep
            # ownership private to this display until replacement or teardown.
            self.selection = worker
        finally:
            if self.selection is not worker:
                worker.stdin.close()
                if worker.returncode is None:
                    worker.kill()
                await worker.wait()

    async def clear_selection(self):
        if self.selection is not None:
            worker, self.selection = self.selection, None
            worker.stdin.close()
            try:
                await asyncio.wait_for(worker.wait(), 5)
            except asyncio.TimeoutError:
                worker.kill()
                await worker.wait()

    async def drain(self):
        await self.input_request({'kind': 'drain'})

    async def wait(self):
        while (self.display.proc is not None and self.display.proc.poll() is None
               and self.input_worker.returncode is None
               and not self.input_failed):
            await asyncio.sleep(.25)
        raise StudioError('Native display or input worker exited; this environment has been stopped.')

    async def __aexit__(self, *exc):
        await self.clear_selection()
        if self.input_worker:
            self.input_worker.stdin.close()
            try:
                await asyncio.wait_for(self.input_worker.wait(), 5)
            except asyncio.TimeoutError:
                self.input_worker.kill()
                await self.input_worker.wait()
        if self.display:
            await asyncio.to_thread(self.display.kill)
        if self.root:
            shutil.rmtree(self.root)
            self.root = None
