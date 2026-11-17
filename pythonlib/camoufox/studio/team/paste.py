"""Supervise explicit text on a private X display, without reading clipboard data."""

import select
import struct
import subprocess
import sys
import time

from .native import MAX_TEXT_BYTES, validate_event

from Xlib import display


def main():
    stream = sys.stdin.buffer
    header = stream.read(4)
    if len(header) != 4:
        raise ValueError('Missing native text length.')
    length = struct.unpack('!I', header)[0]
    if not 0 < length <= MAX_TEXT_BYTES:
        raise ValueError('Invalid native text length.')
    data = stream.read(length)
    text = data.decode('utf-8')
    if len(data) != length:
        raise ValueError('Invalid native text.')
    validate_event({'kind': 'text', 'text': text}, 0, 0)
    connection = display.Display()
    worker = None
    try:
        clipboard = connection.intern_atom('CLIPBOARD')
        previous = connection.get_selection_owner(clipboard)
        # Foreground mode supervises the actual owner while xclip provides
        # GTK's native conversions, rather than duplicating the selection protocol.
        worker = subprocess.Popen(['xclip', '-selection', 'clipboard', '-in', '-quiet'],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        worker.stdin.write(data)
        worker.stdin.close()
        deadline = time.monotonic() + 5
        while connection.get_selection_owner(clipboard) == previous:
            if worker.poll() is not None or time.monotonic() >= deadline:
                raise RuntimeError('Private text selection ownership failed.')
            if select.select([stream.fileno()], [], [], .01)[0]:
                return
        sys.stdout.buffer.write(b'R')
        sys.stdout.buffer.flush()
        while worker.poll() is None:
            if select.select([stream.fileno()], [], [], .1)[0]:
                return
    finally:
        connection.close()
        if worker is not None:
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)


if __name__ == '__main__':
    main()
