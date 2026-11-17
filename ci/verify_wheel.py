"""Validate the actual frontend files contained in one locally built wheel."""

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from camoufox.studio.team.assets import built_assets


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel_dir', type=Path)
    args = parser.parse_args()
    wheels = list(args.wheel_dir.glob('camoufox-*.whl'))
    if len(wheels) != 1:
        parser.error('Expected exactly one freshly built Camoufox wheel.')
    with TemporaryDirectory(prefix='studio-wheel-') as directory:
        with ZipFile(wheels[0]) as wheel:
            wheel.extractall(directory)
        assets = built_assets(Path(directory) / 'camoufox/studio/team/web')
        print(f'Wheel frontend verified: {len(assets)} declared assets.')


if __name__ == '__main__':
    main()
