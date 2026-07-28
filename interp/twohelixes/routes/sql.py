"""The SQL editor: schema, completion, generation, execution, saved queries.

Completion is the latency-sensitive one. A keystroke cannot wait on a model,
so `/v1/sql/complete` answers from the schema synchronously and only consults
the model when the prefix is ambiguous enough to be worth it - and even then
under a short timeout, falling back to the schema answer.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from twohelixes import auth, config, llm, ratelimit, router, store
from twohelixes.connectors import registry
from twohelixes.connectors.base import ConnectorError, UnsafeQuery, assert_read_only
from twohelixes.pipeline import prompts

log = logging.getLogger("twohelixes.routes.sql")

SQL_KEYWORDS = (
    "SELECT",
    "FROM",
    "WHERE",
    "GROUP BY",
    "ORDER BY",
    "HAVING",
    "LIMIT",
    "JOIN",
    "LEFT JOIN",
    "INNER JOIN",
    "ON",
    "AS",
    "AND",
    "OR",
    "NOT",
    "IN",
    "BETWEEN",
    "LIKE",
    "IS NULL",
    "IS NOT NULL",
    "COUNT(",
    "SUM(",
    "AVG(",
    "MIN(",
    "MAX(",
    "DISTINCT",
    "CASE WHEN",
    "WITH",
    "UNION ALL",
    "DATE_TRUNC(",
)

IDENTIFIER_TAIL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)$")
MAX_SQL_CHARS = 20_000


def _connector(ctx: router.Context, identity: Any):
    source_id = ctx.field("source_id")
    if not source_id:
        raise ConnectorError("source_id is required")
    return registry.for_source(str(source_id), identity.user_id)


@router.get("/v1/sql/schema")
def schema(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    try:
        connector = _connector(ctx, identity)
        tables = connector.tables()
    except ConnectorError as exc:
        return router.error(400, "connector_error", str(exc))

    return router.json_result(
        {
            "dialect": connector.dialect,
            "supports_sql": connector.supports_sql,
            "tables": [t.to_dict() for t in tables],
        }
    )


@router.post("/v1/sql/run")
def run_sql(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    sql = str(ctx.field("sql") or "").strip()
    if not sql:
        return router.error(400, "missing_sql")
    if len(sql) > MAX_SQL_CHARS:
        return router.error(400, "sql_too_long")

    try:
        assert_read_only(sql)
    except UnsafeQuery as exc:
        return router.error(400, "unsafe_query", str(exc))

    started = time.time()
    try:
        connector = _connector(ctx, identity)
        result = connector.execute(sql, limit=ctx.q_int("limit", 5000))
    except (ConnectorError, UnsafeQuery) as exc:
        _record_history(identity, ctx, sql, False, 0, started, str(exc))
        return router.error(400, "query_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - driver errors are unbounded
        _record_history(identity, ctx, sql, False, 0, started, str(exc))
        return router.error(400, "query_failed", str(exc))

    _record_history(identity, ctx, sql, True, result.row_count, started, "")
    return router.json_result(result.to_dict())


def _record_history(
    identity: Any,
    ctx: router.Context,
    sql: str,
    ok: bool,
    rows: int,
    started: float,
    error: str,
) -> None:
    try:
        store.execute(
            "INSERT INTO query_history (id, user_id, source_id, sql, ok, row_count, "
            "duration_ms, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                store.new_id(),
                identity.user_id,
                ctx.field("source_id"),
                sql[:MAX_SQL_CHARS],
                1 if ok else 0,
                rows,
                int((time.time() - started) * 1000),
                error[:500],
                time.time(),
            ),
        )
    except Exception:  # noqa: BLE001
        log.debug("could not record query history", exc_info=True)


@router.post("/v1/sql/complete")
def complete(ctx: router.Context) -> router.Result:
    """Autocomplete. Schema-driven first; the model only breaks ties."""
    identity = auth.require(ctx)
    prefix = str(ctx.field("prefix") or "")
    if not prefix.strip():
        return router.json_result({"completions": []})

    try:
        connector = _connector(ctx, identity)
        tables = connector.tables()
    except ConnectorError as exc:
        return router.error(400, "connector_error", str(exc))

    local = _schema_completions(prefix, tables)
    if not ctx.q_bool("ai", True) or len(local) >= 5:
        return router.json_result({"completions": local[:8], "source": "schema"})

    try:
        answer = llm.json_call(
            f"Dialect: {connector.dialect}\n"
            f"Schema:\n{connector.schema_summary(max_tables=25)}\n\n"
            f"Text before cursor:\n{prefix[-800:]}",
            system=prompts.SQL_COMPLETE_SYSTEM,
            model=config.MODEL_FAST,
            max_tokens=400,
            attempts=1,
        )
        suggestions = answer.get("completions") or []
    except llm.LLMError:
        suggestions = []

    merged = local + [
        {"text": str(s.get("text", "")), "detail": str(s.get("detail", "")), "kind": "ai"}
        for s in suggestions
        if s.get("text")
    ]
    return router.json_result({"completions": merged[:8], "source": "schema+ai"})


def _schema_completions(prefix: str, tables: list[Any]) -> list[dict[str, str]]:
    """Complete table and column names, and the clause that comes next."""
    tail_match = IDENTIFIER_TAIL.search(prefix)
    tail = tail_match.group(1).lower() if tail_match else ""
    upper = prefix.upper().rstrip()

    out: list[dict[str, str]] = []

    # After FROM / JOIN the next token is a table.
    if upper.endswith("FROM") or upper.endswith("JOIN") or re.search(r"(FROM|JOIN)\s+\w*$", upper):
        for table in tables:
            if not tail or table.name.lower().startswith(tail):
                out.append(
                    {
                        "text": table.qualified,
                        "detail": f"{len(table.columns)} columns",
                        "kind": "table",
                    }
                )
        if out:
            return out[:8]

    # Otherwise offer columns, preferring tables already named in the query.
    mentioned = {
        t for t in (tbl.name.lower() for tbl in tables) if re.search(rf"\b{re.escape(t)}\b", prefix.lower())
    }
    for table in tables:
        preferred = table.name.lower() in mentioned
        for column in table.columns:
            if tail and not column.name.lower().startswith(tail):
                continue
            out.append(
                {
                    "text": column.name,
                    "detail": f"{table.name}.{column.name} · {column.type}",
                    "kind": "column",
                    "_rank": "0" if preferred else "1",
                }
            )

    out.sort(key=lambda c: (c.pop("_rank", "1"), c["text"]))

    if tail:
        for keyword in SQL_KEYWORDS:
            if keyword.lower().startswith(tail):
                out.append({"text": keyword, "detail": "keyword", "kind": "keyword"})

    return out[:8]


@router.post("/v1/sql/generate")
def generate(ctx: router.Context) -> router.Result:
    """Plain language to SQL."""
    identity = ctx.user
    if identity is None or not identity.signed_in:
        return router.error(401, "signin_required")

    decision = ratelimit.check(identity, "sql_generate")
    if not decision.allowed:
        status = 402 if "credit" in decision.reason or "quota" in decision.reason else 429
        return router.Result(status=status, body=decision.to_payload())

    question = str(ctx.field("question") or "").strip()
    if not question:
        return router.error(400, "missing_question")

    try:
        connector = _connector(ctx, identity)
        schema_text = connector.schema_summary()
    except ConnectorError as exc:
        return router.error(400, "connector_error", str(exc))

    try:
        answer = llm.json_call(
            f"Dialect: {connector.dialect}\nSchema:\n{schema_text}\n\nRequest: {question}",
            system=prompts.SQL_GENERATE_SYSTEM,
            model=config.MODEL_DEFAULT,
        )
    except llm.LLMError as exc:
        return router.error(503, "llm_unavailable", str(exc))

    sql = str(answer.get("sql") or "").strip()
    if not sql:
        return router.error(422, "no_sql")

    try:
        assert_read_only(sql)
    except UnsafeQuery as exc:
        return router.error(422, "unsafe_generation", str(exc))

    ratelimit.consume(identity, "sql_generate")
    return router.json_result(
        {
            "sql": sql,
            "explanation": answer.get("explanation", ""),
            "tables_used": answer.get("tables_used", []),
            "estimated_rows": answer.get("estimated_rows", ""),
        }
    )


@router.get("/v1/sql/suggestions")
def suggestions(ctx: router.Context) -> router.Result:
    """Questions worth asking of this source, with the SQL to answer them."""
    identity = auth.require(ctx)
    try:
        connector = _connector(ctx, identity)
        schema_text = connector.schema_summary()
    except ConnectorError as exc:
        return router.error(400, "connector_error", str(exc))

    try:
        answer = llm.json_call(
            f"Dialect: {connector.dialect}\nSchema:\n{schema_text}",
            system=prompts.SQL_SUGGEST_SYSTEM,
            # Starter questions off a schema summary: small, bounded, and
            # nobody's chart depends on them being the best possible ones.
            model=config.MODEL_MINI,
        )
    except llm.LLMError as exc:
        return router.error(503, "llm_unavailable", str(exc))

    safe: list[dict[str, Any]] = []
    for item in answer.get("suggestions") or []:
        sql = str(item.get("sql") or "")
        try:
            assert_read_only(sql)
        except UnsafeQuery:
            continue
        safe.append(item)

    return router.json_result({"suggestions": safe})


# --------------------------------------------------------------------------
# Saved queries
# --------------------------------------------------------------------------


@router.get("/v1/queries")
def list_saved(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT id, name, sql, params, description, source_id, run_count, updated_at "
        "FROM saved_queries WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200",
        (identity.user_id,),
    )
    out = store.rows_to_dicts(rows)
    for entry in out:
        entry["params"] = store.load_json(entry.get("params"), {})
    return router.json_result({"queries": out})


@router.post("/v1/queries")
def save_query(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    name = str(ctx.field("name") or "").strip()
    sql = str(ctx.field("sql") or "").strip()
    if not name or not sql:
        return router.error(400, "name_and_sql_required")

    try:
        assert_read_only(sql)
    except UnsafeQuery as exc:
        return router.error(400, "unsafe_query", str(exc))

    now = time.time()
    query_id = str(ctx.field("id") or "") or store.new_id()
    existing = store.one(
        "SELECT id FROM saved_queries WHERE id = ? AND user_id = ?",
        (query_id, identity.user_id),
    )

    if existing:
        store.execute(
            "UPDATE saved_queries SET name = ?, sql = ?, params = ?, description = ?, "
            "source_id = ?, updated_at = ? WHERE id = ?",
            (
                name,
                sql,
                store.dump_json(ctx.field("params") or {}),
                str(ctx.field("description") or ""),
                ctx.field("source_id"),
                now,
                query_id,
            ),
        )
    else:
        store.execute(
            "INSERT INTO saved_queries (id, user_id, source_id, name, sql, params, "
            "description, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_id,
                identity.user_id,
                ctx.field("source_id"),
                name,
                sql,
                store.dump_json(ctx.field("params") or {}),
                str(ctx.field("description") or ""),
                now,
                now,
            ),
        )

    return router.json_result({"id": query_id, "name": name})


@router.delete("/v1/queries/{query_id}")
def delete_saved(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    store.execute(
        "DELETE FROM saved_queries WHERE id = ? AND user_id = ?",
        (ctx.params["query_id"], identity.user_id),
    )
    return router.json_result({"deleted": True})


@router.get("/v1/sql/history")
def history(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT sql, ok, row_count, duration_ms, error, created_at FROM query_history "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
        (identity.user_id, ctx.q_int("limit", 50)),
    )
    return router.json_result({"history": store.rows_to_dicts(rows)})
