"""The editorial pages are public, styled, structured, and discoverable."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET

import pytest

from twohelixes import router


@pytest.fixture(scope="module", autouse=True)
def _routes() -> None:
    router.build()


def _get(path: str) -> tuple[int, str]:
    status, _content_type, _headers, body = router.handle("GET", path, "", "", "{}")
    return int(status), body


def test_blog_index_links_to_the_hosting_article() -> None:
    status, body = _get("/blog")
    assert status == 200
    assert 'href="/blog/codex-infinity-hosting"' in body
    assert '<link rel="stylesheet" href="/static/styles.css">' in body


def test_hosting_article_has_valid_blogposting_metadata() -> None:
    status, body = _get("/blog/codex-infinity-hosting")
    assert status == 200
    marker = '<script type="application/ld+json">'
    start = body.index(marker) + len(marker)
    payload = json.loads(body[start : body.index("</script>", start)])
    assert payload["@type"] == "BlogPosting"
    assert payload["headline"] == "Why we build on Codex Infinity"
    assert payload["mainEntityOfPage"].endswith("/blog/codex-infinity-hosting")
    assert "https://codex-infinity.com" in body


def test_blog_pages_are_in_the_sitemap() -> None:
    status, body = _get("/sitemap.xml")
    assert status == 200
    root = ET.fromstring(body)
    namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    locations = {node.text or "" for node in root.iter(f"{namespace}loc")}
    assert any(location.endswith("/blog") for location in locations)
    assert any(location.endswith("/blog/codex-infinity-hosting") for location in locations)
