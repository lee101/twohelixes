"""Turning a chart configuration into a Plotly figure.

The model proposes a configuration; this module is what refuses to build
something wrong. `validate_config` repairs references to columns that do not
exist, downgrades forms the data cannot support, and enforces the rules that
have no legitimate exception (no dual axes, no cycled palette, no silent tail).

`heuristic_config` is the no-LLM path: it picks a defensible chart from the
column roles alone, which is what the pipeline falls back to when the gateway
is down and what the eval suite uses as a baseline.
"""

from __future__ import annotations

import logging
from typing import Any

from twohelixes.charts import palette
from twohelixes.interpreter import tools

log = logging.getLogger("twohelixes.pipeline.figures")

VALID_TYPES = {
    "line",
    "bar",
    "hbar",
    "area",
    "scatter",
    "pie",
    "heatmap",
    "box",
    "histogram",
    "candlestick",
    "sankey",
    "treemap",
    "funnel",
    "waterfall",
    "stat",
    "table",
}

# Above this many categories a pie is unreadable; above the bar limit the tail
# is folded into "Other".
MAX_PIE_SLICES = 6
MAX_BAR_CATEGORIES = 24
LONG_LABEL_CHARS = 14


def validate_config(
    config: dict[str, Any], frame: Any, emit: Any = None
) -> dict[str, Any]:
    """Repair a proposed configuration against the data that actually exists."""
    columns = {str(c) for c in frame.columns}
    out = dict(config)

    chart_type = str(out.get("chart_type") or "").lower().strip()
    if chart_type not in VALID_TYPES:
        chart_type = heuristic_config(frame, "")["chart_type"]
        _note(emit, f"Unknown chart type; using {chart_type}.")
    out["chart_type"] = chart_type

    for channel in ("x", "z", "color", "size", "facet"):
        value = out.get(channel)
        if value is not None and str(value) not in columns:
            _note(emit, f"Dropped {channel}: column '{value}' is not in the data.")
            out[channel] = None

    # y may be a list of measures.
    y = out.get("y")
    if isinstance(y, list):
        kept = [c for c in y if str(c) in columns]
        if len(kept) != len(y):
            _note(emit, "Dropped y columns that are not in the data.")
        out["y"] = kept or None
    elif y is not None and str(y) not in columns:
        _note(emit, f"Dropped y: column '{y}' is not in the data.")
        out["y"] = None

    # Fill in anything missing from the roles.
    fallback = heuristic_config(frame, "")
    if not out.get("x"):
        out["x"] = fallback.get("x")
    if not out.get("y"):
        out["y"] = fallback.get("y")

    # Two measures of different scale on one plot is the dual-axis trap in
    # disguise. Keep the first and say so.
    if isinstance(out.get("y"), list) and len(out["y"]) > 1:
        if _scales_differ(frame, out["y"]):
            kept = out["y"][0]
            _note(
                emit,
                f"'{out['y'][1]}' is on a different scale to '{kept}'; "
                "charting one measure. Ask for a second chart to compare them.",
            )
            out["y"] = kept

    # Form downgrades the data forces.
    if chart_type == "pie":
        categories = _distinct(frame, out.get("x"))
        if categories == 2:
            out["chart_type"] = "stat"
            _note(emit, "Two slices is a number, not a pie.")
        elif categories > MAX_PIE_SLICES:
            out["chart_type"] = "bar"
            _note(emit, f"{categories} slices is too many for a pie; using bars.")

    if chart_type == "bar" and _has_long_labels(frame, out.get("x")):
        out["chart_type"] = "hbar"
        out["orientation"] = "h"

    if chart_type in ("line", "area") and out.get("x"):
        if tools.column_role(frame, out["x"]) not in ("time",):
            if _distinct(frame, out["x"]) <= 12:
                out["chart_type"] = "bar"
                _note(emit, "The x axis is categorical; using bars rather than a line.")

    if chart_type == "bar" and out.get("x"):
        if tools.column_role(frame, out["x"]) == "time" and _distinct(frame, out["x"]) > 20:
            out["chart_type"] = "line"
            _note(emit, "Many time buckets read better as a line.")

    out.setdefault("orientation", "h" if out["chart_type"] == "hbar" else "v")
    out.setdefault("stacked", False)
    out.setdefault("title", "")
    out.setdefault("agg", None)
    return out


def _note(emit: Any, message: str) -> None:
    if emit is not None:
        emit.warn(message)
    else:
        log.info("config: %s", message)


def _distinct(frame: Any, column: Any) -> int:
    if not column or str(column) not in {str(c) for c in frame.columns}:
        return 0
    return int(frame[column].nunique(dropna=True))


def _has_long_labels(frame: Any, column: Any) -> bool:
    if not column or str(column) not in {str(c) for c in frame.columns}:
        return False
    try:
        widest = frame[column].astype(str).str.len().max()
    except Exception:  # noqa: BLE001
        return False
    return bool(widest and widest > LONG_LABEL_CHARS)


def _scales_differ(frame: Any, columns: list[str], ratio: float = 20.0) -> bool:
    magnitudes = []
    for column in columns:
        try:
            value = float(frame[column].abs().median())
        except Exception:  # noqa: BLE001
            continue
        if value > 0:
            magnitudes.append(value)
    if len(magnitudes) < 2:
        return False
    return max(magnitudes) / min(magnitudes) > ratio


def heuristic_config(frame: Any, question: str) -> dict[str, Any]:
    """Pick a defensible chart from column roles alone."""
    time_column = tools.find_time_column(frame)
    measures = tools.find_measures(frame)
    categories = tools.find_categories(frame)

    if not measures:
        # Nothing to measure: show the distribution of the first category.
        if categories:
            return {
                "chart_type": "bar",
                "x": categories[0],
                "y": None,
                "agg": "count",
                "title": f"Count by {tools.humanise(categories[0])}",
            }
        return {"chart_type": "table", "x": None, "y": None, "title": "Data"}

    measure = measures[0]

    if time_column:
        return {
            "chart_type": "line",
            "x": time_column,
            "y": measure,
            "color": categories[0] if categories else None,
            "agg": "sum",
            "title": f"{tools.humanise(measure)} over time",
            "x_title": tools.humanise(time_column),
            "y_title": tools.humanise(measure),
        }

    if categories:
        category = categories[0]
        return {
            "chart_type": "hbar" if _has_long_labels(frame, category) else "bar",
            "x": category,
            "y": measure,
            "agg": "sum",
            "title": f"{tools.humanise(measure)} by {tools.humanise(category)}",
            "x_title": tools.humanise(category),
            "y_title": tools.humanise(measure),
        }

    if len(measures) >= 2:
        return {
            "chart_type": "scatter",
            "x": measures[0],
            "y": measures[1],
            "title": f"{tools.humanise(measures[1])} vs {tools.humanise(measures[0])}",
        }

    if len(frame) == 1:
        return {"chart_type": "stat", "y": measure, "title": tools.humanise(measure)}

    return {
        "chart_type": "histogram",
        "x": measure,
        "title": f"Distribution of {tools.humanise(measure)}",
    }


def build(
    frame: Any, config: dict[str, Any], mode: str = "light"
) -> tuple[dict[str, Any], list[str]]:
    """Construct the Plotly figure dict for a validated configuration."""
    warnings: list[str] = []
    chart_type = str(config.get("chart_type") or "bar")
    x = config.get("x")
    y = config.get("y")
    color = config.get("color")

    if chart_type == "stat":
        return _stat(frame, config), warnings
    if chart_type == "table":
        return _table(frame, config), warnings

    working = frame
    if config.get("agg") and x and y and not color:
        try:
            working = tools.summarise(frame, x, y, str(config["agg"]))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Could not aggregate ({exc}); charting raw rows.")

    if chart_type in ("bar", "hbar") and x:
        distinct = _distinct(working, x)
        if distinct > MAX_BAR_CATEGORIES and y:
            working = tools.top_n(working, str(x), str(y), n=MAX_BAR_CATEGORIES - 1)
            warnings.append(
                f"{distinct} categories; showing the top {MAX_BAR_CATEGORIES - 1} "
                "with the rest as 'Other'."
            )

    traces = _traces(working, config, chart_type, mode, warnings)
    layout: dict[str, Any] = {
        "title": {"text": config.get("title") or ""},
        "xaxis": {"title": {"text": config.get("x_title") or (tools.humanise(x) if x else "")}},
        "yaxis": {
            "title": {
                "text": config.get("y_title")
                or (tools.humanise(y) if isinstance(y, str) else "")
            }
        },
    }
    if config.get("stacked"):
        layout["barmode"] = "stack"

    return {"data": traces, "layout": layout}, warnings


def _traces(
    frame: Any,
    config: dict[str, Any],
    chart_type: str,
    mode: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    x = config.get("x")
    y = config.get("y")
    color = config.get("color")

    if color and str(color) in {str(c) for c in frame.columns}:
        return _grouped_traces(frame, config, chart_type, mode, warnings)

    values_x = _column(frame, x)
    values_y = _column(frame, y)

    if chart_type == "line":
        return [
            {
                "type": "scatter",
                "mode": "lines",
                "x": values_x,
                "y": values_y,
                "name": tools.humanise(y) if isinstance(y, str) else "value",
                "_series_index": 0,
            }
        ]
    if chart_type == "area":
        return [
            {
                "type": "scatter",
                "mode": "lines",
                "fill": "tozeroy",
                "x": values_x,
                "y": values_y,
                "name": tools.humanise(y) if isinstance(y, str) else "value",
                "_series_index": 0,
            }
        ]
    if chart_type == "scatter":
        trace = {
            "type": "scatter",
            "mode": "markers",
            "x": values_x,
            "y": values_y,
            "name": tools.humanise(y) if isinstance(y, str) else "value",
            "_series_index": 0,
        }
        size = config.get("size")
        if size and str(size) in {str(c) for c in frame.columns}:
            trace["marker"] = {"size": _column(frame, size), "sizemode": "area"}
        return [trace]
    if chart_type == "hbar":
        return [
            {
                "type": "bar",
                "orientation": "h",
                "x": values_y,
                "y": values_x,
                "name": tools.humanise(y) if isinstance(y, str) else "value",
                "_series_index": 0,
            }
        ]
    if chart_type == "pie":
        return [
            {
                "type": "pie",
                "labels": values_x,
                "values": values_y,
                "_series_index": 0,
            }
        ]
    if chart_type == "histogram":
        return [{"type": "histogram", "x": values_x or values_y, "_series_index": 0}]
    if chart_type == "box":
        return [{"type": "box", "y": values_y, "x": values_x, "_series_index": 0}]
    if chart_type == "heatmap":
        return _heatmap(frame, config, warnings)
    if chart_type == "candlestick":
        return _candlestick(frame, config, warnings)

    return [
        {
            "type": "bar",
            "x": values_x,
            "y": values_y,
            "name": tools.humanise(y) if isinstance(y, str) else "value",
            "_series_index": 0,
        }
    ]


def _grouped_traces(
    frame: Any,
    config: dict[str, Any],
    chart_type: str,
    mode: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """One trace per colour group, capped so the palette is never cycled."""
    x, y, color = config.get("x"), config.get("y"), config.get("color")
    limit = palette.max_series_for(chart_type)

    order = (
        frame.groupby(color)[y].sum().sort_values(ascending=False).index.tolist()
        if isinstance(y, str)
        else frame[color].dropna().unique().tolist()
    )

    traces: list[dict[str, Any]] = []
    for index, group in enumerate(order[:limit]):
        subset = frame[frame[color] == group]
        traces.append(
            {
                "type": "bar" if chart_type in ("bar", "hbar") else "scatter",
                "mode": "lines" if chart_type in ("line", "area") else None,
                "fill": "tozeroy" if chart_type == "area" else None,
                "x": _column(subset, x),
                "y": _column(subset, y),
                "name": str(group),
                # The slot is pinned to the entity, so filtering other series
                # out later never repaints this one.
                "_series_index": index,
            }
        )

    if len(order) > limit:
        tail = frame[frame[color].isin(order[limit:])]
        if isinstance(y, str) and len(tail):
            grouped = tail.groupby(x)[y].sum().reset_index()
            traces.append(
                {
                    "type": "bar" if chart_type in ("bar", "hbar") else "scatter",
                    "mode": "lines" if chart_type in ("line", "area") else None,
                    "x": _column(grouped, x),
                    "y": _column(grouped, y),
                    "name": palette.OTHER_LABEL,
                    "_series_index": limit,
                }
            )
        warnings.append(
            f"{len(order)} groups; showing the top {limit} and folding "
            f"{len(order) - limit} into '{palette.OTHER_LABEL}'."
        )

    for trace in traces:
        for key in ("mode", "fill"):
            if trace.get(key) is None:
                trace.pop(key, None)
    return traces


def _heatmap(frame: Any, config: dict[str, Any], warnings: list[str]) -> list[dict[str, Any]]:
    x, y, z = config.get("x"), config.get("y"), config.get("z")
    if not (x and y and z):
        warnings.append("A heatmap needs x, y and z; falling back to a bar chart.")
        return [{"type": "bar", "x": _column(frame, x), "y": _column(frame, y), "_series_index": 0}]

    pivot = frame.pivot_table(index=y, columns=x, values=z, aggfunc="mean")
    return [
        {
            "type": "heatmap",
            "x": [str(c) for c in pivot.columns],
            "y": [str(i) for i in pivot.index],
            "z": pivot.values.tolist(),
            "_series_index": 0,
        }
    ]


def _candlestick(
    frame: Any, config: dict[str, Any], warnings: list[str]
) -> list[dict[str, Any]]:
    columns = {str(c).lower(): str(c) for c in frame.columns}
    required = ("open", "high", "low", "close")
    if not all(name in columns for name in required):
        warnings.append("No OHLC columns found; using a line chart.")
        return [
            {
                "type": "scatter",
                "mode": "lines",
                "x": _column(frame, config.get("x")),
                "y": _column(frame, config.get("y")),
                "_series_index": 0,
            }
        ]
    return [
        {
            "type": "candlestick",
            "x": _column(frame, config.get("x") or tools.find_time_column(frame)),
            "open": _column(frame, columns["open"]),
            "high": _column(frame, columns["high"]),
            "low": _column(frame, columns["low"]),
            "close": _column(frame, columns["close"]),
            "_series_index": 0,
        }
    ]


def _stat(frame: Any, config: dict[str, Any]) -> dict[str, Any]:
    """A hero number - the right form when the answer is one value."""
    y = config.get("y")
    value: Any = None
    if y and str(y) in {str(c) for c in frame.columns}:
        series = frame[y].dropna()
        value = float(series.iloc[0]) if len(series) == 1 else float(series.sum())

    return {
        "data": [
            {
                "type": "indicator",
                "mode": "number",
                "value": value,
                "number": {"valueformat": ",.4~s"},
                "_series_index": 0,
            }
        ],
        "layout": {"title": {"text": config.get("title") or ""}},
    }


def _table(frame: Any, config: dict[str, Any]) -> dict[str, Any]:
    head = frame.head(200)
    return {
        "data": [
            {
                "type": "table",
                "header": {"values": [str(c) for c in head.columns]},
                "cells": {"values": [head[c].astype(str).tolist() for c in head.columns]},
                "_series_index": 0,
            }
        ],
        "layout": {"title": {"text": config.get("title") or "Data"}},
    }


def _column(frame: Any, name: Any) -> list[Any]:
    if not name:
        return []
    key = str(name)
    if key not in {str(c) for c in frame.columns}:
        return []
    series = frame[key]
    try:
        if hasattr(series.dtype, "tz") or "datetime" in str(series.dtype):
            return [v.isoformat() if hasattr(v, "isoformat") else str(v) for v in series]
    except Exception:  # noqa: BLE001
        pass
    return series.tolist()
