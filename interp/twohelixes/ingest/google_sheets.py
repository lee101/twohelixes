"""Resolve and download public Google Sheets as bounded tabular files."""

from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

_SHEET = re.compile(r"^/spreadsheets/d/([^/]+)")
_PUBLISHED = re.compile(r"^/spreadsheets/d/e/([^/]+)/pub")


class GoogleSheetsError(ValueError):
    """A safe Google Sheets error that may be returned to the caller."""


def resolve_google_sheets_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or (parsed.hostname or "").casefold() != "docs.google.com":
        raise GoogleSheetsError("Only Google Sheets links from docs.google.com are supported.")

    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    output = (query.get("output") or query.get("format") or ["csv"])[0].casefold()
    suffix = ".tsv" if output == "tsv" else ".csv"

    published = _PUBLISHED.match(parsed.path)
    if published:
        query["output"] = ["tsv" if suffix == ".tsv" else "csv"]
        resolved = urlunsplit(
            (
                "https",
                "docs.google.com",
                parsed.path,
                urlencode(query, doseq=True),
                "",
            )
        )
        return resolved, suffix

    sheet = _SHEET.match(parsed.path)
    if not sheet:
        raise GoogleSheetsError("This is not a recognised Google Sheets link.")
    gid = (query.get("gid") or fragment.get("gid") or ["0"])[0]
    resolved = (
        f"https://docs.google.com/spreadsheets/d/{sheet.group(1)}/export?"
        + urlencode({"format": suffix[1:], "gid": gid})
    )
    return resolved, suffix


def fetch_google_sheet(
    url: str,
    *,
    max_bytes: int,
    deadline: float | None = None,
) -> tuple[bytes | bytearray, str]:
    resolved, suffix = resolve_google_sheets_url(url)
    remaining = 20.0 if deadline is None else deadline - time.monotonic()
    if remaining <= 0:
        raise GoogleSheetsError("Google Sheets download exceeded the import time limit.")
    end_at = time.monotonic() + remaining
    timeout = httpx.Timeout(min(remaining, 20.0), connect=min(remaining, 8.0))
    try:
        with httpx.stream(
            "GET",
            resolved,
            follow_redirects=True,
            timeout=timeout,
            headers={"User-Agent": "twoHelixes public sheet importer"},
        ) as response:
            if response.status_code in {401, 403, 404}:
                raise GoogleSheetsError(
                    "This Google Sheet is not public. Publish it or allow link access, then try again."
                )
            response.raise_for_status()
            if "text/html" in response.headers.get("content-type", "").casefold():
                raise GoogleSheetsError(
                    "This Google Sheet is not public. Publish it or allow link access, then try again."
                )
            length = response.headers.get("content-length")
            if length and int(length) > max_bytes:
                raise GoogleSheetsError("The Google Sheet exceeds the upload size limit.")
            data = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() >= end_at:
                    raise GoogleSheetsError(
                        "Google Sheets download exceeded the import time limit."
                    )
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise GoogleSheetsError("The Google Sheet exceeds the upload size limit.")
    except GoogleSheetsError:
        raise
    except (httpx.HTTPError, ValueError):
        raise GoogleSheetsError(
            "The Google Sheet could not be downloaded. Make sure it is public and try again."
        ) from None

    preview = bytes(data[:4096]).lstrip().lower()
    if not data or preview.startswith((b"<!doctype html", b"<html")):
        raise GoogleSheetsError(
            "This Google Sheet is not public. Publish it or allow link access, then try again."
        )
    return data, f"google-sheet{suffix}"


__all__ = [
    "GoogleSheetsError",
    "fetch_google_sheet",
    "resolve_google_sheets_url",
]
