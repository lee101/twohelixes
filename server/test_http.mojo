"""Assertions over the HTTP layer, in the language it is written in.

`smoke.mojo` prints; nothing reads what it prints, so it cannot fail a build.
These are the cases that broke - or would have broken silently - when the parse
and serialize paths were rewritten to stop allocating per byte.

    pixi run mojo build server/test_http.mojo -I server -o build/test_http
    build/test_http

Raises if anything failed, so the process exits non-zero.
"""

from th.http import (
    METHOD_GET,
    METHOD_POST,
    PARSE_OK,
    Request,
    Response,
    Slice,
    append_str,
    headers_json,
    json_escape,
    parse_request,
    serialize,
    slice_str,
)


def check(name: StringSlice, got: StringSlice, want: StringSlice) -> Int:
    """Returns 1 on failure so callers can sum; Mojo has no mutable global."""
    if got == want:
        print("  ok   ", name)
        return 0
    print("  FAIL ", name)
    print("    got  =", got)
    print("    want =", want)
    return 1


def check_int(name: StringSlice, got: Int, want: Int) -> Int:
    return check(name, String(got), String(want))


def to_bytes(s: StringSlice) -> List[UInt8]:
    var buf = List[UInt8]()
    buf.extend(s.as_bytes())
    return buf^


def whole(buf: List[UInt8]) -> String:
    return slice_str(buf, Slice(0, len(buf)))


def test_slice_str_is_byte_exact() -> Int:
    print("slice_str")
    var bad = 0
    var raw = to_bytes("café ☕ 日本語")
    # The old implementation built the string with chr(byte), which is a
    # latin-1 decode: every byte above 127 became its own codepoint, so a
    # 3-byte character reached the Python layer as three characters.
    bad += check("utf-8 survives the round trip", whole(raw), "café ☕ 日本語")
    bad += check_int("byte length unchanged", whole(raw).byte_length(), len(raw))
    bad += check("empty slice", slice_str(raw, Slice(0, 0)), "")
    bad += check("negative length is empty", slice_str(raw, Slice(2, -1)), "")
    return bad


def test_parse_and_slice_a_request() -> Int:
    print("parse_request")
    var bad = 0
    var body = '{"q":"café ☕"}'
    var raw = to_bytes(
        "POST /v1/query?dash=7&q=caf%C3%A9 HTTP/1.1\r\n"
        "Host: twohelixes.com\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 17\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
        + body
    )
    var req = Request()
    var rc = parse_request(raw, len(raw), req)
    bad += check_int("rc", rc, PARSE_OK)
    bad += check_int("method", req.method, METHOD_POST)
    bad += check("path", slice_str(raw, req.path), "/v1/query")
    bad += check("query", slice_str(raw, req.query), "dash=7&q=caf%C3%A9")
    bad += check(
        "content-type",
        slice_str(raw, req.header(raw, "content-type")),
        "application/json",
    )
    bad += check("body", slice_str(raw, req.body), body)
    bad += check("keepalive", String(req.keepalive), "True")
    return bad


def test_headers_json() -> Int:
    print("headers_json")
    var bad = 0
    var raw = to_bytes(
        "GET / HTTP/1.1\r\nHost: twohelixes.com\r\nX-Mixed-Case: Value\r\n\r\n"
    )
    var req = Request()
    _ = parse_request(raw, len(raw), req)
    bad += check(
        "names lowercased, values not",
        headers_json(raw, req),
        '{"host":"twohelixes.com","x-mixed-case":"Value"}',
    )

    var quoted = to_bytes('GET / HTTP/1.1\r\nX-Q: he said "hi"\\ok\r\n\r\n')
    var req2 = Request()
    _ = parse_request(quoted, len(quoted), req2)
    bad += check(
        "quote and backslash escaped",
        headers_json(quoted, req2),
        '{"x-q":"he said \\"hi\\"\\\\ok"}',
    )

    var utf8 = to_bytes("GET / HTTP/1.1\r\nX-U: café\r\n\r\n")
    var req3 = Request()
    _ = parse_request(utf8, len(utf8), req3)
    bad += check(
        "utf-8 header passes through", headers_json(utf8, req3), '{"x-u":"café"}'
    )
    return bad


def test_json_escape() -> Int:
    print("json_escape")
    var bad = 0
    bad += check("plain string is untouched", json_escape("hello"), "hello")
    bad += check("quote", json_escape('a"b'), 'a\\"b')
    bad += check("backslash", json_escape("a\\b"), "a\\\\b")
    bad += check("newline", json_escape("a\nb"), "a\\nb")
    bad += check("carriage return", json_escape("a\rb"), "a\\rb")
    bad += check("tab", json_escape("a\tb"), "a\\tb")
    bad += check("utf-8 is not escaped", json_escape("café"), "café")
    return bad


def test_append_str_and_body() -> Int:
    print("append_str / body")
    var bad = 0
    var buf = List[UInt8]()
    append_str(buf, "abc")
    append_str(buf, "")
    append_str(buf, "déf")
    bad += check("appended bytes", whole(buf), "abcdéf")

    var resp = Response()
    resp.set_body_str("café")
    bad += check_int("body byte length", len(resp.body), 5)
    resp.append_body("!")
    bad += check("body after append", whole(resp.body), "café!")
    resp.set_body_str("x")
    bad += check_int("set_body_str replaces", len(resp.body), 1)
    return bad


def test_serialize_round_trip() -> Int:
    print("serialize")
    var bad = 0
    var resp = Response()
    resp.status = 200
    resp.add_header("Content-Type", "application/json; charset=utf-8")
    resp.set_body_str('{"ok":true}')

    var wire = List[UInt8]()
    serialize(resp, wire, False)
    var text = whole(wire)
    bad += check("status line", text[byte=0:15], "HTTP/1.1 200 OK")
    var n = text.byte_length()
    bad += check("body is last", text[byte = n - 11 : n], '{"ok":true}')

    # A HEAD response carries the headers of the GET and none of its body.
    var head = List[UInt8]()
    serialize(resp, head, True)
    var head_text = whole(head)
    var m = head_text.byte_length()
    var tail = String(head_text[byte = m - 11 : m])
    bad += check("HEAD omits the body", String(tail != '{"ok":true}'), "True")
    bad += check_int("HEAD is shorter", Int(m < n), 1)
    return bad


def test_pipelined_requests_parse_independently() -> Int:
    print("pipelining")
    var bad = 0
    var raw = to_bytes(
        "GET /a HTTP/1.1\r\nHost: h\r\n\r\nGET /b HTTP/1.1\r\nHost: h\r\n\r\n"
    )
    var first = Request()
    _ = parse_request(raw, len(raw), first)
    bad += check("first path", slice_str(raw, first.path), "/a")
    bad += check_int("first method", first.method, METHOD_GET)

    # The loop shifts the leftover down and reparses; do the same here.
    var consumed = first.head_len + first.content_length
    var rest = List[UInt8]()
    rest.extend(Span(raw)[consumed : len(raw)])
    var second = Request()
    _ = parse_request(rest, len(rest), second)
    bad += check("second path", slice_str(rest, second.path), "/b")
    return bad


def main() raises:
    var bad = 0
    bad += test_slice_str_is_byte_exact()
    bad += test_parse_and_slice_a_request()
    bad += test_headers_json()
    bad += test_json_escape()
    bad += test_append_str_and_body()
    bad += test_serialize_round_trip()
    bad += test_pipelined_requests_parse_independently()
    print("")
    if bad > 0:
        print(bad, "failed")
        raise Error("http tests failed")
    print("all http tests passed")
