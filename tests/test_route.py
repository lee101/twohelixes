"""The fast route: what it answers, what it refuses, and what it costs.

The gate is only worth having if it is right far more often than it fires, so
these tests measure both. The expensive assertion is the negative one - every
question it answers has to produce the chart the pipeline would have produced
anyway, because a fast wrong chart is worse than a slow right one.
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from twohelixes.datasets import examples, samples
from twohelixes.pipeline import figures, route


@pytest.fixture
def sales() -> Any:
    return pd.DataFrame(
        {
            "order_date": pd.to_datetime(
                ["2024-01-01", "2024-02-01", "2024-03-01"] * 2
            ),
            "region": ["North"] * 3 + ["South"] * 3,
            "revenue": [100.0, 120.0, 140.0, 90.0, 80.0, 70.0],
            "units": [10, 12, 14, 9, 8, 7],
        }
    )


# --------------------------------------------------------------------------
# What it answers
# --------------------------------------------------------------------------


def test_a_question_about_time_becomes_a_line(sales: Any) -> None:
    routed = route.classify("how did revenue trend over time?", sales)
    assert routed is not None
    assert routed.family == "time"
    assert routed.config["chart_type"] == "line"
    assert routed.config["x"] == "order_date"
    assert routed.config["y"] == "revenue"


def test_it_charts_the_measure_the_question_names(sales: Any) -> None:
    """Two measures, one named: a question about units is not about revenue."""
    routed = route.classify("show units over time", sales)
    assert routed is not None
    assert routed.config["y"] == "units"


def test_a_correlation_question_becomes_a_scatter(sales: Any) -> None:
    routed = route.classify("is revenue correlated with units?", sales)
    assert routed is not None
    assert routed.config["chart_type"] == "scatter"
    assert {routed.config["x"], routed.config["y"]} == {"revenue", "units"}


def test_a_ranking_question_becomes_a_bar(sales: Any) -> None:
    routed = route.classify("which region has the most revenue?", sales)
    assert routed is not None
    assert routed.config["chart_type"] == "bar"
    assert routed.config["x"] == "region"


# --------------------------------------------------------------------------
# What it refuses - the half that matters
# --------------------------------------------------------------------------


def test_it_refuses_a_question_that_names_two_charts(sales: Any) -> None:
    # A share over time is a line or a pie depending on what was meant, and
    # guessing is exactly the failure this gate exists to avoid.
    assert route.classify("what share of revenue over time by region?", sales) is None


def test_the_data_vetoes_the_cue() -> None:
    """"Over time" with no time column is a misread question, not a line."""
    frame = pd.DataFrame({"region": ["North", "South"], "revenue": [10.0, 20.0]})
    assert route.classify("how did revenue trend over time?", frame) is None


def test_it_refuses_when_the_measure_is_ambiguous(sales: Any) -> None:
    # Two measures and the question names neither: which one goes on the axis
    # is a judgement, so it goes to the model.
    assert route.classify("how has this trended over time?", sales) is None


def test_it_refuses_an_open_question(sales: Any) -> None:
    for question in (
        "what is interesting here?",
        "summarise this data",
        "what should I look at?",
        "why did the numbers move in March, and what else changed?",
    ):
        assert route.classify(question, sales) is None, question


def test_it_can_be_switched_off(sales: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route, "enabled", lambda: False)
    assert route.classify("how did revenue trend over time?", sales) is None


def test_it_never_returns_a_configuration_the_frame_cannot_carry(sales: Any) -> None:
    """Anything it returns must survive validation without losing its form."""
    for question in (
        "how did revenue trend over time?",
        "which region has the most revenue?",
        "is revenue correlated with units?",
    ):
        routed = route.classify(question, sales)
        assert routed is not None
        validated = figures.validate_config(dict(routed.config), sales)
        # validate_config may downgrade a form the data cannot support. If it
        # does, the gate should not have fired.
        assert validated["chart_type"] == routed.config["chart_type"], question


# --------------------------------------------------------------------------
# Hit rate over the questions we actually ship
# --------------------------------------------------------------------------


def test_it_answers_a_useful_share_of_the_sample_questions() -> None:
    """Measured, not asserted at a guess: the gate has to earn its place.

    Every question on the dataset pages is a question a real visitor asks, so
    the hit rate over them is the closest thing to a production number this
    suite can produce without a gateway.
    """
    samples.materialise()
    answered = 0
    total = 0
    misses: list[str] = []

    for sample in samples.SAMPLES:
        frame = samples.frame(sample.key)
        for question in sample.questions:
            total += 1
            if route.classify(question, frame) is not None:
                answered += 1
            else:
                misses.append(f"{sample.key}: {question}")

    # A gate that fires on nothing is dead code; one that fires on everything
    # is not a gate. Both ends are failures.
    #
    # The floor is deliberately low. Tightening the rules to stop the gate
    # answering "which channel brings the most revenue per order" - where
    # "revenue" is not a column and could mean `amount` or `net_amount` - took
    # the hit rate from 36% to 20%, and that was the right trade: the model
    # call it now makes costs 0.02 cents, and the wrong chart it used to draw
    # cost the user their trust in every other chart.
    assert total > 20
    assert 0.15 <= answered / total <= 0.9, (
        f"{answered}/{total} answered without a model call; misses: {misses}"
    )


def test_where_it_fires_on_a_worked_example_it_agrees_with_the_example() -> None:
    """The examples are hand-picked correct charts. Disagreeing is a bug.

    Only the form is compared: the example may aggregate differently, but if
    the gate says "pie" where the example says "line", one of them is wrong
    and it is not the example.
    """
    samples.materialise()
    disagreements: list[str] = []

    for example in examples.EXAMPLES:
        frame = samples.frame(example.dataset)
        routed = route.classify(example.question, frame)
        if routed is None:
            continue
        wanted = str(example.config["chart_type"])
        got = str(routed.config["chart_type"])
        # bar and hbar are the same decision; the orientation is decided later
        # from the label lengths, not from the question.
        if {wanted, got} == {"bar", "hbar"}:
            continue
        # The example shapes the frame first, so a form that depends on the
        # shaped columns (a scatter over aggregates) is not comparable.
        if wanted != got:
            disagreements.append(
                f"{example.dataset}/{example.slug}: example={wanted} route={got}"
            )

    assert not disagreements, disagreements
