"""The machine catalogue: what a hosted session runs on, and what it costs.

Before this existed there was one number — `CREDIT_COST["notebook_minute"] = 1`
— charged identically whether the session was a local process, a €4/month
Hetzner box or an H100. A GPU minute sold at a cent is sold at a quarter of what
it costs, so the more the product was used the more it lost. Every price here is
derived from the provider's list price rather than chosen, and a test asserts
the derivation.

    sell_credits_per_minute = ceil(provider_cost_per_hour / 60 * 100 * MARGIN)

`MARGIN` is 2.0: the second copy of the cost covers what the provider bills us
for and the customer never sees — boot time before a session is usable, the
minutes between a crashed owner and the reclaim sweep, failed provisions, egress
— plus Stripe's cut of the credits that paid for it. It is not the profit; the
profit is what is left of it.

CPU classes are *included* in a subscription (the plan's machine-hours draw down
first). GPU classes never are: an included GPU hour costs more than the entire
Plus plan, so it cannot be bundled at any allowance a subscriber would notice.

RunPod costs were read from the provider's own API on 2026-07-28 (the run that
found L40S 15% above what this file used to claim, which had put it under the
margin floor). Hetzner costs are list prices of the same date, and every one can be overridden
from the environment (`TWOHELIXES_MACHINE_COST_<ID>`, US cents per hour) so a
price change is a config edit rather than a deploy. `scripts/check-machine-prices.py`
re-reads them from the provider APIs and reports drift — the number that must
never quietly go stale is the one everything else is derived from.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from twohelixes import config

log = logging.getLogger("twohelixes.machines")

PRICES_AS_OF = "2026-07-28"


class UnknownMachine(Exception):
    pass


class MachineNotAllowed(Exception):
    """The caller's plan may not launch this class."""


@dataclass(frozen=True)
class MachineClass:
    id: str
    label: str
    provider: str  # local | hetzner | runpod
    provider_ref: str  # hetzner server_type, or runpod gpuTypeId
    cost_cents_per_hour: float  # what the provider charges us
    vcpu: int = 0
    ram_gb: int = 0
    disk_gb: int = 0
    gpu: str = ""
    gpu_ram_gb: int = 0
    included: bool = False  # may draw on the plan's machine allowance
    min_plan: str = "plus"  # cheapest plan allowed to launch it
    max_session_minutes: int = 8 * 60
    idle_timeout_minutes: int = 30
    blurb: str = ""

    # ---- pricing ---------------------------------------------------

    @property
    def cost_per_hour(self) -> float:
        """Provider cost, after any environment override."""
        override = config.get(f"TWOHELIXES_MACHINE_COST_{self.id.upper().replace('-', '_')}")
        if override:
            try:
                return float(override)
            except ValueError:
                log.warning("bad cost override for %s: %r", self.id, override)
        return self.cost_cents_per_hour

    @property
    def credits_per_minute(self) -> int:
        """What we sell a minute for, in credits (1 credit == 1 US cent).

        Rounded *up*: rounding a fractional cent down on a class billed by the
        minute gives the margin away a minute at a time, and at these numbers
        the rounding is worth more than it looks — an H100 at 2x is 9.3c/min,
        and truncating to 9 is a 3% discount nobody asked for.
        """
        per_minute = self.cost_per_hour / 60.0 * config.MACHINE_MARGIN
        return max(1, math.ceil(per_minute - 1e-9))

    @property
    def sell_cents_per_hour(self) -> int:
        return self.credits_per_minute * 60

    @property
    def minimum_minutes(self) -> int:
        """Balance required to start one, in minutes of runtime.

        Longer for GPUs: the gap between "cannot pay any more" and "instance is
        actually gone" is a sweep interval, and on an H100 that gap is worth
        more than most users' entire balance.
        """
        return 15 if self.gpu else 5

    @property
    def minimum_credits(self) -> int:
        return self.credits_per_minute * self.minimum_minutes

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "provider": self.provider,
            "vcpu": self.vcpu,
            "ram_gb": self.ram_gb,
            "disk_gb": self.disk_gb,
            "gpu": self.gpu,
            "gpu_ram_gb": self.gpu_ram_gb,
            "included": self.included,
            "min_plan": self.min_plan,
            "credits_per_minute": self.credits_per_minute,
            "price_per_hour": f"${self.sell_cents_per_hour / 100:.2f}",
            "minimum_credits": self.minimum_credits,
            "max_session_minutes": self.max_session_minutes,
            "idle_timeout_minutes": self.idle_timeout_minutes,
            "blurb": self.blurb,
        }


# --------------------------------------------------------------------------
# The catalogue.
#
# Hetzner bills by the hour with a monthly cap, so the hourly figures are the
# capped monthly price / 730 where that is cheaper — which it is for anything
# left running, and anything left running is the case that costs us.
#
# RunPod costs are Secure Cloud on-demand, not Community Cloud: Community is
# cheaper and can evict a pod mid-session, and a session that vanishes halfway
# through is worth less than the few cents it saves.
# --------------------------------------------------------------------------

CATALOG: tuple[MachineClass, ...] = (
    MachineClass(
        id="local",
        label="Shared",
        provider="local",
        provider_ref="",
        cost_cents_per_hour=1.0,  # our own box: power and the share of it held
        vcpu=2, ram_gb=4,
        included=True,
        min_plan="plus",
        max_session_minutes=4 * 60,
        blurb="Runs on our servers. Best for small frames and quick edits.",
    ),
    MachineClass(
        id="cpu-small",
        label="CPU Small",
        provider="hetzner",
        provider_ref="cpx21",
        cost_cents_per_hour=1.3,  # CPX21 €7.55/mo ≈ $8.60 / 730h
        vcpu=3, ram_gb=4, disk_gb=80,
        included=True,
        min_plan="plus",
        blurb="3 vCPU, 4 GB. Enough for most pandas work.",
    ),
    MachineClass(
        id="cpu-medium",
        label="CPU Medium",
        provider="hetzner",
        provider_ref="cpx41",
        cost_cents_per_hour=4.0,  # CPX41 €26.85/mo ≈ $29 / 730h
        vcpu=8, ram_gb=16, disk_gb=240,
        included=True,
        min_plan="plus",
        blurb="8 vCPU, 16 GB. Frames that stopped fitting comfortably.",
    ),
    MachineClass(
        id="cpu-large",
        label="CPU Large",
        provider="hetzner",
        provider_ref="cpx51",
        cost_cents_per_hour=8.0,  # CPX51 €54.90/mo ≈ $59 / 730h
        vcpu=16, ram_gb=32, disk_gb=360,
        included=True,
        min_plan="pro",
        blurb="16 vCPU, 32 GB. Wide joins and long transforms.",
    ),
    MachineClass(
        id="cpu-dedicated",
        label="CPU Dedicated",
        provider="hetzner",
        provider_ref="ccx33",
        cost_cents_per_hour=17.0,  # CCX33 €109/mo ≈ $118 / 730h
        vcpu=8, ram_gb=32, disk_gb=240,
        included=False,  # dedicated vCPU costs 13x the shared class
        min_plan="pro",
        blurb="8 dedicated vCPU, 32 GB. No noisy neighbours; steady timings.",
    ),
    MachineClass(
        id="gpu-a5000",
        label="GPU RTX A5000",
        provider="runpod",
        provider_ref="NVIDIA RTX A5000",
        cost_cents_per_hour=27.0,
        vcpu=8, ram_gb=32,
        gpu="NVIDIA RTX A5000", gpu_ram_gb=24,
        included=False,
        min_plan="plus",
        max_session_minutes=6 * 60,
        idle_timeout_minutes=15,
        blurb="24 GB. The cheapest card here; fine for kernels and small models.",
    ),
    MachineClass(
        id="gpu-l4",
        label="GPU L4",
        provider="runpod",
        provider_ref="NVIDIA L4",
        cost_cents_per_hour=39.0,
        vcpu=8, ram_gb=32,
        gpu="NVIDIA L4", gpu_ram_gb=24,
        included=False,
        min_plan="plus",
        max_session_minutes=6 * 60,
        idle_timeout_minutes=15,
        blurb="24 GB, low power, always available.",
    ),
    MachineClass(
        id="gpu-4090",
        label="GPU RTX 4090",
        provider="runpod",
        provider_ref="NVIDIA GeForce RTX 4090",
        cost_cents_per_hour=69.0,
        vcpu=8, ram_gb=32,
        gpu="NVIDIA RTX 4090", gpu_ram_gb=24,
        included=False,
        min_plan="plus",
        max_session_minutes=6 * 60,
        idle_timeout_minutes=15,
        blurb="24 GB, fastest consumer card. The sane GPU default.",
    ),
    MachineClass(
        id="gpu-5090",
        label="GPU RTX 5090",
        provider="runpod",
        provider_ref="NVIDIA GeForce RTX 5090",
        cost_cents_per_hour=99.0,
        vcpu=12, ram_gb=48,
        gpu="NVIDIA RTX 5090", gpu_ram_gb=32,
        included=False,
        min_plan="plus",
        max_session_minutes=6 * 60,
        idle_timeout_minutes=15,
        blurb="32 GB Blackwell. Same card our own kernels are tuned on.",
    ),
    MachineClass(
        id="gpu-l40s",
        label="GPU L40S",
        provider="runpod",
        provider_ref="NVIDIA L40S",
        cost_cents_per_hour=99.0,
        vcpu=16, ram_gb=62,
        gpu="NVIDIA L40S", gpu_ram_gb=48,
        included=False,
        min_plan="pro",
        max_session_minutes=6 * 60,
        idle_timeout_minutes=15,
        blurb="48 GB. Bigger models than a 4090 will hold.",
    ),
    MachineClass(
        id="gpu-a100",
        label="GPU A100 80GB",
        provider="runpod",
        provider_ref="NVIDIA A100 80GB PCIe",
        cost_cents_per_hour=139.0,
        vcpu=16, ram_gb=125,
        gpu="NVIDIA A100", gpu_ram_gb=80,
        included=False,
        min_plan="pro",
        max_session_minutes=4 * 60,
        idle_timeout_minutes=10,
        blurb="80 GB, NVLink-class memory bandwidth.",
    ),
    MachineClass(
        id="gpu-mi300x",
        label="GPU MI300X",
        provider="runpod",
        provider_ref="AMD Instinct MI300X OAM",
        cost_cents_per_hour=239.0,
        vcpu=24, ram_gb=250,
        gpu="AMD Instinct MI300X", gpu_ram_gb=192,
        included=False,
        min_plan="pro",
        max_session_minutes=4 * 60,
        idle_timeout_minutes=10,
        blurb="192 GB. The most memory per dollar we can rent.",
    ),
    MachineClass(
        id="gpu-h100",
        label="GPU H100 80GB",
        provider="runpod",
        provider_ref="NVIDIA H100 80GB HBM3",
        cost_cents_per_hour=299.0,
        vcpu=20, ram_gb=250,
        gpu="NVIDIA H100", gpu_ram_gb=80,
        included=False,
        min_plan="pro",
        max_session_minutes=4 * 60,
        idle_timeout_minutes=10,
        blurb="80 GB HBM3.",
    ),
    MachineClass(
        id="gpu-h200",
        label="GPU H200",
        provider="runpod",
        provider_ref="NVIDIA H200",
        cost_cents_per_hour=439.0,
        vcpu=24, ram_gb=250,
        gpu="NVIDIA H200", gpu_ram_gb=141,
        included=False,
        min_plan="pro",
        max_session_minutes=4 * 60,
        idle_timeout_minutes=10,
        blurb="141 GB HBM3e.",
    ),
    MachineClass(
        id="gpu-b200",
        label="GPU B200",
        provider="runpod",
        provider_ref="NVIDIA B200",
        cost_cents_per_hour=589.0,
        vcpu=28, ram_gb=283,
        gpu="NVIDIA B200", gpu_ram_gb=180,
        included=False,
        min_plan="pro",
        max_session_minutes=4 * 60,
        idle_timeout_minutes=10,
        blurb="180 GB Blackwell. Fastest thing we rent.",
    ),
)

BY_ID = {m.id: m for m in CATALOG}
DEFAULT_ID = "local"

# Plan order, for `min_plan` checks. Anything not listed sorts below free.
PLAN_RANK = {"": 0, "anonymous": 0, "free": 1, "plus": 2, "pro": 3, "team": 4}


def get(machine_id: str | None) -> MachineClass:
    mid = (machine_id or DEFAULT_ID).strip()
    if mid not in BY_ID:
        raise UnknownMachine(f"unknown machine class {mid!r}")
    return BY_ID[mid]


def enabled_ids() -> set[str]:
    """Classes this deployment will actually launch.

    A class whose provider has no key configured is listed but not startable,
    and `TWOHELIXES_MACHINES` narrows it further — a box with a Hetzner token
    and no intention of renting H100s should not offer them.
    """
    allowed = config.get("TWOHELIXES_MACHINES", "")
    ids = {m.strip() for m in allowed.split(",") if m.strip()} if allowed else set(BY_ID)
    have = {
        "local": True,
        "hetzner": bool(config.get("HETZNER_API_TOKEN")),
        "runpod": bool(config.get("RUNPOD_API_KEY")),
    }
    return {mid for mid in ids if mid in BY_ID and have.get(BY_ID[mid].provider, False)}


def catalog_for(plan: str = "", *, include_unavailable: bool = True) -> list[dict[str, Any]]:
    """The catalogue as the pricing page and the machine picker need it."""
    rank = PLAN_RANK.get(plan or "", 0)
    live = enabled_ids()
    out = []
    for m in CATALOG:
        if not include_unavailable and m.id not in live:
            continue
        row = m.as_dict()
        row["available"] = m.id in live
        row["allowed"] = rank >= PLAN_RANK.get(m.min_plan, 99)
        out.append(row)
    return out


def check_allowed(plan: str, machine: MachineClass) -> None:
    if machine.id not in enabled_ids():
        raise MachineNotAllowed(f"{machine.label} is not available on this deployment")
    if PLAN_RANK.get(plan or "", 0) < PLAN_RANK.get(machine.min_plan, 99):
        raise MachineNotAllowed(
            f"{machine.label} needs the {machine.min_plan.title()} plan or higher"
        )


def draws_on_allowance(machine: MachineClass) -> bool:
    """Whether a plan's included machine minutes cover this class.

    Only the CPU classes. The included hours are sized against `cpu-small`; a
    GPU minute is 50x that, so letting one draw on the same pool would empty a
    month's allowance in twelve minutes and hand out $2 of compute for free.
    """
    return machine.included


def rate_for(machine: MachineClass) -> int:
    return machine.credits_per_minute


# --------------------------------------------------------------------------
# Cost reporting — what the plans actually cost us at full use
# --------------------------------------------------------------------------


def plan_machine_cost_cents(plan_id: str) -> float:
    """Provider cost of a plan's included machine hours, if all of them ran.

    The number the pricing has to survive: a plan whose included compute costs
    more than the plan is priced at is a plan that loses money on its best
    customers.
    """
    allowance = config.PLAN_ALLOWANCES.get(plan_id, {})
    minutes = int(allowance.get("notebook_minute", 0))
    included = get(config.MACHINE_INCLUDED_CLASS)
    return minutes / 60.0 * included.cost_per_hour


# --------------------------------------------------------------------------
# Orphan reaping — the guard that actually stops us losing money
# --------------------------------------------------------------------------

# An instance younger than this is left alone: it may be mid-provision, with
# its meter row not yet carrying the remote handle.
ORPHAN_GRACE_SECONDS = 10 * 60


def _open_remote_handles() -> set[str]:
    from twohelixes import metering, store

    handles: set[str] = set()
    for row in store.rows_to_dicts(
        store.query("SELECT remote FROM meters WHERE status = ?", (metering.OPEN,))
    ):
        try:
            remote = json.loads(row.get("remote") or "{}")
        except (TypeError, ValueError):
            continue
        for key in ("id", "server_id", "pod_id"):
            if remote.get(key):
                handles.add(str(remote[key]))
    return handles


def reap_orphans(*, dry_run: bool = False, now: float | None = None) -> dict[str, Any]:
    """Destroy provider instances no open meter is paying for.

    The sweep in `metering` reclaims instances it knows about. This asks the
    *provider* what is running and kills anything that answers which we are not
    billing anyone for — the case the meter-side sweep structurally cannot see,
    because it happens when the meter row was never written: a provision that
    succeeded after we gave up waiting, a worker killed between the API call and
    the database write, a manual test someone forgot.

    Without this, one lost H100 costs $67 a day, silently, forever.
    """
    from twohelixes.notebooks import compute

    now = now or time.time()
    known = _open_remote_handles()
    killed: list[dict[str, Any]] = []
    spared: list[dict[str, Any]] = []
    errors: list[str] = []

    for provider, lister, killer in (
        ("hetzner", compute.hetzner_list, compute.hetzner_delete),
        ("runpod", compute.runpod_list, compute.runpod_delete),
    ):
        if not enabled_ids() & {m.id for m in CATALOG if m.provider == provider}:
            continue
        try:
            instances = lister()
        except Exception as exc:  # noqa: BLE001 - one provider must not stop the other
            errors.append(f"{provider}: {exc}")
            continue

        for inst in instances:
            handle = str(inst.get("id", ""))
            age = now - float(inst.get("created_at") or now)
            if handle in known:
                spared.append({"provider": provider, "id": handle, "why": "metered"})
                continue
            if age < ORPHAN_GRACE_SECONDS:
                spared.append({"provider": provider, "id": handle, "why": "too new"})
                continue
            entry = {"provider": provider, "id": handle, "age_seconds": round(age)}
            if dry_run:
                killed.append({**entry, "dry_run": True})
                continue
            try:
                killer(handle)
                killed.append(entry)
                log.warning("reaped orphan %s instance %s (age %.0fs)", provider, handle, age)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider} {handle}: {exc}")

    return {"killed": killed, "spared": spared, "errors": errors}


@dataclass
class _ReaperState:
    started: bool = False
    lock: Any = field(default_factory=lambda: __import__("threading").Lock())


_reaper = _ReaperState()


def start_orphan_reaper(interval: float = 900.0) -> None:
    """One reaper per process; harmless if several run, since it is idempotent."""
    import threading

    with _reaper.lock:
        if _reaper.started:
            return
        _reaper.started = True

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                result = reap_orphans()
                if result["killed"]:
                    log.warning("orphan reaper killed %d instance(s)", len(result["killed"]))
            except Exception:  # noqa: BLE001 - the reaper must never die
                log.exception("orphan reap failed")

    threading.Thread(target=loop, name="machine-reaper", daemon=True).start()
