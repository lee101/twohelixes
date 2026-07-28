"""Python-side routing.

The Mojo loop already decided this request needs Python; everything here is
product logic. Routes are registered as (method, pattern) with `{param}`
segments, matched by a small trie-free scan - route counts are in the dozens,
so a linear scan over pre-split segments beats building anything cleverer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

log = logging.getLogger("twohelixes.router")

Handler = Callable[["Context"], "Result"]
StreamHandler = Callable[[Any, "Context"], None]


@dataclass
class Result:
    status: int = 200
    body: Any = None
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)

    def render(self) -> tuple[str, str, str, str]:
        if self.content_type.startswith("application/json"):
            payload = json.dumps(self.body, default=str, separators=(",", ":"))
        elif self.body is None:
            payload = ""
        elif isinstance(self.body, (bytes, bytearray)):
            payload = self.body.decode("utf-8", "replace")
        else:
            payload = str(self.body)
        extra = json.dumps(self.headers) if self.headers else ""
        return (str(self.status), self.content_type, extra, payload)


def json_result(body: Any, status: int = 200, **headers: str) -> Result:
    return Result(status=status, body=body, headers=dict(headers))


def error(status: int, code: str, detail: str = "", **extra: Any) -> Result:
    payload: dict[str, Any] = {"error": code}
    if detail:
        payload["detail"] = detail
    payload.update(extra)
    return Result(status=status, body=payload)


def html(body: str, status: int = 200, **headers: str) -> Result:
    return Result(
        status=status,
        body=body,
        content_type="text/html; charset=utf-8",
        headers=dict(headers),
    )


@dataclass
class Context:
    method: str
    path: str
    raw_query: str
    raw_body: str
    headers: dict[str, str]
    params: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    _json: Any = None
    _json_parsed: bool = False
    user: Any = None

    # -- accessors ---------------------------------------------------------

    def q(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        if not values:
            return default
        return values[0]

    def q_int(self, name: str, default: int) -> int:
        raw = self.q(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    def q_bool(self, name: str, default: bool = False) -> bool:
        raw = self.q(name)
        if raw is None:
            return default
        return raw.lower() in ("1", "true", "yes", "on")

    @property
    def json(self) -> Any:
        if not self._json_parsed:
            self._json_parsed = True
            if self.raw_body:
                try:
                    self._json = json.loads(self.raw_body)
                except (ValueError, TypeError):
                    self._json = None
        return self._json

    def field(self, name: str, default: Any = None) -> Any:
        data = self.json
        if isinstance(data, dict) and name in data:
            return data[name]
        return self.q(name, default)

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def cookie(self, name: str) -> str | None:
        raw = self.header("cookie")
        if not raw:
            return None
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return unquote(value)
        return None

    @property
    def client_ip(self) -> str:
        forwarded = self.header("cf-connecting-ip") or self.header("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.header("x-real-ip", "0.0.0.0")


# --------------------------------------------------------------------------
# Registration
# --------------------------------------------------------------------------

_routes: list[tuple[str, list[str], Handler]] = []
_stream_routes: dict[str, StreamHandler] = {}
_built = False


def route(method: str, pattern: str) -> Callable[[Handler], Handler]:
    def decorate(fn: Handler) -> Handler:
        _routes.append((method.upper(), _split(pattern), fn))
        return fn

    return decorate


def get(pattern: str) -> Callable[[Handler], Handler]:
    return route("GET", pattern)


def post(pattern: str) -> Callable[[Handler], Handler]:
    return route("POST", pattern)


def put(pattern: str) -> Callable[[Handler], Handler]:
    return route("PUT", pattern)


def delete(pattern: str) -> Callable[[Handler], Handler]:
    return route("DELETE", pattern)


def patch(pattern: str) -> Callable[[Handler], Handler]:
    return route("PATCH", pattern)


def stream(pattern: str) -> Callable[[StreamHandler], StreamHandler]:
    def decorate(fn: StreamHandler) -> StreamHandler:
        _stream_routes[pattern] = fn
        return fn

    return decorate


def _split(pattern: str) -> list[str]:
    return [seg for seg in pattern.strip("/").split("/") if seg != ""]


def build() -> None:
    """Import every route module exactly once."""
    global _built
    if _built:
        return
    from twohelixes.routes import (  # noqa: F401
        agents,
        analytics,
        billing,
        builder,
        charts,
        connectors,
        dashboards,
        datasets,
        library,
        notebooks,
        pages,
        query,
        seo,
        sql,
        static_files,
        teams,
    )

    _built = True
    log.info("router: %d routes, %d stream routes", len(_routes), len(_stream_routes))


def stream_handler(path: str) -> StreamHandler | None:
    return _stream_routes.get(path)


def build_context(
    method: str, path: str, query: str, body: str, headers: str
) -> Context:
    try:
        parsed_headers = json.loads(headers) if headers else {}
    except ValueError:
        parsed_headers = {}
    ctx = Context(
        method=method.upper(),
        path=path,
        raw_query=query,
        raw_body=body,
        headers={str(k).lower(): str(v) for k, v in parsed_headers.items()},
        query=parse_qs(query, keep_blank_values=True) if query else {},
    )
    from twohelixes import auth

    ctx.user = auth.identify(ctx)
    return ctx


def _match(method: str, segments: list[str]) -> tuple[Handler, dict[str, str]] | None:
    for route_method, pattern, handler in _routes:
        if route_method != method:
            continue
        if len(pattern) != len(segments):
            continue
        params: dict[str, str] = {}
        ok = True
        for want, got in zip(pattern, segments):
            if want.startswith("{") and want.endswith("}"):
                params[want[1:-1]] = unquote(got)
            elif want != got:
                ok = False
                break
        if ok:
            return handler, params
    return None


def handle(
    method: str, path: str, query: str, body: str, headers: str
) -> tuple[str, str, str, str]:
    build()
    ctx = build_context(method, path, query, body, headers)
    segments = _split(path)

    # HEAD is GET without a body. The Mojo layer already suppresses the body
    # on serialize, so matching it against GET routes is all that is needed -
    # and without this, every uptime monitor and link checker gets a 405.
    lookup_method = "GET" if ctx.method == "HEAD" else ctx.method

    found = _match(lookup_method, segments)
    if found is None:
        # Distinguish 405 from 404 so clients get a useful signal.
        for other in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            if other != lookup_method and _match(other, segments):
                return error(405, "method_not_allowed").render()
        return error(404, "not_found", path).render()

    handler, params = found
    ctx.params = params

    try:
        result = handler(ctx)
    except Exception as exc:  # noqa: BLE001
        from twohelixes import auth

        # A missing session is a 401, not a server error. Without this every
        # unauthenticated call to a protected route returned 500 with a stack
        # trace in the detail.
        if isinstance(exc, auth.Unauthorized):
            return error(401, "signin_required", "Sign in to continue.").render()
        raise

    if not isinstance(result, Result):
        result = json_result(result)
    return result.render()
