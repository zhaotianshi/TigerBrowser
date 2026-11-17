"""Regional timezone choices from the metadata on ZoneInfo's data path."""

from importlib.resources import files
from pathlib import Path
import zoneinfo


def timezone_catalog():
    source = None
    for directory in zoneinfo.TZPATH:
        root = Path(directory)
        if root.is_dir() and any(root.iterdir()):
            source = root / 'tzdata.zi'
            break
    if source is None:
        source = files('tzdata.zoneinfo').joinpath('tzdata.zi')
    lines = [line.split() for line in source.read_text(encoding='utf-8').splitlines()]
    version = next((parts[2] for parts in lines if len(parts) == 3 and parts[:2] == ['#', 'version']), None)
    names = sorted({parts[1] for parts in lines
                    if len(parts) >= 2 and parts[0] in ('Z', 'Zone') and '/' in parts[1]})
    if not version or not names:
        raise ValueError('The timezone database metadata is invalid.')
    for name in names:
        zoneinfo.ZoneInfo(name)
    return {'version': version, 'timezones': [
        {'name': name, 'region': name.partition('/')[0]} for name in names]}
