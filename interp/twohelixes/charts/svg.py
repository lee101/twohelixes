"""Our own SVG exporter.

Plotly's own SVG export goes through Kaleido, which means spawning a headless
browser per render: slow, memory-hungry, and a hard dependency on a Chromium
build matching the box. For the chart forms this product actually produces,
emitting SVG directly is faster by orders of magnitude, deterministic, and
produces markup a designer can open and edit.

Kaleido is still used for PNG, where rasterising by hand would be worse. Forms
this module cannot draw fall back to it too, so export never fails outright.
"""

from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass
from typing import Any

from twohelixes.charts import palette

log = logging.getLogger("twohelixes.charts.svg")

SUPPORTED = frozenset({"bar", "hbar", "line", "area", "scatter", "pie"})

FONT = "Inter, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


def _themed(face: palette.Surface, mode: str, theme_vars: bool) -> palette.Surface:
    """Swap literal colours for CSS custom properties.

    An inline SVG inherits the page's custom properties, so emitting
    `var(--text-primary, #0b0b0b)` makes one rendering correct in both themes
    without JavaScript and without shipping the chart twice. The literal stays
    as the fallback, so a downloaded standalone file still looks right.
    """
    if not theme_vars:
        return face
    return palette.Surface(
        background=f"var(--panel, {face.background})",
        text_primary=f"var(--text-primary, {face.text_primary})",
        text_secondary=f"var(--text-secondary, {face.text_secondary})",
        text_muted=f"var(--text-muted, {face.text_muted})",
        grid=f"var(--grid, {face.grid})",
        axis=f"var(--axis, {face.axis})",
        ring=f"var(--panel, {face.ring})",
    )


def _themed_series(index: int, literal: str, theme_vars: bool) -> str:
    if not theme_vars:
        return literal
    if index >= palette.MAX_SERIES:
        return f"var(--series-other, {literal})"
    return f"var(--series-{index + 1}, {literal})"


@dataclass
class Box:
    width: float
    height: float
    top: float = 56
    right: float = 28
    bottom: float = 56
    left: float = 68

    @property
    def plot_width(self) -> float:
        return max(1.0, self.width - self.left - self.right)

    @property
    def plot_height(self) -> float:
        return max(1.0, self.height - self.top - self.bottom)


def render(
    figure: dict[str, Any],
    *,
    width: int = 900,
    height: int = 520,
    mode: str = "light",
    transparent: bool = False,
    theme_vars: bool = False,
) -> str:
    """Render a Plotly figure dict as standalone SVG.

    `transparent` omits the background rect so the chart takes the colour of
    whatever frames it. Without it, a chart embedded in a panel draws its own
    surface and reads as a box inside a box.
    """
    traces = [t for t in (figure.get("data") or []) if t.get("type") != "table"]
    layout = figure.get("layout") or {}
    if not traces:
        return _empty(width, height, mode, "No data")

    kind = _kind(traces[0])
    if kind not in SUPPORTED:
        raise Unsupported(kind)

    face = _themed(palette.surface(mode), mode, theme_vars)
    box = Box(float(width), float(height))
    if not _title(layout):
        box.top = 24

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">'
    ]
    if not transparent:
        parts.append(
            f'<rect width="{width}" height="{height}" fill="{face.background}"/>'
        )

    title = _title(layout)
    if title:
        parts.append(
            f'<text x="{box.left - 12}" y="30" font-size="16" font-weight="600" '
            f'fill="{face.text_primary}">{html.escape(title)}</text>'
        )

    if theme_vars:
        traces = [dict(t) for t in traces]
        for position, trace in enumerate(traces):
            index = int(trace.get("_series_index", position))
            literal = str(trace.get("_color") or palette.series_color(index, mode))
            trace["_color"] = _themed_series(index, literal, True)

    if kind == "pie":
        parts.extend(_pie(traces[0], box, face, mode))
    else:
        parts.extend(_cartesian(traces, kind, box, face, layout, mode))

    parts.append("</svg>")
    return "".join(parts)


class Unsupported(Exception):
    pass


def _kind(trace: dict[str, Any]) -> str:
    kind = str(trace.get("type") or "scatter")
    if kind == "bar":
        return "hbar" if trace.get("orientation") == "h" else "bar"
    if kind in ("scatter", "scattergl"):
        if trace.get("fill"):
            return "area"
        mode = str(trace.get("mode") or "lines")
        return "scatter" if "markers" in mode and "lines" not in mode else "line"
    return kind


def _title(layout: dict[str, Any]) -> str:
    title = layout.get("title")
    if isinstance(title, dict):
        return str(title.get("text") or "")
    return str(title or "")


def _axis_title(layout: dict[str, Any], axis: str) -> str:
    node = layout.get(axis) or {}
    title = node.get("title")
    if isinstance(title, dict):
        return str(title.get("text") or "")
    return str(title or "")


def _numbers(values: Any) -> list[float]:
    out: list[float] = []
    for value in values or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = math.nan
        out.append(number)
    return out


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """Round tick values, so an axis reads 0/25/50 rather than 0/23.7/47.4."""
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        return [low, high] if high > low else [0.0, 1.0]

    span = high - low
    raw = span / max(1, count)
    magnitude = 10 ** math.floor(math.log10(raw))
    for multiple in (1, 2, 2.5, 5, 10):
        step = magnitude * multiple
        if raw <= step:
            break

    start = math.floor(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step * 0.5 and len(ticks) < 20:
        ticks.append(round(value, 10))
        value += step
    return ticks


def _format_tick(value: float) -> str:
    magnitude = abs(value)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if magnitude >= threshold:
            trimmed = f"{value / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{trimmed}{suffix}"
    if magnitude and magnitude < 1:
        return f"{value:.3g}"
    return f"{value:,.0f}"


def _cartesian(
    traces: list[dict[str, Any]],
    kind: str,
    box: Box,
    face: palette.Surface,
    layout: dict[str, Any],
    mode: str,
) -> list[str]:
    horizontal = kind == "hbar"

    categories: list[str] = []
    for trace in traces:
        source = trace.get("y") if horizontal else trace.get("x")
        for value in source or []:
            label = str(value)
            if label not in categories:
                categories.append(label)

    series_values: list[list[float]] = []
    for trace in traces:
        series_values.append(_numbers(trace.get("x") if horizontal else trace.get("y")))

    flat = [v for values in series_values for v in values if math.isfinite(v)]
    if not flat:
        return [_empty_text(box, face, "No numeric values")]

    low = min(0.0, min(flat))
    high = max(flat)
    if high == low:
        high = low + 1.0

    ticks = _nice_ticks(low, high)
    low, high = min(low, ticks[0]), max(high, ticks[-1])

    parts: list[str] = []

    def value_pos(value: float) -> float:
        fraction = (value - low) / (high - low)
        if horizontal:
            return box.left + fraction * box.plot_width
        return box.top + box.plot_height - fraction * box.plot_height

    # Gridlines: hairline, solid, one step off the surface.
    for tick in ticks:
        position = value_pos(tick)
        if horizontal:
            parts.append(
                f'<line x1="{position:.1f}" y1="{box.top}" x2="{position:.1f}" '
                f'y2="{box.top + box.plot_height}" stroke="{face.grid}" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{position:.1f}" y="{box.top + box.plot_height + 18}" '
                f'font-size="11" fill="{face.text_muted}" text-anchor="middle">'
                f"{_format_tick(tick)}</text>"
            )
        else:
            parts.append(
                f'<line x1="{box.left}" y1="{position:.1f}" '
                f'x2="{box.left + box.plot_width}" y2="{position:.1f}" '
                f'stroke="{face.grid}" stroke-width="1"/>'
            )
            parts.append(
                f'<text x="{box.left - 10}" y="{position + 4:.1f}" font-size="11" '
                f'fill="{face.text_muted}" text-anchor="end">{_format_tick(tick)}</text>'
            )

    slot = (box.plot_height if horizontal else box.plot_width) / max(1, len(categories))

    if kind in ("bar", "hbar"):
        parts.extend(
            _bars(traces, series_values, categories, box, face, slot, horizontal, value_pos, low)
        )
    else:
        parts.extend(
            _lines(traces, series_values, categories, box, face, slot, kind, value_pos, mode)
        )

    parts.extend(_category_labels(categories, box, face, slot, horizontal))

    axis_label = _axis_title(layout, "xaxis")
    if axis_label:
        parts.append(
            f'<text x="{box.left + box.plot_width / 2:.1f}" y="{box.height - 12}" '
            f'font-size="12" fill="{face.text_secondary}" text-anchor="middle">'
            f"{html.escape(axis_label)}</text>"
        )

    if len([t for t in traces if t.get("name")]) >= 2:
        parts.extend(_legend(traces, box, face))

    return parts


def _bars(
    traces: list[dict[str, Any]],
    series_values: list[list[float]],
    categories: list[str],
    box: Box,
    face: palette.Surface,
    slot: float,
    horizontal: bool,
    value_pos: Any,
    low: float,
) -> list[str]:
    parts: list[str] = []
    count = max(1, len(traces))
    # Cap thickness and leave the leftover as air, rather than filling the slot.
    thickness = min(24.0, (slot * 0.7) / count)
    baseline = value_pos(max(0.0, low))

    for series_index, (trace, values) in enumerate(zip(traces, series_values)):
        color = trace.get("_color") or palette.series_color(series_index)
        for index, value in enumerate(values):
            if not math.isfinite(value) or index >= len(categories):
                continue
            centre = (
                box.top + slot * (index + 0.5) if horizontal else box.left + slot * (index + 0.5)
            )
            offset = (series_index - (count - 1) / 2) * thickness
            position = value_pos(value)

            if horizontal:
                x = min(baseline, position)
                width = abs(position - baseline)
                y = centre + offset - thickness / 2
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" '
                    f'height="{thickness:.1f}" fill="{color}" rx="4" '
                    f'stroke="{face.background}" stroke-width="2"/>'
                )
            else:
                y = min(baseline, position)
                height = abs(position - baseline)
                x = centre + offset - thickness / 2
                parts.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{thickness:.1f}" '
                    f'height="{height:.1f}" fill="{color}" rx="4" '
                    f'stroke="{face.background}" stroke-width="2"/>'
                )
    return parts


def _lines(
    traces: list[dict[str, Any]],
    series_values: list[list[float]],
    categories: list[str],
    box: Box,
    face: palette.Surface,
    slot: float,
    kind: str,
    value_pos: Any,
    mode: str,
) -> list[str]:
    parts: list[str] = []
    for series_index, (trace, values) in enumerate(zip(traces, series_values)):
        color = trace.get("_color") or palette.series_color(series_index, mode)
        points: list[tuple[float, float]] = []
        for index, value in enumerate(values):
            if not math.isfinite(value):
                continue
            x = box.left + slot * (index + 0.5)
            points.append((x, value_pos(value)))

        if not points:
            continue

        if kind == "scatter":
            for x, y in points:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" '
                    f'stroke="{face.background}" stroke-width="2"/>'
                )
            continue

        path = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in points)
        if kind == "area":
            baseline = box.top + box.plot_height
            fill = (
                f"{path} L {points[-1][0]:.1f} {baseline:.1f} "
                f"L {points[0][0]:.1f} {baseline:.1f} Z"
            )
            parts.append(f'<path d="{fill}" fill="{color}" fill-opacity="0.10"/>')

        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        # An end dot anchors the series to its direct label.
        parts.append(
            f'<circle cx="{points[-1][0]:.1f}" cy="{points[-1][1]:.1f}" r="4" '
            f'fill="{color}" stroke="{face.background}" stroke-width="2"/>'
        )
    return parts


MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)

# Rough advance width of the 11px label font, in px per character. Used to
# decide how many labels fit; being approximate is fine because the result is
# only ever used to thin, never to position.
CHAR_WIDTH = 6.1


def _looks_iso_date(value: str) -> bool:
    return len(value) >= 10 and value[4] == "-" and value[7] == "-"


def _format_category(label: str, all_labels: list[str]) -> str:
    """Turn ISO timestamps into something a human reads on an axis.

    A raw `2024-01-01` is 10 characters; a dozen of them collide into an
    unreadable smear. `Jan` is three, and when the series crosses a year
    boundary the year is kept so the axis stays unambiguous.
    """
    if not _looks_iso_date(label):
        return label

    years = {v[:4] for v in all_labels if _looks_iso_date(v)}
    month = MONTHS[max(0, min(11, int(label[5:7]) - 1))]
    day = label[8:10]

    # Monthly series: one label per month, so the day is noise.
    days = {v[8:10] for v in all_labels if _looks_iso_date(v)}
    if len(days) == 1:
        return f"{month} {label[2:4]}" if len(years) > 1 else month

    if len(years) > 1:
        return f"{month} {day} '{label[2:4]}"
    return f"{month} {day}"


def _category_labels(
    categories: list[str], box: Box, face: palette.Surface, slot: float, horizontal: bool
) -> list[str]:
    parts: list[str] = []
    formatted = [_format_category(c, categories) for c in categories]

    # Thin to what actually fits, rather than to a fixed divisor: a dozen
    # 10-character dates need far more room than a dozen short words.
    if horizontal:
        step = max(1, math.ceil(len(categories) / max(1, box.plot_height / 22)))
    else:
        widest = max((len(t) for t in formatted), default=1) * CHAR_WIDTH + 12
        fits = max(1, int(box.plot_width // widest))
        step = max(1, math.ceil(len(categories) / fits))

    for index, label in enumerate(formatted):
        if index % step:
            continue
        text = html.escape(label if len(label) <= 18 else label[:17] + "…")
        if horizontal:
            y = box.top + slot * (index + 0.5) + 4
            parts.append(
                f'<text x="{box.left - 10}" y="{y:.1f}" font-size="11" '
                f'fill="{face.text_muted}" text-anchor="end">{text}</text>'
            )
        else:
            x = box.left + slot * (index + 0.5)
            parts.append(
                f'<text x="{x:.1f}" y="{box.top + box.plot_height + 18}" font-size="11" '
                f'fill="{face.text_muted}" text-anchor="middle">{text}</text>'
            )
    return parts


def _legend(traces: list[dict[str, Any]], box: Box, face: palette.Surface) -> list[str]:
    parts: list[str] = []
    x = box.left
    y = box.top - 14
    for index, trace in enumerate(traces):
        name = str(trace.get("name") or "")
        if not name:
            continue
        color = trace.get("_color") or palette.series_color(index)
        parts.append(
            f'<rect x="{x:.1f}" y="{y - 8:.1f}" width="10" height="10" rx="2" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{x + 15:.1f}" y="{y + 1:.1f}" font-size="11" '
            f'fill="{face.text_secondary}">{html.escape(name)}</text>'
        )
        x += 24 + len(name) * 6.2
    return parts


def _pie(
    trace: dict[str, Any], box: Box, face: palette.Surface, mode: str
) -> list[str]:
    labels = [str(v) for v in (trace.get("labels") or [])]
    values = _numbers(trace.get("values"))
    total = sum(v for v in values if math.isfinite(v))
    if total <= 0:
        return [_empty_text(box, face, "No values")]

    cx = box.left + box.plot_width / 2
    cy = box.top + box.plot_height / 2
    radius = min(box.plot_width, box.plot_height) / 2 - 8

    parts: list[str] = []
    angle = -math.pi / 2
    for index, (label, value) in enumerate(zip(labels, values)):
        if not math.isfinite(value) or value <= 0:
            continue
        sweep = 2 * math.pi * value / total
        end = angle + sweep
        large = 1 if sweep > math.pi else 0

        x1, y1 = cx + radius * math.cos(angle), cy + radius * math.sin(angle)
        x2, y2 = cx + radius * math.cos(end), cy + radius * math.sin(end)
        color = palette.series_color(index, mode)

        parts.append(
            f'<path d="M {cx:.1f} {cy:.1f} L {x1:.1f} {y1:.1f} '
            f'A {radius:.1f} {radius:.1f} 0 {large} 1 {x2:.1f} {y2:.1f} Z" '
            f'fill="{color}" stroke="{face.background}" stroke-width="2"/>'
        )

        mid = angle + sweep / 2
        label_radius = radius + 18
        anchor = "start" if math.cos(mid) >= 0 else "end"
        parts.append(
            f'<text x="{cx + label_radius * math.cos(mid):.1f}" '
            f'y="{cy + label_radius * math.sin(mid) + 4:.1f}" font-size="11" '
            f'fill="{face.text_secondary}" text-anchor="{anchor}">'
            f"{html.escape(label)} {value / total * 100:.0f}%</text>"
        )
        angle = end
    return parts


def _empty(width: int, height: int, mode: str, message: str) -> str:
    face = palette.surface(mode)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'font-family="{FONT}">'
        f'<rect width="{width}" height="{height}" fill="{face.background}"/>'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" font-size="13" '
        f'fill="{face.text_muted}">{html.escape(message)}</text></svg>'
    )


def _empty_text(box: Box, face: palette.Surface, message: str) -> str:
    return (
        f'<text x="{box.width / 2:.1f}" y="{box.height / 2:.1f}" text-anchor="middle" '
        f'font-size="13" fill="{face.text_muted}">{html.escape(message)}</text>'
    )


def to_png(figure: dict[str, Any], width: int = 900, height: int = 520) -> bytes:
    """PNG export. Kaleido here, because rasterising by hand would be worse."""
    import plotly.io as pio

    return pio.to_image(figure, format="png", width=width, height=height, scale=2)


def export(
    figure: dict[str, Any],
    fmt: str = "svg",
    width: int = 900,
    height: int = 520,
    mode: str = "light",
    transparent: bool = False,
    theme_vars: bool = False,
) -> tuple[bytes, str]:
    """Return (payload, content type). Falls back to Plotly for unknown forms."""
    if fmt == "png":
        return to_png(figure, width, height), "image/png"

    try:
        markup = render(
            figure,
            width=width,
            height=height,
            mode=mode,
            transparent=transparent,
            theme_vars=theme_vars,
        )
        return markup.encode(), "image/svg+xml"
    except Unsupported as exc:
        log.info("no native SVG for %s; using plotly", exc)
        import plotly.io as pio

        payload = pio.to_image(figure, format="svg", width=width, height=height)
        return payload, "image/svg+xml"
