"""Bounded conversion of freeform documents into queryable frames."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pandas as pd

DOCUMENT_SUFFIXES = {
    ".pdf",
    ".docx",
    ".doc",
    ".pptx",
    ".html",
    ".htm",
    ".md",
    ".markdown",
    ".rtf",
    ".epub",
    ".msg",
}
DOCUMENT_IMPORT_SECONDS = 60.0
MAX_MARKDOWN_BYTES = 64 * 1024 * 1024
MARKITDOWN_MEMORY_BYTES = 768 * 1024 * 1024

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_PAGE = re.compile(r"^\s*<!--\s*page\s+(\d+)\s*-->\s*$", re.IGNORECASE)
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


class DocumentImportError(ValueError):
    """A safe error that the upload route may show directly."""


class MarkItDownRequired(DocumentImportError):
    """The optional document converter is not installed."""


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return DOCUMENT_IMPORT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DocumentImportError("Document conversion exceeded the import time limit.")
    return remaining


def _python_for_module(origin: str) -> Path:
    location = Path(origin).resolve()
    for parent in location.parents:
        if parent.name == "site-packages":
            candidate = parent.parents[2] / "bin" / "python"
            if candidate.exists():
                return candidate
            break
    executable = Path(sys.executable)
    if executable.exists():
        return executable
    raise MarkItDownRequired(
        "This file type needs markitdown; install it in the server Python environment."
    )


def _markitdown_python() -> Path:
    """Locate markitdown without importing it into the server process.

    The conversion runs in a subprocess so a malformed document cannot take
    the worker's memory with it; importing the library here to find its path
    would pull that same weight into every worker permanently and give the
    isolation back. A spec carries the file location without executing it.
    """
    try:
        spec = importlib.util.find_spec("markitdown")
    except (ImportError, ModuleNotFoundError, ValueError):
        spec = None
    if spec is None or not spec.origin:
        raise MarkItDownRequired(
            "This file type needs markitdown; install it in the server Python environment."
        )
    return _python_for_module(spec.origin)


def _convert_to_markdown(path: Path, *, deadline: float | None) -> str:
    python = _markitdown_python()
    remaining = _remaining(deadline)
    end_at = time.monotonic() + remaining
    with tempfile.TemporaryDirectory(prefix="twohelixes-document-") as folder:
        output = Path(folder) / "document.md"
        status = Path(folder) / "status.json"
        env = os.environ.copy()
        interp_root = str(Path(__file__).resolve().parents[2])
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            interp_root if not existing else os.pathsep.join((interp_root, existing))
        )
        command = [
            str(python),
            "-m",
            "twohelixes.ingest.document_worker",
            str(path),
            str(output),
            str(status),
            str(MAX_MARKDOWN_BYTES),
            str(MARKITDOWN_MEMORY_BYTES),
            str(max(1, int(remaining))),
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
            )
        except OSError:
            raise DocumentImportError(
                "The document converter could not be started."
            ) from None

        def stop() -> None:
            """Kill the whole session, not just the child we started.

            `start_new_session=True` makes the converter a process-group
            leader, and some markitdown format handlers shell out. Killing
            only the direct child leaves those grandchildren running with the
            workload that just breached a limit - unbounded, on a path a
            stranger can trigger by uploading a file.
            """
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                process.kill()

        limit_hit = ""
        while process.poll() is None:
            if time.monotonic() >= end_at:
                limit_hit = "time"
                stop()
                break
            try:
                status_text = Path(f"/proc/{process.pid}/status").read_text()
                match = re.search(r"^VmRSS:\s+(\d+)\s+kB", status_text, re.MULTILINE)
                if match and int(match.group(1)) * 1024 > MARKITDOWN_MEMORY_BYTES:
                    limit_hit = "memory"
                    stop()
                    break
            except OSError:
                pass
            time.sleep(0.05)
        try:
            returncode = process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            stop()
            returncode = process.wait()

        if limit_hit == "time":
            raise DocumentImportError(
                "Document conversion exceeded the import time limit."
            )
        if limit_hit == "memory":
            raise DocumentImportError(
                "Document conversion exceeded the import memory limit."
            )

        state: dict[str, Any] = {}
        try:
            state = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if returncode != 0 or not state.get("ok"):
            if state.get("kind") == "missing_dependency":
                raise MarkItDownRequired(
                    "This file type needs markitdown with its format extras installed."
                )
            if state.get("kind") == "too_large":
                raise DocumentImportError(
                    "The converted document exceeds the import size limit."
                )
            raise DocumentImportError("The document could not be converted to Markdown.")
        try:
            return output.read_text(encoding="utf-8")
        except OSError:
            raise DocumentImportError(
                "The document conversion produced no readable text."
            ) from None


def _read_markdown(path: Path) -> str:
    """Read Markdown as-is; Markdown is already the converter's output format."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise DocumentImportError("The Markdown document could not be read.") from exc
    if len(data) > MAX_MARKDOWN_BYTES:
        raise DocumentImportError("The Markdown document exceeds the import size limit.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DocumentImportError("The Markdown document is not valid UTF-8.") from exc


def _split_pipe_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith(r"\|"):
        text = text[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for character in text:
        if escaped:
            current.append(character)
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "`":
            in_code = not in_code
            current.append(character)
        elif character == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_separator(line: str) -> bool:
    cells = _split_pipe_row(line)
    return len(cells) >= 2 and all(
        _SEPARATOR_CELL.fullmatch(cell.replace(" ", "")) for cell in cells
    )


def _table_name(heading: str, number: int) -> str:
    clean = re.sub(r"[*_`[\]]", "", heading).strip()
    return clean[:120] if clean else f"Table {number}"


def _nearest_title(lines: list[str], start: int) -> str:
    for line in reversed(lines[max(0, start - 8) : start]):
        text = line.strip().lstrip("#").strip()
        if (
            text
            and "|" not in text
            and len(text) <= 120
            and not text.endswith((".", "?", "!", ";"))
        ):
            return text
    return ""


def _extract_markdown_tables(markdown: str) -> list[tuple[str, pd.DataFrame]]:
    lines = markdown.splitlines()
    headings: list[str] = []
    tables: list[tuple[str, pd.DataFrame]] = []
    signatures: set[tuple[tuple[str, ...], ...]] = set()
    index = 1
    while index < len(lines):
        heading_match = _HEADING.match(lines[index - 1])
        if heading_match:
            headings.append(heading_match.group(2).strip())
        if not _is_separator(lines[index]) or "|" not in lines[index - 1]:
            index += 1
            continue

        start = index - 1
        rows = [_split_pipe_row(lines[start])]
        cursor = index + 1
        while cursor < len(lines):
            line = lines[cursor]
            if not line.strip() or "|" not in line:
                break
            cells = _split_pipe_row(line)
            if len(cells) < 2:
                break
            rows.append(cells)
            cursor += 1

        width = max((len(row) for row in rows), default=0)
        if width >= 2 and len(rows) >= 2:
            padded = [row + [""] * (width - len(row)) for row in rows]
            signature = tuple(tuple(cell.casefold() for cell in row) for row in padded)
            if signature in signatures:
                index = max(index + 1, cursor)
                continue
            signatures.add(signature)
            name = _table_name(
                headings[-1] if headings else _nearest_title(lines, start),
                len(tables) + 1,
            )
            tables.append(
                (name, pd.DataFrame(padded, columns=range(width), dtype=object))
            )
        index = max(index + 1, cursor)

    if not tables:
        return []

    seen: dict[str, int] = {}
    named: list[tuple[str, pd.DataFrame]] = []
    for name, frame in tables:
        seen[name] = seen.get(name, 0) + 1
        candidate = name if seen[name] == 1 else f"{name} ({seen[name]})"
        frame.attrs["_twohelixes_document_table"] = True
        named.append((candidate, frame))
    return named


def _document_frame(markdown: str) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    heading = "Document"
    page = 0
    paragraph: list[str] = []
    sequence = 0

    def flush() -> None:
        nonlocal paragraph, sequence
        text = " ".join(part.strip() for part in paragraph if part.strip()).strip()
        paragraph = []
        if not text:
            return
        sequence += 1
        rows.append(
            {
                "heading": heading,
                "text": text,
                "section_index": page or sequence,
                "word_count": len(re.findall(r"\b[\w'-]+\b", text)),
            }
        )

    in_table = False
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            page = 0
            in_table = False
            continue
        page_match = _PAGE.match(line)
        if page_match:
            flush()
            page = int(page_match.group(1))
            in_table = False
            continue
        if index + 1 < len(lines) and _is_separator(lines[index + 1]):
            flush()
            in_table = True
            continue
        if in_table:
            if not line.strip():
                in_table = False
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line)
    flush()

    if not rows:
        rows.append(
            {
                "heading": heading,
                "text": "",
                "section_index": 1,
                "word_count": 0,
            }
        )
    frame = pd.DataFrame(rows)
    frame.attrs["_twohelixes_source_has_header"] = True
    frame.attrs["_twohelixes_dataset_kind"] = "document"
    frame.attrs["_twohelixes_notes"] = [
        "No tables were found; imported the document as one row per section or paragraph."
    ]
    return frame


def read_document_sheets(
    path: Path, *, deadline: float | None = None
) -> dict[str, pd.DataFrame]:
    is_markdown = path.suffix.casefold() in {".md", ".markdown"}
    markdown = _read_markdown(path) if is_markdown else _convert_to_markdown(path, deadline=deadline)
    tables = _extract_markdown_tables(markdown)
    if tables:
        sheets = dict(tables)
        for frame in sheets.values():
            frame.attrs["_twohelixes_notes"] = [
                f"{'Read Markdown directly and found' if is_markdown else 'Converted the document to Markdown and found'} {len(tables)} table"
                f"{'s' if len(tables) != 1 else ''}."
            ]
        return sheets
    return {"Document": _document_frame(markdown)}


__all__ = [
    "DOCUMENT_IMPORT_SECONDS",
    "DOCUMENT_SUFFIXES",
    "DocumentImportError",
    "MarkItDownRequired",
    "read_document_sheets",
]
