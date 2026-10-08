import time

import pytest

from test_analytics import SITE, collect, dispatch, fresh_db, rows  # noqa: F401
from twohelixes import config, store
from twohelixes.analytics_api import compact_summary
from twohelixes.analytics_bots import bot_sql, headless_fingerprint

DESKTOP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"
MOBILE_UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/141.0 Mobile Safari/537.36"

CASES = [
    ("desktop", "1366x1366", "1366x1366", "square_fullscreen"),
    ("desktop", "1280x1200", "1280x1200", "square_fullscreen"),
    ("desktop", "1600x1600", "1600x1600", "square_fullscreen"),
    ("desktop", "800x600", "1024x569", "small_screen"),
    ("desktop", "800x600", "800x600", "small_screen"),
    ("desktop", "600x800", "800x600", "small_screen"),
    ("desktop", "1920x1080", "800x600", "automation_viewport"),
    ("desktop", "1920x1080", "1280x720", "automation_viewport"),
    ("desktop", "1920x1080", "1919x992", "known_fingerprint"),
    ("desktop", "1920x1080", "1920x1080", ""),
    ("desktop", "1280x720", "1280x720", ""),
    ("desktop", "1440x900", "1440x900", ""),
    ("desktop", "1280x1024", "1280x1024", ""),
    ("desktop", "1366x1366", "1366x1200", ""),
    ("desktop", "1920x1080", "1919x958", ""),
    ("desktop", "1920x1080", "1920x945", ""),
    ("desktop", "13x6x1366", "13x6x1366", ""),
    ("desktop", "abcdxefgh", "abcdxefgh", ""),
    ("desktop", "", "800x600", ""),
    ("desktop", "800x600", "", ""),
    ("mobile", "360x640", "360x640", ""),
    ("mobile", "800x600", "800x600", ""),
    ("tablet", "1024x1024", "1024x1024", ""),
    ("unknown", "1366x1366", "1366x1366", ""),
]


@pytest.mark.parametrize("device,screen,viewport,rule", CASES)
def test_headless_fingerprint_rules(device: str, screen: str, viewport: str, rule: str) -> None:
    assert headless_fingerprint(device, screen, viewport) == rule


def test_sql_predicate_agrees_with_python_on_this_backend() -> None:
    assert collect([{"event": "page_view", "client_id": "seed", "session_id": "seed"}]) == 204
    template = rows()[0]
    seeded = len(rows(include_session_start=True))
    columns = [key for key in template if key != "id"]
    with store.transaction() as conn:
        for index, (device, screen, viewport, _rule) in enumerate(CASES + [("desktop", None, None, "")]):
            values = dict(template, device=device, screen=screen, viewport=viewport,
                          client_id=f"case-{index}", session_id=f"case-{index}", external_id=None)
            conn.execute(
                f"INSERT INTO analytics_events (id, {', '.join(columns)})"
                f" VALUES (?, {', '.join('?' for _ in columns)})",
                (store.new_id(), *[values[column] for column in columns]),
            )
    found = store.rows_to_dicts(store.query(
        f"SELECT device, screen, viewport, CASE WHEN {bot_sql()} THEN 1 ELSE 0 END AS bot,"
        f" CASE WHEN NOT {bot_sql('e')} THEN 1 ELSE 0 END AS human"
        " FROM analytics_events e WHERE site_id = ?",
        (SITE["id"],),
    ))
    assert len(found) == len(CASES) + 1 + seeded
    for row in found:
        expected = int(bool(headless_fingerprint(row["device"] or "", row["screen"] or "", row["viewport"] or "")))
        assert (row["bot"], row["human"]) == (expected, 1 - expected), row


def _send(screen: str, viewport: str, ua: str = DESKTOP_UA, client: str = "c") -> int:
    status, _ = dispatch(
        "POST",
        "/v1/collect",
        body=__import__("json").dumps({"site_id": SITE["write_key"], "events": [
            {"event": "page_view", "client_id": client, "session_id": client,
             "screen": screen, "viewport": viewport, "page_location": "https://netwrck.com/"},
        ]}),
        headers={"user-agent": ua},
    )
    return status


def test_headless_fingerprints_are_dropped_at_ingest() -> None:
    assert _send("1366x1366", "1366x1366", client="square") == 204
    assert _send("800x600", "1024x569", client="small") == 204
    assert _send("1920x1080", "1919x992", client="known") == 204
    assert rows() == []
    assert _send("1920x1080", "1920x945", client="human") == 204
    assert _send("360x640", "360x640", ua=MOBILE_UA, client="phone") == 204
    assert sorted(row["client_id"] for row in rows()) == ["human", "phone"]


@pytest.mark.parametrize("ua", [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 HeadlessChrome/141.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11) Chrome/141.0 Mobile Safari/537.36 Chrome-Lighthouse",
    "Mozilla/5.0 (Windows NT 10.0) Chrome/141.0 Safari/537.36 PTST/250901",
])
def test_automation_user_agents_are_dropped(ua: str) -> None:
    assert _send("1920x1080", "1920x945", ua=ua) == 204
    assert rows() == []


def _history() -> None:
    now = time.time() * 1000 - 10
    events = [
        {"event": "page_view", "client_id": "human", "session_id": "h1", "ts": now,
         "page_location": "https://netwrck.com/pricing", "screen": "1920x1080", "viewport": "1920x945"},
        {"event": "page_view", "client_id": "human", "session_id": "h1", "ts": now + 1,
         "page_location": "https://netwrck.com/signup", "screen": "1920x1080", "viewport": "1920x945"},
        {"event": "sign_up", "client_id": "human", "session_id": "h1", "ts": now + 2,
         "screen": "1920x1080", "viewport": "1920x945"},
    ]
    for index in range(3):
        events += [
            {"event": "page_view", "client_id": f"bot{index}", "session_id": f"b{index}", "ts": now,
             "page_location": "https://netwrck.com/signup", "screen": "1920x1080", "viewport": "1920x945"},
            {"event": "sign_up", "client_id": f"bot{index}", "session_id": f"b{index}", "ts": now + 1,
             "screen": "1920x1080", "viewport": "1920x945"},
        ]
    assert collect(events) == 204
    store.execute(
        "UPDATE analytics_events SET screen = '1366x1366', viewport = '1366x1366'"
        " WHERE site_id = ? AND client_id IN ('bot0', 'bot1', 'bot2')",
        (SITE["id"],),
    )


def test_reports_ignore_stored_headless_history() -> None:
    _history()
    base = f"site_id={SITE['id']}&days=1"

    status, summary = dispatch("GET", "/v1/analytics/summary", query=base)
    assert status == 200
    assert summary["totals"]["sessions"] == 1
    assert summary["totals"]["users"] == 1
    assert summary["totals"]["page_views"] == 2
    assert summary["totals"]["bounces"] == 0
    assert {row["key"]: row["events"] for row in summary["top_pages"]}["/signup"] == 1

    status, everything = dispatch("GET", "/v1/analytics/summary", query=base + "&include_bots=1")
    assert everything["totals"]["sessions"] == 4

    status, compact = dispatch("GET", "/v1/analytics/summary", query=base + "&compact=1")
    assert compact["totals"]["sessions"] == 1
    assert compact["totals"]["bounce_rate"] == 0.0
    assert compact_summary(SITE["id"], time.time() - 86400, time.time() + 1, include_bots=True)["totals"]["sessions"] == 4

    status, series = dispatch("GET", "/v1/analytics/timeseries", query=base)
    assert sum(point["sessions"] for point in series["series"]) == 1

    status, report = dispatch("GET", "/v1/analytics/funnel", query=base + "&steps=page_view,sign_up")
    assert [stage["sessions"] for stage in report["steps"]] == [1, 1]
    status, report = dispatch("GET", "/v1/analytics/funnel", query=base + "&steps=page_view,sign_up&include_bots=1")
    assert [stage["sessions"] for stage in report["steps"]] == [4, 4]

    status, paths = dispatch("GET", "/v1/analytics/paths", query=base + "&mode=events")
    assert paths["sessions"] == 1


def _funnel(steps: str) -> tuple[int, dict]:
    from urllib.parse import quote

    return dispatch("GET", "/v1/analytics/funnel", query=f"site_id={SITE['id']}&days=1&steps={quote(steps)}")


def test_funnel_path_and_alternative_qualifiers() -> None:
    now = time.time() * 1000 - 10

    def visit(client: str, path: str, then: str | None) -> list[dict]:
        events = [{"event": "page_view", "client_id": client, "session_id": client, "ts": now,
                   "page_location": f"https://netwrck.com{path}"}]
        if then:
            events.append({"event": then, "client_id": client, "session_id": client, "ts": now + 1})
        return events

    assert collect(
        visit("a", "/signup", "paywall_open")
        + visit("b", "/signup/", "paywall")
        + visit("c", "/signup/google", "paywall_open")
        + visit("d", "/pricing", "paywall_open")
        + visit("e", "/signups", None)
    ) == 204

    status, report = _funnel("page_view:/signup,paywall_open|paywall")
    assert status == 200
    assert [stage["sessions"] for stage in report["steps"]] == [2, 2]
    assert report["steps"][0]["event"] == "page_view:/signup"
    assert report["steps"][0]["match"] == [{"event": "page_view", "path": "/signup", "prefix": False}]
    assert [m["event"] for m in report["steps"][1]["match"]] == ["paywall_open", "paywall"]

    status, report = _funnel("page_view:/signup*,paywall_open")
    assert [stage["sessions"] for stage in report["steps"]] == [4, 2]

    status, report = _funnel("page_view:/signup/*|page_view:/pricing,paywall_open|paywall")
    assert [stage["sessions"] for stage in report["steps"]] == [3, 3]

    status, report = _funnel("page_view,paywall_open")
    assert [stage["sessions"] for stage in report["steps"]] == [5, 3]


def test_funnel_qualifier_edge_cases() -> None:
    now = time.time() * 1000 - 10
    long_name = "x" * 64
    assert collect([
        {"event": "checkout:started", "client_id": "a", "session_id": "a", "ts": now},
        {"event": long_name, "client_id": "a", "session_id": "a", "ts": now + 1,
         "page_location": "https://netwrck.com/a"},
    ]) == 204
    status, report = _funnel(f"checkout:started,{long_name}:/a|{long_name}:/b|{long_name}:/c")
    assert status == 200
    assert [stage["sessions"] for stage in report["steps"]] == [1, 1]

    status, body = _funnel("a,b|c|d|e|f|g|h|i|j")
    assert status == 400
    assert body["error"] == "too_many_step_alternatives"

    status, body = _funnel("a,|,")
    assert status == 400
    assert body["error"] == "at_least_two_steps_required"


def test_tracker_skips_automated_browsers() -> None:
    source = (config.REPO_ROOT / "web" / "src" / "th.js").read_text()
    assert "navigator.webdriver === true" in source
    assert "HeadlessChrome" in source
    built = (config.REPO_ROOT / "static" / "th.js").read_text()
    assert "webdriver" in built


def test_tracker_dedupes_history_page_views_and_keeps_queued_identity() -> None:
    source = (config.REPO_ROOT / "web" / "src" / "th.js").read_text()
    assert "if (pageKey() !== lastPageKey) page();" in source
    assert "config.autoIdentify === false" in source
    assert "userId === autoUserId" in source
