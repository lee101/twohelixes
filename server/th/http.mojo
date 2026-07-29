"""HTTP/1.1 request parsing and response building.

The parser never copies: it records byte offsets into the connection's read
buffer and hands out `StringSlice`s on demand. A request is only re-parsed when
more bytes arrive, so a partial read costs one scan of the new tail.
"""

comptime METHOD_UNKNOWN: Int = 0
comptime METHOD_GET: Int = 1
comptime METHOD_POST: Int = 2
comptime METHOD_PUT: Int = 3
comptime METHOD_DELETE: Int = 4
comptime METHOD_HEAD: Int = 5
comptime METHOD_OPTIONS: Int = 6
comptime METHOD_PATCH: Int = 7

comptime PARSE_INCOMPLETE: Int = 0
comptime PARSE_OK: Int = 1
comptime PARSE_ERROR: Int = 2

comptime MAX_HEADERS: Int = 64
comptime MAX_HEADER_BYTES: Int = 32 * 1024


struct Slice(ImplicitlyCopyable, Movable):
    """A (start, length) pair into the connection read buffer."""

    var start: Int
    var length: Int

    def __init__(out self):
        self.start = 0
        self.length = 0

    def __init__(out self, start: Int, length: Int):
        self.start = start
        self.length = length

    def is_empty(self) -> Bool:
        return self.length <= 0


struct HeaderRef(ImplicitlyCopyable, Movable):
    var name: Slice
    var value: Slice

    def __init__(out self):
        self.name = Slice()
        self.value = Slice()

    def __init__(out self, name: Slice, value: Slice):
        self.name = name
        self.value = value


def _lower(c: UInt8) -> UInt8:
    if c >= 65 and c <= 90:
        return c + 32
    return c


def ascii_eq_ci(buf: Span[UInt8, _], s: Slice, literal: StringSlice) -> Bool:
    """Case-insensitive compare of a buffer slice against an ASCII literal."""
    var lit = literal.as_bytes()
    if s.length != len(lit):
        return False
    for i in range(s.length):
        if _lower(buf[s.start + i]) != _lower(lit[i]):
            return False
    return True


def slice_str(buf: Span[UInt8, _], s: Slice) -> String:
    """The bytes of `s` as a String, in one copy.

    This used to build the string a character at a time with `chr(byte)`, which
    is a reallocation per byte on a path this takes for every request path,
    query and body - a megabyte upload paid a million of them. It was also
    wrong for anything but ASCII: `chr` of each byte is a latin-1 decode, so a
    UTF-8 body arrived at the Python layer as mojibake ("café" -> "cafÃ©").
    The bytes are already UTF-8; they are handed over as they are.
    """
    if s.length <= 0:
        return String("")
    return String(
        StringSlice(unsafe_from_utf8=buf[s.start : s.start + s.length])
    )


struct Request(Movable):
    """Parsed view over a request that lives in the connection's read buffer."""

    var method: Int
    var target: Slice
    var path: Slice
    var query: Slice
    var headers: List[HeaderRef]
    var header_count: Int
    var body: Slice
    var content_length: Int
    var keepalive: Bool
    var head_len: Int
    var expect_continue: Bool

    def __init__(out self):
        self.method = METHOD_UNKNOWN
        self.target = Slice()
        self.path = Slice()
        self.query = Slice()
        self.headers = List[HeaderRef]()
        self.header_count = 0
        self.body = Slice()
        self.content_length = 0
        self.keepalive = True
        self.head_len = 0
        self.expect_continue = False

    def reset(mut self):
        self.method = METHOD_UNKNOWN
        self.target = Slice()
        self.path = Slice()
        self.query = Slice()
        self.headers.clear()
        self.header_count = 0
        self.body = Slice()
        self.content_length = 0
        self.keepalive = True
        self.head_len = 0
        self.expect_continue = False

    def header(self, buf: Span[UInt8, _], name: StringSlice) -> Slice:
        for i in range(self.header_count):
            if ascii_eq_ci(buf, self.headers[i].name, name):
                return self.headers[i].value
        return Slice()

    def has_header(self, buf: Span[UInt8, _], name: StringSlice) -> Bool:
        return not self.header(buf, name).is_empty()


def parse_method(buf: Span[UInt8, _], s: Slice) -> Int:
    if s.length == 3:
        if buf[s.start] == 71 and buf[s.start + 1] == 69:
            return METHOD_GET
        if buf[s.start] == 80 and buf[s.start + 1] == 85:
            return METHOD_PUT
    if s.length == 4:
        if buf[s.start] == 80 and buf[s.start + 1] == 79:
            return METHOD_POST
        if buf[s.start] == 72:
            return METHOD_HEAD
    if s.length == 5 and buf[s.start] == 80:
        return METHOD_PATCH
    if s.length == 6 and buf[s.start] == 68:
        return METHOD_DELETE
    if s.length == 7 and buf[s.start] == 79:
        return METHOD_OPTIONS
    return METHOD_UNKNOWN


def parse_request(buf: Span[UInt8, _], avail: Int, mut req: Request) -> Int:
    """Parse a full request out of `buf[0:avail]`.

    Returns PARSE_INCOMPLETE if more bytes are needed, PARSE_OK when both head
    and body are present, PARSE_ERROR on anything malformed or oversized.
    """
    req.reset()

    # Locate end of head.
    var head_end = -1
    var i = 0
    while i + 3 < avail:
        if (
            buf[i] == 13
            and buf[i + 1] == 10
            and buf[i + 2] == 13
            and buf[i + 3] == 10
        ):
            head_end = i + 4
            break
        i += 1
    if head_end < 0:
        if avail > MAX_HEADER_BYTES:
            return PARSE_ERROR
        return PARSE_INCOMPLETE

    # Request line: METHOD SP TARGET SP VERSION CRLF
    var p = 0
    var m_start = 0
    while p < head_end and buf[p] != 32:
        p += 1
    if p >= head_end:
        return PARSE_ERROR
    req.method = parse_method(buf, Slice(m_start, p - m_start))
    p += 1

    var t_start = p
    while p < head_end and buf[p] != 32:
        p += 1
    if p >= head_end:
        return PARSE_ERROR
    req.target = Slice(t_start, p - t_start)

    # Split target into path and query.
    var q = t_start
    var qmark = -1
    while q < t_start + req.target.length:
        if buf[q] == 63:
            qmark = q
            break
        q += 1
    if qmark < 0:
        req.path = Slice(t_start, req.target.length)
        req.query = Slice()
    else:
        req.path = Slice(t_start, qmark - t_start)
        req.query = Slice(qmark + 1, t_start + req.target.length - qmark - 1)

    # Version, then skip to CRLF.
    var v_start = p + 1
    while p < head_end and buf[p] != 13:
        p += 1
    var http_11 = False
    if p - v_start >= 8:
        http_11 = buf[v_start + 7] == 49  # "HTTP/1.1"
    req.keepalive = http_11
    p += 2  # skip CRLF

    # Headers.
    while p + 1 < head_end:
        if buf[p] == 13 and buf[p + 1] == 10:
            p += 2
            break
        var n_start = p
        while p < head_end and buf[p] != 58:
            p += 1
        if p >= head_end:
            return PARSE_ERROR
        var name = Slice(n_start, p - n_start)
        p += 1
        while p < head_end and (buf[p] == 32 or buf[p] == 9):
            p += 1
        var v2_start = p
        while p < head_end and buf[p] != 13:
            p += 1
        var value = Slice(v2_start, p - v2_start)
        p += 2  # CRLF

        if req.header_count >= MAX_HEADERS:
            return PARSE_ERROR
        req.headers.append(HeaderRef(name, value))
        req.header_count += 1

    req.head_len = head_end

    # Connection semantics.
    var conn = req.header(buf, "connection")
    if not conn.is_empty():
        if ascii_eq_ci(buf, conn, "close"):
            req.keepalive = False
        elif ascii_eq_ci(buf, conn, "keep-alive"):
            req.keepalive = True

    var expect = req.header(buf, "expect")
    if not expect.is_empty() and ascii_eq_ci(buf, expect, "100-continue"):
        req.expect_continue = True

    # Body.
    var cl = req.header(buf, "content-length")
    if cl.is_empty():
        req.content_length = 0
        req.body = Slice(head_end, 0)
        return PARSE_OK

    var n = 0
    for k in range(cl.length):
        var c = buf[cl.start + k]
        if c < 48 or c > 57:
            return PARSE_ERROR
        n = n * 10 + Int(c - 48)
    req.content_length = n
    if avail - head_end < n:
        return PARSE_INCOMPLETE
    req.body = Slice(head_end, n)
    return PARSE_OK


# --------------------------------------------------------------------------
# Response building
# --------------------------------------------------------------------------


def status_text(code: Int) -> StringSlice[ImmStaticOrigin]:
    if code == 200:
        return "OK"
    if code == 201:
        return "Created"
    if code == 204:
        return "No Content"
    if code == 206:
        return "Partial Content"
    if code == 301:
        return "Moved Permanently"
    if code == 302:
        return "Found"
    if code == 303:
        return "See Other"
    if code == 304:
        return "Not Modified"
    if code == 400:
        return "Bad Request"
    if code == 401:
        return "Unauthorized"
    if code == 402:
        return "Payment Required"
    if code == 403:
        return "Forbidden"
    if code == 404:
        return "Not Found"
    if code == 405:
        return "Method Not Allowed"
    if code == 409:
        return "Conflict"
    if code == 413:
        return "Payload Too Large"
    if code == 415:
        return "Unsupported Media Type"
    if code == 422:
        return "Unprocessable Entity"
    if code == 429:
        return "Too Many Requests"
    if code == 500:
        return "Internal Server Error"
    if code == 502:
        return "Bad Gateway"
    if code == 503:
        return "Service Unavailable"
    if code == 504:
        return "Gateway Timeout"
    return "Unknown"


struct Response(Movable):
    """A response accumulated as bytes, written out by the event loop."""

    var status: Int
    var headers: String
    var body: List[UInt8]
    var keepalive: Bool
    var streaming: Bool

    def __init__(out self):
        self.status = 200
        self.headers = String("")
        self.body = List[UInt8]()
        self.keepalive = True
        self.streaming = False

    def reset(mut self):
        self.status = 200
        self.headers = String("")
        self.body.clear()
        self.keepalive = True
        self.streaming = False

    def add_header(mut self, name: StringSlice, value: StringSlice):
        self.headers += name
        self.headers += ": "
        self.headers += value
        self.headers += "\r\n"

    def set_body_str(mut self, s: StringSlice):
        self.body.clear()
        self.body.extend(s.as_bytes())

    def set_body_bytes(mut self, s: Span[UInt8, _]):
        self.body.clear()
        self.body.extend(s)

    def append_body(mut self, s: StringSlice):
        self.body.extend(s.as_bytes())


def append_str(mut out: List[UInt8], s: StringSlice):
    # `extend` is one bounds check and one memcpy. Appending byte by byte cost
    # a bounds check and a capacity test per byte, and every response body in
    # the product goes through here.
    out.extend(s.as_bytes())


def _needs_escape(b: Span[UInt8, _]) -> Bool:
    for i in range(len(b)):
        var c = b[i]
        if c == 34 or c == 92 or c < 32:
            return True
    return False


def _escape_into(mut out: List[UInt8], b: Span[UInt8, _], lower: Bool):
    """Append `b` to `out`, JSON-escaped, optionally ASCII-lowercased.

    Almost no header value contains a character that needs escaping, so the
    common case is a single `extend` and no per-byte work at all. Bytes above
    127 are passed through: they are already UTF-8 and JSON takes them as they
    are.
    """
    if not lower and not _needs_escape(b):
        out.extend(b)
        return
    for i in range(len(b)):
        var c = _lower(b[i]) if lower else b[i]
        if c == 34:
            out.extend('\\"'.as_bytes())
        elif c == 92:
            out.extend("\\\\".as_bytes())
        elif c == 10:
            out.extend("\\n".as_bytes())
        elif c == 13:
            out.extend("\\r".as_bytes())
        elif c == 9:
            out.extend("\\t".as_bytes())
        elif c < 32:
            out.extend("\\u00".as_bytes())
            var hi = c >> 4
            var lo = c & 15
            out.append(UInt8(48 + hi) if hi < 10 else UInt8(87 + hi))
            out.append(UInt8(48 + lo) if lo < 10 else UInt8(87 + lo))
        else:
            out.append(c)


def json_escape(value: StringSlice) -> String:
    """Escape a string for embedding in JSON."""
    var b = value.as_bytes()
    if not _needs_escape(b):
        return String(value)
    var out = List[UInt8]()
    out.reserve(len(b) + 16)
    _escape_into(out, b, False)
    return String(StringSlice(unsafe_from_utf8=Span(out)))


def headers_json(buf: Span[UInt8, _], req: Request) -> String:
    """Serialize request headers as a JSON object for the Python layer.

    Built as bytes and converted once. The previous version made two Strings
    per header - a `slice_str` copy and a `.lower()` copy - and then grew a
    third by concatenation, so a 20-header request allocated more than 40
    times before the Python layer saw anything.
    """
    var out = List[UInt8]()
    out.reserve(512)
    out.append(123)  # {
    for i in range(req.header_count):
        if i > 0:
            out.append(44)  # ,
        out.append(34)
        var name = req.headers[i].name
        _escape_into(out, buf[name.start : name.start + name.length], True)
        out.extend('":"'.as_bytes())
        var value = req.headers[i].value
        _escape_into(out, buf[value.start : value.start + value.length], False)
        out.append(34)
    out.append(125)  # }
    return String(StringSlice(unsafe_from_utf8=Span(out)))


def method_name(method: Int) -> StringSlice[ImmStaticOrigin]:
    if method == METHOD_GET:
        return "GET"
    if method == METHOD_POST:
        return "POST"
    if method == METHOD_PUT:
        return "PUT"
    if method == METHOD_DELETE:
        return "DELETE"
    if method == METHOD_HEAD:
        return "HEAD"
    if method == METHOD_OPTIONS:
        return "OPTIONS"
    if method == METHOD_PATCH:
        return "PATCH"
    return "GET"


def append_int(mut out: List[UInt8], value: Int):
    if value == 0:
        out.append(48)
        return
    var v = value
    var neg = v < 0
    if neg:
        v = -v
    var digits = InlineArray[UInt8, 24](fill=0)
    var n = 0
    while v > 0:
        digits[n] = UInt8(48 + (v % 10))
        v //= 10
        n += 1
    if neg:
        out.append(45)
    var k = n - 1
    while k >= 0:
        out.append(digits[k])
        k -= 1


def serialize(resp: Response, mut out: List[UInt8], head_only: Bool):
    """Write the full HTTP/1.1 response into `out`."""
    append_str(out, "HTTP/1.1 ")
    append_int(out, resp.status)
    append_str(out, " ")
    append_str(out, status_text(resp.status))
    append_str(out, "\r\n")
    append_str(out, resp.headers)
    if not resp.streaming:
        append_str(out, "Content-Length: ")
        append_int(out, len(resp.body))
        append_str(out, "\r\n")
    if resp.keepalive:
        append_str(out, "Connection: keep-alive\r\n")
    else:
        append_str(out, "Connection: close\r\n")
    append_str(out, "\r\n")
    if not head_only:
        for i in range(len(resp.body)):
            out.append(resp.body[i])
