"""The public dataset pages, and the SEO surface they hang off.

These pages make a strong claim - that every chart on them was drawn by the
pipeline that serves customers, from the rows shown underneath it - so the
tests assert the claim rather than the markup: the figures come back clean
through `defaults.audit`, the transform that is printed is the transform that
ran, and the sitemap lists every page that exists rather than a list somebody
remembered to update.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from twohelixes import router
from twohelixes.charts import defaults
from twohelixes.datasets import examples, samples


@pytest.fixture(scope="module", autouse=True)
def _routes() -> None:
    router.build()


def _get(path: str, query: str = "") -> tuple[int, str, str]:
    status, content_type, _headers, body = router.handle(
        "GET", path, query, "", "{}"
    )
    return int(status), content_type, body


# --------------------------------------------------------------------------
# The examples themselves
# --------------------------------------------------------------------------


@pytest.mark.parametrize("example", examples.EXAMPLES, ids=lambda e: f"{e.dataset}/{e.slug}")
def test_every_example_draws_a_chart_that_passes_our_own_audit(example) -> None:
    for mode in ("light", "dark"):
        rendered = examples.render(example.dataset, example.slug, mode)
        assert rendered is not None
        assert rendered.markup.startswith("<svg"), "the exporter drew nothing"
        assert rendered.row_count > 0
        # The rule the marketing charts are held to, held here too: a page
        # that ships a chart our validator rejects is a live regression.
        assert defaults.audit(rendered.figure, mode=mode) == []


@pytest.mark.parametrize("example", examples.EXAMPLES, ids=lambda e: f"{e.dataset}/{e.slug}")
def test_the_trace_describes_the_run_that_actually_happened(example) -> None:
    rendered = examples.render(example.dataset, example.slug)
    assert rendered is not None
    names = [step["name"] for step in rendered.trace]
    assert names == [
        "Finding the data",
        "Shaping the data",
        "Choosing the chart",
        "Applying defaults",
    ]
    # Real numbers, not narration: the row counts in the trace are the row
    # counts of the frames, and the form named is the form drawn.
    assert f"{rendered.row_count:,} out" in rendered.trace[1]["said"]
    assert rendered.trace[2]["said"].startswith(rendered.chart_type)
    assert all(step["ms"] >= 1 for step in rendered.trace)


def test_every_sample_dataset_has_at_least_one_example() -> None:
    missing = [s.key for s in samples.SAMPLES if not examples.BY_DATASET.get(s.key)]
    assert missing == [], "a dataset page with no worked example is a dead page"


def test_example_slugs_are_unique_within_a_dataset() -> None:
    assert len(examples.BY_PATH) == len(examples.EXAMPLES)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def test_the_index_lists_every_dataset_with_a_chart() -> None:
    status, content_type, body = _get("/datasets")
    assert status == 200
    assert content_type.startswith("text/html")
    for sample in samples.SAMPLES:
        assert f'href="/datasets/{sample.key}"' in body
        assert sample.name in body
    assert body.count("<svg") >= len(samples.SAMPLES)
    assert '"@type":"DataCatalog"' in body


def test_a_dataset_page_carries_its_schema_its_rows_and_its_charts() -> None:
    status, _ct, body = _get("/datasets/orders")
    assert status == 200
    frame = samples.frame("orders")
    for column in frame.columns:
        assert str(column) in body
    for example in examples.BY_DATASET["orders"]:
        assert example.headline in body
        assert 'class="trace"' in body
    assert '"@type":"Dataset"' in body
    assert "download.csv" in body


def test_the_dataset_json_ld_is_valid_json_and_names_a_download() -> None:
    _status, _ct, body = _get("/datasets/iris")
    start = body.index('<script type="application/ld+json">') + len(
        '<script type="application/ld+json">'
    )
    payload = json.loads(body[start : body.index("</script>", start)])
    assert payload["@type"] == "Dataset"
    # A Dataset with no distribution is a page about a dataset, not a dataset.
    assert payload["distribution"][0]["encodingFormat"] == "text/csv"
    assert payload["variableMeasured"]


def test_an_example_page_shows_the_transform_that_produced_the_chart() -> None:
    example = examples.BY_DATASET["orders"][0]
    status, _ct, body = _get(example.path)
    assert status == 200
    assert example.question in body
    # The first line of the transform, escaped as it appears in the page.
    assert "groupby" in body
    assert 'class="trace" open' in body
    assert "chart.svg" in body


def test_an_unknown_dataset_is_a_404_page_not_a_stack_trace() -> None:
    status, content_type, body = _get("/datasets/not-a-dataset")
    assert status == 404
    assert content_type.startswith("text/html")
    assert "noindex" in body

    status, _ct, _body = _get("/datasets/orders/not-a-question")
    assert status == 404


def test_the_checkout_return_page_exists() -> None:
    # Stripe's embedded checkout returns here. Without the route a completed
    # payment lands on a JSON 404.
    status, content_type, body = _get("/billing/complete", "session_id=cs_test_123")
    assert status == 200
    assert content_type.startswith("text/html")
    assert "cs_test_123" in body
    assert "noindex" in body


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_the_catalogue_is_public_and_mirrors_the_pages() -> None:
    status, content_type, body = _get("/v1/samples/catalog")
    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    keys = {entry["key"] for entry in payload["datasets"]}
    assert keys == {s.key for s in samples.SAMPLES}
    for entry in payload["datasets"]:
        assert entry["schema"]
        assert entry["examples"]


def test_the_csv_download_has_a_header_and_the_rows() -> None:
    status, content_type, body = _get("/v1/samples/iris/download.csv")
    assert status == 200
    assert content_type.startswith("text/csv")
    lines = body.strip().splitlines()
    assert lines[0].startswith("sepal_length_cm")
    assert len(lines) == len(samples.frame("iris")) + 1


def test_the_example_svg_is_an_svg() -> None:
    example = examples.EXAMPLES[0]
    status, content_type, body = _get(
        f"/v1/samples/{example.dataset}/{example.slug}/chart.svg"
    )
    assert status == 200
    assert content_type == "image/svg+xml"
    assert body.startswith("<svg")


def test_the_example_notebook_needs_no_account() -> None:
    from twohelixes.notebooks import ipynb_export

    example = examples.EXAMPLES[0]
    first_line = example.transform.splitlines()[0]

    status, content_type, body = _get(
        f"/v1/samples/{example.dataset}/{example.slug}/notebook", "format=ipynb"
    )
    assert status == 200, "the whole argument is that the work is yours to take"
    assert content_type.startswith("application/x-ipynb+json")
    assert json.loads(body)["nbformat"] == 4
    # The notebook must run the transform the page printed, not a paraphrase.
    assert any(first_line in cell for cell in ipynb_export.code_cells(body))

    status, content_type, body = _get(
        f"/v1/samples/{example.dataset}/{example.slug}/notebook"
    )
    assert status == 200
    assert "import marimo" in body
    assert first_line in body


# --------------------------------------------------------------------------
# robots.txt and sitemap.xml
# --------------------------------------------------------------------------


def test_robots_points_at_the_sitemap_and_hides_the_private_paths() -> None:
    status, content_type, body = _get("/robots.txt")
    assert status == 200
    assert content_type.startswith("text/plain")
    assert "Sitemap: " in body and "/sitemap.xml" in body
    for path in ("/v1/", "/app", "/share/", "/billing/"):
        assert f"Disallow: {path}" in body


def test_the_sitemap_lists_every_page_that_exists() -> None:
    status, content_type, body = _get("/sitemap.xml")
    assert status == 200
    assert "xml" in content_type

    root = ET.fromstring(body)
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locations = [node.text or "" for node in root.iter(f"{namespace}loc")]

    for path in ("/", "/features", "/pricing", "/docs", "/datasets"):
        assert any(loc.endswith(path) for loc in locations), path
    for sample in samples.SAMPLES:
        assert any(loc.endswith(f"/datasets/{sample.key}") for loc in locations)
    for example in examples.EXAMPLES:
        assert any(loc.endswith(example.path) for loc in locations), example.slug
    # Nothing in the sitemap may be disallowed in robots.txt.
    assert not any("/v1/" in loc or loc.endswith("/app") for loc in locations)


def test_every_sitemap_url_actually_answers() -> None:
    """A sitemap full of 404s is worse than no sitemap."""
    _status, _ct, body = _get("/sitemap.xml")
    root = ET.fromstring(body)
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    for node in root.iter(f"{namespace}loc"):
        path = "/" + (node.text or "").split("/", 3)[-1] if "//" in (node.text or "") else "/"
        # Rebuild the path portion without depending on the configured host.
        text = node.text or ""
        path = text.split("//", 1)[-1]
        path = path[path.index("/") :] if "/" in path else "/"
        status, _ct, _body = _get(path)
        assert status == 200, f"{path} is in the sitemap and returns {status}"
