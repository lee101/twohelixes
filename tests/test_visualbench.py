"""The visual report's exit status covers every diagnostic it prints."""

from __future__ import annotations

import pytest

from bench.visualbench import _report_has_failures


@pytest.mark.parametrize(
    "failure",
    [
        {"error": "navigation failed"},
        {"broken_images": ["/missing.png"]},
        {"contrast_failures": ["muted/page: 3.9"]},
        {"console_errors": ["Failed to load resource"]},
        {"empty_plot": True},
        {"h_overflow_px": 2},
        {"spill_px": 2},
        {"tile_marks": 9},
    ],
)
def test_report_failures_produce_a_failing_exit(failure: dict[str, object]) -> None:
    assert _report_has_failures([failure])


def test_clean_report_passes_with_boundary_values() -> None:
    assert not _report_has_failures(
        [
            {
                "h_overflow_px": 1,
                "spill_px": 1,
                "tile_marks": 10,
                "broken_images": [],
                "contrast_failures": [],
                "console_errors": [],
            }
        ]
    )
