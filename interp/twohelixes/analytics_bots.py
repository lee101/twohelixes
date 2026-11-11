"""Headless-browser fingerprints that a user-agent check cannot see.

Automation running a stock Chrome UA still reports the screen and window it
was launched with. These rules were fitted against production sessions: each
matches no more than a handful of signed-in sessions (2026-11-11 fit: 3 of
~60k, all from the 1280x720 default window). Mobile and tablet are never
matched, and an event without both screen and viewport (server-side, pixel,
SDK) never is. Rules added on 2026-11-11 (default_window, the CHROME_PAIRS
fingerprints, synthetic_window) also require browser Chrome because Firefox
resistFingerprinting reports screen == viewport for real people.

`human_only_sql` is a separate, opt-in session filter for sites that emit
first_interaction; see its docstring.

`headless_fingerprint` drops new events at ingest; `HUMAN` / `HUMAN_E` exclude
the same rows from stored history at report time, so nothing is rewritten.
The two must agree, which tests/test_analytics_bots.py holds on both backends.
"""

from __future__ import annotations

import re

SMALL_SCREENS = ("800x600", "600x800")
AUTOMATION_VIEWPORTS = ("800x600", "1280x720")
DEFAULT_WINDOWS = ("1280x720",)
KNOWN = (("1920x1080", "1919x992"),)
CHROME_PAIRS = (
    ("1920x1080", "1920x961"),
    ("1440x900", "1080x1920"),
    ("1440x900", "945x939"),
    ("1536x960", "945x972"),
    ("1800x1125", "1800x1125"),
)
SYNTHETIC_MIN_WIDTH = 1100
SYNTHETIC_CHROME = (100, 140)
_SCREEN = re.compile(r"(\d{4})x(\d{4})", re.ASCII)
_VIEWPORT = re.compile(r"(\d{4})x(\d{3,4})", re.ASCII)
HUMAN_EVENTS = (
    "purchase", "sign_up", "generate_lead", "conversion", "begin_checkout",
    "game_started", "game_completed", "paywall_signup_clicked", "paywall_login_clicked",
)
HUMAN_INTERACTION_DELAY = 1.0


def _square(screen: str) -> bool:
    if len(screen) != 9 or screen[4] != "x":
        return False
    width, height = screen[:4], screen[5:]
    if not (width.isascii() and width.isdigit() and height.isascii() and height.isdigit()):
        return False
    return abs(int(width) - int(height)) * 10 <= int(width)


def _synthetic_window(screen: str, viewport: str) -> bool:
    """Scripted full-width window on a near-square screen of random size."""
    s, v = _SCREEN.fullmatch(screen), _VIEWPORT.fullmatch(viewport)
    if not s or not v or s[1] != v[1]:
        return False
    width, height, view = int(s[1]), int(s[2]), int(v[2])
    low, high = SYNTHETIC_CHROME
    return (
        width >= SYNTHETIC_MIN_WIDTH
        and height <= width
        and width * 10 < height * 12
        and low <= height - view <= high
    )


def headless_fingerprint(device: str, screen: str, viewport: str, browser: str = "Chrome") -> str:
    """The rule a desktop event matches, or ''. The window rules fitted in
    2026-11 apply to Chrome only: Firefox with resistFingerprinting reports
    screen == viewport for real people."""
    if device != "desktop" or not screen or not viewport:
        return ""
    if screen in SMALL_SCREENS:
        return "small_screen"
    if viewport in AUTOMATION_VIEWPORTS and screen != viewport:
        return "automation_viewport"
    if (screen, viewport) in KNOWN:
        return "known_fingerprint"
    if screen == viewport and _square(screen):
        return "square_fullscreen"
    if browser != "Chrome":
        return ""
    if screen == viewport and screen in DEFAULT_WINDOWS:
        return "default_window"
    if (screen, viewport) in CHROME_PAIRS:
        return "known_fingerprint"
    if _synthetic_window(screen, viewport):
        return "synthetic_window"
    return ""


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def bot_sql(alias: str = "") -> str:
    """Portable SQLite/PostgreSQL predicate, never NULL. Postgres raises on a
    bad integer cast, so the arithmetic sits behind a CASE guard."""
    prefix = f"{alias}." if alias else ""
    s = f"COALESCE({prefix}screen, '')"
    v = f"COALESCE({prefix}viewport, '')"
    digits = "'0123456789'"
    guard = (
        f"length({s}) = 9 AND substr({s}, 5, 1) = 'x'"
        f" AND ltrim(substr({s}, 1, 4), {digits}) = ''"
        f" AND ltrim(substr({s}, 6), {digits}) = ''"
    )
    width = f"CAST(substr({s}, 1, 4) AS INTEGER)"
    height = f"CAST(substr({s}, 6) AS INTEGER)"
    known = " OR ".join(f"({s} = '{a}' AND {v} = '{b}')" for a, b in KNOWN)
    chrome_pairs = " OR ".join(f"({s} = '{a}' AND {v} = '{b}')" for a, b in CHROME_PAIRS)
    vh = f"CAST(substr({v}, 6) AS INTEGER)"
    vguard = (
        f"length({v}) IN (8, 9) AND substr({v}, 5, 1) = 'x'"
        f" AND ltrim(substr({v}, 1, 4), {digits}) = '' AND ltrim(substr({v}, 6), {digits}) = ''"
        f" AND substr({v}, 1, 5) = substr({s}, 1, 5)"
    )
    low, high = SYNTHETIC_CHROME
    synthetic = (
        f"CASE WHEN {guard} AND {vguard} THEN ({width} >= {SYNTHETIC_MIN_WIDTH}"
        f" AND {height} <= {width} AND {width} * 10 < {height} * 12"
        f" AND {height} - {vh} BETWEEN {low} AND {high}) ELSE 1 = 0 END"
    )
    return (
        f"(COALESCE({prefix}device, '') = 'desktop' AND {s} != '' AND {v} != '' AND ("
        f"{s} IN ({_quoted(SMALL_SCREENS)})"
        f" OR ({v} IN ({_quoted(AUTOMATION_VIEWPORTS)}) AND {s} != {v})"
        f" OR {known}"
        f" OR (COALESCE({prefix}browser, '') = 'Chrome' AND (({s} = {v} AND {s} IN ({_quoted(DEFAULT_WINDOWS)}))"
        f" OR {chrome_pairs} OR {synthetic}))"
        f" OR ({s} = {v} AND CASE WHEN {guard}"
        f" THEN abs({width} - {height}) * 10 <= {width} ELSE 1 = 0 END)))"
    )


HUMAN = f" AND NOT {bot_sql()}"
HUMAN_E = f" AND NOT {bot_sql('e')}"


def human_only_since(site_id: str) -> float | None:
    """Start of the site's first session that reported a first_interaction;
    None if it never has. Sessions before that carry no interaction signal, so
    `human_only` reports clip their window to start here instead of counting
    them all as bots."""
    from twohelixes import store

    first = store.row_to_dict(store.one(
        "SELECT session_id FROM analytics_events WHERE site_id = ? AND event_name = 'first_interaction'"
        " ORDER BY ts LIMIT 1",
        (site_id,),
    ))
    if not first:
        return None
    row = store.row_to_dict(store.one(
        "SELECT MIN(ts) AS since FROM analytics_events WHERE site_id = ? AND session_id = ?",
        (site_id, first["session_id"]),
    )) or {}
    since = row.get("since")
    return float(since) if since is not None else None


def human_only_sql(site_id: str, start: float, end: float, alias: str = "") -> str:
    """Keep only sessions with a human signal: an interaction at least
    HUMAN_INTERACTION_DELAY seconds after the session began (scripted wheel
    events fire at load), a signed-in user, or an intent event."""
    prefix = f"{alias}." if alias else ""
    site = site_id.replace("'", "''")
    events = ", ".join(f"'{name}'" for name in HUMAN_EVENTS)
    return (
        f" AND {prefix}session_id IN (SELECT h.session_id FROM analytics_events h"
        f" WHERE h.site_id = '{site}' AND h.ts BETWEEN {float(start)!r} AND {float(end)!r}"
        " GROUP BY h.session_id HAVING"
        f" MAX(CASE WHEN h.event_name = 'first_interaction' THEN h.ts END) - MIN(h.ts) >= {HUMAN_INTERACTION_DELAY}"
        " OR MAX(CASE WHEN h.user_id IS NOT NULL AND h.user_id <> '' THEN 1 ELSE 0 END) = 1"
        f" OR MAX(CASE WHEN h.event_name IN ({events}) THEN 1 ELSE 0 END) = 1)"
    )
