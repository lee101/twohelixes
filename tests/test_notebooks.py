"""Notebook export and hosting.

The export tests matter most: an exported notebook is the user's escape
hatch, and one that does not run is worse than no export at all.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from twohelixes.datasets import samples
from twohelixes.notebooks import compute, ipynb_export, marimo_export

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


# -- .ipynb ---------------------------------------------------------------
#
# The format most people already have open. Both exports come off the same
# NotebookSpec, so these tests are about the document being well formed and
# the code in it running - not about the content, which the marimo tests
# above already cover.


def test_ipynb_is_valid_notebook_json():
    document = json.loads(ipynb_export.build(_spec(inline_rows=[{"a": 1}])))
    assert document["nbformat"] == 4
    assert document["metadata"]["kernelspec"]["name"] == "python3"
    assert {c["cell_type"] for c in document["cells"]} <= {"code", "markdown"}
    for cell in document["cells"]:
        assert isinstance(cell["source"], list)
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


def test_ipynb_code_cells_are_valid_python():
    cells = ipynb_export.code_cells(ipynb_export.build(_spec(inline_rows=[{"a": 1}])))
    assert cells
    for cell in cells:
        ast.parse(cell)


def test_ipynb_escapes_a_hostile_title():
    """A title is user text; it must not be able to close a string or a cell."""
    document = ipynb_export.build(
        _spec(inline_rows=[{"a": 1}], title='""" + __import__("os").system("x") + """')
    )
    json.loads(document)
    for cell in ipynb_export.code_cells(document):
        ast.parse(cell)


def test_ipynb_runs_end_to_end():
    """The claim on the button: run the cells in order and a chart comes out.

    Executed here rather than shelled out to nbclient, because the point is
    that the notebook needs only what our own runner has - pandas and plotly.
    """
    plotly = pytest.importorskip("plotly")
    document = ipynb_export.build(
        marimo_export.NotebookSpec(
            title="Revenue by region",
            question="how did revenue trend by region?",
            transform_code=(
                'result = df.groupby("region", as_index=False)["revenue"].sum()'
            ),
            chart_config={
                "chart_type": "bar",
                "x": "region",
                "y": "revenue",
                "title": "Revenue by region",
            },
            inline_rows=[
                {"region": "North", "revenue": 120},
                {"region": "North", "revenue": 80},
                {"region": "South", "revenue": 60},
            ],
        )
    )
    namespace: dict = {}
    for cell in ipynb_export.code_cells(document):
        exec(compile(cell, "<cell>", "exec"), namespace)

    figure = namespace["fig"]
    assert isinstance(figure, plotly.graph_objects.Figure)
    assert len(figure.data) == 1
    assert list(figure.data[0].x) == ["North", "South"]
    assert list(figure.data[0].y) == [200, 60]
    assert figure.layout.title.text == "Revenue by region"
    # The palette travels with the export, in slot order.
    assert figure.data[0].marker.color == "#2a78d6"


def test_ipynb_sample_exports_all_parse():
    samples.materialise()
    from twohelixes.pipeline import figures

    for sample in samples.SAMPLES:
        frame = samples.frame(sample.key)
        config = figures.validate_config(figures.heuristic_config(frame, ""), frame)
        document = ipynb_export.build(
            marimo_export.NotebookSpec(
                title=sample.name,
                dataset_path=str(samples.path_for(sample.key)),
                chart_config=config,
            )
        )
        for cell in ipynb_export.code_cells(document):
            ast.parse(cell)


def test_ipynb_converts_back_to_a_hostable_marimo_notebook():
    """Hosting an export must run it, not just accept it.

    marimo cells are functions: a name defined in one is invisible to the next
    unless it is returned and taken as an argument. The conversion is only
    correct if that threading is right, so this asserts on the wiring rather
    than on the text.
    """
    document = ipynb_export.build(
        _spec(inline_rows=[{"region": "North", "revenue": 1}])
    )
    source = ipynb_export.to_marimo(document)
    tree = ast.parse(source)

    cells = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and any(getattr(d, "attr", "") == "cell" for d in node.decorator_list)
    ]
    assert len(cells) >= 4

    defined: set[str] = set()
    for cell in cells:
        params = {a.arg for a in cell.args.args}
        assert params <= defined, f"cell takes an undefined name: {params - defined}"
        returned = cell.body[-1]
        if isinstance(returned, ast.Return) and isinstance(returned.value, ast.Tuple):
            defined |= {
                element.id for element in returned.value.elts
                if isinstance(element, ast.Name)
            }
    assert {"pd", "go", "df", "result", "CHART", "fig"} <= defined


def test_ipynb_conversion_rejects_a_non_notebook():
    with pytest.raises(ValueError):
        ipynb_export.to_marimo("not json")


def test_ipynb_conversion_keeps_an_unparseable_cell_as_a_comment():
    document = json.dumps({
        "cells": [{"cell_type": "code", "source": ["def oops(\n"], "outputs": [],
                   "execution_count": None, "metadata": {}}],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    })
    source = ipynb_export.to_marimo(document)
    ast.parse(source)
    assert "# skipped" in source


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
