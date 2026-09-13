# Site analytics

A first-party alternative to Google Analytics that runs inside twoHelixes: one
script tag, events in the same store the analyst agent already queries, and an
export shaped like Segment so a warehouse sync is a copy rather than a rewrite.

## Install

```html
<script>
  window.__thConfig = { siteId: 'thw_…' };
</script>
<script async src="https://twohelixes.com/static/th.js"></script>
```

That is the whole install. The script is ~4 KB minified, loads `async`, and is
written so it cannot take the host page down with it — every entry point is
wrapped, delivery failures are dropped rather than retried, and nothing it does
is synchronous.

`__thConfig` accepts:

| key        | default                              | meaning                            |
| ---------- | ------------------------------------ | ---------------------------------- |
| `siteId`   | required                             | the registered site's write key    |
| `endpoint` | `https://twohelixes.com/v1/collect`  | where to send them                 |
| `autoPage` | `true`                               | fire `page_view` on load           |
| `sampleAfterPerSecond` | `40`                     | begin adaptive client sampling above this event rate |
| `minSampleRate` | `0.01`                          | lowest adaptive keep rate          |

## What is collected without any extra code

* `page_view` on load and on every SPA navigation (`pushState`, `replaceState`,
  `popstate`)
* `outbound_click` for links to another host, `file_download` for common file
  extensions
* `js_error` with an error class and source host, never the message
* `page_exit` with the visible-time engagement counter

Each event carries the identity triple (`client_id`, `session_id`, `user_id`),
the page (location, path and referrer), the campaign (`utm_*`), and coarse
environment (device class, browser, OS, screen, viewport, language).
The app calls `identify` with its opaque account ID after sign-in, so later
events can be joined to the signed-in visitor without sending an email.

## API

GA-shaped:

```js
th('track', 'signup_started', { plan: 'pro' });
th('page');
th('identify', 'user_123', { email: 'a@b.c' });
th('flush');
```

The tracker also accepts the common GA4 command shape:

```js
gtag('event', 'signup_started', { plan: 'pro' });
```

The command is translated into the same first-party queue and remains
non-blocking. Existing sites can keep calling their legacy
`trackEvent(category, action, label, value)` adapter.

Segment-shaped, for code already written against `analytics.js`:

```js
analytics.track('Order Completed', { revenue: 42 });
analytics.page();
analytics.identify('user_123', { email: 'a@b.c' });
```

Calls made before the script finishes loading are replayed if you stub it:

```html
<script>window.th=window.th||function(){(window.th.q=window.th.q||[]).push(arguments)};</script>
```

## Ingest API

`POST /v1/collect` — the native batch. `Content-Type` is irrelevant; a
`sendBeacon` body of `text/plain` containing JSON is the normal case, which is
also what keeps the request "simple" and skips the CORS preflight.

```json
{ "site_id": "thw_…",
  "events": [ { "event": "page_view", "client_id": "…", "session_id": "…",
                "ts": 1785100000000, "page_location": "https://…" } ] }
```

Always answers `204`. Unknown event keys are kept in `props`; unusable events are
dropped individually so one bad row never loses the batch. Bot user agents are
discarded at ingest. Client timestamps more than a day from server time are
replaced — a wrong device clock should not create events in 2031.

An unknown site/write key is discarded with the same `204`. It is not
quarantined: retaining unsolicited events would create an unbounded namespace
and keep data nobody authorised twoHelixes to hold.

`POST /mp/collect?measurement_id=thw_…&api_secret=…` accepts the GA4
Measurement Protocol body (`client_id`, `user_id`, and `events[].name/params`).
`GET /v1/collect?tid=…&cid=…&en=page_view&dl=…` accepts the GA query-string
shape and answers a 1×1 GIF for no-JS and server-rendered pages.

Segment-style bodies (`type: page|track|identify|group|alias`, `anonymousId`,
`userId`, `properties`, `traits`) are accepted on the same endpoint and land in
the same table; `identify` and `alias` also upsert `analytics_identities`, which
is how an anonymous history gets stitched to a user.

Existing Segment senders can use the standard HTTP paths `/v1/batch`,
`/v1/track`, `/v1/page`, `/v1/screen`, `/v1/identify`, `/v1/group`, and
`/v1/alias`. Put the twoHelixes write key in the Basic-auth username exactly as
the Segment Tracking API does.

Mixpanel senders can point event traffic at `POST /track` (or historical
traffic at `/import`) and profile operations at `/engage`. JSON arrays and the
classic base64 `data=` form are accepted; `distinct_id`, `$device_id`,
`$user_id`, `$insert_id`, URL/referrer fields, and profile operations are
normalised without retaining the project token in event properties. Typed
group profiles use `POST /groups`.

Amplitude HTTP V2 senders can use `POST /2/httpapi` or `/batch` with their
normal `api_key` and `events` envelope. Device/user ids, event and user
properties, groups, session/time fields, and `insert_id` are preserved. The
response includes Amplitude's `code`, `events_ingested`, `payload_size_bytes`,
and `server_upload_time` fields. The classic form-encoded Identify API is
available at `POST /identify`.

Segment `messageId`, Mixpanel `$insert_id`, Amplitude `insert_id`, and native
`event_id` values become a source-scoped external id. Retries with the same id
are idempotent. A batch accepts up to 2,000 events; larger payloads receive an
explicit `413 too_many_events` response and should be chunked by the sender.

## Adaptive sampling

Sampling is off at ordinary product traffic. The browser tracker starts
sampling non-critical events only when its observed call rate crosses
`sampleAfterPerSecond`; the server has a second adaptive ceiling for SDKs and
misbehaving clients. The server ceiling is configured with
`TWOHELIXES_ANALYTICS_SAMPLE_AFTER_RPS` and divided across the configured
worker count.

Identity, group, purchase, sign-up, refund, login, and JavaScript-error events
are never server-sampled. A `sample_rate` supplied by a browser describes a
decision already made upstream and is never drawn a second time. It composes
with the server rate into one effective probability and inverse weight.
Accepted sampled events carry `sample_rate` and
`sample_weight` (the inverse probability). Report event/page-view totals use
the weight, recent-event and export responses expose the sampling metadata,
and funnels return observed plus estimated session counts. Sampling decisions
are stable for a session slice so a burst is retained as a path instead of a
bag of unrelated event coin flips.

## Reporting API

Every reporting endpoint requires a signed-in identity and checks site
ownership through the same team-sharing policy as charts and datasets.

| endpoint                                  | returns                                        |
| ----------------------------------------- | ---------------------------------------------- |
| `GET /v1/analytics/summary?site_id=&days=` | totals plus top pages, events, referrers, devices, browsers, countries, campaigns |
| `GET /v1/analytics/timeseries?site_id=&days=` | hourly buckets (daily past 8 days) of events, page views, users |
| `GET /v1/analytics/funnel?site_id=&days=&steps=` | ordered session/user progression through 2–12 event names |
| `GET /v1/analytics/events?site_id=&limit=` | the raw recent event stream                    |
| `GET /v1/analytics/schema?site_id=&days=&event=` | custom event/property catalog with types, sources, and observed/estimated counts |
| `GET /v1/analytics/groups?site_id=&days=&type=` | typed group profiles and activity             |
| `GET /v1/analytics/paths?site_id=&days=&mode=&start=&depth=` | discovered page/event journeys by session |
| `GET /v1/analytics/revenue?site_id=&days=` | weighted net revenue kept separate by currency |
| `GET /v1/analytics/export?site_id=&days=`  | Segment-shaped batch for a warehouse sync      |

The normal chart agent can query the event stream without exporting it first:

```json
POST /v1/query
{"q":"which pages lead to completed sign-ins?","analytics_site_id":"netwrck.com","days":30}
```

The site goes through the same owner/team access check as the reporting API.
Up to 100,000 recent rows can be loaded; properties are exposed as `prop_*`
columns and the chart response has the same shape as every other `/v1/query`.

Funnel steps are evaluated in event-time order within each session. A session
can advance through each named event once; repeated events are harmless, and a
session that misses a step does not count toward later steps. The response
includes reached sessions/users, overall rate, step-to-step rate, and drop-off.

## Privacy

No raw IP is stored. `ip_hash` is `sha256(site | UTC date | ip)` truncated to
32 chars: enough to deduplicate within a day, useless as a durable identifier
across days or across sites. Country comes from Cloudflare's header when
present. There are no third-party cookies and no cross-site identifiers; the
client id is a random value in the site's own `localStorage`.

Do Not Track and Global Privacy Control disable the tracker before it creates a
client ID. The tracker suppresses events while sign-in or checkout sheets are
open, does not collect page titles, and strips JavaScript error messages.

## Why it is built this way

An analytics script's failure mode is other people's pages. That is why
delivery is `sendBeacon` first (it survives the page going away, and the
browser schedules it off the critical path), `fetch(keepalive)` second, and
nothing third — a retry loop on a flaky connection is how an analytics script
becomes the reason a phone gets hot. The queue flushes on `visibilitychange`
and `pagehide` rather than `unload`, which iOS Safari never fires.

Sessionisation happens server-side against the last stored event when the
client does not supply a session id, so the pixel and server-side callers get
sessions on the same 30-minute inactivity rule as the browser tracker.

## CLI

The standalone Go client lives in `cli/` and has no third-party dependencies:

```sh
twohelixes-cli analytics summary --site netwrck.com --days 30
twohelixes-cli analytics funnel --site netwrck.com \
  --steps page_view,sign_in_started,sign_in_completed
twohelixes-cli analytics ask --site netwrck.com \
  "where do people leave the sign-in flow?"
twohelixes-cli analytics paths --site netwrck.com --days 30
twohelixes-cli analytics revenue --site netwrck.com --days 30
```

It can also call `/v1/query` for any connected source and render the returned
Plotly figure directly in the terminal. See `cli/README.md` for installation
and the complete command surface.
