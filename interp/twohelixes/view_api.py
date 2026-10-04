"""Bounded, tenant-owned saved SQL views and record ingestion.

SQL runs only in an isolated in-memory SQLite database containing `source`.
Definitions are durable; evaluation always reloads their authorized inputs.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

import pandas as pd

MAX_ROWS = 100_000
MAX_COLUMNS = 200
MAX_BYTES = 10 * 1024 * 1024
MAX_DEPTH = 16


class ViewError(Exception):
    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.status = status
        self.code = code


def integer(value: Any, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ViewError("invalid_integer")
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError):
        raise ViewError("invalid_integer") from None
    if str(result) != str(value) or not low <= result <= high:
        raise ViewError("invalid_integer")
    return result


def records_frame(body: Any) -> pd.DataFrame:
    if not isinstance(body, dict):
        raise ViewError("invalid_body")
    rows = body.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        raise ViewError("rows_required_or_too_large")
    columns = list(rows[0]) if isinstance(rows[0], dict) else []
    if not columns or len(columns) > MAX_COLUMNS or any(
        not isinstance(c, str) or not c.strip() or len(c) > 200 or "\x00" in c for c in columns
    ) or len({c.casefold() for c in columns}) != len(columns):
        raise ViewError("invalid_columns")
    expected = set(columns)
    for row in rows:
        if not isinstance(row, dict) or set(row) != expected:
            raise ViewError("inconsistent_row_schema")
        for value in row.values():
            if value is not None and not isinstance(value, (str, int, float, bool)):
                raise ViewError("cells_must_be_scalar")
            if isinstance(value, float) and not math.isfinite(value):
                raise ViewError("non_finite_value")
    try:
        payload = json.dumps(rows, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        raise ViewError("invalid_rows") from None
    if len(payload.encode()) > MAX_BYTES:
        raise ViewError("data_too_large", 413)
    try:
        return pd.DataFrame(rows, columns=columns)
    except (ValueError, OverflowError, TypeError):
        raise ViewError("invalid_rows") from None


def encode_frame(frame: pd.DataFrame) -> str:
    if len(frame) > MAX_ROWS or not 0 < len(frame.columns) <= MAX_COLUMNS:
        raise ViewError("result_too_large", 413)
    columns = [str(c) for c in frame.columns]
    if len({c.casefold() for c in columns}) != len(columns):
        raise ViewError("duplicate_columns")
    payload = frame.to_json(orient="records", date_format="iso", double_precision=15)
    if len(payload.encode()) > MAX_BYTES:
        raise ViewError("result_too_large", 413)
    return payload


def sql_frame(frame: pd.DataFrame, sql: str) -> pd.DataFrame:
    if not isinstance(sql, str) or not sql.strip() or len(sql) > 8000:
        raise ViewError("invalid_sql")
    encode_frame(frame)
    from contextlib import closing
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            frame.to_sql("source", conn, index=False)
        except (ValueError, TypeError, OverflowError, sqlite3.Error):
            raise ViewError("invalid_source_data") from None
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                   sqlite3.SQLITE_RECURSIVE}
        def authorize(action, arg1, arg2, database, trigger):
            if action not in allowed:
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and arg1 != "source":
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_FUNCTION and str(arg2).lower() in {
                "load_extension", "readfile", "writefile", "randomblob", "zeroblob"
            }:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        deadline = time.monotonic() + 3
        instructions = 0
        def progress():
            nonlocal instructions
            instructions += 1000
            return int(instructions > 2_000_000 or time.monotonic() > deadline)
        conn.set_progress_handler(progress, 1000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_BYTES)
        try:
            cursor = conn.execute(sql)
            if cursor.description is None:
                raise ViewError("read_only_sql_required")
            columns = [entry[0] for entry in cursor.description]
            rows = cursor.fetchmany(MAX_ROWS + 1)
            result = pd.DataFrame.from_records(rows, columns=columns)
            encode_frame(result)
            return result
        except sqlite3.Error:
            raise ViewError("invalid_or_unsafe_sql") from None


class ViewStore:
    def __init__(self, path: Path, loader: Callable[[str, dict], pd.DataFrame]):
        self.path = path
        self.loader = loader
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS api_objects (id TEXT PRIMARY KEY, "
                         "owner TEXT NOT NULL, kind TEXT NOT NULL, definition TEXT NOT NULL, "
                         "revision INTEGER NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS api_objects_owner ON api_objects(owner, kind)")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def get(self, owner: str, object_id: str, kind: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM api_objects WHERE id=? AND owner=? AND kind=?",
                               (object_id, owner, kind)).fetchone()
        if row is None:
            raise ViewError("not_found", 404)
        result = dict(row)
        result.update(json.loads(result.pop("definition")))
        result.pop("owner")
        return result

    def list(self, owner: str, offset: int, limit: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM api_objects WHERE owner=? AND kind='view' "
                                "ORDER BY created DESC, id LIMIT ? OFFSET ?", (owner, limit, offset)).fetchall()
        return [self.get(owner, row["id"], "view") for row in rows]

    def delete(self, owner: str, object_id: str, revision: Any) -> dict:
        self.get(owner, object_id, "view")
        revision = integer(revision, -1, 1, 2**31)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            children = conn.execute("SELECT definition FROM api_objects WHERE owner=? AND kind='view'",
                                    (owner,)).fetchall()
            if any(json.loads(row["definition"])["source"].get("view_id") == object_id for row in children):
                raise ViewError("view_in_use", 409)
            cursor = conn.execute("DELETE FROM api_objects WHERE id=? AND owner=? AND kind='view' AND revision=?",
                                  (object_id, owner, revision))
            if cursor.rowcount != 1:
                raise ViewError("revision_conflict", 409)
        return {"deleted": True, "view_id": object_id}

    def save(self, owner: str, kind: str, definition: dict, object_id: str | None = None,
             revision: int | None = None) -> dict:
        now = time.time()
        with self.connect() as conn:
            if object_id:
                cursor = conn.execute("UPDATE api_objects SET definition=?, revision=revision+1, "
                                      "updated=? WHERE id=? AND owner=? AND kind=? AND revision=?",
                                      (json.dumps(definition), now, object_id, owner, kind, revision))
                if cursor.rowcount != 1:
                    raise ViewError("revision_conflict", 409)
            else:
                object_id = ("view_" if kind == "view" else "data_") + uuid.uuid4().hex
                conn.execute("INSERT INTO api_objects VALUES (?, ?, ?, ?, 1, ?, ?)",
                             (object_id, owner, kind, json.dumps(definition), now, now))
        return self.get(owner, object_id, kind)

    def ingest(self, owner: str, body: dict, object_id: str | None = None) -> dict:
        frame = records_frame(body)
        revision = None
        name = body.get("name", "Dataset")
        if object_id:
            previous = self.get(owner, object_id, "data")
            revision = integer(body.get("revision"), -1, 1, 2**31)
            if revision != previous["revision"]:
                raise ViewError("revision_conflict", 409)
            mode = body.get("mode", "replace")
            if mode not in {"replace", "append"}:
                raise ViewError("invalid_ingest_mode")
            if mode == "append":
                if list(frame.columns) != previous["columns"]:
                    raise ViewError("schema_mismatch")
                frame = pd.concat([pd.DataFrame(previous["rows"], columns=previous["columns"]), frame], ignore_index=True)
            name = body.get("name", previous["name"])
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ViewError("invalid_name")
        payload = encode_frame(frame)
        result = self.save(owner, "data", {"name": name, "columns": list(frame.columns),
                           "rows": json.loads(payload), "row_count": len(frame)}, object_id, revision)
        result.pop("rows")
        result["dataset_id"] = result["id"]
        return result

    def source_frame(self, owner: str, source: dict, stack: tuple[str, ...] = ()) -> pd.DataFrame:
        if "view_id" in source:
            return self.evaluate(owner, source["view_id"], stack)
        dataset_id = source.get("dataset_id", "")
        if dataset_id.startswith("data_"):
            item = self.get(owner, dataset_id, "data")
            return pd.DataFrame(item["rows"], columns=item["columns"])
        return self.loader(owner, source)

    def evaluate(self, owner: str, object_id: str, stack: tuple[str, ...] = ()) -> pd.DataFrame:
        if object_id in stack:
            raise ViewError("view_cycle", 409)
        if len(stack) >= MAX_DEPTH:
            raise ViewError("view_depth_exceeded", 409)
        view = self.get(owner, object_id, "view")
        return sql_frame(self.source_frame(owner, view["source"], (*stack, object_id)), view["sql"])

    def define(self, owner: str, body: Any, object_id: str | None = None) -> dict:
        if not isinstance(body, dict):
            raise ViewError("invalid_body")
        previous = self.get(owner, object_id, "view") if object_id else {}
        name = body.get("name", previous.get("name", "View"))
        source = body.get("source", previous.get("source"))
        sql = body.get("sql", previous.get("sql", "SELECT * FROM source"))
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise ViewError("invalid_name")
        if not isinstance(source, dict) or len(source) != 1 or not set(source) <= {"view_id", "dataset_id", "filename"}:
            raise ViewError("invalid_source")
        if any(not isinstance(v, str) or not v or len(v) > 2048 for v in source.values()):
            raise ViewError("invalid_source")
        stack = (object_id or "new_view",)
        frame = sql_frame(self.source_frame(owner, source, stack), sql)
        revision = integer(body.get("revision"), -1, 1, 2**31) if object_id else None
        return self.save(owner, "view", {"name": name, "source": source, "sql": sql,
                         "columns": list(frame.columns)}, object_id, revision)

    def page(self, owner: str, object_id: str, offset: Any = None, limit: Any = None) -> dict:
        offset = integer(offset, 0, 0, MAX_ROWS)
        limit = integer(limit, 100, 1, 2000)
        frame = self.evaluate(owner, object_id)
        rows = json.loads(encode_frame(frame.iloc[offset:offset + limit]))
        return {"view_id": object_id, "columns": list(frame.columns), "rows": rows,
                "row_count": len(frame), "offset": offset, "limit": limit,
                "has_more": offset + len(rows) < len(frame), "evaluation": "live"}
