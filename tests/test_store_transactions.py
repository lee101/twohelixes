"""SQLite has one writer, so a transaction left open stops the whole product.

This is the bug these tests exist for: signing in returned a 500 with
"database is locked" while a deep-research agent was running. The cause was not
in the sign-in path at all - a thread elsewhere held a write transaction across
something slow, and every other writer queued behind it until its busy timeout
ran out.

Two things had to be true for that to happen, and each has a test here:

* a write transaction could be opened without a guaranteed close, because the
  `BEGIN IMMEDIATE` / `COMMIT` pairs were written by hand; and
* the watchdog's `PyThreadState_SetAsyncExc` does **not** interrupt a blocking
  call, so a thread killed mid-transaction keeps the lock until whatever it is
  blocked on returns by itself.
"""

from __future__ import annotations

import ctypes
import sqlite3
import threading
import time
from typing import Any

import pytest

from twohelixes import store


REASON = "txn-test"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A clean slate, whichever backend is configured.

    On SQLite that is a fresh file. On Postgres the database is shared, so the
    rows this file writes are tagged and counted by tag - and the connection is
    closed at teardown, because replacing the thread-local without closing it
    leaves a Postgres connection holding its locks until the collector runs,
    which the *next* test sees as a statement timeout in unrelated DDL.
    """
    if not store.is_postgres():
        monkeypatch.setattr(store, "_db_path", lambda: str(tmp_path / "test.db"))
    store.close()
    monkeypatch.setattr(store, "_local", threading.local())
    monkeypatch.setattr(store, "_initialised", False)
    store.init()
    store.execute("DELETE FROM credit_ledger WHERE reason LIKE ?", (f"{REASON}%",))
    yield
    store.execute("DELETE FROM credit_ledger WHERE reason LIKE ?", (f"{REASON}%",))
    store.close()


def _count(reason: str) -> int:
    row = store.one(
        "SELECT COUNT(*) AS n FROM credit_ledger WHERE reason = ?", (reason,)
    )
    return int(row["n"])


def _write(value: str) -> None:
    store.execute(
        "INSERT INTO credit_ledger (id, user_id, delta, balance, reason, created_at) "
        "VALUES (?, 'u', 1, 1, ?, ?)",
        (store.new_id(), f"{REASON}-{value}", time.time()),
    )


def _in_a_thread(target: Any, timeout: float = 20.0) -> dict[str, Any]:
    """Run `target` on its own thread with its own connection."""
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            target()
            result["ok"] = True
        except BaseException as exc:  # noqa: BLE001 - the failure is the result
            result["error"] = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    result["alive"] = thread.is_alive()
    result["thread"] = thread
    return result


# --------------------------------------------------------------------------
# The guarantee
# --------------------------------------------------------------------------


def test_a_failed_transaction_rolls_back_and_releases_the_lock() -> None:
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO credit_ledger (id, user_id, delta, balance, reason, "
                "created_at) VALUES (?, 'u', 1, 1, ?, 0)",
                (store.new_id(), f"{REASON}-rolled-back"),
            )
            raise Boom

    assert not store._in_transaction(store.connection())
    assert _count(f"{REASON}-rolled-back") == 0
    # And the next writer is not waiting on anything.
    _write("after")
    assert _count(f"{REASON}-after") == 1


def test_a_transaction_killed_by_the_watchdog_still_releases_the_lock() -> None:
    """`Timeout` is raised into a running thread; it must not strand the lock.

    The sandbox watchdog interrupts a runaway with
    `PyThreadState_SetAsyncExc`. Before `store.transaction()` that could land
    between BEGIN and COMMIT and leave the write lock held for the life of the
    process.
    """

    class Timeout(Exception):
        pass

    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO credit_ledger (id, user_id, delta, balance, reason, "
                "created_at) VALUES (?, 'u', 1, 1, ?, 0)",
                (store.new_id(), f"{REASON}-held"),
            )
            entered.set()
            # Busy-wait rather than sleep: an async exception cannot interrupt
            # a blocking call, and this test is about the transaction, not
            # about that (which the next test covers).
            while not release.is_set():
                pass

    outcome = _in_a_thread(lambda: holder(), timeout=0.0)
    thread = outcome["thread"]
    assert entered.wait(5), "the holder never opened its transaction"

    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread.ident), ctypes.py_object(Timeout)
    )
    thread.join(10)
    release.set()
    assert not thread.is_alive(), "the killed thread never exited"

    # The point: another thread can write immediately, and the killed
    # transaction left nothing behind.
    started = time.perf_counter()
    writer = _in_a_thread(lambda: _write("signin"))
    waited_ms = (time.perf_counter() - started) * 1000

    assert writer.get("ok"), writer.get("error")
    assert waited_ms < 5000, f"the writer waited {waited_ms:.0f} ms for the lock"
    assert _count(f"{REASON}-held") == 0


def test_a_connection_left_in_a_transaction_is_rolled_back_when_reused() -> None:
    """The backstop for anything that still opens one by hand."""
    conn = store.connection()
    # The dialect `transaction()` would use, so this test says the same thing
    # on both backends.
    conn.execute("BEGIN" if store.is_postgres() else "BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO credit_ledger (id, user_id, delta, balance, reason, created_at) "
        "VALUES (?, 'u', 1, 1, ?, 0)",
        (store.new_id(), f"{REASON}-leaked"),
    )
    assert store._in_transaction(conn)

    # Asking for the connection again is the moment we can notice.
    assert not store._in_transaction(store.connection())
    assert _count(f"{REASON}-leaked") == 0


def test_a_slow_transaction_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """It cannot be prevented, so it has to be findable.

    The fix for a slow transaction is always "move the slow thing out of it",
    and that needs a stack rather than a mystery 500 in an unrelated endpoint.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="twohelixes.store"):
        with store.transaction():
            time.sleep(store.SLOW_TRANSACTION_SECONDS + 0.15)

    assert any("held for" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# The fact that made it so hard to see
# --------------------------------------------------------------------------


def test_an_async_exception_does_not_interrupt_a_blocking_call() -> None:
    """Documented here because it is the reason the log had a 64-second gap.

    `PyThreadState_SetAsyncExc` is checked between bytecodes. A thread inside
    `time.sleep`, a socket read, or a model call keeps running until that call
    returns - so "we interrupt runaway work" is only true of work that is
    executing Python.
    """

    class Boom(Exception):
        pass

    landed = threading.Event()

    def sleeper() -> None:
        try:
            time.sleep(1.5)
        except Boom:
            landed.set()

    thread = threading.Thread(target=sleeper, daemon=True)
    thread.start()
    time.sleep(0.1)

    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread.ident), ctypes.py_object(Boom)
    )
    # Well before the sleep would finish on its own.
    thread.join(0.4)
    assert thread.is_alive(), "if this ever fails, the watchdog got stronger"
    assert not landed.is_set()

    thread.join(5)
