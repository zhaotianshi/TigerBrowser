"""Persistence boundaries for local profiles and operating-system credentials."""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from camoufox.studio.fingerprint_options import generation_arguments, normalize_options
from camoufox.studio.secrets import KeyringSecretStore, SecretStoreError
from camoufox.studio.store import ProfileStore, ValidationError, parse_proxy_line


class MemorySecrets:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


@pytest.fixture
def store(tmp_path):
    return ProfileStore(tmp_path, secret_store=MemorySecrets())


def test_proxy_timezone_hint_survives_reopen_but_not_route_changes(store):
    proxy = store.save_proxy({'name': 'Timezone probe', 'server': 'socks5://127.0.0.1:1080'})
    assert store.record_proxy_timezone(proxy['id'], proxy['updated_at'], 'Asia/Shanghai')
    reopened = ProfileStore(store.root, secret_store=MemorySecrets())
    hint = reopened.get_proxy(proxy['id'])['last_probe']
    assert set(hint) == {'timezone', 'checked_at'} and hint['timezone'] == 'Asia/Shanghai'
    assert hint['checked_at']
    assert 'last_probe' not in reopened.export_config()['proxies'][0]
    store.save_proxy({'server': 'socks5://127.0.0.1:1081'}, proxy['id'])
    assert 'last_probe' not in store.get_proxy(proxy['id'])
    assert store.record_proxy_timezone(proxy['id'], proxy['updated_at'], 'Etc/UTC') is False
    assert 'last_probe' not in store.get_proxy(proxy['id'])


@pytest.mark.parametrize('options', [None, {}, {'hardware_concurrency': 8}])
def test_new_profiles_default_to_full_hd_screen(store, options):
    data = {'name': 'Full HD', 'os': 'windows'}
    if options is not None:
        data['fingerprint'] = options
    profile = store.save_profile(data)
    values = profile['fingerprint']
    assert values['screen_mode'] == 'custom'
    assert (values['screen_width'], values['screen_height']) == (1920, 1080)
    bounds = generation_arguments(values, profile['os'])['screen'].as_conditions()
    assert bounds['screen.width'](1920) and not bounds['screen.width'](1366)
    assert bounds['screen.height'](1080) and not bounds['screen.height'](768)
    assert store.get_profile(profile['id'])['fingerprint'] == values


@pytest.mark.parametrize('mode', ['identity', 'native', 'custom'])
def test_new_profile_preserves_explicit_screen_choice(store, mode):
    options = {'screen_mode': mode, 'screen_width': 2560, 'screen_height': 1440}
    profile = store.save_profile({'name': 'Chosen screen', 'fingerprint': options})
    assert profile['fingerprint'] == normalize_options(options)


def test_moved_workspace_retains_native_credentials_and_browser_data(tmp_path, monkeypatch):
    credentials = {}
    backend = SimpleNamespace(
        get_password=lambda service, user: credentials.get((service, user)),
        set_password=lambda service, user, value: credentials.__setitem__((service, user), value),
    )
    monkeypatch.setattr(KeyringSecretStore, '_native_backend', lambda self: backend)
    original = ProfileStore(tmp_path / 'original')
    proxy = original.save_proxy({'name': 'Proxy', 'server': 'localhost:8080', 'password': 'test-secret'})
    profile = original.save_profile({'name': 'Profile', 'proxy_id': proxy['id']})
    original.set_identity(profile['id'], {'navigator.userAgent': 'saved-identity'})
    directory = original.profile_dir(profile['id'])
    directory.mkdir(parents=True)
    (directory / 'session-data').write_bytes(b'preserved browser data')
    moved_root = tmp_path / 'moved'
    shutil.move(str(original.root), moved_root)
    moved = ProfileStore(moved_root)
    assert moved.get_proxy(proxy['id'], reveal_password=True)['password'] == 'test-secret'
    assert moved.get_profile(profile['id'])['identity'] == {'navigator.userAgent': 'saved-identity'}
    assert (moved.profile_dir(profile['id']) / 'session-data').read_bytes() == b'preserved browser data'


def test_legacy_workspace_keeps_path_derived_credential_namespace(tmp_path):
    original = ProfileStore(tmp_path)
    with sqlite3.connect(original.database_path) as connection:
        connection.execute('DROP TABLE IF EXISTS workspace_metadata')
    namespace = 'camoufox-studio/' + hashlib.sha256(str(tmp_path.resolve()).encode('utf-8')).hexdigest()[:24]
    upgraded = ProfileStore(tmp_path)
    assert upgraded.secret_store.namespace == namespace
    with sqlite3.connect(upgraded.database_path) as connection:
        assert connection.execute(
            "SELECT value FROM workspace_metadata WHERE key = 'credential_namespace'"
        ).fetchone() == (namespace,)


def test_profile_roundtrip_clone_and_archive(store):
    proxy = store.save_proxy({"name": "Office", "server": "localhost:8080"})
    profile = store.save_profile({
        "name": "Research", "group_name": "Work", "tags": ["daily", "qa"],
        "notes": "Persistent session", "proxy_id": proxy["id"], "os": "windows",
        "start_urls": ["https://example.com/", "about:blank"],
    })
    store.set_identity(profile["id"], {"navigator.userAgent": "test-identity"})
    store.mark_launched(profile["id"])
    restarted = ProfileStore(store.root, secret_store=store.secret_store)
    loaded = restarted.get_profile(profile["id"])
    assert loaded["tags"] == ["daily", "qa"]
    assert loaded["identity"] == {"navigator.userAgent": "test-identity"}
    assert loaded["last_launch_at"]
    clone = store.clone_profile(profile["id"])
    assert clone["id"] != profile["id"]
    assert clone["proxy_id"] == proxy["id"]
    assert clone["identity"] is None
    assert clone["last_launch_at"] is None
    assert not store.profile_dir(clone["id"]).exists()
    store.archive_profile(profile["id"])
    assert [p["id"] for p in store.list_profiles()] == [clone["id"]]
    assert [p["id"] for p in store.list_profiles(archived=True)] == [profile["id"]]
    store.archive_profile(profile["id"], False)
    assert len(store.list_profiles()) == 2


def test_identity_invalidated_for_os_or_locale_changes(store):
    profile = store.save_profile({"name": "One", "os": "windows"})
    store.set_identity(profile["id"], {"seed": 123})
    assert store.save_profile({"notes": "edited"}, profile["id"])["identity"] == {"seed": 123}
    assert store.save_profile({"os": "linux"}, profile["id"])["identity"] is None
    store.set_identity(profile["id"], {"seed": 456})
    assert store.save_profile({"locale": "zh-CN"}, profile["id"])["identity"] is None


@pytest.fixture(params=[False, True])
def legacy_profile(store, request):
    profile = store.save_profile({
        "name": "Existing workspace", "os": "windows", "archived": request.param,
    })
    profile = store.set_identity(profile["id"], {"seed": 123})
    profile.pop("fingerprint", None)
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE profiles SET data = ? WHERE id = ?", (json.dumps(profile), profile["id"])
        )
    directory = store.profile_dir(profile["id"])
    directory.mkdir(parents=True)
    (directory / "cookies.sqlite").write_bytes(b"existing-session-fixture")
    return profile


def test_legacy_profile_reads_supply_fingerprint_without_changing_identity_or_data(store, legacy_profile):
    loaded = store.get_profile(legacy_profile["id"])
    listed = store.list_profiles(archived=legacy_profile["archived"])[0]
    assert loaded["fingerprint"] == normalize_options({})
    assert listed["fingerprint"] == loaded["fingerprint"]
    assert loaded["identity"] == listed["identity"] == {"seed": 123}
    assert (store.profile_dir(legacy_profile["id"]) / "cookies.sqlite").read_bytes() == b"existing-session-fixture"
    with sqlite3.connect(store.database_path) as connection:
        row = connection.execute("SELECT data FROM profiles WHERE id = ?", (legacy_profile["id"],)).fetchone()
        assert json.loads(row[0]) == legacy_profile
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_legacy_profile_clone_and_export_supply_fingerprint_defaults(store, legacy_profile):
    payload = store.export_config()
    assert payload["profiles"][0]["fingerprint"] == normalize_options({})
    clone = store.clone_profile(legacy_profile["id"])
    assert clone["fingerprint"] == payload["profiles"][0]["fingerprint"]
    assert clone["identity"] is None
    assert not store.profile_dir(clone["id"]).exists()
    assert store.get_profile(legacy_profile["id"])["identity"] == {"seed": 123}


def test_legacy_profile_edit_backfills_defaults_without_resetting_identity(store, legacy_profile):
    profile = store.save_profile({"notes": "Metadata edit"}, legacy_profile["id"])
    assert profile["fingerprint"] == normalize_options({})
    assert profile["identity"] == {"seed": 123}
    same = store.save_profile({"fingerprint": {}}, profile["id"])
    assert same["identity"] == {"seed": 123}
    with sqlite3.connect(store.database_path) as connection:
        data = connection.execute("SELECT data FROM profiles WHERE id = ?", (profile["id"],)).fetchone()[0]
        assert json.loads(data)["fingerprint"] == normalize_options({})
    assert (store.profile_dir(profile["id"]) / "cookies.sqlite").read_bytes() == b"existing-session-fixture"


def test_legacy_v1_import_uses_current_defaults(store, legacy_profile, tmp_path):
    payload = store.export_config()
    del payload["profiles"][0]["fingerprint"]
    target = ProfileStore(tmp_path / "imported", secret_store=MemorySecrets())
    assert target.import_config(payload) == {"profiles": 1, "proxies": 0}
    imported = target.list_profiles(archived=legacy_profile["archived"])[0]
    assert imported["fingerprint"] == normalize_options({})
    assert imported["identity"] is None
    assert store.get_profile(legacy_profile["id"])["identity"] == {"seed": 123}


def test_fingerprint_options_survive_restart_clone_and_config_roundtrip(store, tmp_path):
    options = {"screen_mode": "identity", "hardware_concurrency": 8,
               "do_not_track": True, "webrtc_mode": "disabled"}
    profile = store.save_profile({"name": "Custom identity", "fingerprint": options})
    expected = normalize_options(options)
    assert profile["fingerprint"] == expected
    store.set_identity(profile["id"], {"seed": 456})
    restarted = ProfileStore(store.root, secret_store=store.secret_store)
    assert restarted.get_profile(profile["id"])["fingerprint"] == expected
    assert restarted.get_profile(profile["id"])["identity"] == {"seed": 456}
    payload = restarted.export_config()
    assert payload["profiles"][0]["fingerprint"] == expected
    clone = restarted.clone_profile(profile["id"])
    assert clone["fingerprint"] == expected
    assert clone["identity"] is None
    target = ProfileStore(tmp_path / "imported", secret_store=MemorySecrets())
    target.import_config(payload)
    assert target.list_profiles()[0]["fingerprint"] == expected


def test_only_actual_normalized_fingerprint_changes_reset_identity(store):
    profile = store.save_profile({"name": "One", "fingerprint": {"screen_mode": "identity"}})
    store.set_identity(profile["id"], {"seed": 123})
    for options in ({}, {"webrtc_mode": "disabled"}, normalize_options({})):
        assert store.save_profile({"fingerprint": options}, profile["id"])["identity"] == {"seed": 123}
    changed = store.save_profile({"fingerprint": {"hardware_concurrency": 8}}, profile["id"])
    assert changed["identity"] is None
    store.set_identity(profile["id"], {"seed": 456})
    equivalent = {"hardware_concurrency": 8, "webgl_vendor": "  ", "webgl_renderer": " "}
    assert store.save_profile({"fingerprint": equivalent}, profile["id"])["identity"] == {"seed": 456}
    assert store.save_profile({"fingerprint": {}}, profile["id"])["identity"] is None


def test_sparse_stored_fingerprint_matches_equivalent_normalized_edit(store):
    profile = store.save_profile({"name": "Sparse", "fingerprint": {"hardware_concurrency": 8}})
    profile = store.set_identity(profile["id"], {"seed": 123})
    profile["fingerprint"] = {"hardware_concurrency": 8}
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE profiles SET data = ? WHERE id = ?", (json.dumps(profile), profile["id"])
        )
    expected = normalize_options(profile["fingerprint"])
    assert store.get_profile(profile["id"])["fingerprint"] == expected
    assert store.list_profiles()[0]["fingerprint"] == expected
    assert store.export_config()["profiles"][0]["fingerprint"] == expected
    updated = store.save_profile({"fingerprint": expected}, profile["id"])
    assert updated["identity"] == {"seed": 123}


@pytest.fixture
def browser_identity():
    return {
        "browser_version": "test-version",
        "config": {
            "navigator.userAgent": "test-agent",
            "screen.width": 1920,
            "screen.height": 1080,
            "fonts": ["Test Font"],
            "audio:seed": 123,
            "webGl:vendor": "Test Vendor",
            "webGl:parameters": {"limits": {"height": 2, "width": 3}},
        },
        "options": {"args": []},
    }


def test_claim_identity_rejects_same_effective_config_despite_order_and_environment(store, browser_identity):
    first = store.save_profile({"name": "First"})
    second = store.save_profile({"name": "Second"})
    assert store.claim_identity(first["id"], browser_identity) is True
    equivalent = {
        "id": str(uuid4()), "browser_version": "other-version",
        "options": {"args": ["different-host-option"], "env": {"PATH": "other-host"}},
        "config": {
            "addons": ["/another/host/addon"], "humanize": False, "timezone": "Etc/UTC",
            "webrtc:ipv4": "203.0.113.8", "geolocation:latitude": 1,
            **dict(reversed(list(browser_identity["config"].items()))),
        },
    }
    equivalent["config"]["webGl:parameters"] = {"limits": {"width": 3, "height": 2}}
    before = store.get_profile(second["id"])
    events = store.list_events()
    assert store.claim_identity(second["id"], equivalent) is False
    assert store.get_profile(second["id"]) == before
    assert store.list_events() == events
    with sqlite3.connect(store.database_path) as connection:
        rows = connection.execute("SELECT digest, created_at FROM identity_history").fetchall()
    assert len(rows) == 1
    assert len(rows[0][0]) == 64 and int(rows[0][0], 16) >= 0
    assert rows[0][1]


def test_claim_identity_reserves_changed_effective_config(store, browser_identity):
    first = store.save_profile({"name": "First"})
    second = store.save_profile({"name": "Second"})
    assert store.claim_identity(first["id"], browser_identity)
    changed = {**browser_identity, "config": {**browser_identity["config"], "audio:seed": 456}}
    assert store.claim_identity(second["id"], changed)
    assert store.get_profile(first["id"])["identity"] == browser_identity
    assert store.get_profile(second["id"])["identity"] == changed


def test_claim_identity_rolls_back_digest_profile_and_event_together(store, browser_identity, monkeypatch):
    profile = store.save_profile({"name": "One"})
    before = store.get_profile(profile["id"])
    events = store.list_events()
    original_event = store._event

    def fail_event(*args, **kwargs):
        raise RuntimeError("Audit write failed")

    monkeypatch.setattr(store, "_event", fail_event)
    with pytest.raises(RuntimeError, match="Audit write"):
        store.claim_identity(profile["id"], browser_identity)
    assert store.get_profile(profile["id"]) == before
    assert store.list_events() == events
    monkeypatch.setattr(store, "_event", original_event)
    assert store.claim_identity(profile["id"], browser_identity) is True


def test_identity_history_is_retained_after_reset_clone_delete_and_restart(store, browser_identity):
    profile = store.save_profile({"name": "Original"})
    assert store.claim_identity(profile["id"], browser_identity)
    clone = store.clone_profile(profile["id"])
    assert clone["identity"] is None
    assert store.claim_identity(clone["id"], browser_identity) is False
    store.set_identity(profile["id"], None)
    assert store.claim_identity(profile["id"], browser_identity) is False
    store.delete_profile(profile["id"])
    restarted = ProfileStore(store.root, secret_store=store.secret_store)
    assert restarted.claim_identity(clone["id"], browser_identity) is False
    assert restarted.get_profile(clone["id"])["identity"] is None


def test_existing_workspace_identities_are_backfilled_without_rewriting_profiles(store, browser_identity):
    first = store.save_profile({"name": "Existing"})
    store.set_identity(first["id"], browser_identity)
    before = store.get_profile(first["id"])
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TABLE IF EXISTS identity_history")
    restarted = ProfileStore(store.root, secret_store=store.secret_store)
    assert restarted.get_profile(first["id"]) == before
    new = restarted.save_profile({"name": "New"})
    assert restarted.claim_identity(new["id"], browser_identity) is False
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM identity_history").fetchone()[0] == 1


def test_manual_identity_assignment_registers_digest_without_refusing_restore(store, browser_identity):
    first = store.save_profile({"name": "First"})
    second = store.save_profile({"name": "Restored"})
    third = store.save_profile({"name": "Fresh"})
    store.set_identity(first["id"], browser_identity)
    store.set_identity(second["id"], browser_identity)
    assert store.get_profile(second["id"])["identity"] == browser_identity
    assert store.claim_identity(third["id"], browser_identity) is False


def test_claim_for_missing_profile_does_not_consume_identity(store, browser_identity):
    with pytest.raises(KeyError):
        store.claim_identity(str(uuid4()), browser_identity)
    profile = store.save_profile({"name": "Valid"})
    assert store.claim_identity(profile["id"], browser_identity) is True


def test_concurrent_stores_can_only_claim_a_digest_once(store, browser_identity):
    other = ProfileStore(store.root, secret_store=store.secret_store)
    first = store.save_profile({"name": "First"})
    second = store.save_profile({"name": "Second"})
    barrier = threading.Barrier(2)

    def claim(current, profile):
        barrier.wait(timeout=10)
        return current.claim_identity(profile["id"], browser_identity)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(claim, current, profile)
                   for current, profile in ((store, first), (other, second))]
        assert sorted(future.result(timeout=20) for future in futures) == [False, True]
    assert sum(profile["identity"] is not None for profile in store.list_profiles()) == 1


def test_interrupted_history_backfill_rolls_back_table_and_retries(store, browser_identity, monkeypatch):
    profile = store.save_profile({"name": "Existing"})
    store.set_identity(profile["id"], browser_identity)
    before = store.get_profile(profile["id"])
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("DROP TABLE identity_history")
    remember = ProfileStore._remember_identity

    def fail_after_insert(connection, digest):
        remember(connection, digest)
        raise RuntimeError("Migration interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(ProfileStore, "_remember_identity", staticmethod(fail_after_insert))
        with pytest.raises(RuntimeError, match="Migration interrupted"):
            ProfileStore(store.root, secret_store=store.secret_store)
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'identity_history'"
        ).fetchone() is None
    restarted = ProfileStore(store.root, secret_store=store.secret_store)
    assert restarted.get_profile(profile["id"]) == before
    new = restarted.save_profile({"name": "New"})
    assert restarted.claim_identity(new["id"], browser_identity) is False


@pytest.mark.parametrize("options", [
    None, [], "native", True, {"unsupported": True}, {"screen_mode": "unsupported"},
    {"hardware_concurrency": True}, {"do_not_track": "false"},
])
def test_invalid_fingerprint_options_are_rejected_atomically(store, options):
    with pytest.raises(ValidationError):
        store.save_profile({"name": "Invalid", "fingerprint": options})
    assert not store.list_profiles()
    assert not store.list_events()


def test_import_with_invalid_fingerprint_does_not_create_any_records(store):
    proxy = store.save_proxy({"name": "Office", "server": "localhost:8080"})
    store.save_profile({"name": "One", "proxy_id": proxy["id"]})
    before = store.export_config()
    payload = json.loads(json.dumps(before))
    payload["profiles"][0]["fingerprint"] = {"unsupported": True}
    with pytest.raises(ValidationError):
        store.import_config(payload)
    assert store.export_config() == before


def test_delete_metadata_preserves_browser_data(store):
    profile = store.save_profile({"name": "One"})
    directory = store.profile_dir(profile["id"])
    directory.mkdir(parents=True)
    session = directory / "session.json"
    session.write_text("{}")
    store.delete_profile(profile["id"])
    assert session.exists()
    with pytest.raises(KeyError):
        store.get_profile(profile["id"])


def test_delete_profile_can_remove_only_its_browser_data(store):
    proxy = store.save_proxy({'name': 'Shared', 'server': 'localhost:8080', 'password': 'fixture-only'})
    first = store.save_profile({'name': 'Delete', 'proxy_id': proxy['id']})
    second = store.save_profile({'name': 'Keep', 'proxy_id': proxy['id']})
    for profile in (first, second):
        directory = store.profile_dir(profile['id'])
        directory.mkdir(parents=True)
        (directory / 'cookies.sqlite').write_bytes(b'fixture')
    store.archive_profile(first['id'])
    store.delete_profile(first['id'], remove_data=True)
    assert not store.profile_dir(first['id']).exists()
    assert (store.profile_dir(second['id']) / 'cookies.sqlite').read_bytes() == b'fixture'
    assert store.get_proxy(proxy['id'], True)['password'] == 'fixture-only'
    assert store.get_profile(second['id'])['proxy_id'] == proxy['id']
    assert not store.list_profiles(archived=True)
    assert store.list_events(limit=1)[0]['action'] == 'profile.deleted'


def test_delete_profile_handles_unstarted_profile(store):
    profile = store.save_profile({'name': 'Unstarted'})
    store.delete_profile(profile['id'], remove_data=True)
    assert not store.list_profiles()


def test_failed_browser_data_removal_preserves_profile_record(store, monkeypatch):
    import shutil
    from camoufox.studio.store import StoreError

    profile = store.save_profile({'name': 'Keep on failure'})
    directory = store.profile_dir(profile['id'])
    directory.mkdir(parents=True)

    def locked(path):
        raise PermissionError('fixture path with private data')

    monkeypatch.setattr(shutil, 'rmtree', locked)
    with pytest.raises(StoreError) as error:
        store.delete_profile(profile['id'], remove_data=True)
    assert 'private data' not in str(error.value)
    assert store.get_profile(profile['id'])['name'] == profile['name']
    assert directory.is_dir()


@pytest.mark.parametrize('link_location', ['profile', 'parent', 'child'])
def test_delete_never_follows_directory_links(store, link_location):
    profile = store.save_profile({'name': 'Linked'})
    directory = store.profile_dir(profile['id'])
    outside = store.root / 'unrelated'
    outside.mkdir()
    (outside / 'keep.txt').write_text('Keep this data')
    link = {'profile': directory, 'parent': directory.parent, 'child': directory / 'linked'}[link_location]
    link.parent.mkdir(parents=True, exist_ok=True)
    assert link.is_relative_to(store.root) and outside.is_relative_to(store.root)
    if sys.platform == 'win32':
        subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "$ErrorActionPreference = 'Stop'; New-Item -ItemType Junction "
             '-Path $env:CAMOUFOX_TEST_LINK -Target $env:CAMOUFOX_TEST_TARGET | Out-Null'],
            env={**os.environ, 'CAMOUFOX_TEST_LINK': str(link), 'CAMOUFOX_TEST_TARGET': str(outside)},
            capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    if link_location == 'child':
        store.delete_profile(profile['id'], remove_data=True)
        assert not directory.exists()
    else:
        with pytest.raises(ValidationError):
            store.delete_profile(profile['id'], remove_data=True)
        assert store.get_profile(profile['id'])['name'] == 'Linked'
    assert (outside / 'keep.txt').read_text() == 'Keep this data'


def test_password_is_redacted_preserved_and_explicitly_clearable(store):
    password = "fixture-" + uuid4().hex
    proxy = store.save_proxy({
        "name": "Private", "server": "HTTP://EXAMPLE.COM:3128/",
        "username": "alice", "password": password,
    })
    assert proxy["server"] == "http://example.com:3128"
    assert proxy["has_password"] is True
    assert "password" not in proxy
    assert store.get_proxy(proxy["id"], reveal_password=True)["password"] == password
    store.save_proxy({"name": "Renamed", "password": ""}, proxy["id"])
    store.save_proxy({"username": "bob"}, proxy["id"])
    assert store.get_proxy(proxy["id"], reveal_password=True)["password"] == password
    for path in store.root.glob("studio.sqlite3*"):
        assert password.encode() not in path.read_bytes()
    assert password not in json.dumps(store.export_config())
    assert password not in json.dumps(store.list_events())
    assert password not in json.dumps(store.list_proxies())
    store.save_proxy({"clear_password": True}, proxy["id"])
    assert store.get_proxy(proxy["id"], reveal_password=True)["password"] == ""
    assert not store.secret_store.values


def test_missing_credential_fails_instead_of_connecting_unauthenticated(store):
    proxy = store.save_proxy({"name": "Private", "server": "localhost:80", "password": "fixture"})
    store.secret_store.values.clear()
    with pytest.raises(SecretStoreError):
        store.get_proxy(proxy["id"], reveal_password=True)


@pytest.mark.parametrize("changes", [{"password": "replacement-fixture"}, {"clear_password": True}])
def test_missing_credential_can_be_replaced_or_explicitly_cleared(store, changes):
    proxy = store.save_proxy({"name": "Private", "server": "localhost:80", "password": "fixture"})
    store.secret_store.values.clear()
    store.save_proxy(changes, proxy["id"])
    assert store.get_proxy(proxy["id"], reveal_password=True)["password"] == changes.get("password", "")


def test_bound_proxy_cannot_be_deleted(store):
    proxy = store.save_proxy({"name": "Office", "server": "localhost:8080"})
    profile = store.save_profile({"name": "One", "proxy_id": proxy["id"]})
    store.archive_profile(profile["id"])
    with pytest.raises(ValidationError):
        store.delete_proxy(proxy["id"])
    store.save_profile({"proxy_id": None}, profile["id"])
    store.delete_proxy(proxy["id"])
    assert not store.list_proxies()


@pytest.mark.parametrize("line, expected", [
    ("localhost:8080", ("http://localhost:8080", "", "")),
    ("example.com:80:alice:has:colons", ("http://example.com:80", "alice", "has:colons")),
    ("socks5://a%40b:p%3A%2F%40@example.com:1080", ("socks5://example.com:1080", "a@b", "p:/@")),
    ("https://[::1]:443", ("https://[::1]:443", "", "")),
])
def test_proxy_import_formats(line, expected):
    parsed = parse_proxy_line(line)
    assert (parsed["server"], parsed["username"], parsed["password"]) == expected


def test_proxy_import_preserves_password_whitespace(store):
    proxy = store.import_proxies("localhost:8080:user: fixture ")[0]
    assert store.get_proxy(proxy["id"], reveal_password=True)["password"] == " fixture "


def test_import_proxies_validates_entire_batch_before_writing(store):
    with pytest.raises(ValidationError):
        store.import_proxies("localhost:8080:user:fixture\ninvalid")
    assert not store.list_proxies()
    assert not store.secret_store.values
    imported = store.import_proxies("localhost:8080\nhttps://example.com:443")
    assert len(imported) == 2


def test_legacy_chain_cannot_run_as_an_ordinary_proxy(store):
    proxy = store.save_proxy({"name": "Legacy route", "server": "socks5://proxy.example:1080"})
    proxy["node"] = {"core": "legacy", "proxies": [{"type": "socks5"}]}
    with store._connect() as connection:
        connection.execute("UPDATE proxies SET data = ? WHERE id = ?", (json.dumps(proxy), proxy["id"]))
        connection.commit()
    assert store.get_proxy(proxy["id"])["server"] == "socks5://proxy.example:1080"
    with pytest.raises(ValidationError, match="unsupported node or chain"):
        store.get_proxy(proxy["id"], reveal_password=True)
    with pytest.raises(ValidationError, match="chains are unsupported"):
        store.import_config(store.export_config())


def test_secret_failure_rolls_back_batch_and_does_not_expose_backend_error(store):
    original_set = store.secret_store.set

    def fail_second(key, value):
        if value == "second-fixture":
            raise RuntimeError("second-fixture")
        original_set(key, value)

    store.secret_store.set = fail_second
    with pytest.raises(SecretStoreError) as error:
        store.import_proxies("localhost:80:a:first-fixture\nlocalhost:81:b:second-fixture")
    assert "second-fixture" not in str(error.value)
    assert not store.list_proxies()
    assert not store.secret_store.values


def test_database_failure_restores_previous_password(store, monkeypatch):
    proxy = store.save_proxy({"name": "Office", "server": "localhost:80", "password": "old-fixture"})

    def fail_event(*args, **kwargs):
        raise RuntimeError("Database write failed")

    monkeypatch.setattr(store, "_event", fail_event)
    with pytest.raises(RuntimeError, match="Database write"):
        store.save_proxy({"password": "new-fixture", "name": "Changed"}, proxy["id"])
    assert store.get_proxy(proxy["id"])["name"] == "Office"
    assert store.get_proxy(proxy["id"], reveal_password=True)["password"] == "old-fixture"


def test_unsafe_keyring_is_rejected_lazily(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: MemorySecrets()))
    credentials = KeyringSecretStore("test")
    with pytest.raises(SecretStoreError, match="native"):
        credentials.set("id", "fixture")


def test_native_keyring_exceptions_do_not_expose_credentials(monkeypatch):
    class WinVaultKeyring:
        priority = 5

        def set_password(self, service, username, password):
            raise RuntimeError(password)

    WinVaultKeyring.__module__ = "keyring.backends.Windows"
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: WinVaultKeyring()))
    credentials = KeyringSecretStore("test")
    with pytest.raises(SecretStoreError) as error:
        credentials.set("id", "private-fixture")
    assert "private-fixture" not in str(error.value)
    assert error.value.__suppress_context__


def test_config_import_remaps_ids_and_excludes_session_identity_and_passwords(store, tmp_path):
    proxy = store.save_proxy({"name": "Office", "server": "localhost:80", "password": "fixture"})
    profile = store.save_profile({"name": "One", "proxy_id": proxy["id"]})
    store.set_identity(profile["id"], {"seed": 321})
    payload = store.export_config()
    assert "identity" not in payload["profiles"][0]
    assert "password" not in payload["proxies"][0]
    target = ProfileStore(tmp_path / "imported", secret_store=MemorySecrets())
    assert target.import_config(payload) == {"profiles": 1, "proxies": 1}
    imported = target.list_profiles()[0]
    assert imported["id"] != profile["id"]
    assert imported["proxy_id"] != proxy["id"]
    assert imported["identity"] is None
    assert not target.get_proxy(imported["proxy_id"])["has_password"]


@pytest.mark.parametrize("defect", ["reference", "duplicate", "unknown", "password"])
def test_config_import_rolls_back_invalid_input(store, defect):
    proxy = store.save_proxy({"name": "Office", "server": "localhost:80"})
    store.save_profile({"name": "One", "proxy_id": proxy["id"]})
    payload = store.export_config()
    if defect == "reference":
        payload["profiles"][0]["proxy_id"] = str(uuid4())
    elif defect == "duplicate":
        payload["profiles"].append(dict(payload["profiles"][0]))
    elif defect == "unknown":
        payload["profiles"][0]["unrecognized"] = True
    else:
        payload["proxies"][0]["password"] = "fixture"
    before = store.export_config()
    with pytest.raises(ValidationError):
        store.import_config(payload)
    assert store.export_config() == before


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "x" * 129}, {"headless": "false"}, {"tags": "tag"},
    {"tags": ["ok", 1]}, {"os": "android"}, {"locale": "invalid_locale"},
    {"timezone": "Missing/Zone"}, {"start_urls": ["file:///etc/passwd"]},
    {"start_urls": ["javascript:alert(1)"]}, {"start_urls": ["https://u:p@example.com"]},
    {"start_urls": ["https://example.com:99999"]}, {"proxy_id": "../../outside"},
    {"proxy_id": str(uuid4())}, {"identity": {}}, {"unknown": True},
])
def test_invalid_profiles_are_rejected_without_changes(store, changes):
    with pytest.raises(ValidationError):
        store.save_profile({"name": "One", **changes})
    assert not store.list_profiles()


@pytest.mark.parametrize("server", [
    "ftp://example.com:80", "http://example.com", "http://user:pass@example.com:80",
    "http://example.com:0", "http://example.com:65536", "http://example.com:80/path",
    "http://example.com:80?query", "http://bad host:80", "http://example.com:80\\path",
    "http://example.com:80?", "http://example.com:80#",
])
def test_invalid_proxy_servers_are_rejected(store, server):
    with pytest.raises(ValidationError):
        store.save_proxy({"name": "One", "server": server})
    assert not store.list_proxies()


@pytest.mark.parametrize("identifier", ["../escape", "", "a" * 36, str(uuid4()).upper()])
def test_profile_directory_requires_canonical_uuid(store, identifier):
    with pytest.raises(ValidationError):
        store.profile_dir(identifier)


def test_empty_update_identifier_cannot_create_a_record(store):
    with pytest.raises(ValidationError):
        store.save_profile({"name": "One"}, "")
    with pytest.raises(ValidationError):
        store.save_proxy({"name": "One", "server": "localhost:80"}, "")
    assert not store.list_profiles()
    assert not store.list_proxies()


def test_settings_and_event_codes(store):
    assert store.get_settings() == {"max_running": 5, "executable_path": "",
                                    "automation_enabled": False, "team_automation_enabled": False}
    store.save_settings({"max_running": 8})
    assert store.get_settings()["max_running"] == 8
    for values in ({"max_running": 0}, {"max_running": True}, {"unexpected": 1},
                   {"automation_enabled": 1}, {"team_automation_enabled": 1}):
        with pytest.raises(ValidationError):
            store.save_settings(values)
    store.record_event("browser.failed", detail="proxy_unreachable")
    assert store.list_events(limit=1)[0]["detail"] == "proxy_unreachable"
    with pytest.raises(ValidationError):
        store.record_event("browser.failed", detail="Error connecting to http://user:secret@proxy")
