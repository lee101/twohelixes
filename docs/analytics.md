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

## API

GA-shaped:

```js
th('track', 'signup_started', { plan: 'pro' });
th('page');
th('identify', 'user_123', { email: 'a@b.c' });
th('flush');
```

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

`GET /v1/collect?tid=…&cid=…&en=page_view&dl=…` — GA4 Measurement-Protocol
shaped, answers a 1×1 GIF. This is the no-JS pixel and the server-side path.

Segment-style bodies (`type: page|track|identify|group|alias`, `anonymousId`,
`userId`, `properties`, `traits`) are accepted on the same endpoint and land in
the same table; `identify` and `alias` also upsert `analytics_identities`, which
is how an anonymous history gets stitched to a user.

## Reporting API

Every reporting endpoint requires a signed-in identity and checks site
ownership through the same team-sharing policy as charts and datasets.

| endpoint                                  | returns                                        |
| ----------------------------------------- | ---------------------------------------------- |
| `GET /v1/analytics/summary?site_id=&days=` | totals plus top pages, events, referrers, devices, browsers, countries, campaigns |
| `GET /v1/analytics/timeseries?site_id=&days=` | hourly buckets (daily past 8 days) of events, page views, users |
| `GET /v1/analytics/events?site_id=&limit=` | the raw recent event stream                    |
| `GET /v1/analytics/export?site_id=&days=`  | Segment-shaped batch for a warehouse sync      |

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
