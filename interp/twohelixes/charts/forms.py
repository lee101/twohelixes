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
import html
import math
import re
from typing import Any

from twohelixes.charts import geo, palette

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
    if color and color not in (detected.get("lat"), detected.get("lon")) and _numeric_in_range(frame, color, -math.inf, math.inf):
        value, color = color, None
    ramp = _plotly_scale(palette.sequential(9, mode))

    if detected["kind"] == "countries":
        country = detected["country"]
        if value and _numeric_in_range(frame, value, -math.inf, math.inf):
            grouped = frame.groupby(country, dropna=False)[value].sum().reset_index()
            measure = value
        else:
            grouped = frame.groupby(country, dropna=False).size().reset_index(name="_count")
            measure = "_count"
        raw_locations = grouped[country].astype(str).tolist()
        codes, unmatched = geo.locations_to_iso3(raw_locations)
        warnings: list[str] = []
        if unmatched:
            warnings.append(
                f"{unmatched} location(s) could not be matched to a country code."
            )
        if not codes:
            return [], ["No country codes could be resolved for a map."]
        # Re-align z with the matched locations only.
        z_values: list[float] = []
        kept_codes: list[str] = []
        for location, number in zip(raw_locations, grouped[measure].tolist()):
            code = geo.to_iso3(location)
            if code is None:
                continue
            kept_codes.append(code)
            z_values.append(float(number))
        return [
            {
                "type": "choropleth",
                "locations": kept_codes,
                "locationmode": "ISO-3",
                "z": z_values,
                "colorscale": ramp,
                "showscale": True,
                "autocolorscale": False,
                "_series_index": 0,
            }
        ], warnings

    latitude, longitude = detected["lat"], detected["lon"]
    import pandas as pd
    frame = frame.copy()
    frame[latitude] = pd.to_numeric(frame[latitude], errors="coerce")
    frame[longitude] = pd.to_numeric(frame[longitude], errors="coerce")
    valid = frame[latitude].between(-90, 90) & frame[longitude].between(-180, 180)
    omitted = int((~valid).sum())
    frame = frame[valid]
    groups = [None]
    warnings = [f"{omitted} rows have missing or invalid coordinates."] if omitted else []
    if color and color not in (latitude, longitude) and color != value:
        groups = list(frame[color].dropna().unique())
        limit = palette.max_series_for("map")
        if len(groups) > limit:
            warnings.append(f"{len(groups)} groups; showing {limit} named groups and Other.")
            kept = groups[:limit]
            frame[color] = frame[color].where(frame[color].isin(kept), palette.OTHER_LABEL)
            groups = kept + [palette.OTHER_LABEL]
        if frame[color].isna().any():
            frame[color] = frame[color].fillna(palette.OTHER_LABEL)
            if palette.OTHER_LABEL not in groups:
                groups.append(palette.OTHER_LABEL)

    traces = []
    for index, group in enumerate(groups):
        subset = frame if group is None else frame[frame[color] == group]
        marker: dict[str, Any] = {"size": 6 if len(frame) > 500 else 10}
        measured = value and value not in (latitude, longitude) and _numeric_in_range(
            subset, value, -math.inf, math.inf
        )
        if measured:
            marker.update(
                {
                    "color": _series(subset, value),
                    "colorscale": ramp,
                    "showscale": group is None or index == 0,
                }
            )
        traces.append(
            {
                "type": "scattermap",
                "mode": "markers",
                "lat": _series(subset, latitude),
                "lon": _series(subset, longitude),
                "name": str(group) if group is not None else None,
                "marker": marker,
                "text": ["<br>".join(html.escape(str(row[c])) for c in
                         ("school_name", "name", "address", "suburb", "postcode")
                         if c in subset.columns and pd.notna(row[c]))
                         for _, row in subset.iterrows()],
                "hovertemplate": "%{text}<br>%{lat:.5f}, %{lon:.5f}"
                    + (f"<br>{html.escape(value)}: %{{marker.color}}" if measured else "")
                    + "<extra>%{fullData.name}</extra>",
                "_series_index": index,
            }
        )
    missing_trace = {"type": "scattermap", "mode": "markers", "name": "No selected score",
                     "lat": [], "lon": [], "text": [], "marker": {"size": 5, "opacity": 0.45}, "_neutral": True,
                     "hovertemplate": "%{text}<br>Selected score unavailable<extra></extra>"}
    for trace in traces:
        colors = trace["marker"].get("color")
        if not isinstance(colors, list):
            continue
        good = [i for i, v in enumerate(colors) if isinstance(v, (int, float)) and math.isfinite(v)]
        good_set = set(good)
        bad = [i for i in range(len(colors)) if i not in good_set]
        for key in ("lat", "lon", "text"):
            missing_trace[key].extend(trace[key][i] for i in bad)
            trace[key] = [trace[key][i] for i in good]
        trace["marker"]["color"] = [colors[i] for i in good]
    traces = [t for t in traces if t["lat"]]
    if missing_trace["lat"]:
        for trace in traces:
            trace["marker"]["size"] = 7
        traces.insert(0, missing_trace)
    return traces, warnings


# --------------------------------------------------------------------------
# Word cloud
# --------------------------------------------------------------------------


_STOP = frozenset(
    """
    a an the and or but if in on at to for of from by with without about into
    through during before after above below between under again further then
    once here there when where why how all each few more most other some such
    no nor not only own same so than too very can will just should now is are
    was were be been being it its this that these those i you he she we they
    """.split()
)


def _text_blob(frame: Any, column: str | None) -> str:
    if column and column in {str(c) for c in frame.columns}:
        return " ".join(str(value) for value in frame[column].dropna().tolist())
    parts: list[str] = []
    for name in frame.columns:
        series = frame[name]
        if getattr(series.dtype, "kind", "") in "OUS":
            parts.extend(str(value) for value in series.dropna().tolist())
    return " ".join(parts)


def _word_frequencies(text: str) -> dict[str, float]:
    counts: dict[str, int] = {}
    for raw in re.findall(r"[A-Za-z][A-Za-z'-]{1,}", text.casefold()):
        if raw in _STOP or len(raw) < 3:
            continue
        counts[raw] = counts.get(raw, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:80]
    return {word: float(count) for word, count in ranked}


def wordcloud(
    frame: Any, config: dict[str, Any], mode: str = "light"
) -> tuple[list, list[str]]:
    """Frequency cloud of free text. Falls back to a bar if there is no text."""
    text_col = _column(frame, config.get("x") or config.get("text"))
    blob = _text_blob(frame, text_col)
    frequencies = _word_frequencies(blob)
    if len(frequencies) < 3:
        return [], ["Not enough distinct words for a word cloud."]

    face = palette.surface(mode)
    try:
        from wordcloud import WordCloud

        image = WordCloud(
            background_color=face.background,
            max_words=120,
            width=1280,
            height=720,
            prefer_horizontal=0.85,
            relative_scaling=0.45,
            colormap="Blues" if mode == "light" else "Blues_r",
        ).generate_from_frequencies(frequencies)
        import base64
        from io import BytesIO

        buffer = BytesIO()
        image.to_image().save(buffer, format="PNG")
        payload = base64.b64encode(buffer.getvalue()).decode("ascii")
        return [
            {
                "type": "image",
                "source": f"data:image/png;base64,{payload}",
                "xref": "paper",
                "yref": "paper",
                "x": 0,
                "y": 1,
                "sizex": 1,
                "sizey": 1,
                "sizing": "stretch",
                "layer": "below",
                "_series_index": 0,
            }
        ], []
    except Exception as exc:  # noqa: BLE001 - optional dependency / render
        log.info("wordcloud image path unavailable (%s); using text marks", exc)

    words = list(frequencies.keys())
    weights = list(frequencies.values())
    peak = max(weights) or 1.0
    sizes = [12 + 28 * (weight / peak) for weight in weights]
    # Deterministic scatter so the same question redraws the same cloud.
    xs = [(index % 10) + (index % 3) * 0.15 for index in range(len(words))]
    ys = [(index // 10) + (index % 5) * 0.08 for index in range(len(words))]
    return [
        {
            "type": "scatter",
            "mode": "text",
            "x": xs,
            "y": ys,
            "text": words,
            "textfont": {
                "size": sizes,
                "color": palette.categorical(mode)[0],
            },
            "hovertext": [f"{word}: {int(weight)}" for word, weight in frequencies.items()],
            "hoverinfo": "text",
            "_series_index": 0,
        }
    ], ["Drew words as text marks because the image renderer was unavailable."]


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
# Gantt
# --------------------------------------------------------------------------

MAX_GANTT_TASKS = 80
_TASK_HINTS = ("task", "activity", "name", "phase", "milestone", "project", "job", "item", "title", "step")
_START_HINTS = ("start", "begin", "from", "kickoff", "launch")
_END_HINTS = ("end", "finish", "due", "deadline", "until", "to")
_DURATION_HINTS = ("duration", "days", "length", "weeks", "effort", "elapsed")
_GROUP_HINTS = ("resource", "team", "owner", "assignee", "status", "category", "group", "department", "stage", "type")
_PROGRESS_HINTS = ("progress", "percent", "pct", "done")


def _hinted(columns: list[str], hints: tuple[str, ...], skip: set[str]) -> str | None:
    for hint in hints:
        for name in columns:
            if name in skip:
                continue
            lowered = name.lower().replace("_", " ")
            if hint in lowered.split() or (len(hint) > 3 and hint in lowered):
                return name
    return None


def _as_time(series: Any) -> tuple[Any, bool]:
    """(converted, is_datetime). Plain numbers stay numeric: week 0..12 is a schedule too."""
    import pandas as pd

    if pd.api.types.is_datetime64_any_dtype(series):
        return series, True
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce"), False
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    if len(series) and parsed.notna().mean() >= 0.6:
        return parsed, True
    return pd.to_numeric(series, errors="coerce"), False


def looks_like_schedule(frame: Any) -> bool:
    """True for a task column plus named start and end columns (not just any two dates)."""
    try:
        names = [str(c) for c in frame.columns]
        start = _hinted(names, _START_HINTS, set())
        end = _hinted(names, _END_HINTS, {start or ""})
        task = _hinted([n for n in names if n not in (start, end)], _TASK_HINTS, set())
        return bool(start and end and task and len(frame) >= 1)
    except Exception:  # noqa: BLE001 - detection must never break chart selection
        return False


def gantt(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Tasks as horizontal bars from start to end.

    Plotly has no gantt trace; the form is a horizontal bar with `base` set to
    the start, which is also how `px.timeline` draws one. Channels: x is the
    task, y/start the start, z/end the end (or duration), color the grouping.
    Zero-length tasks are milestones and get a diamond, not an invisible bar.
    A progress column shades the finished part, so completion is never colour
    alone - it is length.
    """
    import pandas as pd

    warnings: list[str] = []
    if frame is None or len(frame) == 0:
        return [], ["A gantt chart needs rows with a start and an end."]
    names = [str(c) for c in frame.columns]
    numeric = {n for n in names if pd.api.types.is_numeric_dtype(frame[n])}

    def cfg(*keys: str) -> str | None:
        for key in keys:
            value = config.get(key)
            if isinstance(value, str) and (col := _column(frame, value)):
                return col
        return None

    def timelike(name: str) -> bool:
        return _as_time(frame[name])[0].notna().mean() >= 0.6

    task = cfg("task", "x", "label")
    start = cfg("start")
    end = cfg("end")
    duration = cfg("duration")
    group = cfg("group", "color")
    progress = cfg("progress")

    if start is None and (cand := cfg("y")) and timelike(cand):
        start = cand
    if end is None and (cand := cfg("z")) and timelike(cand) and cand != start:
        end = cand
    start = start or _hinted(names, _START_HINTS, {end or ""})
    end = end or _hinted(names, _END_HINTS, {start or ""})
    if start is None:
        dated = [n for n in names if n not in numeric and timelike(n)]
        start = dated[0] if dated else None
        if end is None and len(dated) > 1:
            end = dated[1]
    if start is None:
        pool = [n for n in names if n in numeric]
        start = pool[0] if pool else None
    if start is None:
        return [], ["A gantt chart needs a start column (a date or a number)."]
    if end is None and duration is None:
        duration = _hinted([n for n in names if n in numeric and n != start], _DURATION_HINTS, set())
    if end is None and duration is None:
        pool = [n for n in names if n in numeric and n != start]
        duration = pool[0] if pool else None
    if end is None and duration is None:
        return [], ["A gantt chart needs an end or a duration column."]

    used = {start, end, duration}
    text = [n for n in names if n not in used and n not in numeric]
    task = task if task not in used else None
    task = task or _hinted(text, _TASK_HINTS, set()) or (text[0] if text else None)
    used.add(task)
    group = group if group not in used else None
    group = group or _hinted([n for n in names if n not in used and n not in numeric], _GROUP_HINTS, set())
    progress = progress or _hinted([n for n in names if n in numeric and n not in used], _PROGRESS_HINTS, set())

    work = pd.DataFrame(index=frame.index)
    work["task"] = frame[task].astype(str) if task else [f"Task {i + 1}" for i in range(len(frame))]
    t_start, is_date = _as_time(frame[start])
    work["start"] = t_start
    if end is not None:
        t_end, end_is_date = _as_time(frame[end])
        if end_is_date != is_date:
            return [], ["A gantt chart's start and end must both be dates or both be numbers."]
        work["end"] = t_end
    else:
        length = pd.to_numeric(frame[duration], errors="coerce")
        work["end"] = t_start + (pd.to_timedelta(length, unit="D") if is_date else length)
    work["group"] = frame[group].astype(str) if group else None
    if progress:
        share = pd.to_numeric(frame[progress], errors="coerce")
        if share.max() > 1.0:
            share = share / 100.0
        work["progress"] = share.clip(0, 1).fillna(0.0)

    rows = len(work)
    work = work.dropna(subset=["start", "end"])
    if rows != len(work):
        warnings.append(f"{rows - len(work)} task(s) had no usable start or end and were left out.")
    swapped = work["end"] < work["start"]
    if swapped.any():
        work.loc[swapped, ["start", "end"]] = work.loc[swapped, ["end", "start"]].to_numpy()
        warnings.append(f"{int(swapped.sum())} task(s) ended before they started; the dates were swapped.")
    work = work.sort_values(["start", "end"], kind="stable")
    if len(work) > MAX_GANTT_TASKS:
        warnings.append(f"{len(work)} tasks is unreadable; showing the first {MAX_GANTT_TASKS} by start.")
        work = work.head(MAX_GANTT_TASKS)
    if work.empty:
        return [], ["No task had a valid start and end."]

    span = work["end"] - work["start"]
    width = span.dt.total_seconds() * 1000.0 if is_date else span.astype(float)

    def at(values: Any) -> list[Any]:
        return values.dt.strftime("%Y-%m-%d %H:%M:%S").tolist() if is_date else values.tolist()

    order = work["task"].tolist()
    tags = list(dict.fromkeys(work["group"])) if group else [None]
    limit = palette.MAX_SERIES
    if len(tags) > limit:
        warnings.append(f"{len(tags)} groups; the {len(tags) - limit + 1} smallest are shown as 'Other'.")
        biggest = work["group"].value_counts().index[: limit - 1].tolist()
        work["group"] = work["group"].where(work["group"].isin(biggest), palette.OTHER_LABEL)
        tags = list(dict.fromkeys(work["group"]))

    traces: list[dict[str, Any]] = []
    for index, tag in enumerate(tags):
        part = work if tag is None else work[work["group"] == tag]
        colour = palette.series_color(index, mode)
        other = tag == palette.OTHER_LABEL
        w = width.loc[part.index]
        bars = part[w > 0]
        name = "" if tag is None else str(tag)
        if len(bars):
            bw = width.loc[bars.index]
            has_progress = "progress" in bars
            traces.append({
                "type": "bar",
                "orientation": "h",
                "y": bars["task"].tolist(),
                "x": bw.tolist(),
                "base": at(bars["start"]),
                "name": name,
                "marker": {"opacity": 0.5 if has_progress else 1.0},
                "customdata": list(zip(at(bars["start"]), at(bars["end"]))),
                "hovertemplate": "%{y}<br>%{customdata[0]} to %{customdata[1]}<extra>%{fullData.name}</extra>",
                "_series_index": index,
                "_neutral": other,
                "_gantt": "date" if is_date else "number",
            })
            if has_progress:
                traces.append({
                    "type": "bar",
                    "orientation": "h",
                    "y": bars["task"].tolist(),
                    "x": (bw * bars["progress"]).tolist(),
                    "base": at(bars["start"]),
                    "name": name,
                    "showlegend": False,
                    "hoverinfo": "skip",
                    "_layer": True,
                    "_series_index": index,
                    "_neutral": other,
                    "_gantt": "date" if is_date else "number",
                })
        miles = part[w <= 0]
        if len(miles):
            traces.append({
                "type": "scatter",
                "mode": "markers",
                "y": miles["task"].tolist(),
                "x": at(miles["start"]),
                "name": f"{name} milestone".strip() if len(bars) else (name or "Milestone"),
                "showlegend": not len(bars) and bool(name),
                "marker": {"symbol": "diamond", "size": 14, "color": colour},
                "hovertemplate": "%{y}<br>%{x}<extra>milestone</extra>",
                "_layer": bool(len(bars)),
                "_series_index": index,
                "_neutral": other,
                "_gantt": "date" if is_date else "number",
            })
    if traces:
        traces[0]["_gantt_order"] = order
    return traces, warnings


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
    "gantt": gantt,
    "box": box,
    "wordcloud": wordcloud,
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
