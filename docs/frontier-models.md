# Frontier model routing

The default analysis and deep-agent route is `muse-spark-1.3`. Bounded chart
selection and edits use `deepseek-v4-flash`, escalating to the reasoning model
when required. Existing `TWOHELIXES_MODEL` and `TWOHELIXES_MODEL_DEEP` overrides
are preserved; `TWOHELIXES_MODEL_FAST` and `TWOHELIXES_MODEL_MINI` are also available.

Muse Spark 1.3 was verified in the local OpenPaths `/v1/models` catalogue.
The gateway's configured prices are $1.25 input / $4.25 output per million
tokens. The application records usage for each actual route, including failed
empty completions, and falls back to DeepSeek on failure. Re-measure workload
costs before changing subscription allowances: the older measurements in
`config.py` are not measurements of Muse Spark.

Release reference: [Meta's September 2 announcement](https://research.meta.ai/blog/introducing-muse-spark-1-3).

## Contributor is not an automatic fallback

Contributor may use submitted data for model improvement. Review current
provider terms and obtain the appropriate consent for customer data before
enabling it. It is not currently listed by the local gateway. Provision and
verify that route first; then opt in explicitly:

```sh
TWOHELIXES_MODEL=muse-spark-1.3-contributor
TWOHELIXES_ALLOW_CONTRIBUTOR=1
```

Never label a successful fallback as proof that Contributor itself works.
Test the requested route directly, and verify provider pricing before enabling
it. Until a price is verified and added to `llm.PRICES`, custom routes use a
conservative $5 / $30 per million token estimate, not a claimed provider price.
Restart application workers after changing model configuration.

## Landing-page measurement

The homepage emits `landing_cta_clicked` through the existing privacy-aware
tracker, with fixed `placement` and `variant=helix-intelligence-v1` properties.
No question text, filenames, or dataset contents are included in this event.
Tracker delivery requires the existing analytics site configuration.

The primary CTA opens `/app` with a real sample and a prefilled question; it
does not automatically spend a query. The secondary CTA opens account creation
for visitors bringing their own data. Compare CTA engagement and completed
signups against the previous page using actual traffic; a visual audit alone
cannot establish a conversion uplift.
