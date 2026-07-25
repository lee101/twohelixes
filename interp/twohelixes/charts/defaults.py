"""Chart defaults, applied to every figure before it reaches the client.

The agent chooses *what* to plot. This module decides how it looks, and it is
deliberately not negotiable: mark weights, legend rules, axis formatting, hover
behaviour and colour assignment are applied here so that two charts built by
two different pipeline runs look like they came from the same product.

`audit()` is the other half - it re-reads a finished figure and reports the
anti-patterns that survived, which the eval suite asserts on.
"""

from __future__ import annotations

import logging
from typing import Any

from twohelixes.charts import palette

log = logging.getLogger("twohelixes.charts.defaults")

# Mark specs, fixed across every chart.
BAR_MAX_THICKNESS = 24
BAR_CORNER_RADIUS = 4
LINE_WIDTH = 2
MARKER_MIN_SIZE = 8
AREA_FILL_OPACITY = 0.10
SPACER = 2  # surface-coloured gap between adjacent fills
GRID_WIDTH = 1

# Direct-label every series only while the chart stays readable.
MAX_DIRECT_LABELS = 4
# A legend appears from two series up; one series is named by the title.
MIN_SERIES_FOR_LEGEND = 2

FONT_STACK = (
    "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)

TIME_TICK_FORMAT = "%b %d"
TIME_TICK_FORMAT_LONG = "%b %Y"


def base_layout(mode: str = "light", title: str = "") -> dict[str, Any]:
    """Layout every chart starts from."""
    face = palette.surface(mode)
    return {
        "title": {
            "text": title,
            "font": {"size": 16, "color": face.text_primary, "family": FONT_STACK},
            "x": 0,
            "xanchor": "left",
            "pad": {"b": 12},
        },
        "font": {"family": FONT_STACK, "size": 12, "color": face.text_secondary},
        "paper_bgcolor": face.background,
        "plot_bgcolor": face.background,
        "colorway": palette.plotly_colorway(mode),
        "margin": {"l": 56, "r": 24, "t": 56 if title else 24, "b": 48},
        "hovermode": "closest",
        "hoverlabel": {
            "bgcolor": face.background,
            "bordercolor": face.axis,
            "font": {"family": FONT_STACK, "size": 12, "color": face.text_primary},
            "align": "left",
        },
        "showlegend": False,
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 12, "color": face.text_secondary},
            "bgcolor": "rgba(0,0,0,0)",
            "borderwidth": 0,
        },
        "xaxis": _axis(face),
        "yaxis": _axis(face, zeroline=True),
        "dragmode": "pan",
        "transition": {"duration": 250, "easing": "cubic-in-out"},
    }


def _axis(face: palette.Surface, zeroline: bool = False) -> dict[str, Any]:
    return {
        "showgrid": True,
        "gridcolor": face.grid,
        "gridwidth": GRID_WIDTH,
        "griddash": "solid",
        "showline": False,
        "zeroline": zeroline,
        "zerolinecolor": face.axis,
        "zerolinewidth": GRID_WIDTH,
        "ticks": "outside",
        "ticklen": 4,
        "tickcolor": face.axis,
        "tickfont": {"size": 11, "color": face.text_muted},
        "title": {"font": {"size": 12, "color": face.text_secondary}},
        "automargin": True,
    }


def apply(spec: dict[str, Any], *, mode: str = "light", chart_type: str = "") -> dict[str, Any]:
    """Apply every default to a Plotly figure dict, in place-safe fashion."""
    figure = {
        "data": list(spec.get("data") or []),
        "layout": dict(spec.get("layout") or {}),
    }

    title = _extract_title(figure["layout"])
    layout = base_layout(mode, title)
    layout.update(
        {k: v for k, v in figure["layout"].items() if k not in ("colorway", "template")}
    )
    layout["title"] = base_layout(mode, title)["title"]

    traces = figure["data"]
    chart_type = chart_type or _infer_type(traces)

    traces = _fold_extra_series(traces, chart_type)
    _assign_colors(traces, mode, chart_type)
    _style_marks(traces, mode, chart_type)
    _configure_hover(traces, layout, chart_type)
    _configure_legend(traces, layout)
    _configure_axes(traces, layout, mode)
    _add_direct_labels(traces, mode)

    figure["data"] = traces
    figure["layout"] = layout
    figure["config"] = display_config()
    return figure


def display_config() -> dict[str, Any]:
    return {
        "displaylogo": False,
        "responsive": True,
        "scrollZoom": False,
        "modeBarButtonsToRemove": [
            "lasso2d",
            "select2d",
            "autoScale2d",
            "toggleSpikelines",
        ],
        "toImageButtonOptions": {"format": "svg", "scale": 2},
    }


def _extract_title(layout: dict[str, Any]) -> str:
    title = layout.get("title")
    if isinstance(title, dict):
        return str(title.get("text") or "")
    return str(title or "")


def _infer_type(traces: list[dict[str, Any]]) -> str:
    if not traces:
        return "scatter"
    kind = str(traces[0].get("type") or "scatter")
    if kind == "scatter":
        mode = str(traces[0].get("mode") or "lines")
        if "lines" in mode:
            return "line"
        return "scatter"
    return kind


def _fold_extra_series(traces: list[dict[str, Any]], chart_type: str) -> list[dict[str, Any]]:
    """Never cycle the palette: fold the tail into a single 'Other' series.

    For the all-pairs forms this bites early (3 series), which is the point -
    a scatter with eight colours is unreadable under CVD no matter the hues.
    """
    limit = palette.max_series_for(chart_type)
    if len(traces) <= limit:
        return traces

    kept = traces[:limit]
    folded = traces[limit:]
    log.info("folding %d series into '%s'", len(folded), palette.OTHER_LABEL)

    if chart_type in ("bar", "line", "area"):
        merged = _merge_traces(folded)
        if merged is not None:
            merged["name"] = palette.OTHER_LABEL
            merged["_folded_count"] = len(folded)
            kept.append(merged)
            return kept

    # Forms where summing is meaningless: drop with an explicit marker so the
    # UI can offer "show all" / facet instead of silently truncating.
    kept.append(
        {
            "type": "scatter",
            "x": [],
            "y": [],
            "name": f"{palette.OTHER_LABEL} ({len(folded)} more)",
            "mode": "markers",
            "visible": "legendonly",
            "_folded_count": len(folded),
        }
    )
    return kept


def _merge_traces(traces: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Sum the y values of like-shaped traces onto the first trace's x."""
    if not traces:
        return None
    base = dict(traces[0])
    xs = base.get("x")
    if xs is None:
        return None

    totals: list[float] = []
    length = len(xs)
    for index in range(length):
        running = 0.0
        for trace in traces:
            values = trace.get("y") or []
            if index < len(values):
                try:
                    running += float(values[index])
                except (TypeError, ValueError):
                    continue
        totals.append(running)

    base["y"] = totals
    return base


def _assign_colors(traces: list[dict[str, Any]], mode: str, chart_type: str) -> None:
    """Colour follows the entity's slot, never its rank at render time.

    A trace that already carries an explicit `_series_index` keeps its hue when
    other series are filtered out - that is what stops recolor-on-filter.
    """
    for position, trace in enumerate(traces):
        index = int(trace.get("_series_index", position))
        color = palette.series_color(index, mode)
        if str(trace.get("name", "")).startswith(palette.OTHER_LABEL):
            color = (
                palette.OTHER_COLOR_DARK if mode == "dark" else palette.OTHER_COLOR_LIGHT
            )
        trace["_series_index"] = index
        trace["_color"] = color

        kind = str(trace.get("type") or "scatter")
        if kind in ("bar", "histogram", "waterfall", "funnel"):
            marker = dict(trace.get("marker") or {})
            marker["color"] = color
            trace["marker"] = marker
        elif kind in ("pie",):
            marker = dict(trace.get("marker") or {})
            marker.setdefault(
                "colors",
                [
                    palette.series_color(i, mode)
                    for i in range(len(trace.get("labels") or []))
                ],
            )
            trace["marker"] = marker
        else:
            line = dict(trace.get("line") or {})
            line["color"] = color
            trace["line"] = line
            marker = dict(trace.get("marker") or {})
            marker["color"] = color
            trace["marker"] = marker


def _style_marks(traces: list[dict[str, Any]], mode: str, chart_type: str) -> None:
    face = palette.surface(mode)
    stacked = _is_stacked(traces)

    for trace in traces:
        kind = str(trace.get("type") or "scatter")
        color = trace.get("_color") or palette.series_color(0, mode)

        if kind == "bar":
            marker = dict(trace.get("marker") or {})
            marker["color"] = color
            marker["cornerradius"] = BAR_CORNER_RADIUS
            # A surface-coloured hairline is the 2px spacer between adjacent
            # or stacked fills; without it segments melt into one block.
            marker["line"] = {"color": face.background, "width": SPACER}
            trace["marker"] = marker
            trace.setdefault("width", None)

        elif kind in ("scatter", "scattergl"):
            trace_mode = str(trace.get("mode") or "lines")
            line = dict(trace.get("line") or {})
            line["width"] = LINE_WIDTH
            line["shape"] = line.get("shape", "linear")
            trace["line"] = line

            marker = dict(trace.get("marker") or {})
            size = marker.get("size", MARKER_MIN_SIZE)
            if isinstance(size, (int, float)):
                marker["size"] = max(MARKER_MIN_SIZE, size)
            else:
                marker.setdefault("size", MARKER_MIN_SIZE)
            # A surface ring keeps overlapping points countable.
            marker["line"] = {"color": face.background, "width": SPACER}
            trace["marker"] = marker

            if "lines" in trace_mode and trace.get("fill") in ("tozeroy", "tonexty"):
                trace["fillcolor"] = _rgba(color, AREA_FILL_OPACITY)

        elif kind == "pie":
            trace.setdefault("hole", 0.0)
            marker = dict(trace.get("marker") or {})
            marker["line"] = {"color": face.background, "width": SPACER}
            trace["marker"] = marker
            trace.setdefault("textposition", "outside")
            trace.setdefault("sort", True)

        elif kind in ("heatmap", "densitymapbox"):
            trace.setdefault("colorscale", _plotly_scale(palette.sequential(9, mode)))
            trace.setdefault("showscale", True)

    if stacked:
        for trace in traces:
            if str(trace.get("type")) == "bar":
                marker = dict(trace.get("marker") or {})
                marker["line"] = {"color": face.background, "width": SPACER}
                trace["marker"] = marker


def _is_stacked(traces: list[dict[str, Any]]) -> bool:
    return any(trace.get("stackgroup") for trace in traces)


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _plotly_scale(colors: list[str]) -> list[list[Any]]:
    if len(colors) == 1:
        return [[0, colors[0]], [1, colors[0]]]
    return [[i / (len(colors) - 1), c] for i, c in enumerate(colors)]


def _configure_hover(
    traces: list[dict[str, Any]], layout: dict[str, Any], chart_type: str
) -> None:
    """A crosshair on continuous forms, per-mark tooltips everywhere else."""
    if chart_type in ("line", "area"):
        layout["hovermode"] = "x unified"
        for trace in traces:
            trace.setdefault("hovertemplate", "%{y}<extra>%{fullData.name}</extra>")
        face_axis = layout.get("xaxis", {})
        face_axis["showspikes"] = True
        face_axis["spikemode"] = "across"
        face_axis["spikethickness"] = 1
        face_axis["spikedash"] = "solid"
        layout["xaxis"] = face_axis
    else:
        layout["hovermode"] = "closest"
        for trace in traces:
            if str(trace.get("type")) == "pie":
                trace.setdefault("hovertemplate", "%{label}: %{value}<extra></extra>")
            else:
                trace.setdefault(
                    "hovertemplate", "%{x}: %{y}<extra>%{fullData.name}</extra>"
                )


def _configure_legend(traces: list[dict[str, Any]], layout: dict[str, Any]) -> None:
    named = [t for t in traces if t.get("name")]
    layout["showlegend"] = len(named) >= MIN_SERIES_FOR_LEGEND
    if len(traces) == 1:
        traces[0].setdefault("showlegend", False)


def _configure_axes(
    traces: list[dict[str, Any]], layout: dict[str, Any], mode: str
) -> None:
    """Axis formatting, plus the hard no on dual axes."""
    for trace in traces:
        if trace.get("yaxis") in ("y2", "y3"):
            # Two y-scales invent a correlation the data does not contain.
            log.warning("dropping secondary axis assignment from trace %s", trace.get("name"))
            trace.pop("yaxis", None)
    for key in ("yaxis2", "yaxis3", "xaxis2"):
        layout.pop(key, None)

    x_values = traces[0].get("x") if traces else None
    if _looks_temporal(x_values):
        axis = dict(layout.get("xaxis") or {})
        axis["type"] = "date"
        axis.setdefault("tickformat", TIME_TICK_FORMAT)
        axis["showgrid"] = False  # time gets its ticks, not a grid
        layout["xaxis"] = axis

    yaxis = dict(layout.get("yaxis") or {})
    yaxis.setdefault("tickformat", "~s")  # 1.2M rather than 1200000
    yaxis.setdefault("rangemode", "tozero")
    layout["yaxis"] = yaxis


def _looks_temporal(values: Any) -> bool:
    if not values:
        return False
    sample = values[0] if isinstance(values, (list, tuple)) else None
    if sample is None:
        return False
    text = str(sample)
    return len(text) >= 8 and text[:4].isdigit() and text[4] in "-/"


def _add_direct_labels(traces: list[dict[str, Any]], mode: str) -> None:
    """Label the end of each line when there are few enough to stay readable.

    Text stays in ink colours - a colored mark beside the label carries the
    identity, the words never do.
    """
    line_traces = [
        t
        for t in traces
        if str(t.get("type") or "scatter") in ("scatter", "scattergl")
        and "lines" in str(t.get("mode") or "lines")
    ]
    if not line_traces or len(line_traces) > MAX_DIRECT_LABELS:
        return

    for trace in line_traces:
        trace.setdefault("_direct_label", trace.get("name") or "")


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


def audit(figure: dict[str, Any], mode: str = "light") -> list[dict[str, str]]:
    """Report anti-patterns left in a finished figure.

    The eval suite asserts this comes back empty for generated charts, which
    is how a regression in the pipeline shows up as a test failure rather than
    an ugly chart in production.
    """
    findings: list[dict[str, str]] = []
    traces = figure.get("data") or []
    layout = figure.get("layout") or {}

    if any(t.get("yaxis") in ("y2", "y3") for t in traces) or "yaxis2" in layout:
        findings.append(
            {
                "code": "dual_axis",
                "detail": "Two y-scales on one plot invent a correlation.",
            }
        )

    colors = [t.get("_color") for t in traces if t.get("_color")]
    if len(colors) != len(set(colors)):
        duplicated = [c for c in set(colors) if colors.count(c) > 1]
        other = palette.OTHER_COLOR_DARK if mode == "dark" else palette.OTHER_COLOR_LIGHT
        if any(c != other for c in duplicated):
            findings.append(
                {"code": "cycled_palette", "detail": "A hue is used by two series."}
            )

    chart_type = _infer_type(traces)
    limit = palette.max_series_for(chart_type)
    coloured = [t for t in traces if not str(t.get("name", "")).startswith(palette.OTHER_LABEL)]
    if len(coloured) > limit:
        findings.append(
            {
                "code": "too_many_series",
                "detail": f"{len(coloured)} series on a {chart_type}; limit is {limit}.",
            }
        )

    if len(traces) == 1 and str(traces[0].get("type")) == "bar":
        xs = traces[0].get("x") or []
        if len(xs) == 1:
            findings.append(
                {"code": "one_bar", "detail": "A single bar should be a stat tile."}
            )

    if any(str(t.get("type")) == "pie" for t in traces):
        for trace in traces:
            if str(trace.get("type")) == "pie":
                labels = trace.get("labels") or []
                if len(labels) == 2:
                    findings.append(
                        {"code": "two_slice_pie", "detail": "Use a stat tile."}
                    )
                elif len(labels) > 6:
                    findings.append(
                        {
                            "code": "crowded_pie",
                            "detail": f"{len(labels)} slices; use a bar chart.",
                        }
                    )

    indices = [int(t.get("_series_index", 0)) for t in traces]
    if palette.needs_relief(indices, mode):
        labelled = any(t.get("_direct_label") for t in traces)
        if not labelled and not layout.get("showlegend"):
            findings.append(
                {
                    "code": "contrast_relief_missing",
                    "detail": "Low-contrast slot in use without labels or legend.",
                }
            )

    return findings
