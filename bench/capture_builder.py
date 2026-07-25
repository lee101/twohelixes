"""Capture the chart builder for visual review.

Separate from the main visualbench sweep because it drives a real interaction
sequence - open the builder, apply a plan, disable a step - rather than just
loading a page. A screenshot of an empty builder proves very little.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "visualbench"

# A plan with one human step among the agent's, so the provenance markers are
# visible in the capture.
PLAN = [
    {
        "type": "filter",
        "params": {"column": "refunded", "op": "eq", "value": False},
        "note": "refunds are not revenue",
        "author": "agent",
    },
    {
        "type": "resample",
        "params": {
            "time_column": "order_date",
            "grain": "month",
            "by": ["region"],
            "metrics": [{"column": "net_amount", "agg": "sum", "as": "revenue"}],
        },
        "note": "monthly totals per region",
        "author": "agent",
    },
    {
        "type": "sort",
        "params": {"column": "order_date", "desc": False},
        "author": "human",
    },
]

SHOTS = [
    ("light", "desktop", 1560, 1000, False),
    ("dark", "desktop", 1560, 1000, False),
    ("light", "mobile", 390, 844, True),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7474")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    failures = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

        for theme, name, width, height, mobile in SHOTS:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                color_scheme=theme,
                device_scale_factor=2 if mobile else 1,
                is_mobile=mobile,
                has_touch=mobile,
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "console",
                lambda m: errors.append(m.text) if m.type == "error" else None,
            )

            try:
                page.goto(f"{args.base}/app", wait_until="networkidle", timeout=30000)
                # A fresh identity per capture: the free tier is a lifetime
                # allowance and the bench must not consume a real account's.
                stamp = int(time.time() * 1000)
                page.fill("input.signin-input", f"bench+{theme}-{name}-{stamp}@twohelixes.com")
                page.click("button[type=submit]")
                page.wait_for_selector("textarea.ask-input", timeout=15000)

                page.evaluate("() => window.__thBuilder({ sample: 'orders' })")
                page.wait_for_selector(".builder", timeout=25000)
                page.wait_for_selector(".chart-plot.js-plotly-plot", timeout=40000)

                page.evaluate("(plan) => window.__thSetPlan(plan)", PLAN)
                page.wait_for_timeout(2500)

                shot = OUT / f"builder-{theme}-{name}.png"
                page.screenshot(path=str(shot), full_page=True)

                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth"
                    " - document.documentElement.clientWidth"
                )
                steps = page.eval_on_selector_all(".step", "nodes => nodes.length")

                status = "ok"
                if overflow > 1 or errors or steps != len(PLAN):
                    status = "PROBLEM"
                    failures += 1

                print(
                    f"{theme:6s} {name:8s} steps={steps} overflow={overflow} "
                    f"errors={len(errors)} {status} -> {shot.name}"
                )
                for message in errors[:3]:
                    print(f"    {message[:140]}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"{theme:6s} {name:8s} FAILED: {str(exc)[:160]}")
            finally:
                context.close()

        browser.close()

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
