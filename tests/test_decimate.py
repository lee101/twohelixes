"""Downsampling large series for the chart.

The contract is the same one every accelerator here has: it must not change
what the chart says, it must work with mojo-plotly absent, and it must tell
the user what it did. A chart that quietly drew 4,000 of 500,000 points and
said nothing would be the "silent tail" the chart rules forbid.
"""

import numpy as np
import pandas as pd
import pytest

from twohelixes.charts import decimate
from twohelixes.pipeline import figures


def line_trace(n: int, spike_at: int | None = None) -> dict:
    y = (np.sin(np.arange(n) / 900.0) * 100).tolist()
    if spike_at is not None:
        y[spike_at] = 9999.0
    return {"type": "scatter", "mode": "lines", "x": list(range(n)), "y": y}


def test_a_small_series_is_left_alone():
    trace = line_trace(500)
    before = list(trace["y"])
    warnings: list[str] = []
    decimate.thin([trace], warnings)
    assert trace["y"] == before
    assert warnings == []


def test_a_large_line_is_thinned_and_reported():
    trace = line_trace(200_000)
    warnings: list[str] = []
    decimate.thin([trace], warnings)

    assert len(trace["y"]) <= decimate.MAX_LINE_POINTS + 1
    assert len(trace["x"]) == len(trace["y"])
    assert warnings and "200,000 points" in warnings[0]
    # The x channel keeps the frame's own values, not row numbers of its own.
    assert trace["x"] == sorted(trace["x"])
    assert trace["x"][0] == 0
    assert trace["x"][-1] == 199_999


def test_markers_keep_more_points_than_lines():
    """A marker is individually visible; a line's pixels are shared."""
    n = 15_000  # over the line limit, under the marker one
    assert decimate.MAX_LINE_POINTS < n < decimate.MAX_MARKER_POINTS
    line = line_trace(n)
    markers = line_trace(n)
    markers["mode"] = "markers"
    decimate.thin([line], [])
    decimate.thin([markers], [])
    assert len(line["y"]) <= decimate.MAX_LINE_POINTS + 1
    assert len(markers["y"]) == n

    big = line_trace(60_000)
    big["mode"] = "markers"
    decimate.thin([big], [])
    assert len(big["y"]) <= decimate.MAX_MARKER_POINTS + 1


def test_parallel_channels_are_selected_with_the_same_rows():
    n = 50_000
    trace = line_trace(n)
    trace["text"] = [f"row {i}" for i in range(n)]
    trace["marker"] = {"size": list(range(n)), "color": ["#fff"] * n}
    decimate.thin([trace], [])

    kept = len(trace["y"])
    assert len(trace["text"]) == kept
    assert len(trace["marker"]["size"]) == kept
    assert len(trace["marker"]["color"]) == kept
    # A marker size must still describe the point it is drawn on.
    assert trace["marker"]["size"][0] == trace["x"][0]
    assert trace["marker"]["size"][-1] == trace["x"][-1]
    assert trace["text"][-1] == f"row {trace['x'][-1]}"


def test_a_non_numeric_series_is_untouched():
    trace = {
        "type": "scatter",
        "mode": "lines",
        "x": list(range(10_000)),
        "y": ["a"] * 10_000,
    }
    warnings: list[str] = []
    decimate.thin([trace], warnings)
    assert len(trace["y"]) == 10_000
    assert warnings == []


def test_mismatched_channels_are_untouched():
    """Never invent a pairing: x and y of different lengths is a bug upstream,
    and thinning them by index would turn it into a wrong chart instead."""
    trace = {
        "type": "scatter",
        "mode": "lines",
        "x": list(range(10_000)),
        "y": [1.0] * 9_999,
    }
    decimate.thin([trace], [])
    assert len(trace["x"]) == 10_000
    assert len(trace["y"]) == 9_999


def test_bar_traces_are_not_thinned():
    """A bar per category is the chart; dropping bars drops data."""
    trace = {"type": "bar", "x": list(range(50_000)), "y": [1.0] * 50_000}
    decimate.thin([trace], [])
    assert len(trace["y"]) == 50_000


def test_it_works_without_mojo_plotly(monkeypatch):
    monkeypatch.setattr(decimate, "_state", {"checked": True, "lttb": None})
    trace = line_trace(100_000)
    warnings: list[str] = []
    decimate.thin([trace], warnings)
    assert len(trace["y"]) <= decimate.MAX_LINE_POINTS + 1
    assert "fixed interval" in warnings[0]
    assert trace["x"][0] == 0
    assert trace["x"][-1] == 99_999


def test_the_environment_switch_turns_it_off(monkeypatch):
    monkeypatch.setenv("TWOHELIXES_MOJO_PLOTLY", "0")
    monkeypatch.setattr(decimate, "_state", {"checked": False, "lttb": None})
    assert decimate.available() is False


@pytest.mark.skipif(not decimate.available(), reason="mojo-plotly kernel not built")
def test_lttb_keeps_the_spike_a_stride_would_miss():
    """This is the whole reason for LTTB rather than every nth row: an outlier
    between two sample points is invisible to a stride, and an outlier is
    usually the thing the chart was asked about."""
    spike = 123_456
    with_lttb = line_trace(500_000, spike_at=spike)
    decimate.thin([with_lttb], [])
    assert 9999.0 in with_lttb["y"]
    assert spike in with_lttb["x"]


def test_build_thins_a_long_series_and_notes_it():
    n = 120_000
    frame = pd.DataFrame(
        {
            "t": pd.date_range("2024-01-01", periods=n, freq="min"),
            "v": np.sin(np.arange(n) / 500.0) * 10,
        }
    )
    figure, warnings = figures.build(
        frame, {"chart_type": "line", "x": "t", "y": "v", "title": "t"}
    )
    points = len(figure["data"][0]["y"])
    assert points <= decimate.MAX_LINE_POINTS + 1
    assert any("points" in w for w in warnings)
    # The x values are still timestamps, not the row numbers LTTB ran on.
    assert not isinstance(figure["data"][0]["x"][0], (int, float))
