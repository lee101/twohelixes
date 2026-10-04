# NSW release verification — 13 September 2026

Live and checked:

- https://twohelixes.com/datasets/nsw_schools
- https://twohelixes.com/schools/nsw
- https://twohelixes.com/datasets/nsw_naplan_results
- https://twohelixes.com/app?sample=nsw_schools&example=nsw-map
- https://twohelixes.com/app?sample=nsw_schools&example=nsw-naplan-map
- School CSV, domain CSV, JSON and health endpoints.

The directory contains 3,402 entries: 3,198 head-school reporting entities and
204 subcampuses. Nine head-school responses are currently collected, containing
90 domain rows including suppressed/missing means. Street addresses were matched
for 2,206 entries. Six entries currently have a published Year 3 reading mean.
There are no aggregate achievement indices in this partial release.

Two real production-model pipeline runs produced a 3,402-point location map
and a six-point map coloured continuously by reading score (328–511). Both used
two model calls via the working fallback; this does not establish that Muse
Spark is healthy. Saved run payloads and their traces are published with the app.
Browser tests on the live public URLs checked point counts, loaded map tiles,
selected NSW dataset, continuous score colours, desktop/mobile rendering and
absence of JavaScript errors. The searchable map separately passed source-popup,
address, domain-score, search, filter, CSV and mobile checks.

Validation included 51 focused form/school/quality checks before the preview fix,
76 dataset-page/selection checks, six ranking/crawl-build tests, a publisher
bucket-isolation regression, and 31 school/pipeline checks after fixing JSON
preview serialization. TypeScript typecheck and the frontend build passed.

Production source and static files are backed up under
`/nvme0n1-disk/code/twohelixes/.deploy-backups/nsw-20260913/`.
The production service is active. Both server and R2 static assets were updated.
A shared dotenv incorrectly pointed the initial static upload at Openpaths;
none of those paths overlapped its production build. The misplaced Twohelixes
objects were removed, Openpaths remained HTTP 200, and the publisher now pins
its default bucket to `twohelixesstatic` and ignores hidden temporary files.

The local `nsw-crawl.service` is active and honours the source's Retry-After
until 2026-09-13 09:06:39 UTC (21:06:39 NZST). It then requests at 21-second
intervals, saves successes, retries failures, and publishes checked snapshots
at 50-school checkpoints. A real snapshot publication updated both production
JSON fixtures and Parquet caches successfully. Allow roughly 19 hours after
the pause for the remaining responses, with further delays possible.
Full collection and the automatic aggregate-score release remain pending.

Runtime/crawl instructions: [NSW.md](NSW.md).

The viewport check additionally projects every point onto the actual map
canvas and requires all six reading-score points to be visible at both 1440px
and 390px viewport widths. Maps now fit their point bounds using the rendered
canvas dimensions, including colourbar space.

2026-09-14 coverage correction:
- Saved reading map now retains all 3,402 directory locations; missing scores use neutral markers. Refreshed the existing agent configuration without a new model call (gateway connection failed).
- Search sidebar adds Show next 40; score-selection prompts explicitly retain missing-score schools.
- Snapshot publisher refreshes both saved maps on every collection update and bounds SSH connection waits.
- Rate-limit events now publish updated crawl status; no completion ETA is promised.
- Snapshot published with 12 validated responses. NAPLAN collection remains incomplete.
- Local tests: 4 school tests and 6 collection/ranking tests; frontend build/typecheck; browser checks include all 3,402 points within desktop/mobile bounds and sidebar pagination.

2026-09-15 map-colour correction:
- Dataset embed defaults to available Year 3 reading scores; unavailable measures are labelled and disabled. Sector colouring is available for all directory rows, with its own categorical legend.
- Numeric markers use a matching continuous legend, larger opaque markers, and render after smaller neutral markers. Counts distinguish coloured scores from missing scores.
- Public saved-map controls now reconfigure the complete directory through /v1/schools/nsw/map-config, without requiring a persisted chart ID or model call. Mobile controls retain their open state.
- Saved maps refresh with sector / reading colour channels. Plotly missing-score traces render first and smaller than scored traces.
- Focused visualbench: bench/visualbench_nsw.py. Tests the dataset iframe and real app Colour controls at desktop/mobile widths, reading/numeracy/sector, all 3,402 locations, and actual rendered dataset marker pixels. Artifacts: visualbench/nsw-colour/final-local.
- 46 school/map unit tests passed; frontend build and typecheck passed. Inspected screenshots of the corrected dataset and agent maps.
- Production deployed and verified: all 12 scenarios passed at https://twohelixes.com, including the dataset iframe and real app Colour changes. Live screenshots and machine-readable results: visualbench/nsw-colour/live/report.json.

2026-09-15 subtle markers and collection repair:
- Leaflet scored markers: radius 4.5px versus 3.5px for missing scores (2px diameter difference); sector markers 4px. Plotly scored markers 7px versus 5px neutral. Coloured markers still draw last.
- Crawler now issues fetches in the Chromium page, retaining the normal browser session; reloads the session after a 429 pause. Both numeric and HTTP-date Retry-After are supported. Navigation 429 also stops collection and persists its pause. No proxy changes or shortened source waits.
- Per-response crawl status and snapshots every 25 completed responses; waiting snapshots carry actual score coverage and an 80% target. Empty/suppressed responses do not count as scored schools.
- Current collection: 15 responses, 12 head schools with domain scores out of 3,198 (0.38%). Next permitted attempt 2026-09-15T00:11:02Z. No additional source requests were made during that pause; the changed transport is not yet verified against live My School.
- Tests: 46 school/map checks and 9 collection/ranking checks passed, including real Chromium transport against controlled responses, Retry-After parsing, and separation of response coverage from score coverage. Local visualbench passed all 12 desktop/mobile scenarios, including the maximum 2px marker-size difference.
- ACARA bulk-data application draft prepared in ACARA-NAPLAN-request.md; not sent and no agreement accepted.
- Production visualbench also passed all 12 scenarios: visualbench/nsw-subtle/live/report.json. Smaller markers and live coverage reporting are deployed.
