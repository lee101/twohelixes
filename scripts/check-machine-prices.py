#!/usr/bin/env python3
"""Compare the machine catalogue against what the providers charge today.

Every sell price in `machines.py` is derived from a provider list price that was
copied down by hand on a date. Provider prices move; the derivation does not
notice. This asks Hetzner and RunPod what they charge now and reports the drift,
so the number everything else is computed from cannot quietly go stale.

    ./.venv-13/bin/python scripts/check-machine-prices.py
    ./.venv-13/bin/python scripts/check-machine-prices.py --fail-over 10

Exit code 1 when any class costs more than the tolerance above what the
catalogue believes — which is the direction that loses money — so it can run in
CI or a cron and complain on its own.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "interp"))

from twohelixes import config, machines  # noqa: E402

USD_PER_EUR = float(os.environ.get("USD_PER_EUR", "1.09"))
HOURS_PER_MONTH = 730.0


def hetzner_prices() -> dict[str, float]:
    """`server_type -> cents per hour`, taking the cheaper of hourly and capped.

    Hetzner bills hourly up to a monthly cap, so anything left running costs the
    cap — and anything left running is the case that decides whether this makes
    money.
    """
    import httpx

    token = config.get("HETZNER_API_TOKEN")
    if not token:
        return {}
    out: dict[str, float] = {}
    with httpx.Client(timeout=30) as client:
        response = client.get(
            "https://api.hetzner.cloud/v1/server_types",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        for st in response.json().get("server_types", []):
            for price in st.get("prices", []):
                hourly = float(price.get("price_hourly", {}).get("gross", 0)) * 100 * USD_PER_EUR
                monthly = float(price.get("price_monthly", {}).get("gross", 0)) * 100 * USD_PER_EUR
                capped = monthly / HOURS_PER_MONTH if monthly else hourly
                effective = min(hourly, capped) if hourly else capped
                if effective:
                    out[st["name"]] = min(out.get(st["name"], effective), effective)
    return out


RUNPOD_GRAPHQL = "https://api.runpod.io/graphql"
GPU_TYPES_QUERY = """
query { gpuTypes { id displayName memoryInGb secureCloud securePrice communityPrice } }
"""


def runpod_prices() -> dict[str, float]:
    """`gpuTypeId -> cents per hour` for Secure Cloud on demand.

    The REST API (`rest.runpod.io`) has no GPU catalogue - only pods, endpoints
    and billing - so pricing comes from the GraphQL API. `securePrice`, not
    `communityPrice`: Community pods are cheaper and can be evicted mid-session,
    and a session that vanishes halfway through is worth less than it saves.
    """
    import httpx

    key = config.get("RUNPOD_API_KEY")
    if not key:
        return {}
    with httpx.Client(timeout=30) as client:
        response = client.post(
            RUNPOD_GRAPHQL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": GPU_TYPES_QUERY},
        )
        response.raise_for_status()
        payload = response.json()
    out: dict[str, float] = {}
    for row in payload.get("data", {}).get("gpuTypes", []) or []:
        price = row.get("securePrice")
        if not price:
            continue
        # Keyed by id, because that is what `provider_ref` provisions with; the
        # display name is a label and has changed under us before.
        out[str(row.get("id", ""))] = float(price) * 100
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fail-over", type=float, default=10.0,
                    help="percent a provider price may rise before this exits non-zero")
    args = ap.parse_args()

    try:
        hetzner = hetzner_prices()
    except Exception as exc:  # noqa: BLE001
        print(f"hetzner: {exc}", file=sys.stderr)
        hetzner = {}
    try:
        runpod = runpod_prices()
    except Exception as exc:  # noqa: BLE001
        print(f"runpod: {exc}", file=sys.stderr)
        runpod = {}

    print(f"catalogue prices as of {machines.PRICES_AS_OF}, margin {config.MACHINE_MARGIN}x")
    print(f"{'class':<15}{'catalogue':>11}{'live':>11}{'drift':>9}  {'sell/h':>8}  {'margin':>7}")
    print("-" * 68)

    worst = 0.0
    unchecked = []
    for m in machines.CATALOG:
        live = {"hetzner": hetzner, "runpod": runpod}.get(m.provider, {}).get(m.provider_ref)
        sell = m.sell_cents_per_hour
        if live is None:
            unchecked.append(m.id)
            print(f"{m.id:<15}{m.cost_per_hour:>10.2f}c{'-':>11}{'-':>9}  {sell / 100:>7.2f}$"
                  f"  {sell / max(m.cost_per_hour, 0.01):>6.1f}x")
            continue
        drift = (live - m.cost_per_hour) / max(m.cost_per_hour, 0.01) * 100
        worst = max(worst, drift)
        flag = "  <-- costs more than we think" if drift > args.fail_over else ""
        print(f"{m.id:<15}{m.cost_per_hour:>10.2f}c{live:>10.2f}c{drift:>8.1f}%"
              f"  {sell / 100:>7.2f}$  {sell / max(live, 0.01):>6.1f}x{flag}")

    if unchecked:
        print(f"\nnot checked (provider key missing or name not matched): {', '.join(unchecked)}")

    print("\nincluded machine time, at cost, if a subscriber used all of it:")
    for plan in config.PAID_PLANS:
        price = config.PLAN_ALLOWANCES[plan]["price_cents"]
        cost = machines.plan_machine_cost_cents(plan)
        print(f"  {plan:<6} ${price / 100:>6.0f}/mo   "
              f"{config.PLAN_ALLOWANCES[plan]['notebook_minute'] // 60:>4}h   "
              f"costs ${cost / 100:.2f} ({cost / max(price, 1) * 100:.1f}% of the plan)")

    if worst > args.fail_over:
        print(f"\nFAIL: a provider price rose {worst:.1f}%, over the {args.fail_over:.0f}% tolerance",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
