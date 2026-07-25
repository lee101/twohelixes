"""Chat-to-chart: the main product surface.

Two shapes of the same work. `/v1/query` runs the pipeline and returns the
finished chart, which is what the API and dashboard refresh use.
`/v1/query/stream` runs it on a worker thread and streams every stage boundary,
thought and partial configuration as it happens - that is what makes the UI
feel like watching an analyst work rather than waiting on a spinner.

Rate limiting happens before any model call and charging happens only after
success, so a pipeline that fails does not burn a free query.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from twohelixes import auth, config, ratelimit, router, store
from twohelixes.connectors import registry
from twohelixes.pipeline import orchestrator

log = logging.getLogger("twohelixes.routes.query")

MAX_QUESTION_CHARS = 2000


def _load_frames(identity: Any, ctx: router.Context) -> dict[str, Any]:
    """Resolve whatever data the request points at into named frames."""
    frames: dict[str, Any] = {}

    inline = ctx.field("data")
    if isinstance(inline, list) and inline:
        import pandas as pd

        frames["inline"] = pd.DataFrame(inline)
        return frames

    source_id = ctx.field("source_id")
    sql = ctx.field("sql")
    if source_id and sql:
        connector = registry.for_source(str(source_id), identity.user_id)
        frames[str(ctx.field("name") or "query")] = connector.frame(str(sql))
        return frames

    dataset_ids = ctx.field("dataset_ids") or []
    if isinstance(dataset_ids, str):
        dataset_ids = [dataset_ids]
    for dataset_id in dataset_ids:
        row = store.one(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?",
            (dataset_id, identity.user_id),
        )
        if row is None:
            continue
        frames[row["name"]] = _load_dataset(row)

    return frames


def _load_dataset(row: Any) -> Any:
    import pandas as pd

    storage = row["storage"]
    if storage.endswith(".parquet"):
        return pd.read_parquet(storage)
    if storage.endswith(".csv"):
        return pd.read_csv(storage)
    return pd.read_json(storage)


def _catalog(identity: Any) -> list[dict[str, Any]]:
    rows = store.query(
        "SELECT id, name, description FROM datasets WHERE user_id = ? LIMIT 50",
        (identity.user_id,),
    )
    return store.rows_to_dicts(rows)


def _guard(ctx: router.Context, operation: str) -> tuple[Any, router.Result | None]:
    identity = ctx.user
    if identity is None or not identity.signed_in:
        return identity, router.error(
            401,
            "signin_required",
            "Sign in to run queries. New accounts get "
            f"{config.FREE_QUERIES_PER_USER} free.",
        )

    decision = ratelimit.check(identity, operation)
    if not decision.allowed:
        status = 402 if decision.reason in ("insufficient_credits", "free_quota_exhausted") else 429
        return identity, router.Result(status=status, body=decision.to_payload())

    return identity, None


@router.post("/v1/query")
def run_query(ctx: router.Context) -> router.Result:
    identity, refusal = _guard(ctx, "chat_query")
    if refusal is not None:
        return refusal

    question = str(ctx.field("q") or ctx.field("question") or "").strip()
    if not question:
        return router.error(400, "missing_question")
    if len(question) > MAX_QUESTION_CHARS:
        return router.error(400, "question_too_long")

    try:
        frames = _load_frames(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        return router.error(400, "data_unavailable", str(exc))

    if not frames:
        return router.error(400, "no_data", "Connect a data source or upload a file.")

    result = orchestrator.run(
        question,
        frames,
        catalog=_catalog(identity),
        model=_model_for(identity),
        mode=str(ctx.field("mode") or "light"),
        existing_config=ctx.field("config"),
        edit_request=str(ctx.field("edit") or ""),
    )

    if result.figure is None:
        return router.error(422, "no_chart", "; ".join(result.warnings) or "pipeline failed")

    ratelimit.consume(identity, "chat_query")
    _persist_chart(identity, ctx, question, result)
    return router.json_result({**result.to_payload(), "user": identity.to_public()})


@router.stream("/v1/query/stream")
def stream_query(stream: Any, ctx: router.Context) -> None:
    """The interactive path: emit reasoning as the pipeline produces it."""
    identity = ctx.user
    if identity is None or not identity.signed_in:
        stream.emit(
            "error",
            {
                "code": "signin_required",
                "message": "Sign in to run queries.",
            },
        )
        return

    decision = ratelimit.check(identity, "chat_query")
    if not decision.allowed:
        stream.emit("error", {"code": decision.reason, "message": decision.detail})
        return

    question = str(ctx.field("q") or ctx.field("question") or "").strip()
    if not question:
        stream.emit("error", {"code": "missing_question"})
        return

    stream.emit("accepted", {"question": question, "at": time.time()})

    try:
        frames = _load_frames(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        stream.emit("error", {"code": "data_unavailable", "message": str(exc)})
        return

    if not frames:
        stream.emit(
            "error",
            {"code": "no_data", "message": "Connect a data source or upload a file."},
        )
        return

    stream.emit(
        "datasets",
        {name: {"rows": int(len(frame))} for name, frame in frames.items()},
    )

    result = orchestrator.run(
        question,
        frames,
        catalog=_catalog(identity),
        stream=stream,
        model=_model_for(identity),
        mode=str(ctx.field("mode") or "light"),
        existing_config=ctx.field("config"),
        edit_request=str(ctx.field("edit") or ""),
    )

    if result.figure is None:
        stream.emit(
            "error",
            {"code": "no_chart", "message": "; ".join(result.warnings) or "pipeline failed"},
        )
        return

    ratelimit.consume(identity, "chat_query")
    chart_id = _persist_chart(identity, ctx, question, result)
    stream.emit("result", {**result.to_payload(), "chart_id": chart_id})
    stream.emit("user", identity.to_public())


def _model_for(identity: Any) -> str:
    """Paying callers get the stronger route; free queries stay on Luna."""
    return config.MODEL_ESCALATE if identity.paid else config.MODEL_DEFAULT


def _persist_chart(
    identity: Any, ctx: router.Context, question: str, result: Any
) -> str:
    chart_id = store.new_id()
    now = time.time()
    try:
        store.execute(
            "INSERT INTO charts (id, user_id, dashboard_id, title, query, source_id, "
            "spec, graph_args, trace, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chart_id,
                identity.user_id,
                ctx.field("dashboard_id"),
                str(result.config.get("title") or question)[:200],
                question,
                ctx.field("source_id"),
                store.dump_json(result.figure),
                store.dump_json(result.config),
                store.dump_json(result.trace[-80:]),
                now,
                now,
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception("could not persist chart")
    return chart_id


@router.post("/v1/chart/{chart_id}/config")
def update_chart_config(ctx: router.Context) -> router.Result:
    """Manual control: the user changes x / y / z / type without the model."""
    identity = auth.require(ctx)
    chart_id = ctx.params["chart_id"]

    row = store.one(
        "SELECT * FROM charts WHERE id = ? AND user_id = ?",
        (chart_id, identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    updates = ctx.json
    if not isinstance(updates, dict):
        return router.error(400, "invalid_body")

    current = store.load_json(row["graph_args"], {}) or {}
    current.update({k: v for k, v in updates.items() if k != "data"})

    frame = _frame_for_chart(identity, row)
    if frame is None:
        return router.error(409, "data_unavailable", "Re-run the query to change this chart.")

    from twohelixes.charts import defaults as chart_defaults
    from twohelixes.pipeline import figures

    validated = figures.validate_config(current, frame)
    figure, warnings = figures.build(frame, validated, mode=str(ctx.field("mode") or "light"))
    figure = chart_defaults.apply(
        figure, mode=str(ctx.field("mode") or "light"), chart_type=validated["chart_type"]
    )

    store.execute(
        "UPDATE charts SET spec = ?, graph_args = ?, updated_at = ? WHERE id = ?",
        (store.dump_json(figure), store.dump_json(validated), time.time(), chart_id),
    )

    return router.json_result(
        {
            "figure": figure,
            "config": validated,
            "warnings": warnings,
            "audit": chart_defaults.audit(figure),
        }
    )


def _frame_for_chart(identity: Any, row: Any) -> Any:
    """Re-materialise the data a saved chart was built from."""
    source_id = row["source_id"]
    query = row["query"]
    if not source_id or not query:
        return None
    try:
        connector = registry.for_source(source_id, identity.user_id)
        return connector.frame(query)
    except Exception:  # noqa: BLE001
        return None


@router.get("/v1/chart/{chart_id}")
def get_chart(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = store.one(
        "SELECT * FROM charts WHERE id = ? AND user_id = ?",
        (ctx.params["chart_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    data = store.row_to_dict(row) or {}
    data["spec"] = store.load_json(data.get("spec"), {})
    data["graph_args"] = store.load_json(data.get("graph_args"), {})
    data["trace"] = store.load_json(data.get("trace"), [])
    return router.json_result(data)


@router.get("/v1/me")
def me(ctx: router.Context) -> router.Result:
    identity = ctx.user
    if identity is None:
        return router.json_result({"signed_in": False})
    return router.json_result(identity.to_public())
