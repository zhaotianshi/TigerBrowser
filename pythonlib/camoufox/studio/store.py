"""Validated local configuration, browser identities and credential references."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from camoufox.exceptions import InvalidLocale
from camoufox.locales import verify_locale

from .fingerprint_options import NEW_PROFILE_DEFAULTS, SCREEN_OPTIONS, normalize_options
from .secrets import KeyringSecretStore, SecretStore, SecretStoreError

SCHEMA_VERSION = 1
TEAM_SCHEMA_VERSION = 6
# A fixed template parameters the scrypt N/r/p costs; the random salt lives
# with each member, not in the template. RFC 7914: the template is not a secret.
TEAM_PASSWORD_TEMPLATE = "scrypt$16384$8$1"
# Returns False while no administrator exists, so initialization stays local.
TEAM_SETUP_CODE_PATH = "team_setup.code"
TEAM_LOGIN_FAILURE_LIMIT = 5
TEAM_LOGIN_FAILURE_WINDOW_SECONDS = 300
TEAM_LOGIN_LOCK_SECONDS = 900
MEMBER_ROLES = frozenset({"admin", "operator", "viewer"})
# "checkout" hands site data and proxy credentials to a member's computer, beyond "operate".
GRANT_PERMISSIONS = frozenset({"view", "watch", "operate", "manage", "checkout"})
ROLE_PERMISSIONS = {
    "admin": GRANT_PERMISSIONS,
    "operator": frozenset({"view", "watch", "operate", "checkout"}),
    "viewer": frozenset({"view", "watch"}),
}
GROUP_PERMISSIONS = GRANT_PERMISSIONS - {'manage'}
_EXPORT_KINDS = frozenset({"proxies", "profiles", "members", "sessions", "grants", "audit"})
EXPORT_SCHEMA = "camoufox-studio"
PROFILE_FIELDS = frozenset({
    "name", "group_name", "tags", "notes", "os", "locale", "timezone", "proxy_id",
    "start_urls", "headless", "humanize", "block_images", "archived", "fingerprint",
    "kernel",
})
_PROXY_SECRETS = {"password": "", "short_id": ":short_id", "public_key": ":public_key",
                  "node_secret": ":node_secret"}
_PROXY_CONFIG_FIELDS = frozenset({"name", "server", "username", "node"})
PROXY_FIELDS = frozenset({"name", "server", "username", "password", "clear_password"})
_SETTINGS_DEFAULTS = {"max_running": 5, "executable_path": "",
                      "automation_enabled": False, "team_automation_enabled": False}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CODE = re.compile(r"[a-z0-9][a-z0-9_.:-]*")
_IDENTITY_PREFIXES = (
    "navigator.", "headers.", "screen.", "window.", "fonts", "webGl:", "webGl2:",
    "audio:", "AudioContext:", "voices", "mediaDevices:", "locale:",
)


class ValidationError(ValueError):
    """Configuration is malformed or violates a storage invariant."""


class StoreError(RuntimeError):
    """The local database cannot safely complete an operation."""


def _hash_password(password: str, salt: bytes, template: str) -> str:
    _, n, r, p = template.split("$")
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p))
    return hashlib.sha256(derived).hexdigest()


def _member_data(encoded: str) -> dict:
    return json.loads(encoded)


def _session_data(encoded: str) -> dict:
    return json.loads(encoded)


def _grant_data(encoded: str) -> dict:
    return json.loads(encoded)


def _audit_data(row) -> dict:
    return {
        "id": row["id"], "created_at": row["created_at"], "action": row["action"],
        "actor_id": row["actor_id"], "actor_name": row["actor_name"],
        "target_kind": row["target_kind"], "target_id": row["target_id"],
        "result": row["result"], "detail": row["detail"],
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _now_after(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def _now_before(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def validate_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise ValidationError("An identifier must be a canonical lowercase UUID.") from None
    return value


def _text(value: Any, field: str, limit: int, *, required=False, multiline=False) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValidationError(f"{field} must be text with at most {limit} characters.")
    checked = value.replace("\r", "").replace("\n", "").replace("\t", "") if multiline else value
    if _CONTROL.search(checked) or (required and not value.strip()):
        raise ValidationError(f"{field} contains invalid or empty text.")
    return value


def _fields(data: Any, allowed: frozenset) -> dict:
    if not isinstance(data, dict) or any(key not in allowed for key in data):
        raise ValidationError("Configuration contains unknown fields or is not an object.")
    return data


def _hostname(host: Optional[str]) -> str:
    """Normalize the host shared by ordinary proxy endpoints and node links."""
    if not host:
        raise ValidationError("A server hostname is required.")
    try:
        address = ipaddress.ip_address(host)
        if "%" in host:
            raise ValueError("Scoped IP addresses are not supported.")
        return address.compressed
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValidationError("The server hostname is invalid.") from None
    if len(host) > 253 or not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in host.rstrip(".").split(".")
    ):
        raise ValidationError("The server hostname is invalid.")
    return host


def _split_url(value: str):
    if any(char.isspace() for char in value) or "\\" in value or _CONTROL.search(value):
        raise ValidationError("URLs cannot contain whitespace, control characters or backslashes.")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        _hostname(parsed.hostname)
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        return parsed
    except ValueError:
        raise ValidationError("The URL host or port is invalid.") from None


def validate_start_url(value: str) -> str:
    _text(value, "start URL", 4096, required=True)
    if value == "about:blank":
        return value
    parsed = _split_url(value)
    if parsed.scheme not in {"http", "https"} or parsed.username is not None:
        raise ValidationError("Start URLs must use HTTP or HTTPS without embedded credentials.")
    return value


def normalize_proxy_server(value: str) -> str:
    _text(value, "proxy server", 2048, required=True)
    parsed = _split_url(value if "://" in value else f"http://{value}")
    if (
        parsed.scheme not in {"http", "https", "socks5"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.path not in {"", "/"}
        or "?" in value
        or "#" in value
    ):
        raise ValidationError("A proxy requires a supported protocol, a host and an explicit port.")
    host = _hostname(parsed.hostname)
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}:{parsed.port}"


def parse_proxy_line(line: str) -> dict:
    """Parse a URL, host:port or host:port:username:password without echoing secrets."""
    _text(line, "proxy import line", 8192, required=True)
    if "://" not in line and not line.startswith("[") and line.count(":") >= 3:
        host, port, username, password = line.split(":", 3)
        server = normalize_proxy_server(f"{host}:{port}")
    else:
        parsed = _split_url(line if "://" in line else f"http://{line}")
        if re.search(r"%(?![0-9a-fA-F]{2})", parsed.netloc):
            raise ValidationError("Proxy credentials contain invalid percent encoding.")
        try:
            username = unquote(parsed.username or "", errors="strict")
            password = unquote(parsed.password or "", errors="strict")
        except UnicodeError:
            raise ValidationError("Proxy credentials must be valid UTF-8.") from None
        authority = parsed.netloc.rsplit("@", 1)[-1]
        server = normalize_proxy_server(f"{parsed.scheme}://{authority}{parsed.path}")
        if "?" in line or "#" in line:
            raise ValidationError("Proxy URLs cannot contain a query or fragment.")
    _text(username, "proxy username", 512)
    _text(password, "proxy password", 4096)
    return {"name": server, "server": server, "username": username, "password": password}


def _normalize_fingerprint(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValidationError("Fingerprint options must be an object.")
    try:
        return normalize_options(value)
    except ValueError as error:
        raise ValidationError(str(error)) from None


def validate_member_name(value: str) -> str:
    """Login name matching between exact byte sequences only; case never folds."""
    _text(value, "member name", 64, required=True)
    if not all(char.isalnum() or char in "._-" for char in value):
        raise ValidationError("A member name accepts letters, digits and ._- only.")
    return value


def validate_member_display(value: str) -> str:
    return _text(value, "member display name", 128, required=True)


def validate_member_password(value: str) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 1024:
        raise ValidationError("A member password must be text between 8 and 1024 characters.")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValidationError("A member password cannot contain control characters.")
    return value


def validate_permission_codes(value: Any, *, empty=False) -> list:
    if not isinstance(value, list) or len(value) > len(GRANT_PERMISSIONS):
        raise ValidationError("Permissions must be a list of distinct permission codes.")
    if not all(isinstance(item, str) and item in GRANT_PERMISSIONS for item in value):
        raise ValidationError("Unknown permission code; expected view, watch, operate, manage or checkout.")
    if len(set(value)) != len(value) or (not value and not empty):
        raise ValidationError("Permissions must be distinct and non-empty.")
    return value


def _profile_data(encoded: str) -> dict:
    profile = json.loads(encoded)
    # Legacy workspaces predate editable options; reading must preserve their identity.
    profile["fingerprint"] = _normalize_fingerprint(profile.get("fingerprint", {}))
    profile.setdefault("revision", 1)
    profile.setdefault('kernel', '')
    profile.setdefault('data_revision', 0)
    return profile


def _proxy_data(encoded: str) -> dict:
    proxy = json.loads(encoded)
    proxy.setdefault("node", {})
    for field in _PROXY_SECRETS:
        proxy.setdefault(f"has_{field}", False)
    return proxy


def _validate_timezone(value, *, required=False):
    _text(value, "timezone", 128, required=required)
    if value:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValidationError("The timezone must be a recognized IANA timezone.") from None


def validate_profile(data: dict) -> dict:
    """Validate a complete editable configuration and supply creation defaults."""
    _fields(data, PROFILE_FIELDS)
    defaults = {
        "name": "", "group_name": "", "tags": [], "notes": "",
        "os": {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux"),
        "locale": "en-US", "timezone": "", "proxy_id": None, "start_urls": [],
        "headless": False, "humanize": True, "block_images": False, "archived": False,
        "fingerprint": {}, "kernel": "",
    }
    values = {**defaults, **data}
    from .kernels import validate_kernel
    try:
        validate_kernel(values['kernel'])
    except ValueError:
        raise ValidationError('invalid_kernel') from None
    values["fingerprint"] = _normalize_fingerprint(values["fingerprint"])
    for field, limit in (("name", 128), ("group_name", 128), ("notes", 10000)):
        _text(values[field], field, limit, required=field == "name", multiline=field == "notes")
    if values["os"] not in ("windows", "macos", "linux"):
        raise ValidationError("Profile OS must be windows, macos or linux.")
    for field in ("headless", "humanize", "block_images", "archived"):
        if type(values[field]) is not bool:
            raise ValidationError(f"{field} must be a boolean.")
    for field, limit in (("tags", 50), ("start_urls", 50)):
        items = values[field]
        if not isinstance(items, list) or len(items) > limit:
            raise ValidationError(f"{field} must contain at most {limit} items.")
        values[field] = list(items)
    for tag in values["tags"]:
        _text(tag, "tag", 64, required=True)
    if len(set(values["tags"])) != len(values["tags"]):
        raise ValidationError("Tags must be unique.")
    for url in values["start_urls"]:
        validate_start_url(url)
    _text(values["locale"], "locale", 64, required=True)
    try:
        verify_locale(values["locale"])
    except InvalidLocale:
        raise ValidationError("The profile locale is not a recognized language tag.") from None
    _validate_timezone(values["timezone"])
    if values["proxy_id"] is not None:
        validate_id(values["proxy_id"])
    return values


def _json(value: Any, *, sort_keys: bool = False) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=sort_keys,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValidationError("The value must contain finite JSON data.") from None


def _identity_digest(identity: dict) -> str:
    if not isinstance(identity, dict):
        raise ValidationError("The browser identity must be a JSON object.")
    if len(_json(identity)) > 1_000_000:
        raise ValidationError("The browser identity is too large.")
    config = identity.get("config", identity)
    if not isinstance(config, dict):
        raise ValidationError("The browser identity configuration must be a JSON object.")
    effective = {
        key: value for key, value in config.items()
        if isinstance(key, str) and key.startswith(_IDENTITY_PREFIXES)
    }
    return hashlib.sha256(_json(effective, sort_keys=True).encode("utf-8")).hexdigest()


def public_identity(identity: Optional[dict]) -> Optional[dict]:
    """Return fingerprint values without persisted launch options or host paths."""
    if identity is None:
        return None
    config = identity.get("config", identity)
    return {
        "browser_version": identity.get("browser_version"),
        "kernel": identity.get("kernel"),
        "config": {
            key: value for key, value in config.items()
            if isinstance(key, str) and key.startswith(_IDENTITY_PREFIXES)
        },
    }


class ProfileStore:
    """SQLite metadata and native credentials, with one connection per operation."""

    def __init__(self, root: Path, secret_store: Optional[SecretStore] = None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path = self.root / "studio.sqlite3"
        self._lock = threading.RLock()
        with self._connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise StoreError("The Studio database version is not supported by this installation.")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS proxies (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, has_password INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    id TEXT PRIMARY KEY, proxy_id TEXT REFERENCES proxies(id),
                    archived INTEGER NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS profiles_proxy ON profiles(proxy_id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    action TEXT NOT NULL, profile_id TEXT, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id = 1), data TEXT);
                CREATE TABLE IF NOT EXISTS workspace_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("BEGIN IMMEDIATE")
            # Persist the legacy namespace so moving a workspace preserves native credentials.
            namespace = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:24]
            connection.execute(
                "INSERT OR IGNORE INTO workspace_metadata (key, value) VALUES ('credential_namespace', ?)",
                (f"camoufox-studio/{namespace}",),
            )
            connection.execute(
                "INSERT OR IGNORE INTO workspace_metadata (key, value) VALUES ('workspace_id', ?)",
                (str(uuid4()),),
            )
            namespace = connection.execute(
                "SELECT value FROM workspace_metadata WHERE key = 'credential_namespace'"
            ).fetchone()[0]
            team_version = connection.execute(
                "SELECT value FROM workspace_metadata WHERE key = 'team_schema_version'"
            ).fetchone()
            previous_team_version = int(team_version[0]) if team_version is not None else 0
            if previous_team_version not in (0, 1, 2, 3, 4, 5, TEAM_SCHEMA_VERSION):
                raise StoreError("The Studio team database version is not supported by this installation.")
            history_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'identity_history'"
            ).fetchone()
            connection.execute(
                "CREATE TABLE IF NOT EXISTS identity_history "
                "(digest TEXT PRIMARY KEY, created_at TEXT NOT NULL)"
            )
            if not history_exists:
                # The table and backfill commit together, so an interrupted upgrade is retried.
                for row in connection.execute("SELECT data FROM profiles"):
                    identity = json.loads(row["data"]).get("identity")
                    if identity is not None:
                        self._remember_identity(connection, _identity_digest(identity))
            statements = """
                    CREATE TABLE IF NOT EXISTS team_members (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                        active INTEGER NOT NULL, permission_version INTEGER NOT NULL,
                        data TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS team_sessions (
                        id TEXT PRIMARY KEY, member_id TEXT NOT NULL, data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS team_sessions_member ON team_sessions(member_id);
                    CREATE TABLE IF NOT EXISTS team_api_keys (
                        id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES team_members(id),
                        token_hash TEXT NOT NULL UNIQUE,
                        session_id TEXT NOT NULL UNIQUE REFERENCES team_sessions(id), data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS team_api_keys_member ON team_api_keys(member_id);
                    CREATE TABLE IF NOT EXISTS team_grants (
                        id TEXT PRIMARY KEY, member_id TEXT NOT NULL,
                        profile_id TEXT NOT NULL, data TEXT NOT NULL,
                        UNIQUE(member_id, profile_id)
                    );
                    CREATE INDEX IF NOT EXISTS team_grants_profile ON team_grants(profile_id);
                    CREATE TABLE IF NOT EXISTS team_groups (
                        id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, data TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS team_group_members (
                        group_id TEXT NOT NULL REFERENCES team_groups(id) ON DELETE CASCADE,
                        member_id TEXT NOT NULL REFERENCES team_members(id) ON DELETE CASCADE,
                        PRIMARY KEY(group_id, member_id)
                    );
                    CREATE INDEX IF NOT EXISTS team_group_members_member ON team_group_members(member_id);
                    CREATE TABLE IF NOT EXISTS team_group_grants (
                        id TEXT PRIMARY KEY,
                        group_id TEXT NOT NULL REFERENCES team_groups(id) ON DELETE CASCADE,
                        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                        data TEXT NOT NULL, UNIQUE(group_id, profile_id)
                    );
                    CREATE INDEX IF NOT EXISTS team_group_grants_profile ON team_group_grants(profile_id);
                    CREATE TABLE IF NOT EXISTS team_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                        action TEXT NOT NULL, actor_id TEXT, actor_name TEXT NOT NULL,
                        target_kind TEXT NOT NULL, target_id TEXT,
                        result TEXT NOT NULL, detail TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS team_login_attempts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        member_name TEXT NOT NULL, failed_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS team_login_attempts_name
                        ON team_login_attempts(member_name);
                    CREATE TABLE IF NOT EXISTS team_login_limits (
                        member_name TEXT PRIMARY KEY,
                        failures INTEGER NOT NULL, locked_until REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS team_http_sessions (
                        token_hash TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES team_sessions(id) ON DELETE CASCADE
                    );
                    CREATE TABLE IF NOT EXISTS team_runs (
                        id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
                        owner TEXT NOT NULL, state TEXT NOT NULL, data TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS team_runs_active ON team_runs(profile_id)
                        WHERE state IN ('starting', 'running', 'stopping', 'recovery_required');
                    CREATE TABLE IF NOT EXISTS team_checkouts (
                        id TEXT PRIMARY KEY,
                        profile_id TEXT NOT NULL REFERENCES profiles(id),
                        member_id TEXT NOT NULL, device_id TEXT NOT NULL,
                        state TEXT NOT NULL, token_hash TEXT NOT NULL, data TEXT NOT NULL
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS team_checkouts_active ON team_checkouts(profile_id)
                        WHERE state IN ('checked_out', 'returning');
                    CREATE TABLE IF NOT EXISTS team_commands (
                        id TEXT PRIMARY KEY, member_id TEXT NOT NULL,
                        request_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                        run_id TEXT NOT NULL REFERENCES team_runs(id) ON DELETE CASCADE,
                        data TEXT NOT NULL, UNIQUE(member_id, request_id)
                    );
            """
            for statement in statements.split(';'):
                if statement.strip():
                    connection.execute(statement)
            if previous_team_version == 1:
                for row in connection.execute("SELECT data FROM team_sessions").fetchall():
                    session = _session_data(row['data'])
                    session['revoked_at'] = session['revoked_at'] or _now()
                    connection.execute(
                        "UPDATE team_sessions SET member_id = ?, data = ? WHERE id = ?",
                        (session['member_id'], _json(session), session['id']),
                    )
                # A persisted monotonic deadline has no reliable epoch after reboot.
                connection.execute(
                    "UPDATE team_login_limits SET locked_until = ? WHERE locked_until > 0",
                    (time.time() + TEAM_LOGIN_LOCK_SECONDS,),
                )
            for row in connection.execute("SELECT id, data FROM profiles").fetchall():
                profile = json.loads(row['data'])
                if 'revision' not in profile:
                    profile['revision'] = 1
                    connection.execute("UPDATE profiles SET data = ? WHERE id = ?",
                                       (_json(profile), row['id']))
            connection.execute(
                "INSERT OR REPLACE INTO workspace_metadata (key, value) "
                "VALUES ('team_schema_version', ?)", (str(TEAM_SCHEMA_VERSION),)
            )
            connection.commit()
        self.secret_store = (
            secret_store if secret_store is not None else KeyringSecretStore(namespace)
        )

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self, *, failure_commits=False):
        """Write transaction; failure_commits keeps reserved rows on expected failures."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            undo = []
            try:
                yield connection, undo
                connection.commit()
            except BaseException:
                if failure_commits:
                    connection.commit()
                    raise
                connection.rollback()
                failed = False
                for key, previous in reversed(undo):
                    try:
                        if previous is None:
                            self._secret("delete", key)
                        else:
                            self._secret("set", key, previous)
                    except SecretStoreError:
                        failed = True
                if failed:
                    raise SecretStoreError(
                        "Credential rollback failed; unlock the system keyring before retrying."
                    ) from None
                raise

    def _secret(self, operation: str, key: str, *args):
        try:
            return getattr(self.secret_store, operation)(key, *args)
        except Exception:
            raise SecretStoreError("The operating-system credential operation failed.") from None

    @staticmethod
    def _get(connection, table: str, identifier: str) -> dict:
        validate_id(identifier)
        row = connection.execute(f"SELECT data FROM {table} WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise KeyError(f"The {table[:-1]} does not exist.")
        return _profile_data(row["data"]) if table == "profiles" else _proxy_data(row["data"])

    @staticmethod
    def _event(connection, action: str, profile_id=None, detail=""):
        connection.execute(
            "INSERT INTO events (created_at, action, profile_id, detail) VALUES (?, ?, ?, ?)",
            (_now(), action, profile_id, detail),
        )

    @staticmethod
    def _remember_identity(connection, digest: str) -> bool:
        cursor = connection.execute(
            "INSERT OR IGNORE INTO identity_history (digest, created_at) VALUES (?, ?)",
            (digest, _now()),
        )
        return cursor.rowcount == 1

    def _audit(
        self, connection, action, actor_id=None, actor_name="",
        target_kind="", target_id=None, result="ok", detail="",
    ):
        for field, value, limit in (("action", action, 64), ("result", result, 32), ("detail", detail, 120)):
            _text(value, f"audit {field}", limit, required=field != "detail")
            if value and not _CODE.fullmatch(value):
                raise ValidationError("Audit fields require fixed lowercase codes without user or exception text.")
        _text(actor_name, "audit actor name", 128)
        if target_kind and target_kind not in ("member", "profile", "grant", "session", "setup", "member_group", "run", "snapshot", "api_key",
                                          "checkout"):
            raise ValidationError("Unknown audit target kind.")
        if actor_id is not None:
            validate_id(actor_id)
        if target_id is not None:
            validate_id(target_id)
        connection.execute(
            "INSERT INTO team_audit (created_at, action, actor_id, actor_name, "
            "target_kind, target_id, result, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), action, actor_id, actor_name, target_kind, target_id, result, detail),
        )

    @staticmethod
    def _team_row(connection, identifier: str, *, kind="member"):
        validate_id(identifier)
        table = {'member': 'team_members', 'group': 'team_groups'}[kind]
        row = connection.execute(
            f"SELECT data FROM {table} WHERE id = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise KeyError(f"The {kind} does not exist.")
        return json.loads(row["data"])

    def _named_member(self, connection, name: str) -> Optional[dict]:
        validate_member_name(name)
        row = connection.execute(
            "SELECT data FROM team_members WHERE name = ?", (name,)
        ).fetchone()
        return _member_data(row["data"]) if row else None

    def _active_member(self, connection, identifier: str) -> dict:
        member = self._team_row(connection, identifier)
        if not member["active"]:
            raise ValidationError("member_disabled")
        return member

    def _admin_member(self, connection, identifier: str) -> dict:
        actor = self._active_member(connection, identifier)
        if actor['role'] != 'admin':
            raise ValidationError('admin_required')
        return actor

    def _create_member(self, connection, name, display, role, password, actor=None):
        validate_member_name(name)
        member = self._named_member(connection, name)
        if member is not None:
            raise ValidationError("member_name_in_use")
        salt = secrets.token_bytes(16)
        timestamp = _now()
        data = {
            "id": str(uuid4()), "name": name, "display": validate_member_display(display),
            "role": role, "password_salt": salt.hex(),
            "password_hash": _hash_password(password, salt, TEAM_PASSWORD_TEMPLATE),
            "password_template": TEAM_PASSWORD_TEMPLATE,
            "active": True, "permission_version": 1,
            "created_at": timestamp, "updated_at": timestamp,
        }
        connection.execute(
            "INSERT INTO team_members (id, name, active, permission_version, data) "
            "VALUES (?, ?, 1, 1, ?)",
            (data["id"], data["name"], _json(data)),
        )
        self._audit(connection, "member.created", target_kind="member", target_id=data["id"],
                    **({"actor_id": actor["id"], "actor_name": actor["name"]} if actor else {}))
        return {key: data[key] for key in ("id", "name", "display", "role", "active",
                                           "permission_version", "created_at", "updated_at")}

    def _touch(self, connection, member: dict, **changes) -> dict:
        member.update(changes, updated_at=_now())
        connection.execute(
            "UPDATE team_members SET active = ?, permission_version = ?, data = ? WHERE id = ?",
            (int(member["active"]), member["permission_version"], _json(member), member["id"]),
        )
        return member

    def _bump_version(self, connection, member: dict) -> dict:
        return self._touch(connection, member, permission_version=member["permission_version"] + 1)

    def _revoke_sessions(self, connection, member_id: str, *, preserve_api_keys=False) -> None:
        now = _now()
        for row in connection.execute(
            "SELECT data FROM team_sessions WHERE member_id = ?", (member_id,),
        ):
            session = _session_data(row["data"])
            if preserve_api_keys and session.get('api_key_id'):
                continue
            if session["revoked_at"] is None:
                self._touch_session(connection, session, revoked_at=now)

    @staticmethod
    def _touch_session(connection, session: dict, **changes) -> dict:
        session.update(changes)
        connection.execute(
            "UPDATE team_sessions SET data = ? WHERE id = ?", (_json(session), session["id"])
        )
        return session

    def _fresh_session(self, connection, session: dict):
        """Resolve a session row to its active member, or None when it no longer applies."""
        if session["revoked_at"] is not None:
            return None
        if session["expires_at"] is not None and session["expires_at"] <= _now():
            self._touch_session(connection, session, revoked_at=_now())
            return None
        row = connection.execute(
            "SELECT data FROM team_members WHERE id = ?", (session["member_id"],)
        ).fetchone()
        if row is None:
            self._touch_session(connection, session, revoked_at=_now())
            return None
        member = _member_data(row["data"])
        key_session = bool(session.get('api_key_id'))
        if (not member["active"] or key_session and member['role'] not in ('admin', 'operator')
                or not key_session and member["permission_version"] != session["permission_version"]):
            self._touch_session(connection, session, revoked_at=_now())
            return None
        return member, session

    def _member_permissions(self, connection, member_id: str) -> dict:
        grants: dict[str, set] = {}
        member = self._team_row(connection, member_id)
        for row in connection.execute(
            "SELECT g.data FROM team_grants g JOIN profiles p ON p.id = g.profile_id "
            "WHERE g.member_id = ? UNION ALL "
            "SELECT g.data FROM team_group_grants g "
            "JOIN team_group_members m ON m.group_id = g.group_id "
            "WHERE m.member_id = ?", (member_id, member_id)
        ):
            grant = _grant_data(row["data"])
            effective = set(grant['permissions']) & ROLE_PERMISSIONS[member['role']]
            if effective:
                grants.setdefault(grant['profile_id'], set()).update(effective)
        return grants

    def begin_team_setup(self) -> str:
        """Rotate an unfinished local setup while the desktop owns its workspace."""
        code_path = self.root / TEAM_SETUP_CODE_PATH
        with self._write() as (connection, _):
            if connection.execute("SELECT 1 FROM team_members LIMIT 1").fetchone():
                raise ValidationError('team_already_initialized')
            if code_path.resolve() != code_path:
                raise StoreError('The local team setup path is redirected.')
            code_path.unlink(missing_ok=True)
            code = secrets.token_urlsafe(32)
            descriptor = os.open(code_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'w', encoding='ascii') as stream:
                stream.write(code)
            return code

    def initialize_team(self, setup_code: str, name: str, display: str, password: str) -> dict:
        """Create the first administrator against a one-time local setup code."""
        _text(setup_code, "setup code", 64, required=True)
        validate_member_password(password)
        code_path = self.root / TEAM_SETUP_CODE_PATH
        with self._write() as (connection, _):
            if connection.execute("SELECT 1 FROM team_members LIMIT 1").fetchone():
                raise ValidationError("team_already_initialized")
            try:
                expected = code_path.read_bytes().decode("ascii").strip()
            except (OSError, UnicodeError):
                raise StoreError("The local team setup code is unavailable.") from None
            if not expected or not secrets.compare_digest(setup_code, expected):
                code_path.unlink(missing_ok=True)
                raise ValidationError("invalid_setup_code")
            member = self._create_member(connection, name, display, "admin", password)
            self._audit(connection, "team.initialized", target_kind="setup",
                        actor_id=member["id"], actor_name=member["name"])
            code_path.unlink(missing_ok=True)
        return member

    def create_member(self, actor_id: str, name: str, display: str, role: str, password: str) -> dict:
        validate_member_password(password)
        if role not in ("operator", "viewer"):
            raise ValidationError("New members are operators or viewers; there is exactly one administrator.")
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            return self._create_member(connection, name, display, role, password, actor=actor)

    def list_members(self) -> list[dict]:
        with self._connect() as connection:
            return self._list_members(connection)

    @staticmethod
    def _list_members(connection) -> list[dict]:
        rows = connection.execute("SELECT data FROM team_members ORDER BY rowid")
        return [
            {key: member[key] for key in
             ("id", "name", "display", "role", "active", "permission_version", "created_at", "updated_at")}
            for member in (_member_data(row["data"]) for row in rows)
        ]

    def set_member_active(self, actor_id: str, member_id: str, active: bool) -> dict:
        if type(active) is not bool:
            raise ValidationError("active must be a boolean.")
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            member = self._team_row(connection, member_id)
            if member["id"] == actor["id"]:
                raise ValidationError("cannot_change_self")
            if member["active"] == active:
                return {key: member[key] for key in
                        ("id", "name", "display", "role", "active", "permission_version")}
            if not active:
                self._revoke_sessions(connection, member["id"])
                self._bump_version(connection, member)
                self._touch(connection, member, active=False)
            else:
                self._touch(connection, member, active=True)
            self._audit(connection, "member.activated" if active else "member.disabled",
                        actor_id=actor["id"], actor_name=actor["name"],
                        target_kind="member", target_id=member["id"])
            return {key: member[key] for key in
                    ("id", "name", "display", "role", "active", "permission_version")}

    def reset_member_password(self, actor_id: str, member_id: str, password: str) -> None:
        validate_member_password(password)
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            member = self._active_member(connection, member_id)
            salt = secrets.token_bytes(16)
            self._revoke_sessions(connection, member["id"])
            self._bump_version(connection, member)
            self._touch(connection, member, password_salt=salt.hex(),
                        password_hash=_hash_password(password, salt, TEAM_PASSWORD_TEMPLATE),
                        password_template=TEAM_PASSWORD_TEMPLATE)
            self._audit(connection, "member.password_reset", actor_id=actor["id"],
                        actor_name=actor["name"], target_kind="member", target_id=member["id"])

    def change_own_password(self, member_id: str, current: str, new_password: str) -> None:
        if not isinstance(current, str):
            raise ValidationError("The current password is required.")
        validate_member_password(new_password)
        with self._write() as (connection, _):
            member = self._active_member(connection, member_id)
            salt = bytes.fromhex(member["password_salt"])
            candidate = _hash_password(current, salt, member["password_template"])
            if not secrets.compare_digest(candidate, member["password_hash"]):
                raise ValidationError("invalid_current_password")
            new_salt = secrets.token_bytes(16)
            self._revoke_sessions(connection, member["id"])
            self._bump_version(connection, member)
            self._touch(connection, member, password_salt=new_salt.hex(),
                        password_hash=_hash_password(new_password, new_salt, TEAM_PASSWORD_TEMPLATE),
                        password_template=TEAM_PASSWORD_TEMPLATE)
            self._audit(connection, "member.password_changed", actor_id=member["id"],
                        actor_name=member["name"], target_kind="member", target_id=member["id"])

    def login(self, name: str, password: str) -> dict:
        """Verify a member credential and open a session; failures are rate limited."""
        validate_member_name(name)
        if not isinstance(password, str):
            raise ValidationError("invalid_credentials")
        # Reserved attempt and lock rows must survive an expected credential failure.
        with self._write(failure_commits=True) as (connection, _):
            now = time.time()
            cutoff = _now_before(TEAM_LOGIN_FAILURE_WINDOW_SECONDS)
            connection.execute("DELETE FROM team_login_attempts WHERE failed_at < ?", (cutoff,))
            row = connection.execute(
                "SELECT failures, locked_until FROM team_login_limits WHERE member_name = ?", (name,)
            ).fetchone()
            if row and row["locked_until"] > now:
                raise ValidationError("login_locked")
            member = self._named_member(connection, name)
            verified = False
            if member is not None:
                salt = bytes.fromhex(member["password_salt"])
                candidate = _hash_password(password, salt, member["password_template"])
                verified = member["active"] and secrets.compare_digest(candidate, member["password_hash"])
            else:
                # Unknown accounts still pay the same scrypt cost; an HTTP login must not reveal them by timing.
                _hash_password(password, secrets.token_bytes(16), TEAM_PASSWORD_TEMPLATE)
            if not verified:
                connection.execute(
                    "INSERT INTO team_login_attempts (member_name, failed_at) VALUES (?, ?)",
                    (name, _now()),
                )
                failures = connection.execute(
                    "SELECT COUNT(*) FROM team_login_attempts WHERE member_name = ?", (name,)
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO team_login_limits (member_name, failures, locked_until) "
                    "VALUES (?, ?, 0) ON CONFLICT(member_name) DO UPDATE SET "
                    "failures=excluded.failures",
                    (name, failures),
                )
                if failures >= TEAM_LOGIN_FAILURE_LIMIT:
                    connection.execute(
                        "UPDATE team_login_limits SET locked_until = ? WHERE member_name = ?",
                        (now + TEAM_LOGIN_LOCK_SECONDS, name),
                    )
                    connection.execute(
                        "DELETE FROM team_login_attempts WHERE member_name = ?", (name,)
                    )
                raise ValidationError("invalid_credentials")
            connection.execute("DELETE FROM team_login_attempts WHERE member_name = ?", (name,))
            connection.execute("DELETE FROM team_login_limits WHERE member_name = ?", (name,))
            return self._open_session(connection, member)

    def _open_session(self, connection, member, *, local_automation=False):
        session = {
            "id": str(uuid4()), "token_hash": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
            "member_id": member["id"], "permission_version": member["permission_version"],
            "created_at": _now(), "expires_at": _now_after(12 * 3600), "revoked_at": None,
        }
        if local_automation:
            session['local_automation'] = True
        connection.execute("INSERT INTO team_sessions (id, member_id, data) VALUES (?, ?, ?)",
                           (session['id'], member['id'], _json(session)))
        self._audit(connection, "session.opened", actor_id=member["id"], actor_name=member["name"],
                    target_kind="session", target_id=session["id"],
                    detail='local_automation' if local_automation else '')
        return {"session_id": session["id"], "expires_at": session["expires_at"],
                "member": {key: member[key] for key in
                           ("id", "name", "display", "role", "permission_version")}}

    def open_local_automation_session(self, name):
        """The workspace owner explicitly lends a named member's access to local applications."""
        with self._write() as (connection, _):
            member = self._named_member(connection, name)
            if not member or not member['active'] or member['role'] not in ('admin', 'operator'):
                raise ValidationError('access_denied')
            return self._open_session(connection, member, local_automation=True)

    def renew_local_automation_session(self, session_id):
        """Keep a live local service session valid without reviving revoked or expired access."""
        with self._write() as (connection, _):
            row = connection.execute('SELECT data FROM team_sessions WHERE id = ?', (session_id,)).fetchone()
            session = _session_data(row['data']) if row else None
            if not session or not session.get('local_automation') or not self._fresh_session(connection, session):
                raise ValidationError('session_expired')
            if session['expires_at'] < _now_after(3600):
                self._touch_session(connection, session, expires_at=_now_after(12 * 3600))

    def resolve_session(self, session_id: str) -> Optional[dict]:
        """Resolve an active session to its member and grants, or None if it no longer applies."""
        with self._write() as (connection, _):
            return self._resolve_session(connection, session_id)

    def _resolve_session(self, connection, session_id):
        validate_id(session_id)
        row = connection.execute('SELECT data FROM team_sessions WHERE id = ?', (session_id,)).fetchone()
        resolved = self._fresh_session(connection, _session_data(row['data'])) if row else None
        if resolved is None:
            return None
        member, session = resolved
        result = {
            'session_id': session['id'], 'expires_at': session['expires_at'],
            'member': {key: member[key] for key in ('id', 'name', 'display', 'role', 'permission_version')},
            'permissions': self._member_permissions(connection, member['id']),
        }
        if session.get('api_key_id'):
            result.update(api_key_id=session['api_key_id'], profile_ids=session['profile_ids'])
        return result

    def logout(self, session_id: str) -> None:
        validate_id(session_id)
        with self._write() as (connection, _):
            row = connection.execute(
                "SELECT data FROM team_sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return
            session = _session_data(row["data"])
            if session["revoked_at"] is None:
                self._touch_session(connection, session, revoked_at=_now())

    def _save_grant(self, connection, principal, profile_id, permissions, actor, *, group=False) -> dict:
        table, field = ('team_group_grants', 'group_id') if group else ('team_grants', 'member_id')
        existing = connection.execute(
            f"SELECT data FROM {table} WHERE {field} = ? AND profile_id = ?",
            (principal["id"], profile_id),
        ).fetchone()
        timestamp = _now()
        if existing is None:
            grant = {
                "id": str(uuid4()), field: principal["id"], "profile_id": profile_id,
                "permissions": permissions, "created_at": timestamp, "updated_at": timestamp,
            }
            connection.execute(
                f"INSERT INTO {table} (id, {field}, profile_id, data) VALUES (?, ?, ?, ?)",
                (grant["id"], grant[field], grant["profile_id"], _json(grant)),
            )
        else:
            grant = _grant_data(existing["data"])
            grant.update(permissions=permissions, updated_at=timestamp)
            connection.execute(
                f"UPDATE {table} SET data = ? WHERE id = ?", (_json(grant), grant["id"])
            )
        if not group:
            self._bump_version(connection, principal)
        self._audit(connection, "group.grant_updated" if group else "grant.updated",
                    actor_id=actor["id"], actor_name=actor["name"],
                    target_kind="grant", target_id=grant["id"])
        return grant

    def grant_profile(self, actor_id: str, member_id: str, profile_id: str, permissions: list) -> dict:
        permissions = validate_permission_codes(permissions)
        validate_id(profile_id)
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            member = self._active_member(connection, member_id)
            if member["role"] == "admin":
                raise ValidationError("grant_not_applicable")
            if not set(permissions) <= ROLE_PERMISSIONS[member['role']]:
                raise ValidationError('role_permission_denied')
            if connection.execute(
                "SELECT 1 FROM profiles WHERE id = ?", (profile_id,)
            ).fetchone() is None:
                raise ValidationError("The profile does not exist.")
            return dict(self._save_grant(connection, member, profile_id, permissions, actor))

    def revoke_profile(self, actor_id: str, member_id: str, profile_id: str) -> None:
        validate_id(profile_id)
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            member = self._team_row(connection, member_id)
            row = connection.execute(
                "SELECT data FROM team_grants WHERE member_id = ? AND profile_id = ?",
                (member_id, profile_id),
            ).fetchone()
            if row is None:
                return
            grant = _grant_data(row["data"])
            connection.execute("DELETE FROM team_grants WHERE id = ?", (grant["id"],))
            self._bump_version(connection, member)
            self._audit(connection, "grant.revoked", actor_id=actor["id"], actor_name=actor["name"],
                        target_kind="grant", target_id=grant["id"])

    def list_grants(self, member_id: Optional[str] = None) -> list[dict]:
        with self._connect() as connection:
            if member_id is not None:
                self._active_member(connection, member_id)
                rows = connection.execute(
                    "SELECT data FROM team_grants WHERE member_id = ? ORDER BY rowid", (member_id,)
                )
            else:
                rows = connection.execute("SELECT data FROM team_grants ORDER BY rowid")
            grants = []
            for row in rows:
                grant = _grant_data(row["data"])
                if connection.execute(
                    "SELECT 1 FROM profiles WHERE id = ?", (grant["profile_id"],)
                ).fetchone() is not None:
                    grants.append(dict(grant))
            return grants

    def create_member_group(self, actor_id: str, name: str) -> dict:
        name = _text(name, 'member group name', 128, required=True)
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            if connection.execute('SELECT 1 FROM team_groups WHERE name = ?', (name,)).fetchone():
                raise ValidationError('group_name_in_use')
            timestamp = _now()
            group = {'id': str(uuid4()), 'name': name, 'revision': 1,
                     'created_at': timestamp, 'updated_at': timestamp}
            connection.execute('INSERT INTO team_groups (id, name, data) VALUES (?, ?, ?)',
                               (group['id'], name, _json(group)))
            self._audit(connection, 'group.created', actor_id=actor['id'], actor_name=actor['name'],
                        target_kind='member_group', target_id=group['id'])
            return group

    def list_member_groups(self) -> list[dict]:
        with self._connect() as connection:
            return [json.loads(row['data']) for row in connection.execute(
                'SELECT data FROM team_groups ORDER BY rowid')]

    def get_member_group(self, group_id: str) -> dict:
        with self._connect() as connection:
            return self._team_row(connection, group_id, kind='group')

    def list_group_members(self, group_id: str) -> list[str]:
        with self._connect() as connection:
            self._team_row(connection, group_id, kind='group')
            return self._group_member_ids(connection, group_id)

    @staticmethod
    def _group_member_ids(connection, group_id):
        return [row['member_id'] for row in connection.execute(
            'SELECT member_id FROM team_group_members WHERE group_id = ? ORDER BY member_id', (group_id,))]

    def _checked_group(self, connection, group_id, expected_revision):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationError('The expected revision must be a positive integer.')
        group = self._team_row(connection, group_id, kind='group')
        if group['revision'] != expected_revision:
            raise ValidationError('revision_conflict')
        return group

    def _touch_group(self, connection, group, actor, action):
        group.update(revision=group['revision'] + 1, updated_at=_now())
        connection.execute('UPDATE team_groups SET data = ? WHERE id = ?', (_json(group), group['id']))
        self._audit(connection, action, actor_id=actor['id'], actor_name=actor['name'],
                    target_kind='member_group', target_id=group['id'])
        return group

    def _invalidate_members(self, connection, member_ids):
        for identifier in sorted(member_ids):
            self._revoke_sessions(connection, identifier, preserve_api_keys=True)
            self._bump_version(connection, self._team_row(connection, identifier))

    def set_group_members(self, actor_id: str, group_id: str, member_ids: list, *, expected_revision: int) -> dict:
        if not isinstance(member_ids, list):
            raise ValidationError('Group members must be a list of distinct member IDs.')
        for identifier in member_ids:
            validate_id(identifier)
        if len(set(member_ids)) != len(member_ids):
            raise ValidationError('Group members must be distinct.')
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            group = self._checked_group(connection, group_id, expected_revision)
            for identifier in member_ids:
                if self._team_row(connection, identifier)['role'] == 'admin':
                    raise ValidationError('grant_not_applicable')
            previous = set(self._group_member_ids(connection, group_id))
            wanted = set(member_ids)
            if previous == wanted:
                return group
            connection.executemany('DELETE FROM team_group_members WHERE group_id = ? AND member_id = ?',
                                   [(group_id, identifier) for identifier in previous - wanted])
            connection.executemany('INSERT INTO team_group_members (group_id, member_id) VALUES (?, ?)',
                                   [(group_id, identifier) for identifier in wanted - previous])
            self._invalidate_members(connection, previous ^ wanted)
            return self._touch_group(connection, group, actor, 'group.members_updated')

    def list_group_grants(self, group_id: str) -> list[dict]:
        with self._connect() as connection:
            self._team_row(connection, group_id, kind='group')
            return [json.loads(row['data']) for row in connection.execute(
                'SELECT data FROM team_group_grants WHERE group_id = ? ORDER BY rowid', (group_id,))]

    def save_group_grants(self, actor_id: str, group_id: str, changes: dict, *, expected_revision: int) -> dict:
        """Apply only supplied rows atomically; absent archived grants remain intact."""
        if not isinstance(changes, dict):
            raise ValidationError('Group grant changes must map profile IDs to permission lists.')
        normalized = {}
        for profile_id, permissions in changes.items():
            validate_id(profile_id)
            permissions = sorted(validate_permission_codes(permissions, empty=True))
            if not set(permissions) <= GROUP_PERMISSIONS:
                raise ValidationError('role_permission_denied')
            normalized[profile_id] = permissions
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            group = self._checked_group(connection, group_id, expected_revision)
            existing = {grant['profile_id']: grant for grant in
                        (json.loads(row['data']) for row in connection.execute(
                            'SELECT data FROM team_group_grants WHERE group_id = ?', (group_id,)))}
            changed = False
            for profile_id, permissions in normalized.items():
                self._get(connection, 'profiles', profile_id)
                previous = existing.get(profile_id)
                if set(permissions) == set(previous['permissions'] if previous else []):
                    continue
                changed = True
                if permissions:
                    self._save_grant(connection, group, profile_id, permissions, actor, group=True)
                else:
                    connection.execute('DELETE FROM team_group_grants WHERE id = ?', (previous['id'],))
                    self._audit(connection, 'group.grant_revoked', actor_id=actor['id'], actor_name=actor['name'],
                                target_kind='grant', target_id=previous['id'])
            if not changed:
                return group
            self._invalidate_members(connection, self._group_member_ids(connection, group_id))
            return self._touch_group(connection, group, actor, 'group.grants_updated')

    def delete_member_group(self, actor_id: str, group_id: str, *, expected_revision: int) -> None:
        with self._write() as (connection, _):
            actor = self._admin_member(connection, actor_id)
            self._checked_group(connection, group_id, expected_revision)
            self._invalidate_members(connection, self._group_member_ids(connection, group_id))
            connection.execute('DELETE FROM team_groups WHERE id = ?', (group_id,))
            self._audit(connection, 'group.deleted', actor_id=actor['id'], actor_name=actor['name'],
                        target_kind='member_group', target_id=group_id)

    def visible_profiles(self, member_id: str, *, archived=False) -> list[dict]:
        with self._write() as (connection, _):
            member = self._active_member(connection, member_id)
            if member["role"] == "admin":
                grants = None
            else:
                grants = self._member_permissions(connection, member["id"])
            rows = connection.execute(
                "SELECT data FROM profiles WHERE archived = ? ORDER BY rowid DESC", (int(archived),)
            )
            return [
                _profile_data(row["data"]) for row in rows
                if grants is None or 'view' in grants.get(_profile_data(row["data"])["id"], set())
            ]

    def check_profile_permission(self, member_id: str, profile_id: str, permission: str) -> None:
        if permission not in GRANT_PERMISSIONS:
            raise ValidationError("Unknown permission code.")
        with self._write() as (connection, _):
            member = self._active_member(connection, member_id)
            if member["role"] == "admin":
                self._get(connection, "profiles", profile_id)
                return
            grants = self._member_permissions(connection, member_id)
            if permission not in grants.get(profile_id, set()):
                # Unknown and unauthorized profiles both fail without disclosing existence.
                raise ValidationError("access_denied")

    def check_runtime(self, *, headless):
        from .manifest import verify

        verify(self, headless=headless)

    def native_platform_accepted(self, profile):
        from .migration import native_platform_accepted

        return native_platform_accepted(self, profile)

    def check_execution_ownership(self, profile_id, *, run_id=None, owner=None):
        """Only a reserved team run may start while durable execution records remain active."""
        with self._connect() as connection:
            barrier = connection.execute(
                "SELECT value FROM workspace_metadata WHERE key = 'execution_barrier'").fetchone()
            if barrier:
                reason = json.loads(barrier['value']).get('reason')
                raise StoreError('The workspace is retired.' if reason == 'retired' else
                                 'The restored workspace is quarantined until an exclusive transfer is verified.')
            # A checked-out profile runs only on the lease holder; an unfinished return commit
            # may have moved its directory, so neither can start here.
            if connection.execute(
                    "SELECT 1 FROM team_checkouts WHERE profile_id = ? AND state IN ('checked_out', 'returning')",
                    (profile_id,)).fetchone() or connection.execute(
                    "SELECT 1 FROM workspace_metadata WHERE key LIKE 'checkout_commit:%' "
                    "AND json_extract(value, '$.profile_id') = ?", (profile_id,)).fetchone():
                raise StoreError('This profile is checked out to another computer or still being returned.')
            rows = connection.execute(
                "SELECT id, profile_id, owner, state FROM team_runs WHERE state != 'stopped'").fetchall()
            allowed = not rows if run_id is None else (
                all(row['owner'] == owner and row['state'] != 'recovery_required' for row in rows)
                and any(row['id'] == run_id and row['profile_id'] == profile_id and row['state'] == 'starting'
                        for row in rows))
            if not allowed:
                raise StoreError('Durable execution ownership requires manual recovery before another launch.')

    def record_audit(self, action, *, actor_id=None, actor_name="", target_kind="",
                     target_id=None, result="ok", detail="") -> None:
        with self._write() as (connection, _):
            self._audit(connection, action, actor_id, actor_name, target_kind, target_id,
                        result, detail)

    def list_audit(self, limit: int = 200) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValidationError("Audit limit must be an integer between 1 and 10000.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM team_audit ORDER BY id DESC LIMIT ?", (limit,)
            )
            return [_audit_data(row) for row in rows]

    def profile_dir(self, profile_id: str) -> Path:
        return self.root / "profiles" / validate_id(profile_id)

    def list_profiles(self, archived: bool = False) -> list[dict]:
        if type(archived) is not bool:
            raise ValidationError("archived must be a boolean.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data FROM profiles WHERE archived = ? ORDER BY rowid DESC", (archived,)
            )
            return [_profile_data(row["data"]) for row in rows]

    def get_profile(self, profile_id: str) -> dict:
        with self._connect() as connection:
            return self._get(connection, "profiles", profile_id)

    def uses_team_kernels(self):
        """Team profiles run only their exact kernel; the debug executable setting never applies."""
        return bool(self.list_members())

    def workspace_id(self):
        with self._connect() as connection:
            return connection.execute("SELECT value FROM workspace_metadata WHERE key = 'workspace_id'").fetchone()[0]

    def pending_commits(self, prefix):
        with self._connect() as connection:
            return [row[0] for row in connection.execute(
                'SELECT key FROM workspace_metadata WHERE key LIKE ?', (prefix + '%',))]

    def mirror_profile(self, profile: dict, proxy: Optional[dict]) -> dict:
        """Hold a server profile under its server IDs in a client cache; the server stays authoritative."""
        profile_id = validate_id(profile.get('id'))
        values = validate_profile({key: profile[key] for key in PROFILE_FIELDS})
        if (proxy is None) != (values['proxy_id'] is None) or proxy and proxy.get('id') != values['proxy_id']:
            raise ValidationError('The check-out proxy does not match the profile.')
        for field in ('revision', 'data_revision'):
            if type(profile.get(field)) is not int or profile[field] < 0:
                raise ValidationError('invalid_request')
        identity = profile.get('identity')
        if identity is not None:
            _identity_digest(identity)
        with self._write() as (connection, undo):
            if proxy is not None:
                proxy_id = validate_id(proxy['id'])
                checked = self._validate_proxy({**{key: proxy[key] for key in PROXY_FIELDS & proxy.keys()},
                                                "node": proxy.get("node", {})})
                row = connection.execute('SELECT data FROM proxies WHERE id = ?', (proxy_id,)).fetchone()
                flags = {}
                for field, suffix in _PROXY_SECRETS.items():
                    key = proxy_id + suffix
                    previous = self._secret('get', key) if row and _proxy_data(row['data'])[f'has_{field}'] else None
                    undo.append((key, previous))
                    if checked.get(field):
                        self._secret('set', key, checked[field])
                    elif previous is not None:
                        self._secret('delete', key)
                    flags[f'has_{field}'] = bool(checked.get(field))
                stored = {key: checked[key] for key in sorted(_PROXY_CONFIG_FIELDS)}
                stored.update(id=proxy_id, **flags, created_at=proxy.get('created_at') or _now(),
                              updated_at=proxy.get('updated_at') or _now())
                connection.execute(
                    'INSERT INTO proxies (id, data, has_password) VALUES (?, ?, ?) '
                    'ON CONFLICT(id) DO UPDATE SET data=excluded.data, has_password=excluded.has_password',
                    (proxy_id, _json(stored), flags['has_password']))
            row = connection.execute('SELECT data FROM profiles WHERE id = ?', (profile_id,)).fetchone()
            existing = _profile_data(row['data']) if row else {}
            result = {**values, 'id': profile_id, 'identity': identity, 'revision': profile['revision'],
                      'data_revision': profile['data_revision'],
                      'created_at': existing.get('created_at', _now()), 'updated_at': _now(),
                      'last_launch_at': existing.get('last_launch_at')}
            connection.execute(
                'INSERT INTO profiles (id, proxy_id, archived, data) VALUES (?, ?, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET proxy_id=excluded.proxy_id, archived=excluded.archived, '
                'data=excluded.data', (profile_id, result['proxy_id'], result['archived'], _json(result)))
            self._event(connection, 'profile.mirrored', profile_id)
            return result

    def clear_proxy_secrets(self, proxy_id: str) -> None:
        """Remove a returned check-out's proxy credentials from this host's keyring."""
        with self._write() as (connection, undo):
            proxy = self._get(connection, 'proxies', proxy_id)
            for field, suffix in _PROXY_SECRETS.items():
                if proxy[f'has_{field}']:
                    key = proxy_id + suffix
                    undo.append((key, self._secret('get', key)))
                    self._secret('delete', key)
                    proxy[f'has_{field}'] = False
            connection.execute('UPDATE proxies SET data = ?, has_password = 0 WHERE id = ?',
                               (_json(proxy), proxy_id))

    def kernel_references(self):
        """Exact kernels and version-only identity versions that profiles still require."""
        kernels, versions = set(), set()
        with self._connect() as connection:
            default = connection.execute("SELECT value FROM workspace_metadata WHERE key='default_kernel'").fetchone()
            if default:
                kernels.add(default[0])
            for row in connection.execute('SELECT data FROM profiles'):
                profile = _profile_data(row['data'])
                identity = profile.get('identity') or {}
                if identity.get('kernel'):
                    kernels.add(identity['kernel'])
                elif identity.get('browser_version'):
                    versions.add(identity['browser_version'])
                if profile['kernel']:
                    kernels.add(profile['kernel'])
        return kernels, versions

    def get_default_kernel(self):
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM workspace_metadata WHERE key='default_kernel'").fetchone()
            return row[0] if row else ''

    def set_default_kernel(self, identifier, *, initialize=False):
        from .kernels import validate_kernel
        try:
            if not validate_kernel(identifier):
                raise ValueError
        except ValueError:
            raise ValidationError('invalid_kernel') from None
        with self._write() as (connection, _):
            connection.execute("INSERT OR REPLACE INTO workspace_metadata VALUES ('default_kernel', ?)", (identifier,))
            if initialize:
                for row in connection.execute('SELECT id, data FROM profiles').fetchall():
                    profile = json.loads(row['data'])
                    if not profile.get('kernel') and profile['identity'] is None:
                        profile['kernel'] = identifier
                        connection.execute('UPDATE profiles SET data = ? WHERE id = ?', (_json(profile), row['id']))

    def _save_profile(self, connection, data, profile_id=None) -> dict:
        _fields(data, PROFILE_FIELDS)
        existing = self._get(connection, "profiles", profile_id) if profile_id is not None else {}
        if existing.get('kernel') and data.get('kernel') == '':
            raise ValidationError('invalid_kernel')
        if not existing and isinstance(data.get("fingerprint", {}), dict):
            data = {**data, "fingerprint": {**NEW_PROFILE_DEFAULTS, **data.get("fingerprint", {})}}
        editable = {key: existing[key] for key in PROFILE_FIELDS if key in existing}
        if not existing and 'kernel' not in data:
            row = connection.execute("SELECT value FROM workspace_metadata WHERE key='default_kernel'").fetchone()
            data = {**data, 'kernel': row[0] if row else ''}
        values = validate_profile({**editable, **data})
        if values["proxy_id"] is not None:
            proxy = connection.execute(
                "SELECT 1 FROM proxies WHERE id = ?", (values["proxy_id"],)
            ).fetchone()
            if proxy is None:
                raise ValidationError("The selected proxy does not exist.")
        identity = existing.get("identity")
        if (any(existing.get(key, '') != values[key] for key in ("os", "locale", "kernel"))
                or any(existing.get("fingerprint", {}).get(key) != value
                       for key, value in values["fingerprint"].items() if key not in SCREEN_OPTIONS)):
            identity = None
        timestamp = _now()
        result = {
            **values, "id": profile_id or str(uuid4()), "created_at": existing.get("created_at", timestamp),
            "updated_at": timestamp, "last_launch_at": existing.get("last_launch_at"), "identity": identity,
            "revision": existing.get("revision", 0) + 1, "data_revision": existing.get("data_revision", 0),
        }
        connection.execute(
            "INSERT INTO profiles (id, proxy_id, archived, data) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET proxy_id=excluded.proxy_id, "
            "archived=excluded.archived, data=excluded.data",
            (result["id"], result["proxy_id"], result["archived"], _json(result)),
        )
        self._event(connection, "profile.updated" if existing else "profile.created", result["id"])
        return result

    def save_profile(self, data: dict, profile_id: Optional[str] = None) -> dict:
        with self._write() as (connection, _):
            return self._save_profile(connection, data, profile_id)

    def update_profile_checked(self, data: dict, profile_id: str, expected_revision: int) -> dict:
        """Apply an edit only when the caller saw the current configuration revision."""
        with self._write() as (connection, _):
            self._checked_profile(connection, profile_id, expected_revision)
            return self._save_profile(connection, data, profile_id)

    def _checked_profile(self, connection, profile_id: str, expected_revision: int) -> dict:
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValidationError("The expected revision must be a positive integer.")
        row = connection.execute(
            "SELECT data FROM profiles WHERE id = ?", (validate_id(profile_id),)
        ).fetchone()
        if row is None:
            raise KeyError("The profile does not exist.")
        profile = _profile_data(row["data"])
        if profile["revision"] != expected_revision:
            raise ValidationError("revision_conflict")
        return profile

    def clone_profile(self, profile_id: str) -> dict:
        with self._write() as (connection, _):
            source = self._get(connection, "profiles", profile_id)
            return self._clone_profile(connection, source)

    def clone_profile_checked(self, profile_id: str, expected_revision: int) -> dict:
        with self._write() as (connection, _):
            source = self._checked_profile(connection, profile_id, expected_revision)
            return self._clone_profile(connection, source)

    def _clone_profile(self, connection, source: dict) -> dict:
        values = {key: source[key] for key in PROFILE_FIELDS}
        values["archived"] = False
        if len(values["name"]) <= 121:
            values["name"] += " (copy)"
        result = self._save_profile(connection, values)
        self._event(connection, "profile.cloned", result["id"])
        return result

    def archive_profile(self, profile_id: str, archived: bool = True) -> dict:
        return self.save_profile({"archived": archived}, profile_id)

    def delete_profile(self, profile_id: str, *, remove_data: bool = False) -> None:
        if type(remove_data) is not bool:
            raise ValidationError('remove_data must be a boolean.')
        with self._write() as (connection, _):
            self._get(connection, "profiles", profile_id)
            self._delete_profile(connection, profile_id, remove_data=remove_data)

    def delete_profile_checked(self, profile_id: str, expected_revision: int, *, remove_data: bool = False) -> None:
        if type(remove_data) is not bool:
            raise ValidationError('remove_data must be a boolean.')
        with self._write() as (connection, _):
            self._checked_profile(connection, profile_id, expected_revision)
            self._delete_profile(connection, profile_id, remove_data=remove_data)

    def _delete_profile(self, connection, profile_id: str, *, remove_data: bool) -> None:
        if connection.execute(
                "SELECT 1 FROM team_checkouts WHERE profile_id = ? AND state IN ('checked_out', 'returning')",
                (profile_id,)).fetchone():
            raise ValidationError('profile_checked_out')
        if remove_data:
            directory = self.profile_dir(profile_id)
            try:
                # Refuse redirects through a profile link or any ancestor junction.
                if directory.resolve() != directory:
                    raise ValidationError('环境数据目录包含链接或重定向，已拒绝删除。')
                if directory.exists():
                    shutil.rmtree(directory)
            except OSError:
                raise StoreError('浏览器数据未能完全删除，环境记录已保留。请关闭占用文件的程序并检查权限。') from None
        connection.execute("DELETE FROM team_grants WHERE profile_id = ?", (profile_id,))
        connection.execute("DELETE FROM team_checkouts WHERE profile_id = ?", (profile_id,))
        connection.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        self._event(connection, "profile.deleted", profile_id)

    def _profile_state(self, connection, profile: dict, changes: dict, action: str):
        profile.update(changes)
        profile["updated_at"] = _now()
        connection.execute(
            "UPDATE profiles SET data = ? WHERE id = ?", (_json(profile), profile["id"])
        )
        self._event(connection, action, profile["id"])
        return profile

    def set_identity(self, profile_id: str, identity: Optional[dict]) -> dict:
        digest = _identity_digest(identity) if identity is not None else None
        with self._write() as (connection, _):
            profile = self._get(connection, "profiles", profile_id)
            if digest is not None:
                self._remember_identity(connection, digest)
            return self._profile_state(
                connection, profile, {"identity": identity}, "profile.identity_updated"
            )

    def reset_identity_checked(self, profile_id: str, expected_revision: int) -> dict:
        with self._write() as (connection, _):
            profile = self._checked_profile(connection, profile_id, expected_revision)
            return self._profile_state(
                connection, profile,
                {"identity": None, "revision": profile["revision"] + 1},
                "profile.identity_updated",
            )

    def claim_identity(self, profile_id: str, identity: dict) -> bool:
        """Save a generated identity only if its effective configuration has never been used."""
        digest = _identity_digest(identity)
        with self._write() as (connection, _):
            profile = self._get(connection, "profiles", profile_id)
            if not self._remember_identity(connection, digest):
                return False
            self._profile_state(
                connection, profile, {"identity": identity}, "profile.identity_updated"
            )
            return True

    def mark_launched(self, profile_id: str) -> dict:
        with self._write() as (connection, _):
            profile = self._get(connection, "profiles", profile_id)
            return self._profile_state(
                connection, profile, {"last_launch_at": _now()}, "browser.launched"
            )

    def list_proxies(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT data FROM proxies ORDER BY rowid DESC")
            return [_proxy_data(row["data"]) for row in rows]

    def get_proxy(self, proxy_id: str, reveal_password: bool = False) -> dict:
        if type(reveal_password) is not bool:
            raise ValidationError("reveal_password must be a boolean.")
        with self._lock, self._connect() as connection:
            proxy = self._get(connection, "proxies", proxy_id)
            if reveal_password:
                if proxy['node']:
                    raise ValidationError('This proxy uses an unsupported node or chain. Select an HTTP, HTTPS or SOCKS5 proxy.')
                normalize_proxy_server(proxy['server'])
                for field, suffix in _PROXY_SECRETS.items():
                    value = self._secret("get", proxy_id + suffix) if proxy[f"has_{field}"] else ""
                    if value is None:
                        raise SecretStoreError(
                            "A proxy credential is missing from the operating-system credential store."
                        )
                    proxy[field] = value
            return proxy

    def record_proxy_timezone(self, proxy_id, updated_at, timezone):
        _validate_timezone(timezone, required=True)
        validate_id(proxy_id)
        with self._write() as (connection, _):
            row = connection.execute('SELECT data FROM proxies WHERE id = ?', (proxy_id,)).fetchone()
            if row is None:
                return False
            proxy = _proxy_data(row['data'])
            if proxy['updated_at'] != updated_at:
                return False
            proxy['last_probe'] = {'timezone': timezone, 'checked_at': _now()}
            connection.execute('UPDATE proxies SET data = ? WHERE id = ?', (_json(proxy), proxy_id))
        return True

    @staticmethod
    def _validate_proxy(data: dict, existing: Optional[dict] = None) -> dict:
        _fields(data, PROXY_FIELDS | {"node"})
        values = {"name": "", "server": "", "username": "", "node": {}, **(existing or {}), **data}
        _text(values["name"], "proxy name", 128, required=True)
        values["server"] = normalize_proxy_server(values["server"])
        _text(values["username"], "proxy username", 512)
        _text(values.get("password", ""), "proxy password", 4096)
        if values["node"] != {}:
            raise ValidationError("Proxy nodes and chains are unsupported. Use HTTP, HTTPS or SOCKS5.")
        if type(values.get("clear_password", False)) is not bool:
            raise ValidationError("clear_password must be a boolean.")
        if values.get("clear_password") and values.get("password"):
            raise ValidationError("A proxy password cannot be set and cleared together.")
        return values

    def _save_proxy(self, connection, undo, data, proxy_id=None) -> dict:
        existing = self._get(connection, "proxies", proxy_id) if proxy_id is not None else None
        values = self._validate_proxy(data, existing)
        identifier = proxy_id or str(uuid4())
        flags = {f"has_{field}": bool(existing and existing[f"has_{field}"]) for field in _PROXY_SECRETS}
        for field, suffix in _PROXY_SECRETS.items():
            flag, key = f"has_{field}", identifier + suffix
            if values.get(field) or (values.get(f"clear_{field}") and flags[flag]):
                previous = self._secret("get", key) if flags[flag] else None
                undo.append((key, previous))
                if values.get(f"clear_{field}"):
                    self._secret("delete", key)
                    flags[flag] = False
                else:
                    self._secret("set", key, values[field])
                    flags[flag] = True
        timestamp = _now()
        result = {key: values[key] for key in sorted(_PROXY_CONFIG_FIELDS)}
        result.update({
            "id": identifier, **flags,
            "created_at": existing["created_at"] if existing else timestamp, "updated_at": timestamp,
        })
        connection.execute(
            "INSERT INTO proxies (id, data, has_password) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET data=excluded.data, has_password=excluded.has_password",
            (identifier, _json(result), flags["has_password"]),
        )
        self._event(connection, "proxy.updated" if existing else "proxy.created")
        return result

    def save_proxy(self, data: dict, proxy_id: Optional[str] = None) -> dict:
        with self._write() as (connection, undo):
            return self._save_proxy(connection, undo, data, proxy_id)

    def delete_proxy(self, proxy_id: str) -> None:
        with self._write() as (connection, undo):
            proxy = self._get(connection, "proxies", proxy_id)
            if connection.execute("SELECT 1 FROM profiles WHERE proxy_id = ?", (proxy_id,)).fetchone():
                raise ValidationError("Remove this proxy from every profile before deleting it.")
            for field, suffix in _PROXY_SECRETS.items():
                if proxy[f"has_{field}"]:
                    key = proxy_id + suffix
                    previous = self._secret("get", key)
                    undo.append((key, previous))
                    self._secret("delete", key)
            connection.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
            self._event(connection, "proxy.deleted")

    def import_proxies(self, text: str) -> list[dict]:
        _text(text, "proxy import", 5_000_000, multiline=True)
        lines = [(number, line) for number, line in enumerate(text.splitlines(), 1)
                 if line.strip() and not line.lstrip().startswith('#')]
        if not 1 <= len(lines) <= 10000:
            raise ValidationError("A proxy import must contain between 1 and 10000 entries.")
        values = []
        for number, line in lines:
            try:
                values.append(parse_proxy_line(line))
            except ValidationError as error:
                raise ValidationError(f"Invalid proxy on line {number}: {error}") from None
        with self._write() as (connection, undo):
            return [self._save_proxy(connection, undo, value) for value in values]

    def export_config(self, kinds=None) -> dict:
        """Export configuration only. Team membership, sessions and audit never leave the host."""
        selection = {"proxies", "profiles"} if kinds is None else set(kinds)
        if not selection or selection - _EXPORT_KINDS:
            raise ValidationError("Unknown export kind.")
        if selection - {"proxies", "profiles"}:
            raise ValidationError("Team members, sessions, grants and audit records are not exportable.")
        with self._connect() as connection:
            connection.execute("BEGIN")
            profiles = [
                _profile_data(row["data"])
                for row in connection.execute("SELECT data FROM profiles ORDER BY rowid")
            ]
            proxies = [
                _proxy_data(row["data"])
                for row in connection.execute("SELECT data FROM proxies ORDER BY rowid")
            ]
        return {
            "schema": EXPORT_SCHEMA, "version": 2,
            "profiles": [{key: value[key] for key in sorted(PROFILE_FIELDS | {"id"})} for value in profiles],
            "proxies": [{key: value[key] for key in sorted(_PROXY_CONFIG_FIELDS | {"id"})} for value in proxies],
        }

    def import_config(self, payload: dict) -> dict:
        _fields(payload, frozenset({"schema", "version", "profiles", "proxies"}))
        if (
            payload.get("schema") != EXPORT_SCHEMA
            or type(payload.get("version")) is not int
            or not 1 <= payload["version"] <= 2
        ):
            raise ValidationError("The configuration export schema or version is unsupported.")
        for field in ("profiles", "proxies"):
            if not isinstance(payload.get(field), list) or len(payload[field]) > 10000:
                raise ValidationError("The import requires profile and proxy lists of at most 10000 items.")
        proxy_values, profile_values, seen = [], [], set()
        for field, allowed, destination in (
            ("proxies", _PROXY_CONFIG_FIELDS | {"id"}, proxy_values),
            ("profiles", PROFILE_FIELDS | {"id"}, profile_values),
        ):
            for item in payload[field]:
                _fields(item, allowed)
                identifier = validate_id(item.get("id"))
                if identifier in seen:
                    raise ValidationError("Import identifiers must be unique.")
                seen.add(identifier)
                editable = {key: value for key, value in item.items() if key != "id"}
                validated = (
                    self._validate_proxy(editable) if field == "proxies"
                    else validate_profile(editable)
                )
                destination.append((identifier, validated))
        proxy_ids = {identifier for identifier, _ in proxy_values}
        if any(
            values["proxy_id"] is not None and values["proxy_id"] not in proxy_ids
            for _, values in profile_values
        ):
            raise ValidationError("An imported profile refers to a proxy missing from this import.")
        with self._write() as (connection, undo):
            mapping = {
                old: self._save_proxy(connection, undo, values)["id"]
                for old, values in proxy_values
            }
            for _, values in profile_values:
                values["proxy_id"] = mapping.get(values["proxy_id"])
                self._save_profile(connection, values)
            self._event(connection, "configuration.imported")
        return {"profiles": len(profile_values), "proxies": len(proxy_values)}

    def record_event(self, action: str, profile_id: Optional[str] = None, detail: str = "") -> None:
        """Accept fixed event/reason codes only, never URLs or exception messages."""
        for field, value, limit in (("action", action, 64), ("detail", detail, 120)):
            _text(value, field, limit, required=field == "action")
            if value and not _CODE.fullmatch(value):
                raise ValidationError("Audit events require fixed lowercase codes without user or exception text.")
        if profile_id is not None:
            validate_id(profile_id)
        with self._write() as (connection, _):
            self._event(connection, action, profile_id, detail)

    def list_events(self, limit: int = 200) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValidationError("Event limit must be an integer between 1 and 10000.")
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in rows]

    @staticmethod
    def _get_settings(connection) -> dict:
        row = connection.execute("SELECT data FROM settings WHERE id = 1").fetchone()
        saved = json.loads(row["data"]) if row else {}
        return {key: saved.get(key, default) for key, default in _SETTINGS_DEFAULTS.items()}

    def get_settings(self) -> dict:
        with self._connect() as connection:
            return self._get_settings(connection)

    def save_settings(self, data: dict) -> dict:
        _fields(data, frozenset(_SETTINGS_DEFAULTS))
        with self._write() as (connection, _):
            previous = self._get_settings(connection)
            values = {**previous, **data}
            if type(values["max_running"]) is not int or not 1 <= values["max_running"] <= 50:
                raise ValidationError("Maximum concurrent browsers must be an integer between 1 and 50.")
            _text(values["executable_path"], "executable path", 4096)
            for field in ("automation_enabled", "team_automation_enabled"):
                if type(values[field]) is not bool:
                    raise ValidationError("Automation enabled must be a boolean.")
            connection.execute("INSERT OR REPLACE INTO settings (id, data) VALUES (1, ?)", (_json(values),))
            self._event(connection, "settings.updated")
            return values
