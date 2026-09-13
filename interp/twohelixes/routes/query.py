"""Chat-to-chart: the main product surface.

Two shapes of the same work. `/v1/query` runs the pipeline and returns the
finished chart, which is what the API and dashboard refresh use.
`/v1/query/stream` runs it on a worker thread and streams every stage boundary,
thought and partial configuration as it happens - that is what makes the UI
feel like watching an analyst work rather than waiting on a spinner.

Rate limiting happens before any model call and charging happens only after
success, so a pipeline that fails does not burn a free query.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from twohelixes import auth, config, ratelimit, router, store
from twohelixes.connectors import registry
from twohelixes.pipeline import orchestrator
from twohelixes.routes import teams

log = logging.getLogger("twohelixes.routes.query")

MAX_QUESTION_CHARS = 2000
MAX_SEARCHABLE_DATASETS = 500
MAX_AUTO_DATASETS = 3
SCHEMA_SHORTLIST = 24
SCHEMA_COLUMNS_PER_PHRASE = 12
MAX_SCHEMA_COLUMNS = 120


class DataSelectionError(Exception):
    """A useful, actionable refusal from dataset discovery."""

    def __init__(self, code: str, message: str, candidates: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


def _sample_key(ctx: router.Context) -> str:
    """The sample dataset this request names, if any.

    The trial runs on these and nothing else: an anonymous caller gets to see
    the product work, not a free interpreter pointed at data they uploaded.
    """
    from twohelixes.datasets import samples

    key = str(ctx.field("sample") or "").strip()
    return key if key in samples.BY_KEY else ""


def _load_frames(
    identity: Any, ctx: router.Context, question: str = ""
) -> dict[str, Any]:
    """Resolve whatever data the request points at into named frames."""
    frames: dict[str, Any] = {}

    sample = _sample_key(ctx)
    if sample:
        from twohelixes.datasets import samples

        samples.materialise()
        return {sample: samples.frame(sample)}

    inline = ctx.field("data")
    if isinstance(inline, list) and inline:
        import pandas as pd

        frames["inline"] = pd.DataFrame(inline)
        return frames

    source_id = ctx.field("source_id")
    sql = ctx.field("sql")
    if source_id and sql:
        connector = registry.for_source(str(source_id), identity.user_id)
        frames[str(ctx.field("name") or "query")] = connector.frame(str(sql))
        return frames
    if source_id:
        source = store.one(
            "SELECT name FROM data_sources WHERE id = ? AND user_id = ?",
            (str(source_id), identity.user_id),
        )
        label = str(source["name"]) if source else "That connection"
        raise DataSelectionError(
            "source_query_required",
            f"{label} is a database connection, not a loaded dataset. "
            "Choose a dataset from the Library, or open SQL to select a table.",
        )

    dataset_ids = ctx.field("dataset_ids") or []
    if isinstance(dataset_ids, str):
        dataset_ids = [dataset_ids]
    rows: list[Any] = []
    if dataset_ids:
        for dataset_id in dataset_ids:
            if not teams.can_read(identity.user_id, "dataset", str(dataset_id)):
                continue
            row = store.one("SELECT * FROM datasets WHERE id = ?", (dataset_id,))
            if row is not None:
                rows.append(row)
        if not rows:
            raise DataSelectionError(
                "dataset_not_found",
                "The selected dataset is no longer available. Choose another from the Library.",
            )
    elif getattr(identity, "signed_in", False):
        visible = _visible_datasets(identity.user_id)
        if not visible:
            raise DataSelectionError(
                "no_datasets",
                "You don’t have any datasets yet. Upload a file or attach a sample "
                "from the Library, then ask again.",
            )
        rows = _select_datasets(question, visible)

    for row in rows:
        name = str(row["name"] or "Dataset")
        key = name if name not in frames else f"{name} ({str(row['id'])[:6]})"
        frames[key] = _load_dataset(row)

    return frames


def _load_dataset(row: Any) -> Any:
    import pandas as pd

    storage = row["storage"]
    if storage.endswith(".parquet"):
        return pd.read_parquet(storage)
    if storage.endswith(".csv"):
        return pd.read_csv(storage)
    return pd.read_json(storage)


def _visible_datasets(user_id: str) -> list[dict[str, Any]]:
    """Dataset metadata visible directly or through a team; no files opened."""
    teams.ensure_schema()
    rows = store.query(
        "SELECT DISTINCT d.* FROM datasets d "
        "LEFT JOIN team_objects o ON o.kind = 'dataset' AND o.object_id = d.id "
        "LEFT JOIN team_members m ON m.team_id = o.team_id AND m.user_id = ? "
        "WHERE d.user_id = ? OR m.user_id = ? "
        "ORDER BY d.updated_at DESC, d.created_at DESC LIMIT ?",
        (user_id, user_id, user_id, MAX_SEARCHABLE_DATASETS),
    )
    return store.rows_to_dicts(rows)


_SEARCH_STOPWORDS = {
    "a", "an", "and", "are", "at", "be", "by", "did", "do", "does",
    "for", "from", "has", "have", "how", "i", "in", "is", "it", "me",
    "of", "on", "or", "our", "show", "that", "the", "this", "to", "was",
    "what", "when", "where", "which", "who", "with",
}


def _terms(value: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", value.casefold().replace("_", " "))
    return {
        word[:-1] if len(word) > 4 and word.endswith("s") else word
        for word in words
        if len(word) > 1 and word not in _SEARCH_STOPWORDS
    }


def _dataset_columns(row: Any) -> list[str]:
    columns = store.load_json(row.get("columns"), [])
    return [str(column) for column in columns] if isinstance(columns, list) else []


def _select_datasets(question: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Narrow a large library using lexical + local schema embeddings."""
    if len(rows) == 1:
        return rows
    if not question:
        raise DataSelectionError(
            "dataset_not_selected",
            f"You have {len(rows)} datasets. Choose one from the Library so I know what to analyse.",
            [str(row.get("name") or "Dataset") for row in rows[:5]],
        )

    from twohelixes.pipeline import semantic

    query_terms = _terms(question)
    coarse_documents: dict[str, list[str]] = {}
    columns_by_id: dict[str, list[str]] = {}
    lexical: dict[str, float] = {}
    by_id = {str(row["id"]): row for row in rows}
    for dataset_id, row in by_id.items():
        name = str(row.get("name") or "Dataset")
        description = str(row.get("description") or "")
        columns = _dataset_columns(row)[:MAX_SCHEMA_COLUMNS]
        columns_by_id[dataset_id] = columns
        chunks = [
            columns[index : index + SCHEMA_COLUMNS_PER_PHRASE]
            for index in range(0, len(columns), SCHEMA_COLUMNS_PER_PHRASE)
        ]
        coarse_documents[dataset_id] = [
            f"{name}. {description}",
            *(f"{name} columns: {', '.join(chunk)}" for chunk in chunks),
        ]
        name_terms = _terms(name)
        schema_terms = _terms(" ".join([description, *columns]))
        name_hits = len(query_terms & name_terms)
        schema_hits = len(query_terms & schema_terms)
        lexical[dataset_id] = name_hits * 0.3 + schema_hits * 0.12

    coarse = dict(semantic.rank_documents(question, coarse_documents))
    preliminary = sorted(
        (
            coarse.get(dataset_id, -1.0) + lexical[dataset_id],
            lexical[dataset_id],
            dataset_id,
        )
        for dataset_id in by_id
    )[::-1]
    shortlist_ids = {
        dataset_id for _score, _lexical, dataset_id in preliminary[:SCHEMA_SHORTLIST]
    }
    # A literal schema/name match is always worth the fine pass even if a
    # noisy coarse embedding did not put it in the first page.
    shortlist_ids.update(key for key, value in lexical.items() if value > 0)
    fine_documents = {
        dataset_id: [
            coarse_documents[dataset_id][0],
            *(
                f"{by_id[dataset_id].get('name') or 'Dataset'}: {column}"
                for column in columns_by_id[dataset_id]
            ),
        ]
        for dataset_id in shortlist_ids
    }
    embedded = dict(semantic.rank_documents(question, fine_documents))
    scored = sorted(
        (
            embedded.get(dataset_id, coarse.get(dataset_id, -1.0))
            + lexical[dataset_id],
            lexical[dataset_id],
            dataset_id,
        )
        for dataset_id in by_id
    )[::-1]
    top_score = scored[0][0]
    qualified = [
        item
        for item in scored
        if item[1] > 0
        or embedded.get(item[2], -1.0) >= semantic.ACCEPT_SCORE
    ]
    if not qualified:
        names = [str(by_id[item[2]].get("name") or "Dataset") for item in scored[:5]]
        raise DataSelectionError(
            "dataset_not_selected",
            f"I found {len(rows)} datasets, but none of their names or columns clearly "
            "match that question. Choose one from the Library or mention its name.",
            names,
        )

    selected = [
        by_id[dataset_id]
        for score, _lexical, dataset_id in qualified
        if score >= top_score - 0.08
    ][:MAX_AUTO_DATASETS]
    return selected or [by_id[qualified[0][2]]]


def _catalog(identity: Any, available: set[str] | None = None) -> list[dict[str, Any]]:
    rows = _visible_datasets(identity.user_id)
    catalog = [
        {
            "id": row["id"],
            "name": row["name"],
            "description": row.get("description") or "",
            "columns": _dataset_columns(row),
        }
        for row in rows
    ]
    if available is not None:
        catalog = [entry for entry in catalog if str(entry["name"]) in available]
    return catalog


# Refusals that mean "pay or sign in", not "slow down".
_PAYMENT_REASONS = ("insufficient_credits", "out_of_allowance", "free_quota_exhausted")


def _guard(ctx: router.Context, operation: str) -> tuple[Any, router.Result | None]:
    """Decide whether this request runs, and say what to do if it does not.

    Anonymous callers are no longer refused outright: `ratelimit.check` allows
    one sample-data question a day, which is the only way to show the product
    working before asking for an account.
    """
    identity = ctx.user
    decision = ratelimit.check(
        identity, operation, sample_data=bool(_sample_key(ctx))
    )
    if not decision.allowed:
        if decision.reason in ("signin_required", "trial_used"):
            status = 401
        elif decision.reason in _PAYMENT_REASONS:
            status = 402
        else:
            status = 429
        return identity, router.Result(status=status, body=decision.to_payload())

    return identity, None


@router.post("/v1/query")
def run_query(ctx: router.Context) -> router.Result:
    identity, refusal = _guard(ctx, "chat_query")
    if refusal is not None:
        return refusal

    question = str(ctx.field("q") or ctx.field("question") or "").strip()
    if not question:
        return router.error(400, "missing_question")
    if len(question) > MAX_QUESTION_CHARS:
        return router.error(400, "question_too_long")

    try:
        frames = _load_frames(identity, ctx, question)
    except DataSelectionError as exc:
        return router.error(400, exc.code, str(exc), candidates=exc.candidates)
    except Exception as exc:  # noqa: BLE001
        return router.error(400, "data_unavailable", str(exc))

    if not frames:
        return router.error(
            400,
            "no_datasets",
            "You don’t have any datasets available. Upload a file or attach a sample.",
        )

    result = orchestrator.run(
        question,
        frames,
        catalog=_safe_catalog(identity, set(frames)),
        model=_model_for(identity),
        mode=str(ctx.field("mode") or "light"),
        existing_config=ctx.field("config"),
        edit_request=str(ctx.field("edit") or ""),
    )

    if result.figure is None:
        return router.error(422, "no_chart", "; ".join(result.warnings) or "pipeline failed")

    # Accounting and identity serialisation happen around a chart that already
    # exists. Neither is allowed to turn a finished figure into a 500.
    _charge(identity)
    # The id goes back with the answer. Without it an API caller cannot
    # reconfigure, export or share the chart it just paid for - the streaming
    # path returned one and this one did not, which made the two endpoints
    # different products.
    chart_id = _persist_chart(identity, ctx, question, result)
    payload = {**result.to_payload(), "chart_id": chart_id}
    try:
        payload["user"] = identity.to_public()
    except Exception:  # noqa: BLE001
        log.exception("could not serialise the user")
    return router.json_result(payload)


@router.stream("/v1/query/stream")
def stream_query(stream: Any, ctx: router.Context) -> None:
    """The interactive path: emit reasoning as the pipeline produces it."""
    identity = ctx.user
    decision = ratelimit.check(
        identity, "chat_query", sample_data=bool(_sample_key(ctx))
    )
    if not decision.allowed:
        stream.emit(
            "error",
            {
                "code": decision.reason,
                "message": decision.detail,
                "upgrade": decision.upgrade,
                **({"quote": decision.quote} if decision.quote else {}),
            },
        )
        return

    question = str(ctx.field("q") or ctx.field("question") or "").strip()
    if not question:
        stream.emit("error", {"code": "missing_question"})
        return

    stream.emit("accepted", {"question": question, "at": time.time()})

    try:
        frames = _load_frames(identity, ctx, question)
    except DataSelectionError as exc:
        stream.emit(
            "error",
            {"code": exc.code, "message": str(exc), "candidates": exc.candidates},
        )
        return
    except Exception as exc:  # noqa: BLE001
        stream.emit("error", {"code": "data_unavailable", "message": str(exc)})
        return

    if not frames:
        stream.emit(
            "error",
            {
                "code": "no_datasets",
                "message": "You don’t have any datasets available. Upload a file or attach a sample.",
            },
        )
        return

    stream.emit(
        "datasets",
        {name: {"rows": int(len(frame))} for name, frame in frames.items()},
    )

    result = orchestrator.run(
        question,
        frames,
        catalog=_safe_catalog(identity, set(frames)),
        stream=stream,
        model=_model_for(identity),
        mode=str(ctx.field("mode") or "light"),
        existing_config=ctx.field("config"),
        edit_request=str(ctx.field("edit") or ""),
    )

    if result.figure is None:
        stream.emit(
            "error",
            {"code": "no_chart", "message": "; ".join(result.warnings) or "pipeline failed"},
        )
        return

    # The chart goes out before anything that can fail on the way to the wire.
    chart_id = _persist_chart(identity, ctx, question, result)
    stream.emit("result", {**result.to_payload(), "chart_id": chart_id})
    _charge(identity)
    try:
        stream.emit("user", identity.to_public())
    except Exception:  # noqa: BLE001
        log.exception("could not serialise the user")


def _charge(identity: Any) -> None:
    """Charge for a successful run without risking the run.

    Under-charging one query is cheaper than losing a chart the user waited a
    minute for, so an accounting failure is logged and swallowed.
    """
    try:
        ratelimit.consume(identity, "chat_query")
    except Exception:  # noqa: BLE001
        log.exception("could not charge for a successful query")


def _safe_catalog(
    identity: Any, available: set[str] | None = None
) -> list[dict[str, Any]]:
    """The catalog is context, not data. A broken source listing should cost a
    little discovery quality, not the whole chart."""
    try:
        return _catalog(identity, available)
    except Exception:  # noqa: BLE001
        log.exception("could not load the catalog")
        return []


def _model_for(identity: Any) -> str:
    """Paying callers get the stronger route; free queries stay on Luna."""
    return config.MODEL_ESCALATE if identity.paid else config.MODEL_DEFAULT


def _persist_chart(
    identity: Any, ctx: router.Context, question: str, result: Any
) -> str:
    # The trial has no account to hang a chart on. It still gets the chart -
    # it just cannot save, share or reconfigure it, which is most of what
    # signing in is for.
    if not getattr(identity, "signed_in", False):
        return ""

    chart_id = store.new_id()
    now = time.time()
    try:
        store.execute(
            "INSERT INTO charts (id, user_id, dashboard_id, title, query, source_id, "
            "spec, graph_args, trace, rows_json, cost_micros, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chart_id,
                identity.user_id,
                ctx.field("dashboard_id"),
                str(result.config.get("title") or question)[:200],
                question,
                ctx.field("source_id"),
                store.dump_json(result.figure),
                store.dump_json(result.config),
                store.dump_json(result.trace[-80:]),
                _keepable_rows(ctx, result),
                # What the run cost us. Prices are set from the distribution of
                # this column, so it is worth the four bytes.
                int(getattr(result, "cost_micros", 0) or 0),
                now,
                now,
            ),
        )
    except Exception:  # noqa: BLE001
        log.exception("could not persist chart")
    return chart_id


# Enough to rebuild any chart form, small enough that a chart row stays a row.
# Past this the data belongs in a dataset, which is re-queryable anyway.
MAX_KEPT_ROWS = 5000


def _keepable_rows(ctx: router.Context, result: Any) -> str | None:
    """Keep the data when there is nowhere else to get it from.

    A chart built from a connected source can always be rebuilt by re-running
    its query. One built from an upload or an inline payload cannot, and that
    is exactly the first-run path - so without this, "change the chart type for
    free" failed for every new user with a sample dataset.
    """
    if ctx.field("source_id"):
        return None
    frame = getattr(result, "frame", None)
    if frame is None or not hasattr(frame, "to_dict"):
        return None
    try:
        if len(frame) > MAX_KEPT_ROWS:
            return None
        return store.dump_json(frame.head(MAX_KEPT_ROWS).to_dict("records"))
    except Exception:  # noqa: BLE001 - never fail a chart over its own cache
        log.exception("could not keep the rows for a chart")
        return None


@router.post("/v1/chart/{chart_id}/config")
def update_chart_config(ctx: router.Context) -> router.Result:
    """Manual control: the user changes x / y / z / type without the model."""
    identity = auth.require(ctx)
    chart_id = ctx.params["chart_id"]

    if not teams.can_write(identity.user_id, "chart", chart_id):
        return router.error(404, "not_found")
    row = store.one("SELECT * FROM charts WHERE id = ?", (chart_id,))
    if row is None:
        return router.error(404, "not_found")

    updates = ctx.json
    if not isinstance(updates, dict):
        return router.error(400, "invalid_body")

    current = store.load_json(row["graph_args"], {}) or {}
    current.update({k: v for k, v in updates.items() if k != "data"})

    frame = _frame_for_chart(identity, row)
    if frame is None:
        return router.error(409, "data_unavailable", "Re-run the query to change this chart.")

    from twohelixes.charts import defaults as chart_defaults
    from twohelixes.pipeline import figures

    validated = figures.validate_config(current, frame)
    figure, warnings = figures.build(frame, validated, mode=str(ctx.field("mode") or "light"))
    figure = chart_defaults.apply(
        figure, mode=str(ctx.field("mode") or "light"), chart_type=validated["chart_type"]
    )

    # The name follows the chart. Changing the x channel changes what the chart
    # is about, and a saved chart still called "Revenue by region" while it
    # plots channels is wrong everywhere it is listed, not only on its tile.
    title = str(validated.get("title") or row["title"] or "Chart")[:200]
    store.execute(
        "UPDATE charts SET spec = ?, graph_args = ?, title = ?, updated_at = ?"
        " WHERE id = ?",
        (
            store.dump_json(figure),
            store.dump_json(validated),
            title,
            time.time(),
            chart_id,
        ),
    )

    return router.json_result(
        {
            "figure": figure,
            "config": validated,
            "warnings": warnings,
            "audit": chart_defaults.audit(figure),
            "columns": [str(c) for c in frame.columns],
        }
    )


@router.post("/v1/chart/{chart_id}/edit")
def edit_chart(ctx: router.Context) -> router.Result:
    """Change a saved chart in words, in place.

    A dashboard tile that is re-asked has to keep its identity: the layout, the
    share link and the refresh all point at this chart id. Running the ordinary
    query path instead would mint a second chart and leave the first orphaned in
    the grid, so the edit re-runs against this chart's own data and writes back
    over the same row.
    """
    identity = auth.require(ctx)
    chart_id = ctx.params["chart_id"]

    request = str(ctx.field("edit") or ctx.field("q") or "").strip()
    if not request:
        return router.error(400, "missing_edit")

    if not teams.can_write(identity.user_id, "chart", chart_id):
        return router.error(404, "not_found")
    row = store.one("SELECT * FROM charts WHERE id = ?", (chart_id,))
    if row is None:
        return router.error(404, "not_found")

    decision = ratelimit.check(identity, "chat_query")
    if not decision.allowed:
        return router.error(
            402, decision.reason or "out_of_allowance", decision.detail or ""
        )

    frame = _frame_for_chart(identity, row)
    if frame is None:
        return router.error(
            409, "data_unavailable", "This chart's data is gone. Ask the question again."
        )

    question = str(row["query"] or "").strip() or request
    result = orchestrator.run(
        question,
        {"data": frame},
        model=_model_for(identity),
        mode=str(ctx.field("mode") or "light"),
        existing_config=store.load_json(row["graph_args"], {}) or {},
        edit_request=request,
    )
    if result.figure is None:
        return router.error(422, "no_chart", "; ".join(result.warnings) or "edit failed")

    store.execute(
        "UPDATE charts SET title = ?, spec = ?, graph_args = ?, updated_at = ?"
        " WHERE id = ?",
        (
            str(result.config.get("title") or question)[:200],
            store.dump_json(result.figure),
            store.dump_json(result.config),
            time.time(),
            chart_id,
        ),
    )
    _charge(identity)
    return router.json_result(
        {
            "chart_id": chart_id,
            "title": str(result.config.get("title") or question)[:200],
            **result.to_payload(),
        }
    )


@router.delete("/v1/chart/{chart_id}")
def delete_chart(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    chart_id = ctx.params["chart_id"]
    if not teams.can_write(identity.user_id, "chart", chart_id):
        return router.error(404, "not_found")
    store.execute("DELETE FROM charts WHERE id = ?", (chart_id,))
    store.execute(
        "DELETE FROM team_objects WHERE kind = 'chart' AND object_id = ?", (chart_id,)
    )
    return router.json_result({"deleted": True})


def _frame_for_chart(identity: Any, row: Any) -> Any:
    """Re-materialise the data a saved chart was built from.

    Prefer the live source - a chart on a warehouse should reconfigure against
    today's rows, not a snapshot - and fall back to the rows kept with the
    chart, which is the only option for an upload or an inline payload.
    """
    source_id = row["source_id"]
    query = row["query"]
    if source_id and query:
        try:
            # Access to the chart has already been checked. A team editor must
            # re-run the chart through the owner's connection; looking it up
            # as the editor made every shared warehouse chart non-editable.
            connector = registry.for_source(source_id, str(row["user_id"]))
            return connector.frame(query)
        except Exception:  # noqa: BLE001
            log.exception("could not re-query the source for a chart")

    kept = store.load_json(_column(row, "rows_json"), None)
    if kept:
        import pandas as pd

        return pd.DataFrame(kept)

    # A chart from the builder keeps a descriptor and its steps rather than a
    # copy of the rows, so it can be rebuilt at any size and against whatever
    # the dataset says today.
    plan = store.load_json(_column(row, "trace"), None)
    if isinstance(plan, dict) and plan.get("source"):
        from twohelixes.pipeline import transform
        from twohelixes.routes import builder as builder_routes

        try:
            frame = builder_routes.frame_from_source(identity, plan["source"])
            shaped, _notes = transform.apply(frame, transform.normalise(plan.get("steps") or []))
            return shaped
        except Exception:  # noqa: BLE001
            log.exception("could not rebuild a built chart from its plan")
    return None


def _column(row: Any, name: str) -> Any:
    """Read a column that may predate the migration that added it."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return None


@router.get("/v1/chart/{chart_id}")
def get_chart(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    chart_id = ctx.params["chart_id"]
    if not teams.can_read(identity.user_id, "chart", chart_id):
        return router.error(404, "not_found")
    row = store.one("SELECT * FROM charts WHERE id = ?", (chart_id,))
    if row is None:
        return router.error(404, "not_found")

    data = store.row_to_dict(row) or {}
    data["spec"] = store.load_json(data.get("spec"), {})
    data["graph_args"] = store.load_json(data.get("graph_args"), {})
    data["trace"] = store.load_json(data.get("trace"), [])
    # The kept rows can be five thousand records; the thing a caller actually
    # wants from them is the column list, which is what the manual controls
    # offer. Send the names, not the table.
    kept = store.load_json(data.pop("rows_json", None), None)
    if isinstance(kept, list) and kept:
        data["columns"] = list(kept[0].keys())
    else:
        # A chart on a connected source keeps no rows, and the manual controls
        # are unusable without column names. One query for the names is the
        # price of offering them at all.
        frame = _frame_for_chart(identity, row)
        data["columns"] = [] if frame is None else [str(c) for c in frame.columns]
    return router.json_result(data)


@router.get("/v1/me")
def me(ctx: router.Context) -> router.Result:
    identity = ctx.user
    if identity is None:
        return router.json_result({"signed_in": False})
    token = ctx.cookie(auth.COOKIE_NAME)
    if identity.signed_in and token and auth.renew_session(token):
        # Browsers cap persistent cookies (Chrome currently caps at roughly
        # 400 days), so re-issuing on app boot is what makes "remember me"
        # durable in practice instead of silently ending at that cap.
        return router.Result(
            body=identity.to_public(),
            headers={
                "Set-Cookie": auth.cookie_header(token, secure=not config.is_dev())
            },
        )
    return router.json_result(identity.to_public())
