"""The chart pipeline: search -> join -> transform -> chart config -> defaults.

Each stage emits its reasoning onto the stream as it happens, so the UI shows
the agent working rather than a spinner. A stage that fails degrades instead of
aborting: a failed transform falls back to the cleaned frame, a failed chart
choice falls back to a heuristic pick. The user gets a chart with a note about
what was approximated, which beats an error page.

Stage boundaries are where retries happen. Re-running a failed transform costs
one LLM call; re-running the whole pipeline costs five.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from twohelixes import config, llm
from twohelixes.charts import defaults as chart_defaults
from twohelixes.interpreter import sandbox, tools
from twohelixes.pipeline import figures, prompts, route

log = logging.getLogger("twohelixes.pipeline")

MAX_ROWS_TO_CHART = 5000
MAX_TRANSFORM_ATTEMPTS = 3

# Every model stage retries independently, so without one shared budget three
# slow stages can outlive nginx's own read timeout - and a chart delivered
# after the connection dropped is no chart at all. Past this, stages take
# their deterministic path.
PIPELINE_BUDGET_SECONDS = 240.0


@dataclass
class Stage:
    name: str
    started: float = 0.0
    finished: float = 0.0
    ok: bool = True
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ms(self) -> int:
        return int((self.finished - self.started) * 1000)


@dataclass
class PipelineResult:
    figure: dict[str, Any] | None = None
    config: dict[str, Any] = field(default_factory=dict)
    data_preview: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    row_count: int = 0
    trace: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    audit: list[dict[str, str]] = field(default_factory=list)
    transform_code: str = ""
    interpretation: str = ""
    elapsed_ms: int = 0
    # What the run cost us at the gateway, in millionths of a dollar. The
    # price of a query is set from this, so it travels with the result.
    cost_micros: int = 0
    model_calls: int = 0
    # The frame the chart was actually drawn from. Not part of the payload -
    # the preview is what the client gets - but the caller needs it to keep
    # the rows for data that has no source to re-query.
    frame: Any = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "figure": self.figure,
            "config": self.config,
            "preview": self.data_preview,
            "columns": self.columns,
            "row_count": self.row_count,
            "trace": self.trace,
            "warnings": self.warnings,
            "audit": self.audit,
            "transform_code": self.transform_code,
            "interpretation": self.interpretation,
            "elapsed_ms": self.elapsed_ms,
        }


class Emitter:
    """Wraps a stream so a pipeline run works with or without one."""

    def __init__(self, stream: Any = None, deadline: float = 0.0):
        self.stream = stream
        self.trace: list[dict[str, Any]] = []
        self.deadline = deadline

    def out_of_time(self) -> bool:
        """Whether the next model call would run past the request's budget.

        Each stage retries independently, so three slow stages could otherwise
        add up past the proxy's own read timeout - and a chart nobody is still
        connected to is the same as no chart. Past the deadline every stage
        takes its deterministic path instead.
        """
        return self.deadline > 0 and time.time() >= self.deadline

    def stage(self, name: str, status: str, **detail: Any) -> None:
        event = {"stage": name, "status": status, "at": time.time(), **detail}
        self.trace.append(event)
        if self.stream is not None:
            self.stream.emit("stage", event)

    def thought(self, text: str, stage: str = "") -> None:
        """A human-readable line of reasoning, shown as it arrives."""
        event = {"text": text, "stage": stage}
        self.trace.append({"thought": text, "stage": stage, "at": time.time()})
        if self.stream is not None:
            self.stream.emit("thought", event)

    def delta(self, text: str, stage: str = "") -> None:
        """Token-level updates while a model is still writing."""
        if self.stream is not None:
            self.stream.emit("delta", {"text": text, "stage": stage})

    def warn(self, text: str) -> None:
        self.trace.append({"warning": text, "at": time.time()})
        if self.stream is not None:
            self.stream.emit("warning", {"text": text})

    def partial(self, name: str, payload: Any) -> None:
        if self.stream is not None:
            self.stream.emit("partial", {"name": name, "value": payload})


def run(
    question: str,
    frames: dict[str, Any],
    *,
    catalog: list[dict[str, Any]] | None = None,
    stream: Any = None,
    model: str = config.MODEL_DEFAULT,
    mode: str = "light",
    existing_config: dict[str, Any] | None = None,
    edit_request: str = "",
) -> PipelineResult:
    """Run the full pipeline for one question."""
    started = time.time()
    emit = Emitter(stream, deadline=started + PIPELINE_BUDGET_SECONDS)
    result = PipelineResult()

    if not frames:
        emit.warn("No data is connected yet.")
        result.trace = emit.trace
        return result

    # One measurement around the whole run, salvage included: what a query
    # costs us is what we spent before the user got their chart, not what the
    # happy path spent.
    with llm.measure() as spend:
        try:
            out = _run(question, frames, result, emit, started,
                       catalog=catalog, model=model, mode=mode,
                       existing_config=existing_config, edit_request=edit_request)
        except Exception as exc:  # noqa: BLE001
            # The promise is a chart every time, so the last line of defence
            # draws the widest frame we still hold rather than surfacing a
            # stack trace.
            log.exception("pipeline failed; falling back to a raw view")
            emit.warn(f"The pipeline could not finish ({exc}); charting the source data.")
            out = _salvage(frames, result, emit, started, mode)

    out.cost_micros = spend.micros
    out.model_calls = spend.calls
    log.info(
        "pipeline spend: %d calls, %d in / %d out tokens, %.3f cents",
        spend.calls, spend.input_tokens, spend.output_tokens, spend.cents,
    )
    return out


def _run(
    question: str,
    frames: dict[str, Any],
    result: PipelineResult,
    emit: Emitter,
    started: float,
    *,
    catalog: list[dict[str, Any]] | None,
    model: str,
    mode: str,
    existing_config: dict[str, Any] | None,
    edit_request: str,
) -> PipelineResult:
    # ---- stage 1: search -------------------------------------------------
    emit.stage("search", "start")
    search = _search(question, frames, catalog or [], emit, model)
    result.interpretation = search.get("interpretation") or question
    emit.stage(
        "search",
        "done",
        datasets=search.get("datasets"),
        confidence=search.get("confidence"),
    )

    selected = {
        name: frame
        for name, frame in frames.items()
        if not search.get("datasets") or name in search["datasets"]
    }
    if not selected:
        # Discovery found nothing that answers the question. Charting everything
        # is still more use than an error, but saying so is what stops the user
        # reading an unrelated chart as an answer.
        emit.warn(
            "Nothing here obviously answers that question; charting the data "
            "that is connected so you can see what is available."
        )
        selected = frames

    # ---- stage 2: join ---------------------------------------------------
    if len(selected) > 1:
        emit.stage("join", "start")
        try:
            frame, join_detail = _join(selected, emit, model)
        except Exception as exc:  # noqa: BLE001
            emit.warn(f"The join failed ({exc}); charting the largest table on its own.")
            frame = max(selected.values(), key=lambda f: len(f))
            join_detail = {"fallback": "join_failed"}
        emit.stage("join", "done", **join_detail)
    else:
        frame = next(iter(selected.values()))
        join_detail = {}

    try:
        frame = tools.clean_frame(frame)
    except Exception as exc:  # noqa: BLE001
        emit.warn(f"Could not clean the columns ({exc}); using them as they arrived.")

    if len(frame) == 0:
        emit.warn("The selected data has no rows, so the chart will be empty.")

    # ---- stage 3: transform ---------------------------------------------
    emit.stage("transform", "start")
    shaped, transform_detail = _transform(question, frame, emit, model)
    result.transform_code = transform_detail.get("code", "")
    emit.stage("transform", "done", **{k: v for k, v in transform_detail.items() if k != "code"})

    # An empty result is a real answer to some questions, but it is never a
    # chart. Falling back to the unshaped frame at least shows the shape of
    # what was there, with the note saying why.
    if len(shaped) == 0 and len(frame) > 0:
        emit.warn("The shaped result had no rows; charting the source data instead.")
        shaped = frame

    if len(shaped) > MAX_ROWS_TO_CHART:
        emit.warn(
            f"Charting a {len(shaped):,}-row frame; sampling to {MAX_ROWS_TO_CHART:,}."
        )
        shaped = shaped.head(MAX_ROWS_TO_CHART)

    # ---- stage 4: chart configuration -----------------------------------
    emit.stage("chart", "start")
    try:
        if edit_request and existing_config:
            chart_config = _edit(edit_request, existing_config, shaped, emit, model)
        else:
            chart_config = _choose_chart(question, shaped, emit, model)
    except Exception as exc:  # noqa: BLE001
        emit.warn(f"Chart selection failed ({exc}); picking a form from the data alone.")
        chart_config = figures.heuristic_config(shaped, question)
    emit.stage("chart", "done", chart_type=chart_config.get("chart_type"))
    emit.partial("config", chart_config)

    # ---- stage 5: build and apply defaults ------------------------------
    emit.stage("render", "start")
    figure, build_warnings, chart_config = _build(shaped, chart_config, mode, emit)
    figure = chart_defaults.apply(
        figure, mode=mode, chart_type=str(chart_config.get("chart_type") or "")
    )
    findings = chart_defaults.audit(figure, mode=mode)
    emit.stage("render", "done", issues=len(findings))

    for warning in build_warnings:
        emit.warn(warning)
    for finding in findings:
        emit.warn(f"{finding['code']}: {finding['detail']}")

    result.figure = figure
    result.config = chart_config
    result.frame = shaped
    result.columns = [str(c) for c in shaped.columns]
    result.row_count = int(len(shaped))
    result.data_preview = _preview(shaped)
    result.audit = findings
    result.warnings = [t["warning"] for t in emit.trace if "warning" in t]
    result.trace = emit.trace
    result.elapsed_ms = int((time.time() - started) * 1000)
    return result


def _build(
    shaped: Any, chart_config: dict[str, Any], mode: str, emit: Emitter
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Build the figure, stepping down through simpler forms until one draws.

    A chart config that names a column the shaped frame does not have, or a
    form the data cannot support, is the most common way a run used to end
    with nothing. Rather than fail, drop to the form the data's own roles
    imply, and past that to a table - which any frame can always produce.
    """
    try:
        figure, warnings = figures.build(shaped, chart_config, mode=mode)
        return figure, warnings, chart_config
    except Exception as exc:  # noqa: BLE001
        emit.warn(f"Could not draw a {chart_config.get('chart_type')} ({exc}); "
                  "falling back to a form the data supports.")

    fallback = figures.heuristic_config(shaped, "")
    if fallback.get("chart_type") != chart_config.get("chart_type"):
        try:
            figure, warnings = figures.build(shaped, fallback, mode=mode)
            return figure, warnings, fallback
        except Exception as exc:  # noqa: BLE001
            emit.warn(f"That form did not draw either ({exc}); showing the rows.")

    table = {"chart_type": "table", "title": str(chart_config.get("title") or "Data")}
    figure, warnings = figures.build(shaped, table, mode=mode)
    return figure, warnings, table


def _salvage(
    frames: dict[str, Any],
    result: PipelineResult,
    emit: Emitter,
    started: float,
    mode: str,
) -> PipelineResult:
    """Last resort: chart the largest source frame, or say there was nothing."""
    frame = max(frames.values(), key=lambda f: len(f)) if frames else None
    if frame is not None:
        try:
            chart_config = figures.heuristic_config(frame, "")
            figure, _, chart_config = _build(frame, chart_config, mode, emit)
            result.figure = chart_defaults.apply(
                figure, mode=mode, chart_type=str(chart_config.get("chart_type") or "")
            )
            result.config = chart_config
            result.frame = frame
            result.columns = [str(c) for c in frame.columns]
            result.row_count = int(len(frame))
            result.data_preview = _preview(frame)
        except Exception:  # noqa: BLE001
            log.exception("salvage render failed")
            emit.warn("There was no chart to draw from this data.")

    result.warnings = [t["warning"] for t in emit.trace if "warning" in t]
    result.trace = emit.trace
    result.elapsed_ms = int((time.time() - started) * 1000)
    return result


# --------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------


def _search(
    question: str,
    frames: dict[str, Any],
    catalog: list[dict[str, Any]],
    emit: Emitter,
    model: str,
) -> dict[str, Any]:
    """Decide which datasets and columns the question needs."""
    if len(frames) == 1 and not catalog:
        only = next(iter(frames))
        emit.thought(f"Using the only connected dataset: {only}.", "search")
        return {"datasets": [only], "interpretation": question, "confidence": 1.0}

    profiles = {name: tools.profile(frame) for name, frame in frames.items()}
    prompt = (
        f"Question: {question}\n\n"
        f"Available datasets:\n{_format_catalog(profiles, catalog)}"
    )

    try:
        answer = llm.json_call(prompt, system=prompts.SEARCH_SYSTEM, model=model)
    except llm.LLMError as exc:
        emit.warn(f"Discovery fell back to all datasets ({exc}).")
        return {"datasets": list(frames), "interpretation": question, "confidence": 0.3}

    if answer.get("reasoning"):
        emit.thought(str(answer["reasoning"]), "search")
    return answer


def _format_catalog(
    profiles: dict[str, dict[str, Any]], catalog: list[dict[str, Any]]
) -> str:
    lines: list[str] = []
    for name, profile in profiles.items():
        columns = ", ".join(
            f"{c['name']} ({c['dtype']}, {c['role']})" for c in profile["columns"][:40]
        )
        lines.append(f"- {name}: {profile['rows']} rows; columns: {columns}")
    for entry in catalog:
        lines.append(f"- {entry.get('name')}: {entry.get('description', '')}")
    return "\n".join(lines)


def _join(
    frames: dict[str, Any], emit: Emitter, model: str
) -> tuple[Any, dict[str, Any]]:
    """Merge the selected frames, letting value overlap pick the keys."""
    names = list(frames)
    base_name = names[0]
    merged = frames[base_name]
    detail: dict[str, Any] = {"steps": []}

    for name in names[1:]:
        right = frames[name]
        candidates = tools.suggest_join_keys(merged, right)
        if not candidates:
            emit.warn(f"No shared values between {base_name} and {name}; skipping it.")
            continue

        emit.thought(
            f"Joining {base_name} to {name} on "
            f"{candidates[0]['left']} = {candidates[0]['right']} "
            f"({candidates[0]['overlap']:.0%} value overlap).",
            "join",
        )
        merged, step = tools.smart_join(merged, right)
        step["right_dataset"] = name
        detail["steps"].append(step)

        if step.get("rows_after", 0) > step.get("rows_before", 0) * 5:
            emit.warn(
                f"The join to {name} multiplied rows "
                f"{step['rows_before']:,} -> {step['rows_after']:,}; "
                "the key is probably not unique."
            )

    return merged, detail


def _transform(
    question: str, frame: Any, emit: Emitter, model: str
) -> tuple[Any, dict[str, Any]]:
    """Ask for reshaping code and run it, retrying on the error text."""
    profile = tools.profile(frame)
    base_prompt = (
        f"Question: {question}\n\n"
        f"Frame profile:\n{profile}\n\n"
        "Write the transformation."
    )

    last_error = ""
    for attempt in range(MAX_TRANSFORM_ATTEMPTS):
        prompt = base_prompt
        if last_error:
            prompt += (
                f"\n\nYour previous attempt failed with:\n{last_error}\n"
                "Fix it and return the corrected code."
            )

        try:
            answer = llm.json_call(prompt, system=prompts.TRANSFORM_SYSTEM, model=model)
        except llm.LLMError as exc:
            emit.warn(f"Transformation stage unavailable ({exc}); charting raw data.")
            return frame, {"code": "", "fallback": "raw"}

        code = str(answer.get("code") or "")
        if answer.get("explanation"):
            emit.thought(str(answer["explanation"]), "transform")

        if not code.strip():
            return frame, {"code": "", "fallback": "empty"}

        run_result = sandbox.run(code, {"df": frame}, timeout=45.0)
        if run_result.ok:
            shaped = run_result.variables.get("result")
            if shaped is None:
                shaped = run_result.value
            if shaped is None or not hasattr(shaped, "columns"):
                last_error = "`result` was not assigned a DataFrame."
                continue
            if len(shaped) == 0:
                emit.warn("The transformation produced no rows; using the source data.")
                return frame, {"code": code, "fallback": "empty_result"}
            return shaped, {
                "code": code,
                "explanation": answer.get("explanation", ""),
                "rows": int(len(shaped)),
                "attempts": attempt + 1,
            }

        last_error = f"{run_result.error_type}: {run_result.error}"
        emit.thought(f"That failed ({last_error}); retrying.", "transform")

    emit.warn("Could not shape the data; charting it as-is.")
    return frame, {"code": "", "fallback": "failed", "error": last_error}


def _small_call(prompt: str, system: str, model: str) -> dict[str, Any]:
    """A small structured decision: cheap model first, the caller's model next.

    The chart form and the meaning of a one-line edit are small JSON objects
    over a profile we have already computed, and `validate_config` repairs
    whatever comes back against the real frame - so the cheap tier's worst
    case is a form we would have picked heuristically anyway. The expensive
    model stays one retry away, because "cheaper" must not mean "sometimes no
    answer": a failed mini call escalates rather than surfacing.
    """
    try:
        return llm.json_call(prompt, system=system, model=config.MODEL_MINI, attempts=2)
    except llm.CircuitOpen:
        raise
    except llm.LLMError as exc:
        log.info("mini model declined (%s); escalating to %s", exc, model)
        return llm.json_call(prompt, system=system, model=model)


def _choose_chart(
    question: str, frame: Any, emit: Emitter, model: str
) -> dict[str, Any]:
    """Pick the chart form and channel mapping, with a heuristic fallback."""
    # A question that names its own chart and has the columns to support it
    # does not need a model. This is a gate, not a chooser: it abstains on
    # anything ambiguous, so the cost of it being cautious is one model call
    # and the cost of it being wrong would be a wrong chart.
    routed = route.classify(question, frame)
    if routed is not None:
        emit.thought(routed.reason, "chart")
        return figures.validate_config(routed.config, frame, emit)

    profile = tools.profile(frame)
    prompt = (
        f"Question: {question}\n\n"
        f"Shaped data profile:\n{profile}\n\n"
        "Choose the chart."
    )

    try:
        answer = _small_call(prompt, prompts.GRAPH_SYSTEM, model)
    except llm.LLMError as exc:
        emit.warn(f"Chart selection fell back to heuristics ({exc}).")
        return figures.heuristic_config(frame, question)

    if answer.get("reasoning"):
        emit.thought(str(answer["reasoning"]), "chart")

    return figures.validate_config(answer, frame, emit)


def _edit(
    request: str,
    existing: dict[str, Any],
    frame: Any,
    emit: Emitter,
    model: str,
) -> dict[str, Any]:
    """Apply a plain-language edit to an existing chart configuration."""
    prompt = (
        f"Current configuration:\n{existing}\n\n"
        f"Data profile:\n{tools.profile(frame)}\n\n"
        f"User request: {request}"
    )

    try:
        answer = _small_call(prompt, prompts.EDIT_SYSTEM, model)
    except llm.LLMError as exc:
        emit.warn(f"Edit could not be interpreted ({exc}); keeping the chart.")
        return existing

    if answer.get("explanation"):
        emit.thought(str(answer["explanation"]), "chart")

    updated = dict(existing)
    updated.update(answer.get("config_updates") or {})
    return figures.validate_config(updated, frame, emit)


def _preview(frame: Any, limit: int = 50) -> list[dict[str, Any]]:
    try:
        return frame.head(limit).to_dict("records")
    except Exception:  # noqa: BLE001
        return []
