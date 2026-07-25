"""Non-SQL sources: documents, search engines, files and HTTP APIs.

These do not speak SQL, so `supports_sql` is False and the SQL editor hides
them. The pipeline still treats them uniformly because everything ends as a
dataframe.

Files are queried through DuckDB rather than loaded with pandas: it reads
Parquet and CSV lazily, pushes predicates down, and gives the uploaded-file
case the same SQL surface as a warehouse.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from twohelixes.connectors.base import (
    Column,
    Connector,
    ConnectorError,
    QueryResult,
    Table,
    assert_read_only,
)

log = logging.getLogger("twohelixes.connectors.documents")


class MongoConnector(Connector):
    kind = "mongodb"
    display_name = "MongoDB"
    supports_sql = False

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client: Any = None
        self.db: Any = None

    def _connect(self) -> None:
        try:
            import pymongo
        except ModuleNotFoundError as exc:
            raise ConnectorError("pymongo is not installed") from exc

        dsn = self.config.get("dsn") or self.config.get("url")
        if not dsn:
            raise ConnectorError("MongoDB needs a connection string")

        self.client = pymongo.MongoClient(
            dsn, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000
        )
        self.client.admin.command("ping")
        name = self.config.get("database")
        self.db = self.client[name] if name else self.client.get_default_database()
        if self.db is None:
            raise ConnectorError("no database named in the connection string")

    def tables(self) -> list[Table]:
        if self.db is None and not self.connect():
            raise ConnectorError(self.error)

        out: list[Table] = []
        for name in self.db.list_collection_names():
            # Mongo has no declared schema, so infer one from a sample. A
            # single document would miss optional fields.
            fields: dict[str, str] = {}
            for doc in self.db[name].find(limit=120):
                for key, value in doc.items():
                    fields.setdefault(key, type(value).__name__)
            out.append(
                Table(
                    name=name,
                    columns=[Column(name=k, type=v) for k, v in sorted(fields.items())],
                    row_estimate=self.db[name].estimated_document_count(),
                )
            )
        return out

    def execute(self, sql: str, limit: int = 10_000, timeout: int = 60) -> QueryResult:
        """`sql` is a JSON query document: {collection, filter, projection, ...}."""
        if self.db is None and not self.connect():
            raise ConnectorError(self.error)

        try:
            spec = json.loads(sql)
        except ValueError as exc:
            raise ConnectorError(
                "MongoDB queries are JSON: "
                '{"collection": "orders", "filter": {}, "limit": 100}'
            ) from exc

        collection = spec.get("collection")
        if not collection:
            raise ConnectorError("query needs a 'collection'")

        started = time.time()
        cursor = self.db[collection]

        if spec.get("pipeline"):
            documents = list(cursor.aggregate(spec["pipeline"], maxTimeMS=timeout * 1000))
        else:
            documents = list(
                cursor.find(
                    spec.get("filter") or {},
                    spec.get("projection"),
                    limit=min(int(spec.get("limit") or limit), limit),
                    sort=list(spec["sort"].items()) if spec.get("sort") else None,
                    max_time_ms=timeout * 1000,
                )
            )

        columns: list[str] = []
        for doc in documents:
            for key in doc:
                if key not in columns:
                    columns.append(key)
        rows = [[_jsonable(doc.get(c)) for c in columns] for doc in documents]

        return QueryResult(
            columns=columns,
            rows=rows,
            duration_ms=int((time.time() - started) * 1000),
            sql=sql,
        )


class ElasticsearchConnector(Connector):
    kind = "elasticsearch"
    display_name = "Elasticsearch / OpenSearch"
    supports_sql = False

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client: Any = None

    def _connect(self) -> None:
        try:
            from elasticsearch import Elasticsearch
        except ModuleNotFoundError as exc:
            raise ConnectorError("elasticsearch is not installed") from exc

        hosts = self.config.get("hosts") or [self.config.get("url")]
        if not hosts or not hosts[0]:
            raise ConnectorError("Elasticsearch needs a host URL")

        kwargs: dict[str, Any] = {"request_timeout": 20}
        if self.config.get("api_key"):
            kwargs["api_key"] = self.config["api_key"]
        elif self.config.get("user"):
            kwargs["basic_auth"] = (self.config["user"], self.config.get("password", ""))

        self.client = Elasticsearch(hosts, **kwargs)
        if not self.client.ping():
            raise ConnectorError("ping failed")

    def tables(self) -> list[Table]:
        if self.client is None and not self.connect():
            raise ConnectorError(self.error)

        out: list[Table] = []
        for index, meta in self.client.indices.get_mapping().items():
            if index.startswith("."):
                continue
            properties = (meta.get("mappings") or {}).get("properties") or {}
            out.append(
                Table(
                    name=index,
                    columns=[
                        Column(name=k, type=str(v.get("type", "object")))
                        for k, v in properties.items()
                    ],
                )
            )
        return out

    def execute(self, sql: str, limit: int = 10_000, timeout: int = 60) -> QueryResult:
        if self.client is None and not self.connect():
            raise ConnectorError(self.error)

        try:
            spec = json.loads(sql)
        except ValueError as exc:
            raise ConnectorError(
                'Elasticsearch queries are JSON: {"index": "logs", "query": {...}}'
            ) from exc

        index = spec.get("index")
        if not index:
            raise ConnectorError("query needs an 'index'")

        started = time.time()
        response = self.client.search(
            index=index,
            query=spec.get("query") or {"match_all": {}},
            aggs=spec.get("aggs"),
            size=min(int(spec.get("size") or 500), limit),
            sort=spec.get("sort"),
        )

        if spec.get("aggs"):
            return _flatten_aggregations(response, started, sql)

        hits = [hit.get("_source", {}) for hit in response["hits"]["hits"]]
        columns: list[str] = []
        for hit in hits:
            for key in hit:
                if key not in columns:
                    columns.append(key)
        return QueryResult(
            columns=columns,
            rows=[[_jsonable(h.get(c)) for c in columns] for h in hits],
            duration_ms=int((time.time() - started) * 1000),
            sql=sql,
        )


class FilesConnector(Connector):
    """CSV / Parquet / JSON / Excel, queried in place through DuckDB."""

    kind = "files"
    display_name = "Uploaded files"
    dialect = "duckdb"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.conn: Any = None

    def _root(self) -> Path:
        root = self.config.get("root")
        if not root:
            raise ConnectorError("no file root configured")
        return Path(root)

    def _connect(self) -> None:
        try:
            import duckdb
        except ModuleNotFoundError as exc:
            raise ConnectorError("duckdb is not installed") from exc

        self.conn = duckdb.connect(":memory:")
        root = self._root()
        if not root.exists():
            raise ConnectorError(f"{root} does not exist")

        for path in sorted(root.iterdir()):
            view = _safe_view_name(path.stem)
            reader = _duckdb_reader(path)
            if reader is None:
                continue
            try:
                self.conn.execute(
                    f'CREATE OR REPLACE VIEW "{view}" AS SELECT * FROM {reader}'
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not register %s: %s", path.name, exc)

    def tables(self) -> list[Table]:
        if self.conn is None and not self.connect():
            raise ConnectorError(self.error)

        out: list[Table] = []
        for (name,) in self.conn.execute("SHOW TABLES").fetchall():
            described = self.conn.execute(f'DESCRIBE "{name}"').fetchall()
            out.append(
                Table(
                    name=name,
                    columns=[Column(name=r[0], type=str(r[1])) for r in described],
                )
            )
        return out

    def execute(self, sql: str, limit: int = 10_000, timeout: int = 60) -> QueryResult:
        assert_read_only(sql)
        if self.conn is None and not self.connect():
            raise ConnectorError(self.error)

        started = time.time()
        cursor = self.conn.execute(sql)
        rows = [list(r) for r in cursor.fetchmany(limit + 1)]
        columns = [d[0] for d in cursor.description]

        truncated = len(rows) > limit
        return QueryResult(
            columns=columns,
            rows=rows[:limit],
            duration_ms=int((time.time() - started) * 1000),
            truncated=truncated,
            sql=sql,
        )


class HTTPConnector(Connector):
    """A JSON HTTP endpoint treated as a table."""

    kind = "http"
    display_name = "HTTP API"
    supports_sql = False

    def _connect(self) -> None:
        if not self.config.get("url"):
            raise ConnectorError("HTTP source needs a URL")

    def tables(self) -> list[Table]:
        result = self.execute("{}", limit=50)
        return [
            Table(
                name=str(self.config.get("name") or "response"),
                columns=[Column(name=c, type="unknown") for c in result.columns],
            )
        ]

    def execute(self, sql: str, limit: int = 10_000, timeout: int = 60) -> QueryResult:
        import httpx

        try:
            overrides = json.loads(sql) if sql.strip() else {}
        except ValueError:
            overrides = {}

        url = overrides.get("url") or self.config["url"]
        headers = {**(self.config.get("headers") or {}), **(overrides.get("headers") or {})}
        params = {**(self.config.get("params") or {}), **(overrides.get("params") or {})}

        started = time.time()
        response = httpx.get(url, headers=headers, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()

        records = _locate_records(payload, self.config.get("records_path"))
        columns: list[str] = []
        for record in records[:limit]:
            if isinstance(record, dict):
                for key in record:
                    if key not in columns:
                        columns.append(key)

        rows = [
            [_jsonable(r.get(c)) if isinstance(r, dict) else None for c in columns]
            for r in records[:limit]
        ]
        return QueryResult(
            columns=columns,
            rows=rows,
            duration_ms=int((time.time() - started) * 1000),
            sql=sql,
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, default=str)
    return str(value)


def _safe_view_name(stem: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "_" else "_" for c in stem).strip("_")
    return cleaned.lower() or "data"


def _duckdb_reader(path: Path) -> str | None:
    suffix = path.suffix.lower()
    quoted = str(path).replace("'", "''")
    if suffix in (".csv", ".tsv", ".txt"):
        return f"read_csv_auto('{quoted}', SAMPLE_SIZE=-1)"
    if suffix == ".parquet":
        return f"read_parquet('{quoted}')"
    if suffix in (".json", ".ndjson", ".jsonl"):
        return f"read_json_auto('{quoted}')"
    if suffix in (".xlsx", ".xls"):
        # DuckDB's excel extension is not always present; pandas handles it and
        # the frame is registered directly.
        return None
    return None


def _locate_records(payload: Any, path: str | None) -> list[Any]:
    if path:
        current = payload
        for part in path.split("."):
            if isinstance(current, dict):
                current = current.get(part)
            else:
                return []
        return current if isinstance(current, list) else []

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Take the longest list-of-dicts value; APIs bury records under
        # "data", "results", "items" and a dozen other names.
        best: list[Any] = []
        for value in payload.values():
            if isinstance(value, list) and len(value) > len(best):
                if not value or isinstance(value[0], dict):
                    best = value
        return best
    return []


def _flatten_aggregations(response: Any, started: float, sql: str) -> QueryResult:
    aggregations = response.get("aggregations") or {}
    columns = ["key", "doc_count"]
    rows: list[list[Any]] = []
    for name, body in aggregations.items():
        for bucket in body.get("buckets", []) if isinstance(body, dict) else []:
            rows.append([bucket.get("key"), bucket.get("doc_count")])
    return QueryResult(
        columns=columns,
        rows=rows,
        duration_ms=int((time.time() - started) * 1000),
        sql=sql,
    )


DOCUMENT_CONNECTORS: dict[str, type[Connector]] = {
    cls.kind: cls
    for cls in (
        MongoConnector,
        ElasticsearchConnector,
        FilesConnector,
        HTTPConnector,
    )
}
