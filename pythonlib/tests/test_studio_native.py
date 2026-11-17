"""Native namespace construction never widens a profile's filesystem authority."""

from types import SimpleNamespace
import asyncio
import json
import shlex
import struct

import pytest

from camoufox.studio.engine import StudioError
from camoufox.studio.team import native


class InputWorker:
    def __init__(self):
        self.stdin = self.stdout = self
        self.returncode = None
        self.reply = b'A'
        self.closed = False
        self.messages = []
        self.written = asyncio.Event()
        self.acknowledge = asyncio.Event()
        self.acknowledge.set()

    def write(self, data):
        self.messages.append(json.loads(data))
        self.written.set()

    async def drain(self):
        pass

    async def readexactly(self, count):
        await self.acknowledge.wait()
        return self.reply

    def close(self):
        self.closed = True

    def kill(self):
        self.returncode = -9
        self.acknowledge.set()

    async def wait(self):
        if self.closed and self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.asyncio
async def test_native_pointer_keys_and_wheel_reuse_one_input_worker(session, monkeypatch):
    session, _ = session
    session.width, session.height = 1920, 1080
    session.input_worker = worker = InputWorker()

    async def command(*args, **kwargs):
        pytest.fail('Native input must reuse its worker instead of spawning xdotool.')

    monkeypatch.setattr(session, 'command', command)
    events = [
        *[{'kind': 'pointer', 'x': x, 'y': 45, 'button': 0, 'down': False} for x in range(64)],
        {'kind': 'key', 'code': 'ShiftLeft', 'down': True},
        {'kind': 'pointer', 'x': 63, 'y': 45, 'button': 1, 'down': True},
        {'kind': 'wheel', 'up': False},
    ]
    for event in events:
        await session.input(event)
    await session.drain()
    assert worker.messages == [*events, {'kind': 'drain'}]


@pytest.mark.asyncio
async def test_native_release_waits_for_input_acknowledgement(session, monkeypatch):
    session, _ = session
    session.width, session.height = 1920, 1080
    session.input_worker = worker = InputWorker()
    worker.acknowledge.clear()

    async def command(*args, **kwargs):
        pytest.fail('Native input must use the supervised input worker.')

    monkeypatch.setattr(session, 'command', command)
    event = {'kind': 'key', 'code': 'ControlLeft', 'down': True}
    dispatch = asyncio.create_task(session.input(event))
    await worker.written.wait()
    release = asyncio.create_task(session.drain())
    await asyncio.sleep(0)
    assert not dispatch.done() and not release.done()
    assert worker.messages == [event]
    worker.acknowledge.set()
    await asyncio.gather(dispatch, release)
    assert worker.messages == [event, {'kind': 'drain'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['cancel', 'eof', 'bad_ack', 'timeout'])
async def test_native_input_failure_fences_the_worker_without_replay(session, failure, monkeypatch):
    session, _ = session
    session.width, session.height = 1920, 1080
    session.input_worker = worker = InputWorker()
    worker.acknowledge.clear()

    async def read(count):
        if failure == 'eof':
            raise asyncio.IncompleteReadError(b'', count)
        if failure == 'bad_ack':
            return b'X'
        if failure == 'timeout':
            raise asyncio.TimeoutError()
        await worker.acknowledge.wait()

    monkeypatch.setattr(worker, 'readexactly', read)
    operation = asyncio.create_task(session.input({'kind': 'key', 'code': 'KeyA', 'down': True}))
    await worker.written.wait()
    if failure == 'cancel':
        operation.cancel()
    with pytest.raises((asyncio.CancelledError, asyncio.IncompleteReadError, asyncio.TimeoutError, StudioError)):
        await operation
    assert worker.returncode == -9 and session.input_failed
    with pytest.raises(StudioError, match='unavailable'):
        await session.drain()
    assert len(worker.messages) == 1


@pytest.mark.asyncio
async def test_native_text_uses_private_selection_and_the_same_input_worker(session, monkeypatch):
    session, _ = session
    session.width, session.height = 1920, 1080
    session.input_worker = worker = InputWorker()
    published = []

    async def publish(data):
        published.append(data)
        session.selection = SimpleNamespace(returncode=None)

    monkeypatch.setattr(session, 'publish_selection', publish)
    await session.input({'kind': 'text', 'text': '中文😀\n'})
    assert published == ['中文😀\n'.encode('utf-8')]
    assert worker.messages == [{'kind': 'drain'}, {'kind': 'shortcut', 'code': 'KeyV'}]


@pytest.mark.asyncio
@pytest.mark.parametrize('ready', [b'R', b'X'])
async def test_native_startup_requires_input_readiness_and_cleans_up(session, monkeypatch, ready):
    session, _ = session
    session.options['env']['CAMOU_CONFIG_1'] = json.dumps({'screen.width': 1920, 'screen.height': 1080})
    worker = InputWorker()
    worker.reply = ready
    created, stopped = [], []
    monkeypatch.setattr(native, 'prerequisites', lambda: None)
    monkeypatch.setattr(native.tempfile, 'mkdtemp', lambda **kwargs: str(session.root))
    monkeypatch.setattr(session, 'create_display', lambda: SimpleNamespace(
        _display=123, get=lambda: ':123', kill=lambda: stopped.append('display'),
        socket=SimpleNamespace(is_socket=lambda: True)), raising=False)

    async def command(*args, **kwargs):
        (session.root / 'xauth').write_bytes(struct.pack('!HHH', 256, 0, 1) + b'0')

    async def create(*args, **kwargs):
        created.append(args[-1])
        assert '--tmpfs' in args and '/tmp/.X11-unix/X123' in args
        assert kwargs['env']['DISPLAY'] == 'unix/:123'
        return worker

    monkeypatch.setattr(session, 'command', command)
    monkeypatch.setattr(native.asyncio, 'create_subprocess_exec', create)
    monkeypatch.setattr(session, 'sandbox', lambda options: options)
    if ready == b'R':
        assert await session.__aenter__() is session
        assert created == ['camoufox.studio.team.input']
        await session.__aexit__(None, None, None)
    else:
        with pytest.raises(StudioError, match='failed to start'):
            await session.__aenter__()
        assert created == ['camoufox.studio.team.input']
    assert worker.returncode == 0 and stopped == ['display'] and session.root is None


@pytest.mark.asyncio
async def test_clipboard_reads_only_the_private_selection_and_bounds_output(session, monkeypatch):
    session, _ = session
    chunks = [b'x' * 65536] * 17
    class Process:
        returncode = None
        killed = False
        async def read(self, count):
            return chunks.pop(0) if chunks else b''
        def kill(self):
            self.killed = True
        async def wait(self):
            self.returncode = 0
    process = Process()
    process.stdout = process
    async def create(*arguments, **kwargs):
        assert '--tmpfs' in arguments and '/tmp/.X11-unix/X123' in arguments
        assert arguments[-5:] == ('-selection', 'clipboard', '-out', '-target', 'UTF8_STRING')
        assert kwargs['env'] == session.env
        return process
    monkeypatch.setattr(native.asyncio, 'create_subprocess_exec', create)
    with pytest.raises(StudioError, match='clipboard_capacity'):
        await session.clipboard('clipboard')
    assert process.killed and process.returncode == 0
    chunks[:] = ['选中文字😀\n'.encode('utf-8')]
    assert await session.clipboard('clipboard') == '选中文字😀\n'


@pytest.fixture
def session(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'
    root.mkdir()
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    executable = bundle / 'camoufox'
    executable.write_bytes(b'controlled test binary')
    directory = tmp_path / 'workspace/profiles/profile'
    directory.mkdir(parents=True)
    options = {'executable_path': str(executable), 'user_data_dir': str(directory),
               'env': {'CAMOU_CONFIG_1': json.dumps({'addons': []})}, 'firefox_user_prefs': {}}
    session = native.NativeSession(options, {'identity': {'config': {'addons': []}}})
    session.root = root
    session.env = {'DISPLAY': ':123'}
    session.display = SimpleNamespace(_display=123)
    monkeypatch.setattr(native, 'namespace', lambda: ['bwrap'])
    return session, directory


@pytest.mark.parametrize('kind', ['Uploads', 'Downloads', 'profile', 'profiles'])
def test_namespace_rejects_linked_profile_or_exchange_directories(session, tmp_path, kind):
    session, directory = session
    target = tmp_path / 'private'
    target.mkdir()
    link = directory / kind if kind in ('Uploads', 'Downloads') else (
        directory if kind == 'profile' else directory.parent)
    if link == directory:
        link.rmdir()
    elif link == directory.parent:
        directory.rmdir()
        link.rmdir()
        (target / 'profile').mkdir()
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(StudioError, match='unsafe_native_path'):
        session.sandbox(session.options)
    assert not (target / 'Uploads').exists()


def test_namespace_rejects_an_asset_bind_that_exposes_the_workspace(session):
    session, directory = session
    session.options['env']['CAMOU_CONFIG_1'] = json.dumps({'addons': [str(directory.parent.parent)]})
    with pytest.raises(StudioError, match='unsafe_native_path'):
        session.sandbox(session.options)


def test_native_accepted_downloads_are_shared_with_the_driver_inside_the_profile_bind(session):
    session, directory = session
    options = session.sandbox(session.options)
    assert options['downloads_path'] == str(directory / 'Downloads')
    arguments = shlex.split((session.root / 'browser').read_text().splitlines()[1])
    pairs = [arguments[index + 1:index + 3] for index, value in enumerate(arguments) if value == '--bind']
    assert [str(directory), str(directory)] in pairs
    assert options['firefox_user_prefs']['browser.download.dir'] == options['downloads_path']


def test_namespace_binds_only_the_fontconfig_file_not_its_cache(session, tmp_path):
    session, directory = session
    cache = tmp_path / 'font-cache'
    cache.mkdir()
    fontconfig = cache / 'fonts.conf'
    fontconfig.write_bytes(b'<fontconfig/>')
    session.options['env']['FONTCONFIG_FILE'] = str(fontconfig)
    options = session.sandbox(session.options)
    wrapper = session.root.joinpath('browser').read_text(encoding='utf-8')
    arguments = shlex.split(wrapper.splitlines()[1])
    pairs = [arguments[index + 1:index + 3] for index, value in enumerate(arguments) if value == '--ro-bind']
    assert [str(fontconfig), str(fontconfig)] in pairs
    assert [str(cache), str(cache)] not in pairs
    assert options['headless'] is False
    assert options['env']['FONTCONFIG_FILE'] == str(fontconfig)


def test_native_workers_hide_host_sockets_and_use_only_the_owned_socket(session):
    session, _ = session
    arguments = session.worker_arguments(['python', '-m', 'camoufox.studio.team.input'])
    assert arguments[arguments.index('--tmpfs') + 1] == '/tmp'
    pairs = [arguments[index + 1:index + 3] for index, value in enumerate(arguments) if value == '--ro-bind']
    assert [str(session.root / 'display-tmp/.X11-unix/X123'), '/tmp/.X11-unix/X123'] in pairs
    assert not any(source == '/tmp/.X11-unix' for source, _ in pairs)
    assert arguments[-4:] == ['--', 'python', '-m', 'camoufox.studio.team.input']


def test_native_workers_include_the_base_interpreter_without_exposing_its_home(session, tmp_path, monkeypatch):
    from camoufox.studio.team import native

    current, _ = session
    environment = tmp_path / 'venv'
    base = tmp_path / 'home/owner/python'
    environment.mkdir()
    base.mkdir(parents=True)
    monkeypatch.setattr(native.sys, 'prefix', str(environment))
    monkeypatch.setattr(native.sys, 'base_prefix', str(base))
    alias = base.parent / 'python-current'
    monkeypatch.setattr(native.sys, '_base_executable', str(alias / 'bin/python'))
    arguments = current.worker_arguments(['python', '-m', 'camoufox.studio.team.input'])
    pairs = [arguments[index + 1:index + 3] for index, value in enumerate(arguments) if value == '--ro-bind']
    assert [str(environment), str(environment)] in pairs
    assert [str(base), str(base)] in pairs
    assert [str(alias), str(alias)] in pairs
    assert not any(source == str(base.parent) for source, _ in pairs)


def test_namespace_binds_the_resolved_extension_from_launch_config(session, tmp_path):
    session, _ = session
    addon = tmp_path / 'installed-extension'
    addon.mkdir()
    saved = str(tmp_path / 'previous-host-extension')
    session.profile['identity']['config']['addons'] = [saved]
    encoded = json.dumps({'addons': [str(addon)]})
    middle = len(encoded) // 2
    session.options['env'].update({'CAMOU_CONFIG_2': encoded[middle:], 'CAMOU_CONFIG_1': encoded[:middle]})
    session.sandbox(session.options)
    arguments = shlex.split((session.root / 'browser').read_text().splitlines()[1])
    pairs = [arguments[index + 1:index + 3] for index, value in enumerate(arguments) if value == '--ro-bind']
    assert [str(addon), str(addon)] in pairs
    assert saved not in arguments
    assert session.profile['identity']['config']['addons'] == [saved]


def test_native_display_uses_selected_launch_resolution_without_rewriting_identity(session, monkeypatch):
    session, _ = session
    session.profile['identity']['config'].update({'screen.width': 1366, 'screen.height': 800})
    session.options['env']['CAMOU_CONFIG_1'] = json.dumps({'screen.width': 1920, 'screen.height': 1080})
    monkeypatch.setattr(native, 'prerequisites', lambda: None)
    monkeypatch.setattr(native.tempfile, 'mkdtemp', lambda **kwargs: str(session.root))

    async def command(*args, **kwargs):
        (session.root / 'xauth').write_bytes(struct.pack('!HHH', 256, 0, 1) + b'0')

    async def cleanup(*args):
        pass

    def display(root, width, height):
        assert (width, height) == (1920, 1080)
        raise StudioError('selected_display_verified')

    monkeypatch.setattr(session, 'command', command)
    monkeypatch.setattr(session, '__aexit__', cleanup)
    monkeypatch.setattr(session, 'create_display', lambda: display(session.root, session.width, session.height), raising=False)
    with pytest.raises(StudioError, match='selected_display_verified'):
        asyncio.run(session.__aenter__())
    assert session.profile['identity']['config']['screen.width'] == 1366
