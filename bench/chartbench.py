"""The chart benchmark: every form the product can draw, drawn.

Two renderers, because the product has two:

* the native SVG exporter, which serves downloads and the marketing pages, and
  covers bar/line/area/scatter/pie only;
* the browser, through the application's own bundle and the same
  `renderFigure` a user hits - which is where sankey, treemap, box, map and the
  rest actually get drawn.

A benchmark that only exercised the first would have passed while two thirds of
the forms were broken in the UI, which is exactly the failure this repository
has had before: a Plotly build that silently drew nothing for half the trace
types. So the browser pass drives the real page, counts the marks Plotly
actually emitted, and fails a form that renders empty.

Every case also goes through the notebook exporter, because "you can take this
away with you" is part of what a chart is here.

Run the app (any port) and then:

    PYTHONPATH=interp pixi run python bench/chartbench.py --base http://127.0.0.1:7474

Add --no-browser for the exporter pass alone. Output lands in chartbench/,
which is gitignored: SVGs, PNGs, report.json and a contact sheet.
"""

from __future__ import annotations

import argparse
import ast
import html
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from twohelixes.charts import defaults, palette, svg
from twohelixes.notebooks import ipynb_export, marimo_export
from twohelixes.pipeline import figures


ROOT = Path(__file__).resolve().parents[1]
SIZES = {
    "mobile": (360, 260),
    "desktop": (760, 460),
}
MODES = ("light", "dark")

# Below this many SVG elements inside the plot, Plotly drew a frame and no
# data. That is the silent failure this benchmark exists to catch, so the
# threshold is deliberately low - it is a smoke test, not a pixel diff.
MIN_MARKS = 6


@dataclass(frozen=True)
class Case:
    frame: pd.DataFrame
    config: dict[str, Any]
    # What Plotly trace the builder is supposed to emit. A form that quietly
    # falls back to a bar chart still renders, still audits clean, and is
    # still a regression.
    expect: tuple[str, ...] = ()
    # A hero number is three text nodes and nothing else, so the empty-plot
    # floor cannot be one number for every form.
    min_marks: int = 0


@dataclass
class Result:
    form: str
    mode: str
    files: dict[str, str] = field(default_factory=dict)
    markups: dict[str, str] = field(default_factory=dict)
    shots: dict[str, str] = field(default_factory=dict)
    marks: dict[str, int] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    exported: bool = False
    figure: dict[str, Any] | None = None


def _cases() -> dict[str, Case]:
    flows = pd.DataFrame(
        {
            "source": [
                "Organic search",
                "Organic search",
                "Paid search",
                "Paid search",
                "Partner referral",
                "Email",
            ],
            "destination": [
                "Product trial",
                "Newsletter",
                "Product trial",
                "Demo request",
                "Demo request",
                "Product trial",
            ],
            "qualified_leads": [860, 310, 720, 260, 190, 340],
        }
    )
    portfolio = pd.DataFrame(
        {
            "division": [
                "Cloud",
                "Cloud",
                "Cloud",
                "Data",
                "Data",
                "Security",
                "Security",
            ],
            "product": [
                "Compute",
                "Storage",
                "Networking",
                "Warehouse",
                "Streaming",
                "Identity",
                "Threat detection",
            ],
            "annual_revenue": [5.8, 4.1, 2.7, 6.4, 3.6, 4.9, 3.2],
        }
    )
    products = pd.DataFrame(
        {
            "annual_revenue": [2.4, 4.8, 3.2, 6.1, 5.3, 2.9, 4.2, 3.7, 5.8],
            "growth_percent": [18, 11, 27, 8, 16, 31, 13, 22, 9],
            "customers": [340, 760, 280, 1050, 680, 190, 590, 410, 920],
            "segment": [
                "Enterprise",
                "Enterprise",
                "Enterprise",
                "Mid-market",
                "Mid-market",
                "Mid-market",
                "Startup",
                "Startup",
                "Startup",
            ],
        }
    )
    countries = pd.DataFrame(
        {
            "country": [
                "United States",
                "Canada",
                "Brazil",
                "United Kingdom",
                "France",
                "Germany",
                "South Africa",
                "India",
                "Japan",
                "Australia",
            ],
            "active_accounts": [4820, 1260, 980, 2170, 1540, 2390, 610, 2860, 1740, 1190],
        }
    )
    funnel = pd.DataFrame(
        {
            "stage": ["Site visit", "Trial started", "Activated", "Invited team", "Subscribed"],
            "accounts": [24000, 8200, 4700, 2600, 1380],
        }
    )
    bridge = pd.DataFrame(
        {
            "driver": [
                "Opening MRR",
                "New business",
                "Expansion",
                "Contraction",
                "Churn",
                "Closing MRR",
            ],
            "mrr_change": [920, 310, 145, -82, -153, 1140],
        }
    )
    support = pd.DataFrame(
        {
            "plan": ["Starter"] * 9 + ["Team"] * 9 + ["Enterprise"] * 9,
            "resolution_hours": [
                9.2,
                12.5,
                7.8,
                15.1,
                10.4,
                8.6,
                11.7,
                9.8,
                28.0,
                6.1,
                8.4,
                5.5,
                7.2,
                9.0,
                6.7,
                7.8,
                5.9,
                16.4,
                3.2,
                4.7,
                2.8,
                5.1,
                3.9,
                4.2,
                3.5,
                4.4,
                9.8,
            ],
        }
    )

    months = pd.to_datetime(
        [f"2024-{m:02d}-01" for m in range(1, 13)] * 3
    )
    trend = pd.DataFrame(
        {
            "month": months,
            "region": ["North"] * 12 + ["South"] * 12 + ["East"] * 12,
            "revenue": [
                312, 348, 371, 402, 445, 468, 501, 534, 578, 612, 664, 703,
                286, 291, 279, 268, 254, 249, 238, 231, 219, 208, 196, 187,
                104, 128, 149, 178, 214, 246, 289, 331, 372, 418, 467, 512,
            ],
        }
    )
    channels = pd.DataFrame(
        {
            "channel": ["Organic", "Paid search", "Email", "Referral", "Social", "Other"],
            "signups": [4820, 3140, 2260, 1180, 740, 610],
        }
    )
    latency = pd.DataFrame({"response_ms": [
        41, 52, 58, 63, 67, 71, 74, 78, 82, 86, 91, 95, 99, 104, 108, 112,
        117, 121, 126, 133, 138, 144, 151, 159, 168, 177, 188, 201, 219, 244,
        271, 305, 348, 402, 486, 612, 780, 940, 1180, 1520,
    ]})
    activity = pd.DataFrame(
        {
            "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri"] * 4,
            "hour": (["09:00"] * 5 + ["12:00"] * 5 + ["15:00"] * 5 + ["18:00"] * 5),
            "sessions": [
                120, 138, 142, 151, 96,
                210, 232, 245, 238, 174,
                198, 221, 236, 229, 160,
                88, 96, 104, 118, 210,
            ],
        }
    )
    prices = pd.DataFrame(
        {
            "day": pd.to_datetime([f"2024-03-{d:02d}" for d in range(1, 16)]),
            "open": [102, 104, 103, 107, 111, 110, 108, 112, 116, 115, 118, 121, 119, 123, 126],
            "high": [105, 106, 108, 112, 113, 112, 113, 117, 118, 119, 123, 124, 124, 128, 130],
            "low": [101, 102, 102, 106, 108, 106, 107, 111, 113, 113, 117, 118, 117, 122, 124],
            "close": [104, 103, 107, 111, 110, 108, 112, 116, 115, 118, 121, 119, 123, 126, 129],
        }
    )
    single = pd.DataFrame({"active_accounts": [18420]})

    return {
        "line": Case(
            trend,
            {
                "chart_type": "line",
                "x": "month",
                "y": "revenue",
                "color": "region",
                "title": "East overtook South in August",
                "y_title": "Revenue ($k)",
            },
            expect=("scatter",),
        ),
        "area": Case(
            trend[trend["region"] == "North"],
            {
                "chart_type": "area",
                "x": "month",
                "y": "revenue",
                "title": "North grew every month of 2024",
                "y_title": "Revenue ($k)",
            },
            expect=("scatter",),
        ),
        "bar": Case(
            channels,
            {
                "chart_type": "bar",
                "x": "channel",
                "y": "signups",
                "title": "Organic drives 34% of signups",
            },
            expect=("bar",),
        ),
        "hbar": Case(
            channels,
            {
                "chart_type": "hbar",
                "x": "channel",
                "y": "signups",
                "title": "Signups by channel",
            },
            expect=("bar",),
        ),
        "scatter": Case(
            products,
            {
                "chart_type": "scatter",
                "x": "annual_revenue",
                "y": "growth_percent",
                "color": "segment",
                "title": "Growth against revenue",
            },
            expect=("scatter",),
        ),
        "pie": Case(
            channels,
            {
                "chart_type": "pie",
                "x": "channel",
                "y": "signups",
                "title": "Share of signups by channel",
            },
            expect=("pie",),
        ),
        "histogram": Case(
            latency,
            {
                "chart_type": "histogram",
                "x": "response_ms",
                "title": "Response time distribution",
            },
            expect=("histogram",),
        ),
        "heatmap": Case(
            activity,
            {
                "chart_type": "heatmap",
                "x": "weekday",
                "y": "hour",
                "z": "sessions",
                "title": "Sessions by weekday and hour",
            },
            expect=("heatmap",),
        ),
        "candlestick": Case(
            prices,
            {
                "chart_type": "candlestick",
                "x": "day",
                "y": "close",
                "title": "Price, first half of March",
            },
            expect=("candlestick",),
        ),
        "stat": Case(
            single,
            {
                "chart_type": "stat",
                "y": "active_accounts",
                "title": "Active accounts",
            },
            expect=("indicator",),
            min_marks=2,
        ),
        "table": Case(
            channels,
            {
                "chart_type": "table",
                "title": "Signups by channel",
            },
            expect=("table",),
        ),
        "sankey": Case(
            flows,
            {
                "chart_type": "sankey",
                "x": "source",
                "color": "destination",
                "y": "qualified_leads",
                "title": "How qualified leads enter the product",
            },
            expect=("sankey",),
        ),
        "treemap": Case(
            portfolio,
            {
                "chart_type": "treemap",
                "x": "product",
                "color": "division",
                "y": "annual_revenue",
                "title": "Annual revenue by product portfolio",
            },
            expect=("treemap",),
        ),
        "sunburst": Case(
            portfolio,
            {
                "chart_type": "sunburst",
                "x": "product",
                "color": "division",
                "y": "annual_revenue",
                "title": "Revenue mix across product divisions",
            },
            expect=("sunburst",),
        ),
        "bubble": Case(
            products,
            {
                "chart_type": "bubble",
                "x": "annual_revenue",
                "y": "growth_percent",
                "size": "customers",
                "color": "segment",
                "title": "Product growth, revenue, and customer base",
                "x_title": "Annual revenue ($m)",
                "y_title": "Growth (%)",
            },
            expect=("scatter",),
        ),
        "map": Case(
            countries,
            {
                "chart_type": "map",
                "x": "country",
                "y": "active_accounts",
                "title": "Active accounts across key markets",
            },
            expect=("choropleth", "scattergeo"),
        ),
        "funnel": Case(
            funnel,
            {
                "chart_type": "funnel",
                "x": "stage",
                "y": "accounts",
                "title": "Account conversion from visit to subscription",
            },
            expect=("funnel",),
        ),
        "waterfall": Case(
            bridge,
            {
                "chart_type": "waterfall",
                "x": "driver",
                "y": "mrr_change",
                "title": "Monthly recurring revenue bridge",
                "y_title": "MRR ($k)",
            },
            expect=("waterfall",),
        ),
        "box": Case(
            support,
            {
                "chart_type": "box",
                "x": "plan",
                "y": "resolution_hours",
                "title": "Support resolution time by plan",
                "y_title": "Hours",
            },
            expect=("box",),
        ),
    }


def _failure_svg(width: int, height: int, mode: str, message: str) -> str:
    face = palette.surface(mode)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<rect width="100%" height="100%" fill="{face.background}"/>'
        f'<text x="50%" y="50%" text-anchor="middle" fill="{face.text_muted}" '
        f'font-family="Inter, sans-serif" font-size="13">'
        f"{html.escape(message[:100])}</text></svg>"
    )


def _build(form: str, mode: str, case: Case, result: Result) -> dict[str, Any] | None:
    """The figure, through the pipeline's own builder and defaults."""
    try:
        config = figures.validate_config(dict(case.config), case.frame)
        if config.get("chart_type") != form:
            result.errors.append(
                f"validate_config downgraded {form} to {config.get('chart_type')}"
            )
        spec, result.warnings = figures.build(case.frame.copy(), config, mode=mode)
        traces = spec.get("data") or []
        if not traces:
            raise ValueError("builder returned no traces")
        kind = str(traces[0].get("type") or "")
        if case.expect and kind not in case.expect:
            raise ValueError(f"builder returned '{kind}', expected {'/'.join(case.expect)}")
        figure = defaults.apply(spec, mode=mode, chart_type=form)
        result.findings = defaults.audit(figure, mode=mode)
        return figure
    except Exception as exc:  # noqa: BLE001 - the rest of the matrix must still run
        result.errors.append(f"build: {type(exc).__name__}: {exc}")
        return None


def _export_svg(form: str, mode: str, figure: dict[str, Any] | None, result: Result, out: Path) -> None:
    """The native exporter, where it covers the form.

    It covers five trace types by design; the rest are drawn in the browser and
    exported as PNG. Saying so here keeps the sheet honest instead of showing a
    failure for a path that was never claimed.
    """
    for size, (width, height) in SIZES.items():
        filename = f"{form}-{mode}-{size}.svg"
        if figure is None:
            markup = _failure_svg(width, height, mode, result.errors[0])
        elif not _exporter_covers(figure):
            markup = _failure_svg(width, height, mode, "browser-rendered form, see PNG")
        else:
            try:
                markup = svg.render(
                    figure, width=width, height=height, mode=mode,
                    transparent=True, theme_vars=True,
                )
            except Exception as exc:  # noqa: BLE001 - expose exporter gaps in the sheet
                result.errors.append(f"{size} render: {type(exc).__name__}: {exc}")
                markup = _failure_svg(width, height, mode, f"Render failed: {exc}")
        (out / filename).write_text(markup, encoding="utf-8")
        result.files[size] = filename
        result.markups[size] = markup


def _exporter_covers(figure: dict[str, Any]) -> bool:
    traces = figure.get("data") or []
    if not traces:
        return False
    kind = str(traces[0].get("type") or "")
    if kind == "scatter" and traces[0].get("mode") == "markers":
        kind = "scatter"
    return kind in svg.SUPPORTED


def _check_export(form: str, case: Case, figure: dict[str, Any] | None, result: Result) -> None:
    """A chart you cannot take away is half a chart.

    Both notebook formats are generated from the same spec the UI would send,
    and every code cell of the .ipynb is parsed. Parsing is not running, but a
    cell that does not parse cannot run anywhere, and it is the failure a
    template change actually produces.
    """
    if figure is None:
        return
    spec = marimo_export.NotebookSpec(
        title=str(case.config.get("title") or form),
        question=f"benchmark case: {form}",
        chart_config=dict(case.config),
        inline_rows=case.frame.head(50).to_dict("records"),
    )
    try:
        document = ipynb_export.build(spec)
        for cell in ipynb_export.code_cells(document):
            ast.parse(cell)
        ast.parse(marimo_export.build(spec))
        ast.parse(ipynb_export.to_marimo(document))
        result.exported = True
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"notebook export: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# The browser pass
# --------------------------------------------------------------------------


# A phone-width chart card in the product is about 340 tall; the desktop panel
# about 460. Benchmarking at sizes the product does not use finds problems the
# product does not have.
BROWSER_SIZES = {"mobile": (360, 340), "desktop": (760, 460)}

# Rendered through the application's own hook, so the bundle, the theme tokens
# and the stylesheet are the ones a user gets.
RENDER_JS = """
async ([figure, width, height]) => {
  const host = document.createElement('div');
  host.id = 'benchplot';
  host.style.cssText =
    `width:${width}px;height:${height}px;box-sizing:border-box;` +
    'position:fixed;left:0;top:0;z-index:9999;' +
    'background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:8px';
  document.body.append(host);
  const render = window.__thRenderFigure;
  if (!render) throw new Error('__thRenderFigure missing - stale bundle?');
  await render(host, figure);
  // Plotly promotes the host itself to .js-plotly-plot, so querySelector on
  // the host finds nothing and every form reads as empty.
  const marks = host.querySelectorAll(
    '.main-svg path, .main-svg rect, .main-svg text, .main-svg circle, .table-cell'
  ).length;
  return marks;
}
"""


def _browser_pass(
    base: str,
    results: dict[tuple[str, str], Result],
    out: Path,
    case_floor: dict[str, int],
) -> list[str]:
    """Draw every figure in a real page and screenshot it."""
    from playwright.sync_api import sync_playwright

    problems: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        for mode in MODES:
            for size, (width, height) in BROWSER_SIZES.items():
                context = browser.new_context(
                    viewport={"width": max(width + 40, 420), "height": height + 120},
                    device_scale_factor=2,
                    color_scheme=mode,
                    is_mobile=size == "mobile",
                )
                page = context.new_page()
                console: list[str] = []
                page.on("console", lambda m: console.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: console.append(str(e)))
                try:
                    page.goto(f"{base}/app", wait_until="networkidle", timeout=30000)
                except Exception as exc:  # noqa: BLE001
                    problems.append(f"{mode}/{size}: could not open the app: {exc}")
                    context.close()
                    continue

                for (form, result_mode), result in results.items():
                    if result_mode != mode or result.figure is None:
                        continue
                    name = f"{form}-{mode}-{size}.png"
                    try:
                        marks = page.evaluate(
                            RENDER_JS, [result.figure, width, height]
                        )
                        node = page.query_selector("#benchplot")
                        node.screenshot(path=str(out / name))
                        result.shots[size] = name
                        result.marks[size] = int(marks)
                        if int(marks) < (case_floor.get(form) or MIN_MARKS):
                            result.errors.append(
                                f"{size}: drew {marks} marks - the plot is empty"
                            )
                        page.evaluate("() => document.getElementById('benchplot')?.remove()")
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"{size} browser: {type(exc).__name__}: {exc}")
                        page.evaluate("() => document.getElementById('benchplot')?.remove()")

                if console:
                    problems.append(f"{mode}/{size} console: {console[:3]}")
                context.close()
        browser.close()
    return problems



def _theme_rule(mode: str) -> str:
    required = {
        "--text-primary",
        "--text-secondary",
        "--text-muted",
        "--surface-1",
        "--grid",
        "--axis",
        *(f"--series-{index}" for index in range(1, 9)),
    }
    variables = palette.as_css_variables(mode)
    declarations = [f"{name}:{variables[name]}" for name in variables if name in required]
    declarations.append("--panel:var(--surface-1)")
    return f".theme-{mode}{{{';'.join(declarations)}}}"


def _diagnostics(result: Result) -> str:
    rows = []
    for finding in result.findings:
        code = html.escape(str(finding.get("code") or "audit"))
        detail = html.escape(str(finding.get("detail") or ""))
        rows.append(f'<li class="finding"><strong>{code}</strong>: {detail}</li>')
    for error in result.errors:
        rows.append(f'<li class="finding"><strong>failure</strong>: {html.escape(error)}</li>')
    for warning in result.warnings:
        rows.append(f'<li class="warning"><strong>builder note</strong>: {html.escape(warning)}</li>')
    if not result.exported:
        rows.append('<li class="warning"><strong>export</strong>: no notebook produced</li>')
    return f"<ul class=\"diagnostics\">{''.join(rows)}</ul>" if rows else ""


def _panels(result: Result) -> str:
    """One card per renderer and size: the exporter's SVG and the browser's PNG."""
    panels = []
    for size, (width, height) in SIZES.items():
        markup = result.markups.get(size)
        if markup:
            panels.append(
                '<figure class="chart"><figcaption>exporter '
                f"<span>{size} {width}x{height}</span></figcaption>"
                f'<div class="svg-frame {size}">{markup}</div></figure>'
            )
    for size, name in result.shots.items():
        marks = result.marks.get(size, 0)
        panels.append(
            '<figure class="chart"><figcaption>browser '
            f'<span>{size} - {marks} marks</span></figcaption>'
            f'<div class="svg-frame {size}"><img src="{html.escape(name)}" alt=""></div>'
            "</figure>"
        )
    return f'<div class="sizes">{"".join(panels)}</div>'


def _write_index(out: Path, forms_count: int, results: list[Result]) -> None:
    failures = sum(bool(result.errors) for result in results)
    findings = sum(len(result.findings) for result in results)
    sections = []
    for mode in MODES:
        cards = []
        for result in results:
            if result.mode != mode:
                continue
            cards.append(
                '<article class="form">'
                f"<h3>{html.escape(result.form)}</h3>"
                f"{_panels(result)}"
                f"{_diagnostics(result)}"
                "</article>"
            )
        sections.append(
            f'<section class="theme theme-{mode}"><h2>{mode.title()}</h2>'
            f'{"".join(cards)}</section>'
        )

    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>twoHelixes chart benchmark</title>
<style>
*{{box-sizing:border-box}}
body{{margin:0;background:#111;color:#fff;font:14px Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
header{{padding:28px max(24px,calc((100vw - 1220px)/2));background:#111}}
h1,h2,h3,p{{margin:0}}
header p{{margin-top:8px;color:#aaa}}
.theme{{padding:28px max(24px,calc((100vw - 1220px)/2));background:var(--surface-1);color:var(--text-primary)}}
.theme h2{{font-size:24px;margin-bottom:20px}}
.form{{margin-bottom:28px;padding:20px;background:var(--panel);border:1px solid var(--grid);border-radius:12px}}
.form h3{{font-size:17px;margin-bottom:16px}}
.sizes{{display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start}}
.chart{{margin:0;min-width:0}}
.chart figcaption{{display:flex;gap:10px;justify-content:space-between;margin-bottom:8px;color:var(--text-secondary);font-weight:600}}
.chart figcaption span{{color:var(--text-muted);font-size:12px;font-weight:400}}
.svg-frame{{overflow:hidden;border:1px solid var(--grid);border-radius:8px}}
.svg-frame svg,.svg-frame img{{display:block;max-width:100%;height:auto}}
.svg-frame.mobile{{width:360px}}
.svg-frame.desktop{{width:760px;max-width:100%}}
.diagnostics{{margin:14px 0 0;padding-left:20px}}
.diagnostics li+li{{margin-top:5px}}
.finding{{color:#e34948}}
.warning{{color:#eda100}}
{_theme_rule("light")}
{_theme_rule("dark")}
</style>
</head>
<body>
<header>
  <h1>Chart benchmark</h1>
  <p>{forms_count} forms, {failures} failures, {findings} audit findings</p>
</header>
{"".join(sections)}
</body>
</html>
"""
    (out / "index.html").write_text(document, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", action="append", default=[], metavar="NAME")
    parser.add_argument("--out", type=Path, default=ROOT / "chartbench", metavar="DIR")
    parser.add_argument("--base", default="http://127.0.0.1:7474")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    cases = _cases()
    # Every type the pipeline will accept has to have a case, or a form can be
    # added and never drawn here.
    missing = figures.VALID_TYPES - set(cases)
    stale = set(cases) - figures.VALID_TYPES
    if missing or stale:
        parser.error(
            f"case matrix does not match figures.VALID_TYPES "
            f"(missing={sorted(missing)}, stale={sorted(stale)})"
        )

    requested = set(args.only)
    unknown = requested - set(cases)
    if unknown:
        parser.error(f"unknown form(s): {', '.join(sorted(unknown))}")
    selected = tuple(name for name in sorted(cases) if not requested or name in requested)

    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    ordered: dict[tuple[str, str], Result] = {}
    for mode in MODES:
        for form in selected:
            result = Result(form=form, mode=mode)
            result.figure = _build(form, mode, cases[form], result)
            _export_svg(form, mode, result.figure, result, out)
            _check_export(form, cases[form], result.figure, result)
            ordered[(form, mode)] = result

    problems: list[str] = []
    if not args.no_browser:
        try:
            floors = {name: case.min_marks for name, case in cases.items() if case.min_marks}
            problems = _browser_pass(args.base, ordered, out, floors)
        except ImportError:
            problems = ["playwright is not installed; skipped the browser pass"]

    results = list(ordered.values())
    _write_index(out, len(selected), results)
    (out / "report.json").write_text(
        json.dumps(
            [
                {
                    "form": r.form, "mode": r.mode, "marks": r.marks,
                    "findings": r.findings, "warnings": r.warnings,
                    "errors": r.errors, "exported": r.exported,
                }
                for r in results
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    failures = [result for result in results if result.errors]
    findings = sum(len(result.findings) for result in results)
    drawn = sum(1 for r in results if r.marks)
    print(
        f"chartbench: {len(selected)} forms x {len(MODES)} modes, "
        f"{drawn} drawn in the browser, {len(failures)} failures, "
        f"{findings} audit findings -> {out}"
    )
    for result in failures:
        print(f"  {result.form}/{result.mode}: {'; '.join(result.errors)}", file=sys.stderr)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    return 1 if failures or findings or problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
