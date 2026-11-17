"""Runtime registration hashes actual files and never draws a replacement identity."""

import json
import hashlib

import pytest

from camoufox.studio import manifest
from camoufox.studio.store import ProfileStore, StoreError
from camoufox.studio.store import _json


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    store = ProfileStore(tmp_path / 'workspace')
    package = tmp_path / 'browser'
    package.mkdir()
    binary = package / 'camoufox'
    binary.write_bytes(b'fixture-browser')
    (package / 'fonts').mkdir()
    (package / 'fonts/face.ttf').write_bytes(b'fixture-font')
    addon = tmp_path / 'addon'
    addon.mkdir()
    (addon / 'manifest.json').write_text('{}')
    store.save_settings({'executable_path': str(binary)})
    monkeypatch.setattr(manifest, 'INSTALL_DIR', tmp_path / 'host-cache')
    monkeypatch.setattr(manifest, 'is_pinned', lambda: True)
    monkeypatch.setattr(manifest, 'default_addon_paths', lambda: [str(addon)])
    monkeypatch.setattr(manifest.platform, 'system', lambda: 'Linux')
    return store, package, addon


@pytest.mark.parametrize('artifact', ['browser', 'fonts/face.ttf', 'addon'])
def test_actual_artifact_changes_fail_without_resetting_identity(runtime, artifact):
    store, package, addon = runtime
    profile = store.save_profile({'name': 'Bound'})
    identity = {'config': {'navigator.platform': 'Linux x86_64'}}
    store.set_identity(profile['id'], identity)
    manifest.register(store.root)
    manifest.verify(store)
    target = (package / 'camoufox' if artifact == 'browser' else
              addon / 'manifest.json' if artifact == 'addon' else package / artifact)
    target.write_bytes(b'changed-artifact-without-version-change')
    with pytest.raises(StoreError, match='runtime_mismatch'):
        manifest.verify(store)
    assert store.get_profile(profile['id'])['identity'] == identity
    assert not (store.root / 'workspace.lock').exists()


def test_missing_manifest_is_not_filled_with_an_invented_baseline(runtime):
    store, _, _ = runtime
    assert manifest.registered(store) is None
    manifest.verify(store)
    assert manifest.registered(store) is None
    assert not (manifest.INSTALL_DIR / 'studio-host-id').exists()


def test_bound_runtime_cannot_be_silently_changed_to_native_desktop(runtime):
    store, _, _ = runtime
    manifest.register(store.root)
    with pytest.raises(StoreError, match='runtime_mismatch'):
        store.check_runtime(headless=False)


def test_registration_blocks_restored_or_unresolved_workspaces(runtime):
    store, _, _ = runtime
    with store._write() as (connection, _):
        connection.execute("INSERT INTO workspace_metadata VALUES ('execution_barrier', ?)",
                           (json.dumps({'reason': 'restore_quarantine'}),))
    with pytest.raises(StoreError, match='workspace_quarantined'):
        manifest.register(store.root)
    assert manifest.registered(store) is None


def test_host_binding_is_separate_from_workspace_and_required_after_registration(runtime):
    store, _, _ = runtime
    manifest.register(store.root)
    record = manifest.registered(store)
    assert 'host_id' in record and record['mode'] == 'headless'
    (manifest.INSTALL_DIR / 'studio-host-id').unlink()
    with pytest.raises(StoreError, match='runtime_host_unregistered'):
        manifest.verify(store)


def test_model_mismatch_cannot_be_registered_as_a_pinned_model(runtime, monkeypatch):
    monkeypatch.setattr(manifest, 'is_pinned', lambda: False)
    with pytest.raises(StoreError, match='runtime_model_mismatch'):
        manifest.register(runtime[0].root)


def test_portable_artifact_digest_uses_case_sensitive_relative_names(tmp_path):
    (tmp_path / 'a.js').write_bytes(b'lower')
    (tmp_path / 'B.js').write_bytes(b'upper')
    expected = hashlib.sha256(
        _json(['B.js', hashlib.sha256(b'upper').hexdigest()]).encode()
        + _json(['a.js', hashlib.sha256(b'lower').hexdigest()]).encode()).hexdigest()
    assert manifest._tree(tmp_path, portable=True) == expected
