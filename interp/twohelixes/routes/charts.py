"""Export and the brand mark endpoints."""

from __future__ import annotations

import base64

from twohelixes import auth, router, store
from twohelixes.charts import helix, palette, svg


@router.get("/v1/chart/{chart_id}/export")
def export_chart(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = store.one(
        "SELECT spec, title FROM charts WHERE id = ? AND user_id = ?",
        (ctx.params["chart_id"], identity.user_id),
    )
    if row is None:
        return router.error(404, "not_found")

    figure = store.load_json(row["spec"], {})
    fmt = (ctx.q("format") or "svg").lower()
    if fmt not in ("svg", "png", "json", "csv"):
        return router.error(400, "unsupported_format")

    if fmt == "json":
        return router.json_result(figure)

    if fmt == "csv":
        name = _filename(str(row["title"]))
        return router.Result(
            status=200,
            body=figure_csv(figure),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}.csv"'},
        )

    try:
        payload, content_type = svg.export(
            figure,
            fmt=fmt,
            width=ctx.q_int("width", 900),
            height=ctx.q_int("height", 520),
            mode=ctx.q("mode", "light") or "light",
        )
    except Exception as exc:  # noqa: BLE001
        return router.error(500, "export_failed", str(exc))

    disposition = f'attachment; filename="{_filename(str(row["title"]))}.{fmt}"'

    if fmt == "png":
        # PNG crosses the Mojo boundary as a string, so it travels base64.
        return router.Result(
            status=200,
            body={"format": "png", "base64": base64.b64encode(payload).decode()},
        )

    return router.Result(
        status=200,
        body=payload.decode(),
        content_type=content_type,
        headers={"Content-Disposition": disposition},
    )


def _filename(title: str) -> str:
    cleaned = "".join(c for c in title if c.isalnum() or c in " -_").strip()
    return cleaned or "chart"


def figure_csv(figure: dict) -> str:
    """Every value the figure plots, as one CSV.

    The stored figure is the full charted series, not the 50-row preview the
    UI shows, so this is the honest "give me the data behind this chart"
    answer without re-running the query. Traces are emitted one after another
    with a series column, because two traces rarely share an x axis exactly and
    aligning them would invent rows that were never plotted.
    """
    import csv
    import io

    plotted = ("x", "y", "z", "labels", "values", "lat", "lon", "locations", "text")
    traces = [
        (
            str(trace.get("name") or f"series_{index + 1}"),
            {k: v for k, v in trace.items() if k in plotted and isinstance(v, list)},
        )
        for index, trace in enumerate(figure.get("data") or [])
        if isinstance(trace, dict)
    ]
    traces = [(name, channels) for name, channels in traces if channels]
    if not traces:
        return ""

    # One header for the union of channels, so a figure mixing forms - a bar
    # with a line over it - still reads as one table.
    columns = [key for key in plotted if any(key in ch for _, ch in traces)]

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["series", *columns])
    for name, channels in traces:
        rows = max(len(v) for v in channels.values())
        for row in range(rows):
            writer.writerow(
                [name, *(_at(channels.get(key) or [], row) for key in columns)]
            )

    return out.getvalue()


def _at(values: list, index: int) -> object:
    return values[index] if index < len(values) else ""


@router.post("/v1/export")
def export_inline(ctx: router.Context) -> router.Result:
    """Export a figure the client already holds, without saving it first."""
    figure = ctx.field("figure")
    if not isinstance(figure, dict):
        return router.error(400, "missing_figure")

    try:
        payload, content_type = svg.export(
            figure,
            fmt=str(ctx.field("format") or "svg"),
            width=int(ctx.field("width") or 900),
            height=int(ctx.field("height") or 520),
            mode=str(ctx.field("mode") or "light"),
        )
    except Exception as exc:  # noqa: BLE001
        return router.error(500, "export_failed", str(exc))

    if content_type == "image/png":
        return router.json_result(
            {"format": "png", "base64": base64.b64encode(payload).decode()}
        )
    return router.Result(status=200, body=payload.decode(), content_type=content_type)


@router.get("/brand/helix.svg")
def brand_logo(ctx: router.Context) -> router.Result:
    return router.Result(
        status=200,
        body=helix.logo(ctx.q_int("size", 64), ctx.q("mode", "light") or "light"),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/brand/spinner.svg")
def brand_spinner(ctx: router.Context) -> router.Result:
    return router.Result(
        status=200,
        body=helix.spinner(ctx.q_int("size", 48), ctx.q("mode", "light") or "light"),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/favicon.svg")
def favicon(ctx: router.Context) -> router.Result:
    return router.Result(
        status=200,
        body=helix.favicon(),
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@router.get("/v1/theme")
def theme(ctx: router.Context) -> router.Result:
    """The validated palette, so the client never invents a hue."""
    return router.json_result(
        {
            "light": palette.as_css_variables("light"),
            "dark": palette.as_css_variables("dark"),
            "categorical": {
                "light": list(palette.CATEGORICAL_LIGHT),
                "dark": list(palette.CATEGORICAL_DARK),
            },
            "max_series": palette.MAX_SERIES,
            "all_pairs_max_series": palette.ALL_PAIRS_MAX_SERIES,
            "other_label": palette.OTHER_LABEL,
        }
    )
