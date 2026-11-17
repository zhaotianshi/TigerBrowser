"""PyInstaller entry point; route worker processes before creating a GUI."""

from multiprocessing import freeze_support
import os
import sys

if __name__ == '__main__':
    # Windowed Python has no standard streams; libraries may still flush diagnostics.
    # Existing worker pipes must stay untouched.
    for stream in ('stdout', 'stderr'):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, 'w', encoding='utf-8'))
    freeze_support()
    from camoufox.studio.desktop import main
    raise SystemExit(main())
