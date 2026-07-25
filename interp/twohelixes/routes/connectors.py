"""Data source management and file upload."""

from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any

from twohelixes import auth, config, router, store
from twohelixes.connectors import registry
from twohelixes.connectors.base import ConnectorError

log = logging.getLogger("twohelixes.routes.connectors")

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
ALLOWED_SUFFIXES = {".csv", ".tsv", ".txt", ".json", ".ndjson", ".jsonl", ".parquet", ".xlsx", ".xls"}


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
    if not filename or not isinstance(content, str):
        return router.error(400, "filename_and_content_required")

    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        return router.error(
            415, "unsupported_type", f"{suffix or 'file'} is not supported"
        )

    try:
        raw = base64.b64decode(content, validate=True)
    except Exception:  # noqa: BLE001
        return router.error(400, "invalid_base64")

    if len(raw) > MAX_UPLOAD_BYTES:
        return router.error(413, "file_too_large")

    user_dir = config.data_dir() / "uploads" / identity.user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c for c in Path(filename).name if c.isalnum() or c in "._-")
    target = user_dir / (safe_name or f"upload{suffix}")
    target.write_bytes(raw)

    frame = _read_any(target)
    if frame is None:
        target.unlink(missing_ok=True)
        return router.error(400, "unreadable_file")

    parquet_path = target.with_suffix(".parquet")
    try:
        frame.to_parquet(parquet_path)
        storage = str(parquet_path)
    except Exception:  # noqa: BLE001 - pyarrow may be absent
        storage = str(target)

    dataset_id = store.new_id()
    store.execute(
        "INSERT INTO datasets (id, user_id, source_id, name, description, columns, "
        "row_count, storage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dataset_id,
            identity.user_id,
            None,
            Path(filename).stem,
            f"Uploaded {filename}",
            store.dump_json([str(c) for c in frame.columns]),
            int(len(frame)),
            storage,
            time.time(),
        ),
    )

    # A per-user folder source makes every upload queryable with SQL through
    # DuckDB, not just chartable.
    _ensure_files_source(identity.user_id, user_dir)

    from twohelixes.interpreter import tools

    return router.json_result(
        {
            "dataset_id": dataset_id,
            "name": Path(filename).stem,
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "profile": tools.profile(frame),
            "preview": frame.head(20).to_dict("records"),
        },
        status=201,
    )


def _read_any(path: Path) -> Any:
    import pandas as pd

    suffix = path.suffix.lower()
    try:
        if suffix in (".csv", ".txt"):
            return pd.read_csv(path)
        if suffix == ".tsv":
            return pd.read_csv(path, sep="\t")
        if suffix == ".parquet":
            return pd.read_parquet(path)
        if suffix in (".json",):
            return pd.read_json(path)
        if suffix in (".ndjson", ".jsonl"):
            return pd.read_json(path, lines=True)
        if suffix in (".xlsx", ".xls"):
            return pd.read_excel(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read %s: %s", path.name, exc)
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

    try:
        ticket = r2.upload_ticket(
            identity.user_id,
            str(body.get("filename") or ""),
            str(body.get("content_type") or ""),
            int(body.get("size") or 0),
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

    user_dir = config.data_dir() / "uploads" / identity.user_id
    user_dir.mkdir(parents=True, exist_ok=True)
    name = Path(key).name
    target = user_dir / name
    target.write_bytes(raw)

    frame = _read_any(target)
    if frame is None:
        target.unlink(missing_ok=True)
        return router.error(400, "unreadable_file")

    dataset_id = _register_dataset(identity, frame, name, target)
    from twohelixes.interpreter import tools

    return router.json_result(
        {
            "dataset_id": dataset_id,
            "name": Path(name).stem,
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "bytes": meta.get("size", len(raw)),
            "profile": tools.profile(frame),
            "preview": frame.head(20).to_dict("records"),
        },
        status=201,
    )


def _register_dataset(identity: Any, frame: Any, name: str, target: Path) -> str:
    """Store a dataframe as Parquet where possible and record it."""
    parquet_path = target.with_suffix(".parquet")
    try:
        frame.to_parquet(parquet_path)
        storage = str(parquet_path)
    except Exception:  # noqa: BLE001 - pyarrow may be absent
        storage = str(target)

    dataset_id = store.new_id()
    store.execute(
        "INSERT INTO datasets (id, user_id, source_id, name, description, columns, "
        "row_count, storage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dataset_id,
            identity.user_id,
            None,
            Path(name).stem,
            f"Uploaded {name}",
            store.dump_json([str(c) for c in frame.columns]),
            int(len(frame)),
            storage,
            time.time(),
        ),
    )
    _ensure_files_source(identity.user_id, target.parent)
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
    target = user_dir / f"{key}.parquet"
    frame.to_parquet(target)

    dataset_id = store.new_id()
    store.execute(
        "INSERT INTO datasets (id, user_id, source_id, name, description, columns, "
        "row_count, storage, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dataset_id,
            identity.user_id,
            None,
            sample.name,
            sample.description,
            store.dump_json([str(c) for c in frame.columns]),
            int(len(frame)),
            str(target),
            time.time(),
        ),
    )
    _ensure_files_source(identity.user_id, user_dir)

    return router.json_result(
        {
            "dataset_id": dataset_id,
            "name": sample.name,
            "rows": int(len(frame)),
            "columns": [str(c) for c in frame.columns],
            "questions": sample.questions,
        },
        status=201,
    )


@router.get("/v1/datasets")
def list_datasets(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    rows = store.query(
        "SELECT id, name, description, columns, row_count, created_at FROM datasets "
        "WHERE user_id = ? ORDER BY created_at DESC LIMIT 200",
        (identity.user_id,),
    )
    out = store.rows_to_dicts(rows)
    for entry in out:
        entry["columns"] = store.load_json(entry.get("columns"), [])
    return router.json_result({"datasets": out})


@router.delete("/v1/datasets/{dataset_id}")
def delete_dataset(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = store.one(
        "SELECT storage FROM datasets WHERE id = ? AND user_id = ?",
        (ctx.params["dataset_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    store.execute("DELETE FROM datasets WHERE id = ?", (ctx.params["dataset_id"],))
    try:
        Path(row["storage"]).unlink(missing_ok=True)
    except OSError:
        pass
    return router.json_result({"deleted": True})
