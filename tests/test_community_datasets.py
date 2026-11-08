"""Public community datasets: publishing, curation, and what must never leak."""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from twohelixes import auth, config, ratelimit, router, store
from twohelixes.routes import community
from twohelixes.routes import connectors as connector_routes
from twohelixes.routes import dashboards as dashboard_routes


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    store.init()
    router.build()
    yield
    store.close()
    store._initialised = False


def _identity(email: str) -> auth.Identity:
    user = store.create_user(email)
    return auth.Identity(user_id=user["id"], email=email, plan="free")


def _ctx(identity, body=None, params=None, query="", method="POST") -> router.Context:
    context = router.build_context(
        method, "/", query, json.dumps(body) if body is not None else "", ""
    )
    context.user = identity
    context.params = params or {}
    return context


def _get(path: str, query: str = "") -> tuple[int, str, dict, str]:
    status, content_type, headers, body = router.handle("GET", path, query, "", "{}")
    return int(status), content_type, json.loads(headers) if headers else {}, body


def _dataset(identity, tmp_path: Path, rows: int = 80) -> str:
    dataset_id = store.new_id()
    path = tmp_path / f"{dataset_id}.parquet"
    pd.DataFrame(
        {"city": [f"c{i}" for i in range(rows)], "value": list(range(rows))}
    ).to_parquet(path, index=False)
    raw = tmp_path / "secret-upload.csv"
    raw.write_text("x")
    now = time.time()
    store.execute(
        "INSERT INTO datasets (id, user_id, name, description, columns, row_count, "
        "storage, raw_storage, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (dataset_id, identity.user_id, "Cities", "", '["city","value"]', rows,
         str(path), str(raw), now, now),
    )
    return dataset_id


def _chart(identity, title="Chart") -> str:
    chart_id = store.new_id()
    now = time.time()
    spec = {
        "data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}],
        "layout": {"title": {"text": title}},
    }
    store.execute(
        "INSERT INTO charts (id, user_id, title, query, spec, graph_args, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?)",
        (chart_id, identity.user_id, title, "SELECT secret FROM private_table",
         json.dumps(spec), "{}", now, now),
    )
    return chart_id


def _publish(identity, dataset_id, **extra) -> router.Result:
    body = {"confirm_public": True, "slug": "city-values", "title": "City values", **extra}
    return community.publish(_ctx(identity, body, {"dataset_id": dataset_id}, method="PUT"))


@pytest.fixture()
def owner(tmp_path):
    identity = _identity("owner@example.com")
    return identity, _dataset(identity, tmp_path)


def test_publish_requires_confirmation_and_ownership(owner, tmp_path) -> None:
    identity, dataset_id = owner
    result = community.publish(
        _ctx(identity, {"slug": "city-values"}, {"dataset_id": dataset_id}, method="PUT")
    )
    assert result.status == 400 and result.body["error"] == "confirm_required"

    stranger = _identity("stranger@example.com")
    assert _publish(stranger, dataset_id).status == 404
    assert _publish(identity, dataset_id).status == 200
    assert community.unpublish(
        _ctx(stranger, None, {"dataset_id": dataset_id}, method="DELETE")
    ).status == 404

    with pytest.raises(auth.Unauthorized):
        _publish(auth.Identity(), dataset_id)


def test_publish_then_page_then_unpublish_is_404(owner) -> None:
    identity, dataset_id = owner
    assert _get("/datasets/u/city-values")[0] == 404
    result = _publish(identity, dataset_id, description="# Notes\n\nHello **world**")
    assert result.body["published"] and result.body["url"].endswith("/datasets/u/city-values")

    status, ctype, _h, body = _get("/datasets/u/city-values")
    assert status == 200 and ctype.startswith("text/html")
    assert "<h2>Notes</h2>" in body and "<strong>world</strong>" in body
    assert 'rel="canonical"' in body and "og:title" in body
    payload = json.loads(
        body.split('application/ld+json">')[1].split("</script>")[0]
    )
    assert payload["@type"] == "Dataset" and "distribution" not in payload
    assert _get("/v1/public/datasets/city-values")[0] == 200

    community.unpublish(_ctx(identity, None, {"dataset_id": dataset_id}, method="DELETE"))
    status, _c, _h, body = _get("/datasets/u/city-values")
    assert status == 404 and "noindex" in body
    assert _get("/v1/public/datasets/city-values")[0] == 404
    assert _get("/v1/public/datasets/city-values/download.csv")[0] == 404
    assert "city-values" not in _get("/sitemap.xml")[3]


def test_description_is_sanitised() -> None:
    hostile = (
        "<script>alert(1)</script> [x](javascript:alert(1)) "
        "[ok](https://example.com/a?b=1&c=*2*) ![i](http://x/y.png) "
        '<img src=x onerror=alert(1)> **b** *i* `<b>` \n\n- one\n- two\n\n```\n<i>\n```'
    )
    out = community.render_markdown(hostile)
    assert "<script" not in out and "<img" not in out
    assert 'href="javascript' not in out
    assert 'href="https://example.com/a?b=1&amp;c=*2*" rel="nofollow ugc noopener"' in out
    assert "<strong>b</strong>" in out and "<em>i</em>" in out
    assert "<code>&lt;b&gt;</code>" in out
    assert "<ul><li>one</li><li>two</li></ul>" in out
    assert "<pre><code>&lt;i&gt;</code></pre>" in out


def test_xss_in_every_public_field(owner) -> None:
    identity, dataset_id = owner
    payload = '"><script>alert(1)</script>'
    result = _publish(
        identity, dataset_id, title=f"T{payload}", author_name=payload,
        description=payload,
    )
    assert result.status == 200
    _s, _c, _h, body = _get("/datasets/u/city-values")
    assert "<script>alert(1)" not in body
    assert "\\u003cscript" in body or "&lt;script" in body


def test_slug_rules(owner, tmp_path) -> None:
    identity, dataset_id = owner
    for bad in ("u", "community", "iris", "Ab", "a--b", "-abc", "x" * 70, "has space"):
        assert _publish(identity, dataset_id, slug=bad).status == 400, bad
    assert _publish(identity, dataset_id).status == 200
    other = _dataset(_identity("b@example.com"), tmp_path)
    b = store.one("SELECT user_id FROM datasets WHERE id = ?", (other,))
    clash = community.publish(
        _ctx(auth.Identity(user_id=b["user_id"]), {"confirm_public": True, "slug": "city-values"},
             {"dataset_id": other}, method="PUT")
    )
    assert clash.status == 409 and clash.body["error"] == "slug_taken"
    assert _publish(identity, dataset_id, tags=["a b", "BAD!"]).status == 400
    assert _publish(identity, dataset_id, license="nope").status == 400
    assert _publish(identity, dataset_id, title="x").status == 400
    assert _publish(identity, dataset_id, description="x" * 10_001).status == 400


def test_linking_enables_public_and_tells_the_caller(owner) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id)
    chart_id = _chart(identity, "Revenue")
    board = dashboard_routes.create_dashboard(_ctx(identity, {"title": "Board"})).body["id"]

    def link(kind, object_id, caption="why"):
        return community.add_link(
            _ctx(identity, {"kind": kind, "id": object_id, "caption": caption},
                 {"dataset_id": dataset_id})
        )

    first = link("chart", chart_id)
    assert first.status == 201 and first.body["auto_enabled"]["chart_public"]
    assert first.body["notice"]
    again = link("chart", chart_id, "new caption")
    assert again.status == 200 and not again.body["auto_enabled"]["chart_public"]
    assert len(again.body["links"]) == 1 and again.body["links"][0]["caption"] == "new caption"

    second = link("dashboard", board)
    assert second.body["auto_enabled"]["dashboard_share"]
    token = store.one("SELECT share_token FROM dashboards WHERE id = ?", (board,))["share_token"]
    assert token and second.body["share_url"].endswith(f"/share/{token}")

    _s, _c, _h, body = _get("/datasets/u/city-values")
    assert "<svg" in body and "new caption" in body and f"/share/{token}" in body
    assert "private_table" not in body

    api = json.loads(_get("/v1/public/datasets/city-values")[3])
    assert [a["kind"] for a in api["analytics"]] == ["chart", "dashboard"]

    dashboard_routes.share_dashboard(
        _ctx(identity, {"public": False}, {"dashboard_id": board})
    )
    api = json.loads(_get("/v1/public/datasets/city-values")[3])
    assert [a["kind"] for a in api["analytics"]] == ["chart"]

    gone = community.remove_link(
        _ctx(identity, None, {"dataset_id": dataset_id},
             query=f"kind=chart&id={chart_id}&revoke=true", method="DELETE")
    )
    assert gone.body["unlinked"] and not gone.body["still_public"]
    assert store.one("SELECT is_public FROM charts WHERE id = ?", (chart_id,))["is_public"] == 0
    assert json.loads(_get("/v1/public/datasets/city-values")[3])["analytics"] == []


def test_cannot_link_someone_elses_items(owner) -> None:
    identity, dataset_id = owner
    stranger = _identity("s@example.com")
    foreign = _chart(stranger)
    result = community.add_link(
        _ctx(identity, {"kind": "chart", "id": foreign}, {"dataset_id": dataset_id})
    )
    assert result.status == 404
    assert store.one("SELECT is_public FROM charts WHERE id = ?", (foreign,))["is_public"] == 0
    mine = _chart(identity)
    assert community.add_link(
        _ctx(stranger, {"kind": "chart", "id": mine}, {"dataset_id": dataset_id})
    ).status == 404
    assert community.add_link(
        _ctx(identity, {"kind": "sheet", "id": mine}, {"dataset_id": dataset_id})
    ).status == 400


def test_preview_is_bounded_and_nothing_private_leaks(owner) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id, allow_download=True)
    raw = _get("/v1/public/datasets/city-values")[3]
    api = json.loads(raw)
    assert len(api["preview"]) == community.PREVIEW_ROWS and api["row_count"] == 80
    assert api["schema"][0]["name"] == "city"
    page = _get("/datasets/u/city-values")[3]
    assert page.count("<tr>") <= community.PREVIEW_ROWS + 1 + 2 + 1
    listing = _get("/v1/public/datasets")[3]
    for text in (raw, page, listing, _get("/datasets/community")[3]):
        for forbidden in (
            identity.user_id, "owner@example.com", "secret-upload", ".parquet",
            "storage", "raw_storage", "user_id", "source_id", "shape_report",
        ):
            assert forbidden not in text, forbidden
    assert "Community member" in raw


def test_csv_download_flag_and_cap(owner, monkeypatch) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id)
    assert _get("/v1/public/datasets/city-values/download.csv")[0] == 403
    assert "Download CSV" not in _get("/datasets/u/city-values")[3]

    _publish(identity, dataset_id, allow_download=True)
    status, ctype, headers, body = _get("/v1/public/datasets/city-values/download.csv")
    assert status == 200 and ctype.startswith("text/csv")
    assert len(body.strip().splitlines()) == 81 and "X-Rows-Truncated" not in headers
    assert "Download CSV" in _get("/datasets/u/city-values")[3]

    monkeypatch.setattr(community, "MAX_CSV_ROWS", 10)
    _s, _c, headers, body = _get("/v1/public/datasets/city-values/download.csv")
    assert len(body.strip().splitlines()) == 11 and headers["X-Rows-Truncated"] == "10"


def test_listing_search_tags_pagination_and_sitemap(owner, tmp_path, monkeypatch) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id, tags=["geo", "demo"], description="Population of cities")
    second = _dataset(identity, tmp_path)
    _publish_second = community.publish(
        _ctx(identity, {"confirm_public": True, "slug": "other-set", "title": "Other set",
                        "tags": ["finance"]}, {"dataset_id": second}, method="PUT")
    )
    assert _publish_second.status == 200

    def listing(query):
        return json.loads(_get("/v1/public/datasets", query)[3])

    assert listing("")["total"] == 2
    assert [d["slug"] for d in listing("tag=geo")["datasets"]] == ["city-values"]
    assert [d["slug"] for d in listing("q=population")["datasets"]] == ["city-values"]
    assert listing("q=%25")["total"] == 0
    monkeypatch.setattr(community, "PAGE_SIZE", 1)
    assert len(listing("page=2")["datasets"]) == 1
    page = _get("/datasets/community", "tag=finance")[3]
    assert "Other set" in page and "City values" not in page

    _s, _c, _h, xml = _get("/sitemap.xml")
    locs = [e.text for e in ET.fromstring(xml).iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    site = config.site_url().rstrip("/")
    assert f"{site}/datasets/u/city-values" in locs and f"{site}/datasets/community" in locs
    for path in ("/datasets/u/city-values", "/datasets/community"):
        assert _get(path)[0] == 200


def test_public_reads_are_rate_limited(owner, monkeypatch) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id, allow_download=True)
    monkeypatch.setattr(config, "PUBLIC_READ_PER_MINUTE", 3)
    codes = [_get("/v1/public/datasets")[0] for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]
    assert _get("/datasets/community")[0] == 429
    assert ratelimit.public_read(_ctx(None), "public_download") is None


def test_deleting_a_dataset_drops_its_links(owner) -> None:
    identity, dataset_id = owner
    chart = _chart(identity)
    community.add_link(_ctx(identity, {"kind": "chart", "id": chart}, {"dataset_id": dataset_id}))
    connector_routes.delete_dataset(_ctx(identity, None, {"dataset_id": dataset_id}, method="DELETE"))
    assert store.one("SELECT 1 FROM dataset_links WHERE dataset_id = ?", (dataset_id,)) is None


def test_import_copies_only_downloadable_datasets(owner) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id)
    visitor = _identity("v@example.com")
    call = lambda: community.public_import(_ctx(visitor, None, {"slug": "city-values"}))
    assert call().status == 403
    _publish(identity, dataset_id, allow_download=True)
    done = call()
    assert done.status == 201
    mine = store.one("SELECT user_id, row_count FROM datasets WHERE id = ?", (done.body["dataset_id"],))
    assert mine["user_id"] == visitor.user_id and mine["row_count"] == 80


def test_additive_migration_is_safe_on_an_old_database(tmp_path, monkeypatch) -> None:
    import sqlite3

    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path / "old"))
    (tmp_path / "old").mkdir()
    conn = sqlite3.connect(tmp_path / "old" / "twohelixes.db")
    conn.execute(
        "CREATE TABLE datasets (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, "
        "columns TEXT NOT NULL, storage TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute("INSERT INTO datasets VALUES ('d','u','n','[]','s',1)")
    conn.commit()
    conn.close()
    store.close()
    store._initialised = False
    store.init()
    store._initialised = False
    store.init()
    row = store.one("SELECT is_public, public_slug, allow_download FROM datasets WHERE id = 'd'")
    assert row["is_public"] == 0 and row["public_slug"] is None


def test_hostile_chart_title_is_escaped_in_the_gallery(owner) -> None:
    identity, dataset_id = owner
    _publish(identity, dataset_id)
    chart = _chart(identity, "<script>alert(9)</script>")
    community.add_link(
        _ctx(identity, {"kind": "chart", "id": chart, "caption": "<b>x</b>"},
             {"dataset_id": dataset_id})
    )
    body = _get("/datasets/u/city-values")[3]
    assert "<script>alert(9)" not in body and "<b>x</b>" not in body
