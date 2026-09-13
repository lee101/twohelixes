# Queensland school map data pipeline

The map is served at `/schools`, the dataset at
`/datasets/queensland_schools`, and JSON at `/v1/schools/data`.
This release contains 1,774 Queensland directory entries from May 2020,
downloaded from the Queensland Department of Education under CC BY 4.0.
The source and its SHA-256 are recorded in the published JSON metadata.

NAPLAN results are unavailable. All scores and ranks are null and all markers
are grey. National coverage and current addresses are not claimed.

From the repository root:

```sh
python3 -m unittest discover -s schoolmap/tests -v
python3 schoolmap/scripts/build_dataset.py \
  --output interp/twohelixes/datasets/fixtures/schools.json
```

For an authorised, complete result release add `--results PATH.csv`. See
`data/README.md` for the schema. Reading and numeracy carry 30% each, writing
20%, spelling and grammar/punctuation 10% each. Scores are standardised within
year level and domain across the supplied schools, averaged with equal
year-level weights, then converted by the normal CDF to a 0–100 index.
This is not an empirical percentile or a school-quality rating. At least 80%
of domain weight must be available for every represented year level; omitted
year levels cannot be detected, so source completeness must be checked.
Ties share rank. Invalid numbers, duplicate cells and mixed releases fail.

Update the sample description, source attribution and release labels together
before publishing actual scores. The school fixture cache is refreshed when
its contents change. The map HTML lives in
`interp/twohelixes/datasets/fixtures/schools.html`.

The gateway currently advertises Muse Spark 1.3 but returned no healthy
provider during verification; Contributor returned model-not-found. The
existing fallback successfully answered school-sector count queries.
