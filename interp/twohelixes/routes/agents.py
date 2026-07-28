"""Long-running agents and deep research.

Paid-only, because a single run costs many model calls and can execute code
for minutes.

Billing is a base fee plus **metered minutes**, not a flat charge. A flat
charge is either a tax on the two-minute runs or a subsidy for the twenty-
minute ones, and the twenty-minute ones are the expensive ones: they hold a
sandbox and a model budget for as long as they take. The meter lives in
`metering.py`, so it keeps running - and keeps being auditable - across the
worker restarts that a fifteen-minute job can easily outlive.

The promise on top of that: **you never pay for a run that told you nothing.**
If the agent produced no findings and no chart, the base and every metered
minute go back.

A job outlives the request that started it. `/v1/agent/stream` is the
interactive path; `/v1/agent` starts the same work in the background and
returns a job id, which is the path an API client wants - polling
`/v1/jobs/{id}` needs no SSE client and survives a dropped connection.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from twohelixes import auth, config, credits, entitlements, llm, metering, router, store
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
    data["billing"] = _billing_for(data["id"])
    return router.json_result(data)


def _billing_for(job_id: str) -> dict[str, Any]:
    """What this job cost, itemised.

    A metered charge nobody can trace back to minutes is a charge nobody can
    check, so the job carries its own meter.
    """
    row = store.one(
        "SELECT rate, minutes, charged, refunded, status, started_at, closed_at "
        "FROM meters WHERE kind = 'long_agent' AND ref = ? ORDER BY started_at DESC LIMIT 1",
        (job_id,),
    )
    meter = store.row_to_dict(row) or {}
    base = int(config.CREDIT_COST.get("deep_research", 150))
    return {
        "base_credits": base,
        "credits_per_minute": int(meter.get("rate") or 0),
        "minutes": int(meter.get("minutes") or 0),
        "metered_credits": int(meter.get("charged") or 0),
        "refunded_credits": int(meter.get("refunded") or 0),
        "meter_status": meter.get("status"),
    }


# --------------------------------------------------------------------------
# The run itself
# --------------------------------------------------------------------------


class _Recorder:
    """Stands in for the SSE stream when a job runs in the background.

    Same interface, so `_run` has one code path: a background job and a
    streamed one cannot drift into behaving differently, which is the usual
    way "the API does something subtly different" happens.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, name: str, payload: Any = None) -> None:
        self.events.append({"event": name, **(payload if isinstance(payload, dict) else {})})


def _run(
    stream: Any,
    identity: Any,
    kind: str,
    goal: str,
    frames: dict[str, Any],
    job_id: str,
    meter_id: str,
    base_cost: int,
) -> dict[str, Any]:
    """Drive the agent loop, emitting as it goes. Returns the final result."""
    findings: list[str] = []
    progress: list[dict[str, Any]] = []
    charts: list[dict[str, Any]] = []
    variables = dict(frames)
    profiles = {name: tools.profile(frame) for name, frame in frames.items()}
    transcript: list[str] = [
        f"Goal: {goal}",
        f"Available frames: {list(frames)}",
        f"Profiles: {profiles}",
    ]

    deadline = time.time() + MAX_STEP_SECONDS
    produced = False

    for step in range(MAX_STEPS):
        if time.time() > deadline:
            stream.emit("note", {"message": "Time budget reached; summarising."})
            break

        if _cancelled(job_id):
            stream.emit("note", {"message": "Cancelled."})
            break

        # The heartbeat is what tells the other workers this job still has an
        # owner; without it a long step looks like an abandoned meter.
        metering.heartbeat(meter_id)
        if _out_of_credits(meter_id):
            stream.emit(
                "note",
                {"message": "Credit balance reached zero; stopping and summarising."},
            )
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
                produced = True
            else:
                record["error"] = run_result.error
                transcript.append(
                    f"Ran:\n{code}\nFailed: {run_result.error_type}: {run_result.error}"
                )
                stream.emit("output", {"step": step, "error": run_result.error, "ok": False})

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
                produced = True

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

    # Stop the clock before deciding what to refund, so the last partial minute
    # is inside the total either way.
    closing = metering.close_meter(meter_id)
    billing = _billing_for(job_id)

    if not findings and not produced:
        # The run said nothing. Give back the metered minutes and the base fee:
        # a user who waited for an answer and got none has already paid enough.
        # An included run gives back the allowance instead - refunding credits
        # it never spent would be minting them.
        metering.refund(meter_id, reason=f"{kind}_refund")
        if base_cost:
            credits.grant(identity.user_id, base_cost, reason=f"{kind}_refund", ref=job_id)
        else:
            entitlements.give_back(identity.user_id, "deep_research", 1)
        status = "failed"
        spent = 0
        stream.emit(
            "refund",
            {"credits": base_cost + billing["metered_credits"], "reason": "nothing produced"},
        )
    else:
        spent = base_cost + billing["metered_credits"]

    store.execute(
        "UPDATE jobs SET status = ?, result = ?, credits_spent = ?, updated_at = ? WHERE id = ?",
        (status, store.dump_json(result), spent, time.time(), job_id),
    )
    stream.emit(
        "complete",
        {
            "job_id": job_id,
            "status": status,
            "credits_spent": spent,
            "billing": {**_billing_for(job_id), "closing_minutes": closing.get("minutes", 0)},
            **result,
        },
    )
    return {"status": status, "credits_spent": spent, **result}


def _cancelled(job_id: str) -> bool:
    row = store.one("SELECT status FROM jobs WHERE id = ?", (job_id,))
    return bool(row) and str(row["status"]) == "cancelled"


def _out_of_credits(meter_id: str) -> bool:
    row = store.one("SELECT status FROM meters WHERE id = ?", (meter_id,))
    return bool(row) and str(row["status"]) == metering.UNPAID


def _prepare(identity: Any, ctx: router.Context) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Everything both entry points need: checks, frames, job row, meter.

    Returns `(error, context)`; the error is a plain dict so the streaming
    handler can emit it and the JSON handler can return it, without either one
    re-implementing the checks.
    """
    if identity is None or not identity.signed_in:
        return {"code": "signin_required", "status": 401}, {}
    if not identity.paid:
        return {
            "code": "upgrade_required",
            "status": 402,
            "message": (
                "Long-running agents need a paid plan or credits. Pro includes "
                f"{config.PLAN_ALLOWANCES['pro']['deep_research']} runs a month."
            ),
        }, {}

    kind = str(ctx.field("kind") or "deep_research")
    base_cost = int(config.CREDIT_COST.get(kind, config.CREDIT_COST["deep_research"]))
    rate = int(config.CREDIT_COST.get("long_agent_minute", 8))

    # A plan that includes deep-research runs covers the whole run - base and
    # minutes. Charging by the minute inside an "included" run would make the
    # allowance meaningless the moment a run took longer than average, which is
    # exactly when the customer notices.
    included = entitlements.take_units(identity.user_id, "deep_research", 1) == 1
    if included:
        base_cost, rate = 0, 0
    else:
        # The base plus a few minutes: starting a run that dies of poverty at
        # step two spends the user's credits on nothing.
        needed = base_cost + rate * metering.MINIMUM_MINUTES
        if identity.api_credits < needed:
            return {
                "code": "insufficient_credits",
                "status": 402,
                "message": (
                    f"This run costs {base_cost} credits plus {rate}/minute; "
                    f"{needed} must be available to start. Pro includes "
                    f"{config.PLAN_ALLOWANCES['pro']['deep_research']} runs a month."
                ),
            }, {}

    goal = str(ctx.field("goal") or ctx.field("q") or "").strip()
    if not goal:
        return {"code": "missing_goal", "status": 400}, {}

    from twohelixes.routes import query as query_routes

    try:
        frames = query_routes._load_frames(identity, ctx)
    except Exception as exc:  # noqa: BLE001
        return {"code": "data_unavailable", "status": 400, "message": str(exc)}, {}
    if not frames:
        return {"code": "no_data", "status": 400}, {}

    job_id = store.new_id()
    now = time.time()
    store.execute(
        "INSERT INTO jobs (id, user_id, kind, prompt, status, progress, "
        "credits_spent, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, identity.user_id, kind, goal, "running", store.dump_json([]), base_cost, now, now),
    )

    metering.start_sweeper()
    try:
        meter = metering.open_meter(identity.user_id, "long_agent", job_id, rate)
    except credits.InsufficientCredits as exc:
        store.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (job_id,))
        return {"code": "insufficient_credits", "status": 402, "message": str(exc)}, {}

    if base_cost:
        credits.deduct(identity.user_id, base_cost, reason=kind, ref=job_id)
    return None, {
        "included": included,
        "kind": kind,
        "goal": goal,
        "frames": frames,
        "job_id": job_id,
        "meter_id": meter["id"],
        "base_cost": base_cost,
        "rate": rate,
    }


@router.stream("/v1/agent/stream")
def run_agent(stream: Any, ctx: router.Context) -> None:
    """Deep research: hypothesise, test against the data, revise, report."""
    identity = ctx.user
    error, work = _prepare(identity, ctx)
    if error:
        stream.emit("error", {k: v for k, v in error.items() if k != "status"})
        return

    stream.emit(
        "job",
        {
            "id": work["job_id"],
            "credits_charged": work["base_cost"],
            "credits_per_minute": work["rate"],
            "included_in_plan": work["included"],
        },
    )
    try:
        _run(
            stream, identity, work["kind"], work["goal"], work["frames"],
            work["job_id"], work["meter_id"], work["base_cost"],
        )
    except Exception:  # noqa: BLE001 - a crash must not leave a meter running
        log.exception("agent job %s failed", work["job_id"])
        metering.close_meter(work["meter_id"], detail="run crashed")
        metering.refund(work["meter_id"], reason=f"{work['kind']}_refund")
        credits.grant(
            identity.user_id, work["base_cost"],
            reason=f"{work['kind']}_refund", ref=work["job_id"],
        )
        store.execute(
            "UPDATE jobs SET status = 'failed', credits_spent = 0, updated_at = ? WHERE id = ?",
            (time.time(), work["job_id"]),
        )
        stream.emit("error", {"code": "agent_failed"})


@router.post("/v1/agent")
def start_agent(ctx: router.Context) -> router.Result:
    """Start a run in the background and return its job id.

    The API path. An HTTP client that wants an answer in twenty minutes should
    not have to hold an SSE connection open for twenty minutes, and a dropped
    connection must not cost the run - the job and its meter live in the
    database, so `/v1/jobs/{id}` answers from any worker.
    """
    identity = auth.require(ctx)
    error, work = _prepare(identity, ctx)
    if error:
        return router.error(
            int(error.get("status", 400)),
            str(error["code"]),
            str(error.get("message", "")),
        )

    def background() -> None:
        recorder = _Recorder()
        try:
            _run(
                recorder, identity, work["kind"], work["goal"], work["frames"],
                work["job_id"], work["meter_id"], work["base_cost"],
            )
        except Exception:  # noqa: BLE001
            log.exception("background agent job %s failed", work["job_id"])
            metering.close_meter(work["meter_id"], detail="run crashed")
            metering.refund(work["meter_id"], reason=f"{work['kind']}_refund")
            credits.grant(
                identity.user_id, work["base_cost"],
                reason=f"{work['kind']}_refund", ref=work["job_id"],
            )
            store.execute(
                "UPDATE jobs SET status = 'failed', credits_spent = 0, updated_at = ? "
                "WHERE id = ?",
                (time.time(), work["job_id"]),
            )

    threading.Thread(
        target=background, name=f"agent-{work['job_id'][:8]}", daemon=True
    ).start()

    return router.json_result(
        {
            "job_id": work["job_id"],
            "status": "running",
            "base_credits": work["base_cost"],
            "credits_per_minute": work["rate"],
            "included_in_plan": work["included"],
            "poll": f"/v1/jobs/{work['job_id']}",
        },
        status=202,
    )


@router.post("/v1/agent/cancel/{job_id}")
def cancel_job(ctx: router.Context) -> router.Result:
    """Cancel a run and stop its meter.

    Marking the row alone left the clock running: the loop reads the status
    between steps, but a step can be ninety seconds of sandbox, and nobody
    should pay for minutes after they asked it to stop.
    """
    identity = auth.require(ctx)
    store.execute(
        "UPDATE jobs SET status = 'cancelled', updated_at = ? WHERE id = ? AND user_id = ?",
        (time.time(), ctx.params["job_id"], identity.user_id),
    )
    meter = metering.by_ref("long_agent", ctx.params["job_id"])
    if meter and meter["user_id"] == identity.user_id:
        metering.close_meter(meter["id"], detail="cancelled by the user")
    return router.json_result({"cancelled": True, "billing": _billing_for(ctx.params["job_id"])})
