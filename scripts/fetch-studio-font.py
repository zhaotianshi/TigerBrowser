"""Fetch the pinned, licensed console font; browsers never contact a font CDN."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from camoufox.pkgman import verify_sha256, webdl


def main():
    repository = Path(__file__).resolve().parent.parent
    spec = json.loads((repository / 'scripts/data/studio-font.json').read_text(encoding='utf-8'))
    target = repository / 'frontend/studio/public/assets'
    for asset in spec['assets']:
        path = target / asset['name']
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == asset['sha256']:
            continue
        with webdl(asset['url'], desc=asset['name']) as data:
            verify_sha256(data, asset['sha256'], asset['name'])
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=target, prefix=path.name + '-', suffix='.part',
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(data.read())
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
