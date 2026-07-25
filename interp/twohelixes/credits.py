"""API credit ledger.

Every balance change is an append-only ledger row carrying the resulting
balance, so a disputed charge can be reconstructed without trusting the
denormalised `users.api_credits` column. That column exists only so the hot
path can read a balance with one indexed lookup.

1 credit == 1 US cent.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from twohelixes import store

log = logging.getLogger("twohelixes.credits")


class InsufficientCredits(Exception):
    def __init__(self, needed: int, available: int):
        super().__init__(f"needs {needed} credits, has {available}")
        self.needed = needed
        self.available = available


def balance(user_id: str) -> int:
    row = store.one("SELECT api_credits FROM users WHERE id = ?", (user_id,))
    return int(row["api_credits"]) if row else 0


def _write(user_id: str, delta: int, reason: str, ref: str | None) -> int:
    conn = store.connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT api_credits FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown user {user_id}")
        current = int(row["api_credits"])
        new_balance = current + delta
        if new_balance < 0:
            raise InsufficientCredits(-delta, current)
        conn.execute(
            "UPDATE users SET api_credits = ? WHERE id = ?", (new_balance, user_id)
        )
        conn.execute(
            "INSERT INTO credit_ledger (id, user_id, delta, balance, reason, ref, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (store.new_id(), user_id, delta, new_balance, reason, ref, time.time()),
        )
        conn.execute("COMMIT")
        return new_balance
    except Exception:
        conn.execute("ROLLBACK")
        raise


def grant(user_id: str, amount: int, reason: str = "purchase", ref: str | None = None) -> int:
    if amount <= 0:
        return balance(user_id)
    new_balance = _write(user_id, amount, reason, ref)
    log.info("granted %d credits to %s (%s) -> %d", amount, user_id, reason, new_balance)
    return new_balance


def deduct(user_id: str, amount: int, reason: str = "usage", ref: str | None = None) -> int:
    if amount <= 0:
        return balance(user_id)
    return _write(user_id, -amount, reason, ref)


def ledger(user_id: str, limit: int = 100) -> list[dict[str, Any]]:
    return store.rows_to_dicts(
        store.query(
            "SELECT delta, balance, reason, ref, created_at FROM credit_ledger "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
    )
