"""The static-embedding column matcher: what it resolves and what it refuses.

These tests are calibration as much as verification. The thresholds in
`semantic.py` are the only thing standing between "resolves the column nobody
named" and "confidently charts the wrong number", so the cases below are the
evidence for the values they hold - positives that must clear both gates,
negatives that must clear neither.

Skipped when pybed or the model is absent, because the whole feature is
optional by design: `./scripts/setup-venvs.sh` installs the first and copies
the second out of ../gobed.
"""

from __future__ import annotations

import pytest

from twohelixes.pipeline import semantic

pytestmark = pytest.mark.skipif(
    not semantic.available(),
    reason="pybed or the embedding model is not installed (scripts/setup-venvs.sh)",
)

ORDER_MEASURES = ["units", "amount", "discount", "net_amount"]
TICKET_MEASURES = ["first_response_hours", "resolution_hours", "satisfaction"]
ENERGY_MEASURES = ["kwh", "temperature_c"]
WEB_MEASURES = ["sessions", "bounce_rate", "avg_seconds", "conversions"]


# --------------------------------------------------------------------------
# What it resolves
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,candidates,expected",
    [
        # The word is not in the question in any of these. That is the point:
        # literal matching already handles the ones where it is.
        ("does customer happiness drop when tickets take longer?",
         TICKET_MEASURES, "satisfaction"),
        ("which topics take longest to resolve?",
         TICKET_MEASURES, "resolution_hours"),
        ("what is the power draw at each site?", ENERGY_MEASURES, "kwh"),
        ("how hot did it get at each site?", ENERGY_MEASURES, "temperature_c"),
    ],
)
def test_it_finds_the_column_the_question_means(question, candidates, expected):
    resolved = semantic.resolve(question, candidates)
    assert resolved is not None, f"abstained on {question!r}"
    assert resolved[0] == expected


# --------------------------------------------------------------------------
# What it refuses - the half that keeps it safe
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question,candidates",
    [
        # Nothing to do with any column.
        ("what should I look at?", ORDER_MEASURES),
        ("clean up the data", WEB_MEASURES),
        ("export this to excel", ORDER_MEASURES),
        ("summarise this", TICKET_MEASURES),
        # `amount` and `net_amount` are both revenue and score within a
        # hair of each other. A person should pick; we should not.
        ("which channel brings the most revenue per order?", ORDER_MEASURES),
        # Topically close to two candidates at once.
        ("how many visitors came from each source?", WEB_MEASURES),
    ],
)
def test_it_abstains_rather_than_guessing(question, candidates):
    assert semantic.resolve(question, candidates) is None


def test_an_empty_or_single_candidate_set_is_not_a_decision():
    assert semantic.resolve("anything", []) is None
    # One candidate always wins by definition, so the margin cannot mean
    # anything - the caller handles that case before asking.
    resolved = semantic.resolve("what is the power draw?", ["kwh"])
    assert resolved is None or resolved[0] == "kwh"


def test_it_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(semantic, "available", lambda: False)
    assert semantic.resolve("what is the power draw at each site?", ENERGY_MEASURES) is None


# --------------------------------------------------------------------------
# The properties that make it usable in a request
# --------------------------------------------------------------------------


def test_embedding_is_fast_enough_to_sit_in_the_request_path() -> None:
    """A gate that costs more than the call it avoids is not a gate."""
    import time

    semantic.embed("warm the cache")
    started = time.perf_counter()
    for index in range(200):
        semantic.embed(f"how did revenue trend in region {index}")
    micros = (time.perf_counter() - started) / 200 * 1e6

    # Measured at ~40us on this box. The bound is loose because CI hardware is
    # not this box; it is here to catch a regression of the kind that would
    # mean a transformer had crept in.
    assert micros < 2000, f"{micros:.0f} us per embedding"


def test_column_vectors_are_cached() -> None:
    semantic.resolve("what is the power draw at each site?", ENERGY_MEASURES)
    # The columns of a dataset are asked about over and over; their vectors
    # should be computed once.
    assert "kwh" in semantic._vectors
    assert semantic._vectors["kwh"] is semantic.embed("kwh")


def test_it_needs_no_torch() -> None:
    """The server runs on the 3.12 environment the AOT binary embeds.

    torch is not in it and is not going to be. If importing the embedder ever
    pulls one in, this feature stops working in production and only there.
    """
    import sys

    semantic.available()
    assert "torch" not in sys.modules


def test_the_runtime_environment_can_import_pybed_without_pth_processing() -> None:
    """The binary adds site-packages with `sys.path.insert`, which runs no `.pth`.

    An *editable* install is a `.pth` file plus an import finder, and `site`
    only executes those when it processes a site-packages directory itself.
    Installed editable, pybed imports fine from `.venv/bin/python` and is
    invisible to the embedded interpreter that actually serves requests - the
    log said "pybed is not installed" in production while every local check
    passed. A real package directory is the only form that survives.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    runtime = root / ".venv" / "lib" / "python3.12" / "site-packages"
    if not runtime.exists():
        pytest.skip("no runtime venv (scripts/setup-venvs.sh)")

    assert (runtime / "pybed" / "__init__.py").exists(), (
        "pybed is installed editable; the server will not see it"
    )
