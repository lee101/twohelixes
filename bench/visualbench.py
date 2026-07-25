"""Visual benchmark: screenshot every page at mobile and desktop widths.

Writes to visualbench/, which is gitignored. The point is to catch layout
regressions that no unit test sees - a control row that wraps badly at 390px,
a chart that overflows its card, a dark-mode surface that swallows a series
colour.

Run with the server already up:

    pixi run python bench/visualbench.py --base http://127.0.0.1:7474
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "visualbench"

VIEWPORTS = {
    "mobile": {"width": 390, "height": 844, "scale": 2, "mobile": True},
    "tablet": {"width": 834, "height": 1112, "scale": 2, "mobile": True},
    "desktop": {"width": 1440, "height": 900, "scale": 1, "mobile": False},
}

PAGES = [
    ("home", "/"),
    ("features", "/features"),
    ("pricing", "/pricing"),
    ("docs", "/docs"),
    ("app", "/app"),
]

THEMES = ("light", "dark")

SAMPLE_QUERY = {
    "q": "how did revenue trend over time by region?",
    "data": [
        {"order_date": "2024-01-15", "region": "North", "revenue": 1200},
        {"order_date": "2024-02-15", "region": "North", "revenue": 1500},
        {"order_date": "2024-03-15", "region": "North", "revenue": 1800},
        {"order_date": "2024-01-15", "region": "South", "revenue": 900},
        {"order_date": "2024-02-15", "region": "South", "revenue": 850},
        {"order_date": "2024-03-15", "region": "South", "revenue": 700},
        {"order_date": "2024-01-15", "region": "East", "revenue": 400},
        {"order_date": "2024-02-15", "region": "East", "revenue": 650},
        {"order_date": "2024-03-15", "region": "East", "revenue": 980},
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:7474")
    parser.add_argument("--email", default="visualbench@twohelixes.com")
    parser.add_argument("--skip-query", action="store_true")
    args = parser.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed", file=sys.stderr)
        return 2

    OUT.mkdir(parents=True, exist_ok=True)
    report: list[dict[str, object]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])

        for theme in THEMES:
            for name, viewport in VIEWPORTS.items():
                context = browser.new_context(
                    viewport={"width": viewport["width"], "height": viewport["height"]},
                    device_scale_factor=viewport["scale"],
                    is_mobile=viewport["mobile"],
                    has_touch=viewport["mobile"],
                    color_scheme=theme,
                )
                page = context.new_page()
                errors: list[str] = []
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))

                for label, path in PAGES:
                    target = f"{args.base}{path}"
                    try:
                        page.goto(target, wait_until="networkidle", timeout=30000)
                    except Exception as exc:  # noqa: BLE001
                        report.append(
                            {"page": label, "theme": theme, "viewport": name, "error": str(exc)}
                        )
                        continue

                    page.wait_for_timeout(400)
                    shot = OUT / f"{label}-{theme}-{name}.png"
                    page.screenshot(path=str(shot), full_page=True)

                    # A page that scrolls horizontally is a layout bug.
                    overflow = page.evaluate(
                        "() => document.documentElement.scrollWidth - "
                        "document.documentElement.clientWidth"
                    )

                    # A broken <img> still screenshots as a tidy empty box, so
                    # it survives a human skim of the captures. The art plates
                    # were 404ing behind the app port for exactly this reason:
                    # nginx serves /static/ in production, and pointing the
                    # bench straight at the app silently drops every image.
                    # `complete && naturalWidth === 0` is the failure case: a
                    # lazy image that has not started yet reports complete
                    # false, and counting those would flag every below-the-fold
                    # plate on a phone.
                    broken = page.evaluate(
                        "() => [...document.images]"
                        ".filter(i => i.complete && i.naturalWidth === 0)"
                        ".map(i => i.currentSrc || i.src)"
                    )
                    report.append(
                        {
                            "page": label,
                            "theme": theme,
                            "viewport": name,
                            "file": shot.name,
                            "h_overflow_px": overflow,
                            "broken_images": broken,
                            "console_errors": list(errors),
                        }
                    )
                    errors.clear()

                context.close()

        if not args.skip_query:
            report.extend(_capture_query(browser, args))

        browser.close()

    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    _write_index(report)

    overflows = [r for r in report if isinstance(r.get("h_overflow_px"), int) and r["h_overflow_px"] > 1]
    console = [r for r in report if r.get("console_errors")]
    failures = [r for r in report if r.get("error")]
    imgs = [r for r in report if r.get("broken_images")]

    print(f"captured {len([r for r in report if r.get('file')])} screenshots -> {OUT}")
    print(f"horizontal overflow: {len(overflows)}")
    for row in overflows:
        print(f"  {row['page']} {row['theme']} {row['viewport']}: {row['h_overflow_px']}px")
    print(f"broken images: {len(imgs)}")
    for row in imgs[:6]:
        print(f"  {row['page']} {row['viewport']}: {row['broken_images'][:3]}")
    print(f"console errors: {len(console)}")
    for row in console[:6]:
        print(f"  {row['page']} {row['viewport']}: {row['console_errors'][:2]}")
    print(f"navigation failures: {len(failures)}")
    for row in failures[:6]:
        print(f"  {row['page']}: {str(row['error'])[:120]}")

    return 1 if (overflows or failures or imgs) else 0


def _capture_query(browser, args) -> list[dict[str, object]]:
    """Sign in, run a real query, and capture the trace and chart."""
    out: list[dict[str, object]] = []
    for theme in THEMES:
        for name in ("mobile", "desktop"):
            viewport = VIEWPORTS[name]
            context = browser.new_context(
                viewport={"width": viewport["width"], "height": viewport["height"]},
                device_scale_factor=viewport["scale"],
                is_mobile=viewport["mobile"],
                color_scheme=theme,
            )
            page = context.new_page()
            try:
                page.goto(f"{args.base}/app", wait_until="networkidle", timeout=30000)
                # A fresh identity per capture: the free tier is a small
                # lifetime allowance, and the bench must not consume a real
                # account's quota or start failing on its fourth screenshot.
                stamp = int(time.time() * 1000)
                email = args.email.replace("@", f"+{theme}-{name}-{stamp}@")
                page.fill("input.signin-input", email)
                page.click("button[type=submit]")
                page.wait_for_selector("textarea.ask-input", timeout=15000)

                # Drive the app's own code path so the capture exercises
                # the real renderer, not a parallel fetch.
                page.evaluate(
                    "(payload) => window.__thAsk(payload.q, {data: payload.data})",
                    SAMPLE_QUERY,
                )
                page.wait_for_selector(".chart-plot.js-plotly-plot", timeout=180000)
                page.wait_for_timeout(1200)
                shot = OUT / f"query-{theme}-{name}.png"
                page.screenshot(path=str(shot), full_page=True)
                out.append({"page": "query", "theme": theme, "viewport": name, "file": shot.name})
            except Exception as exc:  # noqa: BLE001
                out.append({"page": "query", "theme": theme, "viewport": name, "error": str(exc)})
            finally:
                context.close()
    return out


def _write_index(report: list[dict[str, object]]) -> None:
    """A single page to eyeball every capture side by side."""
    cards = []
    for row in report:
        if not row.get("file"):
            continue
        flag = ""
        if isinstance(row.get("h_overflow_px"), int) and row["h_overflow_px"] > 1:
            flag = f"<b style='color:#e34948'>overflow {row['h_overflow_px']}px</b>"
        if row.get("broken_images"):
            flag += (f" <b style='color:#e34948'>"
                     f"{len(row['broken_images'])} broken img</b>")
        cards.append(
            f"<figure style='margin:0'><figcaption style='font:12px system-ui;"
            f"padding:6px 0'>{row['page']} · {row['theme']} · {row['viewport']} {flag}"
            f"</figcaption><img src='{row['file']}' loading='lazy' "
            f"style='width:100%;border:1px solid #ddd;border-radius:8px'></figure>"
        )

    html = (
        "<!doctype html><meta charset='utf-8'><title>twoHelixes visualbench</title>"
        "<body style='font-family:system-ui;margin:24px;background:#fafafa'>"
        f"<h1 style='font-size:18px'>visualbench · {time.strftime('%Y-%m-%d %H:%M')}</h1>"
        "<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));"
        f"gap:18px'>{''.join(cards)}</div></body>"
    )
    (OUT / "index.html").write_text(html)


if __name__ == "__main__":
    raise SystemExit(main())
