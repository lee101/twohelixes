"""Pricing rules.

These are the tests that decide what a customer is charged, so they are
written against the promises the pricing page makes rather than against the
implementation: included usage is spent before credits, an allowance renews,
nothing goes into overdraft, volume discounts apply automatically, and a price
never drops below what the work costs us.
"""

from __future__ import annotations

import time

import pytest

from twohelixes import config, credits, entitlements, store


class FakeIdentity:
    def __init__(self, user_id: str, plan: str, api_credits: int):
        self.user_id = user_id
        self.plan = plan
        self.api_credits = api_credits
        self.signed_in = True
        self.ip = "127.0.0.1"

    @property
    def key(self) -> str:
        return f"user:{self.user_id}"


def _user(plan: str = "free", credits_balance: int = 0) -> FakeIdentity:
    store.init()
    user_id = store.new_id()
    store.execute(
        "INSERT INTO users (id, email, plan, api_credits, free_queries_used, created_at, "
        "plan_period_start, plan_usage) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (
            user_id, f"{user_id}@example.com", plan, credits_balance,
            time.time(), time.time(), store.dump_json({}),
        ),
    )
    return FakeIdentity(user_id, plan, credits_balance)


# -- what gets spent first --------------------------------------------------


def test_included_usage_is_spent_before_credits(user_free=None):
    """A subscriber's included charts are the thing they bought."""
    identity = _user("plus", credits_balance=500)

    result = entitlements.charge(identity, "chat_query")

    assert result.payer == "allowance"
    assert result.credits_required == 0
    assert credits.balance(identity.user_id) == 500


def test_credits_take_over_when_the_allowance_runs_out():
    identity = _user("free", credits_balance=100)
    included = config.PLAN_ALLOWANCES["free"]["chat_query"]

    for _ in range(included):
        assert entitlements.charge(identity, "chat_query").payer == "allowance"

    result = entitlements.charge(identity, "chat_query")

    assert result.payer == "credits"
    assert result.credits_required == config.CREDIT_COST["chat_query"]
    assert credits.balance(identity.user_id) == 100 - result.credits_required


def test_nothing_runs_on_credit_it_does_not_have():
    """No overdraft. A plan that has run out waits; it does not invoice."""
    identity = _user("free", credits_balance=0)
    for _ in range(config.PLAN_ALLOWANCES["free"]["chat_query"]):
        entitlements.charge(identity, "chat_query")

    result = entitlements.quote(identity, "chat_query")

    assert result.payer == "blocked"
    assert result.reason == "out_of_allowance"


def test_a_dashboard_costs_several_charts_of_allowance():
    """It draws several charts, so it should not cost one chart's worth."""
    identity = _user("pro")
    entitlements.charge(identity, "dashboard_build")

    period = entitlements.period_for(identity.user_id)
    assert period.used["chat_query"] == entitlements.ALLOWANCE_UNITS["dashboard_build"]


# -- the period -------------------------------------------------------------


def test_the_allowance_renews():
    """A lifetime allowance meant a new account hit a wall on day one."""
    identity = _user("free")
    for _ in range(config.PLAN_ALLOWANCES["free"]["chat_query"]):
        entitlements.charge(identity, "chat_query")
    assert entitlements.quote(identity, "chat_query").payer == "blocked"

    store.execute(
        "UPDATE users SET plan_period_start = ? WHERE id = ?",
        (time.time() - entitlements.PERIOD_SECONDS - 10, identity.user_id),
    )

    assert entitlements.quote(identity, "chat_query").payer == "allowance"


def test_a_renewal_keeps_the_original_anchor_day():
    """Coming back after two months should not move the renewal date."""
    identity = _user("free")
    anchor = time.time() - (entitlements.PERIOD_SECONDS * 2 + 3600)
    store.execute(
        "UPDATE users SET plan_period_start = ? WHERE id = ?", (anchor, identity.user_id)
    )

    period = entitlements.period_for(identity.user_id)

    drift = (period.started_at - anchor) % entitlements.PERIOD_SECONDS
    assert drift < 1.0, "the renewal day slid"
    assert period.started_at <= time.time()


def test_usage_does_not_leak_across_periods():
    identity = _user("plus")
    entitlements.charge(identity, "chat_query")
    store.execute(
        "UPDATE users SET plan_period_start = ? WHERE id = ?",
        (time.time() - entitlements.PERIOD_SECONDS - 10, identity.user_id),
    )

    period = entitlements.period_for(identity.user_id)

    assert period.used == {}


# -- volume pricing ---------------------------------------------------------


def test_volume_discount_applies_automatically():
    """A growing customer's unit price falls without a sales call."""
    identity = _user("plus", credits_balance=100_000)
    threshold = config.VOLUME_TIERS[1][0]
    credits.deduct(identity.user_id, threshold, reason="chat_query")

    discount, spent, next_tier = entitlements.discount_for(identity.user_id)

    assert discount == config.VOLUME_TIERS[1][1]
    assert spent >= threshold
    assert next_tier == config.VOLUME_TIERS[2][0]
    assert entitlements.price_of("chat_query", discount) < config.CREDIT_COST["chat_query"]


def test_a_sub_credit_price_is_carried_rather_than_rounded():
    """A discount smaller than one credit still has to be real.

    Rounding each charge would either give away half a credit a call or take
    one; the fractions are carried on the account instead, so ten discounted
    charts cost exactly what ten discounted charts should.
    """
    identity = _user("free", credits_balance=100)
    for _ in range(config.PLAN_ALLOWANCES["free"]["chat_query"]):
        entitlements.charge(identity, "chat_query")

    before = credits.balance(identity.user_id)
    for _ in range(10):
        entitlements.charge(identity, "chat_query")
    spent = before - credits.balance(identity.user_id)

    expected = 10 * config.CREDIT_COST["chat_query"]
    assert spent == expected, "carried fractions leaked"


def test_a_discount_is_exact_across_many_calls():
    identity = _user("plus", credits_balance=200_000)
    # Exhaust the included charts, or the calls never reach the credit path.
    store.execute(
        "UPDATE users SET plan_usage = ? WHERE id = ?",
        (store.dump_json({"chat_query": config.PLAN_ALLOWANCES["plus"]["chat_query"]}),
         identity.user_id),
    )
    # Spend enough to reach the second tier, then measure the next hundred.
    credits.deduct(identity.user_id, config.VOLUME_TIERS[1][0], reason="chat_query")
    discount = entitlements.discount_for(identity.user_id)[0]
    assert discount > 0

    before = credits.balance(identity.user_id)
    for _ in range(100):
        entitlements.charge(identity, "chat_query")
    spent = before - credits.balance(identity.user_id)

    expected = round(100 * config.CREDIT_COST["chat_query"] * (1 - discount))
    assert abs(spent - expected) <= 1, (spent, expected)


def test_a_free_operation_stays_free_at_every_tier():
    assert entitlements.price_of("sql_autocomplete", 0.0) == 0
    assert entitlements.price_of("sql_autocomplete", 0.3) == 0


# -- margins ----------------------------------------------------------------


@pytest.mark.parametrize(
    "operation,measured",
    [
        ("chat_query", config.MEASURED_COST_CENTS["chat_query_p90"]),
        ("dashboard_build", config.MEASURED_COST_CENTS["dashboard_build"]),
        ("sql_generate", config.MEASURED_COST_CENTS["sql_generate"]),
    ],
)
def test_the_list_price_covers_the_measured_cost(operation: str, measured: float):
    """A price under its own cost is a subscription to a loss.

    Measured against the p90, not the median: the expensive queries are the
    ones that join tables and write pandas, which is the product.
    """
    assert config.CREDIT_COST[operation] >= measured


def test_the_deepest_discount_still_covers_the_p90_cost():
    deepest = max(rate for _, rate in config.VOLUME_TIERS)
    price = entitlements.price_of("chat_query", deepest)
    assert price >= config.MEASURED_COST_CENTS["chat_query_p90"]


def test_every_plan_is_profitable_at_full_utilisation():
    """The allowance is a promise; it has to be one we can afford to keep."""
    p90 = config.MEASURED_COST_CENTS["chat_query_p90"]
    deep_minute = config.MEASURED_COST_CENTS["deep_research_minute"]

    for name, plan in config.PLAN_ALLOWANCES.items():
        if name == "free":
            continue
        # A deep-research run is charged a base plus minutes; assume a
        # ten-minute run, which is a long one.
        worst_case = (
            plan["chat_query"] * p90
            + plan["deep_research"] * deep_minute * 10
            + plan["notebook_minute"] * 0.05
        )
        assert worst_case < plan["price_cents"], (
            f"{name} loses money at full use: {worst_case:.0f}c cost vs "
            f"{plan['price_cents']}c charged"
        )


# -- reporting --------------------------------------------------------------


def test_the_summary_tells_a_caller_what_happens_next():
    identity = _user("pro", credits_balance=2500)
    entitlements.charge(identity, "chat_query")

    summary = entitlements.summary(identity)

    assert summary["plan"] == "pro"
    assert summary["included"]["chat_query"]["used"] == 1
    assert summary["included"]["chat_query"]["limit"] == (
        config.PLAN_ALLOWANCES["pro"]["chat_query"]
    )
    assert summary["credits"] == 2500
    assert 0 < summary["renews_in_days"] <= config.FREE_PERIOD_DAYS
    assert summary["prices"]["chat_query"] == config.CREDIT_COST["chat_query"]
