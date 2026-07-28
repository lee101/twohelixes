# twoHelixes

A data analytics agent: ask a question in plain language, it finds the data,
joins it, shapes it, picks a chart, and streams its reasoning while it works.
Rebuilt from scratch. It owns its Python environments now — `.venv` (3.12) and
`.venv-13` (3.13), built by `./scripts/setup-venvs.sh`; it used to borrow
askfelix's, which meant an unrelated project's dependency change could break
this one and neither repo recorded what the other needed.

## Architecture

```
Mojo (server/)          libc sockets + epoll, HTTP/1.1 parse & serialize,
                        routing, SSE framing, SO_REUSEPORT worker processes
   |  std.python, one narrow call boundary (strings in, 4-tuple out)
Python (interp/)        routes, pipeline, code interpreter, connectors,
                        charts, billing — runs inside the Mojo process
TypeScript (web/)       Bun-built SPA: ask box, live trace, chart + controls
```

The Mojo hot path never touches Python: `/healthz` and `/readyz` are answered
in the event loop. Everything else crosses the bridge exactly once per request.

Streaming work runs on Python threads that release the GIL on network I/O; the
loop drains their buffered SSE frames each tick, so a 60-second pipeline run
never blocks another connection.

## Hard-won facts

**The AOT binary embeds CPython 3.12, but `mojo run` uses 3.13.** This is the
single most confusing thing in the repo. `pixi run mojo build` produces a
binary linked against Python 3.12, so it needs this repo's **`.venv`**
(3.12) site-packages. Scripts run with `pixi run python` are 3.13 and need
**`.venv-13`**. Point the wrong one at either and numpy fails with the
misleading "you should not try to import numpy from its source directory" —
the real error underneath is `No module named 'numpy.core._multiarray_umath'`,
because the extension suffix list is `cpython-312` while the files on disk are
`cpython-313`.

**Never let `readable()` and `flush()` call each other.** An earlier version
did, and `mojo build` went from 20 seconds to over 15 minutes chasing the
mutual-recursion cycle through the whole request path. It is also a stack-depth
hazard on a pipelined connection. The loop is iterative for both reasons.

**Do not write a `comptime`-bounded loop over a large constant.** The original
`Loop.__init__` preallocated `MAX_FDS` (65536) connection slots with
`for _ in range(MAX_FDS)`; the compiler unrolls that. Slots grow on demand now.

**Mojo 1.0 dialect notes** (it differs sharply from 24.x/25.x):
- `fn` is gone — use `def`; `def` does *not* imply `raises`, declare it
- `alias` is deprecated — use `comptime`
- stdlib is namespaced under `std.` (`std.python`, `std.ffi`, `std.os`)
- `external_call` lives in `std.ffi`
- `Span[UInt8]` needs its origin unbound: `Span[UInt8, _]`
- `UnsafePointer` is `Pointer[T, origin, _safe=False]`; only `ImmStaticOrigin`
  exists as a nameable static origin, so use `List[...].unsafe_ptr()` for
  mutable buffers and `unsafe_from_address=` for FFI returns
- safe pointers reject arithmetic and bare indexing: slice the `Span` instead
- `len(String)` is an error — `s.byte_length()`
- implicit copies need `ImplicitlyCopyable`, not just `Copyable`
- declare `read`/`write`/`close` externals with `Int` fd args to match the
  stdlib's own declarations, or the linker sees conflicting signatures

## Commands

```bash
# Environments — once. Builds .venv (3.12, the server) and .venv-13 (3.13,
# tests), installs ../pybed, and copies the int8 embedding model from ../gobed.
./scripts/setup-venvs.sh

# Build the server (~20s)
cd server && pixi run mojo build main.mojo -o ../build/twohelixes-server

# Build the frontend
cd web && bun install && bun run build

# Run (note the 3.12 site-packages)
TWOHELIXES_PORT=7474 TWOHELIXES_WORKERS=4 TWOHELIXES_DEV=1 \
  TWOHELIXES_INTERP=$PWD/interp \
  TWOHELIXES_SITE_PACKAGES=$PWD/.venv/lib/python3.12/site-packages \
  ./build/twohelixes-server

# Tests — .venv-13 has pytest, so its interpreter runs them directly
PYTHONPATH=interp ./.venv-13/bin/python -m pytest tests -q

# The same suite against Postgres, plus the SQL typecheck (which needs a real
# server to ask). Both backends are supported, so both are tested.
TWOHELIXES_PG_DSN=postgresql://twohelixes:...@127.0.0.1:5432/twohelixes_test \
  PYTHONPATH=interp ./.venv-13/bin/python -m pytest tests -q

# Move a SQLite database into Postgres (server stopped)
./.venv-13/bin/python scripts/migrate-to-postgres.py --dsn "$TWOHELIXES_PG_DSN" --dry-run

# Visual benchmark — screenshots to visualbench/ (gitignored)
PYTHONPATH=interp ./.venv-13/bin/python bench/visualbench.py --base http://127.0.0.1:7474

# Chart benchmark — every form, both renderers, contact sheet in chartbench/
PYTHONPATH=interp ./.venv-13/bin/python bench/chartbench.py --base http://127.0.0.1:7474

# HTTP load
pixi run python bench/load.py --threads 4 --conns 16 --duration 5

# Machine prices, against what the providers charge today (non-zero on drift)
./.venv-13/bin/python scripts/check-machine-prices.py
```

`bench/visualbench.py` has a third pass beyond the pages and the live query: it
shows **every chart form inside the real chart card** at phone and desktop
widths, in both themes, through `window.__thShowResult`. A form can be perfect
in isolation and still overflow its card or empty itself on a redraw - that is
how the `Plotly.react` failure between axis-less and cartesian forms (treemap
to scatter threw and left the card blank) was found. It reuses chartbench's
cases so the two benchmarks cannot disagree about what a treemap is.

`bench/chartbench.py` draws all 19 chart types twice: through the native SVG
exporter (which covers five trace types) and through the **browser**, using the
app's own bundle via the `window.__thRenderFigure` hook, then counts the marks
Plotly actually emitted. A benchmark that only ran the exporter would pass while
two thirds of the forms drew nothing in the UI — which has happened here before.
Every case also goes through both notebook exporters.

`bench/visualbench.py` also drives the whole dashboard path — empty list, list,
board, composer, per-tile editor — at phone and desktop widths in both themes,
**and then the same board mid-configuration**: the chart form being changed,
the column the tile is about being changed, a tile set two columns wide beside
two that are one. Those are the states a board is actually in while someone
works on it, and no resting screenshot shows them. It builds its panes through
`/v1/builder/*` rather than by asking questions, so the pass costs no model
calls and still goes through the real save-and-attach route a manual pane
takes. It asserts the tiles actually drew, that the editor got its channel
pickers, and that no tile stopped drawing partway through a form change —
`tile_marks`, not `marks`, because the gallery pass reports marks too and a
`stat` chart is legitimately three of them.

Do not kill the server with `pkill -f twohelixes-server` — the pattern matches
the calling shell's own command line. Use `pkill -x twohelixes-serv` (the
process name is truncated to 15 characters).

**Two more things about killing it.** Production runs on this same box under
systemd, so any name-matching kill takes the live site down with the dev
instance; kill by the pid listening on your port instead. And killing the
parent leaves the `SO_REUSEPORT` workers listening: a stale worker keeps
serving *half* the requests on that port with the old `interp` loaded, which
presents as a change that works when you curl it and not when the browser does.
Kill every pid `ss -lntp` reports for the port, then check the count is zero.

## Deployment

`twohelixes.com` is nginx -> `127.0.0.1:7474`, run by the `twohelixes` systemd
unit (`deploy/twohelixes.service`). The nginx config lives at
`deploy/nginx-twohelixes.com.conf` and is symlinked into `sites-enabled`.

Two things in that config are load-bearing:

* `/v1/*/stream` sets `proxy_buffering off` and `gzip off`. With buffering on,
  the whole reasoning trace arrives in one lump at the end and the product's
  main feature silently stops working.
* `Connection` comes from a `map` rather than being hard-coded to `upgrade`.
  Sending `Connection: upgrade` on ordinary requests breaks upstream keepalive,
  and this app has no websockets.

The site previously rendered netwrck.com because the config existed in
`sites-available` but was never enabled, so nginx fell through to the default
server — and the file itself was a netwrck copy pointing at `:8124`.

## Native database drivers

`https://github.com/lee101/mojo-db` — a pure-Mojo PostgreSQL driver written for
this project and open sourced. Vendored here as `vendor/db.mojopkg`; build
against it with `-I vendor`. No TLS yet, so it is only safe against a local or
trusted-network Postgres. The Python connectors in `interp/` remain the
production path.

## Chart rules — enforced, not advisory

`charts/palette.py` and `charts/defaults.py` implement a colour system that was
validated with the six-check validator (lightness band, chroma floor, CVD
separation, normal-vision floor, contrast) in both light and dark mode. The
rules that have no legitimate exception:

- categorical hues are assigned in **fixed slot order and never cycled**; a 9th
  series folds into a neutral "Other"
- scatter/bubble/map/small-multiple forms cap at **3 series**, because with
  every pair adjacent the full eight cannot clear the separation floors
- **no dual axes** — `_configure_axes` strips secondary-axis assignments
- sequential is one hue light→dark; diverging is two hues around a neutral gray
- three light-mode slots are under 3:1 contrast, so charts using them must ship
  labels or a legend (the "relief rule")

`defaults.audit()` re-reads a finished figure and reports anything that slipped
through. The eval suite asserts it comes back empty.

**`defaults.apply` merges layouts, it does not replace them.** Nearly every
builder returns `{"yaxis": {"title": ...}}`, and a flat `dict.update` threw away
the whole styled axis with it — grid colour, tick font, zero line and
`automargin`. Every chart in the product was quietly rendering with Plotly's
defaults and clipping its own category labels. The merge is recursive; keep it
that way.

Two rows in the top margin are ours to reserve, because Plotly reserves
neither: `LEGEND_ROW` for the legend that sits in the title's band, and
`X_TITLE_ROW` at the bottom for an axis title, which `automargin` will not grow
a margin that was set explicitly. The client then measures what it drew —
`separateTitleAndLegend` in `web/src/chart.ts` relayouts once if a wrapped
legend still landed on the title, because arithmetic over guessed font metrics
was consistently 20px short.

## One colour

`palette.BRAND` (with `BRAND_DEEP` / `BRAND_LIGHT`) is the product's only
chrome hue, and it is `--series-1` — the page and the first series of the chart
inside it are literally the same blue. `--accent` derives from it; the other
series colours are data, and status colours are status. Nothing else gets a
hue: bullets, badges, GET/POST tags and the trace dots were using the green
series and made a two-colour site out of a one-colour brand.

The mark in `charts/helix.py` (mirrored in `web/src/helix.ts` — keep them in
step) draws each strand as a **filled ribbon** whose width swells at the front
of the twist and pinches at the back, not a constant-width stroke: a stroke has
no front and no back, and read as a plaited graphic rather than a helix. Below
30px it is a different drawing — fewer turns, thicker ribbon, no rungs, a
higher opacity floor — because the fine build is four crossings inside 26
pixels and reads as texture. The spinner is the same generator, translating by
exactly one period so the loop is seamless.

`bench/make_og.py` regenerates the social card from the same mark and a live
chart. It is a build step, not a route: it needs a browser, and the server must
never need one.

**The marketing charts are real.** `routes/showcase.py` builds the homepage
examples through the same `defaults.apply` + SVG exporter that serves
customers, and logs a warning if `audit()` finds anything. A regression in the
chart defaults visibly changes the homepage — that is the point.

Those charts are emitted with `theme_vars=True`, which swaps literal colours
for `var(--text-primary, #0b0b0b)`-style custom properties. The server cannot
know the viewer's OS theme, so without this a light-rendered chart is
unreadable on a dark page. Inline SVG inherits the page's custom properties, so
one copy is correct in both themes with no JavaScript; the literal hex stays as
the fallback so a downloaded standalone file still looks right.

## Always a chart

The product's claim is that a question comes back as a chart, so the pipeline
has no path that ends with `figure is None` while a frame exists:

- `orchestrator.run` wraps the whole run; anything that escapes lands in
  `_salvage`, which charts the largest source frame.
- `_build` steps down from the chosen form, to the form the column roles imply,
  to a table — which any frame can produce.
- An empty shaped result falls back to the unshaped frame, an empty discovery
  result charts everything with a note, and a failed join charts the largest
  table alone.
- Charging and identity serialisation happen *after* the figure is on the wire,
  because an accounting error must not consume a chart the user waited for.
- `PIPELINE_BUDGET_SECONDS` is one shared deadline. Without it three stages
  retrying independently can outlive nginx's read timeout, and a chart nobody
  is still connected to is the same as no chart.

The sandbox watchdog raises `Timeout` into the running thread with
`PyThreadState_SetAsyncExc` and clears any exception that did not land before
the run returned. An earlier version only set a flag that nothing read, so a
runaway loop held the request open indefinitely.

`tests/test_always_chart.py` holds this contract with the model gateway dead.

## The trace proves the work; it is not the work

The reasoning panel is a `<details>` that opens itself while a run is
happening and folds to one line — `4 stages · 17 s` — when the chart arrives.
A finished stage folds too, to the one line it produced, with its thoughts,
code and output still one click away. On a phone the answer used to be three
screens of transcript with the chart underneath it.

Two rules make the automatic open and close bearable:

- **a hand-toggle pins it.** Once someone opens or closes it themselves, it
  stops moving on its own.
- **a failure stays open**, with the stage that failed unfolded, because that
  is the one case where the reasoning *is* the answer.

`toggle` is dispatched asynchronously, so the "this open was ours, not the
user's" flag has to be cleared **inside the listener**. Clearing it right after
setting `open` meant every one of our own opens read as a hand-toggle, and the
trace then never folded itself away again — which looks exactly like the
feature not being implemented.

The Show/Hide affordance, the leading dot and the summary padding come from
`details.trace > summary` in the inlined design system — the same disclosure
the dataset pages use. `styles.css` only adds what is specific to the app;
drawing a second caret there is how the two drift apart, and briefly did.

## Dashboards you can actually build

The grid, the tiles, the resize and the sharing all shipped before there was
any route to them: `DashboardView` was reachable only from a test hook, which
is the same as the feature not shipping. There is a nav in the app header now,
a list at `dashboards`, and three ways onto a board:

- **Ask, in the board.** The composer streams `/v1/query/stream` with
  `dashboard_id`, which `_persist_chart` already honoured; the client then
  calls `POST /v1/dashboards/{id}/charts` so the tile lands in the *layout*
  too. Chart row and layout entry are written in one transaction — two writes
  that disagree is a chart on a board that does not show it. Attaching the
  same chart twice is a no-op, because "add to dashboard" is a button people
  press again when they are not sure it worked.
- **Voice.** `web/src/voice.ts`, the browser's own `SpeechRecognition`. There
  is deliberately no server fallback: shipping audio to a transcription
  service would make "ask for a chart out loud" a data question, and the
  answer to a data question must not be "we uploaded your microphone". Where
  the API is missing the mic is not drawn at all, rather than drawn and dead.
- **By hand.** The same `Builder` in a sheet, saving with `dashboard_id`.

Per tile: worded changes go to `POST /v1/chart/{id}/edit`, which re-runs
against that chart's own data and writes back over **the same row** — the
layout, the share link and refresh all point at that id, and the ordinary query
path would have minted a second chart and orphaned the first in the grid.
Channel changes (type, x, y, colour, aggregate) go to the existing
`/v1/chart/{id}/config`, which costs nothing. The two are visibly separate
because one spends a query and the other does not.

**A built chart keeps its descriptor, not its rows.** `/v1/builder/save`
stores `{steps, config, source}` in `trace`, and `_frame_for_chart` rebuilds
from it. Before this a chart made in the builder could be looked at and nothing
else: it kept no `rows_json` and had no `source_id`, so the manual controls,
the tile editor and dashboard refresh all had nothing to rebuild a frame from.
The row cache remains for inline data, which genuinely has nowhere else to
come from.

**A tile is restyled for whoever is looking.** `_themed` strips the
theme-owned layout keys and re-runs `defaults.apply` at the viewer's mode.
Stripping first is the whole trick: `apply` merges the figure's layout *over*
the base one, so a paper colour saved in daylight beat the dark base and every
tile came back as a white plate on a dark board.

**Removing a tile has to detach the chart, not just edit the layout.**
`_dashboard_payload` returns every chart whose `dashboard_id` points at the
board, and the view places any it finds without a layout entry — so a tile
removed and saved came straight back on the next load. `DELETE
/v1/dashboards/{id}/charts/{chart_id}` does both halves in one transaction.
`tests/e2e/test_dashboard_e2e.py` found this; no unit test would have, because
the round trip *is* the bug.

**A tile prints no title of its own.** It has a caption, so the figure's title
was a second heading in a 260px box — and a stale one the moment someone
changed a channel. It is dropped in `_themed` (which lets `apply` give the
plot its 32px of top margin back) and again in `tileFigure`, because a tile
redrawn by the config route gets its figure straight from an endpoint that
knows nothing about tiles. The caption itself follows the chart:
`/v1/chart/{id}/config` writes the new title to the row, so a renamed chart is
renamed everywhere it is listed, not only on its tile.

That rename needed a chart rule to go with it. `_refresh_stale_labels` already
rewrote a title naming a measure the plan had dropped; it now also rewrites a
`by <column>` clause that no longer names the x axis — but only when the name
is some *other* column of the frame, so prose someone wrote ("Revenue by
segment leader") is still left alone, as is anything `title_locked`.

Tiles render with `displayModeBar: false` — a pane is a display, not a
workbench — and the width controls are hidden below 720px, where the grid is
one column and they cannot change anything. A **table** tile gets a scroller
of its own: ten columns squeezed into a phone-width tile is ten columns of
ellipsis, so the plot is sized at 96px per column and `.dash-plot-scroll`
takes the overflow. The tile keeps its width and the page never moves.

`tests/e2e/test_dashboard_e2e.py` drives all of it through the built binary:
build a board, configure a tile's form and columns, rename it, resize it,
remove it — and then **reload and check it is all still true**. Every
configuration assertion is made twice for that reason; a control that redraws
locally and never persists looks perfect in a screenshot. The panes are built
through `/v1/builder/*`, so the file costs one model call in total (the
composer test, which is the streaming path onto a board).

## Taking the chart away

Two notebook formats, one `NotebookSpec`, so they cannot drift: marimo (the
reactive one we can also host) and `.ipynb` (the one every analyst already has
open). `?format=ipynb` selects the second on every export endpoint.

The `.ipynb` writer needs no dependency - a notebook is JSON - because the
export path must never be the thing that fails. Neither format imports
twohelixes: they run on `pip install pandas plotly`, which is also what our own
runner has, so "it runs here" and "it runs on your laptop" are the same claim.
`tests/test_notebook_export.py`-style coverage lives in `tests/test_notebooks.py`
and executes every generated code cell.

`ipynb_export.to_marimo` converts back for hosting. marimo cells are functions,
so the names each cell binds are read off its AST and threaded forward as
arguments; skip that and the conversion imports pandas in cell one and raises
NameError in cell two.

## In-browser reshaping (opt-in)

`web/src/reshape/` compiles the builder's typed step plan to parameterised SQL
and runs it in a DuckDB-Wasm worker, then posts the shaped rows to the existing
`/v1/builder/preview` so the backend still validates, styles and audits the
figure. It is off by default behind a localStorage flag, and *any* uncertainty
— an unsupported step, too many rows, a worker crash, a timeout — falls back to
the normal backend preview without telling the user anything went wrong.

Only inline row data is eligible. A connected source is never extracted into
the browser.

The assets are self-hosted in `static/duckdb/` (the MVP single-thread build, so
no COOP/COEP headers are needed). **Running the app server directly, the local
path can never work**: `routes/static_files.py` refuses binary bodies because
the Mojo bridge carries UTF-8 strings, so the `.wasm` 501s and the client falls
back. nginx serves `/static/` from disk in production, which is the only path
those files should take.

Model-authored or user-authored SQL and Python never reach the worker — only
SQL that `reshape/compile.ts` produced from the typed plan.

## Billing work that outlives a request

A chart is billed per call. A hosted notebook or a long-running agent is billed
**per minute**, and the meter is a row in SQLite (`metering.py`), never a timer
in a worker's memory. Three things that forces:

- a session survives - and keeps being billed for - a restart of the worker
  that started it, and is listable and stoppable from any of the four;
- charging advances `paid_through` inside the same `BEGIN IMMEDIATE`
  transaction that writes the ledger row, so concurrent sweeps cannot both bill
  the same minute;
- a meter carries its provider and remote handle, so any worker can reclaim a
  cloud instance whose owner stopped heartbeating. An orphaned instance costs
  real money in real time, which is worse than an unbilled one.

Whole minutes as they elapse, the final partial minute rounded up on close.
Rounding the tail down lets a caller take 59 free seconds per session on
repeat. A meter that cannot be paid charges what the balance covers and then
stops the work rather than running up a debt.

### Machines have a rate card, derived from what they cost us

`machines.py` is the catalogue: fifteen classes from a shared local process to
a B200, each carrying the provider's price per hour. Every sell price is
**derived**, never chosen:

```
credits_per_minute = ceil(provider_cost_per_hour / 60 * MACHINE_MARGIN)   # 2.0x
```

Before it existed there was one flat `CREDIT_COST["notebook_minute"] = 1`,
charged identically for a local process, a €4/month Hetzner box and an H100 —
so a GPU minute sold at a quarter of what it cost, and the product lost more
the more it was used. `tests/test_machines.py` asserts every class clears the
margin, that no GPU class is bundled into a plan, and that a plan's included
hours cost under 10% of the plan price.

**CPU is included, GPU is credits-only.** A paid plan includes machine hours
(Plus 20, Pro 60, Team 200) denominated in the `cpu-small` class, which costs
1.3c an hour — 20 hours is 26 cents, or 1.4% of a $19 plan. A GPU hour costs
more than the whole plan, so `metering._allowance_for` reads the meter's class
out of `detail` and returns no allowance for GPU classes: an allowance sized in
CPU hours would otherwise empty in twelve minutes and give away $2 of compute.

**Three guards, because 2x margin does not survive waste.**

* *Runway before start.* `open_meter(minimum_minutes=...)` comes from the class:
  5 minutes for CPU, 15 for GPU. The window between "cannot pay" and "instance
  destroyed" is a sweep, and one sweep of an H100 costs more than most balances.
* *Ceilings, not just idleness.* A session stops at its class's idle timeout
  (30 min CPU, 10-15 GPU) **and** at `max_session_minutes`, because a session
  can be busy and still be one somebody walked away from on Friday.
* *Orphan reaping.* `machines.reap_orphans()` asks Hetzner and RunPod what is
  actually running and destroys anything no open meter is paying for, sparing
  instances younger than ten minutes. The metering sweep can only reclaim
  instances it has a row for; this catches the case where the row was never
  written — a provision that succeeded after we gave up, a worker killed
  between the API call and the insert. One lost H100 is $67 a day, silently.
  It starts in `api.boot()`, not on the first session.

**The catalogue's cost numbers are checked against the providers.**
`scripts/check-machine-prices.py` reads Hetzner's `server_types` and RunPod's
GraphQL `gpuTypes` (the REST API has no GPU catalogue) and reports drift, with
a non-zero exit when a price has risen past tolerance. Its first run found L40S
15% above what the file claimed, which had put that class under the margin
floor — that is the failure mode the script exists for, and it is worth a cron.

Deep research is a base fee plus metered minutes, and **a run that produces
nothing is refunded in full** - base and minutes. `POST /v1/agent` is the API
path (job id, poll `/v1/jobs/{id}`); `/v1/agent/stream` is the interactive one.
Both go through the same `_run`, so they cannot drift. `GET /v1/usage` reports
every meter, which is what makes a per-minute charge auditable.

## The dataset pages are the product, indexed

`/datasets`, `/datasets/{key}` and `/datasets/{key}/{slug}` are public,
server-rendered, and the same claim as the marketing charts: nothing on them
is a mockup. `datasets/examples.py` holds one `Example` per question, and each
one is executed - `figures.build` -> `defaults.apply` -> `defaults.audit` ->
the native SVG exporter - at render time and cached per worker. A regression in
the chart defaults changes nineteen public pages, and `tests/test_dataset_pages.py`
asserts `audit()` comes back empty for every one of them in both themes.

**The reasoning trace on those pages is derived, not written.** Row counts,
column counts, the chosen form and the timings are read off the run that just
happened; the only authored sentences are why that cut answers the question.
A trace with invented numbers on a page that also shows the rows is a lie
anyone can check.

**One transform string, executed and exported.** The pandas that produced the
chart is the pandas printed on the page and the pandas in the notebook
download, because it is one literal in `examples.py` - the notebook route is
public for the same reason the pages are: the work is the visitor's to take,
and asking for an email before handing it over contradicts the whole argument.

`routes/seo.py` generates `robots.txt` and `sitemap.xml` from the same tables,
so an example added to `examples.py` is in the sitemap without anyone
remembering. The tests hold both directions: every URL in the sitemap answers
200, and nothing in it is disallowed in `robots.txt`.

The router matches **whole segments only** - `{slug}.svg` is compared
literally and never matches. Extensions live in their own segment
(`/{slug}/chart.svg`), and `/v1/datasets` was already taken by the user's
uploaded datasets, which is why the public API is under `/v1/samples/`.

## End-to-end tests

`tests/e2e/` runs the **built binary** on a spare port with its own database,
because the unit suite imports `twohelixes` directly and therefore cannot see
the loop, the bridge, SSE framing, or two workers sharing one SQLite file -
which is where this repository's bugs actually live. Opt-in, since it boots a
server and takes minutes:

```bash
PYTHONPATH=interp ./.venv-13/bin/python -m pytest tests/e2e -q --e2e
```

`tests/e2e/test_signup_e2e.py` drives the part the API tests cannot see: the
sign-in sheet and the Stripe sheet are DOM, not endpoints. It found that a
late `header.site,main,footer.site,.overlay{position:relative}` rule - added to
lift content above the film-grain plate - was overriding `position:fixed` on
both overlays, so on a phone the sign-in sheet was laid out mid-page instead of
docked to the bottom. It also asserts the checkout sheet never sits on
"Preparing secure checkout" for ever: with Stripe keys in the environment the
embedded form must mount, and without them the sheet must say so.

`/billing/complete` is the embedded checkout's `return_url`. Without that route
a completed payment landed on a JSON 404, which reads as "my card was charged
and the site broke". It reports and polls; the webhook is still the only thing
that grants credits.

## Postgres in production, SQLite for a checkout

`TWOHELIXES_PG_DSN` selects Postgres; without it the store is SQLite at
`var/twohelixes.db`. Production sets it in `/etc/twohelixes.env` - root-owned
and 0600, because `deploy/twohelixes.service` is in git and a DSN carries a
password. **Removing that one line is the rollback**: the SQLite file is still
there, and `var/twohelixes-pre-postgres-*.db` is the backup taken at the cut.

SQLite has **one writer**. That is not a performance detail for this product -
four `SO_REUSEPORT` workers plus a thread per streaming job means writers
collide, and that is why signing in could fail with "database is locked" while
an agent ran. Postgres removes the class.

**The SQL is written once, in SQLite's dialect, and adapted at one point.**
`store._Connection` wraps whichever driver is live and rewrites `?` to `%s` on
the way through, so none of the ~147 call sites changed and a third backend
(`mojo-db`, which speaks `$1`) is a change to `_adapt` rather than another
migration. Two traps found doing it:

- **Do not adapt twice.** `store.query()` used to call `_adapt` *and* hand the
  SQL to a connection that adapts. `?` became `%s` became the escaped literal
  `%%s`, and psycopg reported "the query has 0 placeholders" - which reads like
  a bug in the SQL rather than in the plumbing.
- **A dropped psycopg connection keeps its locks.** SQLite connections are
  nearly free; a Postgres one is a server-side process holding whatever its
  transaction took, until the collector gets to it. `store.close()` exists for
  the paths that are not a thread ending normally.

Migrating the live database surfaced a real defect in the old data: SQLite's
`ALTER TABLE ADD COLUMN` cannot add a `NOT NULL` column to a populated table,
so every row predating `credit_dust` and `plan_usage` held a NULL the schema
says cannot exist. Postgres refused them. `scripts/migrate-to-postgres.py`
leaves NULL columns out of the INSERT so the column default applies, which is
what those rows always meant.

## The SQL is typechecked, by Postgres

There is no compile-time SQL checker for Python, and none for Mojo either -
Mojo 1.0 has `comptime` but no SQL ecosystem, and a hand-written parser would
be a worse copy of something that already exists. **Postgres is the type
checker.** `PREPARE` parses a statement, resolves every identifier against the
live schema and reports the parameter and result types - which is exactly what
`sqlx` and `sqlc` do. It is not an approximation of static checking.

`tests/test_sql_types.py` walks the AST of every module, pulls out all 134 SQL
literals and prepares each one. A renamed column fails the suite with the file
and line. It also prints what it *cannot* check: the six places SQL is built
from an f-string, where the table name comes from a dict this repo owns and
never from a request. That list is asserted, so a seventh is a decision rather
than a drift.

It only runs with `TWOHELIXES_PG_DSN` set, which keeps the fast loop fast;
`deploy.sh` runs the whole suite a second time against `TWOHELIXES_PG_TEST_DSN`
so nothing ships unchecked.

**`store.transaction()` is the only way to open a write transaction.**
SQLite has one writer, so a transaction left open stops every other write on
the box - and signing in returning "database is locked" was exactly that,
caused by a thread somewhere else entirely. Two things made it possible:
hand-written `BEGIN IMMEDIATE` / `COMMIT` pairs whose close was not guaranteed,
and the fact that **`PyThreadState_SetAsyncExc` does not interrupt a blocking
call** - the watchdog's `Timeout` is checked between bytecodes, so a thread
inside `time.sleep`, a socket read or a model call keeps the write lock until
that call returns on its own. `tests/test_store_transactions.py` holds both
halves, including a reproduction of the kill that used to strand the lock.

The context manager rolls back on `BaseException` (not `Exception` - the
watchdog's exception and a cancelled thread's are the cases that mattered),
`connection()` rolls back anything handed back mid-transaction as a backstop,
and a transaction held longer than a second logs a warning **with a stack**,
because the fix is always "move the slow thing out of the transaction" and the
stack is what says which thing. Nothing slow goes inside one: no model call, no
HTTP, no sandbox run. After this the api e2e file went from failing
intermittently in 160-290s to passing in 99s - the difference was writers
queueing behind a stranded lock.

**A new connection must not need an exclusive lock.** `store.connection()`
reads `PRAGMA journal_mode` before setting it: switching journal modes takes a
brief exclusive lock, and against a database a deep-research job is writing to
continuously that lock is not always free - so a *new* worker thread's
connection failed with "database is locked" and surfaced as a 500 on signing
in. On an existing database the mode is already `wal` and there is nothing to
lock for. This only ever reproduces with an agent job running, which is why the
e2e suite is where it shows up and the unit suite never sees it.

**Launch the server with a cleaned environment.** The binary embeds CPython
3.12 and finds its home by walking PATH, so started from inside `pixi run` it
picks up the pixi environment's 3.13 and every numpy import fails with the
usual misleading message. `tests/e2e/conftest.py::_clean_env` strips the pixi
entries from PATH as well as PYTHONPATH/PYTHONHOME/VIRTUAL_ENV.

## Pricing

Prices come from measurement, not intuition. `llm.measure()` prices every
gateway call and `orchestrator.run` records the total on the result, so the
distribution is a column in the `charts` table:

| query | model calls | cost |
|---|---|---|
| simple | 2 | ~0.9c |
| with a join | 3 | ~1.4c |
| with a transform | 3 | ~2.1c |

The old 2-credit price was **under water on the p90** - the joins and pandas
transforms this product is proud of. List prices are now set against the p90,
not the median (`config.MEASURED_COST_CENTS` records what was measured, and
`tests/test_entitlements.py` fails if a price drops below its cost or a plan
stops being profitable at full utilisation).

## Which model, and whether a model at all

Three tiers of decision, and they are not the same decision:

* **No model.** `pipeline/route.py` is a *gate*: when a question names its own
  chart ("is X correlated with Y", "how does bounce rate compare across
  sources") and the frame has the columns to support it, the chart form is
  decided lexically and the chart stage makes no call at all. It answers
  **5 of the 25 questions we ship on the dataset pages** and abstains on the
  rest. Every rule is written to abstain, because a false negative costs one
  0.02c call and a false positive costs the user a wrong chart. Tightening it
  to stop answering "which channel brings the most revenue per order" - where
  `revenue` is not a column and could mean `amount` or `net_amount` - took the
  hit rate from 36% to 20%, and that was the right trade.
* **The cheap tier** (`config.MODEL_MINI`, `deepseek-v4-flash`) for small
  structured decisions: the chart form, the meaning of a one-line edit, starter
  questions off a schema. Measured on the same chart-selection call, same
  answer (`bar`):

  | model | cost |
  |---|---|
  | deepseek-v4-flash | **0.021c** |
  | gpt-5.6-luna | 0.314c |
  | gpt-5.6-terra | 1.386c |

  A mini call that fails **escalates to the caller's model** rather than
  degrading - `_small_call` in the orchestrator. Cheaper must never mean
  "sometimes no answer". `CircuitOpen` is re-raised rather than retried: an
  open circuit means the gateway is down, not that the tier is fussy.
* **The caller's tier** for everything that writes code or plans a join.

The aggregate is part of the gate's decision, not a default: summing a length
is nonsense and averaging revenue answers a different question, so `_aggregate`
reads "median"/"average"/"total" out of the question and otherwise decides from
whether the measure's name is additive.

### Static embeddings, where they actually work

`pipeline/semantic.py` resolves **which column a question means** using
`pybed` - the same int8 512-dimension static table `gobed` uses, copied out of
`../gobed/model` by `scripts/setup-venvs.sh`. No transformer forward pass: a
token id indexes a row, the rows are mean-pooled. **~40 microseconds on CPU,
numpy the only dependency** - which is the reason it is usable at all here,
because the server runs on the 3.12 environment the AOT binary embeds and torch
is not in it. This took the gate from 5 of 25 sample questions to 9.

**It does not classify intent, and that is a measurement, not an omission.**
The first version picked the chart *form* by nearest labelled phrasing. Scored
against the questions we ship: "what should I look at?" hit the time-series
prototypes at **0.47** while "is bmi linked to progression" hit the correlation
ones at **0.16**. There is no threshold between those. Mean-pooled static
vectors are dominated by topic words, and our families differ by intent over
shared vocabulary. Intent stayed lexical, where it is exact.

Matching a phrase to a column name is the opposite case and the same
measurement says so - "customer happiness" reaches `satisfaction` at 0.27 with
the next candidate at 0.03. Two rules keep it honest:

- **only candidates of the right role are scored.** Given a whole question the
  grouping word beats the measured word ("each site" wins over "power draw"),
  so resolving a measure scores only measures;
- **a floor and a margin, both.** "which channel brings the most revenue"
  scores `amount` and `net_amount` within 0.04 of each other, which is the data
  saying a person should choose.

`mojo-embed` is **not** in this path. It is a flat int8 index that earns its
keep at a hundred thousand vectors; here the candidate set is the handful of
columns in one frame, and the search is a matmul that does not show up in a
profile. The embedding *model* was the missing piece, and that is what pybed
supplies.

`TWOHELIXES_SEMANTIC_ROUTE=0` turns it off; so does an absent model or an
absent pybed, silently, because it is an accelerator and never a dependency.

`TWOHELIXES_FAST_ROUTE=0` turns the gate off, because it is a quality trade and
a quality trade needs a switch.

The model is **included allowance, then credits, then nothing**
(`entitlements.py`):

- included usage is spent first, because it is what the subscriber bought;
- credits cover the overflow at list price less an automatic volume discount
  based on trailing 30-day spend - no tier to request, no contract;
- there is no overdraft. A plan that runs out waits for the next period or a
  top-up, which is the only version defensible in a chargeback.

Allowances renew on a rolling 30-day period **anchored to sign-up**, advanced
in whole periods so a returning user keeps their renewal day. Free renews too:
a lifetime allowance of three meant a new account hit a wall on its first
afternoon.

**Sub-credit precision matters.** A credit is a cent and a chart is four, so a
10% discount rounds straight back to four and the whole volume scheme is
decoration. Prices are computed in thousandths of a credit and the remainder is
carried on the account (`users.credit_dust`), so discounts are exact in both
directions at any call size.

The funnel starts before sign-up: an anonymous visitor gets
`ANON_QUERIES_PER_DAY` question a day **on the sample data only**, capped by
address. Refusing them outright is a sound bot gate and a terrible funnel - it
asks for an account before the product has done anything.

## Free tier and abuse

Anonymous callers get nothing that costs a model call — that is the bot gate.
Signed-in free users get 3 lifetime AI queries under per-minute/hour/day
limits. Paid credit spend is not rate limited beyond a global per-account
valve. Counters live in SQLite so they are shared across worker processes; an
in-process dict would give a 4-worker box 4× the intended free tier.

Charging happens only after success, so a failed pipeline does not burn a
user's allowance.

**None of that belongs in the app's chrome.** The trial panel and the
"N free queries left" counter in the header are gone. A running countdown to a
wall in the corner of every screen is not what a header is for, and the trial
explained itself twice — once in a panel above the ask box and again in the
refusal, which is the only place it is actually needed. The credit balance
stays for paid accounts, because that is a balance someone spends and can run
out of. The limits themselves are unchanged; only the nagging is.

## Conventions

- No emoji anywhere.
- Comments explain *why*, and only where the reason is not obvious.
- Every stage degrades rather than aborting; the user gets a chart plus a note
  about what was approximated.
- Generated SQL is checked read-only before it reaches a driver.
