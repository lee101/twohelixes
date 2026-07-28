"""Notebook export and hosted notebook sessions.

Export is the cheap, always-available path: a chart becomes a notebook the user
can run anywhere with `pip install pandas plotly` (plus marimo, for the marimo
format). Nothing is provisioned and nothing can fail.

Two formats, one `NotebookSpec`, so they cannot drift: marimo for the reactive
version we also host, `.ipynb` for the format every analyst already has open.
`?format=ipynb` on any export endpoint selects the second.

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
from twohelixes.notebooks import ipynb_export, marimo_export

log = logging.getLogger("twohelixes.routes.notebooks")

FORMATS = {
    "marimo": (marimo_export, "py", "text/x-python; charset=utf-8"),
    "ipynb": (ipynb_export, "ipynb", "application/x-ipynb+json; charset=utf-8"),
}


def _format(ctx: router.Context) -> tuple[Any, str, str]:
    """Which export format was asked for, defaulting to marimo."""
    wanted = (ctx.q("format", "") or "").lower()
    if not wanted and isinstance(ctx.json, dict):
        wanted = str(ctx.json.get("format") or "").lower()
    if wanted in ("jupyter", "notebook"):
        wanted = "ipynb"
    return FORMATS.get(wanted, FORMATS["marimo"])


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

    module, extension, content_type = _format(ctx)
    source = module.from_chart(data, _dataset_path_for(identity, data))
    name = "".join(
        c if (c.isalnum() or c in " -_") else "" for c in str(data.get("title") or "chart")
    ).strip().replace(" ", "_")[:60] or "chart"

    return router.Result(
        status=200,
        body=source,
        content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{name}.{extension}"'},
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
    module, extension, content_type = _format(ctx)
    source = module.build(spec)

    return router.Result(
        status=200,
        body=source,
        content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="notebook.{extension}"'},
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

    module, extension, content_type = _format(ctx)
    source = module.build(
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
        content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{key}.{extension}"'},
    )


# --------------------------------------------------------------------------
# Hosted sessions
# --------------------------------------------------------------------------


@router.get("/v1/notebook/sessions")
def list_sessions(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    from twohelixes.notebooks import runner

    return router.json_result({"sessions": runner.list_for_user(identity.user_id)})


@router.get("/v1/notebook/machines")
def list_machines(ctx: router.Context) -> router.Result:
    """The machine catalogue, priced for whoever is asking.

    One source for the picker and the pricing page, because a picker that says
    69 credits and a page that says 60 is a support ticket.
    """
    from twohelixes import machines

    identity = auth.identify(ctx)
    plan = getattr(identity, "plan", "") if identity else ""
    included = config.PLAN_ALLOWANCES.get(plan or "free", {}).get("notebook_minute", 0)
    return router.json_result({
        "machines": machines.catalog_for(plan),
        "default": machines.DEFAULT_ID,
        "included_minutes": included,
        "included_class": config.MACHINE_INCLUDED_CLASS,
        "prices_as_of": machines.PRICES_AS_OF,
    })


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

    # An .ipynb we exported has to be runnable *here*, not only on the reader's
    # laptop - otherwise "export" and "host" are two different products.
    if source.lstrip().startswith("{"):
        try:
            source = ipynb_export.to_marimo(source)
        except ValueError as exc:
            return router.error(400, "invalid_notebook", str(exc))

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

    from twohelixes import machines

    try:
        machine = machines.get(str(body.get("machine") or "") or None)
    except machines.UnknownMachine as exc:
        return router.error(400, "unknown_machine", str(exc))

    try:
        machines.check_allowed(getattr(identity, "plan", ""), machine)
    except machines.MachineNotAllowed as exc:
        return router.error(403, "machine_not_allowed", str(exc))

    # Checked before anything is provisioned, and stated in credits rather than
    # "insufficient balance": the person needs to know how much to top up, and
    # a GPU wants a quarter hour of runway before it is worth starting.
    balance = credits.balance(identity.user_id)
    if balance < machine.minimum_credits:
        return router.error(
            402, "credits_required",
            f"{machine.label} costs {machine.credits_per_minute} credits a minute; "
            f"starting one needs {machine.minimum_credits} credits and you have {balance}.",
        )

    try:
        session = runner.start(
            identity.user_id,
            source,
            str(body.get("title") or "Notebook"),
            machine_id=machine.id,
            plan=getattr(identity, "plan", ""),
        )
    except machines.MachineNotAllowed as exc:
        return router.error(403, "machine_not_allowed", str(exc))
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
