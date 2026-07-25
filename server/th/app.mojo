"""Request dispatch for twoHelixes.

Routing splits three ways:

* pure-Mojo fast paths (health, readiness) that never touch Python;
* streaming paths, which the loop upgrades to SSE and drives from a Python
  worker thread; and
* everything else, handed to the Python application layer as one call.

The split matters because a health check hitting the Python interpreter would
make the liveness probe as slow as the slowest LLM call.
"""

from th.http import (
    METHOD_GET,
    METHOD_OPTIONS,
    Request,
    Response,
    Slice,
)


# Paths served by a Python worker thread over SSE.
def is_stream_path(buf: Span[UInt8, _], s: Slice) -> Bool:
    return (
        path_eq(buf, s, "/v1/query/stream")
        or path_eq(buf, s, "/v1/dashboard/stream")
        or path_eq(buf, s, "/v1/agent/stream")
        or path_eq(buf, s, "/v1/sql/stream")
    )


def path_eq(buf: Span[UInt8, _], s: Slice, literal: StringSlice) -> Bool:
    var lit = literal.as_bytes()
    if s.length != len(lit):
        return False
    for i in range(s.length):
        if buf[s.start + i] != lit[i]:
            return False
    return True


def path_starts(buf: Span[UInt8, _], s: Slice, literal: StringSlice) -> Bool:
    var lit = literal.as_bytes()
    if s.length < len(lit):
        return False
    for i in range(len(lit)):
        if buf[s.start + i] != lit[i]:
            return False
    return True


def json_response(mut resp: Response, status: Int, body: StringSlice):
    resp.status = status
    resp.add_header("Content-Type", "application/json; charset=utf-8")
    resp.add_header("X-Content-Type-Options", "nosniff")
    resp.set_body_str(body)


def text_response(mut resp: Response, status: Int, body: StringSlice):
    resp.status = status
    resp.add_header("Content-Type", "text/plain; charset=utf-8")
    resp.set_body_str(body)


def security_headers(mut resp: Response):
    resp.add_header("X-Content-Type-Options", "nosniff")
    resp.add_header("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.add_header("X-Frame-Options", "SAMEORIGIN")


def handle_fast(buf: Span[UInt8, _], req: Request, mut resp: Response) -> Bool:
    """Paths answered without entering Python. Returns True when handled."""
    if req.method == METHOD_GET and path_eq(buf, req.path, "/healthz"):
        text_response(resp, 200, "ok")
        return True

    if req.method == METHOD_GET and path_eq(buf, req.path, "/readyz"):
        text_response(resp, 200, "ready")
        return True

    if req.method == METHOD_OPTIONS:
        resp.status = 204
        resp.add_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        return True

    return False
