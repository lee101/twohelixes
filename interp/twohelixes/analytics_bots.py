"""Headless-browser fingerprints that a user-agent check cannot see.

Automation running a stock Chrome UA still reports the screen and window it
was launched with. These rules were fitted against production sessions: every
one matched zero signed-in sessions. Mobile and tablet are never matched, and
an event without both screen and viewport (server-side, pixel, SDK) never is.

`headless_fingerprint` drops new events at ingest; `HUMAN` / `HUMAN_E` exclude
the same rows from stored history at report time, so nothing is rewritten.
The two must agree, which tests/test_analytics_bots.py holds on both backends.
"""

from __future__ import annotations

SMALL_SCREENS = ("800x600", "600x800")
AUTOMATION_VIEWPORTS = ("800x600", "1280x720")
KNOWN = (("1920x1080", "1919x992"),)


def _square(screen: str) -> bool:
    if len(screen) != 9 or screen[4] != "x":
        return False
    width, height = screen[:4], screen[5:]
    if not (width.isascii() and width.isdigit() and height.isascii() and height.isdigit()):
        return False
    return abs(int(width) - int(height)) * 10 <= int(width)


def headless_fingerprint(device: str, screen: str, viewport: str) -> str:
    """The rule a desktop event matches, or ''."""
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
    return (
        f"(COALESCE({prefix}device, '') = 'desktop' AND {s} != '' AND {v} != '' AND ("
        f"{s} IN ({_quoted(SMALL_SCREENS)})"
        f" OR ({v} IN ({_quoted(AUTOMATION_VIEWPORTS)}) AND {s} != {v})"
        f" OR {known}"
        f" OR ({s} = {v} AND CASE WHEN {guard}"
        f" THEN abs({width} - {height}) * 10 <= {width} ELSE 1 = 0 END)))"
    )


HUMAN = f" AND NOT {bot_sql()}"
HUMAN_E = f" AND NOT {bot_sql('e')}"
