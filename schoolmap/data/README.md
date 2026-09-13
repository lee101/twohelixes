# Result input

Optional `raw/naplan_results.csv` columns:

```text
school_id,assessment_year,year_level,domain,mean_score
```

The school ID must be a Queensland Centre Code matching the directory, not
an ACARA ID. Use one release year per file. Year levels: 3, 5, 7, 9. Domains:
`reading`, `numeracy`, `writing`, `spelling`, `grammar_punctuation`.
Retain rows for suppressed results with a blank or `*` score so their missing
domain/year coverage is visible. Do not include duplicate cells or mix scales.
Keep provenance, usage permission, release year and ID mapping with the input.

`raw/qld_locations_2020.json` is the complete 1,774-row CKAN response for
resource `5b39065c-df32-415c-994c-5ff12f8de997`. Source: Queensland Department
of Education, CC BY 4.0. The builder checks the row count and unique IDs,
validates Queensland coordinates, and preserves unlocated/unranked rows.
