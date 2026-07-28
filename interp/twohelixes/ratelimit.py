"""Rate limiting and the decision to let an operation run.

Who pays is `entitlements`. This module answers the narrower question of
whether the request may run at all, which is a different thing: an account with
plenty of allowance can still be asking sixty times a minute.

The policy:

* an anonymous caller gets **one question a day, on our sample data**. It used
  to be none at all, which is a sound bot gate and a terrible funnel - it asks
  for an account before the product has done anything. One question is enough
  to show the thing working and far too little to farm, and it is capped by
  address, so a scraper is bounded by its address pool rather than its
  patience;
* a signed-in caller runs against their plan's allowance, then their credits;
* a caller spending credits is not throttled beyond a global safety valve,
  because they have already paid for the capacity.

Counters live in SQLite so they survive a worker restart and are shared across
worker processes - an in-process dict would let a 4-worker box serve 4x the
intended free tier.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from twohelixes import config, entitlements, store

log = logging.getLogger("twohelixes.ratelimit")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    retry_after: int = 0
    detail: str = ""
    # What would have been charged, so a refusal can say what to do about it
    # rather than only that it happened.
    quote: dict[str, Any] = field(default_factory=dict)
    upgrade: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "error": self.reason,
            "detail": self.detail,
            "retry_after": self.retry_after,
        }
        if self.quote:
            payload["quote"] = self.quote
        if self.upgrade:
            payload["upgrade"] = self.upgrade
        return payload


WINDOWS = (
    ("minute", 60, config.FREE_RATE_PER_MINUTE),
    ("hour", 3600, config.FREE_RATE_PER_HOUR),
    ("day", 86400, config.FREE_RATE_PER_DAY),
)

# Operations an anonymous caller may try. Anything that reaches a customer's
# own data or costs real compute stays behind sign-in.
ANON_OPERATIONS = ("chat_query",)


def _count(identity: str, bucket: str, window: int) -> int:
    row = store.one(
        "SELECT COUNT(*) AS n FROM rate_events "
        "WHERE identity = ? AND bucket = ? AND created_at > ?",
        (identity, bucket, time.time() - window),
    )
    return int(row["n"]) if row else 0


def record(identity: str, bucket: str) -> None:
    store.execute(
        "INSERT INTO rate_events (identity, bucket, created_at) VALUES (?, ?, ?)",
        (identity, bucket, time.time()),
    )


def sweep(older_than: int = 86400 * 2) -> None:
    store.execute(
        "DELETE FROM rate_events WHERE created_at < ?", (time.time() - older_than,)
    )


def anonymous_trial_left(identity: Any) -> int:
    used = _count(f"ip:{getattr(identity, 'ip', '0.0.0.0')}", "anon", 86400)
    return max(0, config.ANON_QUERIES_PER_DAY - used)


def check(identity: Any, operation: str, *, sample_data: bool = False) -> Decision:
    """Decide whether `identity` may run `operation` right now."""
    key = identity.key

    # Global valve applies to everyone, paid included.
    if _count(key, "any", 60) >= config.HARD_RATE_PER_MINUTE:
        return Decision(
            False, "rate_limited", retry_after=60,
            detail="Too many requests in the last minute.",
        )

    if not identity.signed_in:
        if operation not in ANON_OPERATIONS or config.ANON_QUERIES_PER_DAY <= 0:
            return Decision(
                False, "signin_required",
                detail="Sign in to run this.",
                upgrade="signin",
            )
        if config.ANON_SAMPLE_DATA_ONLY and not sample_data:
            return Decision(
                False, "signin_required",
                detail=(
                    "Try it on the sample data first, or sign in to use your own - "
                    "it takes an email and nothing else."
                ),
                upgrade="signin",
            )
        if anonymous_trial_left(identity) <= 0:
            return Decision(
                False, "trial_used",
                detail=(
                    "That is today's free question. An email gets you "
                    f"{config.PLAN_ALLOWANCES['free']['chat_query']} charts a "
                    "month on your own data, renewing, with no card."
                ),
                upgrade="signin",
            )
        return Decision(True)

    quote = entitlements.quote(identity, operation)
    if quote.payer == "blocked":
        detail = (
            "You have used this month's included charts. Add credits, or move "
            "up a plan to raise the allowance."
            if quote.reason == "out_of_allowance"
            else f"This costs {quote.credits_required} credits."
        )
        return Decision(
            False, quote.reason or "insufficient_credits",
            detail=detail, quote=quote.as_dict(), upgrade="plan",
        )

    # Credits are the paid path: not throttled beyond the global valve, because
    # the capacity has been bought.
    if quote.payer == "credits":
        return Decision(True, quote=quote.as_dict())

    # Included allowance: keep the burst windows, so one account cannot turn a
    # month of allowance into a minute of load.
    for bucket, window, limit in WINDOWS:
        if _count(key, bucket, window) >= limit:
            return Decision(
                False, "rate_limited", retry_after=window,
                detail=f"Included usage allows {limit} queries per {bucket}.",
                quote=quote.as_dict(),
            )

    return Decision(True, quote=quote.as_dict())


def consume(identity: Any, operation: str, ref: str = "") -> dict[str, Any]:
    """Charge for a successful operation.

    Called only after the work succeeded, so a failed pipeline does not burn
    anyone's allowance or credits.
    """
    key = identity.key
    record(key, "any")

    if not identity.signed_in:
        record(f"ip:{getattr(identity, 'ip', '0.0.0.0')}", "anon")
        return {"payer": "trial"}

    quote = entitlements.charge(identity, operation, ref=ref)

    if quote.payer == "allowance":
        for bucket, _, _ in WINDOWS:
            record(key, bucket)
    elif quote.payer == "credits":
        identity.api_credits = max(0, identity.api_credits - quote.credits_required)

    return quote.as_dict()
