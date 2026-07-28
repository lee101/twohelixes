"""Messy spreadsheet imports become replayable, tidy data."""

from __future__ import annotations

from pathlib import Path
import gzip
import struct

import pandas as pd
import pytest

from twohelixes import llm
from twohelixes.ingest import (
    GoogleSheetsError,
    fetch_google_sheet,
    ingest_path,
    resolve_google_sheets_url,
)
from twohelixes.ingest import google_sheets
from twohelixes.ingest.agent import fallback
from twohelixes.ingest.clean import apply_report
from twohelixes.ingest.structure import detect_structure


def test_preamble_currency_percent_boolean_and_footer(tmp_path: Path) -> None:
    path = tmp_path / "sales.csv"
    path.write_text(
        "Quarterly sales report,,,\n"
        "Prepared 2026-07-01,,,\n"
        ",,,\n"
        "Region,Revenue,Margin,Active\n"
        'North,"$1,200",12%,yes\n'
        'South,"$2,500",8%,no\n'
        'Total,"$3,700",20%,\n'
    )

    result = ingest_path(path, allow_agent=False)

    assert result.report.chosen_header_row == 3
    assert result.report.dropped_rows == [0, 1, 2, 6]
    assert list(result.frame.columns) == ["region", "revenue", "margin", "active"]
    assert result.frame["revenue"].tolist() == [1200, 2500]
    assert result.frame["margin"].tolist() == [0.12, 0.08]
    assert result.frame["active"].tolist() == [True, False]


def test_unpadded_preamble_does_not_swallow_the_table(tmp_path: Path) -> None:
    """A title row with no delimiters must not decide the file's width.

    Padded preambles ("Report,,,") are what a spreadsheet exports; a title
    typed into cell A1 and saved is one bare field, and taking the field count
    from it drops every real row instead - a one-column dataset with the table
    missing, which is what this reproduces.
    """
    path = tmp_path / "unpadded.csv"
    path.write_text(
        "FY 2026 sales export\n"
        "Generated 2026-07-01\n"
        "\n"
        "Region,Revenue ($),Growth %,Closed\n"
        'North,"1,204.50",12.5%,yes\n'
        'South,"980.00",-3.0%,no\n'
        'TOTAL,"2184.50",,\n'
    )

    result = ingest_path(path, allow_agent=False)

    assert result.report.chosen_header_row == 3
    assert list(result.frame.columns) == ["region", "revenue", "growth", "closed"]
    assert result.frame["revenue"].tolist() == [1204.5, 980.0]
    assert result.frame["closed"].tolist() == [True, False]
    assert result.report.confidence > 0.5


def test_merged_multirow_header_is_flattened(tmp_path: Path) -> None:
    path = tmp_path / "multi.xlsx"
    raw = pd.DataFrame(
        [
            ["Operating report", None, None],
            ["Region", "Sales", None],
            ["Name", "Q1", "Q2"],
            ["North", 10, 12],
            ["South", 11, 14],
        ]
    )
    raw.to_excel(path, index=False, header=False)

    result = ingest_path(path, allow_agent=False)

    assert result.report.header_rows == [1, 2]
    assert list(result.frame.columns) == ["region_name", "sales_q1", "sales_q2"]
    assert len(result.frame) == 2


def test_mixed_dates_and_unnamed_duplicate_columns_are_safe(tmp_path: Path) -> None:
    path = tmp_path / "dates.tsv"
    path.write_text(
        "Date\t\tValue\tValue\n"
        "2026-01-02\tA\t1\t2\n"
        "03/04/2026\tB\t3\t4\n"
        "May 6 2026\tC\t5\t6\n"
    )

    result = ingest_path(path, allow_agent=False)

    assert list(result.frame.columns) == [
        "date",
        "unnamed_column_2",
        "value",
        "value_2",
    ]
    assert str(result.frame["date"].dtype).startswith("datetime64")


def test_wide_period_columns_become_long(tmp_path: Path) -> None:
    path = tmp_path / "wide.csv"
    path.write_text(
        "Region,2022,2023,2024\n"
        "North,10,11,12\n"
        "South,20,21,22\n"
    )

    result = ingest_path(path, allow_agent=False)

    assert result.report.wide_to_long is not None
    assert list(result.frame.columns) == ["region", "year", "value"]
    assert len(result.frame) == 6


def test_best_sheet_is_selected_and_the_others_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame([["Read me"], ["Nothing tabular"]]).to_excel(
            writer, sheet_name="Notes", index=False, header=False
        )
        pd.DataFrame(
            [["Region", "Revenue"], ["North", 10], ["South", 20]]
        ).to_excel(writer, sheet_name="Data", index=False, header=False)

    result = ingest_path(path, allow_agent=False)

    assert result.report.sheet == "Data"
    assert {sheet["name"] for sheet in result.report.sheets} == {"Notes", "Data"}
    assert next(sheet for sheet in result.report.sheets if sheet["name"] == "Data")[
        "selected"
    ]


def test_already_clean_csv_passes_through(tmp_path: Path) -> None:
    path = tmp_path / "clean.csv"
    expected = pd.DataFrame({"city": ["A", "B"], "sales": [1, 2]})
    expected.to_csv(path, index=False)

    result = ingest_path(path, allow_agent=False)

    assert result.frame.to_dict("records") == expected.to_dict("records")
    assert str(result.frame["sales"].dtype) == "Int64"
    assert result.report.chosen_header_row == 0
    assert result.report.confidence >= 0.75


def test_agent_failure_keeps_the_deterministic_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = pd.DataFrame(
        [["Title", None], ["Name", "Value"], ["A", 1], ["B", 2]]
    )
    report = detect_structure(raw)
    expected = apply_report(raw, report)

    def dead(*args: object, **kwargs: object) -> dict[str, object]:
        raise llm.LLMError("gateway dead")

    monkeypatch.setattr("twohelixes.ingest.agent._small_call", dead)
    frame, fallback_report = fallback(raw, report)

    pd.testing.assert_frame_equal(frame, expected)
    assert any("kept deterministic" in note for note in fallback_report.notes)


def test_text_accepts_an_uncommon_delimiter(tmp_path: Path) -> None:
    path = tmp_path / "carets.txt"
    path.write_text("City^Orders^Revenue\nOslo^4^120\nLima^7^210\n")

    result = ingest_path(path, allow_agent=False)

    assert list(result.frame.columns) == ["city", "orders", "revenue"]
    assert result.frame["orders"].tolist() == [4, 7]


def test_aligned_fixed_width_text_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "aligned.txt"
    path.write_text(
        "City          Orders  Revenue\n"
        "New York      4       120.50\n"
        "San Francisco 7       210.25\n"
    )

    result = ingest_path(path, allow_agent=False)

    assert list(result.frame.columns) == ["city", "orders", "revenue"]
    assert result.frame["city"].tolist() == ["New York", "San Francisco"]
    assert result.frame["revenue"].tolist() == [120.5, 210.25]


def test_json_lines_xml_and_gzipped_csv(tmp_path: Path) -> None:
    jsonl = tmp_path / "rows.jsonl"
    jsonl.write_text('{"city":"A","sales":1}\n{"city":"B","sales":2}\n')
    xml = tmp_path / "rows.xml"
    xml.write_text(
        "<rows><row><city>A</city><sales>1</sales></row>"
        "<row><city>B</city><sales>2</sales></row></rows>"
    )
    compressed = tmp_path / "rows.csv.gz"
    with gzip.open(compressed, "wt") as handle:
        handle.write("city,sales\nA,1\nB,2\n")

    for path in (jsonl, xml, compressed):
        result = ingest_path(path, allow_agent=False)
        assert result.frame.to_dict("records") == [
            {"city": "A", "sales": 1},
            {"city": "B", "sales": 2},
        ]


def test_dbf_is_a_structured_input(tmp_path: Path) -> None:
    path = tmp_path / "rows.dbf"
    fields = [("NAME", "C", 10, 0), ("VALUE", "N", 5, 0)]
    header_length = 32 + 32 * len(fields) + 1
    record_length = 1 + sum(field[2] for field in fields)
    header = bytearray(32)
    header[0] = 0x03
    header[1:4] = bytes((126, 7, 27))
    header[4:8] = struct.pack("<I", 2)
    header[8:10] = struct.pack("<H", header_length)
    header[10:12] = struct.pack("<H", record_length)
    descriptors = bytearray()
    for name, kind, width, decimals in fields:
        descriptor = bytearray(32)
        descriptor[: len(name)] = name.encode("ascii")
        descriptor[11] = ord(kind)
        descriptor[16] = width
        descriptor[17] = decimals
        descriptors.extend(descriptor)
    records = (
        b" " + b"Ada       " + b"   91"
        + b" " + b"Lin       " + b"   87"
    )
    path.write_bytes(bytes(header) + bytes(descriptors) + b"\r" + records + b"\x1a")

    result = ingest_path(path, allow_agent=False)

    assert result.frame.to_dict("records") == [
        {"name": "Ada", "value": 91},
        {"name": "Lin", "value": 87},
    ]


def test_google_sheet_share_and_published_links_resolve_to_exports() -> None:
    share, share_suffix = resolve_google_sheets_url(
        "https://docs.google.com/spreadsheets/d/sheet-id/edit?usp=sharing#gid=42"
    )
    published, published_suffix = resolve_google_sheets_url(
        "https://docs.google.com/spreadsheets/d/e/published-id/pub?output=tsv"
    )

    assert share == (
        "https://docs.google.com/spreadsheets/d/sheet-id/export?format=csv&gid=42"
    )
    assert share_suffix == ".csv"
    assert published.endswith("/pub?output=tsv")
    assert published_suffix == ".tsv"


def test_private_google_sheet_has_a_plain_public_access_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PrivateResponse:
        status_code = 403
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        google_sheets.httpx,
        "stream",
        lambda *args, **kwargs: PrivateResponse(),
    )

    with pytest.raises(GoogleSheetsError, match="not public"):
        fetch_google_sheet(
            "https://docs.google.com/spreadsheets/d/private/edit",
            max_bytes=1024,
        )
