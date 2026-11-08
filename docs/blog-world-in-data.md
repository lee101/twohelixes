# Ask the world: four Our World in Data datasets, one question away

twoHelixes now ships four public world datasets built from Our World in Data
indicators (CC BY 4.0): health, climate and energy, prosperity, and food. Each
has worked example questions, a chart that passes our chart audit in both
themes, and a downloadable notebook, so the answer is yours to rerun.

Every figure below was computed from the fixtures in this repository, using each
country's latest available year unless stated.

## What the data says

- **Life expectancy.** Monaco (86.4), San Marino (85.7) and Hong Kong (85.5)
  lead; Nigeria (54.5), Chad (55.1) and Lesotho (57.4) trail. Since 1990 the
  median country gained 7.5 years, and only 3 of 236 entities went down.
- **Child mortality.** The median country's under-five rate fell from 4.4% in
  1990 to 1.5%, about a third of where it started.
- **Money and years.** Across 198 countries in 2019, log GDP per person and life
  expectancy correlate at 0.84. Correlation, not a lever: the examples plot it
  and say so.
- **Happiness.** Finland (7.76), Iceland (7.54) and Denmark (7.54) top the
  Cantril ladder; Afghanistan (1.45) is lowest.
- **Carbon.** Qatar (41.3 t per person), Kuwait, Brunei and Bahrain are the
  highest CO2 emitters per head in 2024.
- **Internet.** The median country had 2.3% of people online in 2000 and 81% by
  2025.

## How it is built

The fixtures are produced by a Mojo program, `datasets/mojo/owid_bundle.mojo`.
It reads the raw Our World in Data CSVs, full-outer-joins them on country code and
year, drops aggregates such as `OWID_WRL`, and writes one compact CSV per theme.
`scripts/build-world-fixtures.sh` downloads, builds and regenerates all four.

Empty cells stay empty. A missing value is never a zero, and every example
transform drops them explicitly before ranking.

## Honest limits

- "Latest year" differs by country, and the examples say so.
- Small territories report some implausible values (several show a 0% internet
  share in 2025). We have not checked why, so treat them with suspicion.
- These are correlations over observed indicators, not causal claims.

Source: Our World in Data, CC BY 4.0. Try it at `/datasets`.
