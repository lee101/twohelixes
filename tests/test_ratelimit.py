"""Admission limits are reservations, not post-hoc accounting."""

from __future__ import annotations

from pathlib import Path

import pytest

from twohelixes import auth, config, ratelimit, store


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    store.init()
    yield
    store.close()
    store._initialised = False


def test_free_plan_is_exactly_ten_generations() -> None:
    assert config.PLAN_ALLOWANCES["free"]["chat_query"] == 10


def test_anonymous_trial_is_reserved_before_work_starts() -> None:
    visitor = auth.Identity(ip="203.0.113.10")

    first = ratelimit.check(visitor, "chat_query", sample_data=True)
    simultaneous = ratelimit.check(visitor, "chat_query", sample_data=True)

    assert first.allowed
    assert not simultaneous.allowed
    assert simultaneous.reason == "trial_used"


def test_free_burst_is_reserved_before_work_starts() -> None:
    user_id = store.new_id()
    store.execute(
        "INSERT INTO users (id, email, plan, created_at, plan_usage) "
        "VALUES (?, ?, 'free', 0, ?)",
        (user_id, "rate@test.local", store.dump_json({})),
    )
    identity = auth.Identity(user_id=user_id, plan="free")

    decisions = [ratelimit.check(identity, "chat_query") for _ in range(4)]

    assert [decision.allowed for decision in decisions] == [True, True, True, False]
    assert decisions[-1].reason == "rate_limited"
