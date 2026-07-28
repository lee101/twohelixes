"""What a caller is allowed to spend, and in what order.

One place answers "may this run, and who pays for it", because the answer has
three parts that were previously scattered: a monthly allowance included with
the plan, a prepaid credit balance, and a volume discount that depends on what
the caller has already spent this month.

The order is deliberate and is the whole of the pricing model:

1. **Included allowance first.** A subscriber's included charts are the thing
   they bought; spending their credits while allowance remains would be taking
   the more valuable currency first.
2. **Then credits**, at the list price less whatever volume discount the
   caller's trailing spend has earned.
3. **Then nothing.** No overdraft, no surprise invoice. A plan that has run out
   waits for the next period or a top-up, which is what people expect from a
   subscription and is also the only version we can defend in a chargeback.

The allowance renews on a rolling 30-day period anchored to when the plan
started, not on calendar months: a user who subscribes on the 31st should not
get a shorter first period than one who subscribes on the 1st.

Free is a plan like any other here. It renews too - a lifetime allowance of
three queries meant a new account hit a wall on its first afternoon, which is a
strange thing to do to someone who has just arrived.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from twohelixes import config, credits, store

log = logging.getLogger("twohelixes.entitlements")

PERIOD_SECONDS = config.FREE_PERIOD_DAYS * 86400

# Operations that draw on a named allowance. Anything else is credits-only.
ALLOWANCE_FOR = {
    "chat_query": "chat_query",
    "dashboard_build": "chat_query",  # a dashboard is several charts
    "structure_import": "chat_query",
    "deep_research": "deep_research",
    "notebook_minute": "notebook_minute",
}

# A dashboard build spends this many of the chart allowance, because that is
# roughly how many charts it draws.
ALLOWANCE_UNITS = {"dashboard_build": 3}


@dataclass
class Quote:
    """What an operation will cost this caller, right now."""

    operation: str
    allowance: str = ""
    allowance_units: int = 0
    allowance_left: int = 0
    credits_required: int = 0
    micro_price: int = 0
    list_credits: int = 0
    discount: float = 0.0
    payer: str = "credits"  # allowance | credits | blocked
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "payer": self.payer,
            "credits": self.credits_required,
            "unit_price": round(self.micro_price / 1000, 3) if self.micro_price else 0,
            "list_credits": self.list_credits,
            "discount": self.discount,
            "allowance": self.allowance,
            "allowance_left": self.allowance_left,
            "reason": self.reason,
        }


@dataclass
class Period:
    plan: str
    started_at: float
    ends_at: float
    used: dict[str, int] = field(default_factory=dict)

    def left(self, allowance: str) -> int:
        included = int(config.PLAN_ALLOWANCES.get(self.plan, {}).get(allowance, 0) or 0)
        return max(0, included - int(self.used.get(allowance, 0)))


# --------------------------------------------------------------------------
# The period
# --------------------------------------------------------------------------


def period_for(user_id: str) -> Period:
    """The caller's current allowance period, rolling it over if it has ended.

    Rolling over on read rather than on a schedule means there is no cron to
    miss and no window where a paid-up subscriber is told they have run out.
    """
    row = store.one(
        "SELECT plan, plan_period_start, plan_usage FROM users WHERE id = ?", (user_id,)
    )
    if row is None:
        return Period(plan="free", started_at=time.time(), ends_at=time.time() + PERIOD_SECONDS)

    plan = str(row["plan"] or "free")
    started = float(row["plan_period_start"] or 0)
    used = store.load_json(row["plan_usage"], {}) or {}
    now = time.time()

    if not started or now >= started + PERIOD_SECONDS:
        # Anchor forward in whole periods rather than to `now`, so a user who
        # returns after two months keeps their original renewal day.
        if started:
            elapsed = now - started
            started = started + (int(elapsed // PERIOD_SECONDS) * PERIOD_SECONDS)
        else:
            started = now
        used = {}
        store.execute(
            "UPDATE users SET plan_period_start = ?, plan_usage = ? WHERE id = ?",
            (started, store.dump_json(used), user_id),
        )

    return Period(plan=plan, started_at=started, ends_at=started + PERIOD_SECONDS, used=used)


def take_units_locked(conn: Any, user_id: str, allowance: str, units: int) -> int:
    """Take up to `units` from an allowance inside a caller's transaction.

    The `_locked` twin exists for the same reason `credits.apply_locked` does:
    the meter charges minutes inside one immediate transaction, and opening a
    second one from inside it is an error, not a nesting.
    """
    row = conn.execute(
        "SELECT plan, plan_usage FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if row is None:
        return 0
    plan = str(row["plan"] or "free")
    included = int(config.PLAN_ALLOWANCES.get(plan, {}).get(allowance, 0) or 0)
    used = store.load_json(row["plan_usage"], {}) or {}
    current = int(used.get(allowance, 0))
    take = max(0, min(units, included - current))
    if take:
        used[allowance] = current + take
        conn.execute(
            "UPDATE users SET plan_usage = ? WHERE id = ?",
            (store.dump_json(used), user_id),
        )
    return take


def take_units(user_id: str, allowance: str, units: int) -> int:
    """Take up to `units` from an allowance; return how many were taken.

    Partial by design, because metered work arrives in lumps: a session that
    runs five minutes when two are included should use the two and pay for
    three, not fall back to paying for five.
    """
    with store.transaction() as conn:
        taken = take_units_locked(conn, user_id, allowance, units)
        return taken


def give_back(user_id: str, allowance: str, units: int) -> None:
    """Return allowance taken for work that produced nothing.

    The refund path for included usage. Without it, "you never pay for a run
    that told you nothing" would be true for credits and false for a plan,
    which is the version that reads as a trick.
    """
    with store.transaction() as conn:
        row = conn.execute("SELECT plan_usage FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is not None:
            used = store.load_json(row["plan_usage"], {}) or {}
            used[allowance] = max(0, int(used.get(allowance, 0)) - units)
            conn.execute(
                "UPDATE users SET plan_usage = ? WHERE id = ?",
                (store.dump_json(used), user_id),
            )


def _consume_allowance(user_id: str, allowance: str, units: int) -> bool:
    """Take `units` from the allowance, atomically. False if it is not there."""
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT plan, plan_period_start, plan_usage FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return False

        plan = str(row["plan"] or "free")
        included = int(config.PLAN_ALLOWANCES.get(plan, {}).get(allowance, 0) or 0)
        used = store.load_json(row["plan_usage"], {}) or {}
        current = int(used.get(allowance, 0))
        if current + units > included:
            return False

        used[allowance] = current + units
        conn.execute(
            "UPDATE users SET plan_usage = ? WHERE id = ?",
            (store.dump_json(used), user_id),
        )
        return True


# --------------------------------------------------------------------------
# Volume pricing
# --------------------------------------------------------------------------


def spend_last_30_days(user_id: str) -> int:
    row = store.one(
        "SELECT COALESCE(SUM(-delta), 0) AS spent FROM credit_ledger "
        "WHERE user_id = ? AND delta < 0 AND created_at > ?",
        (user_id, time.time() - 30 * 86400),
    )
    return int(row["spent"]) if row else 0


def discount_for(user_id: str) -> tuple[float, int, int | None]:
    """(discount, spend, next tier threshold) for this caller.

    Automatic rather than negotiated: a customer whose usage grows should see
    their unit price fall without having to ask, and a customer deciding
    whether to move more volume here should be able to see what it would earn
    them before they commit.
    """
    spent = spend_last_30_days(user_id)
    discount = 0.0
    next_threshold: int | None = None
    for threshold, rate in config.VOLUME_TIERS:
        if spent >= threshold:
            discount = rate
        elif next_threshold is None:
            next_threshold = threshold
    return discount, spent, next_threshold


# A credit is a cent, and the unit prices are small integers, so a 10%
# discount on a 4-credit chart rounds straight back to 4 and the whole volume
# scheme is decorative. Prices are therefore computed in thousandths of a
# credit and the remainder is carried on the account: the customer is charged
# whole credits, and the fractions accumulate until they are worth one.
#
# The alternative - rounding each charge - either gives away up to half a
# credit per call or takes one, at a scale of millions of calls. Carrying the
# dust is exact in both directions.
MICRO = 1000


def price_micro(operation: str, discount: float = 0.0) -> int:
    """Price in thousandths of a credit, after the volume discount."""
    listed = int(config.CREDIT_COST.get(operation, 0))
    if listed <= 0:
        return 0
    return max(1, round(listed * MICRO * (1.0 - discount)))


def price_of(operation: str, discount: float = 0.0) -> float:
    """Price in credits, as a number a human can read on an invoice."""
    return round(price_micro(operation, discount) / MICRO, 3)


def _dust(user_id: str) -> int:
    row = store.one("SELECT credit_dust FROM users WHERE id = ?", (user_id,))
    return int((row["credit_dust"] if row else 0) or 0)


# --------------------------------------------------------------------------
# Quoting and charging
# --------------------------------------------------------------------------


def quote(identity: Any, operation: str, units: int = 0) -> Quote:
    """What this operation costs this caller, without charging for it."""
    listed = int(config.CREDIT_COST.get(operation, 0))
    result = Quote(operation=operation, list_credits=listed, credits_required=listed)

    if listed == 0:
        result.payer = "free"
        return result

    if not getattr(identity, "signed_in", False):
        result.payer = "blocked"
        result.reason = "signin_required"
        return result

    allowance = ALLOWANCE_FOR.get(operation, "")
    needed = units or ALLOWANCE_UNITS.get(operation, 1)
    if allowance:
        period = period_for(identity.user_id)
        left = period.left(allowance)
        result.allowance = allowance
        result.allowance_left = left
        result.allowance_units = needed
        if left >= needed:
            result.payer = "allowance"
            result.credits_required = 0
            return result

    discount, _, _ = discount_for(identity.user_id)
    result.discount = discount
    result.micro_price = price_micro(operation, discount)
    # What this call will actually take off the balance, once the fractions
    # carried from previous calls are included.
    result.credits_required = (result.micro_price + _dust(identity.user_id)) // MICRO
    if credits.balance(identity.user_id) < result.credits_required:
        result.payer = "blocked"
        result.reason = "out_of_allowance" if allowance else "insufficient_credits"
        return result

    result.payer = "credits"
    return result


def charge(identity: Any, operation: str, ref: str = "", units: int = 0) -> Quote:
    """Take payment for a completed operation.

    Called after success, never before: a failed pipeline must not spend
    anyone's allowance, which is the same rule the credit path already had.
    """
    result = quote(identity, operation, units=units)
    if result.payer == "allowance":
        needed = result.allowance_units or 1
        if _consume_allowance(identity.user_id, result.allowance, needed):
            return result
        # Lost a race with another request in the same period; fall through
        # and let the credits path answer instead of double-spending.
        result = quote(identity, operation, units=units)

    if result.payer == "credits":
        try:
            _spend(identity.user_id, operation, result.micro_price, ref)
        except credits.InsufficientCredits:
            result.payer = "blocked"
            result.reason = "insufficient_credits"
    return result


def _spend(user_id: str, operation: str, micro: int, ref: str = "") -> None:
    """Take whole credits and carry the fraction, atomically."""
    with store.transaction() as conn:
        row = conn.execute(
            "SELECT credit_dust FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        carried = int((row["credit_dust"] if row else 0) or 0) + micro
        whole, remainder = divmod(carried, MICRO)
        if whole:
            credits.apply_locked(conn, user_id, -whole, operation, ref or None)
        conn.execute(
            "UPDATE users SET credit_dust = ? WHERE id = ?", (remainder, user_id)
        )


def summary(identity: Any) -> dict[str, Any]:
    """Everything a caller needs to understand their next bill."""
    if not getattr(identity, "signed_in", False):
        return {"plan": "anon", "signed_in": False}

    period = period_for(identity.user_id)
    plan = config.PLAN_ALLOWANCES.get(period.plan, config.PLAN_ALLOWANCES["free"])
    discount, spent, next_threshold = discount_for(identity.user_id)

    return {
        "signed_in": True,
        "plan": period.plan,
        "period_start": period.started_at,
        "period_end": period.ends_at,
        "renews_in_days": max(0, round((period.ends_at - time.time()) / 86400, 1)),
        "included": {
            name: {"limit": int(plan.get(name, 0) or 0), "used": int(period.used.get(name, 0))}
            for name in ("chat_query", "deep_research", "notebook_minute")
        },
        "credits": credits.balance(identity.user_id),
        "volume": {
            "spend_30d_credits": spent,
            "discount": discount,
            "next_tier_at_credits": next_threshold,
            "next_tier_discount": next(
                (rate for threshold, rate in config.VOLUME_TIERS if threshold == next_threshold),
                None,
            ),
        },
        "prices": {
            name: price_of(name, discount)
            for name in config.CREDIT_COST
            if config.CREDIT_COST[name]
        },
        "list_prices": {
            name: value for name, value in config.CREDIT_COST.items() if value
        },
    }
