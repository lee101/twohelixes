"""Visual benchmark: screenshot every page, and every chart form, at real widths.

Writes to visualbench/, which is gitignored. The point is to catch layout
regressions that no unit test sees - a control row that wraps badly at 390px,
a chart that overflows its card, a dark-mode surface that swallows a series
colour.

Four passes:

* every marketing and app page, at three widths and both themes;
* one real query, driven through the app's own code path;
* **every chart form the product can draw, inside the real chart card** - the
  same figures `bench/chartbench.py` builds, but shown in the card a user sees,
  with its controls, notes and export row around them. A form can be perfect in
  isolation and still overflow its card, wrap its legend onto the title, or
  push the page sideways on a phone; that only shows up in the card.
* the data library at phone and desktop widths in both themes, with additional
  phone captures for a completed upload and its import override sheet.

Run with the server already up (the chart pass needs `interp` importable):

    PYTHONPATH=interp pixi run python bench/visualbench.py --base http://127.0.0.1:7474
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

# The chart pass renders into the card at the width the card really gets.
GALLERY_VIEWPORTS = ("mobile", "desktop")

PAGES = [
    ("home", "/"),
    ("features", "/features"),
    ("pricing", "/pricing"),
    ("docs", "/docs"),
    # The dataset pages carry more of our own charts than any other page, at
    # the smallest sizes we render them, so a palette or spacing regression
    # shows up here before it shows up anywhere else.
    ("datasets", "/datasets"),
    ("dataset-detail", "/datasets/orders"),
    ("dataset-example", "/datasets/orders/net-revenue-by-region-over-time"),
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
    parser.add_argument("--skip-pages", action="store_true")
    parser.add_argument("--skip-query", action="store_true")
    parser.add_argument("--skip-gallery", action="store_true")
    parser.add_argument("--skip-library", action="store_true")
    parser.add_argument("--skip-dashboards", action="store_true")
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

        if not args.skip_pages:
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

        if not args.skip_gallery:
            report.extend(_capture_gallery(browser, args))

        if not args.skip_library:
            report.extend(_capture_library(browser, args))

        if not args.skip_dashboards:
            report.extend(_capture_dashboards(browser, args))

        browser.close()

    (OUT / "report.json").write_text(json.dumps(report, indent=2))
    _write_index(report)

    overflows = [r for r in report if isinstance(r.get("h_overflow_px"), int) and r["h_overflow_px"] > 1]
    empty = [r for r in report if r.get("empty_plot")]
    # A tile that stopped drawing partway through a configuration change is
    # the failure this pass exists to catch, and it looks like a tidy box.
    # Keyed on `tile_marks`, not `marks`: the gallery pass reports marks too,
    # and a `stat` chart is legitimately three of them - one big number.
    blank = [
        r for r in report if isinstance(r.get("tile_marks"), int) and r["tile_marks"] < 10
    ]
    spills = [r for r in report if isinstance(r.get("spill_px"), int) and r["spill_px"] > 1]
    console = [r for r in report if r.get("console_errors")]
    failures = [r for r in report if r.get("error")]
    imgs = [r for r in report if r.get("broken_images")]

    print(f"captured {len([r for r in report if r.get('file')])} screenshots -> {OUT}")
    print(f"horizontal overflow: {len(overflows)}")
    for row in overflows:
        print(f"  {row['page']} {row['theme']} {row['viewport']}: {row['h_overflow_px']}px")
    charts = [r for r in report if str(r.get("page", "")).startswith("chart:")]
    print(f"chart forms captured: {len(charts)}")
    print(f"empty plots: {len(empty)}")
    for row in empty[:6]:
        print(f"  {row['page']} {row['theme']} {row['viewport']}: {row.get('marks')} marks")
    print(f"charts overflowing their card: {len(spills)}")
    for row in spills[:6]:
        print(f"  {row['page']} {row['theme']} {row['viewport']}: {row['spill_px']}px")
    print(f"tiles that stopped drawing mid-configuration: {len(blank)}")
    for row in blank[:6]:
        print(f"  {row['page']} {row['theme']} {row['viewport']}: {row['tile_marks']} marks")
    print(f"broken images: {len(imgs)}")
    for row in imgs[:6]:
        print(f"  {row['page']} {row['viewport']}: {row['broken_images'][:3]}")
    print(f"console errors: {len(console)}")
    for row in console[:6]:
        print(f"  {row['page']} {row['viewport']}: {row['console_errors'][:2]}")
    print(f"navigation failures: {len(failures)}")
    for row in failures[:6]:
        print(f"  {row['page']}: {str(row['error'])[:120]}")

    return 1 if (overflows or failures or imgs or empty or spills or blank) else 0


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
                # A fresh identity per capture: the bench must not consume a
                # real account's monthly allowance.
                stamp = int(time.time() * 1000)
                email = args.email.replace("@", f"+{theme}-{name}-{stamp}@")
                # An anonymous visitor now lands on the product, not a form.
                page.click("button:has-text('Sign in')")
                page.wait_for_selector("input.signin-input", timeout=10000)
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


DASHBOARD_TILES = [
    {
        "title": "Revenue by region",
        "config": {"chart_type": "bar", "x": "region", "y": "net_amount", "agg": "sum"},
    },
    {
        "title": "Revenue over time",
        # Resampled, because a line of two years of daily orders per region is
        # a solid block of colour - a real chart, and a useless one to look at.
        "steps": [
            {
                "type": "resample",
                "params": {
                    "time_column": "order_date",
                    "grain": "month",
                    "by": ["region"],
                    "metrics": [{"column": "net_amount", "agg": "sum", "as": "net_amount"}],
                },
            }
        ],
        "config": {
            "chart_type": "line",
            "x": "order_date",
            "y": "net_amount",
            "color": "region",
        },
    },
    {
        "title": "Share of revenue",
        "config": {"chart_type": "pie", "x": "channel", "y": "net_amount", "agg": "sum"},
    },
]


def _tile_marks(page) -> int:
    """What the first tile actually put on screen.

    Plotly ignores a trace type it cannot draw without erroring, so a form
    that draws nothing screenshots as a tidy empty box - which survives a
    human skim of the captures. Counting the marks is what does not.
    """
    return page.evaluate(
        "() => {const t = document.querySelector('.dash-tile');"
        " return t ? t.querySelectorAll('.main-svg path, .main-svg rect,"
        " .main-svg text').length : 0;}"
    )


def _capture_dashboards(browser, args) -> list[dict[str, object]]:
    """The dashboard path end to end, at rest and mid-configuration.

    Empty list, list, board, composer, tile editor - then the same board while
    its settings are being changed: the chart form, the column a tile is about,
    and the tile widths. Those are the states a board is actually in while
    someone works on it, and the ones no resting screenshot shows.

    Panes are built through `/v1/builder` rather than by asking a question, so
    the pass costs no model calls and still exercises the real save-and-attach
    route a manual pane takes. A grid of three charts at 390px is where tile
    chrome, the composer and the per-tile editor all have to fit, and none of
    that is visible to a unit test.
    """
    out: list[dict[str, object]] = []
    for theme in THEMES:
        for name in GALLERY_VIEWPORTS:
            viewport = VIEWPORTS[name]
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

            def capture(label: str, **extra: object) -> None:
                shot = OUT / f"dashboard-{label}-{theme}-{name}.png"
                page.screenshot(path=str(shot), full_page=True)
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth - "
                    "document.documentElement.clientWidth"
                )
                out.append(
                    {
                        "page": f"dashboard:{label}",
                        "theme": theme,
                        "viewport": name,
                        "file": shot.name,
                        "h_overflow_px": overflow,
                        "console_errors": list(errors),
                        **extra,
                    }
                )
                errors.clear()

            try:
                page.goto(f"{args.base}/app", wait_until="networkidle", timeout=30000)
                stamp = int(time.time() * 1000)
                email = args.email.replace("@", f"+dash-{theme}-{name}-{stamp}@")
                page.click("button:has-text('Sign in')")
                page.wait_for_selector("input.signin-input", timeout=10000)
                page.fill("input.signin-input", email)
                page.click("button[type=submit]")
                page.wait_for_selector("textarea.ask-input", timeout=15000)

                # The empty state is the first thing a new account sees here,
                # and an empty page with one button on it is easy to ship badly.
                page.click(".app-nav-item:has-text('Dashboards')")
                page.wait_for_selector(".dash-list", timeout=10000)
                page.wait_for_timeout(300)
                capture("list-empty")

                dashboard_id = page.evaluate(
                    """async (tiles) => {
                      const post = (path, body) => fetch(path, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(body),
                      }).then(r => r.json());

                      const board = await post('/v1/dashboards', {title: 'Sales overview'});
                      for (const tile of tiles) {
                        const source = {
                          sample: 'orders', steps: tile.steps ?? [], config: tile.config,
                        };
                        const preview = await post('/v1/builder/preview', source);
                        const saved = await post('/v1/builder/save', {
                          ...source,
                          figure: preview.figure,
                          config: {...tile.config, title: tile.title},
                          dashboard_id: board.id,
                        });
                        await post(`/v1/dashboards/${board.id}/charts`, {
                          chart_id: saved.chart_id, w: 1,
                        });
                      }
                      return board.id;
                    }""",
                    DASHBOARD_TILES,
                )

                page.click(".app-nav-item:has-text('Ask')")
                page.click(".app-nav-item:has-text('Dashboards')")
                page.wait_for_selector(".dash-list-item", timeout=10000)
                page.wait_for_timeout(300)
                capture("list")

                page.click(".dash-list-open")
                page.wait_for_selector(".dash-tile", timeout=20000)
                page.wait_for_timeout(1500)
                tiles = page.evaluate("() => document.querySelectorAll('.dash-tile').length")
                # A tile whose figure did not draw is an empty box that a human
                # skim of the captures will not catch.
                drawn = page.evaluate(
                    "() => document.querySelectorAll('.dash-plot.js-plotly-plot').length"
                )
                capture("board", tiles=tiles, drawn=drawn, dashboard_id=dashboard_id)

                page.click(".dash-add")
                page.wait_for_selector(".dash-composer-ask", timeout=5000)
                page.wait_for_timeout(300)
                capture("composer")
                page.click(".dash-add")

                page.click(".dash-tile .dash-tile-controls button:has-text('Edit')")
                page.wait_for_selector(".dash-tile-editor", timeout=10000)
                # The channel pickers arrive after a round trip for the columns.
                page.wait_for_timeout(1200)
                editor_controls = page.evaluate(
                    "() => document.querySelectorAll('.dash-tile-controls-grid .control').length"
                )
                capture("tile-editor", editor_controls=editor_controls)

                # -- configuring, not just looking ------------------------
                # Every capture above is a resting state. These are the ones
                # taken mid-configuration, because that is when a board is at
                # its widest: an editor open under a tile, a chart mid-redraw,
                # a tile two columns wide next to two that are one.
                for form, label in (("hbar", "hbar"), ("area", "area"), ("table", "table")):
                    page.select_option(".dash-tile-editor [data-control=chart]", form)
                    page.wait_for_timeout(2200)
                    capture(
                        f"configured-{label}",
                        tile_marks=_tile_marks(page),
                        trace=page.evaluate(
                            "() => document.querySelector('.dash-plot')?.data?.[0]?.type ?? ''"
                        ),
                    )

                # Back to a real chart, then change what it is *about* - the
                # channel change is the one that has the furthest to travel.
                page.select_option(".dash-tile-editor [data-control=chart]", "bar")
                page.wait_for_timeout(2000)
                page.select_option(".dash-tile-editor [data-control=x]", "channel")
                page.wait_for_timeout(2200)
                capture("configured-column", tile_marks=_tile_marks(page))
                page.click(".dash-tile button:has-text('Edit')")

                # Board settings: a wide tile beside narrow ones is the layout
                # most likely to break, and it only exists once someone sets it.
                if name != "mobile":
                    page.click(".dash-tile [data-size='2']")
                    page.wait_for_timeout(1500)
                    capture(
                        "configured-layout",
                        widths=page.evaluate(
                            "() => [...document.querySelectorAll('.dash-tile')]"
                            ".map(t => t.dataset.width)"
                        ),
                        drawn=page.evaluate(
                            "() => document.querySelectorAll('.dash-plot.js-plotly-plot').length"
                        ),
                    )
            except Exception as exc:  # noqa: BLE001
                out.append(
                    {
                        "page": "dashboard",
                        "theme": theme,
                        "viewport": name,
                        "error": str(exc),
                    }
                )
            finally:
                context.close()
    return out


def _capture_library(browser, args) -> list[dict[str, object]]:
    """Capture the drawer and the two upload states most likely to regress."""
    out: list[dict[str, object]] = []
    for theme in THEMES:
        for name in GALLERY_VIEWPORTS:
            viewport = VIEWPORTS[name]
            context = browser.new_context(
                viewport={"width": viewport["width"], "height": viewport["height"]},
                device_scale_factor=viewport["scale"],
                is_mobile=viewport["mobile"],
                has_touch=viewport["mobile"],
                color_scheme=theme,
            )
            uploaded = {"done": False}

            def payload(route, value, status=200):
                route.fulfill(
                    status=status,
                    content_type="application/json",
                    body=json.dumps(value),
                )

            def api_route(route):
                request = route.request
                path = request.url.split("?", 1)[0]
                if path.endswith("/v1/me"):
                    payload(
                        route,
                        {
                            "signed_in": True,
                            "email": "visualbench@twohelixes.com",
                            "plan": "free",
                            "api_credits": 0,
                            "free_queries_used": 0,
                            "free_queries_total": 15,
                            "free_queries_left": 15,
                            "paid": False,
                            "is_admin": False,
                        },
                    )
                    return
                if path.endswith("/v1/sources"):
                    payload(
                        route,
                        {
                            "sources": [
                                {
                                    "id": "source-1",
                                    "name": "Production warehouse",
                                    "kind": "postgresql",
                                    "display_name": "PostgreSQL",
                                    "supports_sql": True,
                                }
                            ]
                        },
                    )
                    return
                if path.endswith("/v1/library"):
                    datasets = [
                        {
                            "id": "dataset-1",
                            "name": "Quarterly orders",
                            "folder_id": "folder-sales",
                            "row_count": 18420,
                            "columns": [
                                "order_id",
                                "ordered_at",
                                "region",
                                "customer",
                                "product",
                                "quantity",
                                "revenue",
                                "discount",
                                "status",
                            ],
                            "shape_report": {
                                "header_row": 0,
                                "header_rows": [0],
                                "preamble_rows_dropped": 0,
                                "notes": [],
                                "confidence": 0.98,
                                "sheets": [],
                            },
                            "share": {"shared": True},
                        },
                        {
                            "id": "dataset-2",
                            "name": "Customer retention",
                            "folder_id": "folder-research",
                            "row_count": 8205,
                            "column_count": 12,
                            "shape_report": {
                                "header_row": 2,
                                "preamble_rows_dropped": 2,
                                "notes": [],
                                "confidence": 0.91,
                                "sheets": [],
                            },
                            "share": {"shared": False},
                        },
                    ]
                    if uploaded["done"]:
                        datasets.insert(
                            0,
                            {
                                "id": "dataset-new",
                                "name": "regional pipeline",
                                "folder_id": None,
                                "row_count": 4102,
                                "column_count": 12,
                                "shape_report": {
                                    "header_row": 3,
                                    "preamble_rows_dropped": 3,
                                    "notes": ["Detected a title and two blank preamble rows."],
                                    "confidence": 0.86,
                                    "sheets": [
                                        {"name": "Pipeline"},
                                        {"name": "Archive"},
                                    ],
                                },
                                "share": {"shared": False},
                            },
                        )
                    payload(
                        route,
                        {
                            "folders": [
                                {
                                    "id": "folder-sales",
                                    "name": "Sales",
                                    "parent_id": None,
                                    "children": [
                                        {
                                            "id": "folder-archive",
                                            "name": "Archive",
                                            "parent_id": "folder-sales",
                                            "children": [],
                                        }
                                    ],
                                },
                                {
                                    "id": "folder-research",
                                    "name": "Research",
                                    "parent_id": None,
                                    "children": [],
                                },
                            ],
                            "datasets": datasets,
                            "sources": [
                                {
                                    "id": "source-1",
                                    "name": "Production warehouse",
                                    "kind": "PostgreSQL",
                                    "folder_id": "folder-sales",
                                    "status": "Connected",
                                }
                            ],
                        },
                    )
                    return
                if path.endswith("/v1/upload"):
                    uploaded["done"] = True
                    payload(
                        route,
                        {
                            "dataset_id": "dataset-new",
                            "name": "regional pipeline",
                            "rows": 4102,
                            "columns": [
                                "region",
                                "owner",
                                "stage",
                                "amount",
                                "opened_at",
                                "close_date",
                                "probability",
                                "segment",
                                "source",
                                "currency",
                                "notes",
                                "account_id",
                            ],
                        },
                        status=201,
                    )
                    return
                if path.endswith("/v1/datasets/dataset-new/preview"):
                    payload(
                        route,
                        {
                            "schema": [
                                {"name": "region", "dtype": "object"},
                                {"name": "owner", "dtype": "object"},
                                {"name": "stage", "dtype": "object"},
                                {"name": "amount", "dtype": "float64"},
                            ],
                            "rows": [
                                {
                                    "region": "North",
                                    "owner": "A. Chen",
                                    "stage": "Proposal",
                                    "amount": 182000,
                                },
                                {
                                    "region": "West",
                                    "owner": "M. Diaz",
                                    "stage": "Discovery",
                                    "amount": 96000,
                                },
                            ],
                            "shape_report": {
                                "header_row": 3,
                                "header_rows": [3],
                                "preamble_rows_dropped": 3,
                                "notes": ["Detected a title and two blank preamble rows."],
                                "confidence": 0.86,
                                "sheets": [
                                    {"name": "Pipeline"},
                                    {"name": "Archive"},
                                ],
                            },
                        },
                    )
                    return
                payload(route, {"error": "not_found"}, status=404)

            context.route("**/v1/**", api_route)
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            try:
                page.goto(f"{args.base}/app", wait_until="networkidle", timeout=30000)
                page.click("button:has-text('Library')")
                page.wait_for_selector(".library-shell.is-open", timeout=10000)
                page.wait_for_timeout(250)
                shot = OUT / f"library-drawer-{theme}-{name}.png"
                page.screenshot(path=str(shot))
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth - "
                    "document.documentElement.clientWidth"
                )
                out.append(
                    {
                        "page": "library:drawer",
                        "theme": theme,
                        "viewport": name,
                        "file": shot.name,
                        "h_overflow_px": overflow,
                        "console_errors": list(errors),
                    }
                )
                errors.clear()

                if name == "mobile":
                    page.click(".library-action-bar button:has-text('Upload')")
                    page.set_input_files(
                        ".file-input",
                        files=[
                            {
                                "name": "regional pipeline.csv",
                                "mimeType": "text/csv",
                                "buffer": b"title\\nprepared 2026-07-27\\n\\n"
                                b"region,owner,stage,amount\\nNorth,A. Chen,Proposal,182000\\n",
                            }
                        ],
                    )
                    page.wait_for_selector(".upload-item", timeout=5000)
                    page.wait_for_function(
                        "() => document.querySelector('.upload-item')"
                        "?.matches('.is-success, .is-error')",
                        timeout=15000,
                    )
                    upload_class = page.locator(".upload-item").first.get_attribute("class")
                    if upload_class and "is-error" in upload_class:
                        raise RuntimeError(
                            "mock upload failed: "
                            + page.locator(".upload-item").first.inner_text()
                        )
                    page.wait_for_timeout(250)
                    shot = OUT / f"library-upload-result-{theme}-mobile.png"
                    page.screenshot(path=str(shot))
                    out.append(
                        {
                            "page": "library:upload-result",
                            "theme": theme,
                            "viewport": "mobile",
                            "file": shot.name,
                            "console_errors": list(errors),
                        }
                    )
                    errors.clear()
                    page.click(".upload-item button:has-text('Not right?')")
                    page.wait_for_selector(".override-sheet", timeout=10000)
                    page.wait_for_timeout(250)
                    shot = OUT / f"library-override-{theme}-mobile.png"
                    page.screenshot(path=str(shot))
                    out.append(
                        {
                            "page": "library:override",
                            "theme": theme,
                            "viewport": "mobile",
                            "file": shot.name,
                            "console_errors": list(errors),
                        }
                    )
                page.go_back(wait_until="commit")
                page.wait_for_selector(".library-shell", state="hidden", timeout=5000)
            except Exception as exc:  # noqa: BLE001
                out.append(
                    {
                        "page": "library",
                        "theme": theme,
                        "viewport": name,
                        "error": str(exc),
                    }
                )
            finally:
                context.close()
    return out



# Marks below which a plot is empty, per form; chartbench owns the exceptions.
_FLOORS: dict[str, int] = {}
DEFAULT_MARK_FLOOR = 6

SHOW_RESULT_JS = """
async ([result]) => {
  if (!window.__thShowResult) throw new Error('__thShowResult missing - stale bundle?');
  await window.__thShowResult(result);
  const card = document.querySelector('.chart-view');
  const plot = document.querySelector('.chart-plot');
  const marks = plot
    ? plot.querySelectorAll('.main-svg path, .main-svg rect, .main-svg text, .main-svg circle').length
    : 0;
  const spill = card && plot
    ? Math.round(plot.getBoundingClientRect().right - card.getBoundingClientRect().right)
    : 0;
  return {
    marks,
    spill,
    overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  };
}
"""


def _gallery_figures() -> dict:
    """Every chart form, built by the pipeline, keyed by (form, mode).

    Shares `bench/chartbench.py`'s cases rather than inventing a second set:
    two benchmarks disagreeing about what a treemap is made of is how one of
    them silently stops covering anything.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "interp"))
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    import chartbench

    cases = chartbench._cases()
    figures: dict[tuple[str, str], dict] = {}
    # A hero number is three text nodes and nothing else, so "the plot is
    # empty" cannot be one threshold for every form.
    _FLOORS.update({k: c.min_marks for k, c in cases.items() if c.min_marks})
    for mode in THEMES:
        for form, case in cases.items():
            result = chartbench.Result(form=form, mode=mode)
            figure = chartbench._build(form, mode, case, result)
            if figure is not None:
                figures[(form, mode)] = {
                    "figure": json.loads(json.dumps(figure, default=str)),
                    "config": dict(case.config),
                    # Through JSON with a string fallback: a pandas Timestamp
                    # is not serialisable to the browser, and the whole form
                    # fails on the way in rather than on the way out.
                    "preview": json.loads(
                        json.dumps(case.frame.head(20).to_dict("records"), default=str)
                    ),
                    "columns": [str(c) for c in case.frame.columns],
                    "row_count": int(len(case.frame)),
                    "warnings": result.warnings,
                    "audit": result.findings,
                    "transform_code": "",
                    "elapsed_ms": 0,
                }
    return figures


def _capture_gallery(browser, args) -> list[dict[str, object]]:
    """Photograph every chart form inside the real chart card."""
    try:
        figures = _gallery_figures()
    except Exception as exc:  # noqa: BLE001 - a missing interp path is not a failure
        print(f"gallery skipped: {exc}", file=sys.stderr)
        return []

    out: list[dict[str, object]] = []
    for theme in THEMES:
        for name in GALLERY_VIEWPORTS:
            viewport = VIEWPORTS[name]
            context = browser.new_context(
                viewport={"width": viewport["width"], "height": viewport["height"]},
                device_scale_factor=viewport["scale"],
                is_mobile=viewport["mobile"],
                color_scheme=theme,
            )
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            try:
                page.goto(f"{args.base}/app", wait_until="networkidle", timeout=30000)
                stamp = int(time.time() * 1000)
                page.click("button:has-text('Sign in')")
                page.wait_for_selector("input.signin-input", timeout=10000)
                page.fill(
                    "input.signin-input",
                    args.email.replace("@", f"+gallery-{theme}-{name}-{stamp}@"),
                )
                page.click("button[type=submit]")
                page.wait_for_selector("textarea.ask-input", timeout=15000)
            except Exception as exc:  # noqa: BLE001
                out.append({"page": "gallery", "theme": theme, "viewport": name, "error": str(exc)})
                context.close()
                continue

            for (form, mode), result in figures.items():
                if mode != theme:
                    continue
                try:
                    measured = page.evaluate(SHOW_RESULT_JS, [result])
                    page.wait_for_timeout(250)
                    shot = OUT / f"chart-{form}-{theme}-{name}.png"
                    node = page.query_selector(".chart-view")
                    node.screenshot(path=str(shot))
                    row: dict[str, object] = {
                        "page": f"chart:{form}",
                        "theme": theme,
                        "viewport": name,
                        "file": shot.name,
                        "h_overflow_px": int(measured["overflow"]),
                        "marks": int(measured["marks"]),
                        "spill_px": int(measured["spill"]),
                        "console_errors": list(errors),
                    }
                    # An empty plot is the failure Plotly does not raise on.
                    if int(measured["marks"]) < _FLOORS.get(form, DEFAULT_MARK_FLOOR):
                        row["empty_plot"] = True
                    out.append(row)
                except Exception as exc:  # noqa: BLE001
                    out.append(
                        {"page": f"chart:{form}", "theme": theme, "viewport": name,
                         "error": str(exc)}
                    )
                errors.clear()

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
        if row.get("empty_plot"):
            flag += " <b style='color:#e34948'>empty plot</b>"
        if isinstance(row.get("spill_px"), int) and row["spill_px"] > 1:
            flag += f" <b style='color:#e34948'>spills {row['spill_px']}px</b>"
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
