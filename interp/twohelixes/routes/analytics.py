"""Site analytics: ingest and reporting.

Ingest is deliberately forgiving. A tracker that 400s on an unexpected field
loses the whole batch, and the page it runs on gains nothing from being told -
so unknown keys land in `props`, bad rows are skipped rather than failing the
batch, and every response is a 204 the browser can ignore.

Three wire formats reach the same table:

* the native batch (`POST /v1/collect`, `{"site_id": ..., "events": [...]}`)
* Segment-style calls (`type: page|track|identify|group|alias`)
* GA4 Measurement Protocol style query strings (`GET /v1/collect?en=page_view`),
  which is what the no-JS pixel and server-side callers use.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from twohelixes import router, store
from twohelixes.router import Context, Result, error, json_result

log = logging.getLogger("twohelixes.analytics")

MAX_EVENTS_PER_BATCH = 50
MAX_STRING = 1024
MAX_PROPS_BYTES = 8192
SESSION_GAP_SECONDS = 30 * 60

# A 1x1 transparent GIF for the no-JS pixel.
PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)

_SEGMENT_EVENT = {
    "page": "page_view",
    "screen": "page_view",
    "track": "",
    "identify": "identify",
    "group": "group",
    "alias": "alias",
}


def _clip(value: Any, limit: int = MAX_STRING) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit]


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        return urlsplit(url).netloc.lower()[:255]
    except ValueError:
        return ""


def _path_of(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    path = parts.path or "/"
    return path[:MAX_STRING]


def _ip_hash(ip: str, site_id: str) -> str:
    """Salted per site and per day: enough to count uniques, useless as an ID."""
    if not ip:
        return ""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    return hashlib.sha256(f"{site_id}|{day}|{ip}".encode()).hexdigest()[:32]


_BROWSERS = (
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("Chrome/", "Chrome"),
    ("Firefox/", "Firefox"),
    ("Safari/", "Safari"),
)
_OSES = (
    ("Android", "Android"),
    ("iPhone", "iOS"),
    ("iPad", "iOS"),
    ("Mac OS X", "macOS"),
    ("Windows", "Windows"),
    ("Linux", "Linux"),
    ("CrOS", "ChromeOS"),
)
_BOT = re.compile(r"bot|crawler|spider|crawling|preview|monitor|curl|wget|python-requests", re.I)


def _parse_ua(ua: str) -> tuple[str, str, str, bool]:
    """(device, browser, os, is_bot). Deliberately coarse - no UA database."""
    if not ua:
        return "unknown", "unknown", "unknown", False
    is_bot = bool(_BOT.search(ua))
    browser = "other"
    for needle, name in _BROWSERS:
        if needle in ua:
            browser = name
            break
    os_name = "other"
    for needle, name in _OSES:
        if needle in ua:
            os_name = name
            break
    if "iPad" in ua or "Tablet" in ua:
        device = "tablet"
    elif "Mobi" in ua or "Android" in ua or "iPhone" in ua:
        device = "mobile"
    else:
        device = "desktop"
    return device, browser, os_name, is_bot


def _props_json(raw: Any) -> str:
    if not raw:
        return ""
    try:
        text = json.dumps(raw, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        return ""
    if len(text) > MAX_PROPS_BYTES:
        return text[:MAX_PROPS_BYTES]
    return text


def _session_for(site_id: str, client_id: str, given: str, now: float) -> str:
    """Honour the tracker's session id; derive one when it is missing.

    A GA-style 30 minute inactivity window, evaluated against the last event we
    stored for this client rather than against a cookie, so the pixel and
    server-side callers get sessionisation too.
    """
    if given:
        return given[:64]
    row = store.one(
        "SELECT session_id, ts FROM analytics_events WHERE site_id = ? AND client_id = ?"
        " ORDER BY ts DESC LIMIT 1",
        (site_id, client_id),
    )
    data = store.row_to_dict(row)
    if data and now - float(data["ts"] or 0) < SESSION_GAP_SECONDS:
        return str(data["session_id"])
    return store.new_id()


def _normalise(event: dict[str, Any], ctx: Context, site_id: str, now: float) -> dict[str, Any] | None:
    kind = _clip(event.get("type"), 32).lower()
    name = _clip(event.get("event") or event.get("name") or event.get("en"), 64)
    if kind in _SEGMENT_EVENT and not name:
        name = _SEGMENT_EVENT[kind]
    if not name:
        name = "page_view" if kind in ("page", "screen") else ""
    if not name:
        return None

    client_id = _clip(
        event.get("client_id") or event.get("anonymousId") or event.get("cid"), 64
    )
    if not client_id:
        return None

    page_location = _clip(event.get("page_location") or event.get("url") or event.get("dl"))
    props = event.get("props") or event.get("properties") or event.get("traits") or {}
    if not isinstance(props, dict):
        props = {"value": props}

    ua = ctx.header("user-agent")
    device, browser, os_name, is_bot = _parse_ua(ua)
    if is_bot:
        return None

    ts = event.get("ts")
    try:
        ts = float(ts) / 1000.0 if ts and float(ts) > 1e11 else float(ts or now)
    except (TypeError, ValueError):
        ts = now
    # Never trust a client clock far from ours.
    if abs(ts - now) > 86400:
        ts = now

    referrer = _clip(event.get("referrer") or event.get("dr"))
    return {
        "id": store.new_id(),
        "site_id": site_id,
        "client_id": client_id,
        "session_id": _session_for(site_id, client_id, _clip(event.get("session_id") or event.get("sid"), 64), now),
        "user_id": _clip(event.get("user_id") or event.get("userId"), 64) or None,
        "event_name": name,
        "ts": ts,
        "received_at": now,
        "page_location": page_location,
        "page_path": _clip(event.get("page_path")) or _path_of(page_location),
        "page_title": _clip(event.get("page_title") or event.get("dt"), 255),
        "referrer": referrer,
        "referrer_host": _host_of(referrer),
        "utm_source": _clip(event.get("utm_source"), 128),
        "utm_medium": _clip(event.get("utm_medium"), 128),
        "utm_campaign": _clip(event.get("utm_campaign"), 128),
        "utm_term": _clip(event.get("utm_term"), 128),
        "utm_content": _clip(event.get("utm_content"), 128),
        "device": device,
        "browser": browser,
        "os": os_name,
        "screen": _clip(event.get("screen"), 32),
        "viewport": _clip(event.get("viewport"), 32),
        "language": _clip(event.get("language") or event.get("ul"), 32),
        "country": _clip(ctx.header("cf-ipcountry"), 8),
        "ip_hash": _ip_hash(ctx.client_ip, site_id),
        "engagement_ms": _int(event.get("engagement_ms") or event.get("_et")),
        "props": _props_json(props),
        "_kind": kind,
        "_traits": event.get("traits") if isinstance(event.get("traits"), dict) else None,
    }


def _int(value: Any) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, 86_400_000))


_COLUMNS = (
    "id site_id client_id session_id user_id event_name ts received_at page_location page_path"
    " page_title referrer referrer_host utm_source utm_medium utm_campaign utm_term utm_content"
    " device browser os screen viewport language country ip_hash engagement_ms props"
).split()


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    """Loud the first time, quiet after: a broken ingest must not hide, and it
    must not fill the disk either."""
    if message not in _warned:
        _warned.add(message)
        log.warning(message, exc_info=True)
    else:
        log.debug(message, exc_info=True)


def _store(rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    placeholders = ", ".join("?" for _ in _COLUMNS)
    # The connection adapts `?` to the driver's paramstyle itself; adapting here
    # too produced valid-looking SQL that Postgres rejected, and the per-row
    # except swallowed it - every event was dropped while ingest still said 204.
    sql = f"INSERT INTO analytics_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
    written = 0
    with store.transaction() as conn:
        for row in rows:
            try:
                conn.execute(sql, tuple(row[c] for c in _COLUMNS))
                written += 1
            except Exception:  # one bad row must not lose the batch
                _warn_once("analytics: row rejected")
            if row.get("_kind") in ("identify", "alias") and row.get("user_id"):
                _upsert_identity(conn, row)
    return written


def _upsert_identity(conn: Any, row: dict[str, Any]) -> None:
    traits = _props_json(row.get("_traits") or {})
    try:
        existing = conn.execute(
            "SELECT client_id FROM analytics_identities WHERE site_id = ? AND client_id = ?",
            (row["site_id"], row["client_id"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE analytics_identities SET user_id = ?, traits = ?, last_seen = ?"
                " WHERE site_id = ? AND client_id = ?",
                (row["user_id"], traits, row["ts"], row["site_id"], row["client_id"]),
            )
        else:
            conn.execute(
                "INSERT INTO analytics_identities (site_id, client_id, user_id, traits,"
                " first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                (row["site_id"], row["client_id"], row["user_id"], traits, row["ts"], row["ts"]),
            )
    except Exception:
        _warn_once("analytics: identity upsert failed")


def _site_id(ctx: Context, payload: Any) -> str:
    if isinstance(payload, dict):
        candidate = payload.get("site_id") or payload.get("tid") or payload.get("writeKey")
        if candidate:
            return _clip(candidate, 64)
    candidate = ctx.q("site_id") or ctx.q("tid")
    if candidate:
        return _clip(candidate, 64)
    origin = ctx.header("origin") or ctx.header("referer")
    return _host_of(origin) or "unknown"


def _cors(ctx: Context) -> dict[str, str]:
    origin = ctx.header("origin") or "*"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


@router.post("/v1/collect")
def collect(ctx: Context) -> Result:
    now = time.time()
    payload = ctx.json
    if payload is None and ctx.raw_body:
        # sendBeacon sends text/plain; the body is still JSON.
        try:
            payload = json.loads(ctx.raw_body)
        except (ValueError, TypeError):
            payload = None

    events: list[Any] = []
    if isinstance(payload, dict):
        raw = payload.get("events") or payload.get("batch")
        if isinstance(raw, list):
            events = raw
        else:
            events = [payload]
    elif isinstance(payload, list):
        events = payload

    site_id = _site_id(ctx, payload if isinstance(payload, dict) else {})
    rows: list[dict[str, Any]] = []
    for event in events[:MAX_EVENTS_PER_BATCH]:
        if not isinstance(event, dict):
            continue
        merged = dict(event)
        if isinstance(payload, dict):
            for shared in ("client_id", "anonymousId", "session_id", "user_id", "userId", "screen", "viewport", "language"):
                merged.setdefault(shared, payload.get(shared))
        normalised = _normalise(merged, ctx, site_id, now)
        if normalised:
            rows.append(normalised)

    written = _store(rows)
    return Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx)) if written or not rows else Result(
        status=204, body=None, content_type="text/plain", headers=_cors(ctx)
    )


@router.route("OPTIONS", "/v1/collect")
def collect_preflight(ctx: Context) -> Result:
    return Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx))


@router.get("/v1/collect")
def collect_pixel(ctx: Context) -> Result:
    """GA4-Measurement-Protocol-shaped GET. Always answers with a 1x1 GIF."""
    now = time.time()
    flat = {key: values[0] for key, values in ctx.query.items() if values}
    site_id = _site_id(ctx, flat)
    props = {k[3:]: v for k, v in flat.items() if k.startswith("ep.") or k.startswith("epn")}
    flat["props"] = props
    row = _normalise(flat, ctx, site_id, now)
    if row:
        _store([row])
    headers = _cors(ctx)
    headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return Result(
        status=200,
        body=PIXEL.decode("latin-1"),
        content_type="image/gif",
        headers=headers,
    )


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def _window(ctx: Context) -> tuple[float, float]:
    days = max(1, min(ctx.q_int("days", 7), 365))
    end = time.time()
    return end - days * 86400, end


@router.get("/v1/analytics/summary")
def summary(ctx: Context) -> Result:
    site_id = _clip(ctx.q("site_id", ""), 64)
    if not site_id:
        return error(400, "site_id_required")
    start, end = _window(ctx)

    totals = store.row_to_dict(
        store.one(
            "SELECT COUNT(*) AS events, COUNT(DISTINCT client_id) AS users,"
            " COUNT(DISTINCT session_id) AS sessions,"
            " SUM(CASE WHEN event_name = 'page_view' THEN 1 ELSE 0 END) AS page_views,"
            " SUM(engagement_ms) AS engagement_ms"
            " FROM analytics_events WHERE site_id = ? AND ts BETWEEN ? AND ?",
            (site_id, start, end),
        )
    ) or {}

    def group(column: str, limit: int = 15) -> list[dict[str, Any]]:
        rows = store.query(
            f"SELECT {column} AS key, COUNT(*) AS events, COUNT(DISTINCT client_id) AS users"
            " FROM analytics_events WHERE site_id = ? AND ts BETWEEN ? AND ?"
            f" AND {column} IS NOT NULL AND {column} != ''"
            f" GROUP BY {column} ORDER BY events DESC LIMIT {limit}",
            (site_id, start, end),
        )
        return store.rows_to_dicts(rows)

    return json_result(
        {
            "site_id": site_id,
            "window": {"start": start, "end": end},
            "totals": {
                "events": int(totals.get("events") or 0),
                "users": int(totals.get("users") or 0),
                "sessions": int(totals.get("sessions") or 0),
                "page_views": int(totals.get("page_views") or 0),
                "engagement_ms": int(totals.get("engagement_ms") or 0),
            },
            "top_pages": group("page_path"),
            "top_events": group("event_name"),
            "referrers": group("referrer_host"),
            "devices": group("device", 5),
            "browsers": group("browser", 8),
            "countries": group("country", 15),
            "campaigns": group("utm_campaign"),
        }
    )


@router.get("/v1/analytics/events")
def recent_events(ctx: Context) -> Result:
    site_id = _clip(ctx.q("site_id", ""), 64)
    if not site_id:
        return error(400, "site_id_required")
    start, end = _window(ctx)
    limit = max(1, min(ctx.q_int("limit", 100), 1000))
    rows = store.query(
        "SELECT event_name, ts, page_path, referrer_host, device, browser, country, client_id,"
        " session_id, user_id, props FROM analytics_events"
        " WHERE site_id = ? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT ?",
        (site_id, start, end, limit),
    )
    events = store.rows_to_dicts(rows)
    for event in events:
        event["props"] = store.load_json(event.get("props"), {})
    return json_result({"site_id": site_id, "events": events})


@router.get("/v1/analytics/timeseries")
def timeseries(ctx: Context) -> Result:
    """Hourly buckets, computed in Python so both backends behave identically."""
    site_id = _clip(ctx.q("site_id", ""), 64)
    if not site_id:
        return error(400, "site_id_required")
    start, end = _window(ctx)
    bucket_seconds = 3600 if (end - start) <= 8 * 86400 else 86400

    rows = store.query(
        "SELECT ts, client_id, event_name FROM analytics_events"
        " WHERE site_id = ? AND ts BETWEEN ? AND ?",
        (site_id, start, end),
    )
    buckets: dict[int, dict[str, Any]] = {}
    for row in store.rows_to_dicts(rows):
        slot = int(float(row["ts"]) // bucket_seconds * bucket_seconds)
        entry = buckets.setdefault(slot, {"ts": slot, "events": 0, "page_views": 0, "_users": set()})
        entry["events"] += 1
        if row["event_name"] == "page_view":
            entry["page_views"] += 1
        entry["_users"].add(row["client_id"])

    series = []
    for slot in sorted(buckets):
        entry = buckets[slot]
        series.append(
            {
                "ts": entry["ts"],
                "events": entry["events"],
                "page_views": entry["page_views"],
                "users": len(entry["_users"]),
            }
        )
    return json_result({"site_id": site_id, "bucket_seconds": bucket_seconds, "series": series})


@router.get("/v1/analytics/export")
def export_events(ctx: Context) -> Result:
    """Segment-shaped export, for syncing into a warehouse."""
    site_id = _clip(ctx.q("site_id", ""), 64)
    if not site_id:
        return error(400, "site_id_required")
    start, end = _window(ctx)
    limit = max(1, min(ctx.q_int("limit", 1000), 10000))
    rows = store.query(
        "SELECT * FROM analytics_events WHERE site_id = ? AND ts BETWEEN ? AND ?"
        " ORDER BY ts ASC LIMIT ?",
        (site_id, start, end, limit),
    )
    out = []
    for row in store.rows_to_dicts(rows):
        out.append(
            {
                "type": "page" if row["event_name"] == "page_view" else "track",
                "event": row["event_name"],
                "anonymousId": row["client_id"],
                "userId": row["user_id"],
                "timestamp": row["ts"],
                "context": {
                    "page": {
                        "path": row["page_path"],
                        "url": row["page_location"],
                        "title": row["page_title"],
                        "referrer": row["referrer"],
                    },
                    "campaign": {
                        "source": row["utm_source"],
                        "medium": row["utm_medium"],
                        "name": row["utm_campaign"],
                    },
                    "locale": row["language"],
                    "userAgent": {"device": row["device"], "browser": row["browser"], "os": row["os"]},
                },
                "properties": store.load_json(row.get("props"), {}),
            }
        )
    return json_result({"site_id": site_id, "batch": out})
