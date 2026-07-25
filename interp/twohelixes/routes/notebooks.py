"""Notebook export and hosted notebook sessions.

Export is the cheap, always-available path: a chart becomes a marimo notebook
the user can run anywhere with `pip install marimo pandas plotly`. Nothing is
provisioned and nothing can fail.

Hosting is the expensive path and is gated behind credits, because a live
notebook is a process with memory and a CPU share for as long as it is open.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from twohelixes import auth, config, credits, router, store
from twohelixes.notebooks import marimo_export

log = logging.getLogger("twohelixes.routes.notebooks")


def _dataset_path_for(identity: Any, row: dict[str, Any]) -> str:
    """Where the notebook should read its data from.

    Prefers a dataset the user already has on disk; a notebook that points at
    a path the reader does not have is worse than one that points at nothing,
    because it looks like it should work.
    """
    dataset = store.one(
        "SELECT storage FROM datasets WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
        (identity.user_id,),
    )
    if dataset and dataset["storage"]:
        return str(dataset["storage"])
    return ""


@router.get("/v1/chart/{chart_id}/notebook")
def export_notebook(ctx: router.Context) -> router.Result:
    """Export a saved chart as a runnable marimo notebook."""
    identity = auth.require(ctx)
    row = store.one(
        "SELECT * FROM charts WHERE id = ? AND user_id = ?",
        (ctx.params["chart_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    data = store.row_to_dict(row) or {}
    data["graph_args"] = store.load_json(data.get("graph_args"), {})
    data["trace"] = store.load_json(data.get("trace"), [])

    source = marimo_export.from_chart(data, _dataset_path_for(identity, data))
    name = "".join(
        c if (c.isalnum() or c in " -_") else "" for c in str(data.get("title") or "chart")
    ).strip().replace(" ", "_")[:60] or "chart"

    return router.Result(
        status=200,
        body=source,
        content_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}.py"'},
    )


@router.post("/v1/notebook/export")
def export_inline(ctx: router.Context) -> router.Result:
    """Export a notebook from a result the client is holding."""
    identity = auth.require(ctx)
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    spec = marimo_export.NotebookSpec(
        title=str(body.get("title") or "twoHelixes notebook"),
        question=str(body.get("question") or ""),
        sql=str(body.get("sql") or ""),
        source_name=str(body.get("source_name") or ""),
        dataset_path=str(body.get("dataset_path") or ""),
        transform_code=str(body.get("transform_code") or ""),
        chart_config=body.get("config") if isinstance(body.get("config"), dict) else {},
        trace=body.get("trace") if isinstance(body.get("trace"), list) else [],
        inline_rows=body.get("rows") if isinstance(body.get("rows"), list) else None,
    )
    source = marimo_export.build(spec)

    return router.Result(
        status=200,
        body=source,
        content_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="notebook.py"'},
    )


@router.post("/v1/notebook/sample/{key}")
def export_sample_notebook(ctx: router.Context) -> router.Result:
    """A starter notebook for a sample dataset."""
    auth.require(ctx)
    from twohelixes.datasets import samples

    key = ctx.params["key"]
    if key not in samples.BY_KEY:
        return router.error(404, "unknown_sample")

    sample = samples.BY_KEY[key]
    samples.materialise()
    frame = samples.frame(key)

    from twohelixes.pipeline import figures

    config_guess = figures.validate_config(figures.heuristic_config(frame, ""), frame)

    source = marimo_export.build(
        marimo_export.NotebookSpec(
            title=sample.name,
            question=sample.questions[0] if sample.questions else "",
            dataset_path=str(samples.path_for(key)),
            chart_config=config_guess,
        )
    )
    return router.Result(
        status=200,
        body=source,
        content_type="text/x-python; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{key}.py"'},
    )


# --------------------------------------------------------------------------
# Hosted sessions
# --------------------------------------------------------------------------


@router.get("/v1/notebook/sessions")
def list_sessions(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    from twohelixes.notebooks import runner

    return router.json_result({"sessions": runner.list_for_user(identity.user_id)})


@router.post("/v1/notebook/sessions")
def start_session(ctx: router.Context) -> router.Result:
    """Start a hosted notebook.

    Paid only: a live notebook holds a process open, so it is billed per
    minute rather than per request.
    """
    identity = auth.require(ctx)
    if not identity.paid:
        return router.error(
            402,
            "credits_required",
            "Hosted notebooks need API credits; export runs anywhere for free.",
        )

    from twohelixes.notebooks import runner

    body = ctx.json if isinstance(ctx.json, dict) else {}
    source = str(body.get("source") or "")

    if not source:
        chart_id = str(body.get("chart_id") or "")
        if chart_id:
            row = store.one(
                "SELECT * FROM charts WHERE id = ? AND user_id = ?",
                (chart_id, identity.user_id),
            )
            if row is None:
                return router.error(404, "chart_not_found")
            data = store.row_to_dict(row) or {}
            data["graph_args"] = store.load_json(data.get("graph_args"), {})
            data["trace"] = store.load_json(data.get("trace"), [])
            source = marimo_export.from_chart(data, _dataset_path_for(identity, data))

    if not source:
        return router.error(400, "nothing_to_run")

    try:
        session = runner.start(identity.user_id, source, str(body.get("title") or "Notebook"))
    except runner.RunnerError as exc:
        return router.error(503, "runner_unavailable", str(exc))

    return router.json_result(session, status=201)


@router.delete("/v1/notebook/sessions/{session_id}")
def stop_session(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    from twohelixes.notebooks import runner

    stopped = runner.stop(identity.user_id, ctx.params["session_id"])
    if not stopped:
        return router.error(404, "not_found")
    return router.json_result({"stopped": True})
