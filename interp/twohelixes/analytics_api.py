"""Compact reporting for CLI clients that do not need dimension breakdowns."""

from __future__ import annotations

import logging

from twohelixes import store
from twohelixes.analytics_bots import HUMAN, HUMAN_E, human_only_since, human_only_sql


def human_scope(site_id: str, start: float, end: float, *, human_only: bool) -> tuple[float, str, str, dict]:
    """Window start and the extra session predicates for `human_only`, plus
    the note reports echo so the narrowing is never silent."""
    since = human_only_since(site_id) if human_only else None
    note = {"requested": human_only, "applied": since is not None, "since": since}
    if since is None:
        return start, "", "", note
    start = max(start, since)
    return start, human_only_sql(site_id, start, end), human_only_sql(site_id, start, end, "e"), note


log = logging.getLogger(__name__)

WORK_MEM = "128MB"


def compact_summary(
    site_id: str, start: float, end: float, *, include_bots: bool = False, human_only: bool = False
) -> dict:
    scope = human_scope(site_id, start, end, human_only=human_only)
    conn = store.connection()
    postgres = store.is_postgres()
    if postgres:
        conn.execute("BEGIN")
        conn.execute("SET LOCAL jit = off")
        conn.execute(f"SET LOCAL work_mem = '{WORK_MEM}'")
    try:
        return _compact_summary(conn, site_id, end, include_bots, scope)
    finally:
        if postgres:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                log.exception("could not end compact summary transaction")


def _one(conn, sql: str, params: tuple) -> dict:
    return store.row_to_dict(conn.execute(sql, params).fetchone()) or {}


def _compact_summary(conn, site_id: str, end: float, include_bots: bool, scope: tuple) -> dict:
    start, only, only_e, note = scope
    params = (site_id, start, end)
    human, human_e = ("", "") if include_bots else (HUMAN, HUMAN_E)
    human, human_e = human + only, human_e + only_e
    totals = _one(
        conn,
        "SELECT COUNT(*) AS observed_events,"
        " COALESCE(SUM(COALESCE(e.sample_weight, 1)), 0) AS events,"
        " COUNT(DISTINCT COALESCE(NULLIF(e.user_id, ''), NULLIF(i.user_id, ''), e.client_id)) AS users,"
        " COALESCE(SUM(CASE WHEN e.event_name = 'page_view'"
        " THEN COALESCE(e.sample_weight, 1) ELSE 0 END), 0) AS page_views,"
        " COALESCE(SUM(e.engagement_ms), 0) AS engagement_ms"
        " FROM analytics_events e LEFT JOIN analytics_identities i"
        " ON i.site_id = e.site_id AND i.client_id = e.client_id"
        " WHERE e.site_id = ? AND e.ts BETWEEN ? AND ?" + human_e, params,
    )
    sessions = _one(
        conn,
        "SELECT COUNT(*) AS sessions, COALESCE(SUM(CASE WHEN engagement_ms >= 10000"
        " OR page_views >= 2 OR has_key_event = 1 THEN 1 ELSE 0 END), 0) AS engaged_sessions"
        " FROM (SELECT client_id, session_id,"
        " SUM(CASE WHEN event_name = 'page_view' THEN 1 ELSE 0 END) AS page_views,"
        " SUM(engagement_ms) AS engagement_ms,"
        " MAX(CASE WHEN event_name IN ('purchase', 'sign_up', 'generate_lead', 'conversion')"
        " THEN 1 ELSE 0 END) AS has_key_event"
        f" FROM analytics_events WHERE site_id = ? AND ts BETWEEN ? AND ? AND session_id != ''{human}"
        " GROUP BY client_id, session_id) s", params,
    )
    n = int(sessions.get("sessions") or 0)
    engaged = int(sessions.get("engaged_sessions") or 0)
    return {
        "site_id": site_id,
        "window": {"start": start, "end": end},
        "human_only": note,
        "totals": {
            "events": round(float(totals.get("events") or 0)),
            "observed_events": int(totals.get("observed_events") or 0),
            "users": int(totals.get("users") or 0),
            "sessions": n,
            "page_views": round(float(totals.get("page_views") or 0)),
            "engagement_ms": int(totals.get("engagement_ms") or 0),
            "engaged_sessions": engaged,
            "bounces": n - engaged,
            "bounce_rate": (n - engaged) / n if n else 0.0,
        },
    }
