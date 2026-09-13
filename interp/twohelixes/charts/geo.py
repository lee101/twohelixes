"""Country name / ISO remapping for choropleth maps.

Copied in spirit from askfelix's `mapping.py`: Plotly's choropleth wants ISO-3
codes, and people type country names. The CSV under `static/data/` is the same
table askfelix ships.
"""

from __future__ import annotations

import csv
from functools import lru_cache
from pathlib import Path
from typing import Any

from twohelixes import config


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, str], dict[str, str]]:
    candidates = (
        config.REPO_ROOT / "assets" / "data" / "countries_codes_and_coordinates.csv",
        config.REPO_ROOT / "static" / "data" / "countries_codes_and_coordinates.csv",
    )
    path = next((candidate for candidate in candidates if candidate.is_file()), None)
    name_to_iso3: dict[str, str] = {}
    iso2_to_iso3: dict[str, str] = {}
    if path is None:
        return name_to_iso3, iso2_to_iso3
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            name = (row.get("Country") or "").strip().strip('"').casefold()
            iso2 = (row.get("Alpha-2 code") or "").strip().strip('"').casefold()
            iso3 = (row.get("Alpha-3 code") or "").strip().strip('"').upper()
            if name and iso3:
                name_to_iso3[name] = iso3
            if iso2 and iso3:
                iso2_to_iso3[iso2] = iso3
    return name_to_iso3, iso2_to_iso3


def to_iso3(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 3 and text.isalpha():
        return text.upper()
    name_to_iso3, iso2_to_iso3 = _tables()
    key = text.casefold()
    if len(key) == 2 and key.isalpha():
        return iso2_to_iso3.get(key)
    return name_to_iso3.get(key)


def locations_to_iso3(values: list[Any]) -> tuple[list[str], int]:
    """Map a location column to ISO-3; returns (codes, unmatched_count)."""
    codes: list[str] = []
    unmatched = 0
    for value in values:
        code = to_iso3(value)
        if code is None:
            unmatched += 1
            continue
        codes.append(code)
    return codes, unmatched
