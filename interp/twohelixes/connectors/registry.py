"""Connector registry, construction, and the connection cache.

Connections are cached per source id with a TTL. Opening a Snowflake session
costs seconds; a dashboard refreshing eight tiles must not pay that eight
times. The cache is per-worker, which is fine because each worker is a
separate process with its own pool.

Credentials are encrypted at rest with a key derived from the session secret,
so a database dump does not hand over a customer's warehouse password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import threading
import time
from typing import Any

from twohelixes import config, store
from twohelixes.connectors.base import Connector, ConnectorError
from twohelixes.connectors.document_sources import DOCUMENT_CONNECTORS
from twohelixes.connectors.sql_sources import SQL_CONNECTORS

log = logging.getLogger("twohelixes.connectors.registry")

CONNECTORS: dict[str, type[Connector]] = {**SQL_CONNECTORS, **DOCUMENT_CONNECTORS}

CACHE_TTL = 600.0
_cache: dict[str, tuple[Connector, float]] = {}
_cache_lock = threading.Lock()

# Fields never returned to the client.
SECRET_FIELDS = frozenset(
    {"password", "api_key", "secret", "token", "private_key", "dsn", "url", "credentials"}
)


def kinds() -> list[dict[str, Any]]:
    """Everything the UI can offer, for the 'add a source' picker."""
    out = []
    for kind, cls in sorted(CONNECTORS.items()):
        out.append(
            {
                "kind": kind,
                "display_name": cls.display_name,
                "supports_sql": cls.supports_sql,
                "dialect": getattr(cls, "dialect", "sql"),
                "fields": _fields_for(kind),
            }
        )
    return out


def _fields_for(kind: str) -> list[dict[str, Any]]:
    common = [
        {"name": "host", "label": "Host", "type": "text"},
        {"name": "port", "label": "Port", "type": "number"},
        {"name": "database", "label": "Database", "type": "text"},
        {"name": "user", "label": "User", "type": "text"},
        {"name": "password", "label": "Password", "type": "password"},
    ]
    if kind in ("sqlite", "duckdb"):
        return [{"name": "path", "label": "File path", "type": "text"}]
    if kind == "files":
        return [{"name": "root", "label": "Folder", "type": "text"}]
    if kind == "bigquery":
        return [
            {"name": "project", "label": "Project", "type": "text"},
            {"name": "dataset", "label": "Dataset", "type": "text"},
        ]
    if kind == "snowflake":
        return [
            {"name": "account", "label": "Account", "type": "text"},
            {"name": "user", "label": "User", "type": "text"},
            {"name": "password", "label": "Password", "type": "password"},
            {"name": "database", "label": "Database", "type": "text"},
            {"name": "schema", "label": "Schema", "type": "text"},
            {"name": "warehouse", "label": "Warehouse", "type": "text"},
            {"name": "role", "label": "Role", "type": "text"},
        ]
    if kind in ("mongodb", "elasticsearch"):
        return [
            {"name": "url", "label": "Connection URL", "type": "password"},
            {"name": "database", "label": "Database", "type": "text"},
        ]
    if kind == "http":
        return [
            {"name": "url", "label": "URL", "type": "text"},
            {"name": "records_path", "label": "Records path", "type": "text"},
        ]
    if kind == "trino":
        return common + [
            {"name": "catalog", "label": "Catalog", "type": "text"},
            {"name": "schema", "label": "Schema", "type": "text"},
        ]
    return common


# --------------------------------------------------------------------------
# Credential encryption
# --------------------------------------------------------------------------


def _key() -> bytes:
    return hashlib.sha256(f"connector:{config.session_secret()}".encode()).digest()


def _xor(data: bytes, key: bytes) -> bytes:
    """Keystream from HMAC in counter mode.

    Not authenticated encryption - the threat modelled here is a leaked
    database file, not an attacker who can also write to it. Rows carry an
    HMAC tag so tampering is detected on read.
    """
    out = bytearray()
    counter = 0
    while len(out) < len(data):
        block = hmac.new(key, counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(a ^ b for a, b in zip(data, out[: len(data)]))


def encrypt_config(values: dict[str, Any]) -> str:
    raw = json.dumps(values, default=str).encode()
    key = _key()
    blob = _xor(raw, key)
    tag = hmac.new(key, blob, hashlib.sha256).digest()[:16]
    return base64.b64encode(tag + blob).decode()


def decrypt_config(blob: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(blob)
    except Exception:  # noqa: BLE001
        return {}
    if len(raw) < 16:
        return {}
    tag, body = raw[:16], raw[16:]
    key = _key()
    if not hmac.compare_digest(tag, hmac.new(key, body, hashlib.sha256).digest()[:16]):
        log.error("connector config failed its integrity check")
        return {}
    try:
        return json.loads(_xor(body, key))
    except ValueError:
        return {}


def redact(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: ("********" if key in SECRET_FIELDS and value else value)
        for key, value in values.items()
    }


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def build(kind: str, source_config: dict[str, Any]) -> Connector:
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise ConnectorError(f"unknown source type: {kind}")
    return cls(source_config)


def for_source(source_id: str, user_id: str) -> Connector:
    """Return a live connector for a stored source, from cache when warm."""
    now = time.time()
    with _cache_lock:
        cached = _cache.get(source_id)
        if cached and now - cached[1] < CACHE_TTL:
            return cached[0]

    row = store.one(
        "SELECT * FROM data_sources WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    )
    if row is None:
        raise ConnectorError("no such data source")

    connector = build(row["kind"], decrypt_config(row["config"]))
    if not connector.connect():
        store.execute(
            "UPDATE data_sources SET last_error = ? WHERE id = ?",
            (connector.error[:500], source_id),
        )
        raise ConnectorError(connector.error)

    store.execute(
        "UPDATE data_sources SET last_ok_at = ?, last_error = NULL WHERE id = ?",
        (now, source_id),
    )
    with _cache_lock:
        _cache[source_id] = (connector, now)
    return connector


def invalidate(source_id: str) -> None:
    with _cache_lock:
        entry = _cache.pop(source_id, None)
    if entry:
        try:
            entry[0].close()
        except Exception:  # noqa: BLE001
            pass


def list_sources(user_id: str) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT id, name, kind, folder_id, created_at, last_ok_at, last_error "
        "FROM data_sources WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    out = store.rows_to_dicts(rows)
    for entry in out:
        cls = CONNECTORS.get(entry["kind"])
        entry["display_name"] = cls.display_name if cls else entry["kind"]
        entry["supports_sql"] = cls.supports_sql if cls else False
        entry["dialect"] = getattr(cls, "dialect", "sql") if cls else "sql"
    return out


def create_source(
    user_id: str, name: str, kind: str, source_config: dict[str, Any]
) -> dict[str, Any]:
    if kind not in CONNECTORS:
        raise ConnectorError(f"unknown source type: {kind}")

    connector = build(kind, source_config)
    test = connector.test()
    if not test["ok"]:
        raise ConnectorError(test["error"] or "could not connect")

    source_id = store.new_id()
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO data_sources (id, user_id, name, kind, config, created_at, "
            "updated_at, last_ok_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                user_id,
                name.strip() or CONNECTORS[kind].display_name,
                kind,
                encrypt_config(source_config),
                now,
                now,
                now,
            ),
        )
    return {
        "id": source_id,
        "name": name,
        "kind": kind,
        "folder_id": None,
        "test": test,
    }


def delete_source(user_id: str, source_id: str) -> bool:
    invalidate(source_id)
    row = store.one(
        "SELECT id FROM data_sources WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    )
    if row is None:
        return False
    with store.transaction() as conn:
        conn.execute("DELETE FROM data_sources WHERE id = ?", (source_id,))
    return True
