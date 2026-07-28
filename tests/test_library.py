"""Folders and dataset sharing preserve the existing ownership model."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from twohelixes import auth, router, store
from twohelixes.ingest import normalise_frame
from twohelixes.routes import library, teams


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    teams._ready = False
    store.init()
    teams.ensure_schema()
    yield
    store.close()
    store._initialised = False
    teams._ready = False


def _identity(email: str) -> auth.Identity:
    user = store.create_user(email)
    return auth.Identity(user_id=user["id"], email=email, plan="free")


def _ctx(
    identity: auth.Identity,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    query: str = "",
) -> router.Context:
    context = router.build_context(
        "POST", "/", query, json.dumps(body) if body is not None else "", ""
    )
    context.user = identity
    context.params = params or {}
    return context


def _dataset(identity: auth.Identity, tmp_path: Path) -> str:
    frame = pd.DataFrame({"city": ["A", "B"], "sales": [1, 2]})
    raw = tmp_path / f"{store.new_id()}.source.parquet"
    storage = tmp_path / f"{store.new_id()}.parquet"
    frame.to_parquet(raw, index=False)
    frame.to_parquet(storage, index=False)
    report = normalise_frame(frame).report
    dataset_id = store.new_id()
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO datasets (id, user_id, name, description, columns, "
            "row_count, storage, raw_storage, shape_report, sheet_name, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dataset_id,
                identity.user_id,
                "Sales",
                "A dataset",
                store.dump_json(["city", "sales"]),
                2,
                str(storage),
                str(raw),
                store.dump_json(report.to_dict()),
                "Sheet1",
                now,
                now,
            ),
        )
    return dataset_id


def test_folder_crud_cycle_rejection_and_nonempty_refusal(tmp_path: Path) -> None:
    owner = _identity("folders@example.com")
    root_response = library.create_folder(_ctx(owner, body={"name": "Root"}))
    root = root_response.body["id"]
    child_response = library.create_folder(
        _ctx(owner, body={"name": "Child", "parent_id": root})
    )
    child = child_response.body["id"]

    cycle = library.patch_folder(
        _ctx(owner, body={"parent_id": child}, params={"folder_id": root})
    )
    assert cycle.status == 409
    assert cycle.body["error"] == "folder_cycle"

    refused = library.delete_folder(
        _ctx(owner, params={"folder_id": root})
    )
    assert refused.status == 409
    assert refused.body["error"] == "folder_not_empty"

    assert library.delete_folder(
        _ctx(owner, params={"folder_id": child})
    ).body["deleted"]
    renamed = library.patch_folder(
        _ctx(owner, body={"name": "Renamed"}, params={"folder_id": root})
    )
    assert renamed.body["name"] == "Renamed"
    assert library.delete_folder(
        _ctx(owner, params={"folder_id": root})
    ).body["deleted"]


def test_dataset_rename_move_preview_and_library_payload(tmp_path: Path) -> None:
    owner = _identity("move@example.com")
    dataset_id = _dataset(owner, tmp_path)
    folder = library.create_folder(_ctx(owner, body={"name": "Analysis"})).body["id"]

    updated = library.patch_dataset(
        _ctx(
            owner,
            body={"name": "FY Sales", "folder_id": folder, "description": "Current"},
            params={"dataset_id": dataset_id},
        )
    )
    assert updated.body["folder_id"] == folder
    assert updated.body["name"] == "FY Sales"

    preview = library.dataset_preview(
        _ctx(owner, params={"dataset_id": dataset_id}, query="rows=1")
    )
    assert preview.body["row_count"] == 2
    assert len(preview.body["rows"]) == 1
    assert preview.body["schema"][0]["name"] == "city"

    payload = library.library(_ctx(owner)).body
    assert payload["folders"][0]["id"] == folder
    assert payload["datasets"][0]["column_count"] == 2
    assert payload["datasets"][0]["shape_report"]["confidence"] == 1.0


def test_source_rename_and_move(tmp_path: Path) -> None:
    owner = _identity("source-move@example.com")
    folder = library.create_folder(_ctx(owner, body={"name": "Sources"})).body["id"]
    source_id = store.new_id()
    now = time.time()
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO data_sources (id, user_id, name, kind, config, created_at, "
            "updated_at) VALUES (?, ?, ?, 'files', ?, ?, ?)",
            (source_id, owner.user_id, "Old", "not-used", now, now),
        )

    updated = library.patch_source(
        _ctx(
            owner,
            body={"name": "Files", "folder_id": folder},
            params={"source_id": source_id},
        )
    )

    assert updated.body["name"] == "Files"
    assert updated.body["folder_id"] == folder


def test_restructure_rewrites_parquet_with_validated_overrides(tmp_path: Path) -> None:
    owner = _identity("restructure@example.com")
    dataset_id = _dataset(owner, tmp_path)

    response = library.restructure_dataset(
        _ctx(
            owner,
            body={
                "header_row": -1,
                "renames": {"sales": "Net sales"},
                "types": {"Net sales": "number"},
            },
            params={"dataset_id": dataset_id},
        )
    )

    assert response.status == 200
    assert response.body["columns"] == ["city", "net_sales"]
    row = store.one("SELECT storage, columns FROM datasets WHERE id = ?", (dataset_id,))
    assert store.load_json(row["columns"], []) == ["city", "net_sales"]
    assert list(pd.read_parquet(row["storage"]).columns) == ["city", "net_sales"]


def test_share_and_unshare_permissions(tmp_path: Path) -> None:
    owner = _identity("share-owner@example.com")
    stranger = _identity("share-stranger@example.com")
    dataset_id = _dataset(owner, tmp_path)

    denied = library.share_dataset(
        _ctx(stranger, body={}, params={"dataset_id": dataset_id})
    )
    assert denied.status == 403

    shared = library.share_dataset(
        _ctx(owner, body={"label": "Read only"}, params={"dataset_id": dataset_id})
    )
    assert shared.status == 201
    token = shared.body["token"]
    assert teams.can_read(None, "dataset", dataset_id, token=token)
    assert not teams.can_write(stranger.user_id, "dataset", dataset_id)

    denied_unshare = library.unshare_dataset(
        _ctx(stranger, params={"dataset_id": dataset_id})
    )
    assert denied_unshare.status == 403
    revoked = library.unshare_dataset(
        _ctx(owner, params={"dataset_id": dataset_id})
    )
    assert revoked.body["links_revoked"] == 1
    assert teams.resolve_share(token) is None
