"""Persistence, over SQLite or PostgreSQL.

`TWOHELIXES_PG_DSN` selects Postgres; without it this is SQLite in WAL mode.
Both are supported on purpose: SQLite is the zero-setup default that makes a
checkout runnable and the test suite fast, and Postgres is what production
runs, because SQLite has **one writer** and this product has four worker
processes plus a thread per streaming job. That single writer is not a
performance detail - it is why signing in could fail with "database is locked"
while an agent job was running.

**The SQL is written once, in SQLite's dialect, and adapted at one point.**
147 call sites use `?` placeholders; `_adapt()` rewrites them to `%s` for
psycopg. Rewriting every call site would have been a worse migration and would
have made a future move to `mojo-db` (which speaks `$1`) another one - the
translation is a dozen lines in one place, and `tests/test_sql_types.py`
prepares every statement against a real Postgres, so a dialect mistake fails
the suite rather than a request.

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
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from twohelixes import config

log = logging.getLogger("twohelixes.store")

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

POSTGRES = "postgres"
SQLITE = "sqlite"


def dsn() -> str:
    return config.get("TWOHELIXES_PG_DSN") or ""


def backend() -> str:
    return POSTGRES if dsn() else SQLITE


def is_postgres() -> bool:
    return backend() == POSTGRES

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
    is_admin        INTEGER NOT NULL DEFAULT 0,
    -- The allowance period: when it started, and what has been used in it.
    -- Rolling rather than calendar, so subscribing on the 31st is not a
    -- shorter first month than subscribing on the 1st.
    plan_period_start REAL,
    plan_usage      TEXT,
    stripe_subscription TEXT,
    -- Thousandths of a credit left over from discounted pricing, carried
    -- forward so a volume discount is exact rather than rounded away.
    credit_dust     INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS folders (
    id          TEXT PRIMARY KEY,
    user_id     TEXT,
    team_id     TEXT,
    parent_id   TEXT,
    name        TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS folders_user ON folders(user_id);
CREATE INDEX IF NOT EXISTS folders_team ON folders(team_id);
CREATE INDEX IF NOT EXISTS folders_parent ON folders(parent_id);

CREATE TABLE IF NOT EXISTS data_sources (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    folder_id   TEXT,
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
    folder_id   TEXT,
    name        TEXT NOT NULL,
    description TEXT,
    columns     TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0,
    storage     TEXT NOT NULL,
    raw_storage TEXT,
    shape_report TEXT,
    sheet_name  TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL
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
    -- The rows the chart was drawn from, when they came from an upload or an
    -- inline payload rather than a source we can query again. Without them a
    -- manual edit - the free, no-model path the product advertises - has
    -- nothing to rebuild from and fails with "re-run the query".
    rows_json    TEXT,
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

-- Per-minute billing for work that outlives a request. In SQLite rather than
-- in a worker's memory for the same reason the rate counters are: four
-- SO_REUSEPORT workers have to share one view of what is running, and a meter
-- that disappears with a worker restart leaves a cloud instance billing us
-- with nobody watching.
CREATE TABLE IF NOT EXISTS meters (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    ref          TEXT NOT NULL,
    rate         INTEGER NOT NULL,
    provider     TEXT NOT NULL DEFAULT 'local',
    remote       TEXT,
    detail       TEXT,
    owner        TEXT,
    status       TEXT NOT NULL,
    started_at   REAL NOT NULL,
    paid_through REAL NOT NULL,
    last_seen    REAL NOT NULL,
    closed_at    REAL,
    charged      INTEGER NOT NULL DEFAULT 0,
    refunded     INTEGER NOT NULL DEFAULT 0,
    minutes      INTEGER NOT NULL DEFAULT 0,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS meters_user ON meters(user_id, started_at);
CREATE INDEX IF NOT EXISTS meters_open ON meters(status, last_seen);

CREATE TABLE IF NOT EXISTS rate_events (
    identity    TEXT NOT NULL,
    bucket      TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_events_lookup ON rate_events(identity, bucket, created_at);

CREATE TABLE IF NOT EXISTS analytics_sites (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    domain      TEXT NOT NULL,
    name        TEXT NOT NULL,
    write_key   TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    UNIQUE (user_id, domain)
);
CREATE INDEX IF NOT EXISTS analytics_sites_user ON analytics_sites(user_id, created_at);

-- Site analytics. One row per event, GA-shaped: the identity triple
-- (site/client/session), the page it happened on, the acquisition fields, and
-- a JSON bag for everything specific to the event.
CREATE TABLE IF NOT EXISTS analytics_events (
    id            TEXT PRIMARY KEY,
    site_id       TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    user_id       TEXT,
    event_name    TEXT NOT NULL,
    ts            REAL NOT NULL,
    received_at   REAL NOT NULL,
    page_location TEXT,
    page_path     TEXT,
    page_title    TEXT,
    referrer      TEXT,
    referrer_host TEXT,
    utm_source    TEXT,
    utm_medium    TEXT,
    utm_campaign  TEXT,
    utm_term      TEXT,
    utm_content   TEXT,
    device        TEXT,
    browser       TEXT,
    os            TEXT,
    screen        TEXT,
    viewport      TEXT,
    language      TEXT,
    country       TEXT,
    ip_hash       TEXT,
    engagement_ms INTEGER NOT NULL DEFAULT 0,
    props         TEXT
);
CREATE INDEX IF NOT EXISTS analytics_events_site_ts ON analytics_events(site_id, ts);
CREATE INDEX IF NOT EXISTS analytics_events_session ON analytics_events(site_id, session_id, ts);
CREATE INDEX IF NOT EXISTS analytics_events_name ON analytics_events(site_id, event_name, ts);
CREATE INDEX IF NOT EXISTS analytics_events_client ON analytics_events(site_id, client_id, ts);

-- Identity stitching for the Segment-style identify/alias calls.
CREATE TABLE IF NOT EXISTS analytics_identities (
    site_id     TEXT NOT NULL,
    client_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    traits      TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    PRIMARY KEY (site_id, client_id)
);
CREATE INDEX IF NOT EXISTS analytics_identities_user ON analytics_identities(site_id, user_id);

CREATE TABLE IF NOT EXISTS analytics_event_dashboards (
    site_id       TEXT NOT NULL,
    event_name    TEXT NOT NULL,
    dashboard_id  TEXT,
    status         TEXT NOT NULL,
    claimed_at     REAL NOT NULL,
    created_at     REAL,
    PRIMARY KEY (site_id, event_name)
);
CREATE INDEX IF NOT EXISTS analytics_event_dashboards_status
    ON analytics_event_dashboards(status, claimed_at);
"""


def _db_path() -> str:
    return str(config.data_dir() / "twohelixes.db")


# --------------------------------------------------------------------------
# Dialect
# --------------------------------------------------------------------------


class _Connection:
    """A driver connection that speaks this codebase's dialect.

    Every `conn.execute(...)` in the tree - including the ~40 inside
    `transaction()` blocks, which never pass through `store.query()` - is
    written with `?` placeholders. Wrapping the connection rather than
    rewriting those call sites keeps the dialect in exactly one place, which
    is the property that makes a third backend (`mojo-db`, `$1`) a change to
    `_adapt` rather than another migration.

    Everything not listed here forwards to the driver untouched.
    """

    __slots__ = ("raw",)

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, params: Iterable[Any] = ()) -> Any:
        return self.raw.execute(_adapt(sql), tuple(params))

    def executescript(self, script: str) -> None:
        """SQLite has this; psycopg does not. Both need it."""
        if is_postgres():
            for statement in _statements(script):
                self.raw.execute(_adapt(statement))
            return
        self.raw.executescript(script)

    def raw_execute(self, sql: str) -> Any:
        """For statements that are already in the backend's own dialect."""
        return self.raw.execute(sql)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


def _in_transaction(conn: Any) -> bool:
    raw = getattr(conn, "raw", conn)
    if is_postgres():
        import psycopg

        return raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE
    return bool(raw.in_transaction)


def _connect_postgres() -> Any:
    """One psycopg connection, autocommit, dict rows.

    Autocommit to match SQLite's `isolation_level=None`: `transaction()` is
    the only thing that opens one, and every other statement stands alone.
    Without this psycopg opens an implicit transaction on the first statement
    and holds it until something commits - which is the exact failure mode
    this migration exists to remove, reintroduced by the driver.
    """
    import psycopg
    from psycopg.rows import dict_row

    conn = psycopg.connect(dsn(), autocommit=True, row_factory=dict_row)
    # A statement that runs long enough to matter is a bug; failing beats
    # holding a lock while a caller waits for a request that already timed out.
    conn.execute("SET statement_timeout = '30s'")
    conn.execute("SET idle_in_transaction_session_timeout = '60s'")
    return conn


def _adapt(sql: str) -> str:
    """Rewrite SQLite-dialect SQL for psycopg.

    Two differences and no more, which is why one function can do this:

    * placeholders - `?` becomes `%s`. Question marks inside string literals
      are left alone, because `WHERE note LIKE '%?%'` is not a parameter.
    * a literal `%` has to be doubled for psycopg's own formatting.

    Everything else in this codebase is already portable: the survey before
    the migration found exactly one `INSERT OR REPLACE` in the whole tree.
    """
    if not is_postgres():
        return sql

    out: list[str] = []
    quote: str | None = None
    for char in sql:
        if quote:
            if char == quote:
                quote = None
            out.append(char)
            continue
        if char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        elif char == "%":
            out.append("%%")
        else:
            out.append(char)
    return "".join(out)


def _postgres_schema() -> str:
    """The same schema, in Postgres types.

    Translated rather than maintained separately: two schemas drift, and the
    drift shows up as a column that exists in development and not in
    production. `REAL` means float32 in Postgres and float64 in SQLite, and
    every one of these holds a unix timestamp, so they become
    `DOUBLE PRECISION` - a float32 timestamp is accurate to about a minute in
    2026, which would have quietly broken every "created_at" comparison.
    """
    schema = SCHEMA
    for sqlite_type, postgres_type in (
        ("REAL", "DOUBLE PRECISION"),
        ("INTEGER", "BIGINT"),
    ):
        schema = schema.replace(f" {sqlite_type} ", f" {postgres_type} ")
        schema = schema.replace(f" {sqlite_type},", f" {postgres_type},")
        schema = schema.replace(f" {sqlite_type}\n", f" {postgres_type}\n")
    return schema


def connection() -> Any:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        # A connection handed back mid-transaction means the last operation on
        # this thread neither committed nor rolled back - which under SQLite's
        # single writer holds up every other write on the box. `transaction()`
        # makes that impossible; this catches anything that still manages it,
        # including a thread killed inside its own except clause.
        if _in_transaction(conn):
            log.warning("connection was left in a transaction; rolling back")
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001 - two drivers, two exception trees
                log.exception("could not roll back a leaked transaction")
        return conn

    if is_postgres():
        conn = _Connection(_connect_postgres())
        _local.conn = conn
        return conn

    conn = sqlite3.connect(_db_path(), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # busy_timeout FIRST. `PRAGMA journal_mode=WAL` takes a brief exclusive
    # lock, and with no busy timeout in force yet it fails outright rather than
    # waiting: under a concurrent writer - an agent job checkpointing its
    # progress, say - every *new* connection died with "database is locked",
    # which surfaced as a 500 on something as innocent as signing in. The
    # `timeout=` argument to connect() does not cover it, because pysqlite
    # applies it as a busy handler only after the connection is set up.
    conn.execute("PRAGMA busy_timeout=30000")
    # Only set the journal mode when it is not already WAL. Switching modes
    # needs a brief exclusive lock, and against a database an agent job is
    # writing to continuously that lock is not always available - so a *new
    # connection*, which is what a request on a new worker thread makes, could
    # fail with "database is locked" and surface as a 500 on signing in. Read
    # it first: on an existing database the answer is already "wal" and there
    # is nothing to take a lock for.
    current = conn.execute("PRAGMA journal_mode").fetchone()
    if not current or str(current[0]).lower() != "wal":
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    wrapped = _Connection(conn)
    _local.conn = wrapped
    return wrapped


def close() -> None:
    """Close this thread's connection and forget it.

    SQLite connections are nearly free and a leaked one costs a file handle.
    A Postgres connection is a server-side process holding whatever locks its
    transaction took, so one that is dropped without being closed keeps them
    until the garbage collector gets to it - long enough for the next
    statement to hit `statement_timeout` and report something that looks
    nothing like the cause.

    Under CPython a thread's locals are released when the thread ends, so the
    normal path closes itself; this is for the cases that are not normal, like
    a test that replaces the thread-local store underneath a live connection.
    """
    conn = getattr(_local, "conn", None)
    if conn is None:
        return
    try:
        conn.close()
    except Exception:  # noqa: BLE001 - two drivers, two exception trees
        log.debug("could not close the connection", exc_info=True)
    _local.conn = None


def init() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        conn = connection()
        conn.executescript(_postgres_schema() if is_postgres() else SCHEMA)
        _add_missing_columns(conn)
        _initialised = True
        log.info("store ready on %s", "postgres" if is_postgres() else _db_path())


def _statements(script: str) -> list[str]:
    """Split a DDL script on semicolons that end a statement.

    Naive on purpose: this only ever sees `SCHEMA`, which has no semicolons
    inside literals or function bodies. A general SQL splitter here would be
    a parser nobody asked for.
    """
    return [part.strip() for part in script.split(";") if part.strip()]


# Columns added after a table shipped. `CREATE TABLE IF NOT EXISTS` does
# nothing for a table that already exists, so a new column has to be added
# explicitly or an upgraded server queries a column its database does not have.
_ADDED_COLUMNS = (
    ("charts", "rows_json", "TEXT"),
    ("charts", "cost_micros", "INTEGER"),
    ("users", "plan_period_start", "REAL"),
    ("users", "plan_usage", "TEXT"),
    ("users", "stripe_subscription", "TEXT"),
    ("users", "credit_dust", "INTEGER"),
    ("jobs", "cost_micros", "INTEGER"),
    ("data_sources", "folder_id", "TEXT"),
    ("datasets", "folder_id", "TEXT"),
    ("datasets", "raw_storage", "TEXT"),
    ("datasets", "shape_report", "TEXT"),
    ("datasets", "sheet_name", "TEXT"),
    ("datasets", "updated_at", "REAL"),
)


def _add_missing_columns(conn: Any) -> None:
    for table, column, kind in _ADDED_COLUMNS:
        if is_postgres():
            # Postgres has had this since 9.6 and it is race-free, which
            # matters: four workers run init() at once on a cold box.
            postgres_kind = {"REAL": "DOUBLE PRECISION", "INTEGER": "BIGINT"}.get(
                kind, kind
            )
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {postgres_kind}"
            )
            continue
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            log.info("adding %s.%s", table, column)
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def new_id() -> str:
    return uuid.uuid4().hex


# The longest a write transaction may reasonably take. Everything that opens
# one here is a handful of statements against indexed rows; a second means
# something slow got inside it, which under SQLite's single writer means every
# other write on the box is queued behind it.
SLOW_TRANSACTION_SECONDS = 1.0


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """The only supported way to open a write transaction.

    Hand-written `BEGIN IMMEDIATE` / `COMMIT` pairs are how this went wrong.
    SQLite has one writer, so a transaction held open blocks every other write
    for as long as it lasts - and a thread can stop making progress inside one
    without raising: the sandbox watchdog kills a runaway with
    `PyThreadState_SetAsyncExc`, which **does not interrupt a blocking call**,
    so a thread sleeping or waiting on a socket between BEGIN and COMMIT holds
    the write lock until that call returns on its own. Signing in then fails
    with "database is locked" after its full busy timeout, which is how this
    surfaced: a 500 on the least interesting endpoint in the product, caused by
    an agent thread somewhere else entirely.

    So: `finally`, always, and a warning if one is held long enough to be
    suspicious. Nothing slow - no model call, no HTTP, no sandbox run - goes
    inside one of these.
    """
    conn = connection()
    started = time.time()
    # IMMEDIATE takes SQLite's single write lock up front rather than on the
    # first write, so two writers cannot both read, then both try to upgrade,
    # and one get SQLITE_BUSY halfway through. Postgres takes row locks as it
    # goes and has no equivalent - a plain BEGIN is the same guarantee there.
    conn.execute("BEGIN" if is_postgres() else "BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # BaseException, not Exception: the watchdog raises `Timeout` and a
        # cancelled thread can see KeyboardInterrupt/SystemExit, and those are
        # exactly the cases that used to leave the lock held.
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - two drivers, two exception trees
            log.exception("could not roll back")
        raise
    else:
        conn.execute("COMMIT")
    finally:
        held = time.time() - started
        if held > SLOW_TRANSACTION_SECONDS:
            # Loud, with a stack, because the fix is always "move the slow
            # thing out of the transaction" and the stack says which thing.
            log.warning(
                "write transaction held for %.1fs - every other writer waited "
                "behind it", held, stack_info=True,
            )


# These do *not* call `_adapt` themselves: the connection they get back is a
# `_Connection`, which adapts on the way through. Adapting twice turns `?`
# into `%s` and then into the escaped literal `%%s`, and psycopg reports
# "the query has 0 placeholders" - which reads like a bug in the SQL.
def query(sql: str, params: Iterable[Any] = ()) -> list[Any]:
    return list(connection().execute(sql, tuple(params)))


def one(sql: str, params: Iterable[Any] = ()) -> Any:
    return connection().execute(sql, tuple(params)).fetchone()


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    connection().execute(sql, tuple(params))


def row_to_dict(row: Any) -> dict[str, Any] | None:
    """A row as a plain dict, whichever driver produced it.

    psycopg's `dict_row` already gives a dict; `sqlite3.Row` gives a mapping
    with `.keys()` and no `.items()`. Callers should not have to know which,
    so this is the one place that does.
    """
    if row is None:
        return None
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def rows_to_dicts(rows: Iterable[Any]) -> list[dict[str, Any]]:
    return [converted for row in rows if (converted := row_to_dict(row)) is not None]


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
    now = time.time()
    # The allowance period starts at sign-up, not at first use: "renews on the
    # 14th" is a promise someone can plan around, "renews thirty days after
    # whenever you first got round to asking something" is not.
    execute(
        "INSERT INTO users (id, email, display_name, created_at, plan, "
        "plan_period_start, plan_usage) VALUES (?, ?, ?, ?, 'free', ?, '{}')",
        (user_id, email, display_name or email.split("@")[0], now, now),
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
