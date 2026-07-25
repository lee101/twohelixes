"""The twoHelixes mark: a procedurally generated double helix.

Two strands in exact opposition (phase offset pi), so where one is at the front
of the twist the other is at the back. Depth is encoded as opacity rather than
overlap, which is what gives the mark its translucent look and lets it sit on
any surface without a plate behind it.

Each strand is a single smooth path stroked with a linear gradient whose stops
carry the opacity. That works because the depth function is periodic along the
helix axis, which is exactly the direction the gradient runs - so one path and
a dozen stops replace hundreds of per-sample segments, with no banding and no
beading where the stroke fades.

The same generator produces the static logo and the loading spinner. The
spinner does not rotate the group; it translates the wave by exactly one
period, which reads as a helix turning and loops seamlessly because the curve
is periodic. No JavaScript, no sprite sheet: one inert SVG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Opacity floor and ceiling as a strand passes from back to front.
MIN_ALPHA = 0.16
MAX_ALPHA = 1.0
# Rungs only hint at the connection. Anything heavier and the mark reads as a
# ladder or a set of window blinds rather than a twist.
RUNG_ALPHA_SCALE = 0.22
# Gradient stops per turn. The depth curve is a raised cosine, so this is
# plenty to keep the fade smooth.
STOPS_PER_TURN = 10


@dataclass(frozen=True)
class HelixStyle:
    strand_a: str = "#2a78d6"
    strand_b: str = "#1baf7a"
    rung: str = "#7a7973"
    width: int = 64
    height: int = 64
    turns: float = 2.0
    amplitude: float = 0.34  # fraction of the usable cross-axis
    strand_width: float = 3.0
    rung_width: float = 1.2
    rungs_per_turn: int = 2
    vertical: bool = True
    samples_per_turn: int = 40
    # Raise for small renders: a strand that fades to 0.16 disappears at 16px.
    min_alpha: float = MIN_ALPHA


def _depth(theta: float) -> float:
    """0 at the back of the twist, 1 at the front."""
    return (math.cos(theta) + 1.0) / 2.0


def _alpha(theta: float, floor: float = MIN_ALPHA) -> float:
    return floor + (MAX_ALPHA - floor) * _depth(theta)


def _point(style: HelixStyle, t: float, phase: float, span: float, cross: float):
    """A point on a strand.

    `t` runs 0..1 along the drawn length; the cross-axis carries the sine. The
    cross-axis is inset by half the stroke so a strand at full swing sits
    inside the viewBox instead of being clipped.
    """
    theta = t * style.turns * 2 * math.pi + phase
    inset = style.strand_width / 2.0 + 0.5
    usable = max(1.0, cross - 2 * inset)
    offset = math.sin(theta) * style.amplitude * usable
    along = t * span
    if style.vertical:
        return (cross / 2 + offset, along), theta
    return (along, cross / 2 + offset), theta


def _strand_path(
    style: HelixStyle, phase: float, span: float, cross: float, turns: float
) -> str:
    """One continuous polyline along the strand."""
    steps = max(12, int(style.samples_per_turn * turns))
    points: list[str] = []
    for i in range(steps + 1):
        t = i / steps * (turns / style.turns)
        (x, y), _ = _point(style, t, phase, span, cross)
        points.append(f"{x:.2f} {y:.2f}")
    return "M " + " L ".join(points)


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
    count = max(2, int(style.rungs_per_turn * turns))
    rungs: list[tuple[str, float]] = []
    for i in range(count + 1):
        t = i / count * (turns / style.turns)
        (ax, ay), theta = _point(style, t, 0.0, span, cross)
        (bx, by), _ = _point(style, t, math.pi, span, cross)
        # Widest apart is where a rung actually communicates the twist; near a
        # crossing the two strands coincide and a rung would be a dot.
        separation = abs(math.sin(theta))
        if separation < 0.35:
            continue
        alpha = separation * RUNG_ALPHA_SCALE
        rungs.append((f"M {ax:.2f} {ay:.2f} L {bx:.2f} {by:.2f}", alpha))
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

    path_a = _strand_path(style, 0.0, span, cross, turns)
    path_b = _strand_path(style, math.pi, span, cross, turns)
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
        parts.append(
            f'<path d="{path}" stroke="url(#{uid}-{name})" '
            f'stroke-width="{style.strand_width:g}" stroke-linecap="round" '
            f'stroke-linejoin="round"/>'
        )

    parts.append("</g></g></svg>")
    return "".join(parts)


def logo(size: int = 64, mode: str = "light", uid: str = "thl") -> str:
    """The wordless mark, for the header."""
    style = HelixStyle(
        width=size,
        height=size,
        turns=2.0,
        strand_width=max(2.0, size / 20),
        rung_width=max(0.8, size / 64),
        strand_a="#3987e5" if mode == "dark" else "#2a78d6",
        strand_b="#199e70" if mode == "dark" else "#1baf7a",
        rung="#8f8e85" if mode == "dark" else "#7a7973",
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
        amplitude=0.36,
        strand_width=max(2.0, size / 16),
        rung_width=max(0.8, size / 48),
        rungs_per_turn=2,
        strand_a="#3987e5" if mode == "dark" else "#2a78d6",
        strand_b="#199e70" if mode == "dark" else "#1baf7a",
        rung="#8f8e85" if mode == "dark" else "#7a7973",
    )
    return render(style, animated=True, duration=duration, title="Loading", uid=uid)


def favicon(size: int = 32) -> str:
    """A chunkier build that survives being drawn at 16px."""
    style = HelixStyle(
        width=size,
        height=size,
        turns=1.5,
        amplitude=0.38,
        strand_width=max(3.0, size / 9),
        rung_width=1.0,
        rungs_per_turn=2,
        samples_per_turn=32,
        min_alpha=0.38,
    )
    return render(style, animated=False, title="twoHelixes", uid="thf")
