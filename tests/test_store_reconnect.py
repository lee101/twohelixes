"""Regression coverage for long-lived workers across PostgreSQL restarts."""

from __future__ import annotations

import threading
from typing import Any
import pytest

from twohelixes import store


class _RawConnection:
    def __init__(self, closed: bool) -> None:
        self.closed = closed


def test_connection_replaces_a_closed_postgres_connection(monkeypatch: Any) -> None:
    old = _RawConnection(closed=True)
    replacement = _RawConnection(closed=False)
    local = threading.local()
    local.conn = store._Connection(old)

    monkeypatch.setattr(store, "_local", local)
    monkeypatch.setattr(store, "is_postgres", lambda: True)
    monkeypatch.setattr(store, "_connect_postgres", lambda: replacement)

    connected = store.connection()

    assert connected.raw is replacement
    assert local.conn is connected


@pytest.mark.parametrize("fail", [False, True])
def test_postgres_migrations_hold_and_release_process_lock(monkeypatch: Any, fail: bool) -> None:
    calls = []

    class Connection:
        def execute(self, sql: str) -> None:
            calls.append(sql)

        def executescript(self, sql: str) -> None:
            calls.append("schema")
            if fail:
                raise RuntimeError("migration failed")

    monkeypatch.setattr(store, "_initialised", False)
    monkeypatch.setattr(store, "is_postgres", lambda: True)
    monkeypatch.setattr(store, "connection", Connection)
    monkeypatch.setattr(store, "_add_missing_columns", lambda conn: calls.append("columns"))
    if fail:
        with pytest.raises(RuntimeError, match="migration failed"):
            store.init()
    else:
        store.init()
    assert calls[0] == "SELECT pg_advisory_lock(7474001)"
    assert calls[-1] == "SELECT pg_advisory_unlock(7474001)"
    assert store._initialised is (not fail)
