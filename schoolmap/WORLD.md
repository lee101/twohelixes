# International directory rollout

This is an initial regional rollout, not complete worldwide coverage. It adds
five independently sourced datasets (123,437 source records) and 91 country,
state/province/region views. Existing NSW and historical Queensland maps remain
separate to avoid silently replacing their data or double-counting campuses.

| Dataset | Records | Mapped | Source period / licence |
| --- | ---: | ---: | --- |
| US public elementary/secondary schools | 102,178 | 102,178 | NCES EDGE 2024–25, public domain |
| New Zealand directory | 2,578 | 2,577 | Ministry of Education snapshot retrieved September 2026, CC BY 4.0 |
| Victoria locations | 2,294 | 2,293 | Department of Education 2024, CC BY 4.0 |
| Northern Territory public directory | 272 | 201 | NT Department of Education and Training, September 2026 snapshot, Creative Commons Attribution |
| Canadian primary/secondary facilities | 16,115 | 13,525 | Statistics Canada ODEF 3.0.1 (2025 correction; individual records vary), Open Government Licence – Canada |

Canada includes records with ISCED 1/2/3 flags or explicit K12 facility type;
tertiary-only and unclassified records are excluded. NT includes public-facing
school and preschool listings. No personnel, emails, phones, demographic or
individual-student fields are exposed. NZ mailing postcodes are not represented
as physical-address postcodes. US private schools are not included. Missing or
invalid coordinates are retained in downloads but never guessed onto the map.

Each dataset carries its source URL, licence, source hashes, date and coverage
notes. The source URLs and licensing were checked during acquisition. ACARA's
national data-access download has public-redistribution restrictions, so it was
not republished as a new Australia-wide directory. England's download endpoint
returned HTTP 403; no attempt was made to bypass it. Other countries need
separate source/licence review; they are not shown as collected.

## Access

- `/schools/world`: regional map selector.
- `/schools/region/au-nt`, `/schools/region/us`, `/schools/region/us-ca`,
  `/schools/region/ca`, `/schools/region/nz`, `/schools/region/au-vic`.
- `/v1/schools/world/regions`: coverage registry.
- `/v1/schools/world/data?region=us&zoom=4&bbox=-125,25,-65,49`: viewport
  clusters, source metadata and first 40 list records. Cluster counts preserve
  every matching mapped point. No arbitrary sampling. At most 2,000 markers.
- `/v1/schools/world/download/{region}`: complete region JSON; `format=csv`
  gives spreadsheet-safe CSV. National sources are also registered in the
  dataset catalogue as `world_schools_us`, `world_schools_nz`,
  `world_schools_ca`, `world_schools_au_nt`, and `world_schools_au_vic`.

These datasets contain no achievement ranks or school attendance polygons.
Different national tests must not be combined into a purported global ranking.
NCES district identifiers do not establish a child's school attendance zone.

## Refresh

Download public sources to a separate raw directory, then use
`scripts/build_world_schools.py --raw PATH --output PATH`. The NT downloader
`scripts/download_nt_directory.py` caches only public-directory pages at a
single-request pace and stops on a rate limit. Its raw path currently targets
the shared schoolmap workspace. The build validates counts and unique source
record IDs; do not drop campuses merely because they share a school code.
