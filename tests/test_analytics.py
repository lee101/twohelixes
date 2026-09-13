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
import base64
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import pytest

os.environ.setdefault("TWOHELIXES_DATA_DIR", tempfile.mkdtemp(prefix="th-analytics-"))

from twohelixes import auth, router, store  # noqa: E402
from twohelixes.routes import analytics as analytics_routes  # noqa: E402
from twohelixes.routes import dashboards as dashboard_routes  # noqa: E402
from twohelixes.routes import teams  # noqa: E402
from twohelixes.routes import pages  # noqa: E402
from twohelixes.routes import query as query_routes  # noqa: E402
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


def test_large_batch_uses_bounded_multi_value_inserts() -> None:
    statements: list[str] = []
    raw = store.connection().raw
    raw.set_trace_callback(
        lambda sql: statements.append(sql)
        if sql.lstrip().upper().startswith("INSERT INTO ANALYTICS_EVENTS")
        else None
    )
    events = [
        {
            "event": "purchase",
            "client_id": "bulk-client",
            "session_id": "bulk-session",
            "event_id": f"bulk-{index}",
        }
        for index in range(400)
    ]
    try:
        assert collect(events) == 204
    finally:
        raw.set_trace_callback(None)
    assert len(rows()) == 400
    # 400 events plus one synthetic session_start, in 200-row chunks.
    assert len(statements) == 3


def test_helper_failure_rolls_back_instead_of_acknowledging_loss(
    monkeypatch: Any,
) -> None:
    def fail(_conn: Any, _row: dict[str, Any]) -> None:
        raise RuntimeError("forced helper failure")

    monkeypatch.setattr(analytics_routes, "_link_groups", fail)
    with pytest.raises(RuntimeError, match="forced helper failure"):
        collect(
            [
                {
                    "event": "custom_event",
                    "client_id": "rollback-client",
                    "session_id": "rollback-session",
                }
            ]
        )
    assert rows(include_session_start=True) == []


def test_malformed_events_are_dropped_not_fatal() -> None:
    status, _ = dispatch("POST", "/v1/collect", body="{not json")
    assert status == 204

    assert collect([{"event": "no_client"}, "a string", {"client_id": "c2"}]) == 204
    assert rows() == []

    # A good event alongside bad ones still lands.
    assert collect([{"bad": True}, {"event": "ok", "client_id": "c3"}]) == 204
    assert [r["event_name"] for r in rows()] == ["ok"]


def test_oversized_batches_are_rejected_explicitly() -> None:
    events = [
        {"event": "bulk", "client_id": f"bulk-{index}"}
        for index in range(analytics_routes.MAX_EVENTS_PER_BATCH + 1)
    ]
    status, body = dispatch(
        "POST",
        "/v1/collect",
        body=json.dumps({"site_id": SITE["write_key"], "events": events}),
    )
    assert status == 413
    assert body == {
        "error": "too_many_events",
        "limit": analytics_routes.MAX_EVENTS_PER_BATCH,
    }
    assert rows() == []


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


def test_segment_http_api_accepts_basic_auth_write_key() -> None:
    basic = base64.b64encode(f"{SITE['write_key']}:".encode()).decode()
    status, body = dispatch(
        "POST",
        "/v1/track",
        body=json.dumps(
            {
                "event": "Order Completed",
                "anonymousId": "segment-http",
                "properties": {"revenue": 42},
            }
        ),
        headers={"authorization": f"Basic {basic}"},
    )
    assert status == 200
    assert body == {"success": True}
    assert [row["event_name"] for row in rows()] == ["Order Completed"]


def test_segment_alias_uses_previous_id_for_identity_stitching() -> None:
    assert collect(
        [
            {
                "type": "alias",
                "previousId": "anonymous-before-signin",
                "userId": "known-after-signin",
                "messageId": "alias-1",
            }
        ]
    ) == 204
    identity = store.row_to_dict(
        store.one(
            "SELECT * FROM analytics_identities WHERE site_id = ? AND client_id = ?",
            (SITE["id"], "anonymous-before-signin"),
        )
    )
    assert identity is not None
    assert identity["user_id"] == "known-after-signin"


def test_segment_batch_context_and_clock_correction_are_preserved() -> None:
    sent = time.time() - 60
    original = sent - 300
    status, body = dispatch(
        "POST",
        "/v1/batch",
        body=json.dumps(
            {
                "writeKey": SITE["write_key"],
                "sentAt": datetime.fromtimestamp(sent, timezone.utc).isoformat(),
                "context": {
                    "page": {"url": "https://netwrck.com/from-batch"},
                    "library": {"name": "analytics-python", "version": "1"},
                    "ip": "203.0.113.8",
                },
                "batch": [
                    {
                        "type": "track",
                        "event": "batch_custom",
                        "anonymousId": "segment-clock",
                        "originalTimestamp": datetime.fromtimestamp(
                            original, timezone.utc
                        ).isoformat(),
                    }
                ],
            }
        ),
    )
    assert status == 200
    assert body == {"success": True}
    row = rows()[0]
    assert row["page_path"] == "/from-batch"
    assert row["ts"] == pytest.approx(time.time() - 300, abs=3)
    props = json.loads(row["props"])
    assert props["$context"]["library"]["name"] == "analytics-python"
    assert "ip" not in props["$context"]


def test_ga4_measurement_protocol_post_is_accepted() -> None:
    status, _ = dispatch(
        "POST",
        "/mp/collect",
        query=f"measurement_id={SITE['write_key']}&api_secret=unused",
        body=json.dumps(
            {
                "client_id": "ga4-client",
                "user_id": "ga4-user",
                "events": [
                    {
                        "name": "purchase",
                        "params": {"value": 12.5, "currency": "NZD", "ga_session_id": "ga4-session"},
                    }
                ],
            }
        ),
    )
    assert status == 204
    stored = rows()
    assert [row["event_name"] for row in stored] == ["purchase"]
    assert stored[0]["session_id"] == "ga4-session"
    assert json.loads(stored[0]["props"])["currency"] == "NZD"


def test_ga4_event_id_in_params_is_idempotent() -> None:
    payload = {
        "client_id": "ga4-dedupe",
        "events": [
            {"name": "workspace_exported", "params": {"event_id": "ga-event-1"}}
        ],
    }
    for _ in range(2):
        status, _ = dispatch(
            "POST",
            "/mp/collect",
            query=f"measurement_id={SITE['write_key']}",
            body=json.dumps(payload),
        )
        assert status == 204
    stored = rows()
    assert len(stored) == 1
    assert stored[0]["external_id"] == "ga-event-1"


def test_ga4_transaction_id_does_not_collide_across_event_types() -> None:
    payload = {
        "client_id": "ga4-transaction",
        "events": [
            {
                "name": "purchase",
                "params": {"event_id": "purchase-1", "transaction_id": "order-9"},
            },
            {
                "name": "refund",
                "params": {"event_id": "refund-1", "transaction_id": "order-9"},
            },
        ],
    }
    status, _ = dispatch(
        "POST",
        "/mp/collect",
        query=f"measurement_id={SITE['write_key']}",
        body=json.dumps(payload),
    )
    assert status == 204
    assert [row["event_name"] for row in rows()] == ["purchase", "refund"]


def test_mixpanel_track_is_lossless_and_idempotent() -> None:
    payload = [
        {
            "event": "workspace_exported",
            "properties": {
                "token": SITE["write_key"],
                "distinct_id": "mix-user",
                "$user_id": "user-42",
                "$device_id": "device-42",
                "$insert_id": "mix-1",
                "$current_url": "https://netwrck.com/workspaces/1",
                "format": "csv",
                "row_count": 1234,
            },
        }
    ]
    for _ in range(2):
        status, body = dispatch(
            "POST", "/track", query="verbose=1", body=json.dumps(payload)
        )
        assert status == 200
        assert body["status"] == 1
    stored = rows()
    assert len(stored) == 1
    assert stored[0]["source"] == "mixpanel"
    assert stored[0]["external_id"] == "mix-1"
    assert stored[0]["client_id"] == "device-42"
    assert stored[0]["user_id"] == "user-42"
    assert stored[0]["page_path"] == "/workspaces/1"
    props = json.loads(stored[0]["props"])
    assert props["format"] == "csv"
    assert "token" not in props


def test_mixpanel_engage_merges_profile_operations() -> None:
    first = [
        {
            "$token": SITE["write_key"],
            "$distinct_id": "profile-1",
            "$set": {"plan": "pro"},
            "$set_once": {"created_via": "cli"},
            "$add": {"login_count": 1},
        }
    ]
    second = [
        {
            "$token": SITE["write_key"],
            "$distinct_id": "profile-1",
            "$set": {"plan": "business"},
            "$set_once": {"created_via": "ignored"},
            "$add": {"login_count": 2},
        }
    ]
    assert dispatch("POST", "/engage", body=json.dumps(first))[0] == 200
    assert dispatch("POST", "/engage", body=json.dumps(second))[0] == 200
    identity = store.row_to_dict(
        store.one(
            "SELECT * FROM analytics_identities WHERE site_id = ? AND client_id = ?",
            (SITE["id"], "profile-1"),
        )
    )
    assert identity is not None
    traits = json.loads(identity["traits"])
    assert traits == {"plan": "business", "created_via": "cli", "login_count": 3.0}
    deleted = [{"$token": SITE["write_key"], "$distinct_id": "profile-1", "$delete": ""}]
    assert dispatch("POST", "/engage", body=json.dumps(deleted))[0] == 200
    assert store.one(
        "SELECT 1 FROM analytics_identities WHERE site_id = ? AND client_id = ?",
        (SITE["id"], "profile-1"),
    ) is None


def test_mixpanel_alias_stitches_the_original_identity() -> None:
    payload = [
        {
            "event": "$create_alias",
            "properties": {
                "token": SITE["write_key"],
                "distinct_id": "mix-anonymous",
                "alias": "mix-known",
                "$insert_id": "mix-alias-1",
            },
        }
    ]
    assert dispatch("POST", "/track", body=json.dumps(payload))[0] == 200
    identity = store.row_to_dict(
        store.one(
            "SELECT * FROM analytics_identities WHERE site_id = ? AND client_id = ?",
            (SITE["id"], "mix-anonymous"),
        )
    )
    assert identity is not None
    assert identity["user_id"] == "mix-known"


def test_segment_and_mixpanel_groups_share_typed_group_profiles() -> None:
    assert collect(
        [
            {
                "type": "group",
                "userId": "group-user",
                "groupId": "acct-7",
                "groupType": "account_id",
                "traits": {"name": "Acme"},
            }
        ]
    ) == 204
    status, body = dispatch(
        "POST",
        "/groups",
        query="verbose=1",
        body=json.dumps(
            [
                {
                    "$token": SITE["write_key"],
                    "$group_key": "account_id",
                    "$group_id": "acct-7",
                    "$set": {"plan": "business"},
                    "$add": {"seats": 2},
                }
            ]
        ),
    )
    assert status == 200
    assert body["status"] == 1
    status, report = dispatch(
        "GET", "/v1/analytics/groups", query=f"site_id={SITE['id']}&type=account_id"
    )
    assert status == 200
    assert len(report["groups"]) == 1
    group = report["groups"][0]
    assert group["group_id"] == "acct-7"
    assert group["traits"] == {"name": "Acme", "plan": "business", "seats": 2.0}
    assert group["observed_events"] == 2


def test_amplitude_http_v2_accepts_custom_events_and_profiles() -> None:
    payload = {
        "api_key": SITE["write_key"],
        "events": [
            {
                "device_id": "amp-device",
                "user_id": "amp-user",
                "event_type": "workspace_exported",
                "time": int(time.time() * 1000),
                "session_id": 1788062000000,
                "insert_id": "amp-1",
                "event_properties": {"format": "parquet", "row_count": 500},
                "user_properties": {"plan": "pro"},
                "groups": {"account_id": "acct-7"},
            }
        ],
    }
    status, body = dispatch("POST", "/2/httpapi", body=json.dumps(payload))
    assert status == 200
    assert body["code"] == 200
    assert body["events_ingested"] == 1
    stored = rows()
    assert len(stored) == 1
    assert stored[0]["source"] == "amplitude"
    assert stored[0]["external_id"] == "amp-1"
    props = json.loads(stored[0]["props"])
    assert props["format"] == "parquet"
    assert props["$groups"] == {"account_id": "acct-7"}
    identity = store.row_to_dict(
        store.one(
            "SELECT * FROM analytics_identities WHERE site_id = ? AND client_id = ?",
            (SITE["id"], "amp-device"),
        )
    )
    assert identity is not None
    assert json.loads(identity["traits"])["plan"] == "pro"
    status, groups = dispatch(
        "GET", "/v1/analytics/groups", query=f"site_id={SITE['id']}&type=account_id"
    )
    assert status == 200
    assert groups["groups"][0]["group_id"] == "acct-7"


def test_amplitude_identify_form_api_updates_a_profile() -> None:
    body = urlencode(
        {
            "api_key": SITE["write_key"],
            "identification": json.dumps(
                [
                    {
                        "device_id": "amp-identify-device",
                        "user_id": "amp-identify-user",
                        "user_properties": {
                            "$set": {"plan": "enterprise"},
                            "$add": {"login_count": 1},
                        },
                    }
                ]
            ),
        }
    )
    status, response = dispatch(
        "POST",
        "/identify",
        body=body,
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert status == 200
    assert response["events_ingested"] == 1
    identity = store.row_to_dict(
        store.one(
            "SELECT * FROM analytics_identities WHERE site_id = ? AND client_id = ?",
            (SITE["id"], "amp-identify-device"),
        )
    )
    assert identity is not None
    assert json.loads(identity["traits"]) == {"plan": "enterprise", "login_count": 1.0}


def test_oversized_properties_remain_valid_json() -> None:
    assert collect(
        [
            {
                "event": "large_custom_event",
                "client_id": "large-props",
                "props": {"keep": "small", "huge": "🚀" * 9000},
            }
        ]
    ) == 204
    props = json.loads(rows()[0]["props"])
    assert props["_twohelixes_truncated"] is True
    assert props["keep"] == "small"
    assert "huge" not in props


def test_sampled_events_carry_inverse_weights() -> None:
    events = [
        {
            "event": "pointer_moved",
            "client_id": f"sample-{index}",
            "session_id": f"session-{index}",
            "sample_rate": 0.5,
        }
        for index in range(50)
    ]
    assert collect(events) == 204
    stored = rows()
    # These are the rows already retained upstream. The server must not draw
    # against the upstream probability a second time.
    assert len(stored) == len(events)
    assert {row["sample_rate"] for row in stored} == {0.5}
    assert {row["sample_weight"] for row in stored} == {2.0}


def test_upstream_and_server_sampling_probabilities_compose(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        analytics_routes,
        "_server_sample_rate",
        lambda _site, _now, _ceiling=None: 0.5,
    )
    events = [
        {
            "event": "pointer_moved",
            "client_id": f"composed-{index}",
            "session_id": f"composed-session-{index}",
            "sample_rate": 0.1,
        }
        for index in range(100)
    ]
    assert collect(events) == 204
    stored = rows()
    assert 20 < len(stored) < 80
    assert all(row["sample_rate"] == pytest.approx(0.05) for row in stored)
    assert all(row["sample_weight"] == pytest.approx(20.0) for row in stored)


def test_server_sampling_starts_only_after_the_rate_ceiling(monkeypatch: Any) -> None:
    monkeypatch.setenv("TWOHELIXES_ANALYTICS_SAMPLE_AFTER_RPS", "10")
    monkeypatch.setenv("TWOHELIXES_WORKERS", "1")
    analytics_routes._sample_windows.clear()
    rates = [analytics_routes._server_sample_rate(SITE["id"], 1000.1) for _ in range(30)]
    assert rates[:10] == [1.0] * 10
    assert rates[-1] == pytest.approx(1 / 3)

    keep, rate = analytics_routes._keep_sample(
        {"event": "purchase", "client_id": "important"}, SITE["id"], 1000.1
    )
    assert keep is True
    assert rate == 1.0
    for event_name in (
        "begin_checkout",
        "subscription_dialog_opened",
        "subscription_auth_required",
        "subscription_auth_selected",
        "subscription_checkout_ready",
        "subscription_checkout_failed",
        "subscription_checkout_completed",
        "subscription_dialog_closed",
    ):
        keep, rate = analytics_routes._keep_sample(
            {"event": event_name, "client_id": "important"}, SITE["id"], 1000.1
        )
        assert keep is True
        assert rate == 1.0


def test_chart_agent_can_load_an_owned_analytics_site() -> None:
    collect(
        [
            {
                "event": "sign_in_completed",
                "client_id": "agent-client",
                "session_id": "agent-session",
                "page_location": "https://netwrck.com/login",
                "props": {"surface": "header"},
            }
        ]
    )
    owner = store.get_user_by_email("analytics-owner@test.local")
    assert owner is not None
    identity = auth.Identity(user_id=owner["id"], email=owner["email"])
    ctx = router.Context(
        method="POST",
        path="/v1/query",
        raw_query="",
        raw_body=json.dumps(
            {"analytics_site_id": SITE["id"], "days": 1, "limit": 100}
        ),
        headers={},
        user=identity,
    )
    frames = query_routes._load_frames(identity, ctx)
    frame = frames["analytics_events"]
    assert "prop_surface" in frame.columns
    assert "sign_in_completed" in set(frame["event_name"])


def test_ga_style_pixel_returns_a_gif_and_records() -> None:
    status, body = dispatch(
        "GET",
        "/v1/collect",
        query=f"tid={SITE['write_key']}&cid=pixel1&en=page_view&dl=https%3A%2F%2Fnetwrck.com%2F&dt=Home",
    )
    assert status == 200
    assert body.startswith("GIF89a")
    assert body.encode("latin-1") == analytics_routes.PIXEL
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


def test_funnel_counts_ordered_session_progression() -> None:
    now = time.time() * 1000 - 10
    collect(
        [
            {"event": "page_view", "client_id": "a", "session_id": "s1", "ts": now},
            {"event": "signup_started", "client_id": "a", "session_id": "s1", "ts": now + 1},
            {"event": "sign_in_completed", "client_id": "a", "session_id": "s1", "ts": now + 2},
            {"event": "page_view", "client_id": "b", "session_id": "s2", "ts": now},
            {"event": "signup_started", "client_id": "b", "session_id": "s2", "ts": now + 1},
            {"event": "page_view", "client_id": "c", "session_id": "s3", "ts": now},
            {"event": "sign_in_completed", "client_id": "c", "session_id": "s3", "ts": now + 1},
        ]
    )

    status, report = dispatch(
        "GET",
        "/v1/analytics/funnel",
        query=f"site_id={SITE['id']}&days=1&steps=page_view,signup_started,sign_in_completed",
    )
    assert status == 200
    assert [stage["sessions"] for stage in report["steps"]] == [3, 2, 1]
    assert [stage["users"] for stage in report["steps"]] == [3, 2, 1]
    assert report["steps"][1]["step_rate"] == pytest.approx(2 / 3)
    assert report["steps"][2]["dropoff"] == 1


def test_same_timestamp_funnel_keeps_batch_order() -> None:
    now = time.time() * 1000
    collect(
        [
            {"event": "step_z", "client_id": "ordered", "session_id": "ordered", "ts": now},
            {"event": "step_a", "client_id": "ordered", "session_id": "ordered", "ts": now},
            {"event": "step_m", "client_id": "ordered", "session_id": "ordered", "ts": now},
        ]
    )
    status, report = dispatch(
        "GET",
        "/v1/analytics/funnel",
        query=f"site_id={SITE['id']}&days=1&steps=step_z,step_a,step_m",
    )
    assert status == 200
    assert [step["sessions"] for step in report["steps"]] == [1, 1, 1]


def test_paths_discovers_session_flows_without_predefined_steps() -> None:
    now = time.time() * 1000 - 10
    collect(
        [
            {"event": "page_view", "client_id": "a", "session_id": "s1", "ts": now,
             "page_location": "https://netwrck.com/"},
            {"event": "signup_started", "client_id": "a", "session_id": "s1", "ts": now + 1},
            {"event": "sign_in_completed", "client_id": "a", "session_id": "s1", "ts": now + 2},
            {"event": "page_view", "client_id": "b", "session_id": "s2", "ts": now,
             "page_location": "https://netwrck.com/"},
            {"event": "signup_started", "client_id": "b", "session_id": "s2", "ts": now + 1},
        ]
    )
    status, report = dispatch(
        "GET",
        "/v1/analytics/paths",
        query=f"site_id={SITE['id']}&days=1&mode=events&depth=3",
    )
    assert status == 200
    assert report["sessions"] == 2
    paths = {tuple(path["path"]): path for path in report["paths"]}
    assert paths[("page_view", "signup_started")]["sessions"] == 1
    assert paths[("page_view", "signup_started", "sign_in_completed")]["sessions"] == 1


def test_revenue_is_net_and_never_sums_mixed_currencies() -> None:
    collect(
        [
            {"event": "purchase", "client_id": "buyer-1",
             "props": {"value": 49, "currency": "NZD"}},
            {"event": "refund", "client_id": "buyer-1",
             "props": {"value": 10, "currency": "NZD"}},
            {"event": "Order Completed", "client_id": "buyer-2",
             "props": {"revenue": 20, "currency": "USD"}},
        ]
    )
    status, report = dispatch(
        "GET", "/v1/analytics/revenue", query=f"site_id={SITE['id']}&days=1"
    )
    assert status == 200
    totals = {row["currency"]: row["revenue"] for row in report["currencies"]}
    assert totals == {"NZD": 39.0, "USD": 20.0}


def test_funnel_uses_stitched_user_ids_when_available() -> None:
    now = time.time() * 1000 - 10
    collect(
        [
            {"event": "page_view", "client_id": "anon-a", "session_id": "s1", "ts": now},
            {"event": "sign_in_completed", "client_id": "anon-a", "session_id": "s1",
             "user_id": "user-1", "ts": now + 1},
            {"event": "page_view", "client_id": "anon-b", "session_id": "s2", "ts": now},
            {"event": "sign_in_completed", "client_id": "anon-b", "session_id": "s2",
             "user_id": "user-1", "ts": now + 1},
        ]
    )

    status, report = dispatch(
        "GET",
        "/v1/analytics/funnel",
        query=f"site_id={SITE['id']}&days=1&steps=page_view,sign_in_completed",
    )
    assert status == 200
    assert report["steps"][0]["users"] == 2
    assert report["steps"][1]["users"] == 1


def test_summary_requires_a_site() -> None:
    status, body = dispatch("GET", "/v1/analytics/summary")
    assert status == 400
    assert body["error"] == "site_id_required"


def test_schema_discovers_custom_events_properties_and_sources() -> None:
    collect(
        [
            {
                "event": "workspace_exported",
                "client_id": "schema-1",
                "props": {"format": "csv", "row_count": 12, "compressed": True},
            },
            {
                "event": "workspace_exported",
                "client_id": "schema-2",
                "props": {"format": "parquet", "row_count": 20},
            },
        ]
    )
    status, body = dispatch(
        "GET", "/v1/analytics/schema", query=f"site_id={SITE['id']}&days=1"
    )
    assert status == 200
    exported = next(event for event in body["events"] if event["name"] == "workspace_exported")
    assert exported["observed_events"] == 2
    assert exported["users"] == 2
    assert exported["sources"] == [{"name": "native", "observed_events": 2}]
    properties = {prop["name"]: prop for prop in exported["properties"]}
    assert properties["format"]["types"] == ["string"]
    assert properties["row_count"]["types"] == ["number"]
    assert properties["compressed"]["types"] == ["boolean"]


def test_reporting_resolves_historical_anonymous_events_to_a_person() -> None:
    collect(
        [
            {"event": "page_view", "client_id": "anon-canonical"},
            {
                "type": "identify",
                "anonymousId": "anon-canonical",
                "userId": "known-user",
                "traits": {"plan": "pro"},
            },
        ]
    )
    collect(
        [
            {"event": "page_view", "client_id": "second-device"},
            {
                "type": "identify",
                "anonymousId": "second-device",
                "userId": "known-user",
            },
        ]
    )
    status, body = dispatch(
        "GET", "/v1/analytics/summary", query=f"site_id={SITE['id']}&days=1"
    )
    assert status == 200
    assert body["totals"]["users"] == 1


def test_stranger_is_forbidden_from_every_read_endpoint() -> None:
    stranger = store.create_user("analytics-stranger@test.local")
    headers = {"authorization": f"Bearer {auth.api_key_for(stranger['id'])}"}
    for endpoint in (
        "summary", "events", "schema", "groups", "paths", "revenue",
        "funnel", "timeseries", "export",
    ):
        status, body = dispatch(
            "GET",
            f"/v1/analytics/{endpoint}",
            query=f"site_id={SITE['id']}" + ("&steps=page_view,sign_up" if endpoint == "funnel" else ""),
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


def test_unique_custom_event_names_do_not_amplify_dashboard_work() -> None:
    assert collect(
        [
            {
                "event": f"untrusted_custom_{index}",
                "client_id": "schema-spray",
                "session_id": "schema-spray-session",
            }
            for index in range(100)
        ]
    ) == 204
    claims = store.one(
        "SELECT COUNT(*) AS n FROM analytics_event_dashboards WHERE site_id = ?",
        (SITE["id"],),
    )["n"]
    assert claims == 1  # only the protected synthetic session_start

    analytics_routes._sample_windows.clear()
    assert collect(
        [
            {
                "event": "established_custom_event",
                "client_id": "schema-repeat",
                "session_id": "schema-repeat-session",
            }
            for _ in range(10)
        ]
    ) == 204
    assert store.one(
        "SELECT status FROM analytics_event_dashboards"
        " WHERE site_id = ? AND event_name = ?",
        (SITE["id"], "established_custom_event"),
    )["status"] == "pending"


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
    assert "sampleAfterPerSecond" in source
    assert "sample_rate:" in source
    assert "purchase: true" in source
    assert "begin_checkout: true" in source
    assert "subscription_dialog_opened: true" in source
