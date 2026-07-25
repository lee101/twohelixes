"""Persistence.

SQLite in WAL mode by default: the workload is many small reads plus modest
writes, and WAL keeps readers from blocking the writer. Set TWOHELIXES_PG_DSN
to move to Postgres; the query surface here is intentionally plain SQL so that
switch stays cheap.

Connections are per-thread. Stream jobs run on their own threads, so a shared
connection would need locking on every call for no benefit.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from twohelixes import config

log = logging.getLogger("twohelixes.store")

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT UNIQUE,
    display_name    TEXT,
    created_at      REAL NOT NULL,
    plan            TEXT NOT NULL DEFAULT 'free',
    stripe_id       TEXT,
    api_credits     INTEGER NOT NULL DEFAULT 0,
    free_queries_used INTEGER NOT NULL DEFAULT 0,
    is_admin        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS users_stripe ON users(stripe_id);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS data_sources (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    config      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    last_ok_at  REAL,
    last_error  TEXT
);
CREATE INDEX IF NOT EXISTS data_sources_user ON data_sources(user_id);

CREATE TABLE IF NOT EXISTS datasets (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    source_id   TEXT,
    name        TEXT NOT NULL,
    description TEXT,
    columns     TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0,
    storage     TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS datasets_user ON datasets(user_id);

CREATE TABLE IF NOT EXISTS dashboards (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    layout      TEXT NOT NULL,
    is_public   INTEGER NOT NULL DEFAULT 0,
    share_token TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS dashboards_user ON dashboards(user_id);
CREATE INDEX IF NOT EXISTS dashboards_share ON dashboards(share_token);

CREATE TABLE IF NOT EXISTS charts (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    dashboard_id TEXT,
    title        TEXT NOT NULL,
    query        TEXT,
    source_id    TEXT,
    spec         TEXT NOT NULL,
    graph_args   TEXT NOT NULL,
    trace        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS charts_user ON charts(user_id);
CREATE INDEX IF NOT EXISTS charts_dashboard ON charts(dashboard_id);

CREATE TABLE IF NOT EXISTS saved_queries (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    source_id   TEXT,
    name        TEXT NOT NULL,
    sql         TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    description TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    run_count   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS saved_queries_user ON saved_queries(user_id);

CREATE TABLE IF NOT EXISTS query_history (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    source_id   TEXT,
    sql         TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    row_count   INTEGER,
    duration_ms INTEGER,
    error       TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS query_history_user ON query_history(user_id, created_at);

CREATE TABLE IF NOT EXISTS credit_ledger (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    delta       INTEGER NOT NULL,
    balance     INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    ref         TEXT,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS credit_ledger_user ON credit_ledger(user_id, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    status      TEXT NOT NULL,
    progress    TEXT,
    result      TEXT,
    error       TEXT,
    credits_spent INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_user ON jobs(user_id, created_at);

CREATE TABLE IF NOT EXISTS rate_events (
    identity    TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_lookup ON rate_events(identity, bucket, created_at);
"""


def _db_path() -> str:
    return str(config.data_dir() / "twohelixes.db")


def connection() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        return conn
    conn = sqlite3.connect(_db_path(), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    _local.conn = conn
    return conn


def init() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        conn = connection()
        conn.executescript(SCHEMA)
        _initialised = True
        log.info("store ready at %s", _db_path())


def new_id() -> str:
    return uuid.uuid4().hex


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(connection().execute(sql, tuple(params)))


def one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = connection().execute(sql, tuple(params)).fetchone()
    return rows


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    connection().execute(sql, tuple(params))


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{key: row[key] for key in row.keys()} for row in rows]


def load_json(value: Any, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def dump_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


# --------------------------------------------------------------------------
# Users
# --------------------------------------------------------------------------


def get_user(user_id: str) -> dict[str, Any] | None:
    return row_to_dict(one("SELECT * FROM users WHERE id = ?", (user_id,)))


def get_user_by_email(email: str) -> dict[str, Any] | None:
    return row_to_dict(
        one("SELECT * FROM users WHERE email = ?", (email.strip().lower(),))
    )


def create_user(email: str, display_name: str = "") -> dict[str, Any]:
    email = email.strip().lower()
    existing = get_user_by_email(email)
    if existing:
        return existing
    user_id = new_id()
    execute(
        "INSERT INTO users (id, email, display_name, created_at, plan) "
        "VALUES (?, ?, ?, ?, 'free')",
        (user_id, email, display_name or email.split("@")[0], time.time()),
    )
    return get_user(user_id)  # type: ignore[return-value]


def touch_user(user_id: str, **fields: Any) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    execute(
        f"UPDATE users SET {assignments} WHERE id = ?",
        (*fields.values(), user_id),
    )
