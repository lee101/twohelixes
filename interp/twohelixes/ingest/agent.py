"""Metered model fallback for sheets the deterministic detector cannot trust."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from twohelixes import config, entitlements, llm
from twohelixes.ingest.clean import apply_report, report_from_override
from twohelixes.ingest.structure import ShapeReport

log = logging.getLogger("twohelixes.ingest.agent")

SYSTEM = """You identify the table structure in a small spreadsheet sample.
Return a JSON object with:
{"header_row": integer, "skip_rows": [integer], "renames": {"old": "new"},
 "types": {"column": "string|integer|number|percent|date|boolean"}}
Row indexes are zero-based. Only refer to rows and columns visible in the
sample. Never return code."""


def _sample(raw: pd.DataFrame) -> str:
    lines: list[str] = []
    for row_index, row in raw.head(40).iterrows():
        cells = []
        for value in row.tolist()[:40]:
            text = "" if pd.isna(value) else str(value).replace("\n", " ")
            cells.append(text[:120])
        lines.append(f"{row_index}: " + " | ".join(cells))
    return "\n".join(lines)[:24000]


def _small_call(prompt: str, model: str) -> dict[str, Any]:
    try:
        return llm.json_call(
            prompt,
            system=SYSTEM,
            model=config.MODEL_MINI,
            attempts=2,
            max_tokens=1200,
        )
    except llm.CircuitOpen:
        raise
    except llm.LLMError as exc:
        log.info("mini structure model declined (%s); escalating to %s", exc, model)
        return llm.json_call(
            prompt,
            system=SYSTEM,
            model=model,
            max_tokens=1200,
        )


def fallback(
    raw: pd.DataFrame,
    deterministic: ShapeReport,
    *,
    identity: Any = None,
    model: str | None = None,
) -> tuple[pd.DataFrame, ShapeReport]:
    """Try a JSON plan, retaining the deterministic frame on every failure."""
    frame = apply_report(raw, deterministic)
    caller_model = model or config.MODEL_DEFAULT
    if identity is not None:
        quote = entitlements.quote(identity, "structure_import")
        if quote.payer == "blocked":
            deterministic.notes.append(
                "Model fallback was skipped because no metered usage was available."
            )
            return frame, deterministic

    try:
        with llm.measure() as spend:
            plan = _small_call(
                "Raw sheet sample:\n" + _sample(raw),
                caller_model,
            )
        report = report_from_override(
            raw, plan, base=deterministic, sheet=deterministic.sheet
        )
        candidate = apply_report(raw, report)
        if candidate.shape[1] < 2:
            raise ValueError("the model plan produced fewer than two columns")
        report.agent_used = True
        report.agent_cost_micros = spend.micros
        report.notes.append("Used the metered structure fallback.")
        if identity is not None:
            entitlements.charge(identity, "structure_import")
        return candidate, report
    except Exception as exc:  # noqa: BLE001 - fallback must retain the sheet
        deterministic.notes.append(
            f"Model fallback was unavailable; kept deterministic structure ({exc})."
        )
        return frame, deterministic


__all__ = ["fallback"]
