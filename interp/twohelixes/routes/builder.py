"""The chart builder: shared control between a person and the agent.

Both sides edit the same object - a list of transformation steps plus a chart
configuration. The agent proposes steps; the person reorders, edits, disables
or deletes them. Neither needs the other's permission, and a human edit never
costs a model call.

`/preview` is the loop the builder runs on every change: validate the plan
against the schema, apply it, rebuild the chart, audit it, and return
everything needed to redraw. It is deliberately free - only asking the *agent*
for a plan costs a query.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from twohelixes import auth, config, credits, llm, ratelimit, router, store
from twohelixes.charts import defaults as chart_defaults
from twohelixes.pipeline import figures, transform

log = logging.getLogger("twohelixes.routes.builder")

PREVIEW_ROWS = 50
MAX_PREVIEW_SOURCE_ROWS = 200_000

BUILDER_SYSTEM = """You edit a chart-building plan.

A plan is a list of transformation steps plus a chart configuration. You are
given the current plan, the data's columns and roles, and a request.

Step types and their params:
  filter    {column, op, value}   op: eq|ne|gt|ge|lt|le|in|not_in|contains|
                                      starts_with|ends_with|is_null|not_null
  aggregate {by: [col], metrics: [{column, agg, as}]}  agg: sum|mean|median|
                                      count|nunique|min|max|std|first|last
  derive    {as, expr}            expr is pandas arithmetic over columns
  resample  {time_column, grain, by: [col], metrics: [{column, agg, as}]}
                                  grain: hour|day|week|month|quarter|year
  top_n     {category, measure, n, agg}
  sort      {column, desc}
  limit     {n}
  select    {columns: [col]}
  rename    {map: {old: new}}
  dropna    {columns: [col]}
  pivot     {index, columns, values, agg}

Return JSON:
{
  "steps": [{"type": "...", "params": {...}, "note": "why this step exists"}],
  "config": {"chart_type": "...", "x": "...", "y": "...", "color": "...",
             "agg": "...", "title": "...", "x_title": "...", "y_title": "..."},
  "explanation": "one sentence on what changed"
}

Rules:
- Return the COMPLETE plan, not a patch. Keep the steps the user has already
  got unless the request asks to change them.
- Never invent a column. Use only the columns given.
- Aggregate before charting; a scatter of 200k raw rows is not an answer.
- Use top_n rather than limit when ranking categories, so the tail becomes
  "Other" instead of disappearing.
- The title should state the finding, not restate the axes."""


def _load_frame(identity: Any, ctx: router.Context) -> Any:
    """Resolve whatever data the builder is pointed at."""
    return frame_from_source(
        identity,
        {
            key: ctx.field(key)
            for key in ("dataset_id", "sample", "source_id", "sql", "data")
            if ctx.field(key) is not None
        },
    )


# The keys that name data without carrying it. These are what a saved chart
# keeps so it can be rebuilt later; `data` is deliberately not one of them,
# because an inline payload is kept as rows on the chart instead.
SOURCE_KEYS = ("dataset_id", "sample", "source_id", "sql")


def frame_from_source(identity: Any, source: dict[str, Any]) -> Any:
    """Resolve a data descriptor to a frame.

    Taking a dict rather than the request means a chart saved from the builder
    can be rebuilt from the descriptor stored with it - which is what makes a
    built chart reconfigurable and refreshable months later, without keeping a
    copy of the rows.
    """
    import pandas as pd

    dataset_id = source.get("dataset_id")
    if dataset_id:
        row = store.one(
            "SELECT * FROM datasets WHERE id = ? AND user_id = ?",
            (str(dataset_id), identity.user_id),
        )
        if row is None:
            raise ValueError("no such dataset")
        storage = row["storage"]
        if storage.endswith(".parquet"):
            return pd.read_parquet(storage)
        if storage.endswith(".csv"):
            return pd.read_csv(storage)
        return pd.read_json(storage)

    sample_key = source.get("sample")
    if sample_key:
        from twohelixes.datasets import samples

        return samples.frame(str(sample_key))

    source_id, sql = source.get("source_id"), source.get("sql")
    if source_id and sql:
        from twohelixes.connectors import registry

        connector = registry.for_source(str(source_id), identity.user_id)
        return connector.frame(str(sql))

    rows = source.get("data")
    if isinstance(rows, list) and rows:
        return pd.DataFrame(rows)

    raise ValueError("no data: pass dataset_id, sample, source_id+sql, or data")


@router.post("/v1/builder/schema")
def schema(ctx: router.Context) -> router.Result:
    """Columns with roles, so the builder can offer sensible channels."""
    identity = auth.require(ctx)
    try:
        frame = _load_frame(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        return router.error(400, "data_unavailable", str(exc))

    from twohelixes.interpreter import tools

    profile = tools.profile(frame)
    return router.json_result(
        {
            "rows": int(len(frame)),
            "columns": profile["columns"],
            "suggested": figures.heuristic_config(frame, ""),
            "step_types": sorted(transform.STEP_TYPES),
            "aggregations": sorted(transform.AGGREGATIONS),
            "grains": sorted(transform.TIME_GRAINS),
        }
    )


@router.post("/v1/builder/preview")
def preview(ctx: router.Context) -> router.Result:
    """Apply a plan and return the chart. Free - no model call."""
    identity = auth.require(ctx)

    try:
        frame = _load_frame(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        return router.error(400, "data_unavailable", str(exc))

    if len(frame) > MAX_PREVIEW_SOURCE_ROWS:
        frame = frame.head(MAX_PREVIEW_SOURCE_ROWS)

    try:
        steps = transform.normalise(ctx.field("steps") or [])
    except transform.TransformError as exc:
        return router.error(400, "invalid_plan", str(exc))

    problems = transform.validate(steps, [str(c) for c in frame.columns])
    if problems:
        # Return them rather than raising: the builder shows the bad step in
        # place so the user can fix it without losing the rest of the plan.
        return router.json_result(
            {"ok": False, "problems": problems, "steps": [s.to_dict() for s in steps]},
            status=200,
        )

    started = time.time()
    try:
        shaped, notes = transform.apply(frame, steps)
    except transform.TransformError as exc:
        return router.error(400, "transform_failed", str(exc))

    raw_config = ctx.field("config") or {}
    if not isinstance(raw_config, dict):
        raw_config = {}
    if not raw_config:
        raw_config = figures.heuristic_config(shaped, "")

    mode = str(ctx.field("mode") or "light")
    config_out = figures.validate_config(raw_config, shaped)
    figure, warnings = figures.build(shaped, config_out, mode=mode)
    figure = chart_defaults.apply(figure, mode=mode, chart_type=config_out["chart_type"])

    return router.json_result(
        {
            "ok": True,
            "figure": figure,
            "config": config_out,
            "steps": [s.to_dict() for s in steps],
            "descriptions": [transform.describe(s) for s in steps],
            "notes": notes,
            "warnings": warnings,
            "audit": chart_defaults.audit(figure, mode=mode),
            "columns": [str(c) for c in shaped.columns],
            "row_count": int(len(shaped)),
            "preview": shaped.head(PREVIEW_ROWS).to_dict("records"),
            "python": transform.to_python(steps),
            "elapsed_ms": int((time.time() - started) * 1000),
        }
    )


@router.post("/v1/builder/suggest")
def suggest(ctx: router.Context) -> router.Result:
    """Ask the agent to write or edit the plan. This one costs a query."""
    identity = ctx.user
    if identity is None or not identity.signed_in:
        return router.error(401, "signin_required")

    decision = ratelimit.check(identity, "chat_query")
    if not decision.allowed:
        status = 402 if "credit" in decision.reason or "quota" in decision.reason else 429
        return router.Result(status=status, body=decision.to_payload())

    request = str(ctx.field("request") or "").strip()
    if not request:
        return router.error(400, "missing_request")

    try:
        frame = _load_frame(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        return router.error(400, "data_unavailable", str(exc))

    from twohelixes.interpreter import tools

    try:
        current = transform.normalise(ctx.field("steps") or [])
    except transform.TransformError:
        current = []

    prompt = (
        f"Columns:\n{tools.profile(frame)}\n\n"
        f"Current plan:\n{[s.to_dict() for s in current]}\n\n"
        f"Current chart:\n{ctx.field('config') or {}}\n\n"
        f"Request: {request}"
    )

    try:
        answer = llm.json_call(
            prompt,
            system=BUILDER_SYSTEM,
            model=config.MODEL_ESCALATE if identity.paid else config.MODEL_DEFAULT,
        )
    except llm.LLMError as exc:
        return router.error(503, "llm_unavailable", str(exc))

    try:
        proposed = transform.normalise(answer.get("steps") or [])
    except transform.TransformError as exc:
        return router.error(422, "bad_plan", str(exc))

    # Mark every step the agent wrote, so the builder can show provenance and
    # the user can see at a glance what they changed themselves.
    for step in proposed:
        step.author = "agent"

    problems = transform.validate(proposed, [str(c) for c in frame.columns])
    if problems:
        return router.error(
            422, "plan_does_not_fit", "; ".join(p["error"] for p in problems)
        )

    ratelimit.consume(identity, "chat_query")

    proposed_config = answer.get("config") if isinstance(answer.get("config"), dict) else {}
    return router.json_result(
        {
            "steps": [s.to_dict() for s in proposed],
            "descriptions": [transform.describe(s) for s in proposed],
            "config": proposed_config,
            "explanation": str(answer.get("explanation") or ""),
        }
    )


@router.post("/v1/builder/save")
def save(ctx: router.Context) -> router.Result:
    """Persist a built chart, plan included, so it can be reopened and edited."""
    identity = auth.require(ctx)

    try:
        steps = transform.normalise(ctx.field("steps") or [])
    except transform.TransformError as exc:
        return router.error(400, "invalid_plan", str(exc))

    chart_config = ctx.field("config") or {}
    figure = ctx.field("figure") or {}
    chart_id = str(ctx.field("chart_id") or "") or store.new_id()
    now = time.time()
    shaped_rows = _shaped_rows(identity, ctx, steps)

    existing = store.one(
        "SELECT id FROM charts WHERE id = ? AND user_id = ?", (chart_id, identity.user_id)
    )
    # The descriptor travels with the chart. Without it a built chart could be
    # looked at and nothing else: the manual controls, the tile editor and
    # dashboard refresh all need a frame, and there was nothing to rebuild one
    # from once the request that made it had gone.
    payload = store.dump_json(
        {
            "steps": [s.to_dict() for s in steps],
            "config": chart_config,
            "source": {
                key: ctx.field(key) for key in SOURCE_KEYS if ctx.field(key) is not None
            },
        }
    )

    if existing:
        store.execute(
            "UPDATE charts SET title = ?, spec = ?, graph_args = ?, trace = ?, "
            "rows_json = ?, updated_at = ? WHERE id = ?",
            (
                str(chart_config.get("title") or "Chart")[:200],
                store.dump_json(figure),
                store.dump_json(chart_config),
                payload,
                shaped_rows,
                now,
                chart_id,
            ),
        )
    else:
        store.execute(
            "INSERT INTO charts (id, user_id, dashboard_id, title, query, source_id, "
            "spec, graph_args, trace, rows_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chart_id,
                identity.user_id,
                ctx.field("dashboard_id"),
                str(chart_config.get("title") or "Chart")[:200],
                str(ctx.field("sql") or ctx.field("request") or ""),
                ctx.field("source_id"),
                store.dump_json(figure),
                store.dump_json(chart_config),
                payload,
                shaped_rows,
                now,
                now,
            ),
        )

    return router.json_result({"chart_id": chart_id, "saved": True})


# Enough to redraw any form; past this the data belongs in a dataset.
MAX_KEPT_ROWS = 5000


def _shaped_rows(identity: Any, ctx: router.Context, steps: Any) -> str | None:
    """The rows this chart plots, kept with it.

    Without them a chart built by hand could be looked at and nothing else:
    the manual controls, the tile editor and dashboard refresh all need a frame
    to rebuild from, and `_frame_for_chart` had nowhere to get one. A chart on a
    connected source can be re-queried instead, so it keeps no copy.
    """
    if any(ctx.field(key) is not None for key in SOURCE_KEYS):
        return None
    try:
        frame = _load_frame(identity, ctx)
        shaped, _notes = transform.apply(frame, steps)
        if len(shaped) > MAX_KEPT_ROWS:
            return None
        return store.dump_json(shaped.to_dict("records"))
    except Exception:  # noqa: BLE001 - never fail a save over its own cache
        log.exception("could not keep the rows for a built chart")
        return None


@router.post("/v1/builder/notebook")
def notebook(ctx: router.Context) -> router.Result:
    """Export the plan as a notebook - marimo, or .ipynb with format=ipynb.

    The notebook carries the steps as readable pandas rather than a second
    implementation, so what runs there is what ran here.
    """
    identity = auth.require(ctx)

    try:
        steps = transform.normalise(ctx.field("steps") or [])
    except transform.TransformError as exc:
        return router.error(400, "invalid_plan", str(exc))

    from twohelixes.notebooks import marimo_export
    from twohelixes.routes.notebooks import _format

    dataset_path = ""
    dataset_id = ctx.field("dataset_id")
    if dataset_id:
        row = store.one(
            "SELECT storage FROM datasets WHERE id = ? AND user_id = ?",
            (str(dataset_id), identity.user_id),
        )
        if row:
            dataset_path = str(row["storage"])
    elif ctx.field("sample"):
        from twohelixes.datasets import samples

        dataset_path = str(samples.path_for(str(ctx.field("sample"))))

    chart_config = ctx.field("config") or {}
    module, extension, content_type = _format(ctx)
    source = module.build(
        marimo_export.NotebookSpec(
            title=str(chart_config.get("title") or "twoHelixes chart"),
            question=str(ctx.field("request") or ""),
            dataset_path=dataset_path,
            transform_code=transform.to_python(steps),
            chart_config=chart_config if isinstance(chart_config, dict) else {},
        )
    )

    return router.Result(
        status=200,
        body=source,
        content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="chart.{extension}"'},
    )
