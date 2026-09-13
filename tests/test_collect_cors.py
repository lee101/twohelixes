"""The public collector must remain diagnosable when its handler fails."""

from __future__ import annotations

import json
from typing import Any

import pytest

from twohelixes import api, router


def test_collect_internal_error_keeps_cors_headers(monkeypatch: Any) -> None:
    def fail(*_args: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(router, "handle", fail)
    origin = "https://netwrck.local:8443"

    status, _ctype, raw_headers, body = api.dispatch(
        "POST",
        "/v1/collect",
        "",
        "{}",
        json.dumps({"origin": origin, "content-type": "text/plain"}),
    )

    headers = json.loads(raw_headers)
    assert status == "500"
    assert headers["Access-Control-Allow-Origin"] == origin
    assert headers["Vary"] == "Origin"
    assert json.loads(body)["error"] == "internal_error"


def test_unrelated_internal_error_does_not_gain_public_cors(monkeypatch: Any) -> None:
    def fail(*_args: Any) -> Any:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(router, "handle", fail)
    status, _ctype, raw_headers, _body = api.dispatch(
        "POST", "/v1/query", "", "{}", json.dumps({"origin": "https://example.com"})
    )

    assert status == "500"
    assert raw_headers == ""


@pytest.mark.parametrize(
    "path",
    [
        "/v1/batch",
        "/v1/track",
        "/mp/collect",
        "/track",
        "/engage",
        "/groups",
        "/2/httpapi",
        "/batch",
        "/identify",
    ],
)
def test_compatible_collectors_keep_cors_on_failure(
    path: str, monkeypatch: Any
) -> None:
    monkeypatch.setattr(
        router,
        "handle",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    status, _ctype, raw_headers, _body = api.dispatch(
        "POST", path, "", "{}", json.dumps({"origin": "https://sdk.example"})
    )
    headers = json.loads(raw_headers)
    assert status == "500"
    assert headers["Access-Control-Allow-Origin"] == "https://sdk.example"
    assert headers["Access-Control-Allow-Headers"] == "authorization, content-type"
