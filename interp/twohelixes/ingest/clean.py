"""Apply deterministic or explicitly overridden import plans."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pandas as pd

from twohelixes.ingest.structure import (
    ShapeReport,
    dedupe_names,
    detect_structure,
    parse_number,
)

ALLOWED_TYPES = {"string", "integer", "number", "percent", "date", "boolean"}
_TRUE = {"true", "yes", "y", "on", "enabled", "active", "1"}
_FALSE = {"false", "no", "n", "off", "disabled", "inactive", "0"}


def _empty(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except (TypeError, ValueError):
        pass
    return str(value).strip().lower() in {"", "nan", "none", "null", "<na>"}


def _coerce(series: pd.Series, kind: str) -> pd.Series:
    if kind in {"integer", "number", "percent"}:
        values = series.map(parse_number)
        numeric = pd.to_numeric(values, errors="coerce")
        if kind == "percent":
            numeric = numeric / 100.0
        if kind == "integer":
            return numeric.round().astype("Int64")
        return numeric.astype("Float64")
    if kind == "date":
        return pd.to_datetime(series, format="mixed", errors="coerce")
    if kind == "boolean":
        def boolean(value: Any) -> Any:
            text = str(value).strip().casefold()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
            return pd.NA

        return series.map(boolean).astype("boolean")
    if kind == "string":
        return series.map(lambda value: pd.NA if _empty(value) else str(value).strip()).astype(
            "string"
        )
    return series


def apply_report(raw: pd.DataFrame, report: ShapeReport) -> pd.DataFrame:
    """Replay a validated report without evaluating authored code."""
    frame = raw.copy()
    if report.source_has_header or report.chosen_header_row < 0:
        frame = frame.drop(
            columns=[
                frame.columns[index]
                for index in report.dropped_columns
                if 0 <= index < frame.shape[1]
            ],
            errors="ignore",
        )
        if report.dropped_rows:
            frame = frame.drop(
                index=[index for index in report.dropped_rows if index in frame.index],
                errors="ignore",
            )
    else:
        kept = [
            index
            for index in range(frame.shape[1])
            if index not in set(report.dropped_columns)
        ]
        data_start = (
            max(report.header_rows) + 1
            if report.header_rows
            else report.chosen_header_row + 1
        )
        row_indexes = [
            index
            for index in range(data_start, len(frame))
            if index not in set(report.dropped_rows)
        ]
        frame = frame.iloc[row_indexes, kept]

    frame = frame.reset_index(drop=True)
    frame = frame.dropna(axis=0, how="all").dropna(axis=1, how="all")
    if len(report.column_names) == frame.shape[1]:
        frame.columns = report.column_names
    else:
        names, _, labels = dedupe_names([str(column) for column in frame.columns])
        frame.columns = names
        report.labels.update(labels)

    for column, kind in report.dtype_coercions.items():
        if column in frame.columns and kind in ALLOWED_TYPES:
            frame[column] = _coerce(frame[column], kind)

    if report.wide_to_long:
        plan = report.wide_to_long
        ids = [name for name in plan.get("id_columns", []) if name in frame.columns]
        values = [
            name for name in plan.get("value_columns", []) if name in frame.columns
        ]
        if ids and len(values) >= 2:
            period_labels = {
                name: report.labels.get(name, name) for name in values
            }
            period_labels.update(plan.get("period_labels", {}))
            variable_name = str(plan.get("variable_name") or "variable")
            frame = frame.melt(
                id_vars=ids,
                value_vars=values,
                var_name=variable_name,
                value_name=str(plan.get("value_name") or "value"),
            )
            frame[variable_name] = frame[variable_name].map(period_labels)
            if variable_name == "year":
                frame[variable_name] = pd.to_numeric(
                    frame[variable_name], errors="coerce"
                ).astype("Int64")

    frame.attrs["column_labels"] = {
        column: report.labels.get(column, column) for column in frame.columns
    }
    return frame


def report_from_override(
    raw: pd.DataFrame,
    override: dict[str, Any],
    *,
    base: ShapeReport | None = None,
    sheet: str | None = None,
) -> ShapeReport:
    """Validate a user/model plan and convert it to the normal report format."""
    if not isinstance(override, dict):
        raise ValueError("override must be an object")
    report = base or detect_structure(raw, sheet=sheet)

    header_value = override.get("header_row", report.chosen_header_row)
    try:
        header_row = int(header_value)
    except (TypeError, ValueError) as exc:
        raise ValueError("header_row must be an integer") from exc
    if header_row < -1 or header_row >= max(1, len(raw)):
        raise ValueError("header_row is outside the sheet")

    source_has_header = header_row == -1
    candidate = detect_structure(
        raw,
        sheet=sheet or report.sheet,
        source_has_header=source_has_header,
    )
    if not source_has_header and header_row != candidate.chosen_header_row:
        candidate.chosen_header_row = header_row
        candidate.header_rows = [header_row]
        candidate.dropped_rows = list(range(header_row))
        candidate.source_has_header = False
        labels = [str(value).strip() for value in raw.iloc[header_row].tolist()]
        active = [
            label
            for index, label in enumerate(labels)
            if index not in candidate.dropped_columns
        ]
        names, renames, label_map = dedupe_names(active)
        candidate.column_names = names
        candidate.renames = renames
        candidate.labels = label_map

    skip_rows = override.get("skip_rows", [])
    if isinstance(skip_rows, int):
        skip = list(range(max(0, skip_rows)))
    elif isinstance(skip_rows, list):
        try:
            skip = [int(value) for value in skip_rows]
        except (TypeError, ValueError) as exc:
            raise ValueError("skip_rows must contain integers") from exc
    elif skip_rows in (None, ""):
        skip = []
    else:
        raise ValueError("skip_rows must be an integer or list")
    if any(index < 0 or index >= len(raw) for index in skip):
        raise ValueError("skip_rows contains a row outside the sheet")
    candidate.dropped_rows = sorted(set(candidate.dropped_rows) | set(skip))

    renames = override.get("renames", {})
    if renames is None:
        renames = {}
    if not isinstance(renames, dict):
        raise ValueError("renames must be an object")
    labels_to_names = {label: name for name, label in candidate.labels.items()}
    new_names = list(candidate.column_names)
    requested_names: dict[str, int] = {}
    for raw_key, raw_value in renames.items():
        key, value = str(raw_key), str(raw_value).strip()
        if not value:
            raise ValueError("renamed columns cannot be blank")
        current = key if key in new_names else labels_to_names.get(key)
        if current is None:
            raise ValueError(f"unknown rename column: {key}")
        position = new_names.index(current)
        new_names[position] = value
        requested_names[value] = position
    stable_names, _, _ = dedupe_names(new_names)
    old_to_new = dict(zip(candidate.column_names, stable_names))
    candidate.column_names = stable_names
    candidate.labels = {
        old_to_new.get(name, name): label for name, label in candidate.labels.items()
    }
    candidate.renames = {
        label: old_to_new.get(name, name)
        for label, name in candidate.renames.items()
    }
    candidate.dtype_coercions = {
        old_to_new.get(name, name): kind
        for name, kind in candidate.dtype_coercions.items()
    }

    types = override.get("types", {})
    if types is None:
        types = {}
    if not isinstance(types, dict):
        raise ValueError("types must be an object")
    for raw_key, raw_kind in types.items():
        key, kind = str(raw_key), str(raw_kind).lower()
        if kind not in ALLOWED_TYPES:
            raise ValueError(f"unsupported type: {kind}")
        current = key
        if key in requested_names:
            current = stable_names[requested_names[key]]
        if current not in candidate.column_names:
            current = next(
                (
                    name
                    for name, label in candidate.labels.items()
                    if label == key
                ),
                "",
            )
        if not current:
            raise ValueError(f"unknown typed column: {key}")
        candidate.dtype_coercions[current] = kind

    candidate.wide_to_long = None
    candidate.sheets = list(report.sheets)
    candidate.notes = list(report.notes) + ["Applied an explicit structure override."]
    candidate.confidence = 1.0
    return replace(candidate, sheet=sheet or candidate.sheet)
