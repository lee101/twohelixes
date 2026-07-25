"""Rate limiting and free-query accounting.

The policy the product needs:

* anonymous callers get nothing that costs an LLM call - that is the bot gate;
* a signed-in free user gets `FREE_QUERIES_PER_USER` full pipeline runs total,
  under heavy per-minute/hour/day limits;
* a paying caller spending API credits is not throttled beyond a global safety
  valve, because they have already paid for the capacity.

Counters live in SQLite so they survive a worker restart and are shared across
worker processes - an in-process dict would let a 4-worker box serve 4x the
intended free tier.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from twohelixes import config, store

log = logging.getLogger("twohelixes.ratelimit")


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    retry_after: int = 0
    detail: str = ""

    def to_payload(self) -> dict[str, object]:
        return {
            "error": self.reason,
            "detail": self.detail,
            "retry_after": self.retry_after,
        }


WINDOWS = (
    ("minute", 60, config.FREE_RATE_PER_MINUTE),
    ("hour", 3600, config.FREE_RATE_PER_HOUR),
    ("day", 86400, config.FREE_RATE_PER_DAY),
)


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


def check(identity, operation: str) -> Decision:
    """Decide whether `identity` may run `operation` right now."""
    key = identity.key
    cost = config.CREDIT_COST.get(operation, 0)

    # Global valve applies to everyone, paid included.
    if _count(key, "any", 60) >= config.HARD_RATE_PER_MINUTE:
        return Decision(
            False,
            "rate_limited",
            retry_after=60,
            detail="Too many requests in the last minute.",
        )

    if not identity.signed_in:
        return Decision(
            False,
            "signin_required",
            detail=(
                "Sign in to run queries. New accounts get "
                f"{config.FREE_QUERIES_PER_USER} free queries."
            ),
        )

    # Paying callers: gate on balance, not on counters.
    if identity.paid:
        if identity.api_credits < cost:
            return Decision(
                False,
                "insufficient_credits",
                detail=f"This operation costs {cost} credits.",
            )
        return Decision(True)

    # Free tier: lifetime allowance first, then the burst windows.
    if identity.free_queries_used >= config.FREE_QUERIES_PER_USER:
        return Decision(
            False,
            "free_quota_exhausted",
            detail=(
                f"You have used all {config.FREE_QUERIES_PER_USER} free queries. "
                "Add API credits to keep going."
            ),
        )

    for bucket, window, limit in WINDOWS:
        if _count(key, bucket, window) >= limit:
            return Decision(
                False,
                "rate_limited",
                retry_after=window,
                detail=f"Free tier allows {limit} queries per {bucket}.",
            )

    return Decision(True)


def consume(identity, operation: str) -> None:
    """Charge for a successful operation.

    Called only after the work succeeded, so a failed pipeline does not burn a
    user's free allowance.
    """
    key = identity.key
    record(key, "any")

    if not identity.signed_in:
        return

    if identity.paid:
        from twohelixes import credits

        cost = config.CREDIT_COST.get(operation, 0)
        if cost:
            credits.deduct(identity.user_id, cost, reason=operation)
            identity.api_credits = max(0, identity.api_credits - cost)
        return

    for bucket, _, _ in WINDOWS:
        record(key, bucket)
    identity.free_queries_used += 1
    store.execute(
        "UPDATE users SET free_queries_used = free_queries_used + 1 WHERE id = ?",
        (identity.user_id,),
    )
