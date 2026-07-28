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


def _analytics_chart(
    title: str, rows: list[dict[str, Any]], config_args: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    import pandas as pd

    from twohelixes.pipeline import figures

    frame = pd.DataFrame(rows)
    validated = {
        "x": None,
        "y": None,
        "color": None,
        "agg": None,
        **config_args,
        "title": title,
    }
    figure, _warnings = figures.build(frame, validated)
    figure = chart_defaults.apply(figure, chart_type=str(validated["chart_type"]))
    if chart_defaults.audit(figure):
        validated = {"chart_type": "table", "title": title}
        figure, _warnings = figures.build(frame, validated)
        figure = chart_defaults.apply(figure, chart_type="table")
    return figure, validated


def build_analytics_event_dashboard(site_id: str, event_name: str) -> str | None:
    site = store.row_to_dict(
        store.one("SELECT user_id, name FROM analytics_sites WHERE id = ?", (site_id,))
    )
    if site is None:
        return None
    event_rows = store.rows_to_dicts(
        store.query(
            "SELECT ts, client_id, page_path, device, referrer_host, props"
            " FROM analytics_events WHERE site_id = ? AND event_name = ?"
            " ORDER BY ts DESC LIMIT 10000",
            (site_id, event_name),
        )
    )
    if not event_rows:
        return None

    buckets: dict[int, int] = {}
    for row in event_rows:
        slot = int(float(row["ts"]) // 3600 * 3600)
        buckets[slot] = buckets.get(slot, 0) + 1
    import pandas as pd

    panels: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = [
        (
            "Volume over time",
            [
                {"time": pd.to_datetime(slot, unit="s", utc=True), "events": count}
                for slot, count in sorted(buckets.items())
            ],
            {"chart_type": "line", "x": "time", "y": "events"},
        ),
        (
            "Unique users",
            [{"users": len({str(row["client_id"]) for row in event_rows})}],
            {"chart_type": "stat", "y": "users"},
        ),
    ]

    def distribution(column: str, title: str) -> None:
        counts: dict[str, int] = {}
        for row in event_rows:
            value = str(row.get(column) or "")
            if value:
                counts[value] = counts.get(value, 0) + 1
        if not counts:
            return
        data = [
            {"value": key, "events": count}
            for key, count in sorted(counts.items(), key=lambda item: item[1], reverse=True)[:15]
        ]
        chart_type = "hbar" if len(data) > 1 else "table"
        panels.append(
            (
                title,
                data,
                {"chart_type": chart_type, "x": "value", "y": "events"},
            )
        )

    distribution("page_path", "Top pages")

    prop_counts: dict[tuple[str, str], int] = {}
    for row in event_rows:
        props = store.load_json(row.get("props"), {})
        if not isinstance(props, dict):
            continue
        for key, value in props.items():
            if isinstance(value, (dict, list)):
                continue
            pair = (str(key)[:80], str(value)[:160])
            prop_counts[pair] = prop_counts.get(pair, 0) + 1
    if prop_counts:
        prop_rows = [
            {"property": key, "value": value, "events": count}
            for (key, value), count in sorted(
                prop_counts.items(), key=lambda item: item[1], reverse=True
            )[:30]
        ]
        panels.append(
            ("Top property values", prop_rows, {"chart_type": "table"})
        )

    distribution("device", "Device breakdown")
    distribution("referrer_host", "Referrer breakdown")

    built: list[tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any]]] = []
    for title, rows, args in panels:
        try:
            figure, validated = _analytics_chart(title, rows, args)
            built.append((title, rows, validated, figure))
        except Exception:
            log.exception("could not build automatic analytics panel %s", title)
    if not built:
        return None

    dashboard_id = store.new_id()
    now = time.time()
    layout: list[dict[str, Any]] = []
    chart_rows: list[tuple[Any, ...]] = []
    for index, (title, rows, validated, figure) in enumerate(built):
        chart_id = store.new_id()
        chart_rows.append(
            (
                chart_id,
                site["user_id"],
                dashboard_id,
                title,
                f"Automatic analytics view for {event_name}",
                store.dump_json(figure),
                store.dump_json(validated),
                store.dump_json(rows),
                now,
                now,
            )
        )
        layout.append(
            {"chart_id": chart_id, "w": 2 if index == 0 else 1, "h": 1, "x": 0, "y": index}
        )

    with store.transaction() as conn:
        cursor = conn.execute(
            "UPDATE analytics_event_dashboards SET status = 'building'"
            " WHERE site_id = ? AND event_name = ? AND status = 'pending'",
            (site_id, event_name),
        )
        if not cursor.rowcount:
            return None
        conn.execute(
            "INSERT INTO dashboards (id, user_id, title, layout, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                dashboard_id,
                site["user_id"],
                f"{event_name} — {site['name']}"[:200],
                store.dump_json(layout),
                now,
                now,
            ),
        )
        for chart_row in chart_rows:
            conn.execute(
                "INSERT INTO charts"
                " (id, user_id, dashboard_id, title, query, spec, graph_args,"
                " rows_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                chart_row,
            )
        conn.execute(
            "UPDATE analytics_event_dashboards"
            " SET dashboard_id = ?, status = 'done', created_at = ?"
            " WHERE site_id = ? AND event_name = ? AND status = 'building'",
            (dashboard_id, now, site_id, event_name),
        )
    return dashboard_id


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
    return router.json_result(_dashboard_payload(row, mode=ctx.q("mode", "light") or "light"))


def _dashboard_payload(row: Any, mode: str = "light") -> dict[str, Any]:
    data = store.row_to_dict(row) or {}
    data["layout"] = store.load_json(data.get("layout"), [])
    charts = store.query(
        "SELECT id, title, query, spec, graph_args, updated_at FROM charts "
        "WHERE dashboard_id = ? ORDER BY created_at",
        (data["id"],),
    )
    tiles = []
    for chart in store.rows_to_dicts(charts):
        chart["spec"] = _themed(store.load_json(chart.get("spec"), {}), mode)
        chart["graph_args"] = store.load_json(chart.get("graph_args"), {})
        tiles.append(chart)
    data["charts"] = tiles
    return data


def _themed(figure: dict[str, Any], mode: str) -> dict[str, Any]:
    """Restyle a stored figure for the viewer's theme.

    A saved chart has its colours baked in at the mode it was built in, and a
    dashboard outlives that moment: a board assembled in daylight was three
    white plates on a dark page, which is the same bug the dataset pages solve
    with theme variables. Here the fix is cheaper - restyling is layout and
    colour work over a figure we already have, with no data to reload.
    """
    if not figure.get("data"):
        return figure
    try:
        return chart_defaults.apply(_strip_theme(figure), mode=mode)
    except Exception:  # noqa: BLE001 - a mis-styled chart beats a missing one
        log.exception("could not restyle a dashboard tile")
        return figure


# The layout keys the theme owns. They have to be removed before re-applying:
# `defaults.apply` merges the figure's layout over the base one, so a paper
# colour saved in daylight wins over the dark base and the tile comes back as
# a white plate on a dark board - which is exactly what it did.
_THEMED_LAYOUT_KEYS = ("paper_bgcolor", "plot_bgcolor", "font", "colorway", "hoverlabel")
_THEMED_AXIS_KEYS = ("gridcolor", "linecolor", "zerolinecolor", "tickfont", "tickcolor")


def _strip_theme(figure: dict[str, Any]) -> dict[str, Any]:
    """Drop what the tile owns, so `apply` can decide it again.

    The title goes with the theme keys. A tile already has a caption, so the
    figure's own title is a second heading in a 260px box - and a *stale* one
    the moment someone changes what the chart is about: the caption still said
    "Revenue by region" over a chart of channels. Dropping it here rather than
    in the client also gives the plot the 32px of top margin back, because
    `apply` sizes the margin from whether there is a title.
    """
    layout = {
        key: value
        for key, value in (figure.get("layout") or {}).items()
        if key not in _THEMED_LAYOUT_KEYS and key != "title"
    }
    for key, value in list(layout.items()):
        if key.startswith(("xaxis", "yaxis", "zaxis")) and isinstance(value, dict):
            layout[key] = {k: v for k, v in value.items() if k not in _THEMED_AXIS_KEYS}
    return {"data": figure.get("data") or [], "layout": layout}


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


@router.post("/v1/dashboards/{dashboard_id}/charts")
def add_chart(ctx: router.Context) -> router.Result:
    """Put an existing chart on this dashboard.

    Two writes that have to agree - the chart's `dashboard_id` and the
    dashboard's layout - so they happen in one transaction. Adding the same
    chart twice is a no-op rather than a second tile, because "add to
    dashboard" is a button people press again when they are not sure it worked.
    """
    identity = auth.require(ctx)
    dashboard_id = ctx.params["dashboard_id"]
    chart_id = str(ctx.field("chart_id") or "").strip()
    if not chart_id:
        return router.error(400, "missing_chart_id")

    width = int(ctx.field("w") or 1)
    width = 1 if width < 1 else 3 if width > 3 else width

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT layout FROM dashboards WHERE id = ? AND user_id = ?",
            (dashboard_id, identity.user_id),
        ).fetchone()
        if row is None:
            return router.error(404, "not_found")
        chart = conn.execute(
            "SELECT id FROM charts WHERE id = ? AND user_id = ?",
            (chart_id, identity.user_id),
        ).fetchone()
        if chart is None:
            return router.error(404, "no_such_chart")

        layout = store.load_json(row["layout"], []) or []
        if not any(tile.get("chart_id") == chart_id for tile in layout):
            if len(layout) >= MAX_TILES:
                return router.error(400, "too_many_tiles", f"maximum is {MAX_TILES}")
            layout.append({"chart_id": chart_id, "w": width, "h": 1})
        conn.execute(
            "UPDATE charts SET dashboard_id = ? WHERE id = ?", (dashboard_id, chart_id)
        )
        conn.execute(
            "UPDATE dashboards SET layout = ?, updated_at = ? WHERE id = ?",
            (store.dump_json(layout), time.time(), dashboard_id),
        )

    return router.json_result({"added": True, "layout": layout})


@router.delete("/v1/dashboards/{dashboard_id}/charts/{chart_id}")
def remove_chart(ctx: router.Context) -> router.Result:
    """Take a chart off this dashboard, keeping the chart itself.

    Both halves, in one transaction. Dropping it from the layout alone is not
    a removal: `_dashboard_payload` returns every chart whose `dashboard_id`
    points here, and the view places any it finds without a layout entry - so
    a tile removed and saved came straight back on the next load.
    """
    identity = auth.require(ctx)
    dashboard_id = ctx.params["dashboard_id"]
    chart_id = ctx.params["chart_id"]

    with store.transaction() as conn:
        row = conn.execute(
            "SELECT layout FROM dashboards WHERE id = ? AND user_id = ?",
            (dashboard_id, identity.user_id),
        ).fetchone()
        if row is None:
            return router.error(404, "not_found")

        layout = [
            tile
            for tile in (store.load_json(row["layout"], []) or [])
            if tile.get("chart_id") != chart_id
        ]
        conn.execute(
            "UPDATE charts SET dashboard_id = NULL WHERE id = ? AND user_id = ?"
            " AND dashboard_id = ?",
            (chart_id, identity.user_id, dashboard_id),
        )
        conn.execute(
            "UPDATE dashboards SET layout = ?, updated_at = ? WHERE id = ?",
            (store.dump_json(layout), time.time(), dashboard_id),
        )

    return router.json_result({"removed": True, "layout": layout})


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

    payload = _dashboard_payload(row, mode=ctx.q("mode", "light") or "light")
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
