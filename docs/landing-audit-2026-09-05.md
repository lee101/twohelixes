# Landing page and graph audit — September 5, 2026

Implemented a responsive helix-led homepage positioning twoHelixes as a data
intelligence workspace: connections, AI exploration, dashboards, SQL, sheets,
notebooks, and agents. Sample-data CTAs preserve the sample and question;
bring-your-own-data opens signup. Fixed-label CTA events enable measurement.

## Verified

- Chartbench: all 20 forms × light/dark; 40 successful form/theme cases,
  zero failures and zero chart audit findings. Both mobile and desktop rendering
  were exercised for each case.
- Visualbench: 128 captures, including 80 chart-card captures. Zero horizontal
  overflow, blank plots, card overflow, broken images, console errors, or
  navigation failures. Library, dashboard-building, and workbench journeys
  were excluded from this scoped run.
- Funnel audit: 390px and 1440px, light and dark; sample prefill, signup modal,
  CTA event, real anonymous query-to-chart, and signup-to-workspace passed.
- Direct gateway completion returned `model: muse-spark-1.3` and valid JSON.
  Contributor was absent from the live model catalogue and was not called.
- TypeScript typecheck and all 14 frontend unit tests passed.
- The final scoped backend regression suite passed: 273 tests, including
  routing, chart construction, dataset pages, graph-quality evaluations, and
  the new landing/model/static-asset regressions.

## Bugs found and fixed

- Anonymous queries opened signup before displaying their completed chart.
  Signup is now an invitation below the visible result.
- Map assets were unreachable through the nested static route and JSON files
  were double-encoded by the response layer. Both prevented map rendering.
- Missing `DEFAULT_PRICE` silently discarded model usage accounting.
- Empty reasoning completions were accepted as successful answers rather than
  retried or handed to the fallback route.
- Four illustrative chart headlines contradicted their data. They now derive
  crossover month, shares, and retention endpoints from the plotted values.
- Title validation incorrectly rejected a legitimate color-series grouping.
- Concurrent sample warmup could expose an incomplete Parquet file. Writes now
  publish atomically; a regression test verifies the publish boundary.
- Visualbench reported console errors without failing. They now fail the run;
  the real-query path no longer invents a quality score from SVG mark counts.

Artifacts (gitignored): `visualbench/landing-refresh/visualbench/index.html`,
`visualbench/landing-refresh/chartbench/index.html`, and
`visualbench/landing-refresh/funnel/report.json` with accompanying screenshots.

Validation used local test servers with separate temporary databases, not
production accounts. No production deployment/restart was performed. Actual
conversion improvement remains unmeasured; use live funnel data after rollout.
