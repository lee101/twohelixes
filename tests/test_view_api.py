from pathlib import Path

import pandas as pd
import pytest

from twohelixes.view_api import MAX_DEPTH, ViewError, ViewStore, records_frame, sql_frame


@pytest.fixture
def api(tmp_path):
    def missing(owner, source):
        raise ViewError("not_found", 404)
    return ViewStore(tmp_path / "views.sqlite3", missing)


def seed(api):
    data = api.ingest("owner", {"name": "Sales", "rows": [
        {"city": "A", "sales": 2}, {"city": "A", "sales": 3}, {"city": "B", "sales": 9}]})
    view = api.define("owner", {"name": "Totals", "source": {"dataset_id": data["id"]},
        "sql": "SELECT city, SUM(sales) AS total FROM source GROUP BY city ORDER BY city"})
    return data, view


def test_live_composition_and_ingest_append(api):
    data, view = seed(api)
    child = api.define("owner", {"source": {"view_id": view["id"]},
        "sql": "SELECT * FROM source WHERE total > 6"})
    assert api.page("owner", child["id"])["rows"] == [{"city": "B", "total": 9}]
    updated = api.ingest("owner", {"revision": 1, "mode": "append", "rows": [
        {"city": "A", "sales": 7}]}, data["id"])
    assert updated["revision"] == 2
    assert updated["name"] == "Sales"
    assert api.page("owner", child["id"])["rows"] == [{"city": "A", "total": 12}, {"city": "B", "total": 9}]
    assert ViewStore(api.path, api.loader).page("owner", view["id"])["row_count"] == 2


def test_pagination_and_nulls(api):
    data = api.ingest("owner", {"rows": [{"n": 1}, {"n": None}, {"n": 3}]})
    view = api.define("owner", {"source": {"dataset_id": data["id"]}})
    page = api.page("owner", view["id"], 1, 1)
    assert page["rows"] == [{"n": None}]
    assert page["row_count"] == 3
    assert page["has_more"]
    assert api.page("owner", view["id"], 3)["rows"] == []
    empty = api.define("owner", {"source": {"view_id": view["id"]}, "sql": "SELECT n FROM source WHERE 0"})
    assert api.page("owner", empty["id"])["columns"] == ["n"]


@pytest.mark.parametrize("operation", ["get", "evaluate", "define", "ingest", "delete"])
def test_owner_isolation(api, operation):
    data, view = seed(api)
    with pytest.raises(ViewError) as error:
        if operation == "get":
            api.get("stranger", view["id"], "view")
        elif operation == "evaluate":
            api.evaluate("stranger", view["id"])
        elif operation == "define":
            api.define("stranger", {"source": {"view_id": view["id"]}})
        elif operation == "ingest":
            api.ingest("stranger", {"revision": 1, "rows": [{"n": 1}]}, data["id"])
        else:
            api.delete("stranger", view["id"], 1)
    assert error.value.status == 404
    assert api.list("stranger", 0, 100) == []


def test_revision_conflicts_are_atomic(api):
    data, view = seed(api)
    edited = api.define("owner", {"revision": 1, "sql": "SELECT city FROM source"}, view["id"])
    assert edited["revision"] == 2
    with pytest.raises(ViewError, match="revision_conflict"):
        api.define("owner", {"revision": 1, "name": "Wrong"}, view["id"])
    assert api.get("owner", view["id"], "view")["name"] == "Totals"
    api.ingest("owner", {"revision": 1, "rows": [{"city": "C", "sales": 8}]}, data["id"])
    with pytest.raises(ViewError, match="revision_conflict"):
        api.ingest("owner", {"revision": 1, "rows": [{"city": "D", "sales": 0}]}, data["id"])
    assert api.evaluate("owner", view["id"])["city"].tolist() == ["C"]


def test_cycle_rejected_and_delete_dependency(api):
    data, parent = seed(api)
    child = api.define("owner", {"source": {"view_id": parent["id"]}})
    with pytest.raises(ViewError, match="view_cycle"):
        api.define("owner", {"revision": 1, "source": {"view_id": child["id"]}}, parent["id"])
    with pytest.raises(ViewError, match="view_in_use"):
        api.delete("owner", parent["id"], 1)
    assert api.delete("owner", child["id"], 1)["deleted"]
    assert api.delete("owner", parent["id"], 1)["deleted"]


def test_depth_bounded(api):
    _, view = seed(api)
    for _ in range(MAX_DEPTH - 1):
        view = api.define("owner", {"source": {"view_id": view["id"]}})
    with pytest.raises(ViewError, match="view_depth_exceeded"):
        api.define("owner", {"source": {"view_id": view["id"]}})


@pytest.mark.parametrize("rows", [None, [], [1], [{"": 1}], [{"a": 1}, {"b": 2}],
    [{"a": float("nan")}], [{"a": float("inf")}], [{"a": []}], [{"a": {}}], [{"a": 1, "A": 2}]])
def test_invalid_records(rows):
    with pytest.raises(ViewError):
        records_frame({"rows": rows})


@pytest.mark.parametrize("sql", ["DELETE FROM source", "DROP TABLE source", "UPDATE source SET n=4",
    "ATTACH DATABASE '/tmp/stolen.db' AS stolen", "PRAGMA table_info(source)",
    "SELECT * FROM sqlite_master", "SELECT load_extension('evil')", "SELECT readfile('/etc/passwd')",
    "SELECT randomblob(100000000)", "SELECT * FROM source; SELECT * FROM source",
    "SELECT n AS x, n AS X FROM source", "SELECT * FROM nonexistent",
    "WITH RECURSIVE a(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM a) SELECT SUM(n) FROM a"])
def test_unsafe_or_invalid_sql(sql):
    with pytest.raises(ViewError):
        sql_frame(pd.DataFrame({"n": [1, 2]}), sql)


@pytest.mark.parametrize("source", [None, {}, {"view_id": "a", "dataset_id": "b"},
    {"url": "https://example.com"}, {"view_id": []}])
def test_invalid_sources(api, source):
    with pytest.raises(ViewError):
        api.define("owner", {"source": source})


@pytest.mark.parametrize("value", [0, -1, "abc", True, 1.5, 2001])
def test_invalid_pagination(api, value):
    _, view = seed(api)
    with pytest.raises(ViewError, match="invalid_integer"):
        api.page("owner", view["id"], limit=value)


def test_schema_mismatch_and_no_partial_ingest(api):
    data, view = seed(api)
    with pytest.raises(ViewError, match="schema_mismatch"):
        api.ingest("owner", {"revision": 1, "mode": "append", "rows": [{"n": 1}]}, data["id"])
    assert api.get("owner", data["id"], "data")["revision"] == 1


def test_external_input_authorization_checked_every_evaluation(tmp_path):
    allowed = True
    def load(owner, source):
        if not allowed:
            raise ViewError("not_found", 404)
        return pd.DataFrame({"n": [1]})
    api = ViewStore(tmp_path / "s.db", load)
    view = api.define("owner", {"source": {"filename": "authorized.parquet"}})
    allowed = False
    with pytest.raises(ViewError, match="not_found"):
        api.evaluate("owner", view["id"])
