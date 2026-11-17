"""Ordered XTest input on one private display; acknowledge only after X11 sync."""

import json
import sys

from .native import KEYS, MAX_NATIVE_INPUT_BYTES, validate_event


class NativeInput:
    def __init__(self, connection):
        from Xlib import XK

        self.connection = connection
        if not connection.has_extension('XTEST'):
            raise RuntimeError('The private display requires XTEST.')
        self.keys = {code: connection.keysym_to_keycode(XK.string_to_keysym(key))
                     for code, key in KEYS.items()}
        if not all(self.keys.values()):
            raise RuntimeError('The private display has an incomplete keyboard map.')
        screen = connection.screen()
        self.root = screen.root
        self.width, self.height = screen.width_in_pixels, screen.height_in_pixels
        self.pressed_keys = set()
        self.pressed_buttons = set()
        self.x11_failed = False
        connection.set_error_handler(self.failed)

    def failed(self, error, request):
        # python-xlib swallows callback exceptions; check the flag after sync.
        self.x11_failed = True

    def sync(self):
        self.connection.sync()
        if self.x11_failed:
            raise RuntimeError('Private X11 input failed.')

    def key(self, code, down):
        from Xlib import X

        if down:
            self.pressed_keys.add(code)
        self.connection.xtest_fake_input(X.KeyPress if down else X.KeyRelease, code)
        if not down:
            self.pressed_keys.discard(code)

    def button(self, button, down):
        from Xlib import X

        if down:
            self.pressed_buttons.add(button)
        self.connection.xtest_fake_input(X.ButtonPress if down else X.ButtonRelease, button)
        if not down:
            self.pressed_buttons.discard(button)

    def drain(self):
        for code in sorted(self.pressed_keys):
            self.key(code, False)
        for button in sorted(self.pressed_buttons):
            self.button(button, False)
        self.sync()

    def focus_clicked_window(self):
        from Xlib import X, error

        # The private display has no window manager to focus a clicked popup.
        target = self.root.query_pointer().child
        if not target:
            return
        try:
            if target.get_attributes().override_redirect:
                return
            focus = self.connection.get_input_focus().focus
            while not isinstance(focus, int) and focus.id != self.root.id:
                if focus.id == target.id:
                    return
                focus = focus.query_tree().parent
        except error.BadWindow:
            # The browser can close a popup while processing the pointer move.
            return
        vanished = error.CatchError(error.BadWindow, error.BadMatch)
        target.set_input_focus(X.RevertToPointerRoot, X.CurrentTime, onerror=vanished)

    def dispatch(self, event):
        from Xlib import X

        if event == {'kind': 'drain'}:
            self.drain()
            return
        if (isinstance(event, dict) and set(event) == {'kind', 'code'}
                and event['kind'] == 'shortcut' and event['code'] in ('KeyC', 'KeyV')):
            self.drain()
            self.key(self.keys['ControlLeft'], True)
            self.key(self.keys[event['code']], True)
            self.key(self.keys[event['code']], False)
            self.key(self.keys['ControlLeft'], False)
        else:
            validate_event(event, self.width, self.height)
            if event['kind'] == 'pointer':
                self.connection.xtest_fake_input(X.MotionNotify, x=event['x'], y=event['y'])
                if event['button']:
                    if event['button'] == 1 and event['down']:
                        self.focus_clicked_window()
                    self.button(event['button'], event['down'])
            elif event['kind'] == 'key':
                self.key(self.keys[event['code']], event['down'])
            elif event['kind'] == 'wheel':
                button = 4 if event['up'] else 5
                self.button(button, True)
                self.button(button, False)
            else:
                raise ValueError('Text requires the private selection owner.')
        self.sync()


def main():
    from Xlib import display

    connection = display.Display()
    dispatcher = None
    try:
        dispatcher = NativeInput(connection)
        dispatcher.sync()
        stream, output = sys.stdin.buffer, sys.stdout.buffer
        output.write(b'R')
        output.flush()
        while message := stream.readline(MAX_NATIVE_INPUT_BYTES + 1):
            if len(message) > MAX_NATIVE_INPUT_BYTES or not message.endswith(b'\n'):
                raise ValueError('Invalid native input message.')
            dispatcher.dispatch(json.loads(message))
            output.write(b'A')
            output.flush()
    finally:
        try:
            if dispatcher is not None:
                dispatcher.drain()
        finally:
            connection.close()


if __name__ == '__main__':
    main()
