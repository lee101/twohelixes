"""Long-running agents and deep research.

Paid-only, because a single run costs many model calls and can execute code
for minutes. Credits are charged up front and refunded if the run fails before
producing findings - the user should not pay for a crash.

A job outlives the request that started it: the stream can disconnect and
reconnect, and `/v1/jobs/{id}` returns the state either way.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from twohelixes import auth, config, credits, llm, router, store
from twohelixes.interpreter import sandbox, tools
from twohelixes.pipeline import prompts

log = logging.getLogger("twohelixes.routes.agents")

MAX_STEPS = 24
MAX_STEP_SECONDS = 900


@router.get("/v1/jobs")
def list_jobs(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT id, kind, prompt, status, credits_spent, created_at, updated_at "
        "FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 100",
        (identity.user_id,),
    )
    return router.json_result({"jobs": store.rows_to_dicts(rows)})


@router.get("/v1/jobs/{job_id}")
def get_job(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = store.one(
        "SELECT * FROM jobs WHERE id = ? AND user_id = ?",
        (ctx.params["job_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    data = store.row_to_dict(row) or {}
    data["progress"] = store.load_json(data.get("progress"), [])
    data["result"] = store.load_json(data.get("result"), None)
    return router.json_result(data)


@router.stream("/v1/agent/stream")
def run_agent(stream: Any, ctx: router.Context) -> None:
    """Deep research: hypothesise, test against the data, revise, report."""
    identity = ctx.user
    if identity is None or not identity.signed_in:
        stream.emit("error", {"code": "signin_required"})
        return

    if not identity.paid:
        stream.emit(
            "error",
            {
                "code": "credits_required",
                "message": "Long-running agents need API credits.",
            },
        )
        return

    kind = str(ctx.field("kind") or "deep_research")
    cost = config.CREDIT_COST.get(kind, config.CREDIT_COST["deep_research"])
    if identity.api_credits < cost:
        stream.emit(
            "error",
            {"code": "insufficient_credits", "message": f"This run costs {cost} credits."},
        )
        return

    goal = str(ctx.field("goal") or ctx.field("q") or "").strip()
    if not goal:
        stream.emit("error", {"code": "missing_goal"})
        return

    from twohelixes.routes import query as query_routes

    try:
        frames = query_routes._load_frames(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        stream.emit("error", {"code": "data_unavailable", "message": str(exc)})
        return

    if not frames:
        stream.emit("error", {"code": "no_data"})
        return

    job_id = store.new_id()
    now = time.time()
    store.execute(
        "INSERT INTO jobs (id, user_id, kind, prompt, status, progress, "
        "credits_spent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, identity.user_id, kind, goal, "running", store.dump_json([]), cost, now, now),
    )
    credits.deduct(identity.user_id, cost, reason=kind, ref=job_id)
    stream.emit("job", {"id": job_id, "credits_charged": cost})

    findings: list[str] = []
    progress: list[dict[str, Any]] = []
    charts: list[dict[str, Any]] = []
    variables = dict(frames)
    profiles = {name: tools.profile(frame) for name, frame in frames.items()}
    transcript: list[str] = [f"Goal: {goal}", f"Available frames: {list(frames)}", f"Profiles: {profiles}"]

    deadline = time.time() + MAX_STEP_SECONDS
    failed_early = True

    for step in range(MAX_STEPS):
        if time.time() > deadline:
            stream.emit("note", {"message": "Time budget reached; summarising."})
            break

        stream.emit("step_start", {"step": step})
        try:
            plan = llm.json_call(
                "\n\n".join(transcript[-12:]),
                system=prompts.DEEP_RESEARCH_SYSTEM,
                model=config.MODEL_DEEP,
                max_tokens=3000,
            )
        except llm.LLMError as exc:
            stream.emit("error", {"code": "llm_unavailable", "message": str(exc)})
            break

        thought = str(plan.get("thought") or "")
        if thought:
            stream.emit("thought", {"text": thought, "step": step})

        action = str(plan.get("action") or "finish")
        record: dict[str, Any] = {"step": step, "thought": thought, "action": action}

        if action == "code":
            code = str(plan.get("code") or "")
            stream.emit("code", {"step": step, "code": code})
            run_result = sandbox.run(code, variables, timeout=90.0)
            record["ok"] = run_result.ok
            record["stdout"] = run_result.stdout[-2000:]

            if run_result.ok:
                variables.update(run_result.variables)
                transcript.append(f"Ran:\n{code}\nOutput:\n{run_result.stdout[-3000:]}")
                stream.emit(
                    "output", {"step": step, "stdout": run_result.stdout[-4000:], "ok": True}
                )
                failed_early = False
            else:
                record["error"] = run_result.error
                transcript.append(
                    f"Ran:\n{code}\nFailed: {run_result.error_type}: {run_result.error}"
                )
                stream.emit(
                    "output",
                    {"step": step, "error": run_result.error, "ok": False},
                )

        elif action == "chart":
            request = str(plan.get("chart_request") or "")
            stream.emit("chart_start", {"step": step, "request": request})
            from twohelixes.pipeline import orchestrator

            chart = orchestrator.run(
                request,
                {k: v for k, v in variables.items() if hasattr(v, "columns")},
                model=config.MODEL_ESCALATE,
            )
            if chart.figure is not None:
                charts.append({"request": request, "figure": chart.figure})
                stream.emit("chart", {"step": step, "figure": chart.figure})
                transcript.append(f"Built a chart for: {request}")
                failed_early = False

        for finding in plan.get("findings") or []:
            text = str(finding).strip()
            if text and text not in findings:
                findings.append(text)
                stream.emit("finding", {"text": text})

        progress.append(record)
        store.execute(
            "UPDATE jobs SET progress = ?, updated_at = ? WHERE id = ?",
            (store.dump_json(progress[-40:]), time.time(), job_id),
        )

        if plan.get("done") or action == "finish":
            break

    result = {"findings": findings, "charts": charts, "steps": len(progress)}
    status = "complete" if findings else "empty"

    if failed_early and not findings:
        # Nothing was produced: return the credits rather than charge for a
        # run that told the user nothing.
        credits.grant(identity.user_id, cost, reason=f"{kind}_refund", ref=job_id)
        status = "failed"
        stream.emit("refund", {"credits": cost})

    store.execute(
        "UPDATE jobs SET status = ?, result = ?, updated_at = ? WHERE id = ?",
        (status, store.dump_json(result), time.time(), job_id),
    )
    stream.emit("complete", {"job_id": job_id, "status": status, **result})


@router.post("/v1/agent/cancel/{job_id}")
def cancel_job(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    store.execute(
        "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND user_id = ?",
        (time.time(), ctx.params["job_id"], identity.user_id),
    )
    return router.json_result({"cancelled": True})
