"""The visual report's exit status covers every diagnostic it prints."""

from __future__ import annotations

from pathlib import Path

import pytest

from bench import visualbench
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


def test_contact_sheet_surfaces_failures_without_screenshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(visualbench, "OUT", tmp_path)
    visualbench._write_index(
        [
            {"page": "home", "error": "navigation <failed>"},
            {
                "page": "app",
                "theme": "dark",
                "viewport": "mobile",
                "file": "app.png",
                "console_errors": ["script failed"],
                "tile_marks": 9,
            },
        ]
    )

    index = (tmp_path / "index.html").read_text()
    assert "home · navigation failure" in index
    assert "navigation &lt;failed&gt;" in index
    assert "1 console errors" in index
    assert "blank tile" in index
