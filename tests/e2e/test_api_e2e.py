"""End-to-end: the HTTP surface, against the built server.

What these hold that the unit tests cannot: the Mojo loop really parses and
routes the request, the bridge really carries the body across, the SSE frames
really arrive in order, and two workers really share one database. Anything
asserted here is asserted about the product rather than about a Python
function that happens to be in it.

The model gateway is not assumed to be reachable. Everything here either
avoids it or accepts a graceful degradation, because a benchmark that fails
when an upstream is down teaches nobody anything.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

@pytest.fixture(autouse=True)
def _quiet_box(quiet: None) -> None:
    """Every test starts on a settled box.

    A deep-research thread from an earlier test holds a sandbox and writes to
    the database continuously; the next test then competes with it for a write
    lock and fails in a way that has nothing to do with what it is testing.
    """


def _settle(server: Any, job_id: str, timeout: float = 60.0) -> str:
    """Wait for a cancelled job to actually stop.

    Cancellation is read between steps, and a step can be a model call plus
    ninety seconds of sandbox. Leaving the thread running makes every later
    test share a box with it, which is how an e2e suite becomes "flaky".
    """
    import time as _time

    deadline = _time.time() + timeout
    status = "running"
    while _time.time() < deadline:
        status = str(server.get(f"/v1/jobs/{job_id}")[1].get("status") or "")
        if status != "running":
            return status
        _time.sleep(1.0)
    return status


ROWS = [
    {"order_date": "2024-01-15", "region": "North", "revenue": 1200},
    {"order_date": "2024-02-15", "region": "North", "revenue": 1500},
    {"order_date": "2024-03-15", "region": "North", "revenue": 1800},
    {"order_date": "2024-01-15", "region": "South", "revenue": 900},
    {"order_date": "2024-02-15", "region": "South", "revenue": 850},
    {"order_date": "2024-03-15", "region": "South", "revenue": 700},
]


# -- the loop itself --------------------------------------------------------


def test_health_never_crosses_the_bridge(server: Any):
    """`/healthz` is answered inside the event loop, by design."""
    assert server.get("/healthz")[0] == 200
    assert server.get("/readyz")[0] == 200


def test_the_marketing_pages_render(server: Any):
    status, body = server.request("GET", "/", raw=True)
    assert status == 200
    assert b"twoHelixes" in body
    # The showcase charts are built by the real pipeline at boot; an empty
    # homepage means chart construction broke inside the worker.
    assert body.count(b"<svg") >= 3


def test_an_unknown_route_is_a_json_404(server: Any):
    status, body = server.get("/v1/nope")
    assert status == 404
    assert isinstance(body, dict)


# -- the bot gate -----------------------------------------------------------


def test_anonymous_callers_cannot_point_the_pipeline_at_their_own_data(server: Any):
    """The trial is a demonstration, not a free interpreter."""
    fresh = type(server)(server.base, server.data_dir)
    status, body = fresh.post("/v1/query", {"q": "revenue by region", "data": ROWS})
    assert status == 401, body
    assert body["error"] == "signin_required"


def test_the_trial_runs_once_on_sample_data_and_then_asks_for_an_email(server: Any):
    """The funnel: show it working before asking for anything.

    Refusing anonymous callers outright is a sound bot gate and a bad funnel -
    it asks for an account before the product has done anything at all.
    """
    fresh = type(server)(server.base, server.data_dir)
    status, body = fresh.post("/v1/query", {"q": "revenue by region", "sample": "orders"})
    assert status == 200, body
    assert body["figure"]["data"]
    # Nothing to own yet, so nothing is saved under an account.
    assert body["chart_id"] == ""

    status, body = fresh.post("/v1/query", {"q": "revenue by channel", "sample": "orders"})
    assert status == 401, body
    assert body["error"] == "trial_used"
    assert body["upgrade"] == "signin"


# -- a chart, end to end ----------------------------------------------------


def test_a_question_comes_back_as_a_chart(server: Any, account: dict):
    status, body = server.post("/v1/query", {"q": "revenue by region", "data": ROWS})
    assert status == 200, body
    # The product's central promise. It holds with the gateway down, because
    # the heuristic path is a real path.
    assert body["figure"]["data"], body.get("warnings")
    assert body["config"]["chart_type"]
    # The shaped result, not the input: six rows over two regions come back
    # aggregated, which is the pipeline doing its job.
    assert 1 <= body["row_count"] <= len(ROWS)
    assert body["columns"]


def test_the_chart_exports_as_svg_and_as_a_notebook(server: Any, account: dict):
    status, body = server.post("/v1/query", {"q": "revenue by region", "data": ROWS})
    assert status == 200, body

    status, svg = server.post(
        "/v1/export", {"figure": body["figure"], "format": "svg", "mode": "dark"}, raw=True
    )
    assert status == 200
    assert b"<svg" in svg

    status, notebook = server.post(
        "/v1/notebook/export?format=ipynb",
        {
            "title": "e2e",
            "question": "revenue by region",
            "config": body["config"],
            "transform_code": body.get("transform_code") or "",
            "rows": ROWS,
        },
        raw=True,
    )
    assert status == 200
    document = json.loads(notebook)
    assert document["nbformat"] == 4
    assert any(cell["cell_type"] == "code" for cell in document["cells"])


def test_a_chart_from_inline_data_can_still_be_reconfigured(server, account: dict):
    """The free, no-model edit path, for data with no source to re-query.

    Uploads and inline payloads are the first-run path, and reconfiguring one
    used to fail with "re-run the query" because the rows were gone: the chart
    kept a figure and a config but not the data behind them.
    """
    status, body = server.post("/v1/query", {"q": "revenue by region", "data": ROWS})
    assert status == 200, body
    chart_id = body["chart_id"]
    assert chart_id, "the API cannot address a chart it was not given the id of"

    before = server.get("/v1/billing/balance")[1]["credits"]
    status, updated = server.post(f"/v1/chart/{chart_id}/config", {"chart_type": "bar"})
    assert status == 200, updated
    assert updated["config"]["chart_type"] == "bar"
    assert {t["type"] for t in updated["figure"]["data"]} == {"bar"}
    # No model call, so no charge.
    assert server.get("/v1/billing/balance")[1]["credits"] == before


def test_the_api_key_authenticates_without_a_cookie(server: Any, account: dict):
    bare = type(server)(server.base, server.data_dir)
    bare.api_key = account["api_key"]
    status, body = bare.post(
        "/v1/query", {"q": "revenue by region", "data": ROWS}, use_api_key=True
    )
    assert status == 200, body
    assert body["figure"]["data"]


# -- streaming --------------------------------------------------------------


def test_the_stream_arrives_as_frames_not_as_one_lump(server: Any, account: dict):
    """The product's main feature is watching it work.

    Asserted on the wire because the ways this breaks - proxy buffering, a
    handler that builds the whole body first - are invisible from inside
    Python.
    """
    import urllib.request

    request = urllib.request.Request(
        f"{server.base}/v1/query/stream",
        data=json.dumps({"q": "revenue by region", "data": ROWS}).encode(),
        headers={"Content-Type": "application/json", "Cookie": server.cookie},
        method="POST",
    )
    events: list[str] = []
    with urllib.request.urlopen(request, timeout=180) as response:
        assert response.headers.get("Content-Type", "").startswith("text/event-stream")
        for line in response:
            text = line.decode(errors="replace").strip()
            if text.startswith("event:"):
                events.append(text.split(":", 1)[1].strip())
            if events and events[-1] in ("done", "error"):
                break

    assert "stage" in events or "result" in events, events
    assert events[-1] in ("done", "result", "error"), events


# -- billing ----------------------------------------------------------------


def test_usage_reports_the_plan_the_prices_and_the_balance(server: Any, account: dict):
    status, body = server.get("/v1/usage")
    assert status == 200, body
    assert body["rates"]["notebook_minute"] >= 1
    assert body["rates"]["long_agent_minute"] >= 1
    assert body["credits"] > 0
    assert body["open"] == []
    # The three things a caller needs before spending anything: what is
    # included, what it costs beyond that, and when it renews.
    assert body["included"]["chat_query"]["limit"] > 0
    assert body["volume"]["next_tier_at_credits"] > 0
    assert body["renews_in_days"] > 0


def test_included_charts_are_spent_before_credits(server: Any, account: dict):
    """The plan is what the customer bought; it goes first."""
    before = server.get("/v1/billing/balance")[1]
    status, _ = server.post("/v1/query", {"q": "revenue by region", "data": ROWS})
    assert status == 200
    after = server.get("/v1/billing/balance")[1]

    assert after["credits"] == before["credits"], "credits moved while allowance remained"
    assert after["included"]["chat_query"]["used"] == (
        before["included"]["chat_query"]["used"] + 1
    )


def test_a_query_is_charged_once_and_shows_up_in_the_ledger(server: Any, account: dict):
    server.exhaust_allowance(account["email"])
    before = server.get("/v1/billing/balance")[1]["credits"]
    status, _ = server.post("/v1/query", {"q": "revenue by region", "data": ROWS})
    assert status == 200
    after = server.get("/v1/billing/balance")[1]

    spent = before - after["credits"]
    assert spent > 0, "a paid query that costs nothing is a billing bug"
    assert after["ledger"][0]["delta"] == -spent


def test_an_agent_run_is_refused_without_the_credits_to_finish_it(server: Any, account: dict):
    """The base fee is not the whole cost, so the base fee is not the check.

    A run is a base fee plus metered minutes; letting one start on exactly the
    base fee means it dies of poverty at step two having spent the lot.
    """
    prices = server.get("/v1/usage")[1]["rates"]
    barely = int(prices["deep_research"]) + int(prices["long_agent_minute"])

    conn = server.db()
    conn.execute(
        "UPDATE users SET api_credits = ? WHERE email = ?", (barely, account["email"])
    )
    conn.commit()
    conn.close()

    status, body = server.post("/v1/agent", {"goal": "find something", "data": ROWS})
    assert status == 402, body
    assert body["error"] == "insufficient_credits"
    # Nothing was charged for a run that never started.
    assert server.get("/v1/billing/balance")[1]["credits"] == barely


def test_a_plan_that_includes_deep_research_does_not_charge_for_it(
    server: Any, account: dict
):
    conn = server.db()
    conn.execute(
        "UPDATE users SET plan = 'pro', plan_usage = '{}' WHERE email = ?",
        (account["email"],),
    )
    conn.commit()
    conn.close()

    before = server.get("/v1/billing/balance")[1]["credits"]
    status, body = server.post("/v1/agent", {"goal": "anything", "data": ROWS})
    assert status == 202, body
    assert body["included_in_plan"] is True
    assert body["base_credits"] == 0
    assert body["credits_per_minute"] == 0
    assert server.get("/v1/billing/balance")[1]["credits"] == before

    # The account is per-test, so there is nothing to put back - and writing to
    # the database while a research thread is mid-run only invites a lock.
    server.post(f"/v1/agent/cancel/{body['job_id']}")
    _settle(server, body["job_id"])


def test_an_agent_job_opens_a_meter_and_cancelling_closes_it(server: Any, account: dict):
    status, body = server.post("/v1/agent", {"goal": "why is south shrinking?", "data": ROWS})
    assert status == 202, body
    job_id = body["job_id"]
    assert body["credits_per_minute"] > 0

    conn = server.db()
    meter = conn.execute(
        "SELECT * FROM meters WHERE kind = 'long_agent' AND ref = ?", (job_id,)
    ).fetchone()
    assert meter is not None, "a long-running job with no meter is unbilled compute"
    assert meter["status"] == "open"
    conn.close()

    status, body = server.post(f"/v1/agent/cancel/{job_id}")
    assert status == 200, body
    _settle(server, job_id)

    conn = server.db()
    meter = conn.execute(
        "SELECT * FROM meters WHERE kind = 'long_agent' AND ref = ?", (job_id,)
    ).fetchone()
    # Closed, so the clock has stopped: nobody pays for minutes after they
    # asked it to stop.
    assert meter["status"] != "open"
    conn.close()


def test_a_job_is_visible_from_any_worker(server: Any, account: dict):
    """Two workers, one database.

    A job started on whichever worker took the POST has to be readable from
    whichever worker takes the GET - the failure this replaces reported "not
    found" roughly half the time, which is the worst kind of flake.
    """
    status, body = server.post("/v1/agent", {"goal": "anything", "data": ROWS})
    assert status == 202, body
    job_id = body["job_id"]

    try:
        for _ in range(8):
            status, job = server.get(f"/v1/jobs/{job_id}")
            assert status == 200, job
            assert job["id"] == job_id
            assert "billing" in job
    finally:
        # Leaving a deep-research run going would leave the next test racing a
        # thread doing model calls and sandbox work.
        server.post(f"/v1/agent/cancel/{job_id}")
        _settle(server, job_id)


def test_the_free_tier_is_shared_across_workers(server: Any):
    """An in-process counter would give a two-worker box twice the free tier."""
    fresh = type(server)(server.base, server.data_dir)
    email = f"free-{id(fresh)}@twohelixes.test"
    fresh.sign_in(email)

    allowed = 0
    for _ in range(8):
        status, _ = fresh.post("/v1/query", {"q": "revenue by region", "data": ROWS})
        if status == 200:
            allowed += 1
        elif status in (402, 429):
            break

    conn = fresh.db()
    usage = conn.execute(
        "SELECT plan_usage FROM users WHERE email = ?", (email,)
    ).fetchone()[0]
    conn.close()
    used = json.loads(usage or "{}").get("chat_query", 0)
    assert used == allowed, "the allowance counter and the answers disagree"


@pytest.mark.parametrize("path", ["/v1/usage", "/v1/jobs", "/v1/billing/balance"])
def test_the_account_endpoints_need_an_account(server: Any, path: str):
    anonymous = type(server)(server.base, server.data_dir)
    status, _ = anonymous.get(path)
    assert status == 401
