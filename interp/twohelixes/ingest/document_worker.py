"""Isolated MarkItDown worker with hard resource bounds."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def _status(path: Path, **value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _pdf_tables(path: Path) -> str:
    import pdfplumber

    chunks: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page_number, page in enumerate(pdf.pages, 1):
            words = page.extract_words()
            for table_number, table in enumerate(page.find_tables(), 1):
                rows = table.extract()
                if not rows or max((len(row) for row in rows), default=0) < 2:
                    continue
                above = [word for word in words if float(word["bottom"]) <= table.bbox[1]]
                heading = ""
                if above:
                    nearest_top = max(float(word["top"]) for word in above)
                    heading = " ".join(
                        str(word["text"])
                        for word in sorted(
                            (
                                word
                                for word in above
                                if abs(float(word["top"]) - nearest_top) <= 3
                            ),
                            key=lambda word: float(word["x0"]),
                        )
                    )
                heading = heading.strip() or f"Page {page_number} table {table_number}"
                width = max(len(row) for row in rows)
                clean_rows = [
                    [
                        str(cell or "").replace("|", r"\|").replace("\n", " ").strip()
                        for cell in list(row) + [None] * (width - len(row))
                    ]
                    for row in rows
                ]
                header = clean_rows[0]
                body = clean_rows[1:]
                lines = [
                    "| " + " | ".join(header) + " |",
                    "| " + " | ".join("---" for _ in range(width)) + " |",
                    *["| " + " | ".join(row) + " |" for row in body],
                ]
                chunks.append(f"# {heading}\n\n" + "\n".join(lines))
            page.close()
    return "\n\n".join(chunks)


def main() -> int:
    source, output, status = map(Path, sys.argv[1:4])
    max_output, _memory_limit, seconds = map(int, sys.argv[4:7])

    try:
        import resource

        resource.setrlimit(
            resource.RLIMIT_CPU, (max(1, seconds), max(2, math.ceil(seconds) + 1))
        )
        resource.setrlimit(resource.RLIMIT_FSIZE, (max_output, max_output))
    except (ImportError, OSError, ValueError):
        pass

    try:
        from markitdown import MarkItDown
        from markitdown._exceptions import MissingDependencyException
    except (ImportError, ModuleNotFoundError):
        _status(status, ok=False, kind="missing_dependency")
        return 3

    try:
        try:
            result = MarkItDown(enable_plugins=False).convert_local(source)
            markdown = result.text_content
        except Exception:
            if source.suffix.casefold() != ".doc":
                raise
            from legacy_doc import extract_text

            markdown = extract_text(source.read_bytes()).text
        if source.suffix.casefold() == ".rtf" and markdown.lstrip().startswith(r"{\rtf"):
            from striprtf.striprtf import rtf_to_text

            markdown = rtf_to_text(markdown)
        if source.suffix.casefold() == ".pdf":
            extracted = _pdf_tables(source)
            if extracted:
                markdown = markdown.rstrip() + "\n\n" + extracted
    except MissingDependencyException:
        _status(status, ok=False, kind="missing_dependency")
        return 3
    except Exception:
        _status(status, ok=False, kind="conversion")
        return 4

    encoded = markdown.encode("utf-8")
    if len(encoded) > max_output:
        _status(status, ok=False, kind="too_large")
        return 5
    output.write_bytes(encoded)
    _status(status, ok=True, bytes=len(encoded))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
