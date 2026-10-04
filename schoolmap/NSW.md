# NSW school collection and maps

- Dataset: https://twohelixes.com/datasets/nsw_schools
- Searchable map: https://twohelixes.com/schools/nsw
- Saved real agent run: https://twohelixes.com/app?sample=nsw_schools&example=nsw-map
- Saved agent reading-score map: https://twohelixes.com/app?sample=nsw_schools&example=nsw-naplan-map
- Domain-level results: https://twohelixes.com/datasets/nsw_naplan_results

Initial collection on 13 September 2026: 3,402 NSW search entries, including
204 subcampuses whose results belong to a head school. The crawl therefore
collects 3,198 school responses. The initial snapshot has nine responses,
90 domain rows (including missing/suppressed means), and 2,206 street addresses.
It is a partial result collection, not a completed statewide NAPLAN dataset.

As of 19 September 2026, 189 of 3,198 head-school responses are collected;
164 head schools have published domain scores (5.13%). The recent crawl
collected three responses between successive HTTP 429 responses with
`Retry-After: 3600`. The original roughly 19-hour estimate is not achievable
at that observed rate. Statewide completion remains blocked by source limits.

The managed crawler honours the full source cooldown and spaces explicit
requests by at least 21 seconds. After an API cooldown it now retries using
the existing browser page, without reloading a full results page outside the
request pacing. Startup still initializes the browser session. This removes
redundant navigation; a live throughput improvement has not been established.
Raw successes are checkpointed and validated caches are reused on restart.
No fabricated challenge tokens or alternate IPs are used.

The `nsw-crawl.service` user service on the collection machine resumes after
failures and publishes a checked snapshot every 25 attempted schools and on
cooldown. Raw responses and the private browser state live outside the repository at
`/vfast/data/code/schoolmap/data/raw/nsw`. The snapshot publisher updates only
the two NSW data fixtures, two saved map figures, and Parquet caches on production; it does not
change application code or restart the server. Source browser cookies must
never be committed or published. Public redistribution requires review of
ACARA's terms, including clauses 6.4–6.6; the data-access request in
`ACARA-NAPLAN-request.md` remains an unsubmitted draft.

```sh
systemctl --user status nsw-crawl.service
journalctl --user -u nsw-crawl.service
cat /vfast/data/code/schoolmap/data/raw/nsw/crawl-status.json
tail /vfast/data/code/schoolmap/data/raw/nsw/managed-crawl.log
# Stop/resume:
systemctl --user stop nsw-crawl.service
systemctl --user start --no-block nsw-crawl.service
```

## Run independently

From the Twohelixes repository, with Playwright Chromium and openpyxl installed:

```sh
bash schoolmap/scripts/download_nsw_sources.sh
.venv-13/bin/python schoolmap/scripts/crawl_nsw.py --year 2025
.venv-13/bin/python schoolmap/scripts/build_nsw.py
# Explicitly allow an incomplete collection, without aggregate indices:
.venv-13/bin/python schoolmap/scripts/build_nsw.py --allow-partial
.venv-13/bin/python -m unittest discover -s schoolmap/tests -v
```

`--output` selects the crawler's raw folder; `--raw` selects the builder's
input folder. `--snapshot-command` accepts one executable and runs it at
checkpoints. The checked-in publisher reads `SCHOOLMAP_RAW_DIR`; only when
`SCHOOLMAP_PUBLISH_HOST` is set does it update the configured production host.
Do not run two crawlers against the same raw directory.

## Fields and scoring

The 96% Yes / 4% No / 0% Not stated values for A.G.B.U. Alexander Primary
School are language background other than English (LBOTE), not NAPLAN scores.
LBOTE and NAPLAN participation remain separately labelled profile fields.
Neither contributes to the aggregate index.

`reading_y3`, `writing_y5`, etc. contain 2025 school mean scores. The separate
long-form dataset retains national and similar-school means, lower/upper
margins, available student counts, source URL and retrieval time. Historical
`Graphs` records are not mixed into the 2025 release. Missing/suppressed means
are null. A zero enrolment for a subcampus is not treated as a school result.

The aggregate achievement index is withheld until every head-school response
has been collected. It uses the established Schoolmap weights: reading 30%,
numeracy 30%, writing 20%, spelling 10%, grammar/punctuation 10%. Means are
standardised by year level/domain over the supplied NSW head schools. Each
expected year level needs 80% domain weight; expected levels come from the
school's 2025 year range, so a wholly missing year cannot be silently omitted.
Year-level values are averaged equally and converted through a normal CDF to
0–100. This is not a percentile or an overall school-quality measure. Ranks
are within school type, and ties share rank. Subcampuses are never ranked
independently or duplicated into the reference distribution.

Street addresses join the NSW government directory (4 September 2026 extract)
through ACARA's Location AGE ID, or an unambiguous exact school-name/suburb
match. AGE IDs and SML IDs are different namespaces. Unmatched street
addresses remain missing; suburb, postcode and coordinates are retained.

Source: Australian Curriculum, Assessment and Reporting Authority (ACARA),
My School, https://myschool.edu.au/terms-of-use. Government street addresses:
NSW Department of Education, CC BY 3.0 AU. The public dataset selects factual
fields and does not reproduce school profile narratives.

## Static asset publication

The app loads its JavaScript from the separate Twohelixes R2 bucket. Deploying
server-side static files alone is insufficient. `scripts/publish-static.py`
now defaults explicitly to `twohelixesstatic`, ignoring a sibling dotenv
bucket setting, and excludes hidden in-progress files. Use `--bucket` only
when intentionally targeting another bucket.
