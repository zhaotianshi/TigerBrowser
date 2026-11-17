"""Integrity and archive boundaries for the optional KasmVNC source installer."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('studio_kasmvnc_installer', ROOT / 'scripts/install-studio-kasmvnc.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


@pytest.mark.parametrize('uid,prefix', [(0, []), (1001, ['sudo'])])
def test_system_dependencies_use_sudo_only_for_non_root(uid, prefix, monkeypatch):
    calls = []
    monkeypatch.setattr(installer.os, 'geteuid', lambda: uid, raising=False)
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **options: calls.append((command, options)))
    installer.install_system_dependencies()
    assert calls == [
        ([*prefix, 'apt-get', 'update', '--error-on=any'], {'check': True}),
        ([*prefix, 'apt-get', 'install', '-y', '--no-install-recommends', *installer.PACKAGES], {'check': True}),
    ]


def test_download_publishes_only_verified_content(tmp_path, monkeypatch):
    payload = b'pinned source'
    expected = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(installer.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(payload))
    source = {'url': 'https://example.invalid/source', 'sha256': expected}
    path = installer.fetch(source, tmp_path)
    assert path.name == expected
    assert path.read_bytes() == payload
    assert list(tmp_path.iterdir()) == [path]


def test_download_digest_failure_leaves_no_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'changed source'))
    with pytest.raises(ValueError, match='Source digest mismatch'):
        installer.fetch({'url': 'https://example.invalid/source', 'sha256': '0' * 64}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_corrupt_cache_fails_without_downloading(tmp_path, monkeypatch):
    expected = '0' * 64
    (tmp_path / expected).write_bytes(b'corrupt cache')
    monkeypatch.setattr(installer.urllib.request, 'urlopen', lambda *args, **kwargs: pytest.fail('unexpected network request'))
    with pytest.raises(ValueError, match='Cached source digest mismatch'):
        installer.fetch({'url': 'https://example.invalid/source', 'sha256': expected}, tmp_path)


def test_pinned_sources_do_not_use_timestamped_gitiles_archives():
    pins = json.loads(installer.PINS.read_text())
    for name, source in pins['sources'].items():
        url = urlsplit(source['url'])
        assert not (url.hostname.endswith('.googlesource.com') and '/+archive/' in url.path), (
            f'{name}: Gitiles tar entry timestamps change between downloads, invalidating archive pins.'
        )


@pytest.mark.parametrize('name', ['../outside', '/outside', 'source/../../outside'])
def test_source_archive_rejects_escape(tmp_path, name):
    archive = tmp_path / 'source.tar'
    with tarfile.open(archive, 'w') as output:
        entry = tarfile.TarInfo(name)
        entry.size = 1
        output.addfile(entry, io.BytesIO(b'x'))
    with pytest.raises(ValueError, match='Unsafe source archive path'):
        installer.unpack(archive, tmp_path / 'extracted')
    assert not (tmp_path / 'outside').exists()


def test_manifest_binds_both_binaries_and_exact_sources(tmp_path):
    pins = json.loads(installer.PINS.read_text())
    for name in ('Xkasmvnc', 'kasmvncpasswd'):
        (tmp_path / name).write_bytes(name.encode())
    metadata = installer.runtime_manifest(pins, tmp_path, 'compiler')
    assert metadata['schema'] == 1
    assert metadata['version'] == '1.5.0+studio.2'
    assert metadata['upstream_revision'] == '17265facc40ab50db5740cdf0d12c61173edafc9'
    assert metadata['cache_fix_revision'] == 'a05a0984f55d4aa4de15b001f581c5015c177d78'
    assert metadata['private_ipc_fix_sha256'] == installer.digest(installer.PINS.with_name('studio-kasmvnc-private-runtime.patch'))
    assert metadata['ffmpeg_loader_fix_sha256'] == installer.digest(installer.PINS.with_name('studio-kasmvnc-ffmpeg-loader.patch'))
    assert installer.PINS.with_name('studio-kasmvnc-private-ipc.c').is_file()
    assert installer.PINS.with_name('studio-kasmvnc-ffmpeg-test.cxx').is_file()
    assert metadata['binaries'] == {
        name: hashlib.sha256(name.encode()).hexdigest()
        for name in ('Xkasmvnc', 'kasmvncpasswd')
    }
    assert metadata['sources'] == pins['sources']
    assert all(source['url'].startswith('https://') and len(source['sha256']) == 64
               for source in pins['sources'].values())
