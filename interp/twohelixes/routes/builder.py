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
    import pandas as pd

    dataset_id = ctx.field("dataset_id")
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

    sample_key = ctx.field("sample")
    if sample_key:
        from twohelixes.datasets import samples

        return samples.frame(str(sample_key))

    source_id, sql = ctx.field("source_id"), ctx.field("sql")
    if source_id and sql:
        from twohelixes.connectors import registry

        connector = registry.for_source(str(source_id), identity.user_id)
        return connector.frame(str(sql))

    rows = ctx.field("data")
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

    existing = store.one(
        "SELECT id FROM charts WHERE id = ? AND user_id = ?", (chart_id, identity.user_id)
    )
    payload = store.dump_json(
        {"steps": [s.to_dict() for s in steps], "config": chart_config}
    )

    if existing:
        store.execute(
            "UPDATE charts SET title = ?, spec = ?, graph_args = ?, trace = ?, "
            "updated_at = ? WHERE id = ?",
            (
                str(chart_config.get("title") or "Chart")[:200],
                store.dump_json(figure),
                store.dump_json(chart_config),
                payload,
                now,
                chart_id,
            ),
        )
    else:
        store.execute(
            "INSERT INTO charts (id, user_id, dashboard_id, title, query, source_id, "
            "spec, graph_args, trace, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chart_id,
                identity.user_id,
                ctx.field("dashboard_id"),
                str(chart_config.get("title") or "Chart")[:200],
                str(ctx.field("request") or ""),
                ctx.field("source_id"),
                store.dump_json(figure),
                store.dump_json(chart_config),
                payload,
                now,
                now,
            ),
        )

    return router.json_result({"chart_id": chart_id, "saved": True})


@router.post("/v1/builder/notebook")
def notebook(ctx: router.Context) -> router.Result:
    """Export the plan as a marimo notebook.

    The notebook carries the steps as readable pandas rather than a second
    implementation, so what runs there is what ran here.
    """
    identity = auth.require(ctx)

    try:
        steps = transform.normalise(ctx.field("steps") or [])
    except transform.TransformError as exc:
        return router.error(400, "invalid_plan", str(exc))

    from twohelixes.notebooks import marimo_export

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
    source = marimo_export.build(
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
        content_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="chart.py"'},
    )
