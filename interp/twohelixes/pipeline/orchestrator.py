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
from twohelixes.pipeline import figures, prompts

log = logging.getLogger("twohelixes.pipeline")

MAX_ROWS_TO_CHART = 5000
MAX_TRANSFORM_ATTEMPTS = 3


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

    def __init__(self, stream: Any = None):
        self.stream = stream
        self.trace: list[dict[str, Any]] = []

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
    emit = Emitter(stream)
    result = PipelineResult()

    if not frames:
        emit.warn("No data is connected yet.")
        result.trace = emit.trace
        return result

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
    } or frames

    # ---- stage 2: join ---------------------------------------------------
    if len(selected) > 1:
        emit.stage("join", "start")
        frame, join_detail = _join(selected, emit, model)
        emit.stage("join", "done", **join_detail)
    else:
        frame = next(iter(selected.values()))
        join_detail = {}

    frame = tools.clean_frame(frame)

    # ---- stage 3: transform ---------------------------------------------
    emit.stage("transform", "start")
    shaped, transform_detail = _transform(question, frame, emit, model)
    result.transform_code = transform_detail.get("code", "")
    emit.stage("transform", "done", **{k: v for k, v in transform_detail.items() if k != "code"})

    if len(shaped) > MAX_ROWS_TO_CHART:
        emit.warn(
            f"Charting a {len(shaped):,}-row frame; sampling to {MAX_ROWS_TO_CHART:,}."
        )
        shaped = shaped.head(MAX_ROWS_TO_CHART)

    # ---- stage 4: chart configuration -----------------------------------
    emit.stage("chart", "start")
    if edit_request and existing_config:
        chart_config = _edit(edit_request, existing_config, shaped, emit, model)
    else:
        chart_config = _choose_chart(question, shaped, emit, model)
    emit.stage("chart", "done", chart_type=chart_config.get("chart_type"))
    emit.partial("config", chart_config)

    # ---- stage 5: build and apply defaults ------------------------------
    emit.stage("render", "start")
    figure, build_warnings = figures.build(shaped, chart_config, mode=mode)
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
    result.columns = [str(c) for c in shaped.columns]
    result.row_count = int(len(shaped))
    result.data_preview = _preview(shaped)
    result.audit = findings
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


def _choose_chart(
    question: str, frame: Any, emit: Emitter, model: str
) -> dict[str, Any]:
    """Pick the chart form and channel mapping, with a heuristic fallback."""
    profile = tools.profile(frame)
    prompt = (
        f"Question: {question}\n\n"
        f"Shaped data profile:\n{profile}\n\n"
        "Choose the chart."
    )

    try:
        answer = llm.json_call(prompt, system=prompts.GRAPH_SYSTEM, model=model)
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
        answer = llm.json_call(prompt, system=prompts.EDIT_SYSTEM, model=model)
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
