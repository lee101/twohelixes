"""Analytics ingest and reporting, exercised through the real router.

The ingest path is the one that has to be unbreakable: it runs on someone
else's page, so a rejected batch is data we never get back. These tests push
the shapes a browser actually sends - a sendBeacon body with no content type, a
Segment-style call, a GA-style query string - plus the malformed ones, and
assert that bad input is dropped rather than fatal.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Any

import pytest

os.environ.setdefault("TWOHELIXES_DATA_DIR", tempfile.mkdtemp(prefix="th-analytics-"))

from twohelixes import auth, router, store  # noqa: E402
from twohelixes.routes import analytics as analytics_routes  # noqa: E402
from twohelixes.routes import dashboards as dashboard_routes  # noqa: E402
from twohelixes.routes import teams  # noqa: E402
from twohelixes.routes import pages  # noqa: E402
from twohelixes.charts import defaults as chart_defaults  # noqa: E402

SITE: dict[str, str] = {}


@pytest.fixture(autouse=True)
def fresh_db(tmp_path: Any, monkeypatch: Any) -> Any:
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    store.init()
    router.build()
    user = store.create_user("analytics-owner@test.local")
    site_id = store.new_id()
    write_key = "thw_test_analytics"
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO analytics_sites"
            " (id, user_id, domain, name, write_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (site_id, user["id"], "netwrck.com", "Netwrck", write_key, time.time()),
        )
    SITE.update(
        {
            "id": site_id,
            "write_key": write_key,
            "owner_key": auth.api_key_for(user["id"]),
        }
    )
    monkeypatch.setattr(analytics_routes, "_schedule_dashboards", lambda claimed: None)
    yield
    # Leave the module-level store as we found it: the next test module has its
    # own data dir, and an already-"initialised" store would skip creating it.
    store.close()
    store._initialised = False


def dispatch(
    method: str,
    path: str,
    query: str = "",
    body: str = "",
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    request_headers = {
        "user-agent": "Mozilla/5.0 (Macintosh) Chrome/120",
        "authorization": f"Bearer {SITE.get('owner_key', '')}",
    }
    request_headers.update(headers or {})
    header_blob = json.dumps(request_headers)
    status, ctype, _extra, payload = router.handle(method, path, query, body, header_blob)
    parsed: Any = payload
    if payload and ctype.startswith("application/json"):
        parsed = json.loads(payload)
    return int(status), parsed


def collect(events: list[dict[str, Any]], site_id: str | None = None) -> int:
    status, _ = dispatch(
        "POST",
        "/v1/collect",
        body=json.dumps({"site_id": site_id or SITE["write_key"], "events": events}),
    )
    return status


def rows(site_id: str | None = None, include_session_start: bool = False) -> list[dict[str, Any]]:
    suffix = "" if include_session_start else " AND event_name != 'session_start'"
    return store.rows_to_dicts(
        store.query(
            f"SELECT * FROM analytics_events WHERE site_id = ?{suffix} ORDER BY ts",
            (site_id or SITE["id"],),
        )
    )


def test_a_beacon_batch_is_stored() -> None:
    now = time.time() * 1000
    assert (
        collect(
            [
                {"event": "page_view", "client_id": "c1", "session_id": "s1", "ts": now,
                 "page_location": "https://netwrck.com/ai-chat/Aria?utm_source=x",
                 "page_title": "Aria", "referrer": "https://google.com/search"},
                {"event": "signup_started", "client_id": "c1", "session_id": "s1", "ts": now,
                 "props": {"plan": "free"}},
            ]
        )
        == 204
    )

    stored = rows()
    assert [r["event_name"] for r in stored] == ["page_view", "signup_started"]
    assert stored[0]["page_path"] == "/ai-chat/Aria"
    assert stored[0]["referrer_host"] == "google.com"
    assert stored[0]["browser"] == "Chrome"
    assert stored[0]["device"] == "desktop"
    assert json.loads(stored[1]["props"])["plan"] == "free"
    # The raw IP is never stored, only a salted hash.
    assert stored[0]["ip_hash"]
    assert "0.0.0.0" not in (stored[0]["ip_hash"] or "")


def test_malformed_events_are_dropped_not_fatal() -> None:
    status, _ = dispatch("POST", "/v1/collect", body="{not json")
    assert status == 204

    assert collect([{"event": "no_client"}, "a string", {"client_id": "c2"}]) == 204
    assert rows() == []

    # A good event alongside bad ones still lands.
    assert collect([{"bad": True}, {"event": "ok", "client_id": "c3"}]) == 204
    assert [r["event_name"] for r in rows()] == ["ok"]


def test_bots_are_not_counted() -> None:
    status, _ = dispatch(
        "POST",
        "/v1/collect",
        body=json.dumps({"site_id": SITE["write_key"], "events": [{"event": "page_view", "client_id": "bot"}]}),
        headers={"user-agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"},
    )
    assert status == 204
    assert rows() == []


def test_segment_shape_and_identify_stitching() -> None:
    assert (
        collect(
            [
                {"type": "page", "anonymousId": "seg1", "properties": {"title": "Home"}},
                {"type": "track", "event": "Order Completed", "anonymousId": "seg1",
                 "properties": {"revenue": 42}},
                {"type": "identify", "anonymousId": "seg1", "userId": "user_9",
                 "traits": {"email": "a@b.c"}},
            ]
        )
        == 204
    )
    stored = rows()
    assert [r["event_name"] for r in stored] == ["page_view", "Order Completed", "identify"]

    identity = store.row_to_dict(
        store.one("SELECT * FROM analytics_identities WHERE site_id = ? AND client_id = ?",
                  (SITE["id"], "seg1"))
    )
    assert identity is not None
    assert identity["user_id"] == "user_9"
    assert json.loads(identity["traits"])["email"] == "a@b.c"


def test_ga_style_pixel_returns_a_gif_and_records() -> None:
    status, body = dispatch(
        "GET",
        "/v1/collect",
        query=f"tid={SITE['write_key']}&cid=pixel1&en=page_view&dl=https%3A%2F%2Fnetwrck.com%2F&dt=Home",
    )
    assert status == 200
    assert body.startswith("GIF89a")
    stored = rows()
    assert len(stored) == 1
    assert stored[0]["client_id"] == "pixel1"
    assert stored[0]["page_path"] == "/"


def test_sessions_are_derived_when_the_client_omits_one() -> None:
    collect([{"event": "page_view", "client_id": "c9"}])
    collect([{"event": "page_view", "client_id": "c9"}])
    stored = rows()
    assert len({r["session_id"] for r in stored}) == 1, "same visit should share a session"


def test_summary_and_timeseries_report_what_was_ingested() -> None:
    now = time.time() * 1000
    collect(
        [
            {"event": "page_view", "client_id": "a", "session_id": "s1", "ts": now,
             "page_location": "https://netwrck.com/", "referrer": "https://x.com/"},
            {"event": "page_view", "client_id": "b", "session_id": "s2", "ts": now,
             "page_location": "https://netwrck.com/tools"},
            {"event": "signup_started", "client_id": "b", "session_id": "s2", "ts": now,
             "engagement_ms": 4000},
        ]
    )

    status, summary = dispatch("GET", "/v1/analytics/summary", query=f"site_id={SITE['id']}&days=1")
    assert status == 200
    assert summary["totals"]["events"] == 5
    assert summary["totals"]["users"] == 2
    assert summary["totals"]["sessions"] == 2
    assert summary["totals"]["page_views"] == 2
    assert summary["totals"]["engagement_ms"] == 4000
    paths = {row["key"] for row in summary["top_pages"]}
    assert {"/", "/tools"} <= paths

    status, series = dispatch("GET", "/v1/analytics/timeseries", query=f"site_id={SITE['id']}&days=1")
    assert status == 200
    assert sum(point["events"] for point in series["series"]) == 5

    status, export = dispatch("GET", "/v1/analytics/export", query=f"site_id={SITE['id']}&days=1")
    assert status == 200
    page = next(event for event in export["batch"] if event["event"] == "page_view")
    assert page["type"] == "page"
    assert page["context"]["page"]["path"] == "/"


def test_summary_requires_a_site() -> None:
    status, body = dispatch("GET", "/v1/analytics/summary")
    assert status == 400
    assert body["error"] == "site_id_required"


def test_stranger_is_forbidden_from_every_read_endpoint() -> None:
    stranger = store.create_user("analytics-stranger@test.local")
    headers = {"authorization": f"Bearer {auth.api_key_for(stranger['id'])}"}
    for endpoint in ("summary", "events", "timeseries", "export"):
        status, body = dispatch(
            "GET",
            f"/v1/analytics/{endpoint}",
            query=f"site_id={SITE['id']}",
            headers=headers,
        )
        assert status == 403, endpoint
        assert body["error"] == "forbidden"


def test_team_member_can_read_a_shared_site() -> None:
    teams.ensure_schema()
    owner = store.get_user_by_email("analytics-owner@test.local")
    mate = store.create_user("analytics-mate@test.local")
    team_id = store.new_id()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO teams (id, name, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (team_id, "Analytics", owner["id"], time.time()),
        )
        conn.execute(
            "INSERT INTO team_members (team_id, user_id, role, added_at)"
            " VALUES (?, ?, 'owner', ?)",
            (team_id, owner["id"], time.time()),
        )
        conn.execute(
            "INSERT INTO team_members (team_id, user_id, role, added_at)"
            " VALUES (?, ?, 'viewer', ?)",
            (team_id, mate["id"], time.time()),
        )
        conn.execute(
            "INSERT INTO team_objects (team_id, kind, object_id, added_at)"
            " VALUES (?, 'analytics_site', ?, ?)",
            (team_id, SITE["id"], time.time()),
        )
    headers = {"authorization": f"Bearer {auth.api_key_for(mate['id'])}"}
    status, summary = dispatch(
        "GET",
        "/v1/analytics/summary",
        query=f"site_id={SITE['id']}",
        headers=headers,
    )
    assert status == 200
    assert summary["site_id"] == SITE["id"]


def test_unknown_write_key_is_discarded() -> None:
    assert collect([{"event": "page_view", "client_id": "unknown"}], "guessed.example") == 204
    assert rows() == []


def test_client_clock_skew_is_clamped() -> None:
    collect([{"event": "page_view", "client_id": "skew", "ts": 1_000_000}])
    stored = rows()
    assert abs(stored[0]["ts"] - time.time()) < 60


def test_site_crud_returns_write_key_and_snippet() -> None:
    status, created = dispatch(
        "POST",
        "/v1/analytics/sites",
        body=json.dumps({"domain": "example.test", "name": "Example"}),
    )
    assert status == 201
    assert created["write_key"].startswith("thw_")
    assert created["write_key"] in created["snippet"]
    assert "/static/th.js" in created["snippet"]

    status, listed = dispatch("GET", "/v1/analytics/sites")
    assert status == 200
    assert created["id"] in {site["id"] for site in listed["sites"]}

    status, updated = dispatch(
        "PATCH",
        f"/v1/analytics/sites/{created['id']}",
        body=json.dumps({"name": "Renamed"}),
    )
    assert status == 200
    assert updated["name"] == "Renamed"

    status, deleted = dispatch("DELETE", f"/v1/analytics/sites/{created['id']}")
    assert status == 200
    assert deleted["deleted"] is True


def test_first_event_dashboard_is_claimed_once_and_edits_survive() -> None:
    assert collect(
        [
            {
                "event": "purchase",
                "client_id": "buyer",
                "page_location": "https://netwrck.com/checkout",
                "referrer": "https://search.example/",
                "props": {"plan": "pro"},
            }
        ]
    ) == 204

    built: list[str | None] = []
    lock = threading.Lock()

    def attempt() -> None:
        result = dashboard_routes.build_analytics_event_dashboard(SITE["id"], "purchase")
        with lock:
            built.append(result)

    workers = [threading.Thread(target=attempt) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(20)
        assert not worker.is_alive()

    dashboard_ids = [dashboard_id for dashboard_id in built if dashboard_id]
    assert len(dashboard_ids) == 1
    dashboard_id = dashboard_ids[0]
    charts = store.rows_to_dicts(
        store.query("SELECT spec FROM charts WHERE dashboard_id = ?", (dashboard_id,))
    )
    assert len(charts) >= 5
    for chart in charts:
        assert chart_defaults.audit(json.loads(chart["spec"])) == []

    with store.transaction() as conn:
        conn.execute(
            "UPDATE dashboards SET title = ?, updated_at = ? WHERE id = ?",
            ("My purchase dashboard", time.time(), dashboard_id),
        )
    assert collect([{"event": "purchase", "client_id": "buyer"}]) == 204
    assert dashboard_routes.build_analytics_event_dashboard(SITE["id"], "purchase") is None
    row = store.one("SELECT title FROM dashboards WHERE id = ?", (dashboard_id,))
    assert row["title"] == "My purchase dashboard"
    assert (
        store.one(
            "SELECT COUNT(*) AS n FROM dashboards WHERE id = ?", (dashboard_id,)
        )["n"]
        == 1
    )


def test_self_tracking_is_off_without_config_and_uses_own_origin_when_enabled(
    monkeypatch: Any,
) -> None:
    monkeypatch.setattr(pages.config, "get", lambda name, default=None: None)
    assert '<script async src="/static/th.js"></script>' not in pages.app_shell(None).body

    monkeypatch.setattr(
        pages.config,
        "get",
        lambda name, default=None: "thw_self_test"
        if name == "TWOHELIXES_ANALYTICS_WRITE_KEY"
        else default,
    )
    body = pages.app_shell(None).body
    assert '"siteId":"thw_self_test"' in body
    assert '"endpoint":"/v1/collect"' in body
    assert '<script async src="/static/th.js"></script>' in body


def test_tracker_has_privacy_guards_and_omits_risky_fields() -> None:
    source = (pages.config.REPO_ROOT / "web" / "src" / "th.js").read_text()
    assert "globalPrivacyControl" in source
    assert "doNotTrack" in source
    assert "#signin-overlay[open]" in source
    assert "#checkout-overlay[open]" in source
    assert "page_title:" not in source
    assert "message: String(e.message)" not in source
