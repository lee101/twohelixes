"""Per-minute billing for work that outlives a request.

A chart is billed per call: it succeeds, it costs two credits, done. A hosted
notebook or a long-running agent is not like that. It holds a process - often a
cloud instance we are paying for by the hour - for as long as the user leaves
it open, so the only honest unit is time, and the accounting has to survive
things a request never has to survive.

Three failures this exists to prevent, all of which the in-process version had:

* **A worker restart stopped the billing but not the spending.** Sessions lived
  in a dict inside one of four `SO_REUSEPORT` workers. Restart that worker and
  the meter vanished while the marimo process - or the Hetzner box - kept
  running, unbilled, forever. Meters live in SQLite for the same reason the
  free-tier counters do: four workers must share one view.

* **Two workers could bill the same minute twice.** Charging advances
  `paid_through` inside the same `BEGIN IMMEDIATE` transaction that writes the
  ledger row, so a minute is either charged once or not at all. Whoever loses
  the race reads the advanced mark and finds nothing due.

* **A crashed owner left a cloud instance running.** Meters carry the provider
  and the remote handle, so any worker can reclaim an instance whose owner
  stopped heartbeating - the money keeps being spent whether or not the process
  that started it is alive to notice.

Time is charged in whole minutes: full minutes as they elapse, and the final
partial minute rounded up on close. That is the convention everyone else uses,
and rounding up a partial minute at the end is the only rounding that cannot
be gamed by opening and closing sessions in a loop.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from typing import Any

from twohelixes import credits, store

log = logging.getLogger("twohelixes.metering")

MINUTE = 60.0

# How long a meter may go without a heartbeat before another worker reclaims
# whatever it was paying for. Long enough to survive a slow sweep or a paused
# process, short enough that an orphaned cloud instance is minutes of waste
# rather than hours.
ABANDON_AFTER = 5 * MINUTE

# Balance required to open a meter: nobody should start a cloud instance they
# cannot pay a few minutes for.
MINIMUM_MINUTES = 3

# Which plan allowance each kind of metered work draws on before credits.
ALLOWANCE_FOR_KIND = {
    "notebook": "notebook_minute",
}

def _allowance_for(row: Any) -> str:
    """Which included allowance, if any, may pay for this meter's minutes.

    A notebook on a CPU class draws on the plan's included machine minutes. A
    notebook on a GPU class draws on nothing: one GPU hour costs more than the
    Plus plan, so an allowance sized in CPU hours would be emptied in minutes
    and would hand out compute we paid real money for.
    """
    allowance = ALLOWANCE_FOR_KIND.get(str(row["kind"]), "")
    if not allowance:
        return ""
    try:
        detail = json.loads(row["detail"] or "{}")
    except (TypeError, ValueError, IndexError, KeyError):
        return allowance
    machine_id = str(detail.get("machine") or "")
    if not machine_id:
        return allowance
    from twohelixes import machines

    try:
        return allowance if machines.draws_on_allowance(machines.get(machine_id)) else ""
    except machines.UnknownMachine:
        return allowance


OPEN = "open"
CLOSED = "closed"
UNPAID = "unpaid"
ORPHANED = "orphaned"

_sweep_lock = threading.Lock()
_sweeper_started = False


def owner_id() -> str:
    """Which process holds the thing being metered."""
    return f"{socket.gethostname()}:{os.getpid()}"


# --------------------------------------------------------------------------
# Opening and closing
# --------------------------------------------------------------------------


def open_meter(
    user_id: str,
    kind: str,
    ref: str,
    rate: int,
    *,
    provider: str = "local",
    remote: dict[str, Any] | None = None,
    detail: dict[str, Any] | None = None,
    minimum_minutes: int = MINIMUM_MINUTES,
) -> dict[str, Any]:
    """Start metering. Raises InsufficientCredits before anything is spawned.

    `minimum_minutes` is how much runtime the balance must cover up front. It
    comes from the machine class rather than being fixed, because the window
    between "cannot pay" and "instance actually destroyed" is one sweep either
    way, and one sweep of an H100 costs more than a whole day of a CPU box.
    """
    available = credits.balance(user_id)
    needed = max(rate * max(1, minimum_minutes), rate)
    if available < needed:
        raise credits.InsufficientCredits(needed, available)

    now = time.time()
    meter_id = store.new_id()
    store.execute(
        "INSERT INTO meters (id, user_id, kind, ref, rate, provider, remote, detail, "
        "owner, status, started_at, paid_through, last_seen, charged, minutes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
        (
            meter_id, user_id, kind, ref, int(rate), provider,
            json.dumps(remote or {}), json.dumps(detail or {}),
            owner_id(), OPEN, now, now, now,
        ),
    )
    log.info("meter %s open: %s %s at %d/min for %s", meter_id, kind, ref, rate, user_id)
    return {"id": meter_id, "kind": kind, "ref": ref, "rate": rate, "started_at": now}


def heartbeat(meter_id: str) -> None:
    store.execute(
        "UPDATE meters SET last_seen = ? WHERE id = ? AND status = ?",
        (time.time(), meter_id, OPEN),
    )


def close_meter(meter_id: str, status: str = CLOSED, detail: str = "") -> dict[str, Any]:
    """Charge the final partial minute and stop the clock."""
    result = _charge(meter_id, final=True)
    store.execute(
        "UPDATE meters SET status = ?, closed_at = ?, note = ? WHERE id = ?",
        (status, time.time(), detail, meter_id),
    )
    log.info("meter %s closed (%s): %d credits", meter_id, status, result.get("charged", 0))
    return result


def by_ref(kind: str, ref: str) -> dict[str, Any] | None:
    row = store.one(
        "SELECT * FROM meters WHERE kind = ? AND ref = ? AND status = ? "
        "ORDER BY started_at DESC LIMIT 1",
        (kind, ref, OPEN),
    )
    return store.row_to_dict(row)


# --------------------------------------------------------------------------
# Charging
# --------------------------------------------------------------------------


def _charge(meter_id: str, final: bool = False) -> dict[str, Any]:
    """Take the credits owed by this meter, once.

    The meter row is read, the ledger written and `paid_through` advanced in
    one immediate transaction, so concurrent sweeps cannot both charge for the
    same minute: the loser re-reads an advanced mark and finds nothing due.
    """
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM meters WHERE id = ?", (meter_id,)).fetchone()
        if row is None or row["status"] != OPEN:
            return {"charged": 0, "minutes": 0, "status": row["status"] if row else "missing"}

        now = time.time()
        elapsed = max(0.0, now - float(row["paid_through"]))
        minutes = int(elapsed // MINUTE)
        if final and elapsed - minutes * MINUTE > 0.001:
            # The last partial minute is a whole minute. Anything else lets a
            # caller take 59 free seconds per session, repeatedly.
            minutes += 1
        if minutes <= 0:
            return {"charged": 0, "minutes": 0, "status": OPEN}

        # Included minutes first. A Pro plan that includes notebook time has
        # not been given it if the meter reaches for credits anyway.
        allowance = _allowance_for(row)
        covered = 0
        if allowance and int(row["rate"]) > 0:
            from twohelixes import entitlements

            covered = entitlements.take_units_locked(
                conn, str(row["user_id"]), allowance, minutes
            )

        billable = minutes - covered
        amount = billable * int(row["rate"])
        if amount <= 0:
            conn.execute(
                "UPDATE meters SET paid_through = ?, minutes = minutes + ? WHERE id = ?",
                (float(row["paid_through"]) + minutes * MINUTE, minutes, meter_id),
            )
            return {"charged": 0, "minutes": minutes, "included": covered, "status": OPEN}

        try:
            credits.apply_locked(
                conn, row["user_id"], -amount,
                reason=f"{row['kind']}_minute", ref=row["ref"],
            )
        except credits.InsufficientCredits:
            # Charge what the balance covers, then stop the work. Partial
            # payment beats an unbillable session that keeps running.
            available = int(
                conn.execute(
                    "SELECT api_credits FROM users WHERE id = ?", (row["user_id"],)
                ).fetchone()["api_credits"]
            )
            affordable = available // int(row["rate"]) if row["rate"] else 0
            amount = affordable * int(row["rate"])
            minutes = covered + affordable
            if amount:
                credits.apply_locked(
                    conn, row["user_id"], -amount,
                    reason=f"{row['kind']}_minute", ref=row["ref"],
                )
            conn.execute(
                "UPDATE meters SET paid_through = ?, charged = charged + ?, "
                "minutes = minutes + ?, status = ? WHERE id = ?",
                (now, amount, minutes, UNPAID, meter_id),
            )
            log.info("meter %s ran out of credits", meter_id)
            return {"charged": amount, "minutes": minutes, "status": UNPAID}

        conn.execute(
            "UPDATE meters SET paid_through = ?, charged = charged + ?, "
            "minutes = minutes + ? WHERE id = ?",
            (float(row["paid_through"]) + minutes * MINUTE, amount, minutes, meter_id),
        )
        return {
            "charged": amount, "minutes": minutes,
            "included": covered, "status": OPEN,
        }


def refund(meter_id: str, reason: str) -> int:
    """Return everything a meter took. Used when a run produced nothing."""
    row = store.one("SELECT * FROM meters WHERE id = ?", (meter_id,))
    if row is None:
        return 0
    amount = int(row["charged"])
    if amount <= 0:
        return 0
    credits.grant(row["user_id"], amount, reason=reason, ref=row["ref"])
    store.execute(
        "UPDATE meters SET refunded = ?, note = ? WHERE id = ?", (amount, reason, meter_id)
    )
    log.info("meter %s refunded %d credits (%s)", meter_id, amount, reason)
    return amount


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------


def sweep(now: float | None = None) -> dict[str, list[dict[str, Any]]]:
    """Charge every open meter and report the ones that must be stopped.

    Returns `{"stop": [...]}` - meters whose work has to end, either because
    the user ran out of credits or because whoever was running it stopped
    heartbeating. The caller terminates them, because only it knows how.
    """
    now = now or time.time()
    stop: list[dict[str, Any]] = []

    for row in store.rows_to_dicts(
        store.query("SELECT * FROM meters WHERE status = ?", (OPEN,))
    ):
        try:
            result = _charge(row["id"])
        except Exception:  # noqa: BLE001 - one bad meter must not stall the rest
            log.exception("could not charge meter %s", row["id"])
            continue

        if result["status"] == UNPAID:
            stop.append({**row, "why": UNPAID})
            continue

        if now - float(row["last_seen"]) > ABANDON_AFTER:
            # Whoever owned this is gone. Charge to here, close it, and hand it
            # back so the instance or process can be reclaimed - an orphaned
            # cloud box bills us for real money in real time.
            close_meter(row["id"], status=ORPHANED, detail="owner stopped reporting")
            stop.append({**row, "why": ORPHANED})

    return {"stop": stop}


def start_sweeper(interval: float = 30.0) -> None:
    """One sweep thread per worker. Every worker sweeping is the point: any of
    them can reclaim work whose owner died."""
    global _sweeper_started
    with _sweep_lock:
        if _sweeper_started:
            return
        _sweeper_started = True

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                actions = sweep()
                for meter in actions["stop"]:
                    _reclaim(meter)
            except Exception:  # noqa: BLE001 - the sweeper must never die
                log.exception("metering sweep failed")

    threading.Thread(target=loop, name="metering-sweep", daemon=True).start()


def _reclaim(meter: dict[str, Any]) -> None:
    """Stop the work behind a meter that can no longer be billed."""
    kind = meter.get("kind")
    if kind == "notebook":
        from twohelixes.notebooks import runner

        runner.reclaim(meter)
    elif meter.get("provider") not in ("local", "", None):
        from twohelixes.notebooks import compute

        try:
            compute.stop_remote(str(meter["provider"]), json.loads(meter.get("remote") or "{}"))
        except Exception:  # noqa: BLE001
            log.exception("could not reclaim remote %s", meter.get("ref"))


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def open_for(user_id: str) -> list[dict[str, Any]]:
    return store.rows_to_dicts(
        store.query(
            "SELECT * FROM meters WHERE user_id = ? AND status = ? ORDER BY started_at",
            (user_id, OPEN),
        )
    )


def usage(user_id: str, limit: int = 50) -> dict[str, Any]:
    """What the metered work has cost, in the shape a customer can audit.

    Deliberately more than a total: a per-minute charge that cannot be traced
    to a session, its start time and its minute count is one nobody can check.
    """
    rows = store.rows_to_dicts(
        store.query(
            "SELECT id, kind, ref, rate, provider, status, started_at, closed_at, "
            "charged, refunded, minutes FROM meters WHERE user_id = ? "
            "ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        )
    )
    running = [r for r in rows if r["status"] == OPEN]
    return {
        "open": running,
        "recent": rows,
        "credits_per_minute_open": sum(int(r["rate"]) for r in running),
        "charged_total": sum(int(r["charged"]) for r in rows),
        "refunded_total": sum(int(r["refunded"] or 0) for r in rows),
    }
