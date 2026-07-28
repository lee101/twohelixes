"""Per-minute billing.

These are the tests that decide whether a customer is charged correctly for
work that outlives a request, so they are written against the behaviours a
customer would dispute: charged twice for one minute, charged after they
stopped, charged for a run that produced nothing, or - the one that costs us
rather than them - not charged at all because a worker restarted.
"""

from __future__ import annotations

import json
import time

import pytest

from twohelixes import config, credits, metering, store


def _make_user(plan: str, balance: int = 1000) -> str:
    store.init()
    user_id = store.new_id()
    store.execute(
        "INSERT INTO users (id, email, plan, api_credits, free_queries_used, created_at, "
        "plan_period_start, plan_usage) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, f"{user_id}@example.com", plan, balance, 0, time.time(),
            time.time(), store.dump_json({}),
        ),
    )
    return user_id


@pytest.fixture
def user() -> str:
    """A caller with credits and no included minutes.

    Deliberately a plan with nothing included: these tests are about the
    metering arithmetic, and a plan that covers the minutes would make every
    charge assertion read zero.
    """
    return _make_user("free")


def _age(meter_id: str, seconds: float) -> None:
    """Pretend the meter has been running for `seconds` longer than it has.

    Age by a value that is *not* a whole number of minutes when the test then
    closes the meter. Closing rounds the final partial minute up, so ageing by
    exactly 240 charges four minutes or five depending on whether the
    statements in between took under a millisecond - which they did on SQLite
    and do not on Postgres. That is the product behaving correctly and the
    test measuring the driver.
    """
    row = store.one("SELECT paid_through, started_at FROM meters WHERE id = ?", (meter_id,))
    store.execute(
        "UPDATE meters SET paid_through = ?, started_at = ? WHERE id = ?",
        (float(row["paid_through"]) - seconds, float(row["started_at"]) - seconds, meter_id),
    )


def test_a_meter_charges_whole_elapsed_minutes(user: str):
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    _age(meter["id"], 185)  # three minutes and five seconds

    result = metering._charge(meter["id"])

    assert result["minutes"] == 3
    assert result["charged"] == 6
    assert credits.balance(user) == 994


def test_the_partial_minute_is_only_charged_on_close(user: str):
    """Otherwise a caller takes 59 free seconds per session, on repeat."""
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    _age(meter["id"], 30)

    assert metering._charge(meter["id"])["charged"] == 0
    assert credits.balance(user) == 1000

    metering.close_meter(meter["id"])
    assert credits.balance(user) == 998


def test_two_sweeps_cannot_bill_the_same_minute_twice(user: str):
    """Four workers all sweep; the minute belongs to whoever gets there first."""
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    _age(meter["id"], 120)

    first = metering._charge(meter["id"])
    second = metering._charge(meter["id"])

    assert first["minutes"] == 2
    assert second["minutes"] == 0
    assert credits.balance(user) == 996


def test_charging_does_not_drift(user: str):
    """`paid_through` advances by exactly the minutes charged.

    Advancing it to "now" instead would silently donate the leftover seconds
    of every tick back to the user, which over a long session is real money.
    """
    meter = metering.open_meter(user, "notebook", "s1", rate=1)
    _age(meter["id"], 90)
    metering._charge(meter["id"])

    row = store.one("SELECT paid_through, started_at FROM meters WHERE id = ?", (meter["id"],))
    assert abs((row["paid_through"] - row["started_at"]) - 60.0) < 0.01


def test_a_meter_stops_when_the_credits_run_out(user: str):
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    # The balance can fall while a session is open - other work spends it too.
    store.execute("UPDATE users SET api_credits = 5 WHERE id = ?", (user,))
    _age(meter["id"], 600)  # ten minutes owed, two and a half affordable

    result = metering._charge(meter["id"])

    assert result["status"] == metering.UNPAID
    # Charged what the balance covered, not more: a negative balance is a
    # debt we cannot collect and did not agree to extend.
    assert result["charged"] == 4
    assert credits.balance(user) == 1


def test_the_sweep_returns_unaffordable_meters_for_termination(user: str):
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    store.execute("UPDATE users SET api_credits = 2 WHERE id = ?", (user,))
    _age(meter["id"], 300)

    stop = {m["id"]: m for m in metering.sweep()["stop"]}

    # Other tests leave meters in the same database, so this asserts about
    # this meter rather than about the whole sweep.
    assert meter["id"] in stop
    assert stop[meter["id"]]["why"] == metering.UNPAID


def test_an_abandoned_meter_is_closed_and_handed_back(user: str):
    """The worker that owned it is gone; the cloud instance is not.

    This is the expensive orphan: an instance nobody is watching bills a real
    provider in real time, so another worker has to be able to reclaim it.
    """
    meter = metering.open_meter(
        user, "notebook", "s1", rate=2, provider="hetzner", remote={"id": "srv-1"}
    )
    store.execute(
        "UPDATE meters SET last_seen = ? WHERE id = ?",
        (time.time() - metering.ABANDON_AFTER - 30, meter["id"]),
    )

    stop = {m["id"]: m for m in metering.sweep()["stop"]}

    assert stop[meter["id"]]["why"] == metering.ORPHANED
    assert json.loads(stop[meter["id"]]["remote"])["id"] == "srv-1"
    row = store.one("SELECT status FROM meters WHERE id = ?", (meter["id"],))
    assert row["status"] == metering.ORPHANED


def test_a_heartbeat_keeps_a_working_session_alive(user: str):
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    store.execute(
        "UPDATE meters SET last_seen = ? WHERE id = ?",
        (time.time() - metering.ABANDON_AFTER - 30, meter["id"]),
    )
    metering.heartbeat(meter["id"])

    assert meter["id"] not in {m["id"] for m in metering.sweep()["stop"]}


def test_opening_is_refused_without_the_credits_to_run(user: str):
    """Nothing gets spawned before the money is checked."""
    store.execute("UPDATE users SET api_credits = 1 WHERE id = ?", (user,))
    with pytest.raises(credits.InsufficientCredits):
        metering.open_meter(user, "notebook", "s1", rate=2)


def test_a_refund_returns_every_metered_credit(user: str):
    meter = metering.open_meter(user, "long_agent", "job1", rate=10)
    # 3 whole minutes plus a part-minute the close rounds up: 4 either way.
    _age(meter["id"], 235)
    metering.close_meter(meter["id"])
    assert credits.balance(user) == 960

    returned = metering.refund(meter["id"], reason="deep_research_refund")

    assert returned == 40
    assert credits.balance(user) == 1000


def test_included_minutes_are_used_before_credits():
    """A plan that includes notebook time has not given it if credits move."""
    user_id = _make_user("pro")
    meter = metering.open_meter(user_id, "notebook", "s1", rate=2)
    _age(meter["id"], 180)

    result = metering._charge(meter["id"])

    assert result["minutes"] == 3
    assert result["charged"] == 0
    assert result["included"] == 3
    assert credits.balance(user_id) == 1000

    from twohelixes import entitlements

    period = entitlements.period_for(user_id)
    assert period.used["notebook_minute"] == 3


def test_credits_pick_up_where_the_included_minutes_stop():
    user_id = _make_user("pro")
    included = config.PLAN_ALLOWANCES["pro"]["notebook_minute"]
    store.execute(
        "UPDATE users SET plan_usage = ? WHERE id = ?",
        (store.dump_json({"notebook_minute": included - 1}), user_id),
    )
    meter = metering.open_meter(user_id, "notebook", "s1", rate=2)
    _age(meter["id"], 180)  # three minutes: one included, two paid

    result = metering._charge(meter["id"])

    assert result["included"] == 1
    assert result["charged"] == 4
    assert credits.balance(user_id) == 996


def test_usage_reports_what_is_running_and_what_it_burns(user: str):
    open_meter = metering.open_meter(user, "notebook", "s1", rate=2)
    closed = metering.open_meter(user, "long_agent", "job1", rate=10)
    # 1 whole minute plus a part-minute rounded up on close: 2 either way.
    _age(closed["id"], 115)
    metering.close_meter(closed["id"])

    report = metering.usage(user)

    assert [m["id"] for m in report["open"]] == [open_meter["id"]]
    assert report["credits_per_minute_open"] == 2
    assert report["charged_total"] == 20


def test_a_closed_meter_stops_charging(user: str):
    """The complaint that matters most: billed after you closed it."""
    meter = metering.open_meter(user, "notebook", "s1", rate=2)
    metering.close_meter(meter["id"])
    balance_after_close = credits.balance(user)

    _age(meter["id"], 600)
    metering.sweep()

    assert credits.balance(user) == balance_after_close
