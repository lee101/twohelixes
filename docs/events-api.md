# Events API

Reference for sending product events to twoHelixes and reading them back.
Base URL `https://twohelixes.com` (self-hosted: your origin). Background and
design notes live in [analytics.md](analytics.md).

Two credentials:

| key | prefix | used for | secret? |
| --- | --- | --- | --- |
| write key | `thw_` | ingest only; selects a site | no, ships in browser JS |
| API key | `th_` | reporting, `/v1/query`, site management | yes, server/agent only |

Reporting calls send `Authorization: Bearer th_...`. Ingest needs no header;
the write key travels in the body (or in each protocol's usual place).

## Ingest

All ingest endpoints are forgiving: unknown fields land in `props`, bad rows are
dropped individually, unknown write keys are discarded, bot user agents are
discarded, and client clocks more than a day off are replaced with server time.
Max 2,000 events per request (`413 too_many_events` above that). Event ids
(`event_id`, Segment `messageId`, Mixpanel `$insert_id`, Amplitude `insert_id`)
make retries idempotent.

### Native: `POST /v1/collect`

Always `204`. Any `Content-Type`; `text/plain` JSON from `sendBeacon` avoids a
CORS preflight.

```sh
curl -X POST https://twohelixes.com/v1/collect -d '{
  "site_id": "thw_...",
  "events": [{
    "event": "sign_up_completed",
    "client_id": "c_7f3a",
    "session_id": "1759900000000",
    "user_id": "user_42",
    "ts": 1759900012345,
    "event_id": "evt_01J9",
    "page_location": "https://app.example.com/signup",
    "referrer": "https://example.com/pricing",
    "props": { "method": "google", "surface": "signup_page", "plan": "pro" }
  }]
}'
```

A single event without the `events` wrapper is also accepted
(`{"site_id": "...", "event": "...", "client_id": "...", ...}`).

| field | required | notes |
| --- | --- | --- |
| `event` (or `name`) | yes | <= 64 chars; snake_case recommended |
| `client_id` | yes | anonymous device/browser id; `user_id` is used if absent |
| `user_id` | no | your opaque account id; stitches anonymous history |
| `session_id` | no | server sessionises on 30 min inactivity when omitted |
| `ts` / `timestamp` | no | seconds, ms or us since epoch, or RFC3339 |
| `event_id` | no | idempotency key |
| `page_location`, `referrer` | no | full URLs; `utm_*` parsed from the URL |
| `props` | no | arbitrary JSON object |
| `sample_rate` | no | (0,1] if you sampled client-side |

Identity: send `{"type": "identify", "client_id": "...", "user_id": "...", "traits": {...}}`
in the same batch to link a device to an account.

No-JS pixel: `GET /v1/collect?tid=thw_...&cid=c_7f3a&en=page_view&dl=https%3A%2F%2Fexample.com%2F`
returns a 1x1 GIF.

Browser: one script tag, see [analytics.md](analytics.md#install).

### Segment: `/v1/batch`, `/v1/track`, `/v1/page`, `/v1/screen`, `/v1/identify`, `/v1/group`, `/v1/alias`

Point an existing Segment source or `analytics-node` at the base URL. The write
key is the Basic-auth username, as with Segment.

```sh
curl -X POST https://twohelixes.com/v1/track -u 'thw_...:' \
  -H 'Content-Type: application/json' -d '{
  "event": "purchase", "anonymousId": "c_7f3a", "userId": "user_42",
  "messageId": "ord_991",
  "properties": { "revenue": 19, "currency": "USD", "plan": "pro" }
}'
```

### GA4 Measurement Protocol: `POST /mp/collect`

```sh
curl -X POST 'https://twohelixes.com/mp/collect?measurement_id=thw_...&api_secret=unused' -d '{
  "client_id": "c_7f3a", "user_id": "user_42",
  "events": [{ "name": "begin_checkout", "params": { "value": 19, "currency": "USD", "surface": "paywall" } }]
}'
```

### Mixpanel: `POST /track`, `/import`, `/engage`, `/groups`

JSON arrays or classic base64 `data=`. The project token is the write key and
is not kept in props.

```sh
curl -X POST https://twohelixes.com/track -H 'Content-Type: application/json' -d '[{
  "event": "paywall_open",
  "properties": { "token": "thw_...", "distinct_id": "user_42", "$device_id": "c_7f3a",
                  "$insert_id": "pw_123", "surface": "export_button" }
}]'
```

### Amplitude HTTP V2: `POST /2/httpapi`, `/batch`, `/identify`

```sh
curl -X POST https://twohelixes.com/2/httpapi -H 'Content-Type: application/json' -d '{
  "api_key": "thw_...",
  "events": [{ "event_type": "sign_up_started", "device_id": "c_7f3a",
               "session_id": 1759900000000, "insert_id": "su_1",
               "event_properties": { "surface": "hero_cta" } }]
}'
```

Responds with Amplitude's `code`, `events_ingested`, `payload_size_bytes`,
`server_upload_time`.

## Reporting

All `GET`, all take `site_id` (from `/v1/analytics/sites`) and `days`
(1-365). The key must own the site or reach it through a team.

```sh
H='Authorization: Bearer th_...'
curl -H "$H" https://twohelixes.com/v1/analytics/sites
curl -H "$H" 'https://twohelixes.com/v1/analytics/summary?site_id=SITE&days=30'
curl -H "$H" 'https://twohelixes.com/v1/analytics/timeseries?site_id=SITE&days=7'
curl -H "$H" 'https://twohelixes.com/v1/analytics/events?site_id=SITE&limit=50'
curl -H "$H" 'https://twohelixes.com/v1/analytics/schema?site_id=SITE&days=30&event=purchase'
curl -H "$H" 'https://twohelixes.com/v1/analytics/paths?site_id=SITE&mode=events&depth=6&start=paywall_open'
curl -H "$H" 'https://twohelixes.com/v1/analytics/revenue?site_id=SITE&days=30'
curl -H "$H" -G https://twohelixes.com/v1/analytics/funnel --data-urlencode site_id=SITE \
  --data-urlencode 'steps=page_view,sign_up_started,sign_up_completed,paywall_open,begin_checkout,purchase'
curl -H "$H" 'https://twohelixes.com/v1/analytics/export?site_id=SITE&days=1' > segment-batch.json
```

| endpoint | extra params | returns |
| --- | --- | --- |
| `/v1/analytics/sites` | - | `sites[]`: id, domain, name, write key |
| `/v1/analytics/summary` | - | totals + top pages, events, referrers, devices, browsers, countries, campaigns |
| `/v1/analytics/timeseries` | - | hourly buckets (daily past 8 days): events, page views, users |
| `/v1/analytics/events` | `limit` | recent raw events with props and `sample_weight` |
| `/v1/analytics/schema` | `event` | event names, property names, observed types, sources, counts |
| `/v1/analytics/paths` | `mode` (both/events/pages), `start`, `depth` (2-12), `limit` (1-100) | common session journeys |
| `/v1/analytics/funnel` | `steps` (2-12, comma-separated) | per step: sessions, estimated sessions, users, step rate, drop-off |
| `/v1/analytics/revenue` | - | net revenue per currency and event; refunds negative |
| `/v1/analytics/groups` | `type` | group profiles and activity |
| `/v1/analytics/export` | `limit` | Segment-shaped batch |

Funnel semantics: steps are matched in event-time order within a session; each
step counts once per session; a session that skips a step does not reach later
steps. Steps are passed through verbatim: `page_view:/signup` restricts an event to a
page path (`page_view:/blog/*` for a prefix), and
`sign_up_completed|sign_in_completed` accepts either event (up to 8
alternatives). Clients and the MCP server do not parse steps.

Revenue reads `revenue`, `value`, `amount`, `$revenue`, or `$price * $quantity`
from props, with `currency` (or `$currency`). Currencies are never summed.

Natural language over the raw stream (spends credits):

```sh
curl -H "$H" -H 'Content-Type: application/json' https://twohelixes.com/v1/query \
  -d '{"q":"which surfaces open the paywall most and which convert?","analytics_site_id":"SITE","days":30}'
```

## Recommended SaaS event taxonomy

Use these names verbatim so funnels, the dashboards and the agent line up
across products. All snake_case, past tense for completions.

| event | when | key props |
| --- | --- | --- |
| `page_view` | every page / route change (automatic in th.js) | `page_location` |
| `signup_view` | sign-up screen rendered (optional; `page_view` on `/signup` also works) | `surface` |
| `sign_up_started` | user commits to signing up (clicks "Continue with Google", submits email) | `method`, `surface` |
| `sign_up_completed` | account created; also send `identify` | `method`, `surface` |
| `sign_in_started` | sign-in attempt begins | `method`, `surface` |
| `sign_in_completed` | session established; also send `identify` | `method` |
| `paywall_open` | upgrade/paywall UI shown | `surface`, `plan` |
| `paywall_signup_clicked` | anonymous user clicks sign up from the paywall | `surface` |
| `begin_checkout` | checkout session created / Stripe sheet opened | `plan`, `value`, `currency`, `surface` |
| `purchase` | payment confirmed (send from the server/webhook) | `revenue`, `currency`, `plan`, `transaction_id`, `surface` |
| `refund` | refund issued (server) | `revenue`, `currency`, `transaction_id` |

`surface` is the UI location that triggered the step (`hero_cta`, `nav`,
`export_button`, `quota_banner`, `pricing_page`, `settings`). It is what lets a
funnel answer "which paywall placement converts", so carry the same value from
`paywall_open` through `begin_checkout` and `purchase`.

Rules of thumb:

* Fire `purchase` server-side from the payment webhook with
  `event_id = transaction id` so retries and client duplicates collapse.
* Call `identify` (or send `user_id`) at `sign_up_completed` and
  `sign_in_completed` so pre-signup page views join the account.
* `revenue` is a number in major units (`19.0`, not cents) plus ISO `currency`.
* Keep event names stable; put variation in props.

Canonical funnels:

```text
page_view:/signup,sign_up_started,sign_up_completed
page_view,sign_up_started,sign_up_completed,paywall_open,begin_checkout,purchase
paywall_open,paywall_signup_clicked,sign_up_completed,begin_checkout,purchase
sign_up_completed|sign_in_completed,paywall_open,begin_checkout,purchase
```

## Clients

* CLI and MCP server: [`twohelixes-cli`](https://github.com/lee101/twohelixes-cli)
  (`claude mcp add twohelixes -- twohelixes-cli mcp`)
* JavaScript/TypeScript (browser + Node/Bun): [`twohelixes-js`](https://github.com/lee101/twohelixes-js)
* Python (sync + async): [`twohelixes-python`](https://github.com/lee101/twohelixes-python)
* Go (stdlib only): [`twohelixes-go`](https://github.com/lee101/twohelixes-go)
* Full funnel example app: [`twohelixes-examples`](https://github.com/lee101/twohelixes-examples)

## Email conversions

Attribute signups and purchases to email campaigns. Uses the public write key
(`tid=thw_...`), so it works from any mail tool.

- Open pixel: `<img src="https://twohelixes.com/v1/email/open.gif?tid=thw_...&camp=welcome&cid=RECIPIENT_ID" width="1" height="1">` records `email_open`.
- Tracked link: `https://twohelixes.com/v1/email/click?tid=thw_...&camp=welcome&cid=RECIPIENT_ID&url=https%3A%2F%2Fexample.com%2Fpricing` records `email_click`, then 302s to `url` with `utm_source=email&utm_medium=email&utm_campaign=<camp>` added. `url` must be on the site's own domain (or a subdomain); anything else redirects to the site root.
- `cid` is an opaque recipient id (not an email address). Optional: `uid`, `mid` (message id), `link`, `src`.
- Report: `GET /v1/analytics/email?site_id=...&days=30&conversions=sign_up_completed,purchase` returns per campaign opens, clicks, unique counts, conversions by event, `converted_sessions`, `click_rate`, `conversion_rate`.

Conversions are session based: any session that landed with `utm_medium=email` credits its campaign. Bot user agents are dropped; mail-proxy prefetch (Apple Mail Privacy Protection) can inflate opens, so judge campaigns by clicks and conversions.
