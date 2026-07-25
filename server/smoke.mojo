from th.http import (
    METHOD_POST,
    PARSE_OK,
    Request,
    Slice,
    Response,
    parse_request,
    serialize,
    slice_str,
)


def to_bytes(s: StringSlice) -> List[UInt8]:
    var out = List[UInt8]()
    var b = s.as_bytes()
    for i in range(len(b)):
        out.append(b[i])
    return out^


def main() raises:
    var raw = to_bytes(
        "POST /v1/query?dash=7 HTTP/1.1\r\n"
        "Host: twohelixes.com\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 17\r\n"
        "Connection: keep-alive\r\n"
        "\r\n"
        '{"q":"revenue"}\r\n'
    )
    var req = Request()
    var rc = parse_request(raw, len(raw), req)
    print("parse rc =", rc, "(want", PARSE_OK, ")")
    print("method  =", req.method, "(want", METHOD_POST, ")")
    print("path    =", slice_str(raw, req.path))
    print("query   =", slice_str(raw, req.query))
    print("headers =", req.header_count)
    print("ctype   =", slice_str(raw, req.header(raw, "content-type")))
    print("clen    =", req.content_length)
    print("body    =", slice_str(raw, req.body))
    print("keepalv =", req.keepalive)

    var resp = Response()
    resp.status = 200
    resp.add_header("Content-Type", "application/json")
    resp.set_body_str('{"ok":true}')
    var out = List[UInt8]()
    serialize(resp, out, False)
    print("--- wire ---")
    print(slice_str(out, Slice(0, len(out))))
