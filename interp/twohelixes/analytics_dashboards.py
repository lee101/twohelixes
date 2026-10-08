"""Zero-model-cost, per-site analytics dashboards with live SQL aggregates."""

from __future__ import annotations

import time
from typing import Any

from twohelixes import store
from twohelixes.analytics_bots import HUMAN_E

STANDARD_EVENTS = ("page_view", "session_start", "user_engagement", "page_exit", "identify", "alias")
PERSON = "COALESCE(NULLIF(e.user_id, ''), NULLIF(i.user_id, ''), e.client_id)"
AUTH_USER = "COALESCE(NULLIF(e.user_id, ''), NULLIF(i.user_id, ''))"
JOIN = " LEFT JOIN analytics_identities i ON i.site_id = e.site_id AND i.client_id = e.client_id"


def claim(conn: Any, site_id: str) -> bool:
    cursor = conn.execute(
        "INSERT INTO analytics_site_dashboards (site_id, created_at) VALUES (?, ?)"
        " ON CONFLICT (site_id) DO NOTHING", (site_id, time.time()),
    )
    return bool(cursor.rowcount)


def enqueue_for_user(user_id: str) -> None:
    from twohelixes.routes.analytics import _schedule_dashboards

    candidates = []
    with store.transaction() as conn:
        for site in conn.execute("SELECT id FROM analytics_sites WHERE user_id = ?", (user_id,)).fetchall():
            if claim(conn, str(site["id"])):
                candidates.append((str(site["id"]), ""))
    _schedule_dashboards(candidates)


def panels(site_id: str, days: int = 30, *, now: float | None = None) -> dict[str, tuple[str, list[dict], dict]]:
    import pandas as pd

    days = max(1, min(int(days), 365))
    end = time.time() if now is None else now
    start = (int(end) // 86400 - days + 1) * 86400
    params = (site_id, start, end)
    where = " WHERE e.site_id = ? AND e.ts >= ? AND e.ts <= ?" + HUMAN_E
    base = " FROM analytics_events e" + JOIN + where
    totals = store.row_to_dict(store.one(
        "SELECT COUNT(*) AS observed_events, COALESCE(SUM(e.sample_weight), 0) AS events,"
        f" COUNT(DISTINCT {PERSON}) AS users, COUNT(DISTINCT {AUTH_USER}) AS signed_in_users,"
        " COALESCE(SUM(CASE WHEN e.event_name = 'page_view' THEN e.sample_weight ELSE 0 END), 0) AS views"
        + base, params,
    )) or {}
    sessions = store.one(
        "SELECT COUNT(*) AS n FROM (SELECT e.client_id, e.session_id FROM analytics_events e"
        + where + " AND e.session_id != '' GROUP BY e.client_id, e.session_id) s", params,
    )["n"]
    daily = store.rows_to_dicts(store.query(
        "SELECT CAST(FLOOR(e.ts / 86400) AS BIGINT) * 86400 AS day,"
        f" COUNT(DISTINCT {PERSON}) AS users,"
        " COALESCE(SUM(CASE WHEN e.event_name = 'page_view' THEN e.sample_weight ELSE 0 END), 0) AS views"
        + base + " GROUP BY CAST(FLOOR(e.ts / 86400) AS BIGINT) ORDER BY day", params,
    ))
    daily_sessions = {int(row["day"]): int(row["sessions"]) for row in store.query(
        "SELECT day, COUNT(*) AS sessions FROM (SELECT CAST(FLOOR(e.ts / 86400) AS BIGINT) * 86400 AS day,"
        " e.client_id, e.session_id FROM analytics_events e" + where
        + " AND e.session_id != '' GROUP BY CAST(FLOOR(e.ts / 86400) AS BIGINT), e.client_id, e.session_id) s"
        " GROUP BY day", params,
    )}
    day_map = {int(row["day"]): row for row in daily}
    daily = [
        {"date": pd.to_datetime(slot, unit="s", utc=True), "users": day_map.get(slot, {}).get("users", 0),
         "views": day_map.get(slot, {}).get("views", 0), "sessions": daily_sessions.get(slot, 0)}
        for slot in range(start, int(end) // 86400 * 86400 + 1, 86400)
    ]
    result: dict[str, tuple[str, list[dict], dict]] = {}
    for metric, title, value in (
        ("users", "Observed users", totals.get("users", 0)),
        ("views", "Page views (sampling-adjusted)", totals.get("views", 0)),
        ("sessions", "Observed sessions", sessions),
        ("signed_in_users", "Signed-in users", totals.get("signed_in_users", 0)),
    ):
        result[metric] = (title, [{metric: value}], {"chart_type": "stat", "y": metric})
    for metric, title in (("users", "Users over time (UTC)"), ("views", "Page views over time (UTC)"), ("sessions", "Sessions over time (UTC)")):
        result[metric + "_daily"] = (title, daily, {"chart_type": "line", "x": "date", "y": metric})

    for column, title, measure, condition in (
        ("page_path", "Page views by page", "e.sample_weight", " AND e.event_name = 'page_view'"),
        ("device", "Page views by device", "e.sample_weight", " AND e.event_name = 'page_view'"),
        ("referrer_host", "Page views by referrer", "e.sample_weight", " AND e.event_name = 'page_view'"),
        ("utm_source", "Page views by campaign source", "e.sample_weight", " AND e.event_name = 'page_view'"),
        ("event_name", "Custom events (sampling-adjusted)", "e.sample_weight",
         " AND e.event_name NOT IN (" + ",".join("?" for _ in STANDARD_EVENTS) + ")"),
    ):
        extra = STANDARD_EVENTS if column == "event_name" else ()
        key = f"COALESCE(NULLIF(e.{column}, ''), '(unknown)')"
        rows = store.rows_to_dicts(store.query(
            f"SELECT {key} AS category, SUM({measure}) AS value"
            + base + condition + f" GROUP BY {key} ORDER BY value DESC, category LIMIT 16", params + extra,
        ))
        total = store.one("SELECT COALESCE(SUM(" + measure + "), 0) AS n" + base + condition, params + extra)["n"]
        rows = rows[:15]
        remainder = float(total) - sum(float(row["value"]) for row in rows)
        if remainder > 0:
            rows.append({"category": "Other (remaining categories)", "value": remainder})
        if not rows:
            rows = [{"category": "No events in this window", "value": 0}]
        result[column] = (title, rows, {"chart_type": "hbar", "x": "category", "y": "value"})
    users = store.rows_to_dicts(store.query(
        f"SELECT {AUTH_USER} AS user_id, COUNT(*) AS observed_events"
        + base + f" AND {AUTH_USER} IS NOT NULL GROUP BY {AUTH_USER} ORDER BY observed_events DESC LIMIT 15", params,
    ))
    result["user_ids"] = ("Signed-in user IDs (top 15 observed activity)", users or [{"user_id": "No signed-in activity", "observed_events": 0}], {"chart_type": "table"})
    return result


def build(site_id: str, days: int = 30) -> str | None:
    from twohelixes.routes.dashboards import _analytics_chart

    site = store.row_to_dict(store.one("SELECT * FROM analytics_sites WHERE id = ?", (site_id,)))
    if not site:
        return None
    existing = store.one("SELECT dashboard_id FROM analytics_site_dashboards WHERE site_id = ?", (site_id,))
    if existing and existing["dashboard_id"]:
        return str(existing["dashboard_id"])
    prepared = []
    for metric, (title, rows, config) in panels(site_id, days).items():
        figure, args = _analytics_chart(title, rows, config)
        args["_analytics_auto"] = {"site_id": site_id, "metric": metric, "days": days, "domain": site["domain"]}
        prepared.append((metric, title, rows, figure, args))
    now = time.time()
    dashboard_id = store.new_id()
    layout = []
    with store.transaction() as conn:
        claim(conn, site_id)
        won = conn.execute(
            "UPDATE analytics_site_dashboards SET dashboard_id = ? WHERE site_id = ? AND dashboard_id IS NULL",
            (dashboard_id, site_id),
        )
        if not won.rowcount:
            return str(conn.execute("SELECT dashboard_id FROM analytics_site_dashboards WHERE site_id = ?", (site_id,)).fetchone()["dashboard_id"])
        conn.execute(
            "INSERT INTO dashboards (id, user_id, title, layout, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (dashboard_id, site["user_id"], f"{site['domain']} - Analytics overview (last {days} days)", "[]", now, now),
        )
        for index, (_metric, title, rows, figure, args) in enumerate(prepared):
            chart_id = store.new_id()
            conn.execute(
                "INSERT INTO charts (id, user_id, dashboard_id, title, query, spec, graph_args, rows_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chart_id, site["user_id"], dashboard_id, title, f"{title} for {site['domain']}",
                 store.dump_json(figure), store.dump_json(args), store.dump_json(rows), now, now),
            )
            layout.append({"chart_id": chart_id, "w": 1 if index < 4 else 2, "h": 1, "x": index % 3, "y": index // 3})
        conn.execute("UPDATE dashboards SET layout = ? WHERE id = ?", (store.dump_json(layout), dashboard_id))
    return dashboard_id


def refresh(dashboard_id: str) -> int:
    from twohelixes.pipeline import figures
    from twohelixes.charts import defaults

    cached = {}
    updated = 0
    for chart in store.rows_to_dicts(store.query("SELECT * FROM charts WHERE dashboard_id = ?", (dashboard_id,))):
        config = store.load_json(chart["graph_args"], {})
        source = config.get("_analytics_auto") or {}
        if not source:
            continue
        key = (source["site_id"], int(source.get("days", 30)))
        if key not in cached:
            cached[key] = panels(*key)
        _title, rows, _args = cached[key][source["metric"]]
        import pandas as pd

        frame = pd.DataFrame(rows)
        figure, _warnings = figures.build(frame, config)
        figure = defaults.apply(figure, chart_type=config["chart_type"])
        store.execute("UPDATE charts SET spec = ?, rows_json = ?, updated_at = ? WHERE id = ?",
                      (store.dump_json(figure), store.dump_json(rows), time.time(), chart["id"]))
        updated += 1
    return updated


def refresh_if_stale(dashboard_id: str) -> None:
    import threading

    row = store.one(
        "SELECT MIN(c.updated_at) AS updated FROM charts c"
        " JOIN analytics_site_dashboards a ON a.dashboard_id = c.dashboard_id WHERE c.dashboard_id = ?",
        (dashboard_id,),
    )
    if not row or row["updated"] is None or float(row["updated"]) > time.time() - 300:
        return
    if not _refresh_lock.acquire(blocking=False):
        return
    try:
        refresh(dashboard_id)
    finally:
        _refresh_lock.release()


import threading

_refresh_lock = threading.Lock()
