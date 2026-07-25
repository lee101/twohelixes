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
from typing import Any

from twohelixes.charts import palette

log = logging.getLogger("twohelixes.charts.forms")

# Past this, a sankey is a hairball and a treemap's labels stop fitting.
MAX_SANKEY_LINKS = 60
MAX_TREEMAP_NODES = 120
MAX_FUNNEL_STAGES = 12


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
# Treemap
# --------------------------------------------------------------------------


def treemap(frame: Any, config: dict[str, Any], mode: str = "light") -> tuple[list, list[str]]:
    """Part-to-whole with optional nesting.

    Plotly's treemap wants a flat parent/child table, so a two-level grouping
    has to be flattened with synthetic parent rows. Getting that wrong shows
    up as tiles that do not sum to their parent.
    """
    warnings: list[str] = []
    category = _column(frame, config.get("x") or config.get("category"))
    value = _column(frame, config.get("y") or config.get("value"))
    parent = _column(frame, config.get("color") or config.get("parent"))

    if not category:
        return [], ["A treemap needs a category column."]

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
            f"{len(labels)} tiles is too many to label; showing the largest "
            f"{MAX_TREEMAP_NODES}."
        )
        keep = sorted(range(len(values)), key=lambda i: values[i], reverse=True)[
            :MAX_TREEMAP_NODES
        ]
        keep_set = set(keep)
        labels = [l for i, l in enumerate(labels) if i in keep_set]
        parents = [p for i, p in enumerate(parents) if i in keep_set]
        values = [v for i, v in enumerate(values) if i in keep_set]

    face = palette.surface(mode)
    trace = {
        "type": "treemap",
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
