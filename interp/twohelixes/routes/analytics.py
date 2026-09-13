"""Site analytics: ingest and reporting.

Ingest is deliberately forgiving. A tracker that 400s on an unexpected field
loses the whole batch, and the page it runs on gains nothing from being told -
so unknown keys land in `props`, bad rows are skipped rather than failing the
batch, and every response is a 204 the browser can ignore.

Five wire formats reach the same event model:

* the native batch (`POST /v1/collect`, `{"site_id": ..., "events": [...]}`)
* Segment-style calls (`type: page|track|identify|group|alias`)
* GA4 Measurement Protocol calls and query strings (`GET /v1/collect?en=page_view`),
  which is what the no-JS pixel and server-side callers use.
* Mixpanel event, profile, import, and group calls
* Amplitude HTTP V2 event batches
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import queue
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from twohelixes import auth, config, router, store
from twohelixes.router import Context, Result, error, json_result
from twohelixes.routes import teams

log = logging.getLogger("twohelixes.analytics")

MAX_EVENTS_PER_BATCH = 2000
MAX_STRING = 1024
MAX_PROPS_BYTES = 8192
SESSION_GAP_SECONDS = 30 * 60
DEFAULT_SAMPLE_AFTER_RPS = 100
MIN_SAMPLE_RATE = 0.01

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

# These are sparse, business-significant events. Sampling a scroll or pointer
# stream is harmless; sampling the event that explains whether the stream led
# anywhere is not. Page views are deliberately not in this set: at very high
# volume they are the largest source and can be estimated from their weight.
_NEVER_SAMPLE = {
    "alias",
    "conversion",
    "generate_lead",
    "group",
    "identify",
    "js_error",
    "login",
    "begin_checkout",
    "purchase",
    "refund",
    "session_start",
    "sign_in_completed",
    "sign_up",
    "signup_completed",
    "subscription_dialog_opened",
    "subscription_auth_required",
    "subscription_auth_selected",
    "subscription_checkout_ready",
    "subscription_checkout_failed",
    "subscription_checkout_completed",
    "subscription_dialog_closed",
    "$groupidentify",
    "$identify",
}

_sample_lock = threading.Lock()
_sample_windows: dict[str, tuple[int, int, int]] = {}


def _sample_after_rps() -> int:
    try:
        configured = int(config.get("TWOHELIXES_ANALYTICS_SAMPLE_AFTER_RPS") or 0)
    except (TypeError, ValueError):
        configured = 0
    total = configured or DEFAULT_SAMPLE_AFTER_RPS
    try:
        workers = max(1, int(config.get("TWOHELIXES_WORKERS") or 1))
    except (TypeError, ValueError):
        workers = 1
    return max(10, total // workers)


def _rate_value(value: Any) -> float:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SAMPLE_RATE, min(1.0, rate))


def _server_sample_rate(site_id: str, now: float, ceiling: int | None = None) -> float:
    """A cheap per-worker rate estimate, with no database write on the hot path.

    The previous second sets the initial rate for this second. The live count
    then tightens it during a sudden burst, so the first burst is bounded too.
    Dividing the configured ceiling by the worker count keeps SO_REUSEPORT
    workers close to one site-wide ceiling without coordinating every event.
    """
    second = int(now)
    if ceiling is None:
        ceiling = _sample_after_rps()
    with _sample_lock:
        if site_id not in _sample_windows and len(_sample_windows) >= 4096:
            stale = [
                key
                for key, value in _sample_windows.items()
                if value[0] < second - 60
            ]
            for key in stale[:1024]:
                _sample_windows.pop(key, None)
        window, count, previous = _sample_windows.get(site_id, (second, 0, 0))
        if window != second:
            previous = count if window == second - 1 else 0
            window, count = second, 0
        count += 1
        _sample_windows[site_id] = (window, count, previous)
    observed = max(count, previous)
    return min(1.0, ceiling / observed) if observed > ceiling else 1.0


def _keep_sample(
    event: dict[str, Any], site_id: str, now: float, ceiling: int | None = None
) -> tuple[bool, float]:
    name = _clip(event.get("event") or event.get("name") or event.get("en"), 64).lower()
    kind = _clip(event.get("type"), 32).lower()
    if not name and kind in _SEGMENT_EVENT:
        name = _SEGMENT_EVENT[kind]
    # `sample_rate` describes selection that already happened upstream. The
    # browser includes it only on rows it retained, so drawing against it again
    # would square the probability and make weighted totals undercount badly.
    source_rate = _rate_value(event.get("sample_rate") or event.get("sampleRate"))
    observed_rate = _server_sample_rate(site_id, now, ceiling)
    server_rate = 1.0 if name in _NEVER_SAMPLE else observed_rate
    effective = source_rate * server_rate
    if server_rate >= 1.0:
        return True, effective

    client_id = _clip(
        event.get("client_id")
        or event.get("anonymousId")
        or event.get("previousId")
        or event.get("cid")
        or event.get("user_id")
        or event.get("userId"),
        64,
    )
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    session_id = _clip(
        event.get("session_id") or event.get("sid") or params.get("session_id"), 64
    )
    # Stable within a ten-second/session slice: correlated event bursts are
    # kept or dropped together, which preserves paths better than independent
    # coin flips while still adapting quickly when traffic changes.
    sample_key = f"{site_id}|{session_id or client_id}|{int(now // 10)}"
    draw = int.from_bytes(hashlib.sha256(sample_key.encode()).digest()[:8], "big") / 2**64
    return draw < server_rate, effective


def _clip(value: Any, limit: int = MAX_STRING) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    value = value.strip()
    return value[:limit]


def _timestamp_seconds(value: Any, now: float) -> float:
    try:
        numeric = float(value)
        if numeric > 1e14:
            return numeric / 1_000_000.0
        if numeric > 1e11:
            return numeric / 1000.0
        return numeric
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return now


def _segment_timestamp(event: dict[str, Any], now: float) -> float | None:
    raw = event.get("timestamp") or event.get("originalTimestamp")
    if raw is None:
        return None
    event_time = _timestamp_seconds(raw, now)
    if event.get("sentAt") is not None:
        sent_at = _timestamp_seconds(event.get("sentAt"), now)
        event_time = now - (sent_at - event_time)
    return event_time


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
    if len(text.encode("utf-8")) <= MAX_PROPS_BYTES:
        return text
    # Never slice JSON text: that silently turns the whole property bag into
    # invalid JSON. Retain complete top-level values that fit and mark the bag
    # so exports and the agent can disclose the loss.
    if not isinstance(raw, dict):
        raw = {"value": raw}
    kept: dict[str, Any] = {"_twohelixes_truncated": True}
    used = len(json.dumps(kept, separators=(",", ":")).encode("utf-8"))
    for key, value in raw.items():
        clipped_key = _clip(key, 128)
        if clipped_key in kept:
            continue
        try:
            pair = json.dumps(
                {clipped_key: value}, default=str, separators=(",", ":")
            )
        except (TypeError, ValueError):
            continue
        # The pair's braces replace the final brace in `kept`; a comma is the
        # only extra structural byte. This keeps pruning linear in key count.
        added = len(pair.encode("utf-8")) - 2 + 1
        if used + added <= MAX_PROPS_BYTES:
            kept[clipped_key] = value
            used += added
    return json.dumps(kept, default=str, separators=(",", ":"))


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
    request_meta: dict[str, Any] | None = None,
    attribution_cache: dict[str, dict[str, str]] | None = None,
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
        event.get("client_id")
        or event.get("anonymousId")
        or event.get("previousId")
        or event.get("cid")
        or event.get("user_id")
        or event.get("userId"),
        64,
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
    if _clip(event.get("_source"), 32).lower() == "segment":
        props = dict(props)
        safe_context = {
            str(key): value
            for key, value in context.items()
            if key not in ("ip", "userAgent", "page", "campaign", "groups")
        }
        if safe_context:
            props["$context"] = safe_context
        if isinstance(event.get("integrations"), dict):
            props["$integrations"] = event["integrations"]
        timing = {
            key: event.get(key)
            for key in ("originalTimestamp", "sentAt")
            if event.get(key) is not None
        }
        if timing:
            props["$segment_timing"] = timing
    page_location = _clip(
        event.get("page_location")
        or event.get("url")
        or event.get("dl")
        or props.get("page_location")
        or page.get("url")
    )

    meta = request_meta or _request_metadata(ctx, site_id)
    device = str(meta["device"])
    browser = str(meta["browser"])
    os_name = str(meta["os"])
    is_bot = bool(meta["is_bot"])
    if is_bot:
        return None

    raw_ts = event.get("ts") or event.get("timestamp") or event.get("timestamp_micros")
    ts = _timestamp_seconds(raw_ts, now) if raw_ts is not None else now
    # Never trust a client clock far from ours.
    if ts - now > 86400 or (
        now - ts > 86400 and not bool(event.get("_allow_historical"))
    ):
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
        cached_attribution = (attribution_cache or {}).get(session_id)
        if cached_attribution:
            attribution = dict(cached_attribution)
        else:
            previous = store.row_to_dict(
                store.one(
                    "SELECT utm_source, utm_medium, utm_campaign, utm_term, utm_content"
                    " FROM analytics_events WHERE site_id = ? AND session_id = ?"
                    " ORDER BY ts ASC LIMIT 1",
                    (site_id, session_id),
                )
            )
            if previous:
                attribution = {
                    key: _clip(previous.get(key), 128) for key in attribution
                }
                if attribution_cache is not None:
                    attribution_cache[session_id] = dict(attribution)

    groups = event.get("_groups")
    if not isinstance(groups, dict):
        groups = context.get("groups") if isinstance(context.get("groups"), dict) else {}
    if event.get("groupId") is not None:
        groups = dict(groups)
        groups[_clip(event.get("groupType") or "group", 64)] = event.get("groupId")
    group_traits = event.get("_group_traits")
    if not isinstance(group_traits, dict) and kind == "group":
        group_traits = event.get("traits") if isinstance(event.get("traits"), dict) else {}

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
        "country": str(meta["country"]),
        "ip_hash": str(meta["ip_hash"]),
        "engagement_ms": _int(
            event.get("engagement_ms")
            or event.get("_et")
            or props.get("engagement_time_msec")
            or props.get("engagement_ms")
        ),
        "sample_rate": _rate_value(event.get("_effective_sample_rate")),
        "sample_weight": 1.0 / _rate_value(event.get("_effective_sample_rate")),
        "event_sequence": _int(event.get("_sequence")),
        "source": _clip(event.get("_source") or "native", 32).lower(),
        "external_id": _clip(
            event.get("_external_id")
            or event.get("messageId")
            or event.get("event_id"),
            128,
        )
        or None,
        "props": _props_json(props),
        "_kind": kind,
        "_traits": (
            event.get("traits")
            if kind != "group" and isinstance(event.get("traits"), dict)
            else None
        ),
        "_trait_ops": (
            event.get("_trait_ops") if isinstance(event.get("_trait_ops"), dict) else None
        ),
        "_groups": groups,
        "_group_traits": group_traits,
        "_group_trait_ops": (
            event.get("_group_trait_ops")
            if isinstance(event.get("_group_trait_ops"), dict)
            else None
        ),
        "_delete_profile": bool(event.get("_delete_profile")),
        "_delete_group": bool(event.get("_delete_group")),
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
    " device browser os screen viewport language country ip_hash engagement_ms sample_rate"
    " sample_weight event_sequence source external_id props"
).split()


_warned: set[str] = set()
_dashboard_queue: queue.SimpleQueue[tuple[str, str]] = queue.SimpleQueue()
_dashboard_worker_lock = threading.Lock()
_dashboard_worker_started = False


def _warn_once(message: str) -> None:
    """Loud the first time, quiet after: a broken ingest must not hide, and it
    must not fill the disk either."""
    if message not in _warned:
        _warned.add(message)
        log.warning(message, exc_info=True)
    else:
        log.debug(message, exc_info=True)


def _request_metadata(ctx: Context, site_id: str) -> dict[str, Any]:
    """Compute request-scoped metadata once, even for a large event batch."""
    device, browser, os_name, is_bot = _parse_ua(ctx.header("user-agent"))
    return {
        "device": device,
        "browser": browser,
        "os": os_name,
        "is_bot": is_bot,
        "country": _clip(ctx.header("cf-ipcountry"), 8),
        "ip_hash": _ip_hash(ctx.client_ip, site_id),
    }


_INSERT_CHUNK_ROWS = 200


def _insert_event_rows(conn: Any, rows: list[dict[str, Any]]) -> set[str]:
    """Insert bounded chunks and return the ids that were newly stored.

    Two hundred rows stay below PostgreSQL's parameter limit while replacing
    up to two hundred synchronous database round trips with one.
    """
    row_placeholders = "(" + ", ".join("?" for _ in _COLUMNS) + ")"
    inserted: set[str] = set()
    for start in range(0, len(rows), _INSERT_CHUNK_ROWS):
        chunk = rows[start : start + _INSERT_CHUNK_ROWS]
        values_sql = ", ".join(row_placeholders for _ in chunk)
        params = tuple(row[column] for row in chunk for column in _COLUMNS)
        cursor = conn.execute(
            f"INSERT INTO analytics_events ({', '.join(_COLUMNS)})"
            f" VALUES {values_sql} ON CONFLICT DO NOTHING RETURNING id",
            params,
        )
        inserted.update(str(record["id"]) for record in cursor.fetchall())
    return inserted


def _store(rows: list[dict[str, Any]]) -> tuple[int, list[tuple[str, str]]]:
    if not rows:
        return 0, []
    claimed: list[tuple[str, str]] = []
    dashboard_candidates: dict[tuple[str, str], bool] = {}
    with store.transaction() as conn:
        # Normalisation has already bounded every field. A database error here
        # must abort visibly; swallowing it leaves PostgreSQL's transaction in
        # an aborted state and can otherwise turn total data loss into a 204.
        inserted_ids = _insert_event_rows(conn, rows)
        for row in rows:
            if str(row["id"]) not in inserted_ids:
                continue
            if row.get("_delete_profile"):
                conn.execute(
                    "DELETE FROM analytics_identities WHERE site_id = ? AND client_id = ?",
                    (row["site_id"], row["client_id"]),
                )
            elif row.get("user_id") and (
                row.get("_kind") in ("identify", "alias")
                or row.get("_traits")
                or row.get("_trait_ops")
            ):
                _upsert_identity(conn, row)
            _link_groups(conn, row)
            candidate = (str(row["site_id"]), str(row["event_name"]))
            important = candidate[1].lower() in _NEVER_SAMPLE
            # Bound work from an untrusted write key spraying unique names.
            # Ordinary custom events earn a dashboard after repeated use;
            # important conversion/identity events are eligible immediately.
            if important or candidate in dashboard_candidates or len(dashboard_candidates) < 25:
                dashboard_candidates[candidate] = important
        for (site_id, event_name), important in dashboard_candidates.items():
            if not important:
                tenth = conn.execute(
                    "SELECT 1 FROM analytics_events"
                    " WHERE site_id = ? AND event_name = ? LIMIT 1 OFFSET 9",
                    (site_id, event_name),
                ).fetchone()
                if tenth is None:
                    continue
            cursor = conn.execute(
                "INSERT INTO analytics_event_dashboards"
                " (site_id, event_name, status, claimed_at)"
                " VALUES (?, ?, 'pending', ?)"
                " ON CONFLICT (site_id, event_name) DO NOTHING",
                (site_id, event_name, time.time()),
            )
            if cursor.rowcount:
                claimed.append((site_id, event_name))
    return len(inserted_ids), claimed


def _upsert_identity(conn: Any, row: dict[str, Any]) -> None:
    existing = conn.execute(
        "SELECT client_id, traits FROM analytics_identities"
        " WHERE site_id = ? AND client_id = ?",
        (row["site_id"], row["client_id"]),
    ).fetchone()
    current = store.load_json(existing["traits"], {}) if existing else {}
    if not isinstance(current, dict):
        current = {}
    incoming = row.get("_traits") or {}
    if isinstance(incoming, dict):
        current.update(incoming)
    _apply_trait_ops(current, row.get("_trait_ops") or {})
    traits = _props_json(current)
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


def _apply_trait_ops(traits: dict[str, Any], operations: dict[str, Any]) -> None:
    """Apply the portable subset shared by Mixpanel and Amplitude profiles."""
    for key, value in (operations.get("set") or {}).items():
        traits[str(key)] = value
    for key, value in (operations.get("set_once") or {}).items():
        traits.setdefault(str(key), value)
    for key, value in (operations.get("add") or {}).items():
        name = str(key)
        try:
            traits[name] = float(traits.get(name, 0)) + float(value)
        except (TypeError, ValueError):
            continue
    unset = operations.get("unset") or []
    if isinstance(unset, dict):
        unset = list(unset)
    if isinstance(unset, str):
        unset = [unset]
    for key in unset:
        traits.pop(str(key), None)
    for operation in ("append", "union", "remove"):
        values = operations.get(operation) or {}
        if not isinstance(values, dict):
            continue
        for key, value in values.items():
            name = str(key)
            existing_value = traits.get(name, [])
            items = (
                list(existing_value)
                if isinstance(existing_value, list)
                else [existing_value]
            )
            incoming_values = value if isinstance(value, list) else [value]
            if operation == "remove":
                traits[name] = [item for item in items if item not in incoming_values]
            elif operation == "union":
                traits[name] = items + [
                    item for item in incoming_values if item not in items
                ]
            else:
                traits[name] = items + incoming_values


def _link_groups(conn: Any, row: dict[str, Any]) -> None:
    groups = row.get("_groups") or {}
    if not isinstance(groups, dict):
        return
    for raw_type, raw_ids in groups.items():
        group_type = _clip(raw_type, 64)
        group_ids = raw_ids if isinstance(raw_ids, list) else [raw_ids]
        for raw_id in group_ids:
            group_id = _clip(raw_id, 128)
            if not group_type or not group_id:
                continue
            conn.execute(
                "INSERT INTO analytics_event_groups"
                " (event_id, site_id, group_type, group_id) VALUES (?, ?, ?, ?)"
                " ON CONFLICT DO NOTHING",
                (row["id"], row["site_id"], group_type, group_id),
            )
            existing = conn.execute(
                "SELECT traits, first_seen FROM analytics_groups"
                " WHERE site_id = ? AND group_type = ? AND group_id = ?",
                (row["site_id"], group_type, group_id),
            ).fetchone()
            traits = store.load_json(existing["traits"], {}) if existing else {}
            if not isinstance(traits, dict):
                traits = {}
            incoming = row.get("_group_traits") or {}
            if isinstance(incoming, dict):
                traits.update(incoming)
            _apply_trait_ops(traits, row.get("_group_trait_ops") or {})
            if row.get("_delete_group"):
                conn.execute(
                    "DELETE FROM analytics_groups"
                    " WHERE site_id = ? AND group_type = ? AND group_id = ?",
                    (row["site_id"], group_type, group_id),
                )
            elif existing:
                conn.execute(
                    "UPDATE analytics_groups SET traits = ?, last_seen = ?"
                    " WHERE site_id = ? AND group_type = ? AND group_id = ?",
                    (
                        _props_json(traits),
                        row["ts"],
                        row["site_id"],
                        group_type,
                        group_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO analytics_groups"
                    " (site_id, group_type, group_id, traits, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["site_id"],
                        group_type,
                        group_id,
                        _props_json(traits),
                        row["ts"],
                        row["ts"],
                    ),
                )


def _site_token(ctx: Context, payload: Any) -> str:
    if isinstance(payload, dict):
        candidate = (
            payload.get("site_id")
            or payload.get("tid")
            or payload.get("writeKey")
            or payload.get("measurement_id")
            or payload.get("api_key")
            or payload.get("token")
        )
        if candidate:
            return _clip(candidate, 128)
    candidate = (
        ctx.q("site_id")
        or ctx.q("tid")
        or ctx.q("measurement_id")
        or ctx.q("api_key")
        or ctx.q("token")
    )
    if candidate:
        return _clip(candidate, 128)
    authorization = ctx.header("authorization")
    if authorization.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(authorization[6:].strip()).decode("utf-8")
            return _clip(decoded.split(":", 1)[0], 128)
        except (ValueError, UnicodeDecodeError):
            return ""
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
        conn.execute("DELETE FROM analytics_event_groups WHERE site_id = ?", (site_id,))
        conn.execute("DELETE FROM analytics_groups WHERE site_id = ?", (site_id,))
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
        "Access-Control-Allow-Headers": "authorization, content-type",
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
    first["event_sequence"] = max(0, int(first["event_sequence"]) - 1)
    first["props"] = ""
    first["external_id"] = None
    first["_kind"] = ""
    first["_traits"] = None
    first["_trait_ops"] = None
    first["_groups"] = {}
    first["_group_traits"] = None
    first["_group_trait_ops"] = None
    first["_delete_profile"] = False
    first["_delete_group"] = False
    return [first, row]


def _schedule_dashboards(claimed: list[tuple[str, str]]) -> None:
    if not claimed:
        return

    global _dashboard_worker_started
    with _dashboard_worker_lock:
        if not _dashboard_worker_started:
            threading.Thread(
                target=_dashboard_worker,
                name="analytics-dashboard",
                daemon=True,
            ).start()
            _dashboard_worker_started = True
    for candidate in claimed:
        _dashboard_queue.put(candidate)


def _dashboard_worker() -> None:
    """One serial worker per process; never one thread per custom event."""
    while True:
        site_id, event_name = _dashboard_queue.get()
        try:
            from twohelixes.routes import dashboards

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


def _collect_payload_with_stats(ctx: Context, payload: Any) -> tuple[Result, dict[str, int]]:
    now = time.time()
    events: list[Any] = []
    if isinstance(payload, dict):
        raw = payload.get("events") or payload.get("batch")
        if isinstance(raw, list):
            events = raw
        else:
            events = [payload]
    elif isinstance(payload, list):
        events = payload

    stats = {
        "received": len(events),
        "accepted": 0,
        "sampled_out": 0,
        "stored_rows": 0,
    }
    if len(events) > MAX_EVENTS_PER_BATCH:
        too_many = error(413, "too_many_events", limit=MAX_EVENTS_PER_BATCH)
        too_many.headers.update(_cors(ctx))
        return (
            too_many,
            stats,
        )
    site = _site_for_write(ctx, payload if isinstance(payload, dict) else {})
    if site is None:
        return (
            Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx)),
            stats,
        )
    request_meta = _request_metadata(ctx, str(site["id"]))
    if request_meta["is_bot"]:
        return (
            Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx)),
            stats,
        )
    sample_ceiling = _sample_after_rps()
    rows: list[dict[str, Any]] = []
    session_cache: dict[str, tuple[str, float, bool]] = {}
    attribution_cache: dict[str, dict[str, str]] = {}
    for sequence, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        merged = dict(event)
        merged["_sequence"] = sequence
        if isinstance(payload, dict):
            for shared in (
                "client_id",
                "anonymousId",
                "session_id",
                "user_id",
                "userId",
                "screen",
                "viewport",
                "language",
                "timestamp",
                "timestamp_micros",
                "originalTimestamp",
                "sentAt",
                "context",
                "integrations",
                "_source",
                "_external_id",
            ):
                merged.setdefault(shared, payload.get(shared))
        if _clip(merged.get("_source"), 32).lower() == "segment":
            corrected = _segment_timestamp(merged, now)
            if corrected is not None:
                merged["timestamp"] = corrected
        keep, effective_rate = _keep_sample(
            merged, str(site["id"]), now, sample_ceiling
        )
        if not keep:
            stats["sampled_out"] += 1
            continue
        merged["_effective_sample_rate"] = effective_rate
        normalised = _normalise(
            merged,
            ctx,
            site,
            now,
            session_cache,
            request_meta,
            attribution_cache,
        )
        if normalised:
            stats["accepted"] += 1
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

    written, claimed = _store(rows)
    stats["stored_rows"] = written
    _schedule_dashboards(claimed)
    return (
        Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx)),
        stats,
    )


def _collect_payload(ctx: Context, payload: Any) -> Result:
    result, _stats = _collect_payload_with_stats(ctx, payload)
    return result


@router.post("/v1/collect")
def collect(ctx: Context) -> Result:
    payload = ctx.json
    if payload is None and ctx.raw_body:
        # sendBeacon sends text/plain; the body is still JSON.
        try:
            payload = json.loads(ctx.raw_body)
        except (ValueError, TypeError):
            payload = None
    return _collect_payload(ctx, payload)


def _segment_call(ctx: Context, kind: str) -> Result:
    payload = dict(ctx.json) if isinstance(ctx.json, dict) else {}
    payload.setdefault("type", kind)
    payload.setdefault("_source", "segment")
    if kind in ("page", "screen"):
        payload.setdefault("event", "page_view")
    result = _collect_payload(ctx, payload)
    if result.status != 204:
        return result
    return Result(
        status=200,
        body={"success": True},
        headers=result.headers,
    )


@router.post("/v1/batch")
def segment_batch(ctx: Context) -> Result:
    payload = dict(ctx.json) if isinstance(ctx.json, dict) else {}
    payload.setdefault("_source", "segment")
    result = _collect_payload(ctx, payload)
    if result.status != 204:
        return result
    return Result(status=200, body={"success": True}, headers=result.headers)


@router.post("/v1/track")
def segment_track(ctx: Context) -> Result:
    return _segment_call(ctx, "track")


@router.post("/v1/page")
def segment_page(ctx: Context) -> Result:
    return _segment_call(ctx, "page")


@router.post("/v1/screen")
def segment_screen(ctx: Context) -> Result:
    return _segment_call(ctx, "screen")


@router.post("/v1/identify")
def segment_identify(ctx: Context) -> Result:
    return _segment_call(ctx, "identify")


@router.post("/v1/group")
def segment_group(ctx: Context) -> Result:
    return _segment_call(ctx, "group")


@router.post("/v1/alias")
def segment_alias(ctx: Context) -> Result:
    return _segment_call(ctx, "alias")


@router.post("/mp/collect")
def ga4_measurement_protocol(ctx: Context) -> Result:
    payload = dict(ctx.json) if isinstance(ctx.json, dict) else {}
    payload["measurement_id"] = ctx.q("measurement_id") or payload.get("measurement_id")
    payload["_source"] = "ga4"
    user_properties = payload.get("user_properties")
    if isinstance(user_properties, dict) and isinstance(payload.get("events"), list):
        traits = {
            str(key): value.get("value") if isinstance(value, dict) else value
            for key, value in user_properties.items()
        }
        converted = []
        for raw_event in payload["events"]:
            if not isinstance(raw_event, dict):
                converted.append(raw_event)
                continue
            event = dict(raw_event)
            params = dict(event.get("params") or {})
            params["$user_properties"] = traits
            event["params"] = params
            event.setdefault("traits", traits)
            event.setdefault(
                "_external_id",
                params.get("event_id"),
            )
            event.setdefault("sample_rate", params.get("sample_rate"))
            converted.append(event)
        payload["events"] = converted
    elif isinstance(payload.get("events"), list):
        converted = []
        for raw_event in payload["events"]:
            if not isinstance(raw_event, dict):
                converted.append(raw_event)
                continue
            event = dict(raw_event)
            params = _dict(event.get("params"))
            event.setdefault(
                "_external_id",
                params.get("event_id"),
            )
            event.setdefault("sample_rate", params.get("sample_rate"))
            converted.append(event)
        payload["events"] = converted
    result = _collect_payload(ctx, payload)
    if result.status != 204:
        return result
    return Result(status=204, body=None, content_type="text/plain", headers=result.headers)


def _vendor_body(ctx: Context) -> Any:
    """Decode JSON or the base64 `data=` form used by classic Mixpanel SDKs."""
    if ctx.json is not None:
        return ctx.json
    form = parse_qs(ctx.raw_body, keep_blank_values=True)
    encoded = (form.get("data") or [""])[0]
    if not encoded:
        return None
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        return json.loads(base64.b64decode(padded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _mixpanel_payload(ctx: Context, *, profiles: bool = False) -> dict[str, Any]:
    raw = _vendor_body(ctx)
    items = raw if isinstance(raw, list) else [raw]
    token = ctx.q("token") or ""
    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        properties = _dict(item.get("properties"))
        item_token = item.get("$token") or properties.get("token") or token
        if not token and item_token:
            token = _clip(item_token, 128)
        if profiles:
            distinct_id = item.get("$distinct_id") or item.get("distinct_id")
            device_id = item.get("$device_id") or distinct_id
            operations = {
                "set": _dict(item.get("$set")),
                "set_once": _dict(item.get("$set_once")),
                "add": _dict(item.get("$add")),
                "append": _dict(item.get("$append")),
                "union": _dict(item.get("$union")),
                "remove": _dict(item.get("$remove")),
                "unset": item.get("$unset") or [],
            }
            events.append(
                {
                    "type": "identify",
                    "event": "identify",
                    "client_id": device_id,
                    "user_id": distinct_id,
                    "traits": {},
                    "_trait_ops": operations,
                    "timestamp": item.get("$time"),
                    "_external_id": item.get("$insert_id"),
                    "_source": "mixpanel",
                    "_allow_historical": True,
                    "_delete_profile": "$delete" in item,
                }
            )
            continue
        distinct_id = properties.get("distinct_id")
        user_id = properties.get("$user_id")
        event_name = item.get("event")
        if event_name == "$create_alias":
            client_id = distinct_id
            user_id = properties.get("alias") or user_id
        elif event_name == "$identify":
            client_id = properties.get("$anon_distinct_id") or distinct_id
            user_id = properties.get("$identified_id") or user_id
        else:
            client_id = properties.get("$device_id") or distinct_id or user_id
        safe_properties = {
            str(key): value for key, value in properties.items() if key != "token"
        }
        kind = "identify" if event_name in ("$identify", "$create_alias") else ""
        events.append(
            {
                "type": kind,
                "event": "identify" if kind else event_name,
                "client_id": client_id,
                "user_id": user_id,
                "session_id": properties.get("$session_id") or properties.get("session_id"),
                "timestamp": properties.get("time"),
                "page_location": properties.get("$current_url"),
                "referrer": properties.get("$referrer"),
                "props": safe_properties,
                "traits": safe_properties if kind else None,
                "_external_id": properties.get("$insert_id"),
                "_source": "mixpanel",
                "_allow_historical": True,
                "sample_rate": properties.get("sample_rate"),
            }
        )
    return {"site_id": token, "events": events}


def _mixpanel_response(ctx: Context, stats: dict[str, int]) -> Result:
    if ctx.q_bool("verbose"):
        return json_result(
            {"status": 1, "error": None, "num_records_imported": stats["accepted"]}
        )
    return Result(status=200, body="1", content_type="text/plain", headers=_cors(ctx))


@router.post("/track")
def mixpanel_track(ctx: Context) -> Result:
    result, stats = _collect_payload_with_stats(ctx, _mixpanel_payload(ctx))
    if result.status != 204:
        return Result(
            status=result.status,
            body={"status": 0, "error": "too_many_events"},
            headers=_cors(ctx),
        )
    response = _mixpanel_response(ctx, stats)
    response.headers.update(result.headers)
    return response


@router.post("/import")
def mixpanel_import(ctx: Context) -> Result:
    return mixpanel_track(ctx)


@router.post("/engage")
def mixpanel_engage(ctx: Context) -> Result:
    result, stats = _collect_payload_with_stats(ctx, _mixpanel_payload(ctx, profiles=True))
    if result.status != 204:
        return Result(
            status=result.status,
            body={"status": 0, "error": "too_many_events"},
            headers=_cors(ctx),
        )
    response = _mixpanel_response(ctx, stats)
    response.headers.update(result.headers)
    return response


@router.post("/groups")
def mixpanel_groups(ctx: Context) -> Result:
    raw = _vendor_body(ctx)
    items = raw if isinstance(raw, list) else [raw]
    token = ctx.q("token") or ""
    events: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        token = token or _clip(item.get("$token"), 128)
        group_type = _clip(item.get("$group_key"), 64)
        group_id = _clip(item.get("$group_id"), 128)
        operations = {
            "set": _dict(item.get("$set")),
            "set_once": _dict(item.get("$set_once")),
            "add": _dict(item.get("$add")),
            "append": _dict(item.get("$append")),
            "union": _dict(item.get("$union")),
            "remove": _dict(item.get("$remove")),
            "unset": item.get("$unset") or [],
        }
        events.append(
            {
                "type": "group",
                "event": "$groupidentify",
                "client_id": group_id,
                "_groups": {group_type: group_id},
                "_group_trait_ops": operations,
                "_source": "mixpanel",
                "_allow_historical": True,
                "_delete_group": "$delete" in item,
            }
        )
    result, stats = _collect_payload_with_stats(
        ctx, {"site_id": token, "events": events}
    )
    if result.status != 204:
        return Result(
            status=result.status,
            body={"status": 0, "error": "too_many_events"},
            headers=_cors(ctx),
        )
    response = _mixpanel_response(ctx, stats)
    response.headers.update(result.headers)
    return response


def _amplitude_trait_ops(user_properties: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    properties = _dict(user_properties)
    operations = {
        "set": _dict(properties.pop("$set", {})),
        "set_once": _dict(
            properties.pop("$setOnce", properties.pop("$set_once", {}))
        ),
        "add": _dict(properties.pop("$add", {})),
        "append": _dict(properties.pop("$append", {})),
        "union": _dict(properties.pop("$union", {})),
        "remove": _dict(properties.pop("$remove", {})),
        "unset": properties.pop("$unset", []),
    }
    operations["set"].update(properties)
    return properties, operations


def _amplitude_payload(ctx: Context) -> dict[str, Any]:
    return _amplitude_payload_from(
        ctx,
        ctx.json if isinstance(ctx.json, dict) else {},
    )


def _amplitude_payload_from(ctx: Context, raw: dict[str, Any]) -> dict[str, Any]:
    token = raw.get("api_key") or ctx.q("api_key")
    raw_events = raw.get("events") if isinstance(raw.get("events"), list) else []
    events: list[dict[str, Any]] = []
    for item in raw_events:
        if not isinstance(item, dict):
            continue
        event_name = item.get("event_type")
        user_properties, trait_ops = _amplitude_trait_ops(item.get("user_properties"))
        event_properties = _dict(item.get("event_properties"))
        groups = _dict(item.get("groups"))
        if groups:
            event_properties["$groups"] = groups
        if user_properties or any(trait_ops.values()):
            event_properties["$user_properties"] = item.get("user_properties")
        if item.get("group_properties") is not None:
            event_properties["$group_properties"] = item.get("group_properties")
        session_id = item.get("session_id")
        if session_id == -1:
            session_id = None
        is_identify = event_name in ("$identify", "$groupidentify")
        group_traits, group_trait_ops = _amplitude_trait_ops(
            item.get("group_properties")
        )
        events.append(
            {
                "type": (
                    "group" if event_name == "$groupidentify" else
                    ("identify" if is_identify else "")
                ),
                "event": event_name,
                "client_id": item.get("device_id") or item.get("user_id"),
                "user_id": item.get("user_id"),
                "session_id": session_id,
                "timestamp": item.get("time"),
                "props": event_properties,
                "traits": user_properties,
                "_trait_ops": trait_ops,
                "_groups": groups,
                "_group_traits": group_traits,
                "_group_trait_ops": group_trait_ops,
                "_external_id": item.get("insert_id"),
                "_source": "amplitude",
                "_allow_historical": True,
                "sample_rate": event_properties.get("sample_rate"),
            }
        )
    return {"site_id": token, "events": events}


def _amplitude_response(ctx: Context) -> Result:
    payload = _amplitude_payload(ctx)
    return _amplitude_result(ctx, payload)


def _amplitude_result(ctx: Context, payload: dict[str, Any]) -> Result:
    result, stats = _collect_payload_with_stats(ctx, payload)
    if result.status != 204:
        return Result(
            status=result.status,
            body={
                "code": result.status,
                "error": "too_many_events",
                "events_ingested": 0,
                "payload_size_bytes": len(ctx.raw_body.encode("utf-8")),
                "server_upload_time": int(time.time() * 1000),
            },
            headers=_cors(ctx),
        )
    return Result(
        status=200,
        body={
            "code": 200,
            "events_ingested": stats["accepted"],
            "payload_size_bytes": len(ctx.raw_body.encode("utf-8")),
            "server_upload_time": int(time.time() * 1000),
        },
        headers=result.headers,
    )


@router.post("/2/httpapi")
def amplitude_http_v2(ctx: Context) -> Result:
    return _amplitude_response(ctx)


@router.post("/batch")
def amplitude_batch(ctx: Context) -> Result:
    return _amplitude_response(ctx)


@router.post("/identify")
def amplitude_identify(ctx: Context) -> Result:
    raw = ctx.json if isinstance(ctx.json, dict) else {}
    if raw:
        token = raw.get("api_key") or ctx.q("api_key")
        identifications = raw.get("identification") or raw.get("identifications") or []
    else:
        form = parse_qs(ctx.raw_body, keep_blank_values=True)
        token = (form.get("api_key") or [ctx.q("api_key") or ""])[0]
        encoded = (form.get("identification") or ["[]"])[0]
        try:
            identifications = json.loads(encoded)
        except (TypeError, ValueError):
            identifications = []
    if isinstance(identifications, dict):
        identifications = [identifications]
    events = []
    if isinstance(identifications, list):
        for identification in identifications:
            if not isinstance(identification, dict):
                continue
            events.append(
                {
                    "event_type": "$identify",
                    "user_id": identification.get("user_id"),
                    "device_id": identification.get("device_id"),
                    "user_properties": identification.get("user_properties") or {},
                    "insert_id": identification.get("insert_id"),
                    "time": identification.get("time"),
                }
            )
    payload = _amplitude_payload_from(ctx, {"api_key": token, "events": events})
    return _amplitude_result(ctx, payload)


@router.route("OPTIONS", "/v1/collect")
def collect_preflight(ctx: Context) -> Result:
    return Result(status=204, body=None, content_type="text/plain", headers=_cors(ctx))


for _preflight_path in (
    "/v1/batch",
    "/v1/track",
    "/v1/page",
    "/v1/screen",
    "/v1/identify",
    "/v1/group",
    "/v1/alias",
    "/mp/collect",
    "/track",
    "/import",
    "/engage",
    "/groups",
    "/2/httpapi",
    "/batch",
    "/identify",
):
    router.route("OPTIONS", _preflight_path)(collect_preflight)


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
    if site:
        site_id = str(site["id"])
        request_meta = _request_metadata(ctx, site_id)
        keep, effective_rate = _keep_sample(
            flat, site_id, now, _sample_after_rps()
        )
        flat["_effective_sample_rate"] = effective_rate
        row = (
            _normalise(flat, ctx, site, now, {}, request_meta, {})
            if keep and not request_meta["is_bot"]
            else None
        )
    else:
        row = None
    if row:
        _written, claimed = _store(_with_session_start(row))
        _schedule_dashboards(claimed)
    headers = _cors(ctx)
    headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return Result(
        status=200,
        body=PIXEL,
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


def _identity_map(site_id: str) -> dict[str, str]:
    return {
        str(row["client_id"]): str(row["user_id"])
        for row in store.query(
            "SELECT client_id, user_id FROM analytics_identities WHERE site_id = ?",
            (site_id,),
        )
    }


@router.get("/v1/analytics/summary")
def summary(ctx: Context) -> Result:
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    start, end = _window(ctx)

    totals = store.row_to_dict(
        store.one(
            "SELECT COUNT(*) AS observed_events,"
            " COALESCE(SUM(COALESCE(e.sample_weight, 1)), 0) AS events,"
            " COUNT(DISTINCT COALESCE(e.user_id, i.user_id, e.client_id)) AS users,"
            " COUNT(DISTINCT e.session_id) AS sessions,"
            " SUM(CASE WHEN e.event_name = 'page_view'"
            " THEN COALESCE(e.sample_weight, 1) ELSE 0 END) AS page_views,"
            " SUM(e.engagement_ms) AS engagement_ms"
            " FROM analytics_events e LEFT JOIN analytics_identities i"
            " ON i.site_id = e.site_id AND i.client_id = e.client_id"
            " WHERE e.site_id = ? AND e.ts BETWEEN ? AND ?",
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
            f"SELECT e.{column} AS key,"
            " COALESCE(SUM(COALESCE(e.sample_weight, 1)), 0) AS events,"
            " COUNT(*) AS observed_events,"
            " COUNT(DISTINCT COALESCE(e.user_id, i.user_id, e.client_id)) AS users"
            " FROM analytics_events e LEFT JOIN analytics_identities i"
            " ON i.site_id = e.site_id AND i.client_id = e.client_id"
            " WHERE e.site_id = ? AND e.ts BETWEEN ? AND ?"
            f" AND e.{column} IS NOT NULL AND e.{column} != ''"
            f" GROUP BY e.{column} ORDER BY events DESC LIMIT {limit}",
            (site_id, start, end),
        )
        return store.rows_to_dicts(rows)

    return json_result(
        {
            "site_id": site_id,
            "window": {"start": start, "end": end},
            "totals": {
                "events": round(float(totals.get("events") or 0)),
                "observed_events": int(totals.get("observed_events") or 0),
                "users": int(totals.get("users") or 0),
                "sessions": sessions,
                "page_views": round(float(totals.get("page_views") or 0)),
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
    clauses = ["site_id = ?", "ts BETWEEN ? AND ?"]
    values: list[Any] = [site_id, start, end]
    event_name = _clip(ctx.q("event"), 64)
    source = _clip(ctx.q("source"), 32).lower()
    if event_name:
        clauses.append("event_name = ?")
        values.append(event_name)
    if source:
        clauses.append("source = ?")
        values.append(source)
    values.append(limit)
    rows = store.query(
        "SELECT event_name, ts, page_path, referrer_host, device, browser, country, client_id,"
        " session_id, user_id, sample_rate, sample_weight, source, external_id, props"
        " FROM analytics_events WHERE "
        + " AND ".join(clauses)
        + " ORDER BY ts DESC LIMIT ?",
        values,
    )
    events = store.rows_to_dicts(rows)
    identities = _identity_map(site_id)
    for event in events:
        event["props"] = store.load_json(event.get("props"), {})
        event["person_id"] = (
            event.get("user_id")
            or identities.get(str(event.get("client_id") or ""))
            or event.get("client_id")
        )
    return json_result({"site_id": site_id, "events": events})


def _property_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


@router.get("/v1/analytics/schema")
def event_schema(ctx: Context) -> Result:
    """Discover custom events and properties without requiring a predefined schema."""
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    start, end = _window(ctx)
    selected_event = _clip(ctx.q("event"), 64)
    clauses = ["site_id = ?", "ts BETWEEN ? AND ?"]
    values: list[Any] = [site_id, start, end]
    if selected_event:
        clauses.append("event_name = ?")
        values.append(selected_event)
    rows = store.rows_to_dicts(
        store.query(
            "SELECT event_name, ts, client_id, user_id, sample_weight, source, props"
            " FROM analytics_events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY ts DESC LIMIT 100001",
            values,
        )
    )
    truncated = len(rows) > 100000
    rows = rows[:100000]
    identities = _identity_map(site_id)
    catalog: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("event_name") or "")
        entry = catalog.setdefault(
            name,
            {
                "name": name,
                "observed_events": 0,
                "estimated_events": 0.0,
                "_users": set(),
                "first_seen": float(row["ts"]),
                "last_seen": float(row["ts"]),
                "_sources": {},
                "_properties": {},
            },
        )
        entry["observed_events"] += 1
        entry["estimated_events"] += float(row.get("sample_weight") or 1)
        entry["first_seen"] = min(entry["first_seen"], float(row["ts"]))
        entry["last_seen"] = max(entry["last_seen"], float(row["ts"]))
        client_id = str(row.get("client_id") or "")
        person_id = row.get("user_id") or identities.get(client_id) or client_id
        if person_id:
            entry["_users"].add(str(person_id))
        event_source = str(row.get("source") or "native")
        entry["_sources"][event_source] = entry["_sources"].get(event_source, 0) + 1
        props = store.load_json(row.get("props"), {})
        if not isinstance(props, dict):
            continue
        for key, value in props.items():
            prop = entry["_properties"].setdefault(
                str(key), {"name": str(key), "types": set(), "observed_events": 0}
            )
            prop["types"].add(_property_type(value))
            prop["observed_events"] += 1

    events = []
    for entry in catalog.values():
        entry["estimated_events"] = round(entry["estimated_events"])
        entry["users"] = len(entry.pop("_users"))
        entry["sources"] = [
            {"name": name, "observed_events": count}
            for name, count in sorted(entry.pop("_sources").items())
        ]
        properties = list(entry.pop("_properties").values())
        for prop in properties:
            prop["types"] = sorted(prop["types"])
        entry["properties"] = sorted(
            properties, key=lambda prop: (-prop["observed_events"], prop["name"])
        )
        events.append(entry)
    events.sort(key=lambda entry: (-entry["estimated_events"], entry["name"]))
    return json_result(
        {
            "site_id": site_id,
            "window": {"start": start, "end": end},
            "truncated": truncated,
            "events": events,
        }
    )


@router.get("/v1/analytics/groups")
def analytics_groups(ctx: Context) -> Result:
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    start, end = _window(ctx)
    selected_type = _clip(ctx.q("type"), 64)
    clauses = ["site_id = ?"]
    values: list[Any] = [site_id]
    if selected_type:
        clauses.append("group_type = ?")
        values.append(selected_type)
    profiles = store.rows_to_dicts(
        store.query(
            "SELECT group_type, group_id, traits, first_seen, last_seen"
            " FROM analytics_groups WHERE "
            + " AND ".join(clauses)
            + " ORDER BY last_seen DESC LIMIT 1000",
            values,
        )
    )
    link_type = " AND eg.group_type = ?" if selected_type else ""
    link_values: tuple[Any, ...] = (
        (site_id, start, end, selected_type)
        if selected_type
        else (site_id, start, end)
    )
    metric_rows = store.rows_to_dicts(
        store.query(
            "SELECT eg.group_type, eg.group_id, COUNT(*) AS observed_events,"
            " COALESCE(SUM(COALESCE(e.sample_weight, 1)), 0) AS events,"
            " COUNT(DISTINCT COALESCE(e.user_id, i.user_id, e.client_id)) AS users"
            " FROM analytics_event_groups eg JOIN analytics_events e ON e.id = eg.event_id"
            " LEFT JOIN analytics_identities i"
            " ON i.site_id = e.site_id AND i.client_id = e.client_id"
            " WHERE eg.site_id = ? AND e.ts BETWEEN ? AND ?"
            + link_type
            + " GROUP BY eg.group_type, eg.group_id",
            link_values,
        )
    )
    metrics = {
        (str(row["group_type"]), str(row["group_id"])): row for row in metric_rows
    }
    out = []
    for profile in profiles:
        key = (str(profile["group_type"]), str(profile["group_id"]))
        metric = metrics.get(key, {"observed_events": 0, "events": 0, "users": 0})
        profile["traits"] = store.load_json(profile.get("traits"), {})
        profile["observed_events"] = metric["observed_events"]
        profile["estimated_events"] = round(metric["events"])
        profile["users"] = int(metric["users"] or 0)
        out.append(profile)
    return json_result(
        {"site_id": site_id, "window": {"start": start, "end": end}, "groups": out}
    )


@router.get("/v1/analytics/paths")
def analytics_paths(ctx: Context) -> Result:
    """Discover common session paths instead of requiring predefined funnel steps."""
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    start, end = _window(ctx)
    depth = max(2, min(ctx.q_int("depth", 5), 12))
    limit = max(1, min(ctx.q_int("limit", 20), 100))
    start_node = _clip(ctx.q("start"), 128)
    mode = _clip(ctx.q("mode") or "both", 16).lower()
    if mode not in ("both", "events", "pages"):
        return error(400, "invalid_path_mode")
    rows = store.rows_to_dicts(
        store.query(
            "SELECT session_id, client_id, user_id, event_name, page_path,"
            " sample_weight FROM analytics_events"
            " WHERE site_id = ? AND ts BETWEEN ? AND ?"
            " AND event_name != 'session_start'"
            " ORDER BY session_id, ts, received_at, event_sequence, id LIMIT 200001",
            (site_id, start, end),
        )
    )
    truncated = len(rows) > 200000
    rows = rows[:200000]
    identities = _identity_map(site_id)
    sessions: dict[str, dict[str, Any]] = {}
    for row in rows:
        event_name = str(row.get("event_name") or "")
        page_path = str(row.get("page_path") or "")
        if mode == "events":
            node = event_name
        elif mode == "pages":
            if event_name != "page_view" or not page_path:
                continue
            node = page_path
        else:
            node = f"page_view:{page_path}" if event_name == "page_view" and page_path else event_name
        session_id = str(row.get("session_id") or "")
        if not session_id or not node:
            continue
        entry = sessions.setdefault(
            session_id,
            {
                "nodes": [],
                "weight": 1.0,
                "person_id": (
                    row.get("user_id")
                    or identities.get(str(row.get("client_id") or ""))
                    or row.get("client_id")
                ),
            },
        )
        if not entry["nodes"] or entry["nodes"][-1] != node:
            entry["nodes"].append(node)
        entry["weight"] = max(entry["weight"], float(row.get("sample_weight") or 1))

    paths: dict[tuple[str, ...], dict[str, Any]] = {}
    eligible_sessions = 0
    for entry in sessions.values():
        nodes = entry["nodes"]
        if start_node:
            try:
                offset = nodes.index(start_node)
            except ValueError:
                continue
            nodes = nodes[offset:]
        if not nodes:
            continue
        eligible_sessions += 1
        key = tuple(nodes[:depth])
        path = paths.setdefault(
            key, {"path": list(key), "sessions": 0, "estimated_sessions": 0.0, "_users": set()}
        )
        path["sessions"] += 1
        path["estimated_sessions"] += entry["weight"]
        if entry["person_id"]:
            path["_users"].add(str(entry["person_id"]))
    out = []
    for path in paths.values():
        path["estimated_sessions"] = round(path["estimated_sessions"])
        path["users"] = len(path.pop("_users"))
        path["rate"] = path["sessions"] / eligible_sessions if eligible_sessions else 0.0
        out.append(path)
    out.sort(key=lambda path: (-path["estimated_sessions"], -path["sessions"], path["path"]))
    return json_result(
        {
            "site_id": site_id,
            "window": {"start": start, "end": end},
            "mode": mode,
            "start": start_node or None,
            "depth": depth,
            "truncated": truncated,
            "sessions": eligible_sessions,
            "paths": out[:limit],
        }
    )


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@router.get("/v1/analytics/revenue")
def analytics_revenue(ctx: Context) -> Result:
    """Currency-separated revenue totals; no implicit FX conversion."""
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    start, end = _window(ctx)
    rows = store.rows_to_dicts(
        store.query(
            "SELECT client_id, user_id, event_name, sample_weight, props"
            " FROM analytics_events WHERE site_id = ? AND ts BETWEEN ? AND ?"
            " ORDER BY ts DESC LIMIT 200001",
            (site_id, start, end),
        )
    )
    truncated = len(rows) > 200000
    rows = rows[:200000]
    identities = _identity_map(site_id)
    totals: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        props = store.load_json(row.get("props"), {})
        if not isinstance(props, dict):
            continue
        amount = None
        for key in ("revenue", "value", "$revenue", "amount"):
            amount = _number(props.get(key))
            if amount is not None:
                break
        if amount is None:
            price = _number(props.get("$price"))
            quantity = _number(props.get("$quantity")) or 1.0
            amount = price * quantity if price is not None else None
        if amount is None:
            continue
        event_name = str(row.get("event_name") or "")
        if event_name.lower() == "refund":
            amount = -abs(amount)
        currency = _clip(props.get("currency") or props.get("$currency") or "unspecified", 12).upper()
        key = (currency, event_name)
        entry = totals.setdefault(
            key,
            {
                "currency": currency,
                "event": event_name,
                "observed_transactions": 0,
                "revenue": 0.0,
                "_users": set(),
            },
        )
        entry["observed_transactions"] += 1
        entry["revenue"] += amount * float(row.get("sample_weight") or 1)
        client_id = str(row.get("client_id") or "")
        person_id = row.get("user_id") or identities.get(client_id) or client_id
        if person_id:
            entry["_users"].add(str(person_id))
    breakdown = []
    by_currency: dict[str, float] = {}
    for entry in totals.values():
        entry["revenue"] = round(entry["revenue"], 6)
        entry["users"] = len(entry.pop("_users"))
        by_currency[entry["currency"]] = by_currency.get(entry["currency"], 0.0) + entry["revenue"]
        breakdown.append(entry)
    breakdown.sort(key=lambda entry: (entry["currency"], -entry["revenue"], entry["event"]))
    return json_result(
        {
            "site_id": site_id,
            "window": {"start": start, "end": end},
            "truncated": truncated,
            "currencies": [
                {"currency": currency, "revenue": round(amount, 6)}
                for currency, amount in sorted(by_currency.items())
            ],
            "breakdown": breakdown,
        }
    )


@router.get("/v1/analytics/funnel")
def funnel(ctx: Context) -> Result:
    """Count sessions that reach ordered event stages in the reporting window."""
    site, denied = _site_for_read(ctx)
    if denied:
        return denied
    site_id = str(site["id"])
    raw_steps = ctx.q("steps", "")
    steps: list[str] = []
    seen: set[str] = set()
    for value in raw_steps.split(","):
        step = _clip(value, 64)
        if step and step not in seen:
            steps.append(step)
            seen.add(step)
    if len(steps) < 2:
        return error(400, "at_least_two_steps_required")
    if len(steps) > 12:
        return error(400, "too_many_steps")

    start, end = _window(ctx)
    event_placeholders = ", ".join("?" for _ in steps)
    rows = store.query(
        "SELECT session_id, client_id, user_id, ts, event_name, sample_weight"
        " FROM analytics_events"
        " WHERE site_id = ? AND ts BETWEEN ? AND ?"
        f" AND event_name IN ({event_placeholders})"
        " ORDER BY session_id, ts, received_at, event_sequence, id",
        (site_id, start, end, *steps),
    )
    sessions: dict[str, dict[str, Any]] = {}
    identities = _identity_map(site_id)
    for row in store.rows_to_dicts(rows):
        session_id = str(row.get("session_id") or "")
        if not session_id:
            continue
        entry = sessions.setdefault(
            session_id,
            {"client_id": str(row.get("client_id") or ""), "events": []},
        )
        entry["events"].append(
            {
                "name": str(row.get("event_name") or ""),
                "user_id": str(row.get("user_id") or ""),
                "sample_weight": float(row.get("sample_weight") or 1),
            }
        )

    reached_sessions: list[set[str]] = [set() for _ in steps]
    reached_users: list[set[str]] = [set() for _ in steps]
    reached_weights: list[dict[str, float]] = [dict() for _ in steps]
    for session_id, entry in sessions.items():
        next_step = 0
        for event in entry["events"]:
            event_name = event["name"]
            if next_step >= len(steps) or event_name != steps[next_step]:
                continue
            reached_sessions[next_step].add(session_id)
            reached_weights[next_step][session_id] = max(
                reached_weights[next_step].get(session_id, 1.0),
                float(event["sample_weight"]),
            )
            person_id = (
                event["user_id"]
                or identities.get(entry["client_id"])
                or entry["client_id"]
            )
            if person_id:
                reached_users[next_step].add(person_id)
            next_step += 1

    first_sessions = len(reached_sessions[0])
    first_users = len(reached_users[0])
    first_estimated_sessions = sum(reached_weights[0].values())
    stages = []
    for index, event_name in enumerate(steps):
        sessions_count = len(reached_sessions[index])
        estimated_sessions = round(sum(reached_weights[index].values()))
        users_count = len(reached_users[index])
        previous_sessions = first_sessions if index == 0 else len(reached_sessions[index - 1])
        previous_estimated_sessions = (
            first_estimated_sessions
            if index == 0
            else sum(reached_weights[index - 1].values())
        )
        stages.append(
            {
                "event": event_name,
                "sessions": sessions_count,
                "estimated_sessions": estimated_sessions,
                "users": users_count,
                "rate": sessions_count / first_sessions if first_sessions else 0.0,
                "step_rate": sessions_count / previous_sessions if previous_sessions else 0.0,
                "dropoff": max(0, previous_sessions - sessions_count),
                "estimated_rate": (
                    estimated_sessions / first_estimated_sessions
                    if first_estimated_sessions
                    else 0.0
                ),
                "estimated_step_rate": (
                    estimated_sessions / previous_estimated_sessions
                    if previous_estimated_sessions
                    else 0.0
                ),
                "estimated_dropoff": max(
                    0, round(previous_estimated_sessions) - estimated_sessions
                ),
            }
        )

    return json_result(
        {
            "site_id": site_id,
            "window": {"start": start, "end": end},
            "steps": stages,
            "totals": {"sessions": first_sessions, "users": first_users},
        }
    )


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
        "SELECT ts, client_id, session_id, user_id, event_name, engagement_ms, sample_weight"
        " FROM analytics_events"
        " WHERE site_id = ? AND ts BETWEEN ? AND ?",
        (site_id, start, end),
    )
    buckets: dict[int, dict[str, Any]] = {}
    identities = _identity_map(site_id)
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
        weight = float(row.get("sample_weight") or 1)
        entry["events"] += weight
        if row["event_name"] == "page_view":
            entry["page_views"] += weight
        entry["_users"].add(
            row.get("user_id")
            or identities.get(str(row.get("client_id") or ""))
            or row["client_id"]
        )
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
    identities = _identity_map(site_id)
    for row in store.rows_to_dicts(rows):
        event_name = str(row["event_name"])
        event_type = (
            event_name if event_name in ("identify", "group", "alias") else
            ("page" if event_name == "page_view" else "track")
        )
        out.append(
            {
                "type": event_type,
                "event": event_name,
                "messageId": row.get("external_id") or row["id"],
                "anonymousId": row["client_id"],
                "userId": row["user_id"] or identities.get(str(row["client_id"])),
                "timestamp": datetime.fromtimestamp(
                    float(row["ts"]), tz=timezone.utc
                ).isoformat().replace("+00:00", "Z"),
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
                    "userAgent": f"{row['device']}; {row['browser']}; {row['os']}",
                },
                "properties": store.load_json(row.get("props"), {}),
                "sampleRate": float(row.get("sample_rate") or 1),
                "source": row.get("source") or "native",
            }
        )
    return json_result({"site_id": site_id, "batch": out})
