"""Deterministic spreadsheet structure detection.

The detector works on a raw dataframe whose cells still include the header
and any title rows. It records every decision in a serialisable ShapeReport so
the same decisions can be replayed, explained, or overridden later.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

HEADER_SCAN_ROWS = 40
MAX_HEADER_DEPTH = 3

_EMPTY = {"", "nan", "none", "null", "<na>"}
_FOOTER = re.compile(
    r"^(?:grand\s+)?total\b|^subtotal\b|^source\b|^notes?\b|^footnotes?\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(
    r"^(?:19|20)\d{2}$|^q[1-4][\s_-]*(?:19|20)?\d{2}$|"
    r"^(?:19|20)\d{2}[\s_-]*q[1-4]$|"
    r"^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"(?:uary|ruary|ch|il|e|y|ust|ember|ober|ember|ember)?"
    r"(?:[\s_-]*(?:19|20)\d{2})?$",
    re.IGNORECASE,
)
_DATE_HINT = re.compile(
    r"(?:\d{4}[-/]\d{1,2}[-/]\d{1,2})|"
    r"(?:\d{1,2}[-/]\d{1,2}[-/]\d{2,4})|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.IGNORECASE,
)
_BOOL_TRUE = {"true", "yes", "y", "on", "enabled", "active"}
_BOOL_FALSE = {"false", "no", "n", "off", "disabled", "inactive"}


@dataclass
class ShapeReport:
    """A replayable and user-visible account of an import."""

    chosen_header_row: int
    header_rows: list[int] = field(default_factory=list)
    dropped_rows: list[int] = field(default_factory=list)
    dropped_columns: list[int] = field(default_factory=list)
    renames: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    dtype_coercions: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)
    sheet: str | None = None
    sheets: list[dict[str, Any]] = field(default_factory=list)
    wide_to_long: dict[str, Any] | None = None
    column_names: list[str] = field(default_factory=list)
    source_has_header: bool = False
    dataset_kind: str = "table"
    agent_used: bool = False
    agent_cost_micros: int = 0

    @property
    def header_row(self) -> int:
        return self.chosen_header_row

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["header_row"] = self.chosen_header_row
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ShapeReport":
        value = dict(value or {})
        value["chosen_header_row"] = int(
            value.pop("header_row", value.get("chosen_header_row", -1))
        )
        allowed = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in allowed})

    def summary(self) -> dict[str, Any]:
        preamble = len(
            [row for row in self.dropped_rows if row < max(self.chosen_header_row, 0)]
        )
        return {
            "header_row": self.chosen_header_row,
            "header_rows": self.header_rows,
            "preamble_rows_dropped": preamble,
            "rows_dropped": len(self.dropped_rows),
            "columns_dropped": len(self.dropped_columns),
            "renames": self.renames,
            "dtype_coercions": self.dtype_coercions,
            "confidence": self.confidence,
            "sheet": self.sheet,
            "sheets": self.sheets,
            "wide_to_long": self.wide_to_long,
            "dataset_kind": self.dataset_kind,
            "notes": self.notes,
            "agent_used": self.agent_used,
        }


def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    return "" if text.lower() in _EMPTY else text


def _filled(row: Any) -> list[str]:
    return [text for value in row if (text := _text(value))]


def snake_name(value: Any, fallback: str = "column") -> str:
    text = _text(value)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    text = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_").lower()
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"column_{text}"
    return text[:120]


def dedupe_names(labels: list[str]) -> tuple[list[str], dict[str, str], dict[str, str]]:
    names: list[str] = []
    renames: dict[str, str] = {}
    label_map: dict[str, str] = {}
    seen_names: dict[str, int] = {}
    seen_labels: dict[str, int] = {}

    for index, raw_label in enumerate(labels):
        label = _text(raw_label) or f"Unnamed column {index + 1}"
        seen_labels[label] = seen_labels.get(label, 0) + 1
        rename_key = (
            label
            if seen_labels[label] == 1
            else f"{label} [{seen_labels[label]}]"
        )
        base = snake_name(label, f"column_{index + 1}")
        seen_names[base] = seen_names.get(base, 0) + 1
        name = base if seen_names[base] == 1 else f"{base}_{seen_names[base]}"
        names.append(name)
        renames[rename_key] = name
        label_map[name] = label
    return names, renames, label_map


def _kind(value: Any) -> str:
    text = _text(value)
    if not text:
        return "empty"
    if parse_number(text) is not None:
        return "number"
    if _DATE_HINT.search(text):
        return "date"
    if text.lower() in _BOOL_TRUE | _BOOL_FALSE:
        return "boolean"
    return "text"


def parse_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and not pd.isna(value):
        return float(value)
    text = _text(value)
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = text.strip("()").replace("\u00a0", "").replace(" ", "")
    cleaned = re.sub(r"[$€£¥₹]", "", cleaned)
    cleaned = re.sub(
        r"^(?:USD|EUR|GBP|JPY|INR)|(?:USD|EUR|GBP|JPY|INR)$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace("'", "")
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts) == 2 and 0 < len(parts[1].rstrip("%")) <= 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    cleaned = cleaned.removesuffix("%")
    if not re.fullmatch(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", cleaned):
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def _row_score(raw: pd.DataFrame, row_index: int, depth: int) -> float:
    width = max(1, raw.shape[1])
    header_rows = list(range(row_index, min(len(raw), row_index + depth)))
    first_filled = _filled(raw.iloc[row_index].tolist())
    if width > 1 and len(first_filled) < 2:
        return 0.0
    if depth > 1:
        for continuation in header_rows[1:]:
            values = _filled(raw.iloc[continuation].tolist())
            if not values:
                return 0.0
            text_ratio = sum(_kind(value) == "text" for value in values) / len(values)
            if text_ratio < 0.6:
                return 0.0
    labels = _flatten_header(raw, header_rows)
    nonempty = [label for label in labels if label]
    if len(nonempty) < min(2, width):
        return 0.0

    filled_ratio = min(1.0, len(nonempty) / min(width, 12))
    text_ratio = sum(_kind(value) == "text" for value in nonempty) / len(nonempty)
    unique_ratio = len({value.casefold() for value in nonempty}) / len(nonempty)

    data = raw.iloc[row_index + depth : row_index + depth + 10]
    supported = 0
    contrast = 0
    considered = 0
    for column, label in enumerate(labels):
        if not label:
            continue
        below = [_kind(value) for value in data.iloc[:, column].tolist()]
        below = [kind for kind in below if kind != "empty"]
        if not below:
            continue
        considered += 1
        supported += 1
        if _kind(label) == "text" and any(kind != "text" for kind in below):
            contrast += 1

    support_ratio = supported / max(1, len(nonempty))
    contrast_ratio = contrast / max(1, considered)
    first_row_fill = len(first_filled) / width
    score = (
        0.24 * filled_ratio
        + 0.23 * text_ratio
        + 0.18 * unique_ratio
        + 0.22 * support_ratio
        + 0.10 * contrast_ratio
        + 0.03 * min(1.0, first_row_fill * 2)
    )

    if depth > 1:
        one_row_labels = _flatten_header(raw, [row_index])
        one_unique = len({value.casefold() for value in one_row_labels if value})
        if len(set(labels)) > one_unique:
            score += 0.05
        score -= 0.015 * (depth - 1)
    if len(nonempty) == 1:
        score *= 0.35
    return max(0.0, min(1.0, score))


def _flatten_header(raw: pd.DataFrame, rows: list[int]) -> list[str]:
    if not rows:
        return []
    levels: list[list[str]] = []
    for row_index in rows:
        values = [_text(value) for value in raw.iloc[row_index].tolist()]
        if len(rows) == 1:
            levels.append(values)
            continue
        carried: list[str] = []
        current = ""
        for value in values:
            if value:
                current = value
            carried.append(value or current)
        levels.append(carried)

    labels: list[str] = []
    for column in range(raw.shape[1]):
        parts: list[str] = []
        for level in levels:
            value = level[column]
            if value and (not parts or parts[-1].casefold() != value.casefold()):
                parts.append(value)
        labels.append(" ".join(parts).strip())
    return labels


def _empty_columns(raw: pd.DataFrame) -> list[int]:
    return [
        index
        for index in range(raw.shape[1])
        if not any(_text(value) for value in raw.iloc[:, index].tolist())
    ]


def _footer_rows(raw: pd.DataFrame, data_start: int) -> list[int]:
    dropped: list[int] = []
    seen_footer = False
    for row_index in range(len(raw) - 1, data_start - 1, -1):
        values = _filled(raw.iloc[row_index].tolist())
        if not values:
            dropped.append(row_index)
            continue
        first = values[0]
        explicit = bool(_FOOTER.search(first))
        sparse_note = (
            seen_footer
            and len(values) <= max(1, math.ceil(raw.shape[1] * 0.25))
            and all(_kind(value) == "text" for value in values)
        )
        if explicit or sparse_note:
            dropped.append(row_index)
            seen_footer = True
            continue
        break
    return sorted(dropped)


def _infer_type(values: list[Any]) -> str | None:
    texts = [_text(value) for value in values if _text(value)]
    if not texts:
        return None
    lowered = {value.casefold() for value in texts}
    if lowered <= (_BOOL_TRUE | _BOOL_FALSE) and lowered:
        return "boolean"

    numbers = [parse_number(value) for value in texts]
    numeric_ratio = sum(value is not None for value in numbers) / len(texts)
    percent_ratio = sum(value.strip().endswith("%") for value in texts) / len(texts)
    if percent_ratio >= 0.6 and numeric_ratio >= 0.8:
        return "percent"
    if numeric_ratio >= 0.85:
        present = [value for value in numbers if value is not None]
        if present and all(float(value).is_integer() for value in present):
            return "integer"
        return "number"

    date_candidates = [value for value in texts if _DATE_HINT.search(value)]
    if len(date_candidates) / len(texts) >= 0.7:
        parsed = pd.to_datetime(
            pd.Series(date_candidates, dtype="string"), format="mixed", errors="coerce"
        )
        if float(parsed.notna().mean()) >= 0.8:
            return "date"
    return None


def _wide_plan(
    labels: list[str], names: list[str], coercions: dict[str, str]
) -> dict[str, Any] | None:
    def signature(label: str) -> tuple[str, str] | None:
        text = _text(label)
        if _PERIOD.fullmatch(text):
            return text, ""
        years = re.findall(r"(?:19|20)\d{2}", text)
        if len(years) != 1:
            return None
        template = re.sub(r"(?:19|20)\d{2}", "", text)
        template = re.sub(r"[^0-9A-Za-z]+", " ", template).strip().casefold()
        return years[0], template

    runs: list[tuple[list[int], list[str], str]] = []
    current: list[int] = []
    periods: list[str] = []
    template = ""
    for index, label in enumerate(labels):
        found = signature(label)
        if found and (not current or found[1] == template):
            current.append(index)
            periods.append(found[0])
            template = found[1]
        else:
            if len(current) >= 3:
                runs.append((current, periods, template))
            current = [index] if found else []
            periods = [found[0]] if found else []
            template = found[1] if found else ""
    if len(current) >= 3:
        runs.append((current, periods, template))
    if not runs:
        return None

    run, period_values, value_template = max(runs, key=lambda item: len(item[0]))
    value_columns = [names[index] for index in run]
    id_columns = [name for index, name in enumerate(names) if index not in run]
    if not id_columns:
        return None
    numeric = {"integer", "number", "percent"}
    if sum(coercions.get(name) in numeric for name in value_columns) / len(run) < 0.8:
        return None
    variable_name = (
        "year"
        if all(re.fullmatch(r"(?:19|20)\d{2}", label) for label in period_values)
        else "period"
    )
    value_name = snake_name(value_template) if value_template else "value"
    if value_name in id_columns:
        value_name = f"{value_name}_value"
    return {
        "id_columns": id_columns,
        "value_columns": value_columns,
        "variable_name": variable_name,
        "value_name": value_name,
        "period_labels": dict(zip(value_columns, period_values)),
    }


def detect_structure(
    raw: pd.DataFrame,
    *,
    sheet: str | None = None,
    source_has_header: bool = False,
) -> ShapeReport:
    """Find the most plausible header and all safe cleanup operations."""
    if raw is None:
        raw = pd.DataFrame()
    raw = raw.copy()
    notes = [
        str(note)
        for note in raw.attrs.get("_twohelixes_notes", [])
        if str(note).strip()
    ]
    dataset_kind = str(raw.attrs.get("_twohelixes_dataset_kind") or "table")
    dropped_columns = _empty_columns(raw)

    if source_has_header:
        chosen = -1
        header_rows: list[int] = []
        labels = [_text(column) for column in raw.columns]
        data_start = 0
        confidence = 1.0 if raw.shape[1] >= 2 else 0.25
    elif raw.empty or raw.shape[1] == 0:
        return ShapeReport(
            chosen_header_row=0,
            confidence=0.0,
            sheet=sheet,
            dataset_kind=dataset_kind,
            notes=["The sheet contained no usable cells."],
        )
    else:
        best = (0.0, 0, 1)
        scan = min(len(raw), HEADER_SCAN_ROWS)
        for row_index in range(scan):
            for depth in range(1, min(MAX_HEADER_DEPTH, len(raw) - row_index) + 1):
                score = _row_score(raw, row_index, depth)
                if score > best[0]:
                    best = (score, row_index, depth)
        confidence, chosen, depth = best
        header_rows = list(range(chosen, chosen + depth))
        labels = _flatten_header(raw, header_rows)
        data_start = chosen + depth

    active_labels = [
        label for index, label in enumerate(labels) if index not in dropped_columns
    ]
    names, renames, label_map = dedupe_names(active_labels)

    if chosen > 0:
        notes.append(
            f"Dropped {chosen} row{'s' if chosen != 1 else ''} before the header."
        )
    if len(header_rows) > 1:
        notes.append(f"Flattened {len(header_rows)} header rows into one.")
    if dropped_columns:
        notes.append(
            f"Dropped {len(dropped_columns)} fully empty "
            f"column{'s' if len(dropped_columns) != 1 else ''}."
        )
    renamed = sum(
        label.casefold() != name.casefold()
        for label, name in zip(active_labels, names)
    )
    if renamed:
        notes.append(
            f"Normalised {renamed} column name{'s' if renamed != 1 else ''}."
        )

    dropped_rows = list(range(max(chosen, 0)))
    footer = _footer_rows(raw, data_start)
    dropped_rows.extend(footer)
    for row_index in range(data_start, len(raw)):
        if not _filled(raw.iloc[row_index].tolist()):
            dropped_rows.append(row_index)
    dropped_rows = sorted(set(dropped_rows))
    if footer:
        notes.append(
            f"Dropped {len(footer)} trailing total or footer "
            f"row{'s' if len(footer) != 1 else ''}."
        )

    usable_rows = [
        index
        for index in range(data_start, len(raw))
        if index not in set(dropped_rows)
    ]
    coercions: dict[str, str] = {}
    active_positions = [
        index for index in range(raw.shape[1]) if index not in dropped_columns
    ]
    for name, column_index in zip(names, active_positions):
        inferred = _infer_type([raw.iat[row, column_index] for row in usable_rows])
        if inferred:
            coercions[name] = inferred

    wide = _wide_plan(active_labels, names, coercions)
    if wide:
        notes.append(
            f"Converted {len(wide['value_columns'])} period columns to long form."
        )

    usable_columns = len(names)
    if usable_columns < 2:
        confidence = min(confidence, 0.3)
        notes.append("Fewer than two usable columns were found.")
    if len(usable_rows) < 2:
        confidence = min(confidence, 0.35)
        notes.append("The sheet contained fewer than two usable data rows.")
    if usable_columns >= 2 and len(usable_rows) >= 2 and source_has_header:
        notes.append("The input was already structured; no header rows were changed.")

    return ShapeReport(
        chosen_header_row=chosen,
        header_rows=header_rows,
        dropped_rows=dropped_rows,
        dropped_columns=dropped_columns,
        renames=renames,
        labels=label_map,
        dtype_coercions=coercions,
        confidence=round(max(0.0, min(1.0, confidence)), 3),
        notes=notes,
        sheet=sheet,
        wide_to_long=wide,
        column_names=names,
        source_has_header=source_has_header,
        dataset_kind=dataset_kind,
    )


def usable_score(frame: pd.DataFrame, report: ShapeReport) -> float:
    """Rank sheets by usable tidy data, not by raw cell count."""
    if frame is None or frame.shape[1] < 2 or len(frame) == 0:
        return 0.0
    density = float(frame.notna().mean().mean()) if frame.size else 0.0
    width_penalty = 1.0 / (1.0 + max(0, frame.shape[1] - 80) / 80)
    return (
        report.confidence
        * math.log1p(len(frame))
        * math.log1p(frame.shape[1])
        * max(0.1, density)
        * width_penalty
    )
