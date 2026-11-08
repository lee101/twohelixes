"""Coding agent and the browser artifacts it produces.

`POST /v1/code-agent` turns a request (and optionally an existing artifact to
change) into a validated single-file HTML page, stores it per user, and returns
where to view it. `GET /v1/artifacts/{id}/view` serves it with the headers in
`artifacts.VIEW_HEADERS`, which are the actual security boundary.

The model is the caller's choice from a fixed allowlist (DeepSeek by default).
A file that fails validation gets exactly one repair turn with the problems
listed; if it still fails nothing is saved and the user is told why, rather
than receiving an artifact that renders blank.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from twohelixes import artifacts, auth, config, llm, ratelimit, router, store

log = logging.getLogger("twohelixes.routes.artifacts")

MAX_TOKENS = 16_000


def _payload(row: Any, *, include_source: bool = False) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "title": row["title"],
        "kind": row["kind"],
        "model": row["model"],
        "version": row["version"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "view_url": f"/v1/artifacts/{row['id']}/view",
    }
    if include_source:
        out["source"] = row["source"]
    return out


def _owned(artifact_id: str, user_id: str) -> Any:
    return store.one(
        "SELECT * FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, user_id)
    )


@router.get("/v1/code-agent/models")
def agent_models(ctx: router.Context) -> router.Result:
    auth.require(ctx)
    default = config.resolve_agent_model("")
    return router.json_result(
        {
            "default": default,
            "models": [
                {"alias": alias, "model": model, "default": model == default}
                for alias, model in config.AGENT_MODEL_ALIASES.items()
            ],
        }
    )


@router.get("/v1/artifacts")
def list_artifacts(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT id, title, kind, model, version, created_at, updated_at FROM artifacts "
        "WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200",
        (identity.user_id,),
    )
    return router.json_result({"artifacts": [_payload(r) for r in rows]})


@router.get("/v1/artifacts/{artifact_id}")
def get_artifact(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned(ctx.params["artifact_id"], identity.user_id)
    if row is None:
        return router.error(404, "not_found")
    return router.json_result({"artifact": _payload(row, include_source=True)})


@router.get("/v1/artifacts/{artifact_id}/view")
def view_artifact(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned(ctx.params["artifact_id"], identity.user_id)
    if row is None:
        return router.error(404, "not_found")
    return router.html(row["source"], **artifacts.VIEW_HEADERS)


@router.delete("/v1/artifacts/{artifact_id}")
def delete_artifact(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    artifact_id = ctx.params["artifact_id"]
    if _owned(artifact_id, identity.user_id) is None:
        return router.error(404, "not_found")
    store.execute(
        "DELETE FROM artifacts WHERE id = ? AND user_id = ?", (artifact_id, identity.user_id)
    )
    return router.json_result({"deleted": True})


def generate(
    instruction: str, current: str | None, model: str
) -> tuple[str, str, list[str], list[str]]:
    """(reply, source, remaining_problems, notes). Source is '' when unusable.

    One repair turn at most: a second failure means the model cannot satisfy
    the contract for this request, and more turns only spend money.
    """
    notes: list[str] = []
    prompt = artifacts.edit_prompt(instruction, current) if current else instruction
    text = llm.call(
        prompt, system=artifacts.CODE_AGENT_SYSTEM, model=model,
        temperature=0.3, max_tokens=MAX_TOKENS, attempts=2,
    )
    reply, source = artifacts.extract(text)
    problems = artifacts.validate(source)
    if problems:
        notes.append("repaired: " + "; ".join(problems))
        repair = (
            prompt
            + "\n\nYour previous attempt:\n"
            + (source or text)[: artifacts.MAX_SOURCE_BYTES]
            + "\n\n"
            + artifacts.repair_prompt(problems)
        )
        text = llm.call(
            repair, system=artifacts.CODE_AGENT_SYSTEM, model=model,
            temperature=0.2, max_tokens=MAX_TOKENS, attempts=2,
        )
        reply2, source = artifacts.extract(text)
        reply = reply2 or reply
        problems = artifacts.validate(source)
    return reply, (source if not problems else ""), problems, notes


@router.post("/v1/code-agent")
def code_agent(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    instruction = str(ctx.field("prompt") or "").strip()
    if not instruction:
        return router.error(400, "prompt_required")
    if len(instruction) > artifacts.MAX_PROMPT_CHARS:
        return router.error(400, "prompt_too_long")

    current_row = None
    artifact_id = ctx.field("artifact_id")
    if artifact_id:
        current_row = _owned(str(artifact_id), identity.user_id)
        if current_row is None:
            return router.error(404, "artifact_not_found")
    else:
        count = store.one(
            "SELECT COUNT(*) AS n FROM artifacts WHERE user_id = ?", (identity.user_id,)
        )
        if count is not None and int(count["n"]) >= artifacts.MAX_ARTIFACTS_PER_USER:
            return router.error(409, "artifact_limit_reached")

    decision = ratelimit.check(identity, "code_agent")
    if not decision.allowed:
        status = 402 if decision.reason in {"insufficient_credits", "out_of_allowance"} else 429
        return router.Result(status=status, body=decision.to_payload())

    model = config.resolve_agent_model(ctx.field("model"))
    try:
        reply, source, problems, notes = generate(
            instruction, current_row["source"] if current_row else None, model
        )
    except (llm.LLMError, llm.CircuitOpen) as exc:
        return router.error(503, "llm_unavailable", str(exc))

    if not source:
        # Nothing was produced that is safe to show: do not charge for it.
        return router.error(
            422, "artifact_invalid", "; ".join(problems) or "no file produced", model=model
        )

    now = time.time()
    title = artifacts.title_of(source, instruction[:60])
    if current_row is not None:
        store.execute(
            "UPDATE artifacts SET title = ?, source = ?, prompt = ?, model = ?, "
            "version = version + 1, updated_at = ? WHERE id = ? AND user_id = ?",
            (title, source, instruction, model, now, current_row["id"], identity.user_id),
        )
        saved_id = current_row["id"]
    else:
        saved_id = store.new_id()
        store.execute(
            "INSERT INTO artifacts (id, user_id, title, kind, source, prompt, model, "
            "version, created_at, updated_at) VALUES (?, ?, ?, 'html', ?, ?, ?, 1, ?, ?)",
            (saved_id, identity.user_id, title, source, instruction, model, now, now),
        )
    ratelimit.consume(identity, "code_agent")
    row = _owned(saved_id, identity.user_id)
    return router.json_result(
        {"artifact": _payload(row), "reply": reply, "model": model, "notes": notes},
        200 if current_row is not None else 201,
    )
