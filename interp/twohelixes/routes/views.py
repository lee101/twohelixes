"""Saved data views, independent of chart rendering and model calls."""
from __future__ import annotations

from twohelixes import auth, config, router, store
from twohelixes.routes import teams
from twohelixes.view_api import MAX_BYTES, ViewError, ViewStore, integer


def view_store() -> ViewStore:
    def load(owner, source):
        from twohelixes.routes.query import _load_dataset
        dataset_id = source.get("dataset_id")
        if not dataset_id or not teams.can_read(owner, "dataset", dataset_id):
            raise ViewError("not_found", 404)
        row = store.one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))
        if row is None:
            raise ViewError("not_found", 404)
        try:
            return _load_dataset(row)
        except Exception:
            raise ViewError("data_unavailable", 409) from None
    return ViewStore(config.data_dir() / "view-api.sqlite3", load)


def _call(ctx, operation, status=200):
    identity = auth.require(ctx)
    if len(ctx.raw_body.encode()) > MAX_BYTES:
        return router.error(413, "data_too_large")
    try:
        result = operation(view_store(), identity.user_id)
        return router.json_result(result, status=status)
    except ViewError as exc:
        return router.error(exc.status, exc.code)


@router.post("/v1/ingest")
def ingest(ctx):
    return _call(ctx, lambda api, owner: api.ingest(owner, ctx.json), 201)


@router.put("/v1/ingest/{dataset_id}")
def update_ingest(ctx):
    return _call(ctx, lambda api, owner: api.ingest(owner, ctx.json, ctx.params["dataset_id"]))


@router.post("/v1/views")
def create_view(ctx):
    return _call(ctx, lambda api, owner: api.define(owner, ctx.json), 201)


@router.get("/v1/views")
def list_views(ctx):
    return _call(ctx, lambda api, owner: {"views": api.list(owner,
        integer(ctx.q("offset"), 0, 0, 10_000_000), integer(ctx.q("limit"), 100, 1, 200))})


@router.get("/v1/views/{view_id}")
def get_view(ctx):
    return _call(ctx, lambda api, owner: api.get(owner, ctx.params["view_id"], "view"))


@router.patch("/v1/views/{view_id}")
@router.post("/v1/views/{view_id}/edit")
def edit_view(ctx):
    return _call(ctx, lambda api, owner: api.define(owner, ctx.json, ctx.params["view_id"]))


@router.delete("/v1/views/{view_id}")
def delete_view(ctx):
    return _call(ctx, lambda api, owner: api.delete(owner, ctx.params["view_id"], ctx.field("revision")))


@router.get("/v1/views/{view_id}/rows")
@router.post("/v1/views/{view_id}/evaluate")
def evaluate_view(ctx):
    return _call(ctx, lambda api, owner: api.page(owner, ctx.params["view_id"],
                                                ctx.field("offset"), ctx.field("limit")))
