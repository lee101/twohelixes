"""The chart color system.

Every value here comes from a palette that was validated with the six-check
validator (lightness band, chroma floor, CVD separation, normal-vision floor,
contrast vs surface) in both light and dark mode. Nothing in this file is
eyeballed, and nothing generates a hue at runtime.

The rules the rest of the codebase must not break:

* categorical hues are assigned in fixed slot order and never cycled - a 9th
  series folds into "Other" rather than reusing slot 1;
* scatter / bubble / map / small-multiple forms cap at 3 series, because with
  every pair adjacent the full eight cannot clear the separation floors;
* sequential is one hue light->dark, diverging is two hues around a neutral
  gray midpoint, and neither is ever a rainbow;
* three light-mode slots sit below 3:1 contrast, so any chart using them ships
  visible labels or the table view (the "relief rule").
"""

from __future__ import annotations

from dataclasses import dataclass

# Categorical: fixed order. Index 0 is always the first series.
CATEGORICAL_LIGHT = (
    "#2a78d6",  # blue
    "#eb6834",  # orange
    "#1baf7a",  # aqua
    "#eda100",  # yellow
    "#e87ba4",  # magenta
    "#008300",  # green
    "#4a3aa7",  # violet
    "#e34948",  # red
)

CATEGORICAL_DARK = (
    "#3987e5",
    "#d95926",
    "#199e70",
    "#c98500",
    "#d55181",
    "#008300",
    "#9085e9",
    "#e66767",
)

# Slots whose light-mode contrast is under 3:1. Charts that use them must carry
# visible labels or expose the table view.
NEEDS_RELIEF_LIGHT = frozenset({2, 3, 4})

# Sequential: one hue, light -> dark.
SEQUENTIAL_BLUE = (
    "#cde2fb",
    "#b7d3f6",
    "#9ec5f4",
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
)

# The brand. One hue for the whole product, taken from the validated
# sequential ramp rather than picked separately, so the logo, the buttons, the
# links and the first chart series are literally the same colour. Anything that
# is not data and not a status now uses these three and nothing else.
#
# The dark values step one stop lighter because #2a78d6 on #1a1a19 is 3.4:1 -
# fine for a large mark, short of 4.5:1 for a link sitting in body copy.
BRAND = {"light": "#2a78d6", "dark": "#3987e5"}
BRAND_DEEP = {"light": "#1c5cab", "dark": "#2a78d6"}
BRAND_LIGHT = {"light": "#6da7ec", "dark": "#86b6ef"}

# Ordinal ramps must stay readable at the end nearest the surface.
ORDINAL_LIGHT_START = 3  # #86b6ef, 2.06:1
ORDINAL_DARK_END = 10  # #184f95, 2.15:1

# Diverging: two poles that read as opposite, neutral gray between them.
DIVERGING_LOW = "#2a78d6"
DIVERGING_HIGH = "#e34948"
DIVERGING_MID_LIGHT = "#f0efec"
DIVERGING_MID_DARK = "#383835"

STATUS = {
    "good": {"light": "#008300", "dark": "#008300", "icon": "check"},
    "warning": {"light": "#eda100", "dark": "#c98500", "icon": "alert"},
    "serious": {"light": "#eb6834", "dark": "#d95926", "icon": "warn"},
    "critical": {"light": "#e34948", "dark": "#e66767", "icon": "stop"},
}

# Chart forms where any two marks can end up adjacent, so all pairs must
# separate rather than just neighbours.
ALL_PAIRS_FORMS = frozenset(
    {
        "scatter",
        "scatter_3d",
        "scatter_matrix",
        "bubble",
        "choropleth",
        "map",
        "small_multiples",
        "parallel_coordinates",
    }
)
ALL_PAIRS_MAX_SERIES = 3

# Maximum series before folding the tail into "Other".
MAX_SERIES = len(CATEGORICAL_LIGHT)
OTHER_LABEL = "Other"
OTHER_COLOR_LIGHT = "#8a8a85"
OTHER_COLOR_DARK = "#6f6f6a"


@dataclass(frozen=True)
class Surface:
    background: str
    text_primary: str
    text_secondary: str
    text_muted: str
    grid: str
    axis: str
    ring: str


LIGHT = Surface(
    background="#fcfcfb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#7a7973",
    grid="#e8e7e3",
    axis="#c9c8c3",
    ring="#fcfcfb",
)

DARK = Surface(
    background="#1a1a19",
    text_primary="#ffffff",
    text_secondary="#c3c2b7",
    text_muted="#8f8e85",
    grid="#2e2e2c",
    axis="#454542",
    ring="#1a1a19",
)


def surface(mode: str = "light") -> Surface:
    return DARK if mode == "dark" else LIGHT


def categorical(mode: str = "light") -> tuple[str, ...]:
    return CATEGORICAL_DARK if mode == "dark" else CATEGORICAL_LIGHT


def series_color(index: int, mode: str = "light") -> str:
    """Colour for series `index`, in fixed slot order.

    Anything past the last slot is the neutral "Other" colour - callers should
    have folded the tail before getting here, and this makes it visible when
    they did not.
    """
    palette = categorical(mode)
    if index < 0:
        index = 0
    if index >= len(palette):
        return OTHER_COLOR_DARK if mode == "dark" else OTHER_COLOR_LIGHT
    return palette[index]


def max_series_for(chart_type: str) -> int:
    """How many distinct colours this chart form may carry."""
    if chart_type in ALL_PAIRS_FORMS:
        return ALL_PAIRS_MAX_SERIES
    return MAX_SERIES


def needs_relief(indices: list[int], mode: str = "light") -> bool:
    """True when the chart must ship labels or a table view for readability."""
    if mode == "dark":
        return False
    return any(i in NEEDS_RELIEF_LIGHT for i in indices)


def sequential(steps: int = 7, mode: str = "light", ordinal: bool = False) -> list[str]:
    """`steps` samples of the one-hue ramp.

    An ordinal ramp keeps its surface-nearest end above 2:1; a sequential ramp
    may recede to "near zero" at the light end.
    """
    ramp = list(SEQUENTIAL_BLUE)
    if ordinal:
        ramp = ramp[ORDINAL_LIGHT_START : ORDINAL_DARK_END + 1]
    if mode == "dark":
        ramp = list(reversed(ramp))
    if steps <= 1:
        return [ramp[len(ramp) // 2]]
    if steps >= len(ramp):
        return ramp

    span = len(ramp) - 1
    return [ramp[round(i * span / (steps - 1))] for i in range(steps)]


def diverging(steps: int = 9, mode: str = "light") -> list[str]:
    """Two poles with a neutral midpoint and equal steps per arm."""
    if steps % 2 == 0:
        steps += 1
    arm = steps // 2
    mid = DIVERGING_MID_DARK if mode == "dark" else DIVERGING_MID_LIGHT

    low = _ramp_between(DIVERGING_LOW, mid, arm + 1)[:-1]
    high = list(reversed(_ramp_between(DIVERGING_HIGH, mid, arm + 1)[:-1]))
    return low + [mid] + high


def _ramp_between(start: str, end: str, steps: int) -> list[str]:
    """Linear interpolation in sRGB between two documented endpoints.

    Only used for the diverging arms, whose endpoints and midpoint are both
    documented values; it never invents a categorical hue.
    """
    sr, sg, sb = _to_rgb(start)
    er, eg, eb = _to_rgb(end)
    out = []
    for i in range(steps):
        t = i / max(1, steps - 1)
        out.append(
            "#%02x%02x%02x"
            % (
                round(sr + (er - sr) * t),
                round(sg + (eg - sg) * t),
                round(sb + (eb - sb) * t),
            )
        )
    return out


def _to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def plotly_colorway(mode: str = "light") -> list[str]:
    return list(categorical(mode))


def brand(mode: str = "light", tone: str = "base") -> str:
    """The single product hue, in one of its three tones."""
    table = {"base": BRAND, "deep": BRAND_DEEP, "light": BRAND_LIGHT}[tone]
    return table["dark" if mode == "dark" else "light"]


def as_css_variables(mode: str = "light") -> dict[str, str]:
    """Role-named custom properties for the frontend."""
    face = surface(mode)
    variables = {
        "--surface-1": face.background,
        "--text-primary": face.text_primary,
        "--text-secondary": face.text_secondary,
        "--text-muted": face.text_muted,
        "--grid": face.grid,
        "--axis": face.axis,
        "--ring": face.ring,
        "--brand": brand(mode),
        "--brand-deep": brand(mode, "deep"),
        "--brand-light": brand(mode, "light"),
    }
    for index, color in enumerate(categorical(mode), start=1):
        variables[f"--series-{index}"] = color
    variables["--series-other"] = (
        OTHER_COLOR_DARK if mode == "dark" else OTHER_COLOR_LIGHT
    )
    for name, values in STATUS.items():
        variables[f"--status-{name}"] = values[mode]
    return variables
