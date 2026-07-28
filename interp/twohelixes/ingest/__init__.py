"""One ingestion path for uploaded files and built-in samples."""

from __future__ import annotations

import csv
import gzip
import io
import logging
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from twohelixes.ingest.clean import apply_report
from twohelixes.ingest.document import (
    DOCUMENT_IMPORT_SECONDS,
    DOCUMENT_SUFFIXES,
    DocumentImportError,
    MarkItDownRequired,
    read_document_sheets,
)
from twohelixes.ingest.google_sheets import (
    GoogleSheetsError,
    fetch_google_sheet,
    resolve_google_sheets_url,
)
from twohelixes.ingest.structure import ShapeReport, detect_structure, usable_score

log = logging.getLogger("twohelixes.ingest")

EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".xlsb", ".ods"}
TEXT_SUFFIXES = {".csv", ".tsv", ".txt"}
STRUCTURED_SUFFIXES = {
    ".parquet",
    ".pq",
    ".json",
    ".jsonl",
    ".ndjson",
    ".xml",
    ".dbf",
}
MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 200


@dataclass
class IngestResult:
    frame: pd.DataFrame
    report: ShapeReport
    raw: pd.DataFrame


def _delimiter(data: bytes, suffix: str) -> str | None:
    sample = data[:65536].decode("utf-8-sig", "replace")
    try:
        delimiter = csv.Sniffer().sniff(sample).delimiter
    except csv.Error:
        return None
    if delimiter.isalnum() or delimiter in {"\r", "\n"}:
        return None
    return delimiter


def _fixed_width(text: str) -> bool:
    candidates = [
        line
        for line in text.splitlines()[:200]
        if re.search(r"\S(?:.*?\S)? {2,}\S", line)
    ]
    return len(candidates) >= 2


def _raw_fixed_width(text: str) -> pd.DataFrame:
    frame = pd.read_fwf(
        io.StringIO(text),
        header=None,
        dtype=object,
        keep_default_na=False,
    )
    frame.columns = range(frame.shape[1])
    return frame


def _raw_text(data: bytes, suffix: str) -> pd.DataFrame:
    """Read delimited text as a rectangle of strings, width decided by the file.

    Not `pd.read_csv`: it takes the field count from the first line, and the
    first line of a messy export is a title with no delimiter in it. Every real
    row then becomes a bad line - dropped by `on_bad_lines="skip"`, or a raised
    tokenizing error - and structure detection is handed a one-column frame
    with the actual table missing. `csv.reader` has no such inference; the
    width is the widest row, and short rows are padded.
    """
    text = data.decode("utf-8-sig", "replace")
    separator = _delimiter(data, suffix)
    if separator in {None, " "} and _fixed_width(text):
        return _raw_fixed_width(text)
    separator = separator or ("\t" if suffix == ".tsv" else ",")
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=separator))
    if not rows:
        return pd.DataFrame()

    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]
    return pd.DataFrame(padded, columns=range(width), dtype=object)


def _dbf_frame(source: Any) -> pd.DataFrame:
    try:
        from dbfread import DBF
    except ImportError:
        raise ValueError("DBF imports need the dbfread package") from None

    temporary: Path | None = None
    if isinstance(source, Path):
        location = source
    else:
        data = source.read()
        with tempfile.NamedTemporaryFile(suffix=".dbf", delete=False) as handle:
            handle.write(data)
            location = Path(handle.name)
            temporary = location
    try:
        table = DBF(str(location), load=True, char_decode_errors="replace")
        return pd.DataFrame(iter(table), columns=list(table.field_names))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _structured(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    frame.attrs["_twohelixes_source_has_header"] = True
    return {"Sheet1": frame}


def _read_source(
    source: Any,
    suffix: str,
    *,
    deadline: float | None = None,
) -> dict[str, pd.DataFrame]:
    if suffix in EXCEL_SUFFIXES:
        sheets = pd.read_excel(source, sheet_name=None, header=None, dtype=object)
        return {str(name): frame for name, frame in sheets.items()}
    if suffix in TEXT_SUFFIXES:
        data = source.read_bytes() if isinstance(source, Path) else source.read()
        return {"Sheet1": _raw_text(data, suffix)}
    if suffix in {".parquet", ".pq"}:
        return _structured(pd.read_parquet(source))
    if suffix == ".json":
        return _structured(pd.read_json(source))
    if suffix in {".jsonl", ".ndjson"}:
        return _structured(pd.read_json(source, lines=True))
    if suffix == ".xml":
        return _structured(pd.read_xml(source, parser="etree"))
    if suffix == ".dbf":
        return _structured(_dbf_frame(source))
    if suffix in DOCUMENT_SUFFIXES:
        if not isinstance(source, Path):
            raise ValueError("documents must be read from a local upload")
        return read_document_sheets(source, deadline=deadline)
    raise ValueError(f"unsupported tabular suffix: {suffix}")


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise DocumentImportError("Import exceeded the time limit.")


def _gunzip(path: Path) -> bytes:
    with gzip.open(path, "rb") as handle:
        data = handle.read(MAX_ARCHIVE_BYTES + 1)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("gzip expands beyond the import limit")
    return data


def read_raw_sheets(
    path: Path, *, deadline: float | None = None
) -> dict[str, pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix in DOCUMENT_SUFFIXES and deadline is None:
        deadline = time.monotonic() + DOCUMENT_IMPORT_SECONDS
    if suffix == ".gz":
        inner = Path(path.stem).suffix.lower() or ".csv"
        return _read_source(
            io.BytesIO(_gunzip(path)),
            inner,
            deadline=deadline,
        )
    if suffix != ".zip":
        return _read_source(path, suffix, deadline=deadline)

    sheets: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(path) as archive:
        entries = [
            item
            for item in archive.infolist()[:MAX_ARCHIVE_ENTRIES]
            if not item.is_dir()
            and Path(item.filename).suffix.lower()
            in (TEXT_SUFFIXES | EXCEL_SUFFIXES | STRUCTURED_SUFFIXES)
        ]
        if not entries:
            raise ValueError("archive has no supported tabular file")
        if sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
            raise ValueError("archive expands beyond the import limit")
        for item in entries:
            _check_deadline(deadline)
            member_suffix = Path(item.filename).suffix.lower()
            member = io.BytesIO(archive.read(item))
            member_sheets = _read_source(member, member_suffix, deadline=deadline)
            stem = Path(item.filename).stem or "Sheet"
            for member_sheet, frame in member_sheets.items():
                name = (
                    stem
                    if len(member_sheets) == 1
                    else f"{stem}: {member_sheet}"
                )
                candidate = name
                number = 2
                while candidate in sheets:
                    candidate = f"{name} ({number})"
                    number += 1
                sheets[candidate] = frame
    return sheets


def normalise_frame(frame: pd.DataFrame, *, sheet: str | None = None) -> IngestResult:
    report = detect_structure(frame, sheet=sheet, source_has_header=True)
    return IngestResult(apply_report(frame, report), report, frame)


def ingest_path(
    path: Path,
    *,
    identity: Any = None,
    model: str | None = None,
    allow_agent: bool = True,
    deadline: float | None = None,
) -> IngestResult:
    sheets = read_raw_sheets(path, deadline=deadline)
    structured_input = path.suffix.lower() in STRUCTURED_SUFFIXES
    evaluated: list[tuple[str, pd.DataFrame, ShapeReport, pd.DataFrame, float]] = []
    for sheet, raw in sheets.items():
        report = detect_structure(
            raw,
            sheet=sheet,
            source_has_header=(
                structured_input
                or bool(raw.attrs.get("_twohelixes_source_has_header"))
            ),
        )
        frame = apply_report(raw, report)
        evaluated.append((sheet, raw, report, frame, usable_score(frame, report)))

    evaluated.sort(key=lambda item: item[4], reverse=True)
    sheet, raw, report, frame, _ = evaluated[0]
    report.sheets = [
        {
            "name": item_sheet,
            "selected": item_sheet == sheet,
            "rows": int(len(item_frame)),
            "columns": int(item_frame.shape[1]),
            "confidence": item_report.confidence,
            "score": round(score, 4),
            "notes": item_report.notes,
            "dataset_kind": item_report.dataset_kind,
        }
        for item_sheet, _, item_report, item_frame, score in evaluated
    ]
    if len(evaluated) > 1:
        report.notes.append(
            f"Selected sheet {sheet!r} from {len(evaluated)} sheets."
        )

    needs_agent = report.confidence < 0.62 or frame.shape[1] < 2
    if allow_agent and needs_agent and path.suffix.lower() in DOCUMENT_SUFFIXES:
        report.notes.append(
            "Skipped the model fallback because the document table was already extracted."
        )
    elif allow_agent and needs_agent:
        from twohelixes.ingest.agent import fallback

        _check_deadline(deadline)
        frame, report = fallback(
            raw,
            report,
            identity=identity,
            model=model,
        )
    return IngestResult(frame, report, raw)


def raw_sheet(
    path: Path,
    sheet: str | None = None,
    *,
    deadline: float | None = None,
) -> tuple[str, pd.DataFrame]:
    sheets = read_raw_sheets(path, deadline=deadline)
    if sheet is None:
        name = next(iter(sheets))
        return name, sheets[name]
    if sheet not in sheets:
        raise ValueError(f"unknown sheet: {sheet}")
    return sheet, sheets[sheet]


__all__ = [
    "IngestResult",
    "ShapeReport",
    "DOCUMENT_SUFFIXES",
    "DocumentImportError",
    "GoogleSheetsError",
    "MarkItDownRequired",
    "STRUCTURED_SUFFIXES",
    "TEXT_SUFFIXES",
    "ingest_path",
    "fetch_google_sheet",
    "normalise_frame",
    "raw_sheet",
    "read_raw_sheets",
    "resolve_google_sheets_url",
]
