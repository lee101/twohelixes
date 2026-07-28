"""The public surface, served by the real binary.

The unit tests build these pages by calling the router in-process. What they
cannot see is the Mojo bridge: every response body crosses it as a UTF-8
string, and the dataset pages are the largest bodies this server produces -
around 120 kB of inline SVG on the index. A page that is correct in Python and
truncated on the wire is exactly the bug this repository keeps finding, so the
assertions here are about arrival: the closing tag is present, the SVG count
matches, the XML parses.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlparse

SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


def _get(server, path: str, anonymous: bool = False) -> tuple[int, str]:
    """Raw bodies come back as bytes; every assertion here is about text."""
    status, payload = server.request("GET", path, raw=True, anonymous=anonymous)
    return status, payload.decode("utf-8", "replace")


def test_robots_and_the_sitemap_are_served(server):
    status, body = _get(server, "/robots.txt")
    assert status == 200
    assert "Sitemap:" in body

    status, body = _get(server, "/sitemap.xml")
    assert status == 200
    root = ET.fromstring(body)
    locations = [node.text or "" for node in root.iter(f"{SITEMAP_NS}loc")]
    assert len(locations) > 20, "the datasets and their examples belong in here"
    assert all(urlparse(loc).scheme in ("http", "https") for loc in locations)


def test_the_dataset_index_arrives_whole(server):
    """The largest page we serve, and therefore the one that gets truncated."""
    status, body = _get(server, "/datasets")
    assert status == 200
    assert body.rstrip().endswith("</html>"), "the body was cut off in transit"
    # One lead chart per dataset, plus the brand mark in the header and footer.
    assert body.count("<svg") >= 9
    assert body.count("</svg>") == body.count("<svg")


def test_a_dataset_page_carries_its_charts_and_its_traces(server):
    status, body = _get(server, "/datasets/orders")
    assert status == 200
    assert body.rstrip().endswith("</html>")
    assert body.count('<details class="trace"') >= 3
    assert '"@type":"Dataset"' in body


def test_an_example_page_and_its_downloads(server):
    path = "/datasets/orders/refund-rate-by-category"
    status, body = _get(server, path)
    assert status == 200
    assert "groupby" in body, "the transform is printed, not described"

    status, svg = _get(server, "/v1/samples/orders/refund-rate-by-category/chart.svg")
    assert status == 200
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")

    status, csv = _get(server, "/v1/samples/orders/download.csv")
    assert status == 200
    lines = csv.strip().splitlines()
    assert lines[0].startswith("order_id")
    assert len(lines) > 1000, "a truncated CSV is worse than no CSV"

    status, notebook = _get(
        server, "/v1/samples/orders/refund-rate-by-category/notebook?format=ipynb"
    )
    assert status == 200
    assert '"nbformat"' in notebook


def test_the_dataset_pages_need_no_account(server):
    """Anonymous, with no cookie and no key: that is the point of them."""
    for path in ("/datasets", "/datasets/iris", "/v1/samples/catalog"):
        status, _body = _get(server, path, anonymous=True)
        assert status == 200, path


def test_every_url_in_the_sitemap_answers(server):
    _status, body = _get(server, "/sitemap.xml")
    root = ET.fromstring(body)
    for node in root.iter(f"{SITEMAP_NS}loc"):
        path = urlparse(node.text or "").path or "/"
        status, _body = _get(server, path)
        assert status == 200, f"{path} is advertised to crawlers and returns {status}"
