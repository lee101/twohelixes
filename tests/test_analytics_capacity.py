import json
import queue
import time

import pytest

from test_analytics import SITE, collect, dispatch, fresh_db, rows
from twohelixes import router, store
from twohelixes.routes import analytics

schedule_dashboards = analytics._schedule_dashboards


@pytest.mark.parametrize("path", ["/v1/collect", "/v1/batch", "/v1/track", "/v1/page",
    "/v1/screen", "/v1/identify", "/v1/group", "/v1/alias", "/mp/collect",
    "/track", "/import", "/engage", "/groups", "/2/httpapi", "/batch", "/identify"])
def test_ingest_body_rejected_before_context_or_json_parsing(monkeypatch, path):
    monkeypatch.setenv("TWOHELIXES_ANALYTICS_MAX_BODY_BYTES", "1024")
    monkeypatch.setattr(router, "build_context", lambda *args: pytest.fail("oversized body reached context parsing"))
    status, _ctype, headers, payload = router.handle("POST", path, "", "x" * 1025, "{}")
    assert int(status) == 413
    assert json.loads(payload)["error"] == "analytics_body_too_large"
    assert json.loads(headers)["Access-Control-Allow-Origin"] == "*"
    assert rows() == []


def test_body_budget_counts_utf8_bytes(monkeypatch):
    monkeypatch.setenv("TWOHELIXES_ANALYTICS_MAX_BODY_BYTES", "1024")
    status, _ = dispatch("POST", "/v1/collect", body="\U0001f680" * 300)
    assert status == 413


def test_supported_maximum_batch_is_not_rejected():
    events = [{"event": "purchase", "client_id": f"client-{i}", "session_id": f"session-{i}"}
              for i in range(analytics.MAX_EVENTS_PER_BATCH)]
    assert collect(events) == 204
    assert len([row for row in rows() if row["event_name"] == "purchase"]) == 2000


def test_full_dashboard_backlog_rejects_without_committing_events(monkeypatch):
    monkeypatch.setenv("TWOHELIXES_ANALYTICS_MAX_PENDING_DASHBOARDS", "1")
    with store.transaction() as conn:
        conn.execute("INSERT INTO analytics_event_dashboards (site_id,event_name,status,claimed_at) VALUES (?,?,?,?)",
                     (SITE["id"], "queued", "pending", time.time()))
    body = json.dumps({"site_id": SITE["id"], "events": [{"event": "purchase", "client_id": "buyer"}]})
    status, _ctype, headers, payload = router.handle("POST", "/v1/collect", "", body,
                                                 json.dumps({"user-agent": "Mozilla/5.0 Chrome/120"}))
    assert int(status) == 503
    assert json.loads(payload)["error"] == "analytics_backlog_full"
    assert json.loads(headers)["Retry-After"] == "5"
    assert json.loads(headers)["Access-Control-Expose-Headers"] == "Retry-After"
    assert rows() == []
    store.execute("UPDATE analytics_event_dashboards SET status = 'done'")
    assert collect([{"event": "purchase", "client_id": "buyer"}]) == 204


def test_queue_overflow_is_bounded_and_pending_jobs_remain_recoverable(monkeypatch):
    bounded = queue.Queue(maxsize=1)
    monkeypatch.setattr(analytics, "_dashboard_queue", bounded)
    monkeypatch.setattr(analytics, "_dashboard_worker_started", True)
    with store.transaction() as conn:
        for name in ("first", "second"):
            conn.execute("INSERT INTO analytics_event_dashboards (site_id,event_name,status,claimed_at) VALUES (?,?,?,?)",
                         (SITE["id"], name, "pending", time.time()))
    schedule_dashboards([(SITE["id"], "first"), (SITE["id"], "second")])
    assert bounded.qsize() == 1
    assert bounded.get_nowait() == (SITE["id"], "first")
    store.execute("UPDATE analytics_event_dashboards SET status = 'done' WHERE event_name = 'first'")
    assert analytics._next_pending_dashboard() == (SITE["id"], "second")


def test_native_tracker_replays_are_idempotent():
    events = [{"event": "purchase", "messageId": "stable-browser-event", "client_id": "browser"}]
    assert collect(events) == 204
    original_rows = rows()
    assert collect(events) == 204
    assert rows() == original_rows
