"""Dashboards: manual assembly, AI assembly, sharing, refresh."""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from twohelixes import auth, config, llm, ratelimit, router, store
from twohelixes.charts import defaults as chart_defaults
from twohelixes.pipeline import orchestrator

log = logging.getLogger("twohelixes.routes.dashboards")

MAX_TILES = 24

DASHBOARD_SYSTEM = """You design an analytics dashboard.

Given a goal and the available data, propose the set of charts that answers it.

Return JSON:
{
  "title": "dashboard title",
  "tiles": [
    {"question": "the question this tile answers",
     "width": 1|2|3,
     "why": "what decision it supports"}
  ]
}

Rules:
- Between three and eight tiles. More than that is a report, not a dashboard.
- Lead with the headline number, then trend, then breakdown, then detail.
- Every tile must be answerable from the described data.
- No two tiles should answer the same question."""


@router.get("/v1/dashboards")
def list_dashboards(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT id, title, is_public, share_token, created_at, updated_at "
        "FROM dashboards WHERE user_id = ? ORDER BY updated_at DESC",
        (identity.user_id,),
    )
    return router.json_result({"dashboards": store.rows_to_dicts(rows)})


@router.post("/v1/dashboards")
def create_dashboard(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    title = str(ctx.field("title") or "Untitled dashboard").strip()
    now = time.time()
    dashboard_id = store.new_id()
    store.execute(
        "INSERT INTO dashboards (id, user_id, title, layout, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (dashboard_id, identity.user_id, title, store.dump_json([]), now, now),
    )
    return router.json_result({"id": dashboard_id, "title": title}, status=201)


@router.get("/v1/dashboards/{dashboard_id}")
def get_dashboard(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = store.one(
        "SELECT * FROM dashboards WHERE id = ? AND user_id = ?",
        (ctx.params["dashboard_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")
    return router.json_result(_dashboard_payload(row))


def _dashboard_payload(row: Any) -> dict[str, Any]:
    data = store.row_to_dict(row) or {}
    data["layout"] = store.load_json(data.get("layout"), [])
    charts = store.query(
        "SELECT id, title, query, spec, graph_args, updated_at FROM charts "
        "WHERE dashboard_id = ? ORDER BY created_at",
        (data["id"],),
    )
    tiles = []
    for chart in store.rows_to_dicts(charts):
        chart["spec"] = store.load_json(chart.get("spec"), {})
        chart["graph_args"] = store.load_json(chart.get("graph_args"), {})
        tiles.append(chart)
    data["charts"] = tiles
    return data


@router.put("/v1/dashboards/{dashboard_id}")
def update_dashboard(ctx: router.Context) -> router.Result:
    """Save title and tile layout - the manual dashboard-building path."""
    identity = auth.require(ctx)
    dashboard_id = ctx.params["dashboard_id"]
    row = store.one(
        "SELECT id FROM dashboards WHERE id = ? AND user_id = ?",
        (dashboard_id, identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    layout = ctx.field("layout")
    if layout is not None and not isinstance(layout, list):
        return router.error(400, "layout_must_be_a_list")
    if isinstance(layout, list) and len(layout) > MAX_TILES:
        return router.error(400, "too_many_tiles", f"maximum is {MAX_TILES}")

    fields: list[str] = []
    values: list[Any] = []
    if ctx.field("title") is not None:
        fields.append("title = ?")
        values.append(str(ctx.field("title"))[:200])
    if layout is not None:
        fields.append("layout = ?")
        values.append(store.dump_json(layout))
    fields.append("updated_at = ?")
    values.append(time.time())
    values.append(dashboard_id)

    store.execute(f"UPDATE dashboards SET {', '.join(fields)} WHERE id = ?", values)
    return router.json_result({"updated": True})


@router.delete("/v1/dashboards/{dashboard_id}")
def delete_dashboard(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    store.execute(
        "DELETE FROM dashboards WHERE id = ? AND user_id = ?",
        (ctx.params["dashboard_id"], identity.user_id),
    )
    store.execute(
        "UPDATE charts SET dashboard_id = NULL WHERE dashboard_id = ?",
        (ctx.params["dashboard_id"],),
    )
    return router.json_result({"deleted": True})


@router.post("/v1/dashboards/{dashboard_id}/share")
def share_dashboard(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    dashboard_id = ctx.params["dashboard_id"]
    enable = bool(ctx.field("public", True))

    token = secrets.token_urlsafe(18) if enable else None
    store.execute(
        "UPDATE dashboards SET is_public = ?, share_token = ?, updated_at = ? "
        "WHERE id = ? AND user_id = ?",
        (1 if enable else 0, token, time.time(), dashboard_id, identity.user_id),
    )
    return router.json_result(
        {
            "public": enable,
            "url": f"{config.site_url()}/share/{token}" if token else None,
        }
    )


@router.get("/v1/share/{token}")
def get_shared(ctx: router.Context) -> router.Result:
    """Public read of a shared dashboard. No auth, no data-source access."""
    row = store.one(
        "SELECT * FROM dashboards WHERE share_token = ? AND is_public = 1",
        (ctx.params["token"],),
    )
    if row is None:
        return router.error(404, "not_found")

    payload = _dashboard_payload(row)
    # A share link exposes rendered charts, never the query or the source.
    for chart in payload.get("charts", []):
        chart.pop("query", None)
    payload.pop("user_id", None)
    return router.json_result(payload)


@router.stream("/v1/dashboard/stream")
def build_dashboard(stream: Any, ctx: router.Context) -> None:
    """Build a whole dashboard from one goal, streaming each tile as it lands."""
    identity = ctx.user
    if identity is None or not identity.signed_in:
        stream.emit("error", {"code": "signin_required"})
        return

    decision = ratelimit.check(identity, "dashboard_build")
    if not decision.allowed:
        stream.emit("error", {"code": decision.reason, "message": decision.detail})
        return

    goal = str(ctx.field("goal") or ctx.field("q") or "").strip()
    if not goal:
        stream.emit("error", {"code": "missing_goal"})
        return

    from twohelixes.routes import query as query_routes

    try:
        frames = query_routes._load_frames(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        stream.emit("error", {"code": "data_unavailable", "message": str(exc)})
        return

    if not frames:
        stream.emit("error", {"code": "no_data"})
        return

    from twohelixes.interpreter import tools

    profiles = {name: tools.profile(frame) for name, frame in frames.items()}
    stream.emit("stage", {"stage": "plan", "status": "start"})

    try:
        plan = llm.json_call(
            f"Goal: {goal}\n\nAvailable data:\n{profiles}",
            system=DASHBOARD_SYSTEM,
            model=config.MODEL_ESCALATE if identity.paid else config.MODEL_DEFAULT,
        )
    except llm.LLMError as exc:
        stream.emit("error", {"code": "llm_unavailable", "message": str(exc)})
        return

    tiles = (plan.get("tiles") or [])[:8]
    stream.emit(
        "stage",
        {"stage": "plan", "status": "done", "tiles": len(tiles), "title": plan.get("title")},
    )
    stream.emit("plan", plan)

    dashboard_id = store.new_id()
    now = time.time()
    store.execute(
        "INSERT INTO dashboards (id, user_id, title, layout, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            dashboard_id,
            identity.user_id,
            str(plan.get("title") or goal)[:200],
            store.dump_json([]),
            now,
            now,
        ),
    )
    stream.emit("dashboard", {"id": dashboard_id, "title": plan.get("title")})

    layout: list[dict[str, Any]] = []
    built = 0
    for index, tile in enumerate(tiles):
        question = str(tile.get("question") or "").strip()
        if not question:
            continue

        stream.emit("tile_start", {"index": index, "question": question})
        result = orchestrator.run(
            question,
            frames,
            stream=None,  # tile-level reasoning would drown the dashboard view
            model=config.MODEL_DEFAULT,
            mode=str(ctx.field("mode") or "light"),
        )
        if result.figure is None:
            stream.emit("tile_failed", {"index": index, "question": question})
            continue

        chart_id = store.new_id()
        store.execute(
            "INSERT INTO charts (id, user_id, dashboard_id, title, query, source_id, "
            "spec, graph_args, trace, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chart_id,
                identity.user_id,
                dashboard_id,
                str(result.config.get("title") or question)[:200],
                question,
                ctx.field("source_id"),
                store.dump_json(result.figure),
                store.dump_json(result.config),
                store.dump_json(result.trace[-40:]),
                time.time(),
                time.time(),
            ),
        )
        layout.append(
            {
                "chart_id": chart_id,
                "w": int(tile.get("width") or 1),
                "h": 1,
                "x": 0,
                "y": index,
            }
        )
        built += 1
        stream.emit(
            "tile",
            {
                "index": index,
                "chart_id": chart_id,
                "question": question,
                "figure": result.figure,
                "config": result.config,
                "audit": result.audit,
            },
        )

    store.execute(
        "UPDATE dashboards SET layout = ?, updated_at = ? WHERE id = ?",
        (store.dump_json(layout), time.time(), dashboard_id),
    )

    if built:
        ratelimit.consume(identity, "dashboard_build")
    stream.emit("complete", {"dashboard_id": dashboard_id, "tiles_built": built})


@router.post("/v1/dashboards/{dashboard_id}/refresh")
def refresh_dashboard(ctx: router.Context) -> router.Result:
    """Re-run every tile's query against current data."""
    identity = auth.require(ctx)
    dashboard_id = ctx.params["dashboard_id"]
    charts = store.query(
        "SELECT * FROM charts WHERE dashboard_id = ? AND user_id = ?",
        (dashboard_id, identity.user_id),
    )
    if not charts:
        return router.error(404, "no_charts")

    from twohelixes.pipeline import figures
    from twohelixes.routes import query as query_routes

    refreshed = 0
    failures: list[str] = []
    for row in charts:
        frame = query_routes._frame_for_chart(identity, row)
        if frame is None:
            failures.append(row["id"])
            continue
        config_args = store.load_json(row["graph_args"], {}) or {}
        validated = figures.validate_config(config_args, frame)
        figure, _ = figures.build(frame, validated)
        figure = chart_defaults.apply(figure, chart_type=validated["chart_type"])
        store.execute(
            "UPDATE charts SET spec = ?, updated_at = ? WHERE id = ?",
            (store.dump_json(figure), time.time(), row["id"]),
        )
        refreshed += 1

    return router.json_result({"refreshed": refreshed, "failed": failures})
