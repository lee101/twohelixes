"""The machine rate card.

These are the tests that decide whether hosted compute makes money or loses it.
The product's failure mode is not a wrong chart, it is a GPU sold for a cent a
minute while it costs us four - which is what the code did before this module
existed - so every price is asserted against the provider cost it was derived
from, and the guards that stop an unbilled instance running forever are
asserted as behaviours rather than as documentation.
"""

from __future__ import annotations

import json
import time

import pytest

from twohelixes import config, credits, machines, metering, store


def _make_user(plan: str = "pro", balance: int = 10_000) -> str:
    store.init()
    user_id = store.new_id()
    store.execute(
        "INSERT INTO users (id, email, plan, api_credits, free_queries_used, created_at, "
        "plan_period_start, plan_usage) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, f"{user_id}@example.com", plan, balance, 0, time.time(),
            time.time(), store.dump_json({}),
        ),
    )
    return user_id


# --- pricing --------------------------------------------------------------

def test_every_class_sells_above_provider_cost_times_margin():
    for m in machines.CATALOG:
        sold = m.credits_per_minute * 60.0
        cost = m.cost_per_hour
        assert sold >= cost * config.MACHINE_MARGIN - 1e-9, (
            f"{m.id} sells for {sold:.2f}c/h against {cost:.2f}c/h cost"
        )


def test_gpu_classes_are_priced_well_clear_of_cost():
    """A GPU minute is where a rounding mistake costs real money."""
    for m in machines.CATALOG:
        if not m.gpu:
            continue
        assert m.credits_per_minute * 60 >= m.cost_per_hour * 2


def test_price_rounds_up_never_down():
    cls = machines.MachineClass(
        id="probe", label="Probe", provider="runpod", provider_ref="x",
        cost_cents_per_hour=100.0,  # 2x -> 3.33c/min
    )
    assert cls.credits_per_minute == 4


def test_price_never_rounds_to_zero():
    cls = machines.MachineClass(
        id="probe", label="Probe", provider="hetzner", provider_ref="x",
        cost_cents_per_hour=0.01,
    )
    assert cls.credits_per_minute == 1


def test_cost_can_be_overridden_from_the_environment(monkeypatch):
    """A provider price rise must be a config change, not a deploy."""
    monkeypatch.setenv("TWOHELIXES_MACHINE_COST_GPU_4090", "120")
    config.get.cache_clear() if hasattr(config.get, "cache_clear") else None
    m = machines.get("gpu-4090")
    assert m.cost_per_hour == 120.0
    assert m.credits_per_minute == 4


def test_gpu_needs_more_runway_than_cpu():
    assert machines.get("gpu-h100").minimum_minutes > machines.get("cpu-small").minimum_minutes
    assert machines.get("gpu-h100").minimum_credits > 100


# --- plans ----------------------------------------------------------------

def test_included_machine_hours_cost_a_fraction_of_the_plan():
    """Included compute has to be cheap enough that a heavy user is still profitable."""
    for plan in config.PAID_PLANS:
        price = config.PLAN_ALLOWANCES[plan]["price_cents"]
        cost = machines.plan_machine_cost_cents(plan)
        assert cost < price * 0.10, f"{plan}: included machines cost {cost:.0f}c of {price}c"


def test_free_plan_includes_no_machine_time():
    assert config.PLAN_ALLOWANCES["free"]["notebook_minute"] == 0


def test_included_class_is_actually_an_included_class():
    assert machines.get(config.MACHINE_INCLUDED_CLASS).included


def test_no_gpu_class_is_included_in_a_plan():
    for m in machines.CATALOG:
        if m.gpu:
            assert not m.included, f"{m.id} would be given away with a subscription"


# --- plan gating ----------------------------------------------------------

def test_plan_rank_gates_bigger_machines(monkeypatch):
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("HETZNER_API_TOKEN", "test")
    machines.check_allowed("plus", machines.get("gpu-4090"))
    with pytest.raises(machines.MachineNotAllowed):
        machines.check_allowed("plus", machines.get("gpu-h100"))
    with pytest.raises(machines.MachineNotAllowed):
        machines.check_allowed("free", machines.get("cpu-small"))


def test_unconfigured_provider_is_listed_but_not_startable(monkeypatch):
    # This box has real keys in a shared dotenv, so "not configured" has to be
    # made true rather than assumed.
    monkeypatch.setattr(config, "_dotenv", dict)
    monkeypatch.delenv("RUNPOD_API_KEY", raising=False)
    monkeypatch.setenv("TWOHELIXES_MACHINES", "cpu-small,gpu-4090")
    monkeypatch.setenv("HETZNER_API_TOKEN", "test")
    rows = {r["id"]: r for r in machines.catalog_for("pro")}
    assert rows["cpu-small"]["available"] is True
    assert rows["gpu-4090"]["available"] is False
    with pytest.raises(machines.MachineNotAllowed):
        machines.check_allowed("pro", machines.get("gpu-4090"))


def test_deployment_allowlist_narrows_the_catalogue(monkeypatch):
    monkeypatch.setenv("HETZNER_API_TOKEN", "test")
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("TWOHELIXES_MACHINES", "cpu-small")
    assert machines.enabled_ids() == {"cpu-small"}


def test_unknown_machine_is_rejected():
    with pytest.raises(machines.UnknownMachine):
        machines.get("gpu-moon")


# --- metering integration -------------------------------------------------

def test_cpu_minutes_come_out_of_the_plan_allowance():
    user = _make_user("pro", balance=1000)
    meter = metering.open_meter(
        user, "notebook", "s-cpu", machines.get("cpu-small").credits_per_minute,
        provider="hetzner", detail={"machine": "cpu-small"},
    )
    store.execute(
        "UPDATE meters SET paid_through = ? WHERE id = ?",
        (time.time() - 300, meter["id"]),
    )
    result = metering._charge(meter["id"])
    assert result["included"] == 5
    assert result["charged"] == 0
    assert credits.balance(user) == 1000


def test_gpu_minutes_never_touch_the_allowance():
    user = _make_user("pro", balance=1000)
    gpu = machines.get("gpu-4090")
    meter = metering.open_meter(
        user, "notebook", "s-gpu", gpu.credits_per_minute,
        provider="runpod", detail={"machine": "gpu-4090"},
    )
    store.execute(
        "UPDATE meters SET paid_through = ? WHERE id = ?",
        (time.time() - 300, meter["id"]),
    )
    result = metering._charge(meter["id"])
    assert result.get("included", 0) == 0
    assert result["charged"] == 5 * gpu.credits_per_minute
    assert credits.balance(user) == 1000 - 5 * gpu.credits_per_minute


def test_starting_a_gpu_needs_runway_not_one_minute():
    gpu = machines.get("gpu-h100")
    user = _make_user("pro", balance=gpu.credits_per_minute * 3)
    with pytest.raises(credits.InsufficientCredits):
        metering.open_meter(
            user, "notebook", "s1", gpu.credits_per_minute,
            provider="runpod", detail={"machine": "gpu-h100"},
            minimum_minutes=gpu.minimum_minutes,
        )


# --- orphan reaping -------------------------------------------------------

class _FakeProvider:
    """Stands in for a provider API in the reaper tests."""

    def __init__(self, instances):
        self.instances = list(instances)
        self.deleted: list[str] = []

    def list(self):
        return self.instances

    def delete(self, identifier):
        self.deleted.append(identifier)


@pytest.fixture()
def reaper(monkeypatch):
    from twohelixes.notebooks import compute

    monkeypatch.setenv("HETZNER_API_TOKEN", "test")
    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    monkeypatch.setenv("TWOHELIXES_MACHINES", "cpu-small,gpu-4090")

    hetzner = _FakeProvider([])
    runpod = _FakeProvider([])
    monkeypatch.setattr(compute, "hetzner_list", hetzner.list)
    monkeypatch.setattr(compute, "hetzner_delete", hetzner.delete)
    monkeypatch.setattr(compute, "runpod_list", runpod.list)
    monkeypatch.setattr(compute, "runpod_delete", runpod.delete)
    return hetzner, runpod


def test_reaper_kills_an_instance_no_meter_is_paying_for(reaper):
    hetzner, runpod = reaper
    store.init()
    old = time.time() - 3600
    runpod.instances = [{"id": "pod-lost", "created_at": old}]
    result = machines.reap_orphans()
    assert runpod.deleted == ["pod-lost"]
    assert [k["id"] for k in result["killed"]] == ["pod-lost"]


def test_reaper_spares_an_instance_with_an_open_meter(reaper):
    hetzner, runpod = reaper
    user = _make_user("pro")
    metering.open_meter(
        user, "notebook", "s1", 3, provider="runpod",
        remote={"id": "pod-live"}, detail={"machine": "gpu-4090"},
    )
    runpod.instances = [{"id": "pod-live", "created_at": time.time() - 3600}]
    result = machines.reap_orphans()
    assert runpod.deleted == []
    assert result["spared"][0]["why"] == "metered"


def test_reaper_spares_an_instance_that_is_still_provisioning(reaper):
    hetzner, runpod = reaper
    store.init()
    runpod.instances = [{"id": "pod-new", "created_at": time.time() - 30}]
    result = machines.reap_orphans()
    assert runpod.deleted == []
    assert result["spared"][0]["why"] == "too new"


def test_reaper_dry_run_kills_nothing(reaper):
    hetzner, runpod = reaper
    store.init()
    runpod.instances = [{"id": "pod-lost", "created_at": time.time() - 3600}]
    result = machines.reap_orphans(dry_run=True)
    assert runpod.deleted == []
    assert result["killed"][0]["dry_run"] is True


def test_one_broken_provider_does_not_stop_the_other(reaper):
    hetzner, runpod = reaper
    store.init()

    def boom():
        raise RuntimeError("hetzner is down")

    from twohelixes.notebooks import compute

    compute.hetzner_list = boom
    runpod.instances = [{"id": "pod-lost", "created_at": time.time() - 3600}]
    result = machines.reap_orphans()
    assert runpod.deleted == ["pod-lost"]
    assert any("hetzner" in e for e in result["errors"])


def test_meter_remote_handles_are_read_from_every_open_meter():
    store.init()
    store.execute("DELETE FROM meters")
    user = _make_user("pro")
    metering.open_meter(user, "notebook", "s1", 3, provider="runpod", remote={"id": "a"})
    metering.open_meter(user, "notebook", "s2", 3, provider="hetzner", remote={"server_id": "b"})
    assert machines._open_remote_handles() == {"a", "b"}


def test_catalog_is_json_serialisable():
    json.dumps(machines.catalog_for("pro"))


def test_reaper_never_touches_another_products_pods(monkeypatch):
    """The RunPod account is shared with codex-infinity-site.

    Their pods are not named `th-nb-*`, and destroying one would be a far worse
    failure than the leak this reaper exists to stop.
    """
    from twohelixes.notebooks import compute

    monkeypatch.setenv("RUNPOD_API_KEY", "test")
    old = time.time() - 86400
    # The real lister filters by name; this asserts the filter, not the caller.
    pods = [
        {"id": "pod-theirs", "name": "codex-infinity-worker-3", "desiredStatus": "RUNNING",
         "createdAt": old},
        {"id": "pod-ours", "name": "th-nb-abc123", "desiredStatus": "RUNNING", "createdAt": old},
    ]

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return pods

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **kw):
            return _Response()

    monkeypatch.setattr(compute, "_client", _Client)
    assert [p["id"] for p in compute.runpod_list()] == ["pod-ours"]
    assert {p["id"] for p in compute.runpod_list(ours_only=False)} == {"pod-ours", "pod-theirs"}
