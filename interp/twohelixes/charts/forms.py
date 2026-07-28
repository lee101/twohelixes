"""Chart forms that need real logic, not a trace dict.

Four of the declared chart types built nothing: asking for a sankey got you a
bar chart with no explanation. Each of these needs the data reshaped into a
form Plotly can draw, and each has a way of going wrong that is worth stating.

Every builder returns `(traces, warnings)`. A warning is how the form tells
the user it had to approximate - a sankey that dropped a cycle, a funnel that
was handed unsorted stages - rather than silently producing something wrong.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

from twohelixes.charts import palette

log = logging.getLogger("twohelixes.charts.forms")

# Past this, a sankey is a hairball and a treemap's labels stop fitting.
MAX_SANKEY_LINKS = 60
MAX_TREEMAP_NODES = 120
MAX_FUNNEL_STAGES = 12

_LATITUDE_NAMES = ("lat", "latitude")
_LONGITUDE_NAMES = ("lon", "lng", "longitude")
_COUNTRY_HINTS = ("country", "countries", "nation")
_COMMON_COUNTRIES = frozenset(
    {
        "argentina", "australia", "austria", "belgium", "brazil", "canada",
        "chile", "china", "colombia", "croatia", "czechia", "denmark", "egypt",
        "finland", "france", "germany", "greece", "hungary", "iceland", "india",
        "indonesia", "ireland", "israel", "italy", "japan", "kenya", "malaysia",
        "mexico", "morocco", "netherlands", "new zealand", "nigeria", "norway",
        "pakistan", "peru", "philippines", "poland", "portugal", "romania",
        "russia", "saudi arabia", "singapore", "slovakia", "south africa",
        "south korea", "spain", "sweden", "switzerland", "thailand", "turkey",
        "ukraine", "united arab emirates", "united kingdom", "united states",
        "uruguay", "venezuela", "vietnam",
    }
)


def _column(frame: Any, name: Any) -> str | None:
    if not name:
        return None
    text = str(name)
    return text if text in {str(c) for c in frame.columns} else None


def _series(frame: Any, name: str) -> list[Any]:
    return frame[name].tolist()


# --------------------------------------------------------------------------
# Sankey
# --------------------------------------------------------------------------


def sankey(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Flow between two categorical columns, weighted by a measure.

    Node indices are the fiddly part: Plotly wants integer offsets into one
    label list, and source and target may share names. Prefixing each side
    keeps "Paid" as a source distinct from "Paid" as a target, which is what
    stops a self-loop that never existed in the data.
    """
    warnings: list[str] = []
    source = _column(frame, config.get("x") or config.get("source"))
    target = _column(frame, config.get("color") or config.get("target"))
    value = _column(frame, config.get("y") or config.get("value"))

    if not source or not target:
        return [], ["A sankey needs a source and a target column."]

    grouped = frame.groupby([source, target], dropna=False)
    flows = (
        grouped[value].sum().reset_index()
        if value
        else grouped.size().reset_index(name="_count")
    )
    measure = value or "_count"
    flows = flows.sort_values(measure, ascending=False)

    if len(flows) > MAX_SANKEY_LINKS:
        warnings.append(
            f"{len(flows)} flows is unreadable; showing the {MAX_SANKEY_LINKS} largest."
        )
        flows = flows.head(MAX_SANKEY_LINKS)

    left = [f"{v}" for v in flows[source].astype(str)]
    right = [f"{v}" for v in flows[target].astype(str)]

    labels: list[str] = []
    index: dict[str, int] = {}

    def slot(name: str, side: str) -> int:
        key = f"{side}:{name}"
        if key not in index:
            index[key] = len(labels)
            labels.append(name)
        return index[key]

    sources = [slot(name, "s") for name in left]
    targets = [slot(name, "t") for name in right]

    colours = [palette.series_color(i % palette.MAX_SERIES, mode) for i in range(len(labels))]
    face = palette.surface(mode)

    trace = {
        "type": "sankey",
        "orientation": "h",
        "node": {
            "label": labels,
            "color": colours,
            "pad": 14,
            "thickness": 16,
            "line": {"color": face.background, "width": 2},
        },
        "link": {
            "source": sources,
            "target": targets,
            "value": [float(v) for v in flows[measure]],
            # A translucent link lets crossings stay readable where they overlap.
            "color": [
                _rgba(palette.series_color(s % palette.MAX_SERIES, mode), 0.35)
                for s in sources
            ],
        },
        "_series_index": 0,
    }
    return [trace], warnings


# --------------------------------------------------------------------------
# Hierarchies
# --------------------------------------------------------------------------


def _hierarchy(
    frame: Any, config: dict[str, Any], noun: str
) -> tuple[list[str], list[str], list[float], list[str]]:
    warnings: list[str] = []
    category = _column(frame, config.get("x") or config.get("category"))
    value = _column(frame, config.get("y") or config.get("value"))
    parent = _column(frame, config.get("color") or config.get("parent"))

    if not category:
        return [], [], [], [f"A {noun} needs a category column."]

    labels: list[str] = []
    parents: list[str] = []
    values: list[float] = []

    if parent:
        grouped = (
            frame.groupby([parent, category], dropna=False)[value].sum().reset_index()
            if value
            else frame.groupby([parent, category], dropna=False).size().reset_index(name="_count")
        )
        measure = value or "_count"

        # Parent rows first, with their own totals, so the tiles nest.
        for name, total in grouped.groupby(parent)[measure].sum().items():
            labels.append(str(name))
            parents.append("")
            values.append(float(total))

        for _, row in grouped.iterrows():
            labels.append(f"{row[category]}")
            parents.append(str(row[parent]))
            values.append(float(row[measure]))
    else:
        grouped = (
            frame.groupby(category, dropna=False)[value].sum().reset_index()
            if value
            else frame.groupby(category, dropna=False).size().reset_index(name="_count")
        )
        measure = value or "_count"
        grouped = grouped.sort_values(measure, ascending=False)
        for _, row in grouped.iterrows():
            labels.append(str(row[category]))
            parents.append("")
            values.append(float(row[measure]))

    if len(labels) > MAX_TREEMAP_NODES:
        warnings.append(
            f"{len(labels)} nodes is too many to label; showing the largest "
            f"{MAX_TREEMAP_NODES}."
        )
        keep = sorted(range(len(values)), key=lambda i: values[i], reverse=True)[
            :MAX_TREEMAP_NODES
        ]
        keep_set = set(keep)
        labels = [l for i, l in enumerate(labels) if i in keep_set]
        parents = [p for i, p in enumerate(parents) if i in keep_set]
        values = [v for i, v in enumerate(values) if i in keep_set]

    return labels, parents, values, warnings


def _hierarchy_trace(
    kind: str, frame: Any, config: dict[str, Any], mode: str
) -> tuple[list, list[str]]:
    labels, parents, values, warnings = _hierarchy(frame, config, kind)
    if not labels:
        return [], warnings

    face = palette.surface(mode)
    trace = {
        "type": kind,
        "labels": labels,
        "parents": parents,
        "values": values,
        # "total" so a parent shows its own sum rather than double counting.
        "branchvalues": "total",
        "marker": {
            "colors": [
                palette.series_color(i % palette.MAX_SERIES, mode) for i in range(len(labels))
            ],
            "line": {"color": face.background, "width": 2},
        },
        "textinfo": "label+value+percent parent",
        "_series_index": 0,
    }
    return [trace], warnings


def treemap(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Part-to-whole with optional nesting."""
    return _hierarchy_trace("treemap", frame, config, mode)


def sunburst(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Radial part-to-whole using the same flattened hierarchy as a treemap."""
    return _hierarchy_trace("sunburst", frame, config, mode)


# --------------------------------------------------------------------------
# Bubble
# --------------------------------------------------------------------------


def _bubble_sizes(values: list[Any]) -> list[float]:
    numeric = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 0.0
        numeric.append(max(0.0, number) if math.isfinite(number) else 0.0)
    maximum = max(numeric, default=0.0)
    if maximum <= 0:
        return [6.0] * len(numeric)
    return [min(42.0, max(6.0, 42.0 * math.sqrt(value / maximum))) for value in numeric]


def bubble(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Scatter whose diameter is proportional to the square root of size."""
    x = _column(frame, config.get("x"))
    y = _column(frame, config.get("y"))
    size = _column(frame, config.get("size"))
    color = _column(frame, config.get("color"))
    if not x or not y or not size:
        return [], ["A bubble chart needs x, y and size columns."]

    warnings: list[str] = []
    groups = [None]
    if color:
        groups = list(frame[color].dropna().unique())
        limit = palette.max_series_for("bubble")
        if len(groups) > limit:
            warnings.append(f"{len(groups)} groups; showing the first {limit}.")
            groups = groups[:limit]

    traces: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        subset = frame if group is None else frame[frame[color] == group]
        traces.append(
            {
                "type": "scatter",
                "mode": "markers",
                "x": _series(subset, x),
                "y": _series(subset, y),
                "name": str(group) if group is not None else None,
                "marker": {
                    "size": _bubble_sizes(_series(subset, size)),
                    "sizemode": "diameter",
                },
                "_series_index": index,
            }
        )
    return traces, warnings


# --------------------------------------------------------------------------
# Map
# --------------------------------------------------------------------------


def _numeric_in_range(frame: Any, column: str, low: float, high: float) -> bool:
    series = frame[column]
    if getattr(series.dtype, "kind", "") not in "iuf":
        return False
    values = []
    for value in series.dropna().tolist():
        try:
            number = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(number):
            return False
        values.append(number)
    return bool(values) and all(low <= value <= high for value in values)


def _named_column(frame: Any, names: tuple[str, ...]) -> str | None:
    columns = {str(column).lower(): str(column) for column in frame.columns}
    for name in names:
        if name in columns:
            return columns[name]
    return None


def _country_column(frame: Any, config: dict[str, Any]) -> tuple[str, str] | None:
    known = {str(column) for column in frame.columns}
    preferred = [
        str(config.get(key))
        for key in ("x", "color")
        if config.get(key) is not None and str(config.get(key)) in known
    ]
    candidates = preferred + [str(column) for column in frame.columns if str(column) not in preferred]

    for column in candidates:
        series = frame[column]
        if getattr(series.dtype, "kind", "") not in "OUS":
            continue
        values = [str(value).strip() for value in series.dropna().tolist() if str(value).strip()]
        if not values:
            continue
        sample = values[:100]
        if all(re.fullmatch(r"[A-Za-z]{3}", value) for value in sample):
            return column, "ISO-3"
        normalized = [value.casefold() for value in sample]
        hinted = any(hint in column.casefold() for hint in _COUNTRY_HINTS)
        recognized = sum(value in _COMMON_COUNTRIES for value in normalized)
        if hinted or recognized / len(normalized) >= 0.8:
            return column, "country names"
    return None


def detect_map(frame: Any, config: dict[str, Any] | None = None) -> dict[str, str] | None:
    """Return the geographic columns and map form the frame can support."""
    latitude = _named_column(frame, _LATITUDE_NAMES)
    longitude = _named_column(frame, _LONGITUDE_NAMES)
    if (
        latitude
        and longitude
        and _numeric_in_range(frame, latitude, -90.0, 90.0)
        and _numeric_in_range(frame, longitude, -180.0, 180.0)
    ):
        return {"kind": "points", "lat": latitude, "lon": longitude}

    country = _country_column(frame, config or {})
    if country:
        return {"kind": "countries", "country": country[0], "locationmode": country[1]}
    return None


def _plotly_scale(colors: list[str]) -> list[list[Any]]:
    if len(colors) == 1:
        return [[0.0, colors[0]], [1.0, colors[0]]]
    return [[index / (len(colors) - 1), color] for index, color in enumerate(colors)]


def map_chart(
    frame: Any, config: dict[str, Any], mode: str = "light"
) -> tuple[list, list[str]]:
    """Geographic points when coordinates exist, otherwise country fills."""
    detected = detect_map(frame, config)
    if not detected:
        return [], ["No valid coordinates or country column were found for a map."]

    value = _column(frame, config.get("y") or config.get("value"))
    color = _column(frame, config.get("color"))
    ramp = _plotly_scale(palette.sequential(9, mode))

    if detected["kind"] == "countries":
        country = detected["country"]
        if value and _numeric_in_range(frame, value, -math.inf, math.inf):
            grouped = frame.groupby(country, dropna=False)[value].sum().reset_index()
            measure = value
        else:
            grouped = frame.groupby(country, dropna=False).size().reset_index(name="_count")
            measure = "_count"
        return [
            {
                "type": "choropleth",
                "locations": grouped[country].astype(str).tolist(),
                "locationmode": detected["locationmode"],
                "z": [float(number) for number in grouped[measure].tolist()],
                "colorscale": ramp,
                "showscale": True,
                "_series_index": 0,
            }
        ], []

    latitude, longitude = detected["lat"], detected["lon"]
    groups = [None]
    warnings: list[str] = []
    if color and color not in (latitude, longitude) and color != value:
        groups = list(frame[color].dropna().unique())
        limit = palette.max_series_for("map")
        if len(groups) > limit:
            warnings.append(f"{len(groups)} groups; showing the first {limit}.")
            groups = groups[:limit]

    traces = []
    for index, group in enumerate(groups):
        subset = frame if group is None else frame[frame[color] == group]
        marker: dict[str, Any] = {"size": 10}
        if value and value not in (latitude, longitude) and _numeric_in_range(
            subset, value, -math.inf, math.inf
        ):
            marker.update(
                {
                    "color": _series(subset, value),
                    "colorscale": ramp,
                    "showscale": group is None or index == 0,
                }
            )
        traces.append(
            {
                "type": "scattergeo",
                "mode": "markers",
                "lat": _series(subset, latitude),
                "lon": _series(subset, longitude),
                "name": str(group) if group is not None else None,
                "marker": marker,
                "_series_index": index,
            }
        )
    return traces, warnings


# --------------------------------------------------------------------------
# Funnel
# --------------------------------------------------------------------------


def funnel(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Ordered stages with drop-off.

    Stages are *ordinal*, so they take a one-hue ramp rather than categorical
    colours: the reader should see the order in the colour. A funnel whose
    stages are not monotonically decreasing is nearly always a data error, so
    it is called out rather than drawn silently.
    """
    warnings: list[str] = []
    stage = _column(frame, config.get("x") or config.get("stage"))
    value = _column(frame, config.get("y") or config.get("value"))

    if not stage or not value:
        return [], ["A funnel needs a stage column and a measure."]

    grouped = frame.groupby(stage, dropna=False)[value].sum()

    # Respect an explicit order if one was given; otherwise assume the frame
    # is already in stage order rather than sorting by size, which would
    # invent a funnel out of unordered categories.
    order = config.get("stage_order")
    if isinstance(order, list) and order:
        grouped = grouped.reindex([s for s in order if s in grouped.index])
    else:
        seen: list[Any] = []
        for name in frame[stage]:
            if name not in seen:
                seen.append(name)
        grouped = grouped.reindex([s for s in seen if s in grouped.index])

    if len(grouped) > MAX_FUNNEL_STAGES:
        warnings.append(f"{len(grouped)} stages is too many; showing the first {MAX_FUNNEL_STAGES}.")
        grouped = grouped.head(MAX_FUNNEL_STAGES)

    values = [float(v) for v in grouped.tolist()]
    if any(values[i] < values[i + 1] for i in range(len(values) - 1)):
        warnings.append(
            "Stages do not decrease. Either the order is wrong or a later "
            "stage counts something the earlier one does not."
        )

    ramp = palette.sequential(max(2, len(values)), mode, ordinal=True)
    face = palette.surface(mode)

    trace = {
        "type": "funnel",
        "y": [str(s) for s in grouped.index],
        "x": values,
        "marker": {
            "color": ramp[: len(values)],
            "line": {"color": face.background, "width": 2},
        },
        "textinfo": "value+percent initial",
        "_series_index": 0,
    }
    return [trace], warnings


# --------------------------------------------------------------------------
# Waterfall
# --------------------------------------------------------------------------


def waterfall(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Contributions that build to a total.

    Direction carries meaning here, so this is the one form that uses status
    colours: increases green, decreases red, totals neutral. They ship with a
    sign in the label, so the meaning is never colour alone.
    """
    warnings: list[str] = []
    label = _column(frame, config.get("x") or config.get("label"))
    value = _column(frame, config.get("y") or config.get("value"))

    if not label or not value:
        return [], ["A waterfall needs a label column and a measure."]

    grouped = frame.groupby(label, dropna=False)[value].sum()

    seen: list[Any] = []
    for name in frame[label]:
        if name not in seen:
            seen.append(name)
    grouped = grouped.reindex([s for s in seen if s in grouped.index])

    values = [float(v) for v in grouped.tolist()]
    labels = [str(s) for s in grouped.index]

    explicit = config.get("measure_kinds")
    if isinstance(explicit, list) and len(explicit) == len(values):
        measures = [str(m) for m in explicit]
    else:
        measures = ["relative"] * len(values)
        if len(measures) > 1:
            # The last bar is almost always the total being built to.
            measures[-1] = "total"

    face = palette.surface(mode)
    good = palette.STATUS["good"][mode]
    bad = palette.STATUS["critical"][mode]

    trace = {
        "type": "waterfall",
        "x": labels,
        "y": values,
        "measure": measures,
        "increasing": {"marker": {"color": good}},
        "decreasing": {"marker": {"color": bad}},
        "totals": {"marker": {"color": palette.series_color(0, mode)}},
        "connector": {"line": {"color": face.axis, "width": 1}},
        "text": [f"{'+' if v >= 0 else ''}{v:,.0f}" for v in values],
        "textposition": "outside",
        "_series_index": 0,
    }
    return [trace], warnings


# --------------------------------------------------------------------------
# Box
# --------------------------------------------------------------------------


def box(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Distribution per group.

    One trace per group rather than a single trace with an x column: it is the
    only way each group gets its own palette slot and its own legend entry.
    """
    warnings: list[str] = []
    value = _column(frame, config.get("y") or config.get("value"))
    group = _column(frame, config.get("x") or config.get("color"))

    if not value:
        return [], ["A box plot needs a measure."]

    face = palette.surface(mode)

    if not group:
        return [
            {
                "type": "box",
                "y": _series(frame, value),
                "name": value,
                "marker": {"color": palette.series_color(0, mode)},
                "line": {"color": palette.series_color(0, mode)},
                "boxpoints": "outliers",
                "_series_index": 0,
            }
        ], warnings

    groups = list(frame[group].dropna().unique())
    limit = palette.max_series_for("box")
    if len(groups) > limit:
        warnings.append(
            f"{len(groups)} groups; showing the {limit} largest by median."
        )
        medians = frame.groupby(group)[value].median().sort_values(ascending=False)
        groups = list(medians.head(limit).index)

    traces = []
    for index, name in enumerate(groups):
        subset = frame[frame[group] == name]
        colour = palette.series_color(index, mode)
        traces.append(
            {
                "type": "box",
                "y": subset[value].tolist(),
                "name": str(name),
                "marker": {"color": colour},
                "line": {"color": colour},
                "fillcolor": _rgba(colour, 0.18),
                "boxpoints": "outliers",
                "_series_index": index,
            }
        )
    return traces, warnings


def _rgba(hex_color: str, alpha: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


BUILDERS = {
    "sankey": sankey,
    "treemap": treemap,
    "sunburst": sunburst,
    "bubble": bubble,
    "map": map_chart,
    "funnel": funnel,
    "waterfall": waterfall,
    "box": box,
}


def build(
    kind: str, frame: Any, config: dict[str, Any], mode: str = "light"
) -> tuple[list, list[str]]:
    builder = BUILDERS.get(kind)
    if builder is None:
        return [], [f"'{kind}' has no builder."]
    try:
        return builder(frame, config, mode)
    except Exception as exc:  # noqa: BLE001 - a bad shape must not 500
        log.exception("could not build %s", kind)
        return [], [f"Could not draw a {kind}: {exc}"]
