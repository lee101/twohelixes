"""The product's central promise: a question always comes back as a chart.

Every stage already had a fallback, but the fallbacks assumed the failure was
an `LLMError`. Anything else - a column that vanished between stages, a form
the data cannot support, a hung transform - escaped as an exception and the
user got an error page instead of the chart they waited for. These tests hold
the guarantee for the failures that are not polite about how they arrive.
"""

from __future__ import annotations

import pandas as pd
import pytest

from twohelixes import llm
from twohelixes.interpreter import sandbox
from twohelixes.pipeline import orchestrator
from twohelixes.routes import charts as chart_routes


@pytest.fixture
def dead_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every model call fails, so only the deterministic paths remain."""

    def boom(*args: object, **kwargs: object) -> object:
        raise llm.LLMError("gateway down")

    monkeypatch.setattr(llm, "json_call", boom)
    monkeypatch.setattr(llm, "call", boom)


@pytest.fixture
def orders() -> pd.DataFrame:
    return pd.DataFrame({
        "region": list("ABCD") * 5,
        "revenue": range(20),
        "month": pd.date_range("2024-01-01", periods=20, freq="D"),
    })


FRAMES = {
    "empty": pd.DataFrame({"region": [], "revenue": []}),
    "one_column": pd.DataFrame({"only": [1, 2, 3]}),
    "all_text": pd.DataFrame({"a": ["x", "y"], "b": ["p", "q"]}),
    "single_row": pd.DataFrame({"total": [42]}),
    "mostly_null": pd.DataFrame({"a b": [None, None], "c": [1, None]}),
}


@pytest.mark.parametrize("name", sorted(FRAMES))
def test_awkward_frames_still_chart(name: str, dead_gateway: None) -> None:
    result = orchestrator.run("how did revenue trend by region?", {"orders": FRAMES[name]})
    assert result.figure is not None, name
    assert result.figure.get("data"), name


def test_gateway_down_still_charts(orders: pd.DataFrame, dead_gateway: None) -> None:
    result = orchestrator.run("how did revenue trend by region?", {"orders": orders})
    assert result.figure is not None
    assert result.config.get("chart_type")
    # A degraded run has to say so; a silent fallback is worse than an error.
    assert result.warnings


def test_a_stage_raising_something_unexpected_still_charts(
    orders: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("not an LLMError")

    monkeypatch.setattr(orchestrator, "_search", explode)
    result = orchestrator.run("anything", {"orders": orders})
    assert result.figure is not None
    assert any("could not finish" in w for w in result.warnings)


def test_a_chart_form_the_data_cannot_support_falls_back(
    orders: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    from twohelixes.pipeline import figures

    real_build = figures.build
    calls: list[str] = []

    def build(frame: object, config: dict, **kwargs: object) -> object:
        calls.append(str(config.get("chart_type")))
        if len(calls) == 1:
            raise ValueError("cannot draw that")
        return real_build(frame, config, **kwargs)

    monkeypatch.setattr(figures, "build", build)
    result = orchestrator.run("revenue by region", {"orders": orders})
    assert result.figure is not None
    assert len(calls) > 1


def test_a_hung_transform_is_interrupted() -> None:
    """The watchdog used to set a flag nothing read, so a runaway loop held the
    request open until the proxy gave up."""
    result = sandbox.run("x = 0\nfor i in range(10**11):\n    x += i\n", timeout=1.0)
    assert not result.ok
    assert result.error_type == "Timeout"
    # The async exception must not leak into the next run.
    assert sandbox.run("y = 2\ny").ok


def test_figure_csv_is_every_plotted_point() -> None:
    figure = {"data": [
        {"name": "North", "x": [1, 2], "y": [3, 4]},
        {"name": "South", "x": [1, 2, 3], "y": [5, 6, 7]},
    ]}
    rows = chart_routes.figure_csv(figure).strip().splitlines()
    assert rows[0] == "series,x,y"
    assert len(rows) == 6
    assert chart_routes.figure_csv({"data": []}) == ""
