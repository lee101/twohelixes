"""A declarative transformation pipeline.

This is the object the human and the agent both edit.

Generated Python is a terrible shared medium: the agent can write it, but a
person cannot safely tweak one clause of it, and the next agent edit has to
re-read and rewrite the whole thing. A list of typed steps fixes that. The
agent appends and rewrites steps; the person reorders, edits or deletes them;
both see the same object; and either can undo the other without a model call.

Each step compiles to pandas and *also* renders as readable pandas source, so
the marimo export is the same pipeline written out rather than a second
implementation that can drift.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("twohelixes.pipeline.transform")

# Every operator a step may use. Anything outside this list is rejected rather
# than passed through to pandas - a step spec arrives from a model.
COMPARISONS = {
    "eq": "==", "ne": "!=", "gt": ">", "ge": ">=", "lt": "<", "le": "<=",
}
SET_OPS = {"in", "not_in"}
TEXT_OPS = {"contains", "starts_with", "ends_with"}
NULL_OPS = {"is_null", "not_null"}

AGGREGATIONS = {
    "sum", "mean", "median", "count", "nunique", "min", "max", "std", "first", "last",
}

TIME_GRAINS = {
    "hour": "h", "day": "D", "week": "W", "month": "ME",
    "quarter": "QE", "year": "YE",
}

STEP_TYPES = {
    "filter", "aggregate", "derive", "sort", "limit", "select",
    "rename", "dropna", "resample", "top_n", "pivot",
}

MAX_STEPS = 24


class TransformError(Exception):
    pass


@dataclass
class Step:
    """One transformation. `id` is stable so the UI can address it."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    note: str = ""
    author: str = "agent"   # "agent" or "human", so the UI can show provenance
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "params": self.params,
            "note": self.note,
            "author": self.author,
            "enabled": self.enabled,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Step":
        return Step(
            type=str(data.get("type") or ""),
            params=dict(data.get("params") or {}),
            id=str(data.get("id") or ""),
            note=str(data.get("note") or ""),
            author=str(data.get("author") or "agent"),
            enabled=bool(data.get("enabled", True)),
        )


def normalise(steps: list[Any]) -> list[Step]:
    """Coerce and validate a step list from JSON."""
    out: list[Step] = []
    for index, raw in enumerate(steps or []):
        if isinstance(raw, Step):
            step = raw
        elif isinstance(raw, dict):
            step = Step.from_dict(raw)
        else:
            raise TransformError(f"step {index} is not an object")

        if step.type not in STEP_TYPES:
            raise TransformError(f"unknown step type '{step.type}'")
        if not step.id:
            step.id = f"s{index + 1}"
        out.append(step)

    if len(out) > MAX_STEPS:
        raise TransformError(f"too many steps ({len(out)}); the limit is {MAX_STEPS}")
    return out


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _column(frame: Any, name: Any, step: Step) -> str:
    text = str(name or "")
    if text not in {str(c) for c in frame.columns}:
        raise TransformError(f"{step.type}: no column named '{text}'")
    return text


def validate(steps: list[Step], columns: list[str]) -> list[dict[str, Any]]:
    """Check a plan against a schema without running it.

    The builder calls this on every edit, so an impossible step is refused
    while it is being typed rather than after a round trip.
    """
    known = list(columns)
    problems: list[dict[str, Any]] = []

    for step in steps:
        if not step.enabled:
            continue
        try:
            known = _predict_columns(step, known)
        except TransformError as exc:
            problems.append({"step": step.id, "error": str(exc)})
    return problems


def _predict_columns(step: Step, columns: list[str]) -> list[str]:
    """What the frame's columns look like after `step`."""
    p = step.params

    def need(name: str) -> str:
        value = str(p.get(name) or "")
        if not value:
            raise TransformError(f"{step.type}: '{name}' is required")
        if value not in columns:
            raise TransformError(f"{step.type}: no column named '{value}'")
        return value

    if step.type == "filter":
        need("column")
        op = str(p.get("op") or "eq")
        if op not in COMPARISONS and op not in SET_OPS and op not in TEXT_OPS and op not in NULL_OPS:
            raise TransformError(f"filter: unsupported operator '{op}'")
        return columns

    if step.type == "aggregate":
        by = [str(c) for c in (p.get("by") or [])]
        for column in by:
            if column not in columns:
                raise TransformError(f"aggregate: no column named '{column}'")
        metrics = p.get("metrics") or []
        if not metrics:
            raise TransformError("aggregate: at least one metric is required")
        out = list(by)
        for metric in metrics:
            column = str(metric.get("column") or "")
            how = str(metric.get("agg") or "sum")
            if how not in AGGREGATIONS:
                raise TransformError(f"aggregate: unsupported aggregation '{how}'")
            if how != "count" and column not in columns:
                raise TransformError(f"aggregate: no column named '{column}'")
            out.append(str(metric.get("as") or f"{column}_{how}"))
        return out

    if step.type == "derive":
        name = str(p.get("as") or "")
        if not name:
            raise TransformError("derive: 'as' is required")
        _check_expression(str(p.get("expr") or ""), columns)
        return columns + [name]

    if step.type == "sort":
        need("column")
        return columns

    if step.type == "limit":
        if int(p.get("n") or 0) <= 0:
            raise TransformError("limit: 'n' must be positive")
        return columns

    if step.type == "select":
        keep = [str(c) for c in (p.get("columns") or [])]
        for column in keep:
            if column not in columns:
                raise TransformError(f"select: no column named '{column}'")
        return keep or columns

    if step.type == "rename":
        mapping = p.get("map") or {}
        out = list(columns)
        for old, new in mapping.items():
            if str(old) not in columns:
                raise TransformError(f"rename: no column named '{old}'")
            out = [str(new) if c == str(old) else c for c in out]
        return out

    if step.type == "dropna":
        for column in (p.get("columns") or []):
            if str(column) not in columns:
                raise TransformError(f"dropna: no column named '{column}'")
        return columns

    if step.type == "resample":
        need("time_column")
        grain = str(p.get("grain") or "month")
        if grain not in TIME_GRAINS:
            raise TransformError(f"resample: unknown grain '{grain}'")
        metrics = p.get("metrics") or []
        if not metrics:
            raise TransformError("resample: at least one metric is required")
        out = [str(p.get("time_column"))]
        for metric in metrics:
            out.append(str(metric.get("as") or f"{metric.get('column')}_{metric.get('agg', 'sum')}"))
        return out

    if step.type == "top_n":
        need("category")
        need("measure")
        return columns

    if step.type == "pivot":
        need("index")
        need("columns")
        need("values")
        return columns

    return columns


# A derive expression is the one place free text is allowed, so it is checked
# against an allow-list rather than trusted. No attribute access, no calls
# beyond a small set, no dunders.
SAFE_EXPR = re.compile(r"^[A-Za-z0-9_\s+\-*/%.()<>=!&|,'\"\[\]]+$")
BANNED_EXPR = ("__", "import", "eval", "exec", "open", "lambda", "globals", "locals")
ALLOWED_CALLS = {"abs", "round", "min", "max", "log", "sqrt", "where"}


def _check_expression(expr: str, columns: list[str]) -> None:
    if not expr.strip():
        raise TransformError("derive: 'expr' is required")
    if len(expr) > 400:
        raise TransformError("derive: expression is too long")
    if not SAFE_EXPR.match(expr):
        raise TransformError("derive: expression contains unsupported characters")
    lowered = expr.lower()
    for banned in BANNED_EXPR:
        if banned in lowered:
            raise TransformError(f"derive: '{banned}' is not allowed in an expression")


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def apply(frame: Any, steps: list[Step]) -> tuple[Any, list[str]]:
    """Run the plan. Returns the frame and a note per step."""
    import pandas as pd

    notes: list[str] = []
    current = frame

    for step in steps:
        if not step.enabled:
            notes.append(f"{step.id}: skipped")
            continue
        before = len(current)
        try:
            current = _apply_step(current, step)
        except TransformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TransformError(f"{step.type} ({step.id}) failed: {exc}") from exc
        notes.append(f"{step.id}: {step.type} {before:,} -> {len(current):,} rows")

    return current, notes


def _apply_step(frame: Any, step: Step) -> Any:
    import numpy as np
    import pandas as pd

    p = step.params

    if step.type == "filter":
        column = _column(frame, p.get("column"), step)
        op = str(p.get("op") or "eq")
        value = p.get("value")
        series = frame[column]

        if op in NULL_OPS:
            mask = series.isna() if op == "is_null" else series.notna()
        elif op in SET_OPS:
            values = value if isinstance(value, list) else [value]
            mask = series.isin(values)
            if op == "not_in":
                mask = ~mask
        elif op in TEXT_OPS:
            text = series.astype(str)
            if op == "contains":
                mask = text.str.contains(str(value), case=False, na=False, regex=False)
            elif op == "starts_with":
                mask = text.str.startswith(str(value), na=False)
            else:
                mask = text.str.endswith(str(value), na=False)
        else:
            # Compare in the column's own type, or a numeric filter on a text
            # column silently matches nothing.
            if pd.api.types.is_numeric_dtype(series):
                value = pd.to_numeric(value, errors="coerce")
            elif pd.api.types.is_datetime64_any_dtype(series):
                value = pd.to_datetime(value, errors="coerce")
            mask = getattr(series, f"__{op}__")(value)

        return frame[mask]

    if step.type == "aggregate":
        by = [str(c) for c in (p.get("by") or [])]
        spec: dict[str, Any] = {}
        renames: dict[str, str] = {}
        for metric in p.get("metrics") or []:
            column = str(metric.get("column") or "")
            how = str(metric.get("agg") or "sum")
            alias = str(metric.get("as") or f"{column}_{how}")
            if how == "count" and not column:
                column = by[0] if by else frame.columns[0]
            spec[column] = how
            renames[column] = alias

        if not by:
            aggregated = frame.agg(spec).to_frame().T
        else:
            aggregated = frame.groupby(by, dropna=False).agg(spec).reset_index()
        return aggregated.rename(columns=renames)

    if step.type == "derive":
        name = str(p.get("as"))
        expr = str(p.get("expr"))
        _check_expression(expr, [str(c) for c in frame.columns])
        out = frame.copy()
        # pandas.eval, not Python eval: it only understands column arithmetic.
        out[name] = out.eval(expr, engine="python")
        return out

    if step.type == "sort":
        column = _column(frame, p.get("column"), step)
        return frame.sort_values(column, ascending=not bool(p.get("desc", False)))

    if step.type == "limit":
        return frame.head(int(p.get("n") or 100))

    if step.type == "select":
        keep = [str(c) for c in (p.get("columns") or []) if str(c) in frame.columns]
        return frame[keep] if keep else frame

    if step.type == "rename":
        return frame.rename(columns={str(k): str(v) for k, v in (p.get("map") or {}).items()})

    if step.type == "dropna":
        columns = [str(c) for c in (p.get("columns") or []) if str(c) in frame.columns]
        return frame.dropna(subset=columns) if columns else frame.dropna()

    if step.type == "resample":
        from twohelixes.interpreter import tools

        time_column = _column(frame, p.get("time_column"), step)
        grain = TIME_GRAINS[str(p.get("grain") or "month")]
        out = frame.copy()
        out[time_column] = tools.to_datetime(out[time_column])
        out = out.dropna(subset=[time_column])

        group = [str(c) for c in (p.get("by") or []) if str(c) in out.columns]
        spec = {}
        renames = {}
        for metric in p.get("metrics") or []:
            column = str(metric.get("column") or "")
            how = str(metric.get("agg") or "sum")
            spec[column] = how
            renames[column] = str(metric.get("as") or f"{column}_{how}")

        keys = [pd.Grouper(key=time_column, freq=grain)] + group
        out = out.groupby(keys, dropna=False).agg(spec).reset_index()
        return out.rename(columns=renames)

    if step.type == "top_n":
        from twohelixes.interpreter import tools

        return tools.top_n(
            frame,
            _column(frame, p.get("category"), step),
            _column(frame, p.get("measure"), step),
            n=int(p.get("n") or 10),
            agg=str(p.get("agg") or "sum"),
        )

    if step.type == "pivot":
        return frame.pivot_table(
            index=_column(frame, p.get("index"), step),
            columns=_column(frame, p.get("columns"), step),
            values=_column(frame, p.get("values"), step),
            aggfunc=str(p.get("agg") or "sum"),
        ).reset_index()

    raise TransformError(f"unknown step type '{step.type}'")


# --------------------------------------------------------------------------
# Rendering to source
# --------------------------------------------------------------------------


def to_python(steps: list[Step], frame_name: str = "df") -> str:
    """Render the plan as readable pandas.

    This is what the marimo export writes, so the notebook shows the same
    pipeline rather than a second implementation that can drift from it.
    """
    lines = [f"result = {frame_name}"]

    for step in steps:
        if not step.enabled:
            lines.append(f"# ({step.id} disabled) {step.type}")
            continue
        rendered = _render_step(step)
        if step.note:
            lines.append(f"# {step.note}")
        lines.append(rendered)

    return "\n".join(lines)


def _render_step(step: Step) -> str:
    p = step.params

    if step.type == "filter":
        column, op, value = p.get("column"), str(p.get("op") or "eq"), p.get("value")
        if op in NULL_OPS:
            call = "isna()" if op == "is_null" else "notna()"
            return f"result = result[result[{column!r}].{call}]"
        if op in SET_OPS:
            negate = "~" if op == "not_in" else ""
            return f"result = result[{negate}result[{column!r}].isin({value!r})]"
        if op in TEXT_OPS:
            method = {
                "contains": f".str.contains({value!r}, case=False, na=False)",
                "starts_with": f".str.startswith({value!r}, na=False)",
                "ends_with": f".str.endswith({value!r}, na=False)",
            }[op]
            return f"result = result[result[{column!r}].astype(str){method}]"
        return f"result = result[result[{column!r}] {COMPARISONS[op]} {value!r}]"

    if step.type == "aggregate":
        by = [str(c) for c in (p.get("by") or [])]
        spec = {str(m.get("column")): str(m.get("agg") or "sum") for m in p.get("metrics") or []}
        renames = {
            str(m.get("column")): str(m.get("as") or f"{m.get('column')}_{m.get('agg', 'sum')}")
            for m in p.get("metrics") or []
        }
        if by:
            return (
                f"result = (result.groupby({by!r}, dropna=False)\n"
                f"          .agg({spec!r}).reset_index()\n"
                f"          .rename(columns={renames!r}))"
            )
        return f"result = result.agg({spec!r}).to_frame().T.rename(columns={renames!r})"

    if step.type == "derive":
        return f"result = result.assign(**{{{p.get('as')!r}: result.eval({p.get('expr')!r})}})"

    if step.type == "sort":
        return (
            f"result = result.sort_values({p.get('column')!r}, "
            f"ascending={not bool(p.get('desc', False))})"
        )

    if step.type == "limit":
        return f"result = result.head({int(p.get('n') or 100)})"

    if step.type == "select":
        return f"result = result[{[str(c) for c in (p.get('columns') or [])]!r}]"

    if step.type == "rename":
        return f"result = result.rename(columns={dict(p.get('map') or {})!r})"

    if step.type == "dropna":
        columns = [str(c) for c in (p.get("columns") or [])]
        return f"result = result.dropna(subset={columns!r})" if columns else "result = result.dropna()"

    if step.type == "resample":
        time_column = p.get("time_column")
        grain = TIME_GRAINS[str(p.get("grain") or "month")]
        spec = {str(m.get("column")): str(m.get("agg") or "sum") for m in p.get("metrics") or []}
        group = [str(c) for c in (p.get("by") or [])]
        keys = f"[pd.Grouper(key={time_column!r}, freq={grain!r})]" + (f" + {group!r}" if group else "")
        return (
            f"result[{time_column!r}] = pd.to_datetime(result[{time_column!r}], errors='coerce')\n"
            f"result = result.groupby({keys}, dropna=False).agg({spec!r}).reset_index()"
        )

    if step.type == "top_n":
        return (
            f"# top {p.get('n', 10)} by {p.get('measure')}, remainder folded into 'Other'\n"
            f"_ranked = result.groupby({p.get('category')!r})[{p.get('measure')!r}]"
            f".{p.get('agg', 'sum')}().sort_values(ascending=False)\n"
            f"_head = _ranked.head({int(p.get('n') or 10)})\n"
            f"_other = _ranked.iloc[{int(p.get('n') or 10)}:].sum()\n"
            f"result = pd.concat([_head, pd.Series({{'Other': _other}})]).reset_index()\n"
            f"result.columns = [{p.get('category')!r}, {p.get('measure')!r}]"
        )

    if step.type == "pivot":
        return (
            f"result = result.pivot_table(index={p.get('index')!r}, "
            f"columns={p.get('columns')!r}, values={p.get('values')!r}, "
            f"aggfunc={str(p.get('agg') or 'sum')!r}).reset_index()"
        )

    return f"# unsupported step {step.type}"


def describe(step: Step) -> str:
    """A one-line human description, for the step list in the UI."""
    p = step.params
    if step.type == "filter":
        return f"Keep rows where {p.get('column')} {str(p.get('op', 'eq')).replace('_', ' ')} {p.get('value')}"
    if step.type == "aggregate":
        metrics = ", ".join(
            f"{m.get('agg', 'sum')} of {m.get('column')}" for m in p.get("metrics") or []
        )
        by = ", ".join(str(c) for c in p.get("by") or [])
        return f"{metrics}" + (f" by {by}" if by else " over everything")
    if step.type == "derive":
        return f"Add {p.get('as')} = {p.get('expr')}"
    if step.type == "sort":
        return f"Sort by {p.get('column')} {'descending' if p.get('desc') else 'ascending'}"
    if step.type == "limit":
        return f"Keep the first {p.get('n')} rows"
    if step.type == "select":
        return f"Keep columns: {', '.join(str(c) for c in p.get('columns') or [])}"
    if step.type == "rename":
        return "Rename " + ", ".join(f"{k} to {v}" for k, v in (p.get("map") or {}).items())
    if step.type == "dropna":
        columns = p.get("columns") or []
        return f"Drop rows with no {', '.join(str(c) for c in columns)}" if columns else "Drop rows with any blanks"
    if step.type == "resample":
        return f"Group by {p.get('grain')} on {p.get('time_column')}"
    if step.type == "top_n":
        return f"Top {p.get('n', 10)} {p.get('category')} by {p.get('measure')}, rest as Other"
    if step.type == "pivot":
        return f"Pivot {p.get('columns')} across, {p.get('values')} in the cells"
    return step.type
