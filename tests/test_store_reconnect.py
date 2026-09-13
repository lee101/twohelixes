"""Regression coverage for long-lived workers across PostgreSQL restarts."""

from __future__ import annotations

import threading
from typing import Any

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
