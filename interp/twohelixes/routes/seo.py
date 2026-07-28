"""robots.txt and sitemap.xml.

Generated rather than checked in, because the interesting half of this site is
generated: nine datasets times their worked examples is most of the URLs, and a
hand-maintained sitemap goes stale the first time an example is added.

Crawlable and non-crawlable are decided once, here. Everything under `/v1/` is
an API, `/app` is a JavaScript shell with nothing in it for an indexer, and
`/share/` and `/billing/` are private by nature - a share token in a search
result is a leak, not a listing.
"""

from __future__ import annotations

from twohelixes import config, router
from twohelixes.datasets import examples, samples

# Static pages, with the priority they actually deserve relative to each other.
PAGES: tuple[tuple[str, str, str], ...] = (
    ("/", "weekly", "1.0"),
    ("/features", "weekly", "0.8"),
    ("/pricing", "weekly", "0.8"),
    ("/datasets", "weekly", "0.7"),
    ("/docs", "weekly", "0.6"),
)

# Paths a crawler gains nothing from and we lose something by exposing.
DISALLOW = ("/v1/", "/app", "/share/", "/billing/", "/static/duckdb/")


@router.get("/robots.txt")
def robots(ctx: router.Context) -> router.Result:
    site = config.site_url().rstrip("/")
    rules = "\n".join(f"Disallow: {path}" for path in DISALLOW)
    body = f"""User-agent: *
{rules}
Allow: /

Sitemap: {site}/sitemap.xml
"""
    return router.Result(
        status=200, body=body, content_type="text/plain; charset=utf-8"
    )


@router.get("/sitemap.xml")
def sitemap(ctx: router.Context) -> router.Result:
    site = config.site_url().rstrip("/")
    entries: list[tuple[str, str, str]] = list(PAGES)

    for sample in samples.SAMPLES:
        entries.append((f"/datasets/{sample.key}", "monthly", "0.6"))
        for example in examples.BY_DATASET.get(sample.key, []):
            entries.append((example.path, "monthly", "0.5"))

    urls = "".join(
        f"<url><loc>{site}{path}</loc>"
        f"<changefreq>{changefreq}</changefreq>"
        f"<priority>{priority}</priority></url>"
        for path, changefreq, priority in entries
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{urls}</urlset>"
    )
    return router.Result(
        status=200,
        body=body,
        content_type="application/xml; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )
