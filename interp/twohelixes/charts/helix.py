"""The twoHelixes mark: a procedurally generated double helix.

Two strands in exact opposition (phase offset pi), so where one is at the front
of the twist the other is at the back. Depth drives two things at once: the
opacity of the strand, and its *width*. That second one is what makes the mark
read as a solid object turning in space rather than two sine waves crossing -
a constant-width stroke has no front and no back, and at logo sizes the old
mark looked like a plaited ribbon graphic instead of a helix.

So each strand is a filled ribbon: the curve, offset along its normal by a
half-width that swells at the front of the twist and pinches at the back, and
filled with a linear gradient whose stops carry the same depth as opacity. The
depth function is periodic along the helix axis, which is exactly the direction
the gradient runs, so one path and a dozen stops replace hundreds of per-sample
segments with no banding.

Both strands are one hue - the brand blue in two tones. An earlier version used
blue and green, which made the mark the only place in the product carrying a
second brand colour.

The same generator produces the static logo and the loading spinner. The
spinner does not rotate the group; it translates the wave by exactly one
period, which reads as a helix turning and loops seamlessly because the curve
is periodic. No JavaScript, no sprite sheet: one inert SVG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from twohelixes.charts import palette

# Opacity floor and ceiling as a strand passes from back to front.
MIN_ALPHA = 0.22
MAX_ALPHA = 1.0
# Half-width at the back of the twist, as a fraction of the width at the front.
# Below about 0.3 the strand pinches to a thread and the mark looks broken at
# 24px; above 0.6 there is no depth left to read.
MIN_WIDTH_SCALE = 0.38
# Rungs only hint at the connection. Anything heavier and the mark reads as a
# ladder or a set of window blinds rather than a twist.
RUNG_ALPHA_SCALE = 0.3
# Gradient stops per turn. The depth curve is a raised cosine, so this is
# plenty to keep the fade smooth.
STOPS_PER_TURN = 12


@dataclass(frozen=True)
class HelixStyle:
    strand_a: str = palette.BRAND["light"]
    strand_b: str = palette.BRAND_LIGHT["light"]
    rung: str = palette.BRAND["light"]
    width: int = 64
    height: int = 64
    turns: float = 1.75
    amplitude: float = 0.33  # fraction of the usable cross-axis
    strand_width: float = 5.0
    rung_width: float = 1.3
    rungs_per_turn: int = 3
    vertical: bool = True
    samples_per_turn: int = 48
    # Raise for small renders: a strand that fades to 0.22 disappears at 16px.
    min_alpha: float = MIN_ALPHA
    # Fade the ribbon to a point at both ends of the axis. Right for a static
    # mark, wrong for the spinner, whose ends have to leave the frame mid-swing
    # for the loop to be seamless.
    taper_ends: bool = True


def _depth(theta: float) -> float:
    """0 at the back of the twist, 1 at the front."""
    return (math.cos(theta) + 1.0) / 2.0


def _alpha(theta: float, floor: float = MIN_ALPHA) -> float:
    return floor + (MAX_ALPHA - floor) * _depth(theta)


def _point(style: HelixStyle, t: float, phase: float, span: float, cross: float):
    """A point on a strand, plus the tangent and the phase there.

    `t` runs 0..1 along the drawn length; the cross-axis carries the sine. The
    cross-axis is inset by half the widest the ribbon gets, so a strand at full
    swing sits inside the viewBox instead of being clipped.
    """
    theta = t * style.turns * 2 * math.pi + phase
    inset = style.strand_width / 2.0 + 0.5
    usable = max(1.0, cross - 2 * inset)
    swing = style.amplitude * usable
    offset = math.sin(theta) * swing
    # d/dt of the pair. The constant on the cross term is what makes the
    # normal lean the right way where the curve is steep.
    d_cross = math.cos(theta) * swing * style.turns * 2 * math.pi
    along = t * span
    if style.vertical:
        return (cross / 2 + offset, along), (d_cross, span), theta
    return (along, cross / 2 + offset), (span, d_cross), theta


def _half_width(style: HelixStyle, t: float, theta: float) -> float:
    """Half the ribbon width: swells at the front, pinches at the back."""
    base = style.strand_width / 2.0
    half = base * (MIN_WIDTH_SCALE + (1.0 - MIN_WIDTH_SCALE) * _depth(theta))
    if style.taper_ends:
        # A short cosine ease at each end. Cutting square across the ribbon
        # leaves two blunt slabs against the frame edge; easing to nothing
        # gives the mark a silhouette.
        ease = 0.07
        edge = min(t, 1.0 - t)
        if edge < ease:
            half *= (1.0 - math.cos(math.pi * edge / ease)) / 2.0
    return half


def _ribbon_path(
    style: HelixStyle, phase: float, span: float, cross: float, turns: float
) -> str:
    """One strand as a closed polygon: out along one edge, back along the other."""
    steps = max(24, int(style.samples_per_turn * turns))
    forward: list[str] = []
    backward: list[str] = []
    for i in range(steps + 1):
        t = i / steps
        (x, y), (dx, dy), theta = _point(
            style, t * (turns / style.turns), phase, span, cross
        )
        length = math.hypot(dx, dy) or 1.0
        # Offset along the normal rather than straight across: where the curve
        # is steep a horizontal offset would fatten the ribbon instead of
        # keeping its width honest.
        nx, ny = dy / length, -dx / length
        half = _half_width(style, t, theta)
        forward.append(f"{x + nx * half:.2f} {y + ny * half:.2f}")
        backward.append(f"{x - nx * half:.2f} {y - ny * half:.2f}")
    backward.reverse()
    return "M " + " L ".join(forward + backward) + " Z"


def _gradient_stops(style: HelixStyle, phase: float, turns: float) -> list[tuple[float, float]]:
    """(offset, opacity) pairs sampling the depth curve along the axis."""
    count = max(6, int(STOPS_PER_TURN * turns))
    stops: list[tuple[float, float]] = []
    for i in range(count + 1):
        fraction = i / count
        theta = fraction * turns * 2 * math.pi + phase
        stops.append((fraction, _alpha(theta, style.min_alpha)))
    return stops


def _rungs(
    style: HelixStyle, span: float, cross: float, turns: float
) -> list[tuple[str, float]]:
    if style.rungs_per_turn <= 0:
        return []
    count = max(2, int(style.rungs_per_turn * turns))
    rungs: list[tuple[str, float]] = []
    # Half-step offsets, so no rung ever lands on t=0 or t=1. One that does
    # draws a full-width horizontal line along the frame edge, and the mark
    # grows a shelf.
    for i in range(count):
        t = (i + 0.5) / count * (turns / style.turns)
        (ax, ay), _, theta = _point(style, t, 0.0, span, cross)
        (bx, by), _, _ = _point(style, t, math.pi, span, cross)
        # Widest apart is where a rung actually communicates the twist; near a
        # crossing the two strands coincide and a rung would be a dot.
        separation = abs(math.sin(theta))
        if separation < 0.4:
            continue
        rungs.append(
            (f"M {ax:.2f} {ay:.2f} L {bx:.2f} {by:.2f}", separation * RUNG_ALPHA_SCALE)
        )
    return rungs


def render(
    style: HelixStyle | None = None,
    *,
    animated: bool = False,
    duration: float = 1.6,
    title: str = "twoHelixes",
    uid: str = "th",
) -> str:
    """Return a standalone SVG string."""
    style = style or HelixStyle()
    # An animated mark draws one extra period so the translation can loop.
    turns = style.turns + (1.0 if animated else 0.0)

    axis_length = style.height if style.vertical else style.width
    span = axis_length * (turns / style.turns)
    cross = style.width if style.vertical else style.height

    path_a = _ribbon_path(style, 0.0, span, cross, turns)
    path_b = _ribbon_path(style, math.pi, span, cross, turns)
    rungs = _rungs(style, span, cross, turns)

    gradient_vector = (
        f'x1="0" y1="0" x2="0" y2="{span:.2f}"'
        if style.vertical
        else f'x1="0" y1="0" x2="{span:.2f}" y2="0"'
    )

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {style.width} '
        f'{style.height}" width="{style.width}" height="{style.height}" '
        f'role="img" aria-label="{title}" fill="none">',
        f"<title>{title}</title>",
        "<defs>",
    ]

    for name, phase, color in (("a", 0.0, style.strand_a), ("b", math.pi, style.strand_b)):
        stops = "".join(
            f'<stop offset="{offset:.4f}" stop-color="{color}" '
            f'stop-opacity="{alpha:.3f}"/>'
            for offset, alpha in _gradient_stops(style, phase, turns)
        )
        parts.append(
            f'<linearGradient id="{uid}-{name}" gradientUnits="userSpaceOnUse" '
            f"{gradient_vector}>{stops}</linearGradient>"
        )

    parts.append(
        f'<clipPath id="{uid}-clip"><rect width="{style.width}" '
        f'height="{style.height}"/></clipPath>'
    )
    parts.append("</defs>")

    if animated:
        shift = axis_length
        translate = f"0,{-shift}" if style.vertical else f"{-shift},0"
        parts.append(
            "<style>"
            f"@keyframes {uid}-travel{{"
            "from{transform:translate(0,0)}"
            f"to{{transform:translate({translate})}}"
            "}"
            f".{uid}-spin{{animation:{uid}-travel {duration}s linear infinite}}"
            "@media (prefers-reduced-motion:reduce){"
            f".{uid}-spin{{animation-duration:{duration * 4:g}s}}}}"
            "</style>"
        )
        parts.append(f'<g clip-path="url(#{uid}-clip)"><g class="{uid}-spin">')
    else:
        parts.append(f'<g clip-path="url(#{uid}-clip)"><g>')

    # Rungs sit behind, so the strands read as passing in front of them.
    for path, alpha in rungs:
        parts.append(
            f'<path d="{path}" stroke="{style.rung}" '
            f'stroke-width="{style.rung_width:g}" stroke-opacity="{alpha:.3f}" '
            f'stroke-linecap="round"/>'
        )

    for name, path in (("a", path_a), ("b", path_b)):
        parts.append(f'<path d="{path}" fill="url(#{uid}-{name})"/>')

    parts.append("</g></g></svg>")
    return "".join(parts)


def _tones(mode: str) -> dict[str, str]:
    return {
        "strand_a": palette.brand(mode),
        "strand_b": palette.brand(mode, "light"),
        "rung": palette.brand(mode),
    }


def logo(size: int = 64, mode: str = "light", uid: str = "thl") -> str:
    """The wordless mark, for the header.

    Small renders are a different drawing, not the same one scaled: below about
    28px a 1.75-turn helix is four crossings inside 26 pixels and reads as
    texture. The small build trades turns for stroke and drops the rungs.
    """
    small = size < 30
    style = HelixStyle(
        width=size,
        height=size,
        turns=1.25 if small else 1.75,
        amplitude=0.35 if small else 0.33,
        strand_width=size / 6.5 if small else size / 11,
        rung_width=max(0.9, size / 52),
        rungs_per_turn=0 if small else 3,
        min_alpha=0.4 if small else 0.26,
        **_tones(mode),
    )
    return render(style, animated=False, uid=uid)


def spinner(
    size: int = 48, mode: str = "light", duration: float = 1.6, uid: str = "ths"
) -> str:
    """The loading state - the same mark, turning."""
    style = HelixStyle(
        width=size,
        height=size,
        turns=1.5,
        amplitude=0.35,
        strand_width=max(3.0, size / 10),
        rung_width=max(0.9, size / 44),
        rungs_per_turn=3,
        min_alpha=0.24,
        taper_ends=False,
        **_tones(mode),
    )
    return render(style, animated=True, duration=duration, title="Loading", uid=uid)


def favicon(size: int = 32) -> str:
    """A chunkier build that survives being drawn at 16px."""
    style = HelixStyle(
        width=size,
        height=size,
        turns=1.25,
        amplitude=0.36,
        strand_width=max(4.0, size / 5.5),
        rung_width=1.1,
        rungs_per_turn=2,
        samples_per_turn=32,
        min_alpha=0.42,
        taper_ends=False,
        **_tones("light"),
    )
    return render(style, animated=False, title="twoHelixes", uid="thf")
