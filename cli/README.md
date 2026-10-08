# twohelixes-cli

A dependency-free Go client for [twoHelixes](https://twohelixes.com). It can
generate a chart through the API, render a Plotly figure directly in a
terminal, inspect product analytics, build ordered funnels, ask the chart
agent questions about an analytics stream, and serve all of it to coding
agents as an MCP server.

## Install

```sh
go install github.com/lee101/twohelixes-cli/cmd/twohelixes-cli@latest
export TWOHELIXES_API_KEY=th_...
```

Or build from a checkout:

```sh
git clone https://github.com/lee101/twohelixes-cli && cd twohelixes-cli
go build -o twohelixes-cli ./cmd/twohelixes-cli
```

## Generate and render a graph

```sh
twohelixes-cli chart --source src_123 "revenue by region this quarter"
twohelixes-cli chart --data orders.json "monthly revenue by channel"
twohelixes-cli render chart-response.json
curl -s https://example.test/chart.json | twohelixes-cli render -
```

The API response remains available with `--json`. Terminal rendering supports
bar, horizontal bar, pie, line, and scatter Plotly traces and degrades to a
numeric plot for other Cartesian traces.

## Product analytics

```sh
twohelixes-cli analytics sites
twohelixes-cli analytics summary --site netwrck.com --days 30
twohelixes-cli analytics events --site netwrck.com --limit 50
twohelixes-cli analytics schema --site netwrck.com --days 30
twohelixes-cli analytics schema --site netwrck.com --event purchase --json
twohelixes-cli analytics paths --site netwrck.com --mode events --depth 6
twohelixes-cli analytics paths --site netwrck.com \
  --start page_view:/pricing --mode both
twohelixes-cli analytics revenue --site netwrck.com --days 30
twohelixes-cli analytics funnel --site netwrck.com \
  --steps page_view,sign_in_started,sign_in_completed
twohelixes-cli analytics ask --site netwrck.com \
  "which pages produce the most completed sign-ins?"
twohelixes-cli analytics export --site netwrck.com > segment-batch.json
```

`analytics schema` catalogs arbitrary custom event properties, observed types,
source protocols, and sampled event estimates. `analytics paths` discovers
common event/page journeys without predefined funnel steps. `analytics revenue`
keeps currencies separate rather than applying an implicit exchange rate.
`analytics ask` uses the same chart pipeline as the web product. The API key
must own the analytics site or have access through a twoHelixes team.

## Send an event

```sh
twohelixes-cli track --site thw_... --event deployment_completed \
  --client ci --props '{"service":"checkout","version":"a18c3f2"}'

# `event` is an alias and the custom event name may be positional.
twohelixes-cli event --site thw_... --user user_42 \
  --session 1756512000000 --insert-id order_991 \
  --props @purchase.json purchase_completed
```

Write keys select an analytics site. Read and chart operations require the
account API key. `--props @FILE` reads an object from a file; `@-` reads it
from stdin. `--timestamp` accepts RFC3339 or Unix seconds, milliseconds, or
microseconds, and `--sample-rate` preserves client-side sampling metadata.

The default wire format is twoHelixes native. The same arbitrary custom event
can exercise a migration-compatible endpoint without changing its meaning:

```sh
twohelixes-cli track --protocol segment   --site thw_... --event signed_up
twohelixes-cli track --protocol ga4       --site thw_... --event signed_up
twohelixes-cli track --protocol mixpanel  --site thw_... --event signed_up
twohelixes-cli track --protocol amplitude --site thw_... --event signed_up \
  --session 1756512000000
```

These emit the standard Segment Track, GA4 Measurement Protocol, Mixpanel
Track, and Amplitude HTTP API v2 payload shapes. Amplitude session IDs are Unix
milliseconds and therefore must be numeric.

## MCP server (Claude Code, Cursor, Codex, any MCP client)

`twohelixes-cli mcp` speaks the Model Context Protocol over stdio
(newline-delimited JSON-RPC 2.0; negotiates protocol 2025-11-25, 2025-06-18,
2025-03-26 or 2024-11-05). No extra dependencies.

```sh
go install github.com/lee101/twohelixes-cli/cmd/twohelixes-cli@latest
claude mcp add twohelixes -e TWOHELIXES_API_KEY=th_... -- twohelixes-cli mcp
# or, with the key already exported in your shell:
claude mcp add twohelixes -- twohelixes-cli mcp
```

Generic client config (`.mcp.json`, Cursor, Claude Desktop):

```json
{ "mcpServers": { "twohelixes": {
    "command": "twohelixes-cli", "args": ["mcp"],
    "env": { "TWOHELIXES_API_KEY": "th_...", "TWOHELIXES_WRITE_KEY": "thw_..." } } } }
```

| tool | endpoint | notes |
| --- | --- | --- |
| `list_sites` | `GET /v1/analytics/sites` | site ids for every other tool |
| `summary` | `GET /v1/analytics/summary` | totals, top pages/events/referrers |
| `timeseries` | `GET /v1/analytics/timeseries` | hourly (<=8 days) or daily buckets |
| `funnel` | `GET /v1/analytics/funnel` | `steps` string or array, passed through verbatim |
| `paths` | `GET /v1/analytics/paths` | `mode`, `start`, `depth`, `limit` |
| `events` | `GET /v1/analytics/events` | recent raw events, default 50 |
| `schema` | `GET /v1/analytics/schema` | event/property catalog; call before funnels |
| `revenue` | `GET /v1/analytics/revenue` | per currency, never summed across currencies |
| `ask` | `POST /v1/query` | natural language; spends credits; figure omitted unless `include_figure` |
| `track` | `POST /v1/collect` | single event or `events` batch with a write key |

Funnel steps are sent as-is, so server-side step qualifiers work without a CLI
upgrade: `page_view:/signup` restricts a step to a path and
`sign_up_completed|sign_in_completed` accepts either event, where the server
supports them.

Example prompts: "list my twoHelixes sites and show the signup to purchase
funnel for the last 14 days", "which page do paywall_open sessions start on?".

## Configuration

- `TWOHELIXES_API_KEY`: account API key.
- `TWOHELIXES_WRITE_KEY`: default analytics write key for `track`/`event` and the MCP `track` tool.
- `TWOHELIXES_API_URL` (or `TWOHELIXES_URL`): alternate or self-hosted API origin.
- `COLUMNS`: terminal plot width when `--width` is omitted.

Global flags must appear before the command, for example:

```sh
twohelixes-cli --api-url http://127.0.0.1:7474 analytics sites
```

## Development

```sh
go test -race ./...
go vet ./...
```

The CLI uses only the Go standard library so the published binary has no
runtime dependencies and the module has no third-party supply-chain surface.

## Related

- [twohelixes-js](https://github.com/lee101/twohelixes-js): browser + Node/Bun tracking and reporting client
- [twohelixes-python](https://github.com/lee101/twohelixes-python): sync + async Python client
- [twohelixes-go](https://github.com/lee101/twohelixes-go): stdlib-only Go client
- [twohelixes-examples](https://github.com/lee101/twohelixes-examples): full signup to paywall to checkout funnel app and the recommended event taxonomy

## License

MIT
