"""Connector interface shared by every data source.

A connector answers four questions: can I connect, what is in here, run this
query, and give me a dataframe. Everything above this layer (the SQL editor,
the pipeline's search stage, the dashboard runtime) is written against these
four and nothing else, which is why adding a warehouse is a single file.

Read-only by construction: `execute` refuses anything that is not a SELECT or
a CTE, so a generated query can never mutate a customer's warehouse.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("twohelixes.connectors")

DEFAULT_ROW_LIMIT = 10_000
DEFAULT_TIMEOUT = 60

# Anything that is not a read. Checked on the first statement keyword, after
# stripping comments, and again for stacked statements.
WRITE_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "truncate",
        "alter",
        "create",
        "replace",
        "grant",
        "revoke",
        "merge",
        "call",
        "execute",
        "copy",
        "vacuum",
        "attach",
        "detach",
        "pragma",
    }
)

COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
COMMENT_LINE = re.compile(r"--[^\n]*")


class ConnectorError(Exception):
    pass


class UnsafeQuery(ConnectorError):
    pass


@dataclass
class Column:
    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "nullable": self.nullable,
            "primary_key": self.primary_key,
            "comment": self.comment,
        }


@dataclass
class Table:
    name: str
    schema: str = ""
    columns: list[Column] = field(default_factory=list)
    row_estimate: int | None = None
    comment: str = ""

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}" if self.schema else self.name

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "schema": self.schema,
            "qualified": self.qualified,
            "columns": [c.to_dict() for c in self.columns],
            "row_estimate": self.row_estimate,
            "comment": self.comment,
        }


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    duration_ms: int = 0
    truncated: bool = False
    sql: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_frame(self) -> Any:
        import pandas as pd

        return pd.DataFrame(self.rows, columns=self.columns)

    def to_dict(self, max_rows: int = 1000) -> dict[str, Any]:
        return {
            "columns": self.columns,
            "rows": self.rows[:max_rows],
            "row_count": self.row_count,
            "duration_ms": self.duration_ms,
            "truncated": self.truncated or self.row_count > max_rows,
        }


def strip_comments(sql: str) -> str:
    return COMMENT_LINE.sub(" ", COMMENT_BLOCK.sub(" ", sql)).strip()


def assert_read_only(sql: str) -> None:
    """Reject writes and stacked statements before they reach a driver."""
    cleaned = strip_comments(sql)
    if not cleaned:
        raise UnsafeQuery("empty query")

    # Stacked statements: allow one trailing semicolon, nothing after it.
    statements = [s for s in cleaned.split(";") if s.strip()]
    if len(statements) > 1:
        raise UnsafeQuery("multiple statements are not allowed")

    first = statements[0].lstrip("( \t\n")
    keyword = re.split(r"\s|\(", first, maxsplit=1)[0].lower()
    if keyword in WRITE_KEYWORDS:
        raise UnsafeQuery(f"{keyword.upper()} is not allowed; queries are read-only")
    if keyword not in ("select", "with", "show", "describe", "explain", "table"):
        raise UnsafeQuery(f"unsupported statement: {keyword.upper()}")


def apply_limit(sql: str, limit: int, syntax: str = "LIMIT {limit}") -> str:
    """Bound a query that did not bound itself."""
    cleaned = strip_comments(sql).rstrip(";")
    if re.search(r"\blimit\s+\d+", cleaned, re.IGNORECASE):
        return cleaned
    if re.search(r"\btop\s+\d+", cleaned, re.IGNORECASE):
        return cleaned
    if re.search(r"\bfetch\s+first\b", cleaned, re.IGNORECASE):
        return cleaned
    return f"{cleaned} {syntax.format(limit=limit)}"


class Connector:
    """Base class. Subclasses implement `_connect`, `tables` and `execute`."""

    kind = "base"
    display_name = "Data source"
    # Filled in by SQL subclasses; used by the query generator's prompt.
    dialect = "sql"
    limit_syntax = "LIMIT {limit}"
    supports_sql = True

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.error: str = ""
        self._connected = False

    # -- lifecycle ---------------------------------------------------------

    def connect(self) -> bool:
        try:
            self._connect()
            self._connected = True
            self.error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - driver errors are varied
            self.error = str(exc)
            log.warning("%s connect failed: %s", self.kind, exc)
            return False

    def _connect(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        self._connected = False

    def test(self) -> dict[str, Any]:
        started = time.time()
        ok = self.connect()
        return {
            "ok": ok,
            "error": self.error,
            "duration_ms": int((time.time() - started) * 1000),
            "kind": self.kind,
        }

    # -- introspection -----------------------------------------------------

    def tables(self) -> list[Table]:
        raise NotImplementedError

    def schema_summary(self, max_tables: int = 60, max_columns: int = 40) -> str:
        """A compact schema rendering for prompts."""
        lines: list[str] = []
        for table in self.tables()[:max_tables]:
            columns = ", ".join(
                f"{c.name} {c.type}" for c in table.columns[:max_columns]
            )
            suffix = "" if len(table.columns) <= max_columns else ", ..."
            lines.append(f"{table.qualified}({columns}{suffix})")
        return "\n".join(lines)

    # -- reading -----------------------------------------------------------

    def execute(
        self, sql: str, limit: int = DEFAULT_ROW_LIMIT, timeout: int = DEFAULT_TIMEOUT
    ) -> QueryResult:
        raise NotImplementedError

    def frame(self, sql: str, limit: int = DEFAULT_ROW_LIMIT) -> Any:
        return self.execute(sql, limit=limit).to_frame()

    def sample(self, table: str, rows: int = 200) -> QueryResult:
        return self.execute(f"SELECT * FROM {table}", limit=rows)
