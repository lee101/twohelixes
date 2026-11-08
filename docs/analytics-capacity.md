# Analytics delivery and capacity

The browser tracker protects login, signup, paywall, checkout and purchase
milestones from burst sampling. Both canonical events (`begin_checkout`,
`purchase`) and deployed aliases (`checkout_started`, `purchase_completed`,
`sign_up_completed`) are retained. Reporting funnels still need to explicitly
match the names emitted by each site; retention does not rename events.

Queued batches are limited to 50 events and 48 KiB of UTF-8 JSON. Foreground
flushes use ordinary asynchronous fetch, leaving the browser's shared keepalive
quota available for exit delivery. Oversized individual events retain the
milestone but replace properties with `_twohelixes_truncated: true`. Circular
properties are rejected without poisoning the remaining queue. Storage-blocked
browsers retain client and session identity in memory for the life of the page.

Privacy signals and sensitive login/checkout overlays still suppress collection.
Emit safe conversion milestones after closing these overlays; never include
passwords, payment fields, private prompts or generated content in properties.
Delivery is best effort, not a durable payment ledger. Foreground requests use
a single-flight queue holding at most four 48 KiB batches, with two retries for
network failures, HTTP 429 and 5xx. Backoff starts at one and two seconds plus
up to 250 ms jitter; numeric or HTTP-date Retry-After is honored up to 30 seconds.
Requests abort after ten seconds where AbortController is available. Permanent
4xx errors are not retried. Each event receives a stable messageId, and retries
reuse the serialized body so the backend's unique external-ID index prevents
duplicate events after an ambiguous response. Saturation drops new batches,
not an unlimited backlog; page-exit beacon/fetch remains best effort without
retries, and other scripts can consume the shared exit quota.

## Repeatable local checks

```bash
node --test tests/test_tracker.mjs
PYTHONPATH=interp .venv-13/bin/python -m pytest tests/test_tracker.py tests/test_analytics_capacity.py tests/test_analytics.py tests/test_analytics_compact.py tests/test_analytics_bots.py -q
PYTHONPATH=interp .venv-13/bin/python bench/analytics_load.py --threads 4 --batches 25 --events 100
```

The benchmark disables dotenv configuration, uses a temporary SQLite database,
disables sampling and background dashboard jobs, and checks every purchase row
was committed. It never connects to a running service. A local run on
2026-10-14 committed 10,000 events in 0.952 seconds: 10,504 events/second,
17.38 ms batch p50 and 460.98 ms batch p99. This is a short Python-router
microbenchmark, not an HTTP, PostgreSQL, sustained-throughput or production
capacity guarantee. SQLite's single writer explains tail latency under parallel
ingestion; production uses PostgreSQL.

Before raising production traffic limits, measure HTTP ingress and reporting
queries against a staging PostgreSQL database with realistic retained history,
simultaneous login/checkout requests, and several worker processes. Monitor
ingest failures, database connection count, commit latency, disk growth and
dashboard-job backlog. Sampling has a 1% floor and excludes business milestones,
so it does not place a hard upper bound on storage or CPU under abusive traffic.

Current backend limits are 2,000 events per batch and 200 rows per insert.
Analytics bodies default to 1 MiB; the Mojo event loop rejects oversized
advertised Content-Length after parsing headers, before waiting for the body.
The Python router applies the same UTF-8 byte cap before auth/context or JSON
decoding. Configure `TWOHELIXES_ANALYTICS_MAX_BODY_BYTES` in the worker process
environment (clamped to 1 KiB..32 MiB). Upload endpoints retain their 64 MiB cap.
Body caps apply per request, not across all sockets; partial requests still
need a proxy connection/idle budget.

The in-memory dashboard notification queue defaults to 256 entries, configured
with `TWOHELIXES_ANALYTICS_DASHBOARD_QUEUE_SIZE` (1..4096, at startup). Overflow
does not discard jobs: committed pending rows remain in the database and the
worker scans them when the notification queue empties. A pending/building event
dashboard admission threshold defaults to 1024, configured with
`TWOHELIXES_ANALYTICS_MAX_PENDING_DASHBOARDS` (1..100000). At that threshold,
ingestion returns HTTP 503 with `Retry-After: 5` before committing event rows;
it does not report a successful 204 and silently lose data. The threshold is
global, and may be exceeded by jobs from an already admitted batch or concurrent
PostgreSQL transactions. It is not a strict transactional global job-count cap.
HTTP 413 rejects oversized bodies without retry. The browser tracker retries
503 responses within its bounded foreground budget; durable server-side
producers should back off and use stable external event IDs when retrying.

Python ingest commits
synchronously inside each Mojo worker's event loop, so a slow database commit
can delay unrelated requests on that worker. Reporting scans retained history
and there is no automatic analytics retention purge. PostgreSQL connections
are per thread rather than admission-controlled through a bounded pool. The
serial dashboard worker prevents thread-per-event fan-out. These are remaining
engineering limits, not solved by the delivery and backpressure changes.
Do not promise unlimited traffic without staging
tests and explicit ingress, connection, backlog and storage budgets.
