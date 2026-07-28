# GA4 analytics parity

This is a capability map, not a compatibility claim. “At parity” means the
twoHelixes result follows the same reporting rule for the supported web event
shape. It does not mean the products have the same administration UI or every
GA4 report.

| GA4 concept | twoHelixes concept | Verdict | Exact boundary |
| --- | --- | --- | --- |
| Property / web data stream | Analytics site | Partial | A site is one owned domain and event namespace. There are no app streams or multi-stream properties. Sites can be shared with an existing twoHelixes team. |
| Measurement ID | Write key | At parity | The public write key selects the registered site at collection time. Unknown IDs are discarded; they never create a namespace. |
| `gtag` / `dataLayer` | `th.js` and the Segment aliases | Partial | Page, track and identify calls plus queued `th` calls are supported. The generic `dataLayer` command/config/plugin surface is not. |
| GA4 event and event parameters | `event_name` and `props` | At parity | GA-style names and scalar/nested JSON parameters are retained, subject to collector size limits. |
| `user_pseudo_id` | `client_id` | Deliberately different | It is a random first-party localStorage value, not a Google cookie or cross-site identifier. DNT and GPC disable collection before an ID is created. |
| Sessions, 30-minute inactivity, `session_start` | `session_id`, server sessionisation, generated `session_start` | At parity | A new session begins after at least 30 minutes without an event. The first accepted hit emits one `session_start`; campaign changes do not split a session. |
| `engagement_time_msec` | `engagement_ms` | Partial | Measurement Protocol `_et` and native engagement deltas accumulate. `th.js` counts visible-tab time, but does not reproduce GA4’s complete user-engagement event lifecycle. |
| Conversions / key events | Conventional key-event names in engaged-session reporting | Partial | `purchase`, `sign_up`, `generate_lead`, and `conversion` count as key events. There is no per-site custom key-event registry or attribution report. |
| Audiences | None | Not supported | There is no persistent segment membership, activation, or advertising export. Dataset filters can answer cohort questions without creating an audience object. |
| UTM and traffic-source attribution | Session-scoped `utm_*` plus referrer fields | Partial | URL, GA shorthand and Segment campaign fields are accepted; the first campaign values survive later session events and self-referrals are excluded. Channel grouping, ad-click IDs, and cross-session last-non-direct attribution are absent. |
| Explorations | Chat queries and editable dashboards | Deliberately different | twoHelixes uses questions and charts rather than GA’s free-form exploration canvas. New event names receive a structural starter dashboard without a model call. |
| BigQuery export | `/v1/analytics/export` and the datasets library | Partial | The authenticated endpoint returns a bounded Segment-shaped raw batch that can enter the datasets library. It is not a continuous linked warehouse export and has no intraday tables. |

## Event namespaces that predate ownership

Before `analytics_sites` existed a `site_id` was whatever the tracker sent, and
an absent one was derived from the `Origin` header - so already-collected rows
sit under bare hostnames. The collector now keeps only events whose site is
registered, which means those namespaces stop collecting the moment this ships
unless someone claims them.

Claiming one is a flag on the provisioning script rather than a migration,
because the database cannot know which account owns a hostname:

```bash
PYTHONPATH=interp ./.venv-13/bin/python scripts/provision-analytics-site.py \
  owner@example.com example.com --adopt example.com
```

`--adopt` reuses the legacy namespace as the site's id, so the history stays
queryable and a snippet already deployed with that id keeps working. Minting a
fresh id instead silently strands both. The script prints how many events it is
adopting, and refuses an id another account already holds.

Check what is live before shipping:

```sql
SELECT site_id, COUNT(*) FROM analytics_events
 WHERE site_id NOT IN (SELECT id FROM analytics_sites) GROUP BY site_id;
```

## Reporting rules tested

The parity suite sends Measurement Protocol GETs, native/gtag-shaped events and
Segment calls through the real HTTP collector. It verifies:

- a 30-minute inactivity boundary and exactly one `session_start` per session;
- page-view, user, session, event and engagement totals;
- self-referral exclusion and session UTM persistence;
- an engaged session at 10 seconds of engagement, two page views, or a
  conventional key event, with bounce rate as the inverse.

Unknown write keys are deliberately discarded with the collector’s normal
forgiving response (`204` for POST and the pixel for GET). Rejecting them
instead of quarantining avoids retaining unsolicited data and prevents guessed
hostnames from creating storage namespaces.
