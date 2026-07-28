"""Public dataset pages: the product, working, before anyone signs in.

Every account gets these datasets, but they are also the best answer to "what
does this thing actually do?" - so each one gets a page with its schema, its
rows, and the charts our own pipeline draws from it, with the reasoning trace
one click away. Nothing here is a mockup: `datasets/examples.py` builds each
figure through `figures.build` -> `defaults.apply` -> the SVG exporter, the
same three steps a customer's question goes through.

They are also the pages a search engine can read. A chart in a canvas is
invisible; these are inline SVG with real text in them, plus schema.org
`Dataset` markup, which is what puts them in Google's dataset index rather
than just its web index.

The API mirrors every page (`/v1/samples/catalog`), because a page that a person can
read and a machine cannot is half a feature.
"""

from __future__ import annotations

import html
import json
import logging
from typing import Any

from twohelixes import config, router
from twohelixes.datasets import examples, samples
from twohelixes.routes.pages import _page

log = logging.getLogger("twohelixes.routes.datasets")

PREVIEW_ROWS = 8
MAX_CSV_ROWS = 20_000

CHART_SIZE = (560, 360)
LEAD_SIZE = (420, 260)


# --------------------------------------------------------------------------
# Shape helpers
# --------------------------------------------------------------------------


def _type_name(dtype: Any) -> str:
    kind = getattr(dtype, "kind", "O")
    if kind == "M":
        return "date"
    if kind in "ifu":
        return "number"
    if kind == "b":
        return "boolean"
    return "text"


def _schema(frame: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for name in frame.columns:
        column = frame[name]
        example = ""
        non_null = column.dropna()
        if len(non_null):
            example = str(examples._scalar(non_null.iloc[0]))
        rows.append(
            {
                "name": str(name),
                "type": _type_name(column.dtype),
                "example": example[:40],
            }
        )
    return rows


def _catalogue_entry(key: str) -> dict[str, Any] | None:
    sample = samples.BY_KEY.get(key)
    if sample is None:
        return None
    samples.materialise()
    frame = samples.frame(key)
    entry = sample.to_dict(rows=int(len(frame)), columns=[str(c) for c in frame.columns])
    entry["path"] = f"/datasets/{key}"
    entry["schema"] = _schema(frame)
    entry["examples"] = [
        {
            "slug": e.slug,
            "question": e.question,
            "headline": e.headline,
            "path": e.path,
        }
        for e in examples.BY_DATASET.get(key, [])
    ]
    return entry


# --------------------------------------------------------------------------
# Markup helpers
# --------------------------------------------------------------------------


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = ""
    for row in rows:
        cells = ""
        for column in columns:
            value = row.get(column, "")
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            cells += f'<td class="{"num" if numeric else ""}">{_esc(value)}</td>'
        body += f"<tr>{cells}</tr>"
    return (
        f'<div class="scroll-x"><table class="data"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _trace_markup(trace: list[dict[str, Any]], open_by_default: bool = False) -> str:
    """The reasoning trace, collapsed.

    Open on the single-example page and closed everywhere else: on a page with
    three examples, three expanded traces bury the charts they explain.
    """
    steps = "".join(
        f'<div class="tstep"><div class="top"><i class="dot"></i>'
        f'<span class="nm">{_esc(step["name"])}</span>'
        f'<span class="ms">{int(step["ms"])} ms</span></div>'
        f'<p class="said">{_esc(step["said"])}</p></div>'
        for step in trace
    )
    return (
        f'<details class="trace"{" open" if open_by_default else ""}>'
        "<summary>How it decided</summary>"
        f'<div class="body">{steps}</div></details>'
    )


def _ask_link(dataset: str, question: str) -> str:
    from urllib.parse import quote

    return f"/app?sample={quote(dataset)}&q={quote(question)}"


def _example_block(rendered: examples.Rendered, heading: str = "h3") -> str:
    example = rendered.example
    return f"""
<article class="example">
  <{heading}>{_esc(example.headline)}</{heading}>
  <p>{_esc(example.finding)}</p>
  <figure class="figure">{rendered.markup}
    <figcaption><b>{_esc(example.question)}</b>
    &mdash; {rendered.row_count:,} rows charted from {rendered.source_rows:,},
    drawn as a {_esc(rendered.chart_type)}</figcaption></figure>
  <div class="example-actions">
    <a class="btn btn-primary btn-small"
       href="{_esc(_ask_link(example.dataset, example.question))}">Ask this yourself</a>
    <a class="btn btn-ghost btn-small" href="{_esc(example.path)}">See the working</a>
  </div>
  {_trace_markup(rendered.trace)}
</article>"""


def _mode(ctx: router.Context) -> str:
    return "dark" if (ctx.q("mode", "light") or "light") == "dark" else "light"


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@router.get("/datasets")
def index(ctx: router.Context) -> router.Result:
    mode = _mode(ctx)
    site = config.site_url().rstrip("/")

    cards = ""
    items: list[dict[str, Any]] = []
    for position, sample in enumerate(samples.SAMPLES, start=1):
        entry = _catalogue_entry(sample.key) or {}
        lead = examples.lead(sample.key)
        markup = ""
        if lead is not None:
            rendered = examples.render(sample.key, lead.slug, mode, *LEAD_SIZE)
            markup = rendered.markup if rendered else ""

        questions = "".join(
            f'<a href="{_esc(e["path"])}">{_esc(e["question"])}</a>'
            for e in entry.get("examples", [])
        )
        cards += f"""
<div class="ds-card">
  <h3><a href="/datasets/{_esc(sample.key)}">{_esc(sample.name)}</a></h3>
  <p>{_esc(sample.description)}</p>
  <div class="ds-meta"><span>{entry.get("rows", 0):,} rows</span>
    <span>{len(entry.get("columns", []))} columns</span>
    <span>{_esc(sample.source)}</span></div>
  <figure class="figure">{markup}</figure>
  <div class="ds-questions">{questions}</div>
</div>"""

        items.append(
            {
                "@type": "ListItem",
                "position": position,
                "url": f"{site}/datasets/{sample.key}",
                "name": sample.name,
            }
        )

    structured = {
        "@context": "https://schema.org",
        "@type": "DataCatalog",
        "name": "twoHelixes sample datasets",
        "url": f"{site}/datasets",
        "description": (
            "Open and business-shaped sample datasets, each with worked example "
            "charts and the reasoning behind them."
        ),
        "dataset": [
            {
                "@type": "Dataset",
                "name": s.name,
                "description": s.description,
                "url": f"{site}/datasets/{s.key}",
            }
            for s in samples.SAMPLES
        ],
        "mainEntity": {"@type": "ItemList", "itemListElement": items},
    }

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker">Datasets</p>
  <h1>Datasets you can ask questions of right now</h1>
  <p class="sub">Nine datasets are loaded into every account &mdash; four open
  reference sets everyone benchmarks against, five generated to have the shapes
  real business data has: seasonality, a long tail, a funnel, a cohort. Every
  chart below was drawn by the live pipeline from the real rows, and every one
  shows its reasoning.</p>
</div></section>

<section><div class="shell">
  <div class="ds-grid">{cards}</div>
</div></section>

<section class="band"><div class="shell">
  <h2>Or point it at your own data</h2>
  <p class="lead-in">A spreadsheet, a warehouse, an API. The datasets here are
  a starting point, not the product &mdash; the pipeline that drew these charts
  is the one that answers questions about your data.</p>
  <div class="actions"><a class="btn btn-primary" href="/app"
    data-action="signin">Start free</a>
  <a class="btn btn-ghost" href="/features">See the features</a></div>
</div></section>
</main>"""

    return router.html(
        _page(
            "Sample datasets with worked example charts — twoHelixes",
            "Nine sample datasets with schemas, rows, and example charts drawn "
            "by the live pipeline with their reasoning traces attached.",
            body,
            "/datasets",
            head=_jsonld(structured),
        )
    )


@router.get("/datasets/{key}")
def detail(ctx: router.Context) -> router.Result:
    key = ctx.params["key"]
    sample = samples.BY_KEY.get(key)
    if sample is None:
        return _not_found()

    mode = _mode(ctx)
    site = config.site_url().rstrip("/")
    samples.materialise()
    frame = samples.frame(key)
    schema = _schema(frame)
    preview = examples._preview(frame)

    blocks = ""
    for example in examples.BY_DATASET.get(key, []):
        rendered = examples.render(key, example.slug, mode, *CHART_SIZE)
        if rendered:
            blocks += _example_block(rendered)

    schema_rows = "".join(
        f'<tr><td>{_esc(c["name"])}</td><td>{_esc(c["type"])}</td>'
        f'<td>{_esc(c["example"])}</td></tr>'
        for c in schema
    )

    structured = _dataset_jsonld(sample, frame, site)

    # A dataset-level notebook is the lead example's: a notebook that loads a
    # file and charts nothing is not a starting point, it is homework.
    lead = examples.lead(key)
    notebook_href = (
        f"/v1/samples/{key}/{lead.slug}/notebook?format=ipynb" if lead else ""
    )
    notebook_link = (
        f'<a class="btn btn-ghost" href="{_esc(notebook_href)}">Jupyter notebook</a>'
        if notebook_href
        else ""
    )

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker"><a href="/datasets">Datasets</a> / {_esc(sample.source)}</p>
  <h1>{_esc(sample.name)}</h1>
  <p class="sub">{_esc(sample.description)}</p>
  <div class="actions" style="margin-top:var(--s4)">
    <a class="btn btn-primary" href="{_esc(_ask_link(key, sample.questions[0]
        if sample.questions else ""))}">Ask a question of it</a>
    <a class="btn btn-ghost" href="/v1/samples/{_esc(key)}/download.csv">
      Download CSV</a>
    {notebook_link}
  </div>
  <div class="ds-meta" style="margin-top:var(--s4)">
    <span>{len(frame):,} rows</span><span>{len(frame.columns)} columns</span>
    <span>{_esc(sample.source)}</span></div>
</div></section>

<section><div class="shell">
  <h2>Worked examples</h2>
  <p class="lead-in">Each of these is a real run: the pipeline picked the
  columns, shaped the rows and chose the form. Open the trace to see what it
  decided and why.</p>
  {blocks}
</div></section>

<section class="band"><div class="shell">
  <h2>Schema</h2>
  <p class="lead-in">{len(frame.columns)} columns, typed as the pipeline sees
  them &mdash; which is how it knows what can go on a time axis and what can be
  summed.</p>
  <div class="scroll-x"><table class="data"><thead><tr><th>Column</th>
    <th>Type</th><th>Example</th></tr></thead>
    <tbody>{schema_rows}</tbody></table></div>
</div></section>

<section><div class="shell">
  <h2>First {len(preview)} rows</h2>
  {_table([str(c) for c in frame.columns], preview)}
</div></section>

<section class="band"><div class="shell">
  <h2>Ask it something else</h2>
  <div class="chips">{"".join(
      f'<a class="chip" href="{_esc(_ask_link(key, q))}">{_esc(q)}</a>'
      for q in sample.questions)}</div>
  <div class="actions" style="margin-top:var(--s5)">
    <a class="btn btn-primary" href="/app" data-action="signin">Start free</a>
  </div>
</div></section>
</main>"""

    return router.html(
        _page(
            f"{sample.name} dataset — schema, rows and example charts — twoHelixes",
            f"{sample.description} {len(frame):,} rows, {len(frame.columns)} "
            "columns, with example charts and the reasoning behind each one.",
            body,
            f"/datasets/{key}",
            head=_jsonld(structured),
        )
    )


@router.get("/datasets/{key}/{slug}")
def example_page(ctx: router.Context) -> router.Result:
    key, slug = ctx.params["key"], ctx.params["slug"]
    sample = samples.BY_KEY.get(key)
    example = examples.BY_PATH.get((key, slug))
    if sample is None or example is None:
        return _not_found()

    mode = _mode(ctx)
    site = config.site_url().rstrip("/")
    rendered = examples.render(key, slug, mode, 760, 440)
    if rendered is None:
        return _not_found()

    related = "".join(
        f'<a class="chip" href="{_esc(other.path)}">{_esc(other.question)}</a>'
        for other in examples.BY_DATASET.get(key, [])
        if other.slug != slug
    )

    structured = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"@type": "ListItem", "position": 1, "name": "Datasets",
                     "item": f"{site}/datasets"},
                    {"@type": "ListItem", "position": 2, "name": sample.name,
                     "item": f"{site}/datasets/{key}"},
                    {"@type": "ListItem", "position": 3, "name": example.headline,
                     "item": f"{site}{example.path}"},
                ],
            },
            {
                "@type": "Question",
                "name": example.question,
                "url": f"{site}{example.path}",
                "acceptedAnswer": {
                    "@type": "Answer",
                    "text": f"{example.headline}. {example.finding} {example.reason}",
                },
            },
        ],
    }

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker"><a href="/datasets">Datasets</a> /
    <a href="/datasets/{_esc(key)}">{_esc(sample.name)}</a></p>
  <h1>{_esc(example.headline)}</h1>
  <p class="sub">{_esc(example.finding)}</p>
</div></section>

<section><div class="shell split top">
  <div>
    <div class="askbox" style="background:var(--panel-2);border:1px solid
      var(--border);border-radius:var(--r-md);padding:.7rem .85rem;
      margin-bottom:var(--s4)">
      <span style="display:block;font-size:.7rem;text-transform:uppercase;
        letter-spacing:.06em;color:var(--text-muted)">Question</span>
      {_esc(example.question)}</div>
    <figure class="figure">{rendered.markup}
      <figcaption><b>{_esc(sample.name)}</b> &mdash;
      {rendered.row_count:,} rows charted from {rendered.source_rows:,},
      drawn as a {_esc(rendered.chart_type)}</figcaption></figure>
    <div class="example-actions">
      <a class="btn btn-primary btn-small"
         href="{_esc(_ask_link(key, example.question))}">Ask this yourself</a>
      <a class="btn btn-ghost btn-small"
         href="/v1/samples/{_esc(key)}/{_esc(slug)}/notebook?format=ipynb">
         Jupyter notebook</a>
      <a class="btn btn-ghost btn-small"
         href="/v1/samples/{_esc(key)}/{_esc(slug)}/notebook">marimo notebook</a>
      <a class="btn btn-ghost btn-small"
         href="/v1/samples/{_esc(key)}/{_esc(slug)}/chart.svg">SVG</a>
    </div>
  </div>
  <div>{_trace_markup(rendered.trace, open_by_default=True)}</div>
</div></section>

<section class="band"><div class="shell">
  <h2>The transformation</h2>
  <p class="lead-in">This is the code, not a description of it &mdash; the
  chart above was drawn from what it returns, and the notebook download runs
  the same lines against the same file.</p>
  <div class="codeblock" style="margin:0">
    <pre><code>{_esc(example.transform)}</code></pre>
    <button class="copy" data-copy type="button">Copy</button></div>
</div></section>

<section><div class="shell">
  <h2>What it charted</h2>
  <p class="lead-in">{rendered.row_count:,} rows out of
  {rendered.source_rows:,}, {len(rendered.columns)} columns. First
  {min(len(rendered.rows), PREVIEW_ROWS)} shown.</p>
  {_table(rendered.columns, rendered.rows)}
</div></section>

<section class="band"><div class="shell">
  <h2>More from {_esc(sample.name)}</h2>
  <div class="chips">{related}</div>
  <div class="actions" style="margin-top:var(--s5)">
    <a class="btn btn-primary" href="/app" data-action="signin">
      Ask your own data</a>
    <a class="btn btn-ghost" href="/datasets/{_esc(key)}">
      Back to {_esc(sample.name)}</a></div>
</div></section>
</main>"""

    return router.html(
        _page(
            f"{example.headline} — {sample.name} — twoHelixes",
            f"{example.question} {example.finding}",
            body,
            example.path,
            head=_jsonld(structured),
        )
    )


def _not_found() -> router.Result:
    body = """
<main id="content"><section class="page-head"><div class="shell">
  <h1>No such dataset</h1>
  <p class="sub">It may have been renamed. The full list is on the
  <a href="/datasets">datasets page</a>.</p>
</div></section></main>"""
    return router.html(
        _page("Not found — twoHelixes", "", body, "/datasets",
              head='<meta name="robots" content="noindex">'),
        status=404,
    )


def _jsonld(payload: dict[str, Any]) -> str:
    return (
        '<script type="application/ld+json">'
        + json.dumps(payload, separators=(",", ":"))
        + "</script>"
    )


def _dataset_jsonld(sample: Any, frame: Any, site: str) -> dict[str, Any]:
    """schema.org Dataset, which is what a dataset search indexes on.

    `distribution` is the part that matters and the part usually left off: a
    dataset with no downloadable distribution is not a dataset as far as the
    crawler is concerned, it is a page about one.
    """
    return {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": sample.name,
        "description": sample.description,
        "url": f"{site}/datasets/{sample.key}",
        "identifier": sample.key,
        "isAccessibleForFree": True,
        "license": "https://creativecommons.org/publicdomain/zero/1.0/",
        "creator": {"@type": "Organization", "name": "twoHelixes", "url": site},
        "variableMeasured": [str(c) for c in frame.columns],
        "distribution": [
            {
                "@type": "DataDownload",
                "encodingFormat": "text/csv",
                "contentUrl": f"{site}/v1/samples/{sample.key}/download.csv",
            }
        ],
    }


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


@router.get("/v1/samples/catalog")
def list_datasets(ctx: router.Context) -> router.Result:
    """Everything the pages show, as JSON. Public: the data is public."""
    entries = [e for e in (_catalogue_entry(s.key) for s in samples.SAMPLES) if e]
    return router.json_result({"datasets": entries})


@router.get("/v1/samples/{key}/dataset")
def get_dataset(ctx: router.Context) -> router.Result:
    entry = _catalogue_entry(ctx.params["key"])
    if entry is None:
        return router.error(404, "unknown_dataset")
    return router.json_result(entry)


@router.get("/v1/samples/{key}/download.csv")
def download(ctx: router.Context) -> router.Result:
    key = ctx.params["key"]
    if key not in samples.BY_KEY:
        return router.error(404, "unknown_dataset")
    samples.materialise()
    frame = samples.frame(key)
    truncated = len(frame) > MAX_CSV_ROWS
    body = frame.head(MAX_CSV_ROWS).to_csv(index=False)
    headers = {"Content-Disposition": f'attachment; filename="{key}.csv"'}
    if truncated:
        # Saying so in a header beats silently handing back a different
        # dataset to anyone scripting against this.
        headers["X-Rows-Truncated"] = str(MAX_CSV_ROWS)
    return router.Result(
        status=200, body=body, content_type="text/csv; charset=utf-8", headers=headers
    )


@router.get("/v1/samples/{key}/{slug}/chart.svg")
def example_svg(ctx: router.Context) -> router.Result:
    key, slug = ctx.params["key"], ctx.params["slug"]
    rendered = examples.render(key, slug, _mode(ctx), *CHART_SIZE)
    if rendered is None or not rendered.markup:
        return router.error(404, "unknown_example")
    return router.Result(
        status=200,
        body=rendered.markup,
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/v1/samples/{key}/{slug}/notebook")
def example_notebook(ctx: router.Context) -> router.Result:
    """The example as a notebook, without an account.

    Deliberately unauthenticated: the whole argument of these pages is that the
    work is yours to take, and asking for an email first would contradict it.
    """
    key, slug = ctx.params["key"], ctx.params["slug"]
    sample = samples.BY_KEY.get(key)
    example = examples.BY_PATH.get((key, slug))
    if sample is None or example is None:
        return router.error(404, "unknown_example")

    rendered = examples.render(key, slug)
    from twohelixes.notebooks import ipynb_export, marimo_export

    wanted = (ctx.q("format", "") or "").lower()
    module, extension, content_type = (
        (ipynb_export, "ipynb", "application/x-ipynb+json; charset=utf-8")
        if wanted in ("ipynb", "jupyter", "notebook")
        else (marimo_export, "py", "text/x-python; charset=utf-8")
    )

    samples.materialise()
    source = module.build(
        marimo_export.NotebookSpec(
            title=example.headline,
            question=example.question,
            dataset_path=str(samples.path_for(key)),
            dataset_name=sample.name,
            transform_code=example.transform,
            chart_config=dict(example.config),
            trace=list(rendered.trace) if rendered else [],
        )
    )
    return router.Result(
        status=200,
        body=source,
        content_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{key}-{slug}.{extension}"'
        },
    )


@router.get("/v1/samples/{key}/{slug}/example")
def example_json(ctx: router.Context) -> router.Result:
    rendered = examples.render(ctx.params["key"], ctx.params["slug"], _mode(ctx))
    if rendered is None:
        return router.error(404, "unknown_example")
    example = rendered.example
    return router.json_result(
        {
            "dataset": example.dataset,
            "slug": example.slug,
            "question": example.question,
            "headline": example.headline,
            "finding": example.finding,
            "transform": example.transform,
            "config": example.config,
            "chart_type": rendered.chart_type,
            "figure": rendered.figure,
            "trace": rendered.trace,
            "columns": rendered.columns,
            "rows": rendered.rows,
            "row_count": rendered.row_count,
            "source_rows": rendered.source_rows,
            "warnings": rendered.warnings,
        }
    )
