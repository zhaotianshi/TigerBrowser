"""Pinned KasmVNC displays with private listeners and Studio-owned input."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import select
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ...exceptions import CannotExecuteXvfb
from . import native
from .boundary import namespace_pidfd
from ..engine import StudioError

RUNTIME_VERSION = '1.5.0+studio.2'
REVISION = '17265facc40ab50db5740cdf0d12c61173edafc9'
CACHE_FIX = 'a05a0984f55d4aa4de15b001f581c5015c177d78'
PRIVATE_IPC_FIX = 'fdf37afdf4fcb222df3b7408636055f4dd7836e3d4dd738098907bfcaf4733c8'
FFMPEG_LOADER_FIX = '8c608355ceec16713aec8650bb06e31db2682ec7feb88c3e66fa8c3aa75bc04f'
STARTUP_FAILURES = frozenset({
    'server_spawn', 'display_report', 'listener_check', 'gateway_bind',
    'display_timeout', 'display_exited', 'display_invalid',
    'listener_not_private', 'listener_ambiguous', 'listener_udp',
})


def startup_error(error):
    reason = error.reason if error.reason in {'timeout', 'exited', 'invalid_response'} else 'unavailable'
    exit_code = error.exit_code if type(error.exit_code) is int else 'unknown'
    detail = f'relay_{reason} (exit={exit_code})'
    try:
        if len(error.response) > 128:
            raise ValueError
        code, number, status = json.loads(error.response)
        if (code not in STARTUP_FAILURES or type(number) is not int or not 0 <= number <= 4095
                or not (status is None or type(status) is int and -255 <= status <= 255)):
            raise ValueError
        detail = f'{code} (errno={number}, exit={status if status is not None else "unknown"})'
    except (ValueError, TypeError, UnicodeError):
        pass
    return StudioError(f'kasmvnc_startup_failed: {detail}')


def verify_runtime(tools):
    try:
        path = tools / 'studio-kasmvnc-runtime.json'
        if path.stat().st_size > 65536:
            raise ValueError
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if (manifest.get('schema') != 1 or manifest.get('version') != RUNTIME_VERSION
                or manifest.get('upstream_revision') != REVISION
                or manifest.get('cache_fix_revision') != CACHE_FIX
                or manifest.get('private_ipc_fix_sha256') != PRIVATE_IPC_FIX
                or manifest.get('ffmpeg_loader_fix_sha256') != FFMPEG_LOADER_FIX
                or set(manifest.get('binaries', {})) != {'Xkasmvnc', 'kasmvncpasswd'}):
            raise ValueError
        for name, digest in manifest['binaries'].items():
            calculated = hashlib.sha256()
            with (tools / name).open('rb') as binary:
                while block := binary.read(1024 * 1024):
                    calculated.update(block)
            if calculated.hexdigest() != digest:
                raise ValueError
    except (OSError, ValueError, TypeError, AttributeError):
        raise StudioError('kasmvnc_runtime_unverified') from None


def prerequisites():
    native.prerequisites()
    configured = os.environ.get('CAMOUFOX_KASMVNC_TOOLS')
    found = shutil.which('Xkasmvnc')
    if not configured and not found:
        raise StudioError('kasmvnc_not_installed')
    tools = Path(configured).resolve() if configured else Path(found).resolve().parent
    if not all((tools / name).is_file() for name in ('Xkasmvnc', 'kasmvncpasswd')):
        raise StudioError('kasmvnc_not_installed')
    verify_runtime(tools)
    try:
        result = subprocess.run([str(tools / 'Xkasmvnc'), '-version'], capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        raise StudioError('kasmvnc_unavailable') from None
    reported = result.stdout + result.stderr
    if f'KasmVNC {RUNTIME_VERSION.replace("+", ".")}' not in reported:
        raise StudioError('kasmvnc_version_mismatch')
    return tools


class KasmDisplay(native.NativeDisplay):
    accounts = (('viewer', ('-r',)), ('owner', ('-r', '-o')))

    def __init__(self, root, width, height, *, tools):
        super().__init__(root, width, height)
        self.tools = tools
        self.private = None
        self.child = None
        self.credentials = {}

    def get(self):
        try:
            if self.proc is not None:
                return super().get()
            # Keep credentials and the gateway out of NativeSession's worker mounts.
            self.private = Path(tempfile.mkdtemp(prefix='camoufox-kasm-', dir='/tmp'))
            (self.private / 'gateway').mkdir(mode=0o700)
            for name, rights in self.accounts:
                password = secrets.token_urlsafe(32)
                subprocess.run([str(self.tools / 'kasmvncpasswd'), '-u', name, *rights,
                                str(self.private / 'passwords')],
                               input=(password + '\n' + password + '\n').encode('ascii'),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, check=True)
                self.credentials[name] = password
            (self.private / 'passwords').chmod(0o600)
            display = super().get()
            self.child = namespace_pidfd((self.private / 'process.json').read_bytes())
            return display
        except CannotExecuteXvfb as error:
            self.kill()
            raise startup_error(error) from None
        except BaseException:
            self.kill()
            raise

    @property
    def socket(self):
        return self.private / 'gateway/stream.sock'

    def authorization(self, user):
        value = f'{user}:{self.credentials[user]}'.encode('ascii')
        return {'Authorization': 'Basic ' + base64.b64encode(value).decode('ascii')}

    @property
    def xvfb_path(self):
        private_tmp = self.root / 'display-tmp'
        private_tmp.mkdir(mode=0o700, exist_ok=True)
        sockets = private_tmp / '.X11-unix'
        sockets.mkdir(mode=0o1777, exist_ok=True)
        sockets.chmod(0o1777)
        mounts = {*native.python_mounts(), self.tools}
        arguments = native.namespace() + ['--as-pid-1', '--info-fd', '1',
            '--bind', str(private_tmp), '/tmp', '--ro-bind', str(self.root), str(self.root),
            '--ro-bind', '/etc/ld.so.cache', '/etc/ld.so.cache',
            '--ro-bind', str(self.private / 'passwords'), '/run/kasm-passwords',
            '--bind', str(self.private / 'gateway'), '/run/kasm-gateway']
        for path in sorted(mounts):
            if path != Path('/usr') and Path('/usr') not in path.parents:
                arguments += ['--ro-bind', str(path), str(path)]
        return native.executable_wrapper(self.root / 'display', arguments + ['--', sys.executable,
            '-m', 'camoufox.studio.team.kasm', '--relay', str(self.tools / 'Xkasmvnc')],
            stdout=self.private / 'process.json')

    @property
    def xvfb_args(self):
        return (*super().xvfb_args, '-interface', '127.0.0.1', '-websocketPort', '0',
                '-UseIPv6=0', '-KasmPasswordFile', '/run/kasm-passwords', '-SecurityTypes', 'None',
                '-AcceptKeyEvents=0', '-AcceptPointerEvents=0', '-AcceptCutText=0',
                '-SendCutText=0', '-SendPrimary=0', '-SetPrimary=0', '-AcceptSetDesktopSize=0',
                '-IgnoreClientSettingsKasm=1', '-AllowOverride', '', '-AlwaysShared=1',
                '-DisconnectClients=0', '-publicIP', '127.0.0.1', '-FrameRate', '24',
                '-RectThreads', '1', '-httpd', '')

    def kill(self):
        if self.child is not None:
            # The monitor exiting does not prove its namespace stopped. Pin
            # PID 1 at startup and wait for kernel teardown before reaping it.
            try:
                signal.pidfd_send_signal(self.child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not select.select([self.child], [], [], 5)[0]:
                raise StudioError('Native display did not stop.')
            self.proc.wait(timeout=5)
            os.close(self.child)
            self.child = None
        super().kill()
        if self.private is not None:
            shutil.rmtree(self.private)
            self.private = None
            self.credentials.clear()


def listening_port(pid):
    owned = set()
    for descriptor in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            target = descriptor.readlink().as_posix()
        except FileNotFoundError:
            continue
        if target.startswith('socket:[') and target.endswith(']'):
            owned.add(target[8:-1])
    listeners = []
    for line in (Path('/proc') / str(pid) / 'net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[3] == '0A' and fields[9] in owned:
            address, port = fields[1].split(':')
            if address != '0100007F' or int(port, 16) == 0:
                raise StudioError('listener_not_private')
            listeners.append(int(port, 16))
    if len(listeners) != 1:
        raise StudioError('listener_ambiguous')
    for line in (Path('/proc') / str(pid) / 'net/udp').read_text().splitlines()[1:]:
        if line.split()[9] in owned:
            raise StudioError('listener_udp')
    return listeners[0]


async def close_stream(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 5)
    except (ConnectionError, asyncio.TimeoutError):
        writer.transport.abort()


async def bridge_streams(reader, writer, incoming, upstream):
    async def copy(source, target):
        while block := await source.read(65536):
            target.write(block)
            await asyncio.wait_for(target.drain(), 5)

    transfers = [asyncio.create_task(copy(reader, upstream)),
                 asyncio.create_task(copy(incoming, writer))]
    try:
        await asyncio.wait(transfers, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for transfer in transfers:
            transfer.cancel()
        await asyncio.gather(*transfers, return_exceptions=True)
        await asyncio.gather(close_stream(writer), close_stream(upstream), return_exceptions=True)


async def relay_display(executable, arguments):
    display_at = arguments.index('-displayfd') + 1
    parent_fd = int(arguments[display_at])
    reader_fd, writer_fd = os.pipe()
    arguments[display_at] = str(writer_fd)
    process = None
    server = None
    connections = set()

    async def relay(reader, writer):
        task = asyncio.current_task()
        if len(connections) >= 32:
            await close_stream(writer)
            return
        connections.add(task)
        try:
            incoming, upstream = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', port), 5)
            await bridge_streams(reader, writer, incoming, upstream)
        except (ConnectionError, asyncio.TimeoutError):
            await close_stream(writer)
        finally:
            connections.discard(task)

    def ready():
        value = b''
        deadline = time.monotonic() + 8
        while b'\n' not in value and len(value) < 64:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([reader_fd], [], [], remaining)[0]:
                raise StudioError('display_timeout')
            block = os.read(reader_fd, 64 - len(value))
            if not block:
                raise StudioError('display_exited')
            value += block
        if not value.endswith(b'\n') or not value[:-1].isdigit():
            raise StudioError('display_invalid')
        return value

    stage = 'server_spawn'
    try:
        process = await asyncio.create_subprocess_exec(executable, *arguments,
            pass_fds=(writer_fd,), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(writer_fd)
        writer_fd = None
        stage = 'display_report'
        display = await asyncio.to_thread(ready)
        stage = 'listener_check'
        port = listening_port(process.pid)
        stage = 'gateway_bind'
        path = Path('/run/kasm-gateway/stream.sock')
        server = await asyncio.start_unix_server(relay, path, limit=65536, backlog=16)
        path.chmod(0o600)
        os.write(parent_fd, display)
        os.close(parent_fd)
        parent_fd = None
        return await process.wait()
    except Exception as error:
        if parent_fd is not None:
            # Only fixed codes and numbers cross the readiness pipe; stderr may contain secrets.
            code = str(error) if isinstance(error, StudioError) and str(error) in STARTUP_FAILURES else stage
            number = error.errno if isinstance(error, OSError) and type(error.errno) is int else 0
            status = process.returncode if process is not None else None
            report = json.dumps([code, number, status], separators=(',', ':')).encode('ascii') + b'\n'
            try:
                os.write(parent_fd, report)
            except BrokenPipeError:
                pass
        raise
    finally:
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in tuple(connections):
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        for descriptor in (reader_fd, writer_fd, parent_fd):
            if descriptor is not None:
                os.close(descriptor)


class KasmSession(native.NativeSession):
    transport = 'kasmvnc'

    def create_display(self):
        return KasmDisplay(self.root, self.width, self.height, tools=prerequisites())

    def worker_arguments(self, arguments):
        command = super().worker_arguments(arguments)
        position = command.index('--')
        tools = str(self.display.tools)
        return command[:position] + ['--ro-bind', tools, tools] + command[position:]

    def display_stream(self):
        from websockets.asyncio.client import unix_connect
        # The pinned KasmVNC server ignores Ping frames; browser-facing heartbeats stay enabled.
        return unix_connect(str(self.display.socket), uri='ws://localhost/websockify',
                            origin='http://localhost',
                            additional_headers=self.display.authorization('viewer'),
                            subprotocols=['binary'], compression=None, max_size=64 * 1024 * 1024,
                            max_queue=2, write_limit=65536, open_timeout=5, close_timeout=2,
                            ping_interval=None)


if __name__ == '__main__':
    if len(sys.argv) < 4 or sys.argv[1] != '--relay':
        raise SystemExit('Use the Studio team service to launch KasmVNC.')
    raise SystemExit(asyncio.run(relay_display(sys.argv[2], sys.argv[3:])))
