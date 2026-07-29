"""Draw fewer points than the frame has, without changing what it looks like.

A line over 500,000 rows is 500,000 points serialized into the figure, sent
over the wire, and handed to Plotly to rasterize onto about 900 pixels. The
screen cannot show them, the browser struggles, and the JSON is megabytes -
measured at 122 MB for a 5M-point line by mojo-plotly, against 0.04 MB for the
same picture drawn from a downsample.

LTTB (largest-triangle-three-buckets) is the downsample that keeps the shape:
it picks the point in each bucket that forms the largest triangle with its
neighbours, so spikes and troughs survive where a stride or a mean would erase
them. `mojo-plotly` has it as a Mojo kernel, 4.6x over the vectorised numpy
version; this module uses that when it is there and falls back to a stride
otherwise.

Two rules, the same as every other accelerator here:

- it is never a dependency. No mojo-plotly, no numpy, a compiled library that
  is not built yet - all of them fall through to plain Python.
- it never compiles anything. mojo-plotly builds its `.so` lazily on first
  use, which is fine for a script and unacceptable inside a request;
  `available()` checks that the library is already on disk and gives up if it
  is not. `scripts/setup-venvs.sh` is what builds it.

The user is always told: a decimated chart carries a note saying how many
points it drew out of how many. Silently drawing a different chart from the
one the data describes is the thing this must not do.
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("twohelixes.charts.decimate")

# Above this many points a line is drawing more than the screen can show.
# Plotly's own recommendation for scattergl is the same order.
MAX_LINE_POINTS = 4000

# Markers are individually visible, so a scatter can carry more before the
# picture stops changing - but not unboundedly; overplotting takes over.
MAX_MARKER_POINTS = 20000

_state: dict[str, Any] = {"checked": False, "lttb": None}


def available() -> bool:
    """True when mojo-plotly's kernel is importable and already compiled."""
    if not _state["checked"]:
        _state["checked"] = True
        if os.environ.get("TWOHELIXES_MOJO_PLOTLY", "1") == "0":
            return False
        try:
            import mojoplotly

            # A non-editable install has no `src/` and no `build/`, so
            # mojo-plotly's own default path names a library that cannot
            # exist. `scripts/setup-venvs.sh` drops the built kernel beside
            # the installed package; point the loader at it before it is asked
            # for anything, so it never reaches its compile path.
            shipped = os.path.join(
                os.path.dirname(mojoplotly.__file__), "capi.so"
            )
            if os.path.exists(shipped):
                os.environ.setdefault("MOJOPLOTLY_LIB", shipped)

            from mojoplotly import _lib, data

            if not os.path.exists(_lib.LIB) and not os.path.exists(shipped):
                log.info("mojo-plotly present but not built; using the fallback")
                return False
            if os.path.exists(shipped):
                _lib.LIB = shipped
            _state["lttb"] = data.lttb
        except Exception as exc:  # noqa: BLE001 - absence is a normal state
            log.info("mojo-plotly unavailable: %s", exc)
    return _state["lttb"] is not None


def _indices(values: list[Any], target: int) -> list[int] | None:
    """Row numbers to keep, or None if this series cannot be thinned.

    LTTB runs against `(row number, value)` rather than the frame's own x. The
    x of a line chart is usually a timestamp or a label, and running the
    kernel on row numbers means the selected points come back as row numbers -
    so the original x values are carried through as they are, whatever their
    type, instead of being converted to floats and back.
    """
    try:
        import numpy as np
    except Exception:  # noqa: BLE001
        return None

    try:
        y = np.asarray(values, dtype=float)
    except (TypeError, ValueError):
        return None
    if y.ndim != 1 or len(y) <= target:
        return None
    if not np.isfinite(y).any():
        return None

    if available():
        try:
            xs, _ = _state["lttb"](np.arange(len(y), dtype=float), y, target)
            keep = np.rint(np.asarray(xs)).astype(int)
            keep = np.clip(keep, 0, len(y) - 1)
            keep = np.unique(keep)
            if len(keep) > 1:
                return [int(i) for i in keep]
        except Exception as exc:  # noqa: BLE001 - never required
            log.debug("lttb failed, using the stride: %s", exc)

    # Stride: cheap, order-preserving, and keeps the endpoints. It loses
    # narrow spikes, which is exactly what LTTB is for - hence the note.
    step = max(1, len(y) // target)
    keep = list(range(0, len(y), step))
    if keep[-1] != len(y) - 1:
        keep.append(len(y) - 1)
    return keep


def _target_for(trace: dict[str, Any]) -> int:
    mode = str(trace.get("mode") or "")
    if "markers" in mode and "lines" not in mode:
        return MAX_MARKER_POINTS
    return MAX_LINE_POINTS


def _thin_trace(trace: dict[str, Any]) -> int:
    """Thin one trace in place. Returns the number of points dropped."""
    if trace.get("type") != "scatter":
        return 0
    ys = trace.get("y")
    xs = trace.get("x")
    if not isinstance(ys, list) or not isinstance(xs, list):
        return 0
    if len(xs) != len(ys):
        return 0

    keep = _indices(ys, _target_for(trace))
    if not keep:
        return 0

    dropped = len(ys) - len(keep)
    trace["x"] = [xs[i] for i in keep]
    trace["y"] = [ys[i] for i in keep]
    # Anything else that is per-point has to be selected with the same rows or
    # the marker sizes stop describing the points they are drawn on.
    for key in ("text", "customdata", "hovertext"):
        seq = trace.get(key)
        if isinstance(seq, list) and len(seq) == len(ys):
            trace[key] = [seq[i] for i in keep]
    marker = trace.get("marker")
    if isinstance(marker, dict):
        for key in ("size", "color"):
            seq = marker.get(key)
            if isinstance(seq, list) and len(seq) == len(ys):
                marker[key] = [seq[i] for i in keep]
    return dropped


def thin(traces: list[Any], warnings: list[str]) -> list[Any]:
    """Downsample every oversized scatter trace, noting what was dropped."""
    total_before = 0
    total_after = 0
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        before = len(trace["y"]) if isinstance(trace.get("y"), list) else 0
        if _thin_trace(trace):
            total_before += before
            total_after += len(trace["y"])

    if total_before:
        how = "preserving peaks" if available() else "at a fixed interval"
        warnings.append(
            f"Drew {total_after:,} of {total_before:,} points, sampled {how}."
        )
    return traces
