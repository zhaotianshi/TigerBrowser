"""Desktop client installers a team server offers, described by its client root's manifest.json."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

# The check-out, sync and desktop bridge contract between a server and its clients.
PROTOCOL = 1
# The oldest protocol of the other side this build still speaks, in both directions.
MINIMUM_PROTOCOL = 1
MANIFEST = 'manifest.json'
# Keys match kernel_cache.PLATFORMS, so a client's platform names both its kernel and its installer.
SUFFIXES = {'win-x86_64': '.exe', 'mac-arm64': '.dmg', 'mac-x86_64': '.dmg', 'lin-x86_64': '.deb'}
FIELDS = frozenset({'version', 'platform', 'file', 'sha256', 'size', 'minimum_protocol', 'protocol'})
BLOCK = 1024 * 1024


class ClientReleaseError(ValueError):
    """A fixed error code; manifest contents and host paths never reach clients."""


def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,6}(\.\d{1,6}){1,3}', value):
        raise ClientReleaseError('invalid_client_package')
    return tuple(int(part) for part in value.split('.'))


def valid_file(platform, name):
    """A bare installer name for the platform, never a path."""
    return (platform in SUFFIXES and isinstance(name, str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', name) is not None
            and name.endswith(SUFFIXES[platform]))


def compatible(minimum, protocol):
    """Whether a build speaking protocols `minimum`..`protocol` works with this one."""
    return minimum <= PROTOCOL and MINIMUM_PROTOCOL <= protocol


def validate(record):
    try:
        valid = (isinstance(record, dict) and set(record) == FIELDS and version_key(record['version'])
                 and valid_file(record['platform'], record['file'])
                 and re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
                 and type(record['size']) is int and record['size'] > 0
                 and type(record['minimum_protocol']) is int and type(record['protocol']) is int
                 and 1 <= record['minimum_protocol'] <= record['protocol'])
    except TypeError:
        valid = False
    if not valid:
        raise ClientReleaseError('invalid_client_package')
    return record


def digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(BLOCK), b''):
            value.update(block)
    return value.hexdigest()


def describe(path, platform, version):
    """Write the record package-studio.py places beside each installer."""
    path = Path(path)
    record = validate({'version': version, 'platform': platform, 'file': path.name, 'sha256': digest(path),
                       'size': path.stat().st_size, 'minimum_protocol': MINIMUM_PROTOCOL, 'protocol': PROTOCOL})
    path.with_name(path.name + '.json').write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return record


def write_manifest(directory):
    """Verify every installer record in a directory and combine them into its manifest.json."""
    directory = Path(directory)
    packages = {}
    for path in sorted(directory.glob('*.json')):
        if path.name == MANIFEST:
            continue
        record = validate(json.loads(path.read_text(encoding='utf-8')))
        if path.name != record['file'] + '.json' or record['platform'] in packages:
            raise ClientReleaseError('invalid_client_package')
        installer = directory / record['file']
        if installer.stat().st_size != record['size'] or digest(installer) != record['sha256']:
            raise ClientReleaseError('client_package_digest_mismatch')
        packages[record['platform']] = record
    if not packages:
        raise ClientReleaseError('client_packages_missing')
    descriptor, name = tempfile.mkstemp(prefix='.manifest-', dir=directory)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps({'packages': list(packages.values())}, indent=2) + '\n')
        os.replace(name, directory / MANIFEST)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return packages


def offered(root):
    """Packages by platform; an absent manifest offers none, a damaged one is reported, never guessed."""
    root = Path(root)
    try:
        records = [validate(item) for item in json.loads((root / MANIFEST).read_bytes())['packages']]
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, KeyError):
        raise ClientReleaseError('client_packages_unavailable') from None
    packages = {}
    for record in records:
        path = root / record['file']
        # A build that cannot speak to this server, or a truncated copy, is never offered.
        if (record['platform'] in packages or not compatible(record['minimum_protocol'], record['protocol'])
                or path.is_symlink() or not path.is_file() or path.stat().st_size != record['size']):
            raise ClientReleaseError('client_packages_unavailable')
        packages[record['platform']] = record
    return packages
