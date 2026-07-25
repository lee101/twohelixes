"""Notebook export and hosting.

The export tests matter most: an exported notebook is the user's escape
hatch, and one that does not run is worse than no export at all.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from twohelixes.datasets import samples
from twohelixes.notebooks import compute, marimo_export

NB_PYTHON = Path(__file__).resolve().parents[1] / ".venv-nb" / "bin" / "python"


def _clean_env() -> dict:
    """The notebook venv is a different Python; PYTHONPATH must not follow."""
    return {
        k: v for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }


def _spec(**overrides) -> marimo_export.NotebookSpec:
    base = dict(
        title="Test chart",
        question="how did revenue trend?",
        chart_config={
            "chart_type": "line",
            "x": "order_date",
            "y": "net_amount",
            "color": "region",
            "title": "Revenue by region",
        },
    )
    base.update(overrides)
    return marimo_export.NotebookSpec(**base)


def test_export_is_valid_python():
    source = marimo_export.build(_spec(inline_rows=[{"a": 1}]))
    ast.parse(source)  # raises on failure
    assert "import marimo" in source
    assert "app = marimo.App" in source
    assert 'if __name__ == "__main__":' in source


def test_export_carries_the_transformation():
    code = 'result = df.groupby("region")["net_amount"].sum().reset_index()'
    source = marimo_export.build(_spec(inline_rows=[{"a": 1}], transform_code=code))
    assert code in source
    ast.parse(source)


def test_export_embeds_the_palette_in_order():
    """A notebook that recolours the chart defeats the point of exporting it."""
    source = marimo_export.build(_spec(inline_rows=[{"a": 1}]))
    assert "#2a78d6" in source and "#eb6834" in source
    assert source.index("#2a78d6") < source.index("#eb6834")


def test_export_escapes_a_hostile_title():
    source = marimo_export.build(_spec(inline_rows=[{"a": 1}], title='"""; import os'))
    ast.parse(source)


def test_export_without_data_still_parses():
    source = marimo_export.build(_spec())
    ast.parse(source)
    assert "df = pd.DataFrame()" in source


@pytest.mark.skipif(not NB_PYTHON.exists(), reason="notebook venv not present")
def test_exported_notebook_actually_runs(tmp_path):
    """The whole promise of export: it runs somewhere else and draws the chart."""
    samples.materialise()
    source = marimo_export.build(
        marimo_export.NotebookSpec(
            title="Orders",
            question="revenue by region",
            dataset_path=str(samples.path_for("orders")),
            transform_code=(
                'result = df.groupby("region")["net_amount"].sum().reset_index()'
            ),
            chart_config={
                "chart_type": "bar",
                "x": "region",
                "y": "net_amount",
                "title": "Net revenue by region",
            },
        )
    )
    notebook = tmp_path / "nb.py"
    notebook.write_text(source)

    marimo_bin = NB_PYTHON.parent / "marimo"
    flat = tmp_path / "flat.py"
    subprocess.run(
        [str(marimo_bin), "export", "script", str(notebook), "-o", str(flat)],
        check=True, capture_output=True, timeout=120, env=_clean_env(),
    )

    check = tmp_path / "check.py"
    check.write_text(
        "import runpy\n"
        f"ns = runpy.run_path({str(flat)!r})\n"
        "fig = ns['fig']\n"
        "assert len(fig.data) > 0, 'no traces'\n"
        "assert fig.layout.title.text == 'Net revenue by region'\n"
        "print('OK', len(fig.data))\n"
    )
    result = subprocess.run(
        [str(NB_PYTHON), str(check)], capture_output=True, text=True,
        timeout=180, env=_clean_env(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    assert "OK" in result.stdout


def test_sample_notebooks_all_export():
    samples.materialise()
    from twohelixes.pipeline import figures

    for sample in samples.SAMPLES:
        frame = samples.frame(sample.key)
        config = figures.validate_config(figures.heuristic_config(frame, ""), frame)
        source = marimo_export.build(
            marimo_export.NotebookSpec(
                title=sample.name,
                dataset_path=str(samples.path_for(sample.key)),
                chart_config=config,
            )
        )
        ast.parse(source)


# -- compute ---------------------------------------------------------------


def test_provider_list_reports_configuration():
    providers = compute.available_providers()
    assert providers["local"] is True
    assert set(providers) == {"local", "hetzner", "runpod"}


def test_bootstrap_stays_under_hetzner_user_data_limit():
    """Hetzner rejects a large user_data with an opaque error.

    Codex Infinity lost time to exactly this, so the bootstrap is a stub that
    fetches its payload rather than inlining it.
    """
    script = compute._bootstrap_script("tok", "https://example.com/nb.py")
    assert len(script.encode()) < 30_000
    assert "curl -fsSL" in script  # the payload is fetched, not inlined


def test_bootstrap_without_payload_is_still_valid():
    script = compute._bootstrap_script("tok")
    assert script.startswith("#!/bin/bash")
    assert "marimo edit" in script


def test_unknown_provider_is_rejected():
    with pytest.raises(compute.ComputeError):
        compute.start_remote("azure", "u1", "s1", "print(1)")
