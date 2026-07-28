"""Which model each decision runs on, and what happens when it declines.

The pipeline makes decisions of very different weight with the same tool. The
ones that write pandas or plan a join stay on the strong model; the ones that
return a small JSON object over a profile we already computed - the chart form,
the meaning of a one-line edit - run on the cheap tier, because
`validate_config` repairs whatever comes back against the real frame and the
floor is the heuristic we would have used anyway.

The rule these tests hold is that cheaper never means "sometimes no answer":
a mini call that fails escalates to the caller's model before anything is
allowed to degrade.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from twohelixes import config, llm
from twohelixes.pipeline import orchestrator


@pytest.fixture
def frame() -> Any:
    return pd.DataFrame(
        {
            "region": ["North", "South", "East"],
            "revenue": [120.0, 80.0, 60.0],
        }
    )


# The fast route answers questions that name their own chart without any model
# at all (see `tests/test_route.py`), so a test about *which model* runs has to
# ask something the gate abstains on.
OPEN_QUESTION = "what should I look at here?"


def _warnings(emit: Any) -> list[str]:
    return [str(event["warning"]) for event in emit.trace if "warning" in event]


class _Recorder:
    """Stands in for `llm.json_call`, recording the model each call asked for."""

    def __init__(self, answers: dict[str, Any] | None = None, fail: set[str] | None = None):
        self.models: list[str] = []
        self.answers = answers or {}
        self.fail = fail or set()

    def __call__(self, prompt: str, *, system: str = "", model: str = "", **kwargs: Any):
        self.models.append(model)
        if model in self.fail:
            raise llm.LLMError(f"{model} declined")
        return dict(self.answers)


def test_the_chart_form_is_chosen_on_the_cheap_tier(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder({"chart_type": "bar", "x": "region", "y": "revenue"})
    monkeypatch.setattr(llm, "json_call", recorder)

    emit = orchestrator.Emitter()
    config_out = orchestrator._choose_chart(
        OPEN_QUESTION, frame, emit, config.MODEL_ESCALATE
    )

    assert recorder.models == [config.MODEL_MINI]
    assert config_out["chart_type"] == "bar"


def test_an_edit_is_interpreted_on_the_cheap_tier(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder({"config_updates": {"chart_type": "hbar"}})
    monkeypatch.setattr(llm, "json_call", recorder)

    emit = orchestrator.Emitter()
    updated = orchestrator._edit(
        "make it horizontal",
        {"chart_type": "bar", "x": "region", "y": "revenue"},
        frame,
        emit,
        config.MODEL_DEFAULT,
    )

    assert recorder.models == [config.MODEL_MINI]
    assert updated["chart_type"] == "hbar"


def test_a_declining_mini_model_escalates_rather_than_degrading(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(
        {"chart_type": "line", "x": "region", "y": "revenue"},
        fail={config.MODEL_MINI},
    )
    monkeypatch.setattr(llm, "json_call", recorder)

    emit = orchestrator.Emitter()
    config_out = orchestrator._choose_chart(
        OPEN_QUESTION, frame, emit, config.MODEL_ESCALATE
    )

    assert recorder.models == [config.MODEL_MINI, config.MODEL_ESCALATE]
    # The chart still came from a model, not from the fallback heuristic.
    # (`validate_config` may still note a repair it made - that is a different
    # thing from the stage giving up.)
    assert not any("fell back" in warning for warning in _warnings(emit))


def test_both_models_failing_still_produces_a_chart(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _Recorder(fail={config.MODEL_MINI, config.MODEL_DEFAULT})
    monkeypatch.setattr(llm, "json_call", recorder)

    emit = orchestrator.Emitter()
    config_out = orchestrator._choose_chart(
        OPEN_QUESTION, frame, emit, config.MODEL_DEFAULT
    )

    assert config_out["chart_type"], "always a chart, even with the gateway dead"
    assert any("heuristic" in warning.lower() for warning in _warnings(emit))


def test_an_open_circuit_is_not_retried_on_the_expensive_model(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An open circuit means the gateway is down, not that the tier is fussy."""

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise llm.CircuitOpen("gateway down")

    monkeypatch.setattr(llm, "json_call", boom)

    with pytest.raises(llm.CircuitOpen):
        orchestrator._small_call("prompt", "system", config.MODEL_DEFAULT)


def test_the_cheap_tier_is_priced_so_a_run_can_be_measured() -> None:
    # An unpriced model silently costs DEFAULT_PRICE in the ledger, which is
    # 7x its real input price - the margin numbers would be fiction.
    assert config.MODEL_MINI in llm.PRICES
    mini_in, mini_out = llm.PRICES[config.MODEL_MINI]
    default_in, default_out = llm.PRICES[config.MODEL_DEFAULT]
    assert mini_in < default_in and mini_out < default_out


def test_a_question_that_names_its_own_chart_costs_no_model_call(
    frame: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate runs before the tier choice, so the cheapest call is none."""
    recorder = _Recorder({"chart_type": "pie"})
    monkeypatch.setattr(llm, "json_call", recorder)

    emit = orchestrator.Emitter()
    config_out = orchestrator._choose_chart(
        "which region has the most revenue?", frame, emit, config.MODEL_DEFAULT
    )

    assert recorder.models == []
    assert config_out["chart_type"] in ("bar", "hbar")
