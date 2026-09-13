"""Schema-aware selection before a dataset file is opened."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from twohelixes import auth, router, store
from twohelixes.pipeline import semantic
from twohelixes.routes import connectors
from twohelixes.routes import query, teams


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    teams._ready_for = ""
    store.init()
    yield
    store.close()
    store._initialised = False
    teams._ready_for = ""


def _metadata(name: str, columns: list[str], description: str = "") -> dict[str, Any]:
    return {
        "id": store.new_id(),
        "name": name,
        "description": description,
        "columns": store.dump_json(columns),
    }


def _stored_dataset(
    tmp_path: Path, user_id: str, name: str, frame: pd.DataFrame, description: str = ""
) -> str:
    dataset_id = store.new_id()
    path = tmp_path / f"{dataset_id}.parquet"
    frame.to_parquet(path, index=False)
    now = time.time()
    store.execute(
        "INSERT INTO datasets (id, user_id, name, description, columns, row_count, "
        "storage, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dataset_id,
            user_id,
            name,
            description,
            store.dump_json([str(column) for column in frame.columns]),
            len(frame),
            str(path),
            now,
            now,
        ),
    )
    return dataset_id


def _ctx(identity: auth.Identity, body: dict[str, Any]) -> router.Context:
    context = router.build_context("POST", "/v1/query", "", json.dumps(body), "")
    context.user = identity
    return context


def test_lexical_schema_search_finds_the_dataset_without_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic, "rank_documents", lambda question, documents: [])
    sales = _metadata("Orders", ["order_date", "region", "revenue"])
    support = _metadata(
        "Support tickets", ["topic", "resolution_hours", "satisfaction"]
    )

    selected = query._select_datasets("show revenue by regions", [support, sales])
    assert [row["id"] for row in selected] == [sales["id"]]


def test_embedding_search_can_match_concepts_not_named_in_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sales = _metadata("Orders", ["amount", "region"])
    support = _metadata("Support tickets", ["resolution_hours", "satisfaction"])
    monkeypatch.setattr(
        semantic,
        "rank_documents",
        lambda question, documents: [(support["id"], 0.32), (sales["id"], 0.02)],
    )

    selected = query._select_datasets(
        "does customer happiness fall when replies take longer?", [sales, support]
    )
    assert [row["id"] for row in selected] == [support["id"]]


def test_an_unmatched_large_library_asks_for_a_dataset_instead_of_claiming_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(semantic, "rank_documents", lambda question, documents: [])
    rows = [
        _metadata("Orders", ["revenue", "region"]),
        _metadata("Tickets", ["topic", "resolution_hours"]),
    ]
    with pytest.raises(query.DataSelectionError) as caught:
        query._select_datasets("astronomical telescope temperatures", rows)
    assert caught.value.code == "dataset_not_selected"
    assert caught.value.candidates


def test_query_without_an_explicit_id_loads_only_the_schema_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = store.create_user("selector@test.local")
    identity = auth.Identity(user_id=user["id"], email=user["email"])
    orders = _stored_dataset(
        tmp_path,
        user["id"],
        "Orders",
        pd.DataFrame({"region": ["North"], "revenue": [10]}),
    )
    _stored_dataset(
        tmp_path,
        user["id"],
        "Tickets",
        pd.DataFrame({"topic": ["Login"], "resolution_hours": [2]}),
    )
    monkeypatch.setattr(semantic, "rank_documents", lambda question, documents: [])

    frames = query._load_frames(identity, _ctx(identity, {"q": "revenue by region"}), "revenue by region")
    assert list(frames) == ["Orders"]
    assert int(frames["Orders"]["revenue"].iloc[0]) == 10
    assert orders


def test_no_dataset_error_says_the_library_is_empty() -> None:
    user = store.create_user("empty-library@test.local")
    identity = auth.Identity(user_id=user["id"], email=user["email"])
    with pytest.raises(query.DataSelectionError) as caught:
        query._load_frames(identity, _ctx(identity, {"q": "revenue"}), "revenue")
    assert caught.value.code == "no_datasets"
    assert "don’t have any datasets" in str(caught.value)


def test_team_datasets_are_visible_to_auto_detect_and_the_picker(tmp_path: Path) -> None:
    owner = store.create_user("dataset-owner@test.local")
    member = store.create_user("dataset-member@test.local")
    dataset_id = _stored_dataset(
        tmp_path,
        owner["id"],
        "Team revenue",
        pd.DataFrame({"region": ["West"], "revenue": [25]}),
    )
    team_id = store.new_id()
    now = time.time()
    teams.ensure_schema()
    store.execute(
        "INSERT INTO teams (id, name, owner_id, created_at) VALUES (?, 'Data team', ?, ?)",
        (team_id, owner["id"], now),
    )
    for user_id, role in ((owner["id"], "owner"), (member["id"], "member")):
        store.execute(
            "INSERT INTO team_members (team_id, user_id, role, added_at) VALUES (?, ?, ?, ?)",
            (team_id, user_id, role, now),
        )
    store.execute(
        "INSERT INTO team_objects (team_id, kind, object_id, added_at) "
        "VALUES (?, 'dataset', ?, ?)",
        (team_id, dataset_id, now),
    )
    identity = auth.Identity(user_id=member["id"], email=member["email"])

    assert [row["id"] for row in query._visible_datasets(member["id"])] == [dataset_id]
    payload = connectors.list_datasets(_ctx(identity, {})).body["datasets"]
    assert [row["id"] for row in payload] == [dataset_id]
