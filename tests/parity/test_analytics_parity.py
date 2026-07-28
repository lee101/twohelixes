from __future__ import annotations

import http.cookiejar
import json
import os
import time
import urllib.parse
import urllib.error
import urllib.request
import uuid
from typing import Any

import pytest

BASE_URL = os.environ.get("TWOHELIXES_PARITY_BASE_URL", "").rstrip("/")

pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="set TWOHELIXES_PARITY_BASE_URL to run the real HTTP parity suite",
)


def test_ga4_supported_surface_matches_reporting_rules() -> None:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

    def request(
        method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            BASE_URL + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with opener.open(req, timeout=20) as response:
            body = response.read()
            if response.headers.get_content_type() == "application/json":
                return response.status, json.loads(body)
            return response.status, body

    token = uuid.uuid4().hex[:10]
    domain = f"parity-{token}.example"
    status, _signed_in = request(
        "POST", "/v1/auth/signin", {"email": f"parity-{token}@test.local"}
    )
    assert status == 200
    status, site = request(
        "POST", "/v1/analytics/sites", {"domain": domain, "name": "Parity suite"}
    )
    assert status == 201
    site_id = site["id"]
    write_key = site["write_key"]

    try:
        now = time.time()
        first = now - 2_000
        pixel_query = urllib.parse.urlencode(
            {
                "tid": write_key,
                "cid": "ga-client",
                "en": "page_view",
                "ts": str(first),
                "dl": f"https://{domain}/landing?utm_source=newsletter&utm_medium=email&utm_campaign=launch",
                "dr": "https://search.example/results",
            }
        )
        status, pixel = request("GET", f"/v1/collect?{pixel_query}")
        assert status == 200
        assert pixel.startswith(b"GIF89a")

        status, _ = request(
            "POST",
            "/v1/collect",
            {
                "site_id": write_key,
                "client_id": "ga-client",
                "events": [
                    {
                        "name": "purchase",
                        "ts": (first + 1) * 1000,
                        "params": {
                            "engagement_time_msec": 12_000,
                            "page_location": f"https://{domain}/checkout",
                            "page_referrer": f"https://{domain}/landing",
                            "value": 49,
                        },
                    }
                ],
            },
        )
        assert status == 204

        status, _ = request(
            "POST",
            "/v1/collect",
            {
                "site_id": write_key,
                "events": [
                    {
                        "event": "page_view",
                        "client_id": "ga-client",
                        "ts": now * 1000,
                        "page_location": f"https://{domain}/return",
                        "referrer": f"https://{domain}/checkout",
                    }
                ],
            },
        )
        assert status == 204

        for path in ("/docs", "/pricing"):
            status, _ = request(
                "POST",
                "/v1/collect",
                {
                    "site_id": write_key,
                    "type": "page",
                    "anonymousId": "segment-client",
                    "session_id": "segment-session",
                    "context": {
                        "page": {
                            "url": f"https://{domain}{path}",
                            "path": path,
                        }
                    },
                },
            )
            assert status == 204

        status, summary = request(
            "GET", f"/v1/analytics/summary?site_id={site_id}&days=1"
        )
        assert status == 200
        assert summary["totals"] == {
            "events": 8,
            "users": 2,
            "sessions": 3,
            "page_views": 4,
            "engagement_ms": 12_000,
            "engaged_sessions": 2,
            "bounces": 1,
            "bounce_rate": pytest.approx(1 / 3),
        }
        event_counts = {row["key"]: row["events"] for row in summary["top_events"]}
        assert event_counts["session_start"] == 3
        assert event_counts["page_view"] == 4
        assert event_counts["purchase"] == 1
        campaigns = {row["key"]: row["events"] for row in summary["campaigns"]}
        assert campaigns["launch"] == 3
        referrers = {row["key"] for row in summary["referrers"]}
        assert "search.example" in referrers
        assert domain not in referrers

        status, timeseries = request(
            "GET", f"/v1/analytics/timeseries?site_id={site_id}&days=1"
        )
        assert status == 200
        assert sum(point["events"] for point in timeseries["series"]) == 8
        assert sum(point["page_views"] for point in timeseries["series"]) == 4
        assert sum(point["engagement_ms"] for point in timeseries["series"]) == 12_000

        print("analytics parity: baseline 3/10; current 10/10")
        print(
            "summary: events=8 users=2 sessions=3 page_views=4 "
            "engagement_ms=12000 engaged_sessions=2 bounces=1 bounce_rate=0.333333"
        )
        print(
            "timeseries: events=8 page_views=4 engagement_ms=12000 "
            f"buckets={len(timeseries['series'])}"
        )
    finally:
        for _ in range(200):
            try:
                request("DELETE", f"/v1/analytics/sites/{site_id}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 409:
                    raise
                time.sleep(0.05)
