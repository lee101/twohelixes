# twoHelixes

A data analytics agent: ask a question in plain language, it finds the data,
joins it, shapes it, picks a chart, and streams its reasoning while it works.
Rebuilt from scratch — it shares askfelix's Python environment but none of its
code.

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
binary linked against Python 3.12, so it needs askfelix's **`.venv`**
(3.12) site-packages. Scripts run with `pixi run python` are 3.13 and need
**`.venv-14`**. Point the wrong one at either and numpy fails with the
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
# Build the server (~20s)
cd server && pixi run mojo build main.mojo -o ../build/twohelixes-server

# Build the frontend
cd web && bun install && bun run build

# Run (note the 3.12 site-packages)
TWOHELIXES_PORT=7474 TWOHELIXES_WORKERS=4 TWOHELIXES_DEV=1 \
  TWOHELIXES_INTERP=$PWD/interp \
  TWOHELIXES_SITE_PACKAGES=/nvme0n1-disk/code/askfelix/.venv/lib/python3.12/site-packages \
  ./build/twohelixes-server

# Tests (3.13 site-packages)
PYTHONPATH=interp:/nvme0n1-disk/code/askfelix/.venv-14/lib/python3.13/site-packages \
  pixi run python -m pytest tests -q

# Visual benchmark — screenshots to visualbench/ (gitignored)
PYTHONPATH=/nvme0n1-disk/code/askfelix/.venv-14/lib/python3.13/site-packages \
  pixi run python bench/visualbench.py --base http://127.0.0.1:7474

# HTTP load
pixi run python bench/load.py --threads 4 --conns 16 --duration 5
```

Do not kill the server with `pkill -f twohelixes-server` — the pattern matches
the calling shell's own command line. Use `pkill -x twohelixes-serv` (the
process name is truncated to 15 characters).

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

## Free tier and abuse

Anonymous callers get nothing that costs a model call — that is the bot gate.
Signed-in free users get 3 lifetime AI queries under per-minute/hour/day
limits. Paid credit spend is not rate limited beyond a global per-account
valve. Counters live in SQLite so they are shared across worker processes; an
in-process dict would give a 4-worker box 4× the intended free tier.

Charging happens only after success, so a failed pipeline does not burn a
user's allowance.

## Conventions

- No emoji anywhere.
- Comments explain *why*, and only where the reason is not obvious.
- Every stage degrades rather than aborting; the user gets a chart plus a note
  about what was approximated.
- Generated SQL is checked read-only before it reaches a driver.
