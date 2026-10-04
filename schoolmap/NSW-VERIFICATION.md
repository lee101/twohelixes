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
