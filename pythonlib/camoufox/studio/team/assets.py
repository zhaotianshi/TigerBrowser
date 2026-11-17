"""Validate the packaged Vue manifest and the fixed public decoder/font assets."""

import json
import re
from pathlib import Path

TYPES = {'.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2',
         '.svg': 'image/svg+xml', '.LICENSE': 'text/plain'}
PUBLIC = {
    'assets/noto-sans-sc.woff2': 'font/woff2',
    'assets/noto-sans-sc.LICENSE': 'text/plain',
    'assets/studio.svg': 'image/svg+xml',
    'kasm-client.js': 'text/javascript',
    'kasm-client/rfb.js': 'text/javascript',
    'kasm-client/rfb.js.map': 'application/json',
    'kasm-client/LICENSE.txt': 'text/plain',
    'kasm-client/MPL-2.0.txt': 'text/plain',
    'kasm-client/AUTHORS': 'text/plain',
    'kasm-client/pako-LICENSE.txt': 'text/plain',
    'kasm-client/PROVENANCE.json': 'application/json',
}


def built_assets(root):
    root = Path(root).expanduser().resolve()
    manifest = root / '.vite' / 'manifest.json'
    try:
        if manifest.resolve() != manifest or manifest.stat().st_size > 2 * 1024 * 1024:
            raise ValueError()
        entries = json.loads(manifest.read_bytes())
        if not isinstance(entries, dict) or not entries or len(entries) > 256:
            raise ValueError()
        names = {'index.html', *PUBLIC}
        for entry in entries.values():
            names.add(entry['file'])
            for field in ('css', 'assets'):
                names.update(entry.get(field, []))
        assets = {}
        for name in names:
            if (name != 'index.html' and name not in PUBLIC
                    and not re.fullmatch(r'assets/[A-Za-z0-9][A-Za-z0-9_.-]*', name)):
                raise ValueError()
            path = root / name
            content_type = 'text/html' if name == 'index.html' else PUBLIC.get(name, TYPES.get(path.suffix))
            if (not content_type or path.is_symlink() or not path.is_file()
                    or path.resolve() != path or path.stat().st_size > 16 * 1024 * 1024):
                raise ValueError()
            assets[name] = (path, content_type)
        return assets
    except (OSError, ValueError, KeyError, TypeError):
        raise ValueError('The frontend build manifest or its declared assets are invalid.') from None
