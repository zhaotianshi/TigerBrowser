"""Digest-verified official browser packages cached for every client platform."""

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

from .. import multiversion
from ..pkgman import RepoConfig, fetch_releases, list_available_versions, verify_sha256, webdl
from .kernels import historical_version_matches, validate_kernel

PLATFORMS = {
    'lin-x86_64': ('lin', 'x86_64'),
    'win-x86_64': ('win', 'x86_64'),
    'mac-arm64': ('mac', 'arm64'),
    'mac-x86_64': ('mac', 'x86_64'),
}
# GitHub may stall a connection without closing it; the read timeout is per block.
TIMEOUT = (30, 120)


class KernelCacheError(ValueError):
    """A fixed error code that never contains host paths or upstream text."""


def host_platform():
    config = RepoConfig.get_default()
    return f"{config.get_os_name()}-{config.get_arch()}"


def _official():
    config = RepoConfig.get_default()
    return config, config.name.lower()


def _split(kernel):
    try:
        if not validate_kernel(kernel):
            raise ValueError
    except ValueError:
        raise KernelCacheError('invalid_kernel') from None
    repository, release = kernel.split('/', 1)
    if repository != _official()[1]:
        raise KernelCacheError('kernel_repository_unsupported')
    return repository, release


def _platform(platform):
    if platform not in PLATFORMS:
        raise KernelCacheError('invalid_platform')
    return platform


def _write(path, data):
    descriptor, name = tempfile.mkstemp(prefix='.kernel-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


class KernelCache:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.Lock()

    def _directory(self, kernel):
        repository, release = _split(kernel)
        return self.root / repository / release

    def available(self):
        """Official releases that publish a digest for every client platform."""
        config, repository = _official()
        releases = fetch_releases(config)
        found = {}
        for platform, (os_name, arch) in PLATFORMS.items():
            for item in list_available_versions(config, spoof_os=os_name, spoof_arch=arch, releases=releases):
                entry = found.setdefault(f'{repository}/{item.version.full_string}', {
                    'kernel_id': f'{repository}/{item.version.full_string}',
                    'version': item.version.version, 'build': item.version.build,
                    'prerelease': item.is_prerelease, 'platforms': {}})
                entry['platforms'].setdefault(platform, []).append(bool(item.sha256))
        cached = {item['kernel_id'] for item in self.list()}
        return [{**entry, 'cached': entry['kernel_id'] in cached,
                 'complete': all(entry['platforms'].get(platform) == [True] for platform in PLATFORMS)}
                for entry in found.values()]

    def _assets(self, kernel):
        config, _ = _official()
        _split(kernel)
        releases = fetch_releases(config)
        assets = {}
        for platform, (os_name, arch) in PLATFORMS.items():
            matches = [item for item in list_available_versions(
                config, spoof_os=os_name, spoof_arch=arch, releases=releases)
                if item.version.full_string == kernel.split('/', 1)[1]]
            if not matches:
                raise KernelCacheError('kernel_not_published')
            if len(matches) != 1:
                raise KernelCacheError('kernel_ambiguous')
            if not matches[0].sha256:
                # An unsigned asset could not be distinguished from a replaced one.
                raise KernelCacheError('kernel_digest_missing')
            assets[platform] = matches[0]
        return assets

    def cache(self, kernel):
        """Download all four packages, then install the host package for local runs."""
        with self._lock:
            assets = self._assets(kernel)
            directory = self._directory(kernel)
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for platform, asset in assets.items():
                record = self._record(kernel, platform)
                if record and record['sha256'] == asset.sha256:
                    continue
                self._download(directory, platform, kernel, asset)
            host = host_platform()
            if host in PLATFORMS:
                self.install(kernel, host)
            return self.entry(kernel)

    def _download(self, directory, platform, kernel, asset):
        descriptor, name = tempfile.mkstemp(prefix='.download-', dir=directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, 'w+b') as stream:
                webdl(asset.url, buffer=stream, bar=False, progress_callback=lambda *_: None, timeout=TIMEOUT)
                size = stream.seek(0, os.SEEK_END)
                if asset.asset_size is not None and size != asset.asset_size:
                    raise KernelCacheError('kernel_size_mismatch')
                try:
                    verify_sha256(stream, asset.sha256, desc=kernel)
                except Exception:
                    raise KernelCacheError('kernel_digest_mismatch') from None
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, directory / f'{platform}.zip')
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        metadata = {**asset.to_metadata(), 'kernel_id': kernel, 'platform': platform, 'size': size}
        # The record is written last, so an interrupted download is never listed.
        _write(directory / f'{platform}.json', json.dumps(metadata, sort_keys=True).encode())

    def _record(self, kernel, platform):
        directory = self._directory(kernel)
        try:
            record = json.loads((directory / f'{_platform(platform)}.json').read_bytes())
            if (directory / f'{platform}.zip').stat().st_size != record['size']:
                return None
        except (OSError, ValueError, KeyError):
            return None
        return record

    def package(self, kernel, platform):
        record = self._record(kernel, platform)
        if record is None:
            raise KernelCacheError('kernel_not_cached')
        return self._directory(kernel) / f'{platform}.zip', record

    def install(self, kernel, platform):
        """Install a cached package into the local multi-version directory."""
        archive, record = self.package(kernel, platform)
        with open(archive, 'rb') as stream:
            try:
                verify_sha256(stream, record['sha256'], desc=kernel)
            except Exception:
                raise KernelCacheError('kernel_digest_mismatch') from None
        metadata = {key: record[key] for key in (
            'version', 'build', 'prerelease', 'asset_id', 'asset_size', 'asset_updated_at', 'sha256', 'created_at')}
        return multiversion.install_archive(archive, kernel.split('/', 1)[0], metadata)

    def entry(self, kernel):
        platforms = {}
        for platform in PLATFORMS:
            record = self._record(kernel, platform)
            if record:
                platforms[platform] = {'sha256': record['sha256'], 'size': record['size']}
        return {'kernel_id': kernel, 'platforms': platforms, 'complete': len(platforms) == len(PLATFORMS)}

    def list(self):
        result = []
        _, repository = _official()
        directory = self.root / repository
        if directory.is_dir():
            for release in sorted(directory.iterdir(), reverse=True):
                try:
                    entry = self.entry(f'{repository}/{release.name}')
                except KernelCacheError:
                    continue
                if entry['platforms']:
                    result.append(entry)
        return result

    def remove(self, kernel, references):
        """Delete cached packages and the local install unless any profile still needs them."""
        directory = self._directory(kernel)
        kernels, versions = references
        # A version-only historical identity may resolve to this build alone.
        if kernel in kernels or any(historical_version_matches(kernel.split('/', 1)[1], version)
                                    for version in versions):
            raise KernelCacheError('kernel_in_use')
        with self._lock:
            from .kernels import kernel_id
            for installed in multiversion.list_installed():
                if kernel_id(installed) == kernel:
                    multiversion.remove_version(installed.path)
            if directory.exists():
                shutil.rmtree(directory)
