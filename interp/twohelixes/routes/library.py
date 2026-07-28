"""Dataset, connector, folder, preview, restructure, and share management."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pandas as pd

from twohelixes import auth, config, router, store
from twohelixes.connectors import registry
from twohelixes.ingest import raw_sheet
from twohelixes.ingest.clean import apply_report, report_from_override
from twohelixes.ingest.structure import ShapeReport, detect_structure
from twohelixes.routes import teams


def _folder(folder_id: str | None) -> dict[str, Any] | None:
    if not folder_id:
        return None
    return store.row_to_dict(
        store.one(
            "SELECT id, user_id, team_id, parent_id, name, created_at "
            "FROM folders WHERE id = ?",
            (folder_id,),
        )
    )


def _folder_access(user_id: str, folder_id: str | None) -> bool:
    if folder_id is None:
        return True
    folder = _folder(folder_id)
    if folder is None:
        return False
    if folder.get("user_id") == user_id:
        return True
    team_id = folder.get("team_id")
    return bool(team_id and teams.role_in(str(team_id), user_id))


def _owned_folder(user_id: str, folder_id: str | None) -> dict[str, Any] | None:
    folder = _folder(folder_id)
    if folder and folder.get("user_id") == user_id:
        return folder
    return None


def _share_state(kind: str, object_id: str) -> dict[str, Any]:
    teams.ensure_schema()
    link = store.one(
        "SELECT token, expires_at FROM shares "
        "WHERE kind = ? AND object_id = ? AND revoked = 0 "
        "AND (expires_at IS NULL OR expires_at > ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (kind, object_id, time.time()),
    )
    team_id = teams.team_of(kind, object_id)
    return {
        "shared": bool(link or team_id),
        "link": bool(link),
        "token": str(link["token"]) if link else None,
        "url": (
            f"{config.site_url()}/share/{link['token']}" if link else None
        ),
        "expires_at": link["expires_at"] if link else None,
        "team_id": team_id,
    }


def _shape(value: Any) -> dict[str, Any] | None:
    report = store.load_json(value, None)
    if not isinstance(report, dict):
        return None
    return ShapeReport.from_dict(report).summary()


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    clean = frame.astype(object).where(pd.notna(frame), None)
    return clean.to_dict("records")


def _folder_tree(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nodes = {
        row["id"]: {**row, "children": []}
        for row in rows
    }
    roots: list[dict[str, Any]] = []
    for row in rows:
        node = nodes[row["id"]]
        parent = nodes.get(row.get("parent_id"))
        if parent:
            parent["children"].append(node)
        else:
            roots.append(node)
    for node in nodes.values():
        node["children"].sort(key=lambda item: (item["name"].casefold(), item["id"]))
    roots.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return roots


@router.get("/v1/library")
def library(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    teams.ensure_schema()
    folder_rows = store.rows_to_dicts(
        store.query(
            "SELECT DISTINCT f.id, f.user_id, f.team_id, f.parent_id, f.name, f.created_at "
            "FROM folders f LEFT JOIN team_members m ON m.team_id = f.team_id "
            "WHERE f.user_id = ? OR m.user_id = ? ORDER BY f.created_at",
            (identity.user_id, identity.user_id),
        )
    )
    visible_folders = {row["id"] for row in folder_rows}

    dataset_rows = store.rows_to_dicts(
        store.query(
            "SELECT DISTINCT d.id, d.name, d.description, d.folder_id, d.columns, "
            "d.row_count, d.shape_report, d.created_at, d.updated_at, d.user_id "
            "FROM datasets d LEFT JOIN team_objects o "
            "ON o.kind = 'dataset' AND o.object_id = d.id "
            "LEFT JOIN team_members m ON m.team_id = o.team_id "
            "WHERE d.user_id = ? OR m.user_id = ? "
            "ORDER BY d.created_at DESC LIMIT 500",
            (identity.user_id, identity.user_id),
        )
    )
    datasets: list[dict[str, Any]] = []
    for row in dataset_rows:
        columns = store.load_json(row.pop("columns", None), []) or []
        row.pop("user_id", None)
        if row.get("folder_id") not in visible_folders:
            row["folder_id"] = None
        row["columns"] = columns
        row["column_count"] = len(columns)
        row["shape_report"] = _shape(row.get("shape_report"))
        row["share"] = _share_state("dataset", row["id"])
        datasets.append(row)

    sources = registry.list_sources(identity.user_id)
    for source in sources:
        row = store.one(
            "SELECT folder_id FROM data_sources WHERE id = ? AND user_id = ?",
            (source["id"], identity.user_id),
        )
        source["folder_id"] = row["folder_id"] if row else None
        source["row_count"] = None
        source["column_count"] = None
        source["shape_report"] = None
        source["share"] = {"shared": False, "link": False, "team_id": None}

    return router.json_result(
        {
            "folders": _folder_tree(folder_rows),
            "datasets": datasets,
            "sources": sources,
        }
    )


@router.post("/v1/folders")
def create_folder(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")
    name = str(body.get("name") or "").strip()
    parent_id = body.get("parent_id") or None
    if not name:
        return router.error(400, "name_required")
    if parent_id and _owned_folder(identity.user_id, str(parent_id)) is None:
        return router.error(404, "parent_not_found")
    folder_id = store.new_id()
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO folders (id, user_id, team_id, parent_id, name, created_at) "
            "VALUES (?, ?, NULL, ?, ?, ?)",
            (folder_id, identity.user_id, parent_id, name[:120], now),
        )
    return router.json_result(
        {
            "id": folder_id,
            "name": name[:120],
            "parent_id": parent_id,
            "user_id": identity.user_id,
            "team_id": None,
            "created_at": now,
            "children": [],
        },
        status=201,
    )


def _would_cycle(folder_id: str, parent_id: str | None) -> bool:
    seen = {folder_id}
    current = parent_id
    while current:
        if current in seen:
            return True
        seen.add(current)
        row = store.one("SELECT parent_id FROM folders WHERE id = ?", (current,))
        if row is None:
            return False
        current = row["parent_id"]
    return False


@router.patch("/v1/folders/{folder_id}")
def patch_folder(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    folder_id = ctx.params["folder_id"]
    existing = _owned_folder(identity.user_id, folder_id)
    if existing is None:
        return router.error(404, "not_found")
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    name = existing["name"]
    if "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            return router.error(400, "name_required")
    parent_id = existing.get("parent_id")
    if "parent_id" in body:
        parent_id = body.get("parent_id") or None
        if parent_id and _owned_folder(identity.user_id, str(parent_id)) is None:
            return router.error(404, "parent_not_found")
    if _would_cycle(folder_id, str(parent_id) if parent_id else None):
        return router.error(409, "folder_cycle")

    with store.transaction() as conn:
        conn.execute(
            "UPDATE folders SET name = ?, parent_id = ? WHERE id = ? AND user_id = ?",
            (name[:120], parent_id, folder_id, identity.user_id),
        )
    return router.json_result(
        {"id": folder_id, "name": name[:120], "parent_id": parent_id}
    )


@router.delete("/v1/folders/{folder_id}")
def delete_folder(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    folder_id = ctx.params["folder_id"]
    if _owned_folder(identity.user_id, folder_id) is None:
        return router.error(404, "not_found")
    used = store.one(
        "SELECT "
        "(SELECT COUNT(*) FROM folders WHERE parent_id = ?) + "
        "(SELECT COUNT(*) FROM datasets WHERE folder_id = ?) + "
        "(SELECT COUNT(*) FROM data_sources WHERE folder_id = ?) AS n",
        (folder_id, folder_id, folder_id),
    )
    if used and int(used["n"]):
        return router.error(
            409,
            "folder_not_empty",
            "Move or delete child folders, datasets, and sources first.",
        )
    with store.transaction() as conn:
        conn.execute(
            "DELETE FROM folders WHERE id = ? AND user_id = ?",
            (folder_id, identity.user_id),
        )
    return router.json_result({"deleted": True, "id": folder_id})


def _validated_destination(user_id: str, value: Any) -> tuple[bool, str | None]:
    folder_id = str(value) if value not in (None, "") else None
    return _folder_access(user_id, folder_id), folder_id


@router.patch("/v1/datasets/{dataset_id}")
def patch_dataset(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    dataset_id = ctx.params["dataset_id"]
    if not teams.can_write(identity.user_id, "dataset", dataset_id):
        return router.error(404, "not_found")
    row = store.one(
        "SELECT user_id, name, description, folder_id FROM datasets WHERE id = ?",
        (dataset_id,),
    )
    if row is None:
        return router.error(404, "not_found")
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")
    name = str(body.get("name", row["name"]) or "").strip()
    if not name:
        return router.error(400, "name_required")
    description = (
        body.get("description") if "description" in body else row["description"]
    )
    folder_id = body.get("folder_id") if "folder_id" in body else row["folder_id"]
    allowed, folder_id = _validated_destination(identity.user_id, folder_id)
    if folder_id and row["user_id"] != identity.user_id:
        destination = _folder(folder_id)
        allowed = bool(
            destination
            and destination.get("team_id")
            and destination.get("team_id") == teams.team_of("dataset", dataset_id)
        )
    if not allowed:
        return router.error(404, "folder_not_found")
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "UPDATE datasets SET name = ?, description = ?, folder_id = ?, "
            "updated_at = ? WHERE id = ?",
            (name[:200], description, folder_id, now, dataset_id),
        )
    return router.json_result(
        {
            "id": dataset_id,
            "name": name[:200],
            "description": description,
            "folder_id": folder_id,
            "updated_at": now,
        }
    )


@router.patch("/v1/sources/{source_id}")
def patch_source(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    source_id = ctx.params["source_id"]
    row = store.one(
        "SELECT name, folder_id FROM data_sources WHERE id = ? AND user_id = ?",
        (source_id, identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")
    name = str(body.get("name", row["name"]) or "").strip()
    if not name:
        return router.error(400, "name_required")
    folder_id = body.get("folder_id") if "folder_id" in body else row["folder_id"]
    allowed, folder_id = _validated_destination(identity.user_id, folder_id)
    if not allowed:
        return router.error(404, "folder_not_found")
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "UPDATE data_sources SET name = ?, folder_id = ?, updated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (name[:200], folder_id, now, source_id, identity.user_id),
        )
    registry.invalidate(source_id)
    return router.json_result(
        {"id": source_id, "name": name[:200], "folder_id": folder_id, "updated_at": now}
    )


@router.post("/v1/datasets/{dataset_id}/share")
def share_dataset(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    body = ctx.json if isinstance(ctx.json, dict) else {}
    try:
        share = teams.mint_share(
            identity.user_id,
            "dataset",
            ctx.params["dataset_id"],
            expires_days=(
                int(body["expires_days"])
                if body.get("expires_days") not in (None, "", 0)
                else None
            ),
            label=str(body.get("label") or ""),
        )
    except PermissionError:
        return router.error(403, "not_yours_to_share")
    except (TypeError, ValueError):
        return router.error(400, "invalid_share")
    return router.json_result(share, status=201)


@router.delete("/v1/datasets/{dataset_id}/share")
def unshare_dataset(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    try:
        revoked = teams.revoke_object_shares(
            identity.user_id, "dataset", ctx.params["dataset_id"]
        )
    except PermissionError:
        return router.error(403, "not_yours")
    return router.json_result({"revoked": True, "links_revoked": revoked})


def _dataset_for_read(user_id: str, dataset_id: str) -> dict[str, Any] | None:
    if not teams.can_read(user_id, "dataset", dataset_id):
        return None
    return store.row_to_dict(
        store.one(
            "SELECT id, name, description, columns, row_count, storage, raw_storage, "
            "shape_report, sheet_name, folder_id, created_at, updated_at "
            "FROM datasets WHERE id = ?",
            (dataset_id,),
        )
    )


@router.get("/v1/datasets/{dataset_id}/preview")
def dataset_preview(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _dataset_for_read(identity.user_id, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    count = max(1, min(ctx.q_int("rows", 20), 200))
    try:
        frame = pd.read_parquet(row["storage"])
    except Exception as exc:  # noqa: BLE001
        return router.error(500, "dataset_unavailable", str(exc))
    report = ShapeReport.from_dict(store.load_json(row.get("shape_report"), {}))
    labels = report.labels
    schema = [
        {
            "name": str(column),
            "label": labels.get(str(column), str(column)),
            "dtype": str(frame[column].dtype),
        }
        for column in frame.columns
    ]
    return router.json_result(
        {
            "dataset_id": row["id"],
            "name": row["name"],
            "row_count": int(row["row_count"]),
            "column_count": len(frame.columns),
            "schema": schema,
            "rows": _records(frame.head(count)),
            "shape_report": report.to_dict(),
        }
    )


def _write_parquet(frame: pd.DataFrame, destination: Path) -> None:
    temporary = destination.with_name(destination.name + ".tmp")
    try:
        frame.to_parquet(temporary, index=False)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


@router.post("/v1/datasets/{dataset_id}/restructure")
def restructure_dataset(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    dataset_id = ctx.params["dataset_id"]
    if not teams.can_write(identity.user_id, "dataset", dataset_id):
        return router.error(404, "not_found")
    row = store.row_to_dict(
        store.one(
            "SELECT storage, raw_storage, shape_report, sheet_name "
            "FROM datasets WHERE id = ?",
            (dataset_id,),
        )
    )
    if row is None:
        return router.error(404, "not_found")
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, "invalid_body")

    source = Path(row.get("raw_storage") or row["storage"])
    requested_sheet = body.get("sheet")
    sheet = str(requested_sheet) if requested_sheet not in (None, "") else row.get("sheet_name")
    try:
        sheet_name, raw = raw_sheet(source, sheet)
        previous = ShapeReport.from_dict(store.load_json(row.get("shape_report"), {}))
        base = (
            previous
            if previous.sheet == sheet_name
            else detect_structure(
                raw,
                sheet=sheet_name,
                source_has_header=source.suffix.lower() in {
                    ".parquet",
                    ".pq",
                    ".json",
                    ".jsonl",
                    ".ndjson",
                }
                or bool(raw.attrs.get("_twohelixes_source_has_header")),
            )
        )
        report = report_from_override(raw, body, base=base, sheet=sheet_name)
        frame = apply_report(raw, report)
        if frame.shape[1] < 1:
            return router.error(400, "invalid_structure", "No usable columns remained.")
        previous_storage = Path(row["storage"])
        destination = previous_storage.parent / f"{dataset_id}-{store.new_id()}.parquet"
        _write_parquet(frame, destination)
    except (OSError, ValueError, KeyError) as exc:
        return router.error(400, "invalid_structure", str(exc))
    except Exception as exc:  # noqa: BLE001
        return router.error(500, "restructure_failed", str(exc))

    now = time.time()
    try:
        with store.transaction() as conn:
            conn.execute(
                "UPDATE datasets SET columns = ?, row_count = ?, storage = ?, "
                "raw_storage = COALESCE(raw_storage, ?), shape_report = ?, "
                "sheet_name = ?, updated_at = ? WHERE id = ?",
                (
                    store.dump_json([str(column) for column in frame.columns]),
                    int(len(frame)),
                    str(destination),
                    str(previous_storage),
                    store.dump_json(report.to_dict()),
                    sheet_name,
                    now,
                    dataset_id,
                ),
            )
    except Exception as exc:  # noqa: BLE001
        destination.unlink(missing_ok=True)
        return router.error(500, "restructure_failed", str(exc))
    if row.get("raw_storage") and previous_storage != Path(row["raw_storage"]):
        previous_storage.unlink(missing_ok=True)
    return router.json_result(
        {
            "dataset_id": dataset_id,
            "rows": int(len(frame)),
            "columns": [str(column) for column in frame.columns],
            "shape_report": report.to_dict(),
            "preview": _records(frame.head(20)),
        }
    )
