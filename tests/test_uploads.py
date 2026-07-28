"""What an upload is allowed to be.

A rejected file is the earliest way to lose someone, so the reader tries the
sniffed delimiter and then skips bad rows before it gives up. These tests hold
that behaviour, and the bounds on archives that make it safe.
"""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from twohelixes.routes import connectors


@pytest.fixture
def frame() -> pd.DataFrame:
    return pd.DataFrame({"a": [1, 2, 3], "b": ["x", "y", "z"]})


def test_gzipped_csv(tmp_path: Path, frame: pd.DataFrame) -> None:
    target = tmp_path / "rows.csv.gz"
    with gzip.open(target, "wt") as handle:
        frame.to_csv(handle, index=False)
    read = connectors._read_any(target)
    assert read is not None
    assert list(read.columns) == ["a", "b"]


def test_zip_takes_the_largest_readable_entry(tmp_path: Path, frame: pd.DataFrame) -> None:
    target = tmp_path / "bundle.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("readme.txt", "not a table")
        archive.writestr("rows.csv", frame.to_csv(index=False))
    read = connectors._read_any(target)
    assert read is not None
    assert len(read) == 3


def test_semicolon_delimited_is_not_read_as_one_column(tmp_path: Path) -> None:
    target = tmp_path / "euro.csv"
    target.write_text("a;b;c\n1;2;3\n4;5;6\n")
    read = connectors._read_any(target)
    assert read is not None
    assert list(read.columns) == ["a", "b", "c"]


def test_a_ragged_row_does_not_lose_the_file(tmp_path: Path) -> None:
    """The overlong row keeps its data instead of being skipped.

    The reader takes the width from the widest row, so the stray third field
    lands in a spare column rather than making the row unparseable - and no
    row is dropped, which is what silently losing a customer's data looks
    like.
    """
    target = tmp_path / "ragged.csv"
    target.write_text("a,b\n1,2\n3,4,5\n6,7\n")
    read = connectors._read_any(target)
    assert read is not None
    assert len(read) == 3
    assert list(read.columns)[:2] == ["a", "b"]
    assert read["a"].astype(str).tolist() == ["1", "3", "6"]


def test_an_unreadable_file_is_still_rejected(tmp_path: Path) -> None:
    target = tmp_path / "broken.parquet"
    target.write_bytes(b"\x00\x01\x02")
    assert connectors._read_any(target) is None


def test_a_zip_bomb_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connectors, "MAX_UPLOAD_BYTES", 1024)
    target = tmp_path / "bomb.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("big.csv", "a\n" + "1\n" * 5000)
    assert connectors._read_any(target) is None
