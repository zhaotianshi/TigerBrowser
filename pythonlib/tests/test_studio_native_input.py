"""Private input acknowledgements include native release, errors and EOF cleanup."""

import io
import json
import sys
from types import SimpleNamespace

import pytest

from camoufox.studio.engine import StudioError
from camoufox.studio.team import input as native_input
from camoufox.studio.team.native import KEYS, MAX_NATIVE_INPUT_BYTES


@pytest.fixture
def connection(monkeypatch):
    class Connection:
        def __init__(self):
            self.keymap = {name: index for index, name in enumerate(KEYS.values(), 8)}
            self.keys, self.buttons = set(), set()
            self.pointer = (0, 0)
            self.states = []
            self.closed = False

        def has_extension(self, name):
            return name == 'XTEST'

        def keysym_to_keycode(self, name):
            return self.keymap.get(name, 0)

        def screen(self):
            root = SimpleNamespace(query_pointer=lambda: SimpleNamespace(child=0))
            return SimpleNamespace(width_in_pixels=1920, height_in_pixels=1080, root=root)

        def set_error_handler(self, handler):
            self.error = handler

        def xtest_fake_input(self, event, detail=0, **position):
            if event == 'move':
                self.pointer = position['x'], position['y']
            elif event == 'key-down':
                self.keys.add(detail)
            elif event == 'key-up':
                self.keys.discard(detail)
            elif event == 'button-down':
                self.buttons.add(detail)
            elif event == 'button-up':
                self.buttons.discard(detail)

        def sync(self):
            self.states.append((self.keys.copy(), self.buttons.copy(), self.pointer))

        def close(self):
            self.closed = True

    connection = Connection()
    class BadWindow(Exception):
        pass

    xlib = SimpleNamespace(
        X=SimpleNamespace(KeyPress='key-down', KeyRelease='key-up',
                          ButtonPress='button-down', ButtonRelease='button-up', MotionNotify='move',
                          RevertToPointerRoot=1, CurrentTime=0),
        XK=SimpleNamespace(string_to_keysym=lambda name: name),
        error=SimpleNamespace(BadWindow=BadWindow, BadMatch=BadWindow, CatchError=lambda *_: None),
        display=SimpleNamespace(Display=lambda: connection))
    monkeypatch.setitem(sys.modules, 'Xlib', xlib)
    return connection


def test_worker_release_and_shortcuts_leave_no_pressed_input(connection):
    dispatcher = native_input.NativeInput(connection)
    dispatcher.dispatch({'kind': 'key', 'code': 'ShiftLeft', 'down': True})
    dispatcher.dispatch({'kind': 'pointer', 'x': 100, 'y': 200, 'button': 1, 'down': True})
    assert connection.states[-1] == ({dispatcher.keys['ShiftLeft']}, {1}, (100, 200))
    dispatcher.dispatch({'kind': 'drain'})
    assert connection.states[-1] == (set(), set(), (100, 200))
    for code in ('KeyC', 'KeyV'):
        dispatcher.dispatch({'kind': 'key', 'code': 'AltLeft', 'down': True})
        dispatcher.dispatch({'kind': 'shortcut', 'code': code})
        assert connection.states[-1] == (set(), set(), (100, 200))
    dispatcher.dispatch({'kind': 'wheel', 'up': True})
    assert not connection.buttons


def test_click_focuses_a_popup_before_dispatching_its_button_press(connection):
    dispatcher = native_input.NativeInput(connection)
    calls = []
    main = SimpleNamespace(id=2)
    popup = SimpleNamespace(id=3, get_attributes=lambda: SimpleNamespace(override_redirect=False),
                            set_input_focus=lambda *_, **__: calls.append('focus popup'))
    root = SimpleNamespace(id=1, query_pointer=lambda: SimpleNamespace(child=popup))
    dispatcher.root = root
    connection.get_input_focus = lambda: SimpleNamespace(focus=main)
    main.query_tree = lambda: SimpleNamespace(parent=root)
    original = connection.xtest_fake_input

    def input(event, *args, **kwargs):
        calls.append(event)
        original(event, *args, **kwargs)

    connection.xtest_fake_input = input
    dispatcher.dispatch({'kind': 'pointer', 'x': 200, 'y': 150, 'button': 0, 'down': False})
    dispatcher.dispatch({'kind': 'pointer', 'x': 200, 'y': 150, 'button': 1, 'down': True})
    assert calls == ['move', 'move', 'focus popup', 'button-down']


@pytest.mark.parametrize('event', [
    {'kind': 'pointer', 'x': 1920, 'y': 0, 'button': 1, 'down': True},
    {'kind': 'key', 'code': 'Unidentified', 'down': True},
    {'kind': 'shortcut', 'code': 'KeyA'},
    {'kind': 'text', 'text': 'private text'},
])
def test_worker_rejects_invalid_events_without_dispatch(connection, event):
    dispatcher = native_input.NativeInput(connection)
    with pytest.raises((StudioError, ValueError)):
        dispatcher.dispatch(event)
    assert not connection.states and not connection.keys and not connection.buttons


def test_worker_acknowledges_after_native_sync_and_releases_on_eof(connection, monkeypatch):
    event = {'kind': 'key', 'code': 'ControlLeft', 'down': True}

    class Output(io.BytesIO):
        def write(self, data):
            if data == b'A':
                assert connection.states[-1][0] == {connection.keymap['Control_L']}
            return super().write(data)

    output = Output()
    monkeypatch.setattr(native_input, 'sys', SimpleNamespace(
        stdin=SimpleNamespace(buffer=io.BytesIO(json.dumps(event).encode() + b'\n')),
        stdout=SimpleNamespace(buffer=output)))
    native_input.main()
    assert output.getvalue() == b'RA'
    assert not connection.keys and not connection.buttons and connection.closed


@pytest.mark.parametrize('message', [b'{' + b'x' * MAX_NATIVE_INPUT_BYTES, b'{}', b'{}\n'])
def test_worker_rejects_unbounded_truncated_or_invalid_messages(connection, monkeypatch, message):
    output = io.BytesIO()
    monkeypatch.setattr(native_input, 'sys', SimpleNamespace(
        stdin=SimpleNamespace(buffer=io.BytesIO(message)), stdout=SimpleNamespace(buffer=output)))
    with pytest.raises((ValueError, StudioError)):
        native_input.main()
    assert output.getvalue() == b'R'
    assert connection.closed


def test_worker_does_not_acknowledge_x11_errors(connection, monkeypatch):
    dispatcher = native_input.NativeInput(connection)
    def fail():
        # python-xlib's call_error_handler catches exceptions from callbacks.
        try:
            connection.error(None, None)
        except RuntimeError:
            pass
    monkeypatch.setattr(connection, 'sync', fail)
    with pytest.raises(RuntimeError, match='X11 input failed'):
        dispatcher.dispatch({'kind': 'key', 'code': 'KeyA', 'down': True})


def test_worker_requires_a_complete_native_keymap(connection):
    connection.keymap.pop('Control_L')
    with pytest.raises(RuntimeError, match='incomplete keyboard map'):
        native_input.NativeInput(connection)
