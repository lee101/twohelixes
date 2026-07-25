"""Example charts for the marketing pages.

These are not screenshots or mockups. Each one is built by the same code that
serves customers: figures go through `figures.build`, then `defaults.apply`,
then the native SVG exporter. If the chart defaults regress - a cycled palette,
a missing spacer, a dual axis - the homepage visibly changes and `audit()`
starts returning findings, which the tests assert on.

Rendered once per worker at import and cached, because a marketing page must
not pay for chart construction on every request.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from twohelixes.charts import defaults, palette, svg

log = logging.getLogger("twohelixes.showcase")

_cache: dict[str, str] = {}


def _frame(data: dict[str, list[Any]]) -> Any:
    import pandas as pd

    return pd.DataFrame(data)


def _revenue_trend() -> tuple[dict[str, Any], str]:
    """Three regions over a year: the shape most first questions produce."""
    months = [
        "2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01",
        "2024-05-01", "2024-06-01", "2024-07-01", "2024-08-01",
        "2024-09-01", "2024-10-01", "2024-11-01", "2024-12-01",
    ]
    north = [312, 348, 371, 402, 445, 468, 501, 534, 578, 612, 664, 703]
    south = [286, 291, 279, 268, 254, 249, 238, 231, 219, 208, 196, 187]
    east = [104, 128, 149, 178, 214, 246, 289, 331, 372, 418, 467, 512]

    traces = [
        {"type": "scatter", "mode": "lines", "x": months, "y": north,
         "name": "North", "_series_index": 0},
        {"type": "scatter", "mode": "lines", "x": months, "y": south,
         "name": "South", "_series_index": 1},
        {"type": "scatter", "mode": "lines", "x": months, "y": east,
         "name": "East", "_series_index": 2},
    ]
    layout = {
        "title": {"text": "East overtook South in August"},
        "xaxis": {"title": {"text": "Month"}},
        "yaxis": {"title": {"text": "Revenue ($k)"}},
    }
    return {"data": traces, "layout": layout}, "line"


def _channel_mix() -> tuple[dict[str, Any], str]:
    """A ranking, with the tail folded rather than truncated."""
    channels = ["Organic", "Paid search", "Email", "Referral", "Social", "Other"]
    values = [4820, 3140, 2260, 1180, 740, 610]

    traces = [{
        "type": "bar", "x": channels, "y": values,
        "name": "Signups", "_series_index": 0,
    }]
    layout = {
        "title": {"text": "Organic drives 34% of signups"},
        "xaxis": {"title": {"text": "Channel"}},
        "yaxis": {"title": {"text": "Signups"}},
    }
    return {"data": traces, "layout": layout}, "bar"


def _latency_distribution() -> tuple[dict[str, Any], str]:
    """A distribution: the answer when the question is 'is it slow?'."""
    buckets = ["<50", "50-100", "100-200", "200-400", "400-800", "800+"]
    counts = [1840, 2960, 1420, 480, 160, 52]

    traces = [{
        # Slot 0 (blue): a single nominal series takes the first slot, and
        # it is one of the slots that clears 3:1 on the light surface.
        "type": "bar", "x": buckets, "y": counts,
        "name": "Requests", "_series_index": 0,
    }]
    layout = {
        "title": {"text": "94% of requests finish under 200 ms"},
        "xaxis": {"title": {"text": "Latency (ms)"}},
        "yaxis": {"title": {"text": "Requests"}},
    }
    return {"data": traces, "layout": layout}, "bar"


def _cohort_retention() -> tuple[dict[str, Any], str]:
    """Two series with an area wash, showing the fill treatment."""
    weeks = [f"2024-0{1 + i // 4}-{1 + (i % 4) * 7:02d}" for i in range(12)]
    retained = [100, 82, 71, 64, 59, 55, 52, 50, 48, 47, 46, 45]

    traces = [{
        "type": "scatter", "mode": "lines", "fill": "tozeroy",
        "x": weeks, "y": retained, "name": "Retained", "_series_index": 0,
    }]
    layout = {
        "title": {"text": "Retention settles at 45% after week 8"},
        "xaxis": {"title": {"text": "Week"}},
        "yaxis": {"title": {"text": "Retained (%)"}},
    }
    return {"data": traces, "layout": layout}, "area"


BUILDERS = {
    "revenue": _revenue_trend,
    "channels": _channel_mix,
    "latency": _latency_distribution,
    "retention": _cohort_retention,
}


def chart_svg(name: str, mode: str = "light", width: int = 640, height: int = 380) -> str:
    """Render one example chart, cached per (name, mode, size)."""
    key = f"{name}:{mode}:{width}x{height}"
    if key in _cache:
        return _cache[key]

    builder = BUILDERS.get(name)
    if builder is None:
        return ""

    try:
        spec, chart_type = builder()
        figure = defaults.apply(spec, mode=mode, chart_type=chart_type)

        findings = defaults.audit(figure, mode=mode)
        if findings:
            # A marketing chart that trips our own audit would be embarrassing
            # and is a genuine signal that the defaults changed.
            log.warning("showcase chart %s has audit findings: %s", name, findings)

        # Transparent: the figure frame supplies the surface, so the chart
        # does not draw a second one inside it.
        # theme_vars: the server cannot know the viewer's OS theme, so the
        # chart carries CSS custom properties and adapts client-side. Without
        # it a light-rendered chart is unreadable on a dark page.
        markup = svg.render(
            figure,
            width=width,
            height=height,
            mode=mode,
            transparent=True,
            theme_vars=True,
        )
    except Exception as exc:  # noqa: BLE001 - never let a chart break the page
        log.exception("could not render showcase chart %s", name)
        markup = ""

    _cache[key] = markup
    return markup


def warm() -> None:
    """Pre-render every example at boot so no request pays for it."""
    for name in BUILDERS:
        for mode in ("light", "dark"):
            chart_svg(name, mode)
    log.info("showcase: %d chart variants cached", len(_cache))
