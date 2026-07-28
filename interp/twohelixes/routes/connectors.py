"""Data source management and file upload."""

from __future__ import annotations

import base64
import io
import logging
import time
import zipfile
from pathlib import Path
from typing import Any

from twohelixes import auth, config, router, store
from twohelixes.connectors import registry
from twohelixes.connectors.base import ConnectorError
from twohelixes.ingest import (
    DOCUMENT_SUFFIXES,
    DocumentImportError,
    GoogleSheetsError,
    IngestResult,
    fetch_google_sheet,
    ingest_path,
    normalise_frame,
)
from twohelixes.ingest.document import DOCUMENT_IMPORT_SECONDS

log = logging.getLogger("twohelixes.routes.connectors")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = {
    ".csv", ".tsv", ".txt", ".json", ".ndjson", ".jsonl",
    ".parquet", ".pq",
    ".xlsx", ".xls", ".xlsm", ".xlsb", ".ods",
    ".xml", ".dbf",
    ".gz", ".zip",
} | DOCUMENT_SUFFIXES
ALLOWED_SUFFIX_MESSAGE = (
    "Supported files: CSV, TSV, delimited or fixed-width text, JSON/JSONL, "
    "XML, DBF, Parquet, Excel/ODS, CSV.GZ, ZIP, PDF, Word, PowerPoint, HTML, "
    "Markdown, RTF, EPUB, and MSG."
)

# A zip is expanded in memory, so both the entry count and the expanded size
# have to be bounded or a small upload can cost gigabytes of RAM.
MAX_ZIP_ENTRIES = 200


@router.get("/v1/connectors/kinds")
def kinds(ctx: router.Context) -> router.Result:
    return router.json_result({"kinds": registry.kinds()})


@router.get("/v1/sources")
def list_sources(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    return router.json_result({"sources": registry.list_sources(identity.user_id)})


@router.post("/v1/sources")
def create_source(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    kind = str(body.get("kind") or "")
    name = str(body.get("name") or "")
    source_config = body.get("config")
    if not isinstance(source_config, dict):
        return router.error(400, "missing_config")

    try:
        created = registry.create_source(identity.user_id, name, kind, source_config)
    except ConnectorError as exc:
        return router.error(400, "connect_failed", str(exc))

    return router.json_result(created, status=201)


@router.post("/v1/sources/test")
def test_source(ctx: router.Context) -> router.Result:
    """Validate credentials before saving them."""
    auth.require(ctx)
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    try:
        connector = registry.build(str(body.get("kind") or ""), body.get("config") or {})
    except ConnectorError as exc:
        return router.error(400, "unknown_kind", str(exc))

    return router.json_result(connector.test())


@router.delete("/v1/sources/{source_id}")
def delete_source(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    ok = registry.delete_source(identity.user_id, ctx.params["source_id"])
    if not ok:
        return router.error(404, "not_found")
    return router.json_result({"deleted": True})


@router.get("/v1/sources/{source_id}/tables")
def source_tables(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    try:
        connector = registry.for_source(ctx.params["source_id"], identity.user_id)
        tables = connector.tables()
    except ConnectorError as exc:
        return router.error(400, "connector_error", str(exc))
    return router.json_result(
        {"tables": [t.to_dict() for t in tables], "dialect": connector.dialect}
    )


@router.post("/v1/upload")
def upload(ctx: router.Context) -> router.Result:
    """Accept a base64 file body and register it as a queryable dataset.

    Base64 rather than multipart: the Mojo layer hands Python a string, and a
    JSON envelope keeps that boundary one type wide.
    """
    identity = auth.require(ctx)
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    filename = str(body.get("filename") or "").strip()
    content = body.get("content_base64")
    source_url = str(body.get("url") or "").strip()
    if not source_url and filename.startswith("https://docs.google.com/"):
        source_url = filename
        filename = ""
    if not source_url and (not filename or not isinstance(content, str)):
        return router.error(400, "filename_and_content_required")

    deadline = time.monotonic() + DOCUMENT_IMPORT_SECONDS
    if source_url:
        try:
            raw, downloaded_name = fetch_google_sheet(
                source_url,
                max_bytes=MAX_UPLOAD_BYTES,
                deadline=deadline,
            )
        except GoogleSheetsError as exc:
            return router.error(400, "google_sheet_unavailable", str(exc))
        filename = downloaded_name
    else:
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_SUFFIXES:
            return router.error(415, "unsupported_type", ALLOWED_SUFFIX_MESSAGE)
        try:
            raw = base64.b64decode(content, validate=True)
        except Exception:  # noqa: BLE001
            return router.error(400, "invalid_base64")

    if len(raw) > MAX_UPLOAD_BYTES:
        return router.error(413, "file_too_large")

    suffix = Path(filename).suffix.lower()
    user_dir = config.data_dir() / "uploads" / identity.user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in Path(filename).name if c.isalnum() or c in "._-")
    target = user_dir / f"{store.new_id()}-{safe_name or f'upload{suffix}'}"
    target.write_bytes(raw)

    try:
        result = ingest_path(target, identity=identity, deadline=deadline)
    except DocumentImportError as exc:
        target.unlink(missing_ok=True)
        return router.error(400, "document_import_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not ingest %s: %s", filename, exc)
        target.unlink(missing_ok=True)
        return router.error(400, "unreadable_file")

    try:
        dataset_id = _register_ingest(
            identity,
            result,
            filename,
            target,
            description=f"Uploaded {filename}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("could not persist normalized upload")
        return router.error(500, "normalization_failed", str(exc))

    from twohelixes.interpreter import tools

    frame = result.frame
    return router.json_result(
        {
            "dataset_id": dataset_id,
            "name": Path(filename).stem,
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "profile": tools.profile(frame),
            "preview": frame.head(20).to_dict("records"),
            "shape_report": result.report.to_dict(),
            "dataset_kind": result.report.dataset_kind,
        },
        status=201,
    )


def _read_any(path: Path) -> Any:
    """Read a table out of whatever was uploaded, trying hard before giving up.

    A rejected upload is the earliest and most annoying way to lose someone, so
    the readers below are deliberately forgiving: an unknown suffix is still
    attempted as delimited text, and a failed CSV parse is retried with the
    delimiter sniffed and then with bad lines skipped.
    """
    suffix = path.suffix.lower()

    try:
        if suffix == ".zip":
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()[:MAX_ZIP_ENTRIES]
                if sum(item.file_size for item in entries) > MAX_UPLOAD_BYTES:
                    return None
        return ingest_path(path, allow_agent=False).frame
    except Exception as exc:  # noqa: BLE001
        log.warning("structured ingest failed for %s: %s", path.name, exc)

    if suffix == ".gz":
        return _read_compressed(path)
    if suffix == ".zip":
        return _read_zip(path)
    return _read_table(path, suffix)


def _read_table(path: Any, suffix: str, compression: str | None = None) -> Any:
    import pandas as pd

    kwargs: dict[str, Any] = {} if compression is None else {"compression": compression}
    try:
        if suffix in (".parquet", ".pq"):
            return pd.read_parquet(path)
        if suffix in (".xlsx", ".xls", ".xlsm", ".xlsb", ".ods"):
            return pd.read_excel(path)
        if suffix == ".json":
            return pd.read_json(path, **kwargs)
        if suffix in (".ndjson", ".jsonl"):
            return pd.read_json(path, lines=True, **kwargs)
        if suffix == ".tsv":
            return pd.read_csv(path, sep="\t", **kwargs)
        frame = pd.read_csv(path, **kwargs)
        # A semicolon- or pipe-delimited file parses "successfully" as one
        # column whose name is the whole header row. That charts as nothing, so
        # treat it as a failed parse and let the sniffing retry below run.
        if len(frame.columns) > 1 or not _looks_delimited(str(frame.columns[0])):
            return frame
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s as %s: %s", getattr(path, "name", path), suffix, exc)

    if suffix in (".parquet", ".pq", ".xlsx", ".xls", ".xlsm", ".xlsb", ".ods"):
        return None

    # Text that pandas could not parse with the default comma: let it sniff the
    # delimiter, and failing that keep the rows that do parse. A file with one
    # ragged line is still worth charting.
    for retry in ({"sep": None, "engine": "python"}, {"on_bad_lines": "skip"}):
        try:
            if hasattr(path, "seek"):
                path.seek(0)
            return pd.read_csv(path, **retry, **kwargs)
        except Exception:  # noqa: BLE001
            continue
    return None


def _looks_delimited(header: str) -> bool:
    return any(sep in header for sep in (";", "|", "\t"))


def _read_compressed(path: Path) -> Any:
    """A .gz wrapping one of the text formats; the inner suffix decides."""
    inner = Path(path.stem).suffix.lower() or ".csv"
    return _read_table(path, inner, compression="gzip")


def _read_zip(path: Path) -> Any:
    """The first readable tabular entry in the archive.

    Entries are read through the zip object rather than extracted, so a member
    path like `../../etc/passwd` never touches the filesystem.
    """
    import zipfile

    try:
        archive = zipfile.ZipFile(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open %s: %s", path.name, exc)
        return None

    with archive:
        entries = [
            info
            for info in archive.infolist()[:MAX_ZIP_ENTRIES]
            if not info.is_dir() and not Path(info.filename).name.startswith(".")
        ]
        total = sum(info.file_size for info in entries)
        if total > MAX_UPLOAD_BYTES:
            log.warning("%s expands to %d bytes; refusing", path.name, total)
            return None

        for info in sorted(entries, key=lambda i: -i.file_size):
            suffix = Path(info.filename).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES or suffix in (".zip", ".gz"):
                continue
            with archive.open(info) as handle:
                frame = _read_table(io.BytesIO(handle.read()), suffix)
            if frame is not None:
                return frame
    return None


def _ensure_files_source(user_id: str, folder: Path) -> None:
    existing = store.one(
        "SELECT id FROM data_sources WHERE user_id = ? AND kind = 'files'", (user_id,)
    )
    if existing:
        registry.invalidate(existing["id"])
        return
    try:
        registry.create_source(user_id, "My files", "files", {"root": str(folder)})
    except ConnectorError as exc:
        log.debug("files source not created: %s", exc)


@router.post("/v1/uploads/presign")
def presign_upload(ctx: router.Context) -> router.Result:
    """Hand the browser a URL to PUT a file straight to R2.

    The app server never sees the bytes. That is the only workable route for
    a large file - the Mojo bridge carries UTF-8 strings, so a 200 MB Parquet
    body cannot cross it - and it is cheaper besides.
    """
    identity = auth.require(ctx)
    from twohelixes.storage import r2

    if not r2.is_configured():
        return router.error(
            503,
            "storage_unconfigured",
            "Direct upload needs R2 credentials; use /v1/upload for small files.",
        )

    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    filename = str(body.get("filename") or "")
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return router.error(415, "unsupported_type", ALLOWED_SUFFIX_MESSAGE)
    try:
        size = int(body.get("size") or 0)
    except (TypeError, ValueError):
        return router.error(400, "upload_rejected", "size must be an integer")
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        return router.error(
            413,
            "file_too_large",
            f"size must be between 1 and {MAX_UPLOAD_BYTES} bytes",
        )

    try:
        ticket = r2.upload_ticket(
            identity.user_id,
            filename,
            "application/octet-stream",
            size,
        )
    except r2.R2Error as exc:
        return router.error(400, "upload_rejected", str(exc))

    return router.json_result(ticket)


@router.post("/v1/uploads/complete")
def complete_upload(ctx: router.Context) -> router.Result:
    """Ingest an object the browser already uploaded."""
    identity = auth.require(ctx)
    from twohelixes.storage import r2

    key = str(ctx.field("key") or "")
    # The key is namespaced by user id; refuse anything outside the caller's
    # own prefix so a guessed key cannot pull someone else's object.
    if not key.startswith(f"uploads/{identity.user_id}/"):
        return router.error(403, "not_your_upload")

    try:
        meta = r2.head(key)
        raw = r2.fetch(key)
    except Exception as exc:  # noqa: BLE001
        return router.error(400, "fetch_failed", str(exc))
    if len(raw) > MAX_UPLOAD_BYTES:
        return router.error(413, "file_too_large")

    user_dir = config.data_dir() / "uploads" / identity.user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    name = Path(key).name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return router.error(415, "unsupported_type", ALLOWED_SUFFIX_MESSAGE)
    target = user_dir / name
    target.write_bytes(raw)

    try:
        result = ingest_path(
            target,
            identity=identity,
            deadline=time.monotonic() + DOCUMENT_IMPORT_SECONDS,
        )
    except DocumentImportError as exc:
        target.unlink(missing_ok=True)
        return router.error(400, "document_import_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        log.warning("could not ingest direct upload %s: %s", name, exc)
        target.unlink(missing_ok=True)
        return router.error(400, "unreadable_file")

    try:
        dataset_id = _register_ingest(
            identity,
            result,
            name,
            target,
            description=f"Uploaded {name}",
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("could not persist normalized direct upload")
        return router.error(500, "normalization_failed", str(exc))
    from twohelixes.interpreter import tools

    frame = result.frame
    return router.json_result(
        {
            "dataset_id": dataset_id,
            "name": Path(name).stem,
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "bytes": meta.get("size", len(raw)),
            "profile": tools.profile(frame),
            "preview": frame.head(20).to_dict("records"),
            "shape_report": result.report.to_dict(),
            "dataset_kind": result.report.dataset_kind,
        },
        status=201,
    )


def _register_ingest(
    identity: Any,
    result: IngestResult,
    name: str,
    raw_path: Path,
    *,
    description: str,
) -> str:
    """Write normalized Parquet before opening the short metadata transaction."""
    dataset_id = store.new_id()
    parquet_path = raw_path.parent / f"{dataset_id}.parquet"
    result.frame.to_parquet(parquet_path, index=False)
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO datasets (id, user_id, source_id, folder_id, name, "
            "description, columns, row_count, storage, raw_storage, shape_report, "
            "sheet_name, created_at, updated_at) "
            "VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                identity.user_id,
                Path(name).stem,
                description,
                store.dump_json([str(column) for column in result.frame.columns]),
                int(len(result.frame)),
                str(parquet_path),
                str(raw_path),
                store.dump_json(result.report.to_dict()),
                result.report.sheet,
                now,
                now,
            ),
        )
    _ensure_files_source(identity.user_id, raw_path.parent)
    return dataset_id


@router.get("/v1/samples")
def list_samples(ctx: router.Context) -> router.Result:
    """Sample datasets, available before any source is connected.

    A new account with nothing connected has nothing to ask about, which is
    the fastest way to lose someone in the first minute.
    """
    from twohelixes.datasets import samples

    return router.json_result({"samples": samples.catalogue()})


@router.post("/v1/samples/{key}/attach")
def attach_sample(ctx: router.Context) -> router.Result:
    """Copy a sample into the caller's datasets so it behaves like their own."""
    identity = auth.require(ctx)
    from twohelixes.datasets import samples

    key = ctx.params["key"]
    if key not in samples.BY_KEY:
        return router.error(404, "unknown_sample")

    try:
        frame = samples.frame(key)
    except Exception as exc:  # noqa: BLE001
        return router.error(500, "sample_unavailable", str(exc))

    sample = samples.BY_KEY[key]
    user_dir = config.data_dir() / "uploads" / identity.user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / f"{store.new_id()}-{key}.source.parquet"
    frame.to_parquet(target, index=False)
    result = normalise_frame(frame, sheet="Sheet1")
    dataset_id = _register_ingest(
        identity,
        result,
        sample.name,
        target,
        description=sample.description,
    )

    return router.json_result(
        {
            "dataset_id": dataset_id,
            "name": sample.name,
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "questions": sample.questions,
            "shape_report": result.report.to_dict(),
        },
        status=201,
    )


@router.get("/v1/datasets")
def list_datasets(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT id, name, description, folder_id, columns, row_count, shape_report, "
        "created_at, updated_at FROM datasets "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT 200",
        (identity.user_id,),
    )
    out = store.rows_to_dicts(rows)
    for entry in out:
        entry["columns"] = store.load_json(entry.get("columns"), [])
        entry["shape_report"] = store.load_json(entry.get("shape_report"), None)
    return router.json_result({"datasets": out})


@router.delete("/v1/datasets/{dataset_id}")
def delete_dataset(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    from twohelixes.routes import teams

    teams.ensure_schema()
    row = store.one(
        "SELECT storage, raw_storage FROM datasets WHERE id = ? AND user_id = ?",
        (ctx.params["dataset_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    with store.transaction() as conn:
        conn.execute("DELETE FROM datasets WHERE id = ?", (ctx.params["dataset_id"],))
        conn.execute(
            "DELETE FROM team_objects WHERE kind = 'dataset' AND object_id = ?",
            (ctx.params["dataset_id"],),
        )
        conn.execute(
            "UPDATE shares SET revoked = 1 WHERE kind = 'dataset' AND object_id = ?",
            (ctx.params["dataset_id"],),
        )
    for location in (row["storage"], row["raw_storage"]):
        if not location:
            continue
        try:
            Path(location).unlink(missing_ok=True)
        except OSError:
            pass
    return router.json_result({"deleted": True})
