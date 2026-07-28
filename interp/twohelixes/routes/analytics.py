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
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from twohelixes import auth, config, router, store
from twohelixes.router import Context, Result, error, json_result
from twohelixes.routes import teams

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


def _session_for(
    site_id: str,
    client_id: str,
    given: str,
    event_ts: float,
    cache: dict[str, tuple[str, float, bool]],
) -> tuple[str, bool]:
    """Honour the tracker's session id; derive one when it is missing.

    A GA-style 30 minute inactivity window, evaluated against the last event we
    stored for this client rather than against a cookie, so the pixel and
    server-side callers get sessionisation too.
    """
    cached = cache.get(client_id)
    if given:
        session_id = given[:64]
        if cached and cached[0] == session_id:
            cache[client_id] = (session_id, max(cached[1], event_ts), False)
            return session_id, False
        exists = store.one(
            "SELECT 1 FROM analytics_events WHERE site_id = ? AND client_id = ?"
            " AND session_id = ? LIMIT 1",
            (site_id, client_id, session_id),
        )
        is_new = exists is None
        cache[client_id] = (session_id, event_ts, is_new)
        return session_id, is_new
    if cached and event_ts - cached[1] < SESSION_GAP_SECONDS:
        cache[client_id] = (cached[0], max(cached[1], event_ts), False)
        return cached[0], False
    row = store.one(
        "SELECT session_id, ts FROM analytics_events WHERE site_id = ? AND client_id = ?"
        " AND ts <= ? ORDER BY ts DESC LIMIT 1",
        (site_id, client_id, event_ts),
    )
    data = store.row_to_dict(row)
    if data and event_ts - float(data["ts"] or 0) < SESSION_GAP_SECONDS:
        session_id = str(data["session_id"])
        cache[client_id] = (session_id, event_ts, False)
        return session_id, False
    session_id = store.new_id()
    cache[client_id] = (session_id, event_ts, True)
    return session_id, True


def _normalise(
    event: dict[str, Any],
    ctx: Context,
    site: dict[str, Any],
    now: float,
    session_cache: dict[str, tuple[str, float, bool]],
) -> dict[str, Any] | None:
    site_id = str(site["id"])
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

    props = (
        event.get("props")
        or event.get("properties")
        or event.get("params")
        or event.get("traits")
        or {}
    )
    if not isinstance(props, dict):
        props = {"value": props}
    context = event.get("context") if isinstance(event.get("context"), dict) else {}
    page = context.get("page") if isinstance(context.get("page"), dict) else {}
    campaign = context.get("campaign") if isinstance(context.get("campaign"), dict) else {}
    page_location = _clip(
        event.get("page_location")
        or event.get("url")
        or event.get("dl")
        or props.get("page_location")
        or page.get("url")
    )

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

    session_id, session_start = _session_for(
        site_id,
        client_id,
        _clip(
            event.get("session_id")
            or event.get("sid")
            or props.get("session_id")
            or props.get("ga_session_id"),
            64,
        ),
        ts,
        session_cache,
    )
    page_query: dict[str, list[str]] = {}
    try:
        page_query = parse_qs(urlsplit(page_location).query)
    except ValueError:
        pass

    def campaign_value(native: str, ga: str, segment: str) -> str:
        query_value = (page_query.get(native) or [""])[0]
        return _clip(
            event.get(native)
            or event.get(ga)
            or props.get(native)
            or campaign.get(segment)
            or query_value,
            128,
        )

    referrer = _clip(
        event.get("referrer") or event.get("dr") or props.get("page_referrer") or page.get("referrer")
    )
    referrer_host = _host_of(referrer)
    page_host = _host_of(page_location)
    site_domain = str(site.get("domain") or "").lower()
    if referrer_host and referrer_host in {page_host, site_domain, f"www.{site_domain}"}:
        referrer = ""
        referrer_host = ""

    attribution = {
        "utm_source": campaign_value("utm_source", "cs", "source"),
        "utm_medium": campaign_value("utm_medium", "cm", "medium"),
        "utm_campaign": campaign_value("utm_campaign", "cn", "name"),
        "utm_term": campaign_value("utm_term", "ck", "term"),
        "utm_content": campaign_value("utm_content", "cc", "content"),
    }
    if not any(attribution.values()) and not session_start:
        previous = store.row_to_dict(
            store.one(
                "SELECT utm_source, utm_medium, utm_campaign, utm_term, utm_content"
                " FROM analytics_events WHERE site_id = ? AND session_id = ?"
                " ORDER BY ts ASC LIMIT 1",
                (site_id, session_id),
            )
        )
        if previous:
            attribution = {key: _clip(previous.get(key), 128) for key in attribution}

    return {
        "id": store.new_id(),
        "site_id": site_id,
        "client_id": client_id,
        "session_id": session_id,
        "user_id": _clip(
            event.get("user_id") or event.get("userId") or event.get("uid"), 64
        )
        or None,
        "event_name": name,
        "ts": ts,
        "received_at": now,
        "page_location": page_location,
        "page_path": _clip(event.get("page_path") or props.get("page_path") or page.get("path")) or _path_of(page_location),
        "page_title": _clip(
            event.get("page_title") or event.get("dt") or props.get("page_title") or page.get("title"),
            255,
        ),
        "referrer": referrer,
        "referrer_host": referrer_host,
        **attribution,
        "device": device,
        "browser": browser,
        "os": os_name,
        "screen": _clip(event.get("screen") or event.get("sr"), 32),
        "viewport": _clip(event.get("viewport"), 32),
        "language": _clip(event.get("language") or event.get("ul") or props.get("language"), 32),
        "country": _clip(ctx.header("cf-ipcountry"), 8),
        "ip_hash": _ip_hash(ctx.client_ip, site_id),
        "engagement_ms": _int(
            event.get("engagement_ms")
            or event.get("_et")
            or props.get("engagement_time_msec")
            or props.get("engagement_ms")
        ),
        "props": _props_json(props),
        "_kind": kind,
        "_traits": event.get("traits") if isinstance(event.get("traits"), dict) else None,
        "_session_start": session_start,
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
_dashboard_build_lock = threading.Lock()


def _warn_once(message: str) -> None:
    """Loud the first time, quiet after: a broken ingest must not hide, and it
    must not fill the disk either."""
    if message not in _warned:
        _warned.add(message)
        log.warning(message, exc_info=True)
    else:
        log.debug(message, exc_info=True)


def _store(rows: list[dict[str, Any]]) -> tuple[int, list[tuple[str, str]]]:
    if not rows:
        return 0, []
    placeholders = ", ".join("?" for _ in _COLUMNS)
    # The connection adapts `?` to the driver's paramstyle itself; adapting here
    # too produced valid-looking SQL that Postgres rejected, and the per-row
    # except swallowed it - every event was dropped while ingest still said 204.
    sql = f"INSERT INTO analytics_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
    written = 0
    claimed: list[tuple[str, str]] = []
    with store.transaction() as conn:
        for row in rows:
            inserted = False
            try:
                conn.execute(sql, tuple(row[c] for c in _COLUMNS))
                written += 1
                inserted = True
            except Exception:  # one bad row must not lose the batch
                _warn_once("analytics: row rejected")
            if not inserted:
                continue
            if row.get("_kind") in ("identify", "alias") and row.get("user_id"):
                _upsert_identity(conn, row)
            try:
                cursor = conn.execute(
                    "INSERT INTO analytics_event_dashboards"
                    " (site_id, event_name, status, claimed_at)"
                    " VALUES (?, ?, 'pending', ?)"
                    " ON CONFLICT (site_id, event_name) DO NOTHING",
                    (row["site_id"], row["event_name"], time.time()),
                )
                if cursor.rowcount:
                    claimed.append((row["site_id"], row["event_name"]))
            except Exception:
                _warn_once("analytics: dashboard claim failed")
    return written, claimed


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


def _site_token(ctx: Context, payload: Any) -> str:
    if isinstance(payload, dict):
        candidate = payload.get("site_id") or payload.get("tid") or payload.get("writeKey")
        if candidate:
            return _clip(candidate, 128)
    candidate = ctx.q("site_id") or ctx.q("tid")
    if candidate:
        return _clip(candidate, 128)
    return ""


def _site_for_write(ctx: Context, payload: Any) -> dict[str, Any] | None:
    token = _site_token(ctx, payload)
    if not token:
        return None
    return store.row_to_dict(
        store.one(
            "SELECT id, user_id, domain, name FROM analytics_sites"
            " WHERE id = ? OR write_key = ?",
            (token, token),
        )
    )


def _site_payload(row: Any) -> dict[str, Any]:
    site = store.row_to_dict(row) or {}
    write_key = str(site.get("write_key") or "")
    endpoint = f"{config.site_url().rstrip('/')}/v1/collect"
    site["snippet"] = (
        "<script>window.__thConfig="
        + json.dumps({"siteId": write_key, "endpoint": endpoint}, separators=(",", ":"))
        + ";</script>\n"
        + f'<script async src="{config.site_url().rstrip("/")}/static/th.js"></script>'
    )
    return site


def _domain(value: Any) -> str:
    raw = _clip(value, 255).lower()
    if "://" in raw:
        raw = _host_of(raw)
    return raw.removeprefix("www.").split(":", 1)[0].strip(".")


def _site_for_read(ctx: Context) -> tuple[dict[str, Any] | None, Result | None]:
    identity = auth.require(ctx)
    site_id = _clip(ctx.q("site_id", ""), 64)
    if not site_id:
        return None, error(400, "site_id_required")
    site = store.row_to_dict(
        store.one("SELECT * FROM analytics_sites WHERE id = ?", (site_id,))
    )
    if site is None:
        return None, error(404, "site_not_found")
    if not teams.can_read(identity.user_id, "analytics_site", site_id):
        return None, error(403, "forbidden")
    return site, None


@router.get("/v1/analytics/sites")
def list_sites(ctx: Context) -> Result:
    identity = auth.require(ctx)
    teams.ensure_schema()
    rows = store.query(
        "SELECT DISTINCT s.* FROM analytics_sites s"
        " LEFT JOIN team_objects o ON o.kind = 'analytics_site' AND o.object_id = s.id"
        " LEFT JOIN team_members m ON m.team_id = o.team_id AND m.user_id = ?"
        " WHERE s.user_id = ? OR m.user_id = ? ORDER BY s.created_at DESC",
        (identity.user_id, identity.user_id, identity.user_id),
    )
    return json_result({"sites": [_site_payload(row) for row in rows]})


@router.post("/v1/analytics/sites")
def create_site(ctx: Context) -> Result:
    identity = auth.require(ctx)
    domain = _domain(ctx.field("domain"))
    if not domain or "." not in domain or " " in domain:
        return error(400, "invalid_domain")
    name = _clip(ctx.field("name"), 120) or domain
    site_id = store.new_id()
    write_key = f"thw_{secrets.token_urlsafe(24)}"
    now = time.time()
    try:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO analytics_sites"
                " (id, user_id, domain, name, write_key, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (site_id, identity.user_id, domain, name, write_key, now),
            )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            return error(409, "site_already_exists")
        raise
    row = store.one("SELECT * FROM analytics_sites WHERE id = ?", (site_id,))
    return json_result(_site_payload(row), status=201)


@router.patch("/v1/analytics/sites/{site_id}")
def update_site(ctx: Context) -> Result:
    identity = auth.require(ctx)
    site_id = ctx.params["site_id"]
    if not teams.can_write(identity.user_id, "analytics_site", site_id):
        return error(403, "forbidden")
    fields: list[str] = []
    values: list[Any] = []
    if ctx.field("domain") is not None:
        domain = _domain(ctx.field("domain"))
        if not domain or "." not in domain or " " in domain:
            return error(400, "invalid_domain")
        fields.append("domain = ?")
        values.append(domain)
    if ctx.field("name") is not None:
        name = _clip(ctx.field("name"), 120)
        if not name:
            return error(400, "name_required")
        fields.append("name = ?")
        values.append(name)
    if fields:
        values.append(site_id)
        try:
            with store.transaction() as conn:
                conn.execute(
                    f"UPDATE analytics_sites SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
        except Exception as exc:
            if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
                return error(409, "site_already_exists")
            raise
    return json_result(_site_payload(store.one("SELECT * FROM analytics_sites WHERE id = ?", (site_id,))))


@router.delete("/v1/analytics/sites/{site_id}")
def delete_site(ctx: Context) -> Result:
    identity = auth.require(ctx)
    site_id = ctx.params["site_id"]
    if not teams.can_write(identity.user_id, "analytics_site", site_id):
        return error(403, "forbidden")
    dashboards = store.query(
        "SELECT dashboard_id FROM analytics_event_dashboards"
        " WHERE site_id = ? AND dashboard_id IS NOT NULL",
        (site_id,),
    )
    dashboard_ids = [str(row["dashboard_id"]) for row in dashboards]
    busy = store.one(
        "SELECT 1 FROM analytics_event_dashboards"
        " WHERE site_id = ? AND status IN ('pending', 'building') LIMIT 1",
        (site_id,),
    )
    if busy:
        return error(409, "site_busy_try_again")
    with store.transaction() as conn:
        conn.execute("DELETE FROM analytics_event_dashboards WHERE site_id = ?", (site_id,))
        conn.execute(
            "DELETE FROM team_objects WHERE kind = 'analytics_site' AND object_id = ?",
            (site_id,),
        )
        conn.execute("DELETE FROM analytics_sites WHERE id = ?", (site_id,))
    for offset in range(0, len(dashboard_ids), 100):
        batch = dashboard_ids[offset : offset + 100]
        placeholders = ", ".join("?" for _ in batch)
        with store.transaction() as conn:
            conn.execute(
                f"DELETE FROM charts WHERE dashboard_id IN ({placeholders})", batch
            )
            conn.execute(
                f"DELETE FROM dashboards WHERE id IN ({placeholders})", batch
            )
    _delete_analytics_rows("analytics_events", "id", site_id)
    _delete_analytics_rows("analytics_identities", "client_id", site_id)
    return json_result({"deleted": True})


def _delete_analytics_rows(table: str, key: str, site_id: str) -> None:
    while True:
        rows = store.query(
            f"SELECT {key} FROM {table} WHERE site_id = ? LIMIT 1000", (site_id,)
        )
        values = [row[key] for row in rows]
        if not values:
            return
        placeholders = ", ".join("?" for _ in values)
        with store.transaction() as conn:
            conn.execute(
                f"DELETE FROM {table} WHERE site_id = ?"
                f" AND {key} IN ({placeholders})",
                (site_id, *values),
            )


def _cors(ctx: Context) -> dict[str, str]:
    origin = ctx.header("origin") or "*"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "content-type",
        "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def _with_session_start(row: dict[str, Any]) -> list[dict[str, Any]]:
    if not row.pop("_session_start", False) or row["event_name"] == "session_start":
        return [row]
    first = dict(row)
    first["id"] = store.new_id()
    first["event_name"] = "session_start"
    first["engagement_ms"] = 0
    first["props"] = ""
    first["_kind"] = ""
    first["_traits"] = None
    return [first, row]


def _schedule_dashboards(claimed: list[tuple[str, str]]) -> None:
    if not claimed:
        return

    def build(site_id: str, event_name: str) -> None:
        try:
            from twohelixes.routes import dashboards

            with _dashboard_build_lock:
                dashboard_id = dashboards.build_analytics_event_dashboard(
                    site_id, event_name
                )
                if dashboard_id is None:
                    with store.transaction() as conn:
                        conn.execute(
                            "UPDATE analytics_event_dashboards SET status = 'failed'"
                            " WHERE site_id = ? AND event_name = ? AND status = 'pending'",
                            (site_id, event_name),
                        )
        except Exception:
            log.exception(
                "analytics: automatic dashboard failed for %s/%s", site_id, event_name
            )
            try:
                with store.transaction() as conn:
                    conn.execute(
                        "UPDATE analytics_event_dashboards SET status = 'failed'"
                        " WHERE site_id = ? AND event_name = ?"
                        " AND status IN ('pending', 'building')",
                        (site_id, event_name),
                    )
            except Exception:
                log.debug("analytics: could not mark dashboard failed", exc_info=True)
        finally:
            store.close()

    for site_id, event_name in claimed:
        threading.Thread(
            target=build,
            args=(site_id, event_name),
            name="analytics-dashboard",
            daemon=True,
        ).start()


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

    site = _site_for_write(ctx, payload if isinstance(payload, dict) else {})
    if site is None:
        return Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx))
    rows: list[dict[str, Any]] = []
    session_cache: dict[str, tuple[str, float, bool]] = {}
    attribution_cache: dict[str, dict[str, str]] = {}
    for event in events[:MAX_EVENTS_PER_BATCH]:
        if not isinstance(event, dict):
            continue
        merged = dict(event)
        if isinstance(payload, dict):
            for shared in ("client_id", "anonymousId", "session_id", "user_id", "userId", "screen", "viewport", "language"):
                merged.setdefault(shared, payload.get(shared))
        normalised = _normalise(merged, ctx, site, now, session_cache)
        if normalised:
            session_id = str(normalised["session_id"])
            attribution = {
                key: str(normalised.get(key) or "")
                for key in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")
            }
            if any(attribution.values()):
                attribution_cache[session_id] = attribution
            elif session_id in attribution_cache:
                normalised.update(attribution_cache[session_id])
            rows.extend(_with_session_start(normalised))

    _written, claimed = _store(rows)
    _schedule_dashboards(claimed)
    return Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx))


@router.route("OPTIONS", "/v1/collect")
def collect_preflight(ctx: Context) -> Result:
    return Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx))


@router.get("/v1/collect")
def collect_pixel(ctx: Context) -> Result:
    """GA4-Measurement-Protocol-shaped GET. Always answers with a 1x1 GIF."""
    now = time.time()
    flat = {key: values[0] for key, values in ctx.query.items() if values}
    site = _site_for_write(ctx, flat)
    props = {
        (k[4:] if k.startswith("epn.") else k[3:]): v
        for k, v in flat.items()
        if k.startswith("ep.") or k.startswith("epn.")
    }
    flat["props"] = props
    row = _normalise(flat, ctx, site, now, {}) if site else None
    if row:
        _written, claimed = _store(_with_session_start(row))
        _schedule_dashboards(claimed)
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
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
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
    session_rows = store.query(
        "SELECT session_id,"
        " SUM(CASE WHEN event_name = 'page_view' THEN 1 ELSE 0 END) AS page_views,"
        " SUM(engagement_ms) AS engagement_ms,"
        " MAX(CASE WHEN event_name IN"
        " ('purchase', 'sign_up', 'generate_lead', 'conversion') THEN 1 ELSE 0 END)"
        " AS has_key_event"
        " FROM analytics_events WHERE site_id = ? AND ts BETWEEN ? AND ?"
        " GROUP BY session_id",
        (site_id, start, end),
    )
    engaged_sessions = 0
    for row in store.rows_to_dicts(session_rows):
        if (
            int(row.get("engagement_ms") or 0) >= 10_000
            or int(row.get("page_views") or 0) >= 2
            or bool(row.get("has_key_event"))
        ):
            engaged_sessions += 1
    sessions = int(totals.get("sessions") or 0)
    bounces = max(0, sessions - engaged_sessions)

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
                "sessions": sessions,
                "page_views": int(totals.get("page_views") or 0),
                "engagement_ms": int(totals.get("engagement_ms") or 0),
                "engaged_sessions": engaged_sessions,
                "bounces": bounces,
                "bounce_rate": (bounces / sessions) if sessions else 0.0,
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
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
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
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    start, end = _window(ctx)
    bucket_seconds = 3600 if (end - start) <= 8 * 86400 else 86400

    rows = store.query(
        "SELECT ts, client_id, session_id, event_name, engagement_ms FROM analytics_events"
        " WHERE site_id = ? AND ts BETWEEN ? AND ?",
        (site_id, start, end),
    )
    buckets: dict[int, dict[str, Any]] = {}
    for row in store.rows_to_dicts(rows):
        slot = int(float(row["ts"]) // bucket_seconds * bucket_seconds)
        entry = buckets.setdefault(
            slot,
            {
                "ts": slot,
                "events": 0,
                "page_views": 0,
                "engagement_ms": 0,
                "_users": set(),
                "_sessions": set(),
            },
        )
        entry["events"] += 1
        if row["event_name"] == "page_view":
            entry["page_views"] += 1
        entry["_users"].add(row["client_id"])
        entry["_sessions"].add(row["session_id"])
        entry["engagement_ms"] += int(row.get("engagement_ms") or 0)

    series = []
    for slot in sorted(buckets):
        entry = buckets[slot]
        series.append(
            {
                "ts": entry["ts"],
                "events": entry["events"],
                "page_views": entry["page_views"],
                "users": len(entry["_users"]),
                "sessions": len(entry["_sessions"]),
                "engagement_ms": entry["engagement_ms"],
            }
        )
    return json_result({"site_id": site_id, "bucket_seconds": bucket_seconds, "series": series})


@router.get("/v1/analytics/export")
def export_events(ctx: Context) -> Result:
    """Segment-shaped export, for syncing into a warehouse."""
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
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
