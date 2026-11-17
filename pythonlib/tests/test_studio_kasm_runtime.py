"""Only the pinned cache-corrected server is advertised for production use."""

import hashlib
import json
from pathlib import Path

import pytest

from camoufox.studio.engine import StudioError
from camoufox.studio.team import kasm


def test_installer_and_launcher_bind_the_same_loader():
    data = Path(__file__).resolve().parents[2] / 'scripts/data'
    pins = json.loads((data / 'studio-kasmvnc.json').read_text())
    assert pins['version'] == kasm.RUNTIME_VERSION
    assert pins['ffmpeg_loader_fix_sha256'] == kasm.FFMPEG_LOADER_FIX
    assert hashlib.sha256((data / 'studio-kasmvnc-ffmpeg-loader.patch').read_bytes()).hexdigest() == kasm.FFMPEG_LOADER_FIX


@pytest.fixture
def runtime(tmp_path):
    binaries = {}
    for name in ('Xkasmvnc', 'kasmvncpasswd'):
        content = ('synthetic-' + name).encode()
        (tmp_path / name).write_bytes(content)
        binaries[name] = hashlib.sha256(content).hexdigest()
    metadata = {'schema': 1, 'version': '1.5.0+studio.2', 'upstream_revision': kasm.REVISION,
                'cache_fix_revision': kasm.CACHE_FIX, 'private_ipc_fix_sha256': kasm.PRIVATE_IPC_FIX,
                'ffmpeg_loader_fix_sha256': kasm.FFMPEG_LOADER_FIX,
                'binaries': binaries}
    (tmp_path / 'studio-kasmvnc-runtime.json').write_text(json.dumps(metadata))
    return tmp_path, metadata


def test_pinned_runtime_checks_both_binary_hashes(runtime):
    root, _ = runtime
    kasm.verify_runtime(root)
    (root / 'Xkasmvnc').write_bytes(b'changed-server')
    with pytest.raises(StudioError, match='kasmvnc_runtime_unverified'):
        kasm.verify_runtime(root)


@pytest.mark.parametrize('field,value', [
    ('schema', 2), ('version', '1.5.0'), ('version', '1.5.0+studio.1'), ('upstream_revision', 'other'),
    ('cache_fix_revision', 'other'), ('private_ipc_fix_sha256', 'other'),
    ('ffmpeg_loader_fix_sha256', 'other'), ('binaries', {}),
])
def test_runtime_rejects_an_uncertified_manifest(runtime, field, value):
    root, metadata = runtime
    metadata[field] = value
    (root / 'studio-kasmvnc-runtime.json').write_text(json.dumps(metadata))
    with pytest.raises(StudioError, match='kasmvnc_runtime_unverified'):
        kasm.verify_runtime(root)


def test_runtime_rejects_missing_loader_provenance(runtime):
    root, metadata = runtime
    metadata.pop('ffmpeg_loader_fix_sha256', None)
    (root / 'studio-kasmvnc-runtime.json').write_text(json.dumps(metadata))
    with pytest.raises(StudioError, match='kasmvnc_runtime_unverified'):
        kasm.verify_runtime(root)


def test_runtime_rejects_missing_or_malformed_manifest(runtime):
    root, _ = runtime
    metadata = root / 'studio-kasmvnc-runtime.json'
    metadata.unlink()
    with pytest.raises(StudioError, match='kasmvnc_runtime_unverified'):
        kasm.verify_runtime(root)
    metadata.write_text('[]')
    with pytest.raises(StudioError, match='kasmvnc_runtime_unverified'):
        kasm.verify_runtime(root)
