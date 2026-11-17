"""Desktop entry point, including child-process modes in standalone builds."""

import argparse
import json
import os
from pathlib import Path


def check_runtime():
    """Check native and data dependencies before packaging a client; browsers come from the server."""
    os.environ['QT_QPA_PLATFORM'] = 'offscreen'
    from PySide6.QtCore import qVersion
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication
    from ..addons import confirm_paths
    from ..fpgen_model import is_pinned
    from .client.shell import channel_script
    from .client.update import current_version
    from .distribution import bundled_path, default_addon_paths
    from .secrets import KeyringSecretStore

    if bundled_path('addons') is None or not is_pinned():
        raise RuntimeError('A complete standalone bundle with the verified model is required.')
    confirm_paths(default_addon_paths())
    backend = KeyringSecretStore('TigerBrowser/runtime-check')._native_backend()
    app = QApplication.instance() or QApplication([])
    # Creating a view loads the QtWebEngine process; the console also needs the WebChannel script.
    QWebEngineView().deleteLater()
    channel_script()
    app.processEvents()
    print(json.dumps({'version': current_version(), 'qt': qVersion(), 'model': 'verified',
                      'credentials': type(backend).__name__}))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='TigerBrowser')
    parser.add_argument('--cache-dir', type=Path,
                        help='Use an independent client cache instead of the per-user data directory.')
    parser.add_argument('--check-runtime', action='store_true', help='Validate the standalone runtime.')
    args = parser.parse_args(argv)
    if args.check_runtime:
        return check_runtime()
    from .client.shell import main as shell_main
    return shell_main(args.cache_dir)


if __name__ == '__main__':
    raise SystemExit(main())
