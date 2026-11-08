import json
from types import SimpleNamespace

import pandas as pd
import pytest

from twohelixes import auth, router, store
from twohelixes.routes import query, teams, views
from twohelixes.view_api import ViewError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    teams._ready = False
    store.init()
    user = store.create_user("views@example.com")
    identity = auth.Identity(user_id=user["id"], email=user["email"], plan="free")
    monkeypatch.setattr(auth, "identify", lambda ctx: identity)
    def call(method, path, body=None, qs=""):
        status, _, _, payload = router.handle(method, path, qs, json.dumps(body) if body is not None else "", "")
        return int(status), json.loads(payload)
    yield call, identity
    store.close()
    store._initialised = False
    teams._ready = False


def test_http_lifecycle(client):
    call, identity = client
    status, data = call("POST", "/v1/ingest", {"rows": [{"n": 1}, {"n": 2}]})
    assert status == 201
    status, view = call("POST", "/v1/views", {"source": {"dataset_id": data["dataset_id"]}, "sql": "SELECT SUM(n) AS total FROM source"})
    assert status == 201
    status, page = call("GET", f'/v1/views/{view["id"]}/rows', qs="limit=1")
    assert page["rows"] == [{"total": 3}]
    assert status == 200
    assert call("GET", "/v1/views")[1]["views"][0]["id"] == view["id"]
    assert call("GET", f'/v1/views/{view["id"]}')[0] == 200
    status, _ = call("PUT", f'/v1/ingest/{data["id"]}', {"revision": 1, "rows": [{"n": 9}]})
    assert status == 200
    assert call("POST", f'/v1/views/{view["id"]}/evaluate', {"limit": 1})[1]["rows"] == [{"total": 9}]
    assert call("POST", f'/v1/views/{view["id"]}/edit', {"revision": 1, "sql": "SELECT n FROM source"})[0] == 200
    assert call("PATCH", f'/v1/views/{view["id"]}', {"revision": 1, "name": "Stale"})[0] == 409
    assert call("DELETE", f'/v1/views/{view["id"]}', {"revision": 2})[0] == 200
    assert call("GET", f'/v1/views/{view["id"]}')[0] == 404


def test_http_auth_and_validation(client, monkeypatch):
    call, _ = client
    assert call("POST", "/v1/ingest", [1])[0] == 400
    assert call("POST", "/v1/views", {})[0] == 400
    assert call("GET", "/v1/views", qs="limit=garbage")[0] == 400
    monkeypatch.setattr(auth, "identify", lambda ctx: auth.Identity())
    assert call("GET", "/v1/views")[0] == 401
    assert call("POST", "/v1/ingest", {"rows": [{"n": 1}]})[0] == 401


def test_query_and_edit_consume_view_without_model(client, monkeypatch):
    call, identity = client
    _, data = call("POST", "/v1/ingest", {"rows": [{"n": 1}, {"n": 2}]})
    _, view = call("POST", "/v1/views", {"source": {"dataset_id": data["id"]}, "sql": "SELECT n*3 AS total FROM source"})
    seen = []
    def run(question, frames, **kwargs):
        seen.append((frames["view"].to_dict("records"), kwargs["edit_request"]))
        return SimpleNamespace(figure={"data": []}, config={}, warnings=[], trace=[],
                               to_payload=lambda: {"figure": {"data": []}})
    monkeypatch.setattr(query, "_guard", lambda ctx, op: (identity, None))
    monkeypatch.setattr(query.orchestrator, "run", run)
    monkeypatch.setattr(query, "_safe_catalog", lambda *a: [])
    monkeypatch.setattr(query, "_charge", lambda *a: None)
    monkeypatch.setattr(query, "_persist_chart", lambda *a: "chart")
    assert call("POST", "/v1/query", {"q": "show total", "view_id": view["id"]})[0] == 200
    assert call("POST", "/v1/edit", {"q": "show total", "base_view_id": view["id"], "edit": "use bars"})[0] == 200
    assert seen == [([{"total": 3}, {"total": 6}], ""), ([{"total": 3}, {"total": 6}], "use bars")]
    assert call("POST", "/v1/query", {"q": "show", "view_id": view["id"], "data": []})[0] == 400
    assert call("POST", "/v1/query", {"q": "show", "view_id": "view_missing"})[0] == 404
    ctx = router.build_context("POST", "/", "", json.dumps({"dataset_id": data["id"]}), "")
    assert query._load_frames(identity, ctx)[data["id"]]["n"].tolist() == [1, 2]
