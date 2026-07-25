"""Server-rendered pages: marketing, the app shell, sharing, and sign-in.

Marketing pages are rendered in Mojo's worker on demand rather than built by a
static generator, because they carry live values - the free-query allowance and
the credit pack prices come from config, so a pricing change is one constant.
"""

from __future__ import annotations

import html
import time
from typing import Any

from twohelixes import auth, config, router, store
from twohelixes.charts import helix, palette
from twohelixes.routes import showcase
from twohelixes.routes.billing import CREDIT_PACKS, PLANS

NAV = (
    ("/", "Home"),
    ("/features", "Features"),
    ("/pricing", "Pricing"),
    ("/docs", "Docs"),
)


def _css() -> str:
    light = palette.as_css_variables("light")
    dark = palette.as_css_variables("dark")
    light_vars = ";".join(f"{k}:{v}" for k, v in light.items())
    dark_vars = ";".join(f"{k}:{v}" for k, v in dark.items())

    return f"""
*,*::before,*::after{{box-sizing:border-box}}
body,h1,h2,h3,p,figure,ul{{margin:0}}
ul{{padding:0;list-style:none}}
img,svg{{max-width:100%;display:block}}
:root{{color-scheme:light;{light_vars};
  --radius:14px;--shell:min(1120px,100% - 2.5rem);
  --font:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --panel:#ffffff;--border:#e8e7e3}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{color-scheme:dark;{dark_vars};
    --panel:#212120;--border:#2e2e2c}}
}}
:root[data-theme="dark"]{{color-scheme:dark;{dark_vars};--panel:#212120;--border:#2e2e2c}}
body{{margin:0;font-family:var(--font);background:var(--surface-1);
  color:var(--text-primary);line-height:1.55;-webkit-font-smoothing:antialiased}}
.shell{{width:var(--shell);margin-inline:auto}}
a{{color:var(--series-1);text-decoration:none}}
a:hover{{text-decoration:underline}}
header.site{{position:sticky;top:0;z-index:20;backdrop-filter:blur(8px);
  background:color-mix(in srgb,var(--surface-1) 88%,transparent);
  border-bottom:1px solid var(--border)}}
header.site .shell{{display:flex;align-items:center;gap:1.25rem;
  min-height:64px;flex-wrap:wrap}}
.brand{{display:flex;align-items:center;gap:.6rem;font-weight:650;
  color:var(--text-primary);font-size:1.05rem}}
.brand svg{{width:30px;height:30px}}
nav.main{{display:flex;gap:1.1rem;margin-left:auto;flex-wrap:wrap}}
nav.main a{{color:var(--text-secondary);font-size:.94rem}}
nav.main a[aria-current]{{color:var(--text-primary);font-weight:600}}
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:.5rem;
  padding:.62rem 1.05rem;border-radius:10px;font-weight:600;font-size:.94rem;
  border:1px solid transparent;cursor:pointer;text-decoration:none}}
.btn:hover{{text-decoration:none}}
.btn-primary{{background:var(--series-1);color:#fff}}
.btn-ghost{{border-color:var(--border);color:var(--text-primary);background:transparent}}
.hero{{padding:clamp(3rem,8vw,6rem) 0 clamp(2rem,5vw,3.5rem)}}
.hero h1{{font-size:clamp(2.1rem,5.2vw,3.5rem);line-height:1.08;
  letter-spacing:-.022em;max-width:16ch;font-weight:680}}
.hero p.lede{{margin-top:1.1rem;font-size:clamp(1.02rem,2.1vw,1.2rem);
  color:var(--text-secondary);max-width:56ch}}
.hero .actions{{margin-top:1.9rem;display:flex;gap:.75rem;flex-wrap:wrap}}
.hero-layout{{display:grid;gap:2.5rem;grid-template-columns:1fr;align-items:center}}
@media(min-width:900px){{.hero-layout{{grid-template-columns:1.15fr .85fr}}}}
.helix-hero{{display:flex;justify-content:center}}
.helix-hero svg{{width:min(260px,60vw);height:auto}}
.grid{{display:grid;gap:1.1rem;grid-template-columns:1fr}}
@media(min-width:640px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(min-width:980px){{.grid.three{{grid-template-columns:repeat(3,1fr)}}}}
.card{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.35rem}}
.card h3{{font-size:1.02rem;margin-bottom:.45rem;font-weight:640}}
.card p{{color:var(--text-secondary);font-size:.94rem}}
section{{padding:clamp(2.5rem,6vw,4rem) 0}}
section h2{{font-size:clamp(1.5rem,3.4vw,2.1rem);letter-spacing:-.015em;
  margin-bottom:.6rem;font-weight:660}}
section p.sub{{color:var(--text-secondary);margin-bottom:1.9rem;max-width:60ch}}
.steps{{counter-reset:step}}
.steps .card{{position:relative;padding-left:3.4rem}}
.steps .card::before{{counter-increment:step;content:counter(step);
  position:absolute;left:1.3rem;top:1.35rem;width:26px;height:26px;
  border-radius:50%;background:var(--series-1);color:#fff;font-size:.82rem;
  display:grid;place-items:center;font-weight:700}}
.price-grid{{display:grid;gap:1.1rem;grid-template-columns:1fr}}
@media(min-width:860px){{.price-grid{{grid-template-columns:repeat(3,1fr)}}}}
.plan{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.6rem;display:flex;flex-direction:column}}
.plan.highlight{{border-color:var(--series-1);box-shadow:0 0 0 1px var(--series-1)}}
.plan .amount{{font-size:2.1rem;font-weight:680;letter-spacing:-.02em}}
.plan .cadence{{color:var(--text-muted);font-size:.88rem}}
.plan ul{{margin:1.15rem 0;display:grid;gap:.55rem}}
.plan li{{font-size:.93rem;color:var(--text-secondary);padding-left:1.4rem;
  position:relative}}
.plan li::before{{content:"";position:absolute;left:0;top:.52em;width:8px;
  height:8px;border-radius:2px;background:var(--series-3)}}
.plan .btn{{margin-top:auto}}
table.packs{{width:100%;border-collapse:collapse;margin-top:1rem}}
table.packs th,table.packs td{{text-align:left;padding:.7rem .8rem;
  border-bottom:1px solid var(--border);font-size:.93rem}}
table.packs th{{color:var(--text-muted);font-weight:600;font-size:.82rem;
  text-transform:uppercase;letter-spacing:.04em}}
.scroll-x{{overflow-x:auto}}
code,pre{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88rem}}
pre{{background:var(--panel);border:1px solid var(--border);
  border-radius:10px;padding:1rem;overflow-x:auto}}
footer.site{{border-top:1px solid var(--border);padding:2.5rem 0;
  color:var(--text-muted);font-size:.9rem}}
footer.site .shell{{display:flex;gap:1.5rem;flex-wrap:wrap;
  justify-content:space-between}}
.badge{{display:inline-block;padding:.28rem .6rem;border-radius:999px;
  background:color-mix(in srgb,var(--series-3) 16%,transparent);
  color:var(--series-3);font-size:.78rem;font-weight:650}}
.eyebrow{{display:inline-flex;align-items:center;gap:.45rem;padding:.3rem .7rem;
  border-radius:999px;background:color-mix(in srgb,var(--series-1) 12%,transparent);
  color:var(--series-1);font-size:.79rem;font-weight:650}}
.hero h1{{margin-top:1rem;max-width:15ch;font-size:clamp(2.2rem,5.4vw,3.75rem);
  line-height:1.04;letter-spacing:-.028em;font-weight:700}}
.hero p.lede{{max-width:46ch;line-height:1.5}}
.hero .actions{{margin-top:1.5rem}}
.hero-note{{margin-top:.9rem;color:var(--text-muted);font-size:.87rem}}
@media(min-width:900px){{.hero-layout{{grid-template-columns:1fr 1.08fr}}}}

/* A chart in a frame. Used wherever an example appears. */
.figure{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--radius);padding:.6rem;overflow:hidden}}
.figure svg{{width:100%;height:auto;display:block;border-radius:8px}}
.figure figcaption{{padding:.55rem .5rem .2rem;color:var(--text-muted);
  font-size:.83rem;line-height:1.45}}
.figure figcaption b{{color:var(--text-secondary);font-weight:620}}
.gallery{{display:grid;gap:1.1rem;grid-template-columns:1fr}}
@media(min-width:820px){{.gallery{{grid-template-columns:repeat(2,1fr)}}}}

/* The reasoning trace, shown rather than described. */
.trace-demo{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--radius);padding:1rem;display:grid;gap:.45rem}}
.trace-demo .askbox{{background:var(--surface-1);border:1px solid var(--border);
  border-radius:10px;padding:.6rem .75rem;margin-bottom:.3rem;
  font-size:.95rem;color:var(--text-primary)}}
.trace-demo .askbox span{{display:block;margin-bottom:.2rem;font-size:.72rem;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:.06em;
  font-weight:650}}
.tstep{{border:1px solid var(--border);border-radius:10px;
  padding:.5rem .7rem;display:grid;gap:.25rem}}
.tstep .top{{display:flex;align-items:center;gap:.5rem}}
.tstep .nm{{font-weight:620;font-size:.89rem}}
.tstep .ms{{margin-left:auto;color:var(--text-muted);font-size:.77rem;
  font-variant-numeric:tabular-nums}}
.tstep .said{{color:var(--text-secondary);font-size:.85rem;line-height:1.45}}
.tstep .dot{{width:7px;height:7px;border-radius:50%;
  background:var(--series-3);flex:0 0 auto}}
.tstep.now .dot{{background:var(--series-1)}}

.stats{{display:grid;gap:1rem;grid-template-columns:repeat(2,1fr)}}
@media(min-width:780px){{.stats{{grid-template-columns:repeat(4,1fr)}}}}
.stat{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--radius);padding:1.05rem}}
.stat .n{{font-size:clamp(1.45rem,3vw,1.95rem);font-weight:700;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.1}}
.stat .l{{color:var(--text-muted);font-size:.85rem;margin-top:.25rem}}

.chips{{display:flex;flex-wrap:wrap;gap:.45rem}}
.chip{{border:1px solid var(--border);background:var(--panel);border-radius:8px;
  padding:.34rem .62rem;font-size:.84rem;color:var(--text-secondary)}}
.split{{display:grid;gap:2rem;grid-template-columns:1fr;align-items:center}}
@media(min-width:940px){{.split{{grid-template-columns:1fr 1fr}}}}
.lead-in{{color:var(--text-secondary);max-width:56ch;margin-bottom:1.5rem}}

/* Sections were carrying too much air once they held real content; the
   charts and cards now supply the rhythm the padding used to fake. */
section{{padding:clamp(1.75rem,3.6vw,2.75rem) 0}}
section h2{{margin-bottom:.5rem}}

/* On narrow screens keep the brand and the primary action on one row, with
   the links below. Otherwise the header wraps to three rows and pushes the
   headline off the first screen. */
@media(max-width:719px){{
  header.site .shell{{gap:.6rem;padding-block:.55rem}}
  .brand{{margin-right:auto}}
  nav.main{{order:3;width:100%;margin-left:0;gap:1rem;
    padding-top:.2rem;font-size:.92rem}}
}}

/* A short card beside a tall chart leaves a void, so this one carries a list
   that fills the column and reads faster than a paragraph. */
.rules{{display:grid;gap:.7rem;margin-top:.2rem}}
.rules li{{position:relative;padding-left:1.5rem;font-size:.92rem;
  color:var(--text-secondary);line-height:1.5}}
.rules li::before{{content:"";position:absolute;left:0;top:.5em;width:8px;
  height:8px;border-radius:2px;background:var(--series-3)}}
.rules li b{{color:var(--text-primary);font-weight:620}}
.card.fill{{display:flex;flex-direction:column;justify-content:center}}
"""


def _page(title: str, description: str, body: str, path: str = "/") -> str:
    nav = "".join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == path else ""}>{label}</a>'
        for href, label in NAV
    )
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{_css()}</style>
</head><body>
<header class="site"><div class="shell">
  <a class="brand" href="/">{helix.logo(30, uid="nav")}<span>twoHelixes</span></a>
  <nav class="main">{nav}</nav>
  <a class="btn btn-primary" href="/app">Open app</a>
</div></header>
{body}
<footer class="site"><div class="shell">
  <span>twoHelixes — data analytics agents</span>
  <span><a href="/docs">Docs</a> · <a href="/pricing">Pricing</a> ·
        <a href="/app">App</a></span>
</div></footer>
</body></html>"""


@router.get("/")
def home(ctx: router.Context) -> router.Result:
    mode = ctx.q("mode", "light") or "light"

    # Every chart below is rendered by the same pipeline that serves customers.
    # If the chart defaults regress, this page changes with them.
    hero_chart = showcase.chart_svg("revenue", mode, 720, 400)
    channels = showcase.chart_svg("channels", mode, 620, 360)
    latency = showcase.chart_svg("latency", mode, 620, 360)
    retention = showcase.chart_svg("retention", mode, 620, 360)

    connectors = (
        "PostgreSQL", "MySQL", "SQL Server", "Snowflake", "BigQuery",
        "Redshift", "ClickHouse", "Trino", "Oracle", "DuckDB", "SQLite",
        "MongoDB", "Elasticsearch", "CSV", "Parquet", "Excel", "HTTP APIs",
    )
    chips = "".join(f'<span class="chip">{html.escape(c)}</span>' for c in connectors)

    body = f"""
<main>
<div class="hero"><div class="shell hero-layout">
  <div>
    <span class="eyebrow">Ask a question. Watch it work.</span>
    <h1>Analytics that shows its working</h1>
    <p class="lede">Ask in plain language. twoHelixes finds the data, joins it,
    shapes it and picks the chart &mdash; showing every step, so you can check
    the answer instead of trusting it.</p>
    <div class="actions">
      <a class="btn btn-primary" href="/app">Start free</a>
      <a class="btn btn-ghost" href="#how">See how it works</a>
    </div>
    <p class="hero-note">{config.FREE_QUERIES_PER_USER} free queries when you
    sign in. No card required.</p>
  </div>
  <figure class="figure">
    {hero_chart}
    <figcaption><b>&ldquo;how did revenue trend by region this year?&rdquo;</b>
    &mdash; rendered by the live pipeline, not a screenshot.</figcaption>
  </figure>
</div></div>

<section class="shell" id="how" style="padding-top:1rem">
  <div class="stats">
    <div class="stat"><div class="n">5</div>
      <div class="l">visible stages per answer</div></div>
    <div class="stat"><div class="n">17</div>
      <div class="l">data connectors</div></div>
    <div class="stat"><div class="n">0</div>
      <div class="l">dual axes, ever</div></div>
    <div class="stat"><div class="n">SVG</div>
      <div class="l">export with no headless browser</div></div>
  </div>
</section>

<section><div class="shell split">
  <div>
    <h2>You see the reasoning, not a spinner</h2>
    <p class="lead-in">Each stage streams as it happens: what it looked for,
    which key it joined on, the Python it ran, why it chose that chart. When a
    stage has to approximate, it says so.</p>
    <div class="chips">
      <span class="chip">Stop mid-run</span>
      <span class="chip">Edit and re-run</span>
      <span class="chip">Take over by hand</span>
    </div>
  </div>
  <div class="trace-demo">
    <div class="askbox"><span>Question</span>how did revenue trend by region?</div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Finding the data</span><span class="ms">4 ms</span></div>
      <p class="said">orders joined to regions &mdash; 3 columns are enough to
      answer this.</p></div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Joining</span><span class="ms">61 ms</span></div>
      <p class="said">region_id &rarr; id, 98% value overlap. Row count
      unchanged, so the key is unique.</p></div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Shaping the data</span><span class="ms">1.2 s</span></div>
      <p class="said">Monthly totals per region, sorted by date so the line
      reads left to right.</p></div>
    <div class="tstep now"><div class="top"><i class="dot"></i>
      <span class="nm">Choosing the chart</span><span class="ms">0.9 s</span></div>
      <p class="said">Time on x, one line per region. A bar chart would hide
      the crossover in August.</p></div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Applying defaults</span><span class="ms">3 ms</span></div>
      <p class="said">Palette, spacing and labels applied. 0 issues found.</p></div>
  </div>
</div></section>

<section><div class="shell">
  <h2>Charts that do not mislead</h2>
  <p class="lead-in">The rules are enforced in code, not left to taste. Every
  chart is audited against them before you see it &mdash; including the three
  below.</p>
  <div class="gallery">
    <figure class="figure">{channels}
      <figcaption>Ranked categories, with the tail folded into
      <b>Other</b> rather than silently dropped.</figcaption></figure>
    <figure class="figure">{latency}
      <figcaption>A distribution, titled with <b>the finding</b> rather than
      the column names.</figcaption></figure>
  </div>
  <div class="gallery" style="margin-top:1.1rem">
    <figure class="figure">{retention}
      <figcaption>An area wash at 10% opacity &mdash; a hint of volume, never a
      saturated block.</figcaption></figure>
    <div class="card fill">
      <h3>What is enforced</h3>
      <ul class="rules">
        <li>Hues are assigned in <b>fixed order and never cycled</b> &mdash; a
        ninth series folds into Other rather than reusing a colour.</li>
        <li>Scatter and map forms <b>cap at three series</b>, because past that
        no palette stays colour-blind safe.</li>
        <li><b>Never two y-scales on one plot.</b> Mismatched measures get
        their own chart instead of a fake correlation.</li>
        <li>Light and dark are <b>separately validated</b>, not flipped.</li>
        <li>Every finished chart is <b>re-audited</b>; anything that slips
        through is reported beside it.</li>
      </ul>
    </div>
  </div>
</div></section>

<section><div class="shell">
  <h2>Connect what you already have</h2>
  <p class="lead-in">Warehouses, databases, documents, files and APIs &mdash;
  behind one read-only interface. Generated SQL is checked before it reaches a
  driver.</p>
  <div class="chips">{chips}</div>
</div></section>

<section><div class="shell">
  <h2>Built for people who check the numbers</h2>
  <div class="grid">
    <div class="card"><h3>A real SQL editor</h3><p>Schema-aware completions that
    answer instantly without waiting on a model, AI-generated queries, suggested
    questions per source, and saved queries with history.</p></div>
    <div class="card"><h3>Yours to change</h3><p>Swap the axes, the chart type
    or the grouping by hand &mdash; no query spent. Or describe the change in
    words and let the edit stage apply it.</p></div>
    <div class="card"><h3>Agents that run for minutes</h3><p>Deep research runs
    hypothesis&ndash;test&ndash;revise loops in the code interpreter and reports
    only what it produced output for.</p></div>
    <div class="card"><h3>Export that stays sharp</h3><p>A native SVG exporter
    &mdash; no headless browser, deterministic output, and markup a designer can
    open and edit. PNG and CSV too.</p></div>
  </div>
</div></section>

<section><div class="shell">
  <h2>Ask your first question</h2>
  <p class="lead-in">{config.FREE_QUERIES_PER_USER} free queries when you sign
  in. Connecting a source takes about a minute.</p>
  <a class="btn btn-primary" href="/app">Open twoHelixes</a>
</div></section>
</main>"""

    return router.html(
        _page(
            "twoHelixes — analytics agents that show their working",
            "Ask questions of your data in plain language. twoHelixes finds it, "
            "joins it, charts it, and shows every step of its reasoning.",
            body,
            "/",
        )
    )


@router.get("/features")
def features(ctx: router.Context) -> router.Result:
    body = """
<main><section><div class="shell">
  <h1 style="font-size:clamp(1.9rem,4.4vw,2.8rem);letter-spacing:-.02em;
    margin-bottom:.6rem">Features</h1>
  <p class="sub">Everything below is implemented, not planned.</p>

  <h2 style="margin-top:2rem">The pipeline</h2>
  <p class="sub">Five stages, each streaming its own reasoning. A stage that
  fails degrades rather than aborting: a failed transform falls back to the
  cleaned frame and tells you it did.</p>

  <h2 style="margin-top:2rem">Code interpreter</h2>
  <p class="sub">pandas, polars, numpy, scipy, scikit-learn, statsmodels and
  plotly are pre-imported at worker boot, alongside purpose-built tools -
  column-role inference, currency coercion, overlap-ranked join keys,
  resampling that respects the data's own granularity, outlier masks,
  trend fitting and forecasting.</p>

  <h2 style="margin-top:2rem">SQL editor</h2>
  <p class="sub">Completions answer from the schema synchronously so typing
  never waits on a model. Generation, suggestion, saved queries and full
  history sit alongside. Every generated statement is checked read-only before
  it reaches a driver.</p>

  <h2 style="margin-top:2rem">Charts you control</h2>
  <p class="sub">Change x, y, z, colour, chart type, aggregation and stacking
  by hand at any time, or describe the change in words and let the edit stage
  apply it. The reasoning trace stays attached to the chart.</p>

  <h2 style="margin-top:2rem">Export</h2>
  <p class="sub">A native SVG exporter renders bar, line, area, scatter and pie
  directly - no headless browser, deterministic output, editable markup. PNG
  and JSON export are there too.</p>
</div></section></main>"""
    return router.html(
        _page("Features — twoHelixes", "What twoHelixes does.", body, "/features")
    )


@router.get("/pricing")
def pricing(ctx: router.Context) -> router.Result:
    plans_html = ""
    for plan in PLANS:
        features_html = "".join(f"<li>{html.escape(f)}</li>" for f in plan["features"])
        highlight = " highlight" if plan.get("highlight") else ""
        plans_html += f"""
<div class="plan{highlight}">
  <h3 style="font-size:1.05rem;font-weight:640">{html.escape(plan['name'])}</h3>
  <div class="amount">{html.escape(plan['price'])}</div>
  <div class="cadence">{html.escape(plan['cadence'])}</div>
  <ul>{features_html}</ul>
  <a class="btn btn-primary" href="/app">{html.escape(plan['cta'])}</a>
</div>"""

    packs_html = "".join(
        f"<tr><td>{html.escape(p['label'])}</td>"
        f"<td>{p['credits']:,} credits</td>"
        f"<td>{html.escape(p['bonus'])}</td></tr>"
        for p in CREDIT_PACKS
    )

    costs_html = "".join(
        f"<tr><td>{html.escape(k.replace('_', ' '))}</td><td>{v} credits</td></tr>"
        for k, v in config.CREDIT_COST.items()
    )

    body = f"""
<main><section><div class="shell">
  <h1 style="font-size:clamp(1.9rem,4.4vw,2.8rem);letter-spacing:-.02em;
    margin-bottom:.6rem">Pricing</h1>
  <p class="sub">Start free. Pay only for what the agents actually run.
  One credit is one US cent.</p>
  <div class="price-grid">{plans_html}</div>

  <h2 style="margin-top:3rem">Credit packs</h2>
  <p class="sub">Credits never expire and are shared across chat, dashboards,
  SQL generation and long-running agents.</p>
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>Pack</th><th>Credits</th><th>Bonus</th></tr></thead>
    <tbody>{packs_html}</tbody>
  </table></div>

  <h2 style="margin-top:3rem">What things cost</h2>
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>Operation</th><th>Cost</th></tr></thead>
    <tbody>{costs_html}</tbody>
  </table></div>
  <p style="margin-top:1.2rem;color:var(--text-muted);font-size:.9rem">
  Free accounts get {config.FREE_QUERIES_PER_USER} AI queries in total, under
  per-minute and per-day limits. Paid credit spend is not rate limited.</p>
</div></section></main>"""
    return router.html(
        _page("Pricing — twoHelixes", "Plans and credit pricing.", body, "/pricing")
    )


@router.get("/docs")
def docs(ctx: router.Context) -> router.Result:
    body = """
<main><section><div class="shell">
  <h1 style="font-size:clamp(1.9rem,4.4vw,2.8rem);letter-spacing:-.02em;
    margin-bottom:.6rem">API</h1>
  <p class="sub">Every endpoint takes and returns JSON. Authenticate with a
  bearer API key from the billing page, or with a session cookie.</p>

  <h2>Ask a question</h2>
<pre><code>curl -X POST https://twohelixes.com/v1/query \\
  -H "Authorization: Bearer $TWOHELIXES_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"q":"revenue by region this quarter","source_id":"...","sql":"SELECT * FROM orders"}'</code></pre>

  <h2 style="margin-top:2rem">Stream the reasoning</h2>
<pre><code>curl -N -X POST https://twohelixes.com/v1/query/stream \\
  -H "Authorization: Bearer $TWOHELIXES_KEY" \\
  -d '{"q":"which regions are shrinking?"}'</code></pre>
  <p class="sub">Server-sent events: <code>stage</code>, <code>thought</code>,
  <code>warning</code>, <code>partial</code>, <code>result</code>,
  <code>done</code>.</p>

  <h2 style="margin-top:2rem">Generate SQL</h2>
<pre><code>curl -X POST https://twohelixes.com/v1/sql/generate \\
  -H "Authorization: Bearer $TWOHELIXES_KEY" \\
  -d '{"source_id":"...","question":"top customers by lifetime value"}'</code></pre>

  <h2 style="margin-top:2rem">Export a chart</h2>
<pre><code>curl "https://twohelixes.com/v1/chart/$ID/export?format=svg&amp;mode=dark" \\
  -H "Authorization: Bearer $TWOHELIXES_KEY"</code></pre>

  <h2 style="margin-top:2rem">Rate limits</h2>
  <p class="sub">Free accounts are limited per minute, per hour and per day, and
  to a lifetime allowance of AI queries. Requests paid for with API credits are
  not rate limited beyond a global per-account safety valve.</p>
</div></section></main>"""
    return router.html(
        _page("API docs — twoHelixes", "twoHelixes HTTP API.", body, "/docs")
    )


@router.get("/app")
def app_shell(ctx: router.Context) -> router.Result:
    """The single-page app shell. The bundle takes over from here."""
    return router.html(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>twoHelixes</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{_css()}</style>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<div id="root" data-boot="{int(time.time())}">
  <div class="boot-splash" style="display:grid;place-items:center;min-height:100vh">
    {helix.spinner(72, uid="boot")}
  </div>
</div>
<script type="module" src="/static/app.js"></script>
</body></html>"""
    )


@router.get("/share/{token}")
def shared_page(ctx: router.Context) -> router.Result:
    row = store.one(
        "SELECT title FROM dashboards WHERE share_token = ? AND is_public = 1",
        (ctx.params["token"],),
    )
    if row is None:
        return router.html(
            _page("Not found — twoHelixes", "", "<main><section><div class='shell'>"
                  "<h1>This dashboard is not shared</h1></div></section></main>"),
            status=404,
        )

    title = html.escape(str(row["title"]))
    return router.html(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — twoHelixes</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{_css()}</style>
<link rel="stylesheet" href="/static/app.css">
</head><body>
<div id="root" data-share="{html.escape(ctx.params['token'])}">
  <div style="display:grid;place-items:center;min-height:100vh">
    {helix.spinner(72, uid="shareboot")}
  </div>
</div>
<script type="module" src="/static/app.js"></script>
</body></html>"""
    )


# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------


@router.post("/v1/auth/signin")
def signin(ctx: router.Context) -> router.Result:
    """Create or resume an account.

    In production this is fronted by the app.nz shared-cookie SSO; the email
    path exists so a self-hosted or local instance is usable on its own.
    """
    email = str(ctx.field("email") or "").strip().lower()
    if "@" not in email or len(email) > 320:
        return router.error(400, "invalid_email")

    user = store.get_user_by_email(email) or store.create_user(email)
    token = auth.mint_session(user["id"], ctx.header("user-agent"))

    identity = auth.Identity(
        user_id=user["id"],
        email=user["email"],
        plan=user.get("plan") or "free",
        api_credits=int(user.get("api_credits") or 0),
        free_queries_used=int(user.get("free_queries_used") or 0),
    )
    return router.Result(
        status=200,
        body=identity.to_public(),
        headers={"Set-Cookie": auth.cookie_header(token, secure=not config.is_dev())},
    )


@router.post("/v1/auth/signout")
def signout(ctx: router.Context) -> router.Result:
    token = ctx.cookie(auth.COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    return router.Result(
        status=200,
        body={"signed_out": True},
        headers={"Set-Cookie": auth.clear_cookie_header()},
    )
