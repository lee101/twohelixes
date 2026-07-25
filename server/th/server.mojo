"""Level-triggered epoll event loop.

One loop per worker process; workers share the listen socket via SO_REUSEPORT
so the kernel load-balances accepts without a thundering herd. Each connection
owns two byte buffers that are reused across keep-alive requests.

Streaming responses are the reason this loop has a timeout rather than
blocking indefinitely: every tick it drains whatever SSE frames the Python
worker threads have buffered and writes them out. Polling is O(active
streams), not O(connections).
"""

from th.app import handle_fast, is_stream_path, security_headers
from th.http import (
    PARSE_ERROR,
    PARSE_INCOMPLETE,
    PARSE_OK,
    Request,
    Response,
    Slice,
    append_str,
    headers_json,
    method_name,
    parse_request,
    serialize,
    slice_str,
)
from th.libc import (
    AF_INET,
    EAGAIN,
    EINTR,
    EPOLLERR,
    EPOLLHUP,
    EPOLLIN,
    EPOLLOUT,
    EPOLLRDHUP,
    EPOLL_EVENT_SIZE,
    IPPROTO_TCP,
    SOCKADDR_IN_SIZE,
    SOCK_NONBLOCK,
    SOCK_STREAM,
    SOL_SOCKET,
    SO_REUSEADDR,
    SO_REUSEPORT,
    TCP_NODELAY,
    epoll_add,
    epoll_create,
    epoll_del,
    epoll_mod,
    epoll_wait,
    errno,
    event_fd,
    event_mask,
    fill_sockaddr_in,
    ignore_sigpipe,
    set_sockopt_int,
    sys_accept4,
    sys_bind,
    sys_close,
    sys_listen,
    sys_read,
    sys_socket,
    sys_write,
)
from th.py import Bridge

comptime ST_FREE: Int = 0
comptime ST_READ: Int = 1
comptime ST_WRITE: Int = 2
comptime ST_STREAM: Int = 3

comptime READ_CHUNK: Int = 16 * 1024
comptime MAX_BODY: Int = 64 * 1024 * 1024
comptime MAX_EVENTS: Int = 1024
comptime MAX_FDS: Int = 65536

# How often the loop wakes to drain SSE buffers when nothing else happens.
comptime IDLE_TICK_MS: Int32 = 25
comptime BUSY_TICK_MS: Int32 = 5


struct Conn(Movable):
    var state: Int
    var inbuf: List[UInt8]
    var inlen: Int
    var outbuf: List[UInt8]
    var outpos: Int
    var keepalive: Bool
    var requests: Int
    var stream_id: String

    def __init__(out self):
        self.state = ST_FREE
        self.inbuf = List[UInt8]()
        self.inlen = 0
        self.outbuf = List[UInt8]()
        self.outpos = 0
        self.keepalive = True
        self.requests = 0
        self.stream_id = String("")

    def open(mut self):
        self.state = ST_READ
        self.inlen = 0
        self.outpos = 0
        self.keepalive = True
        self.requests = 0
        self.stream_id = String("")
        self.inbuf.clear()
        self.outbuf.clear()

    def close(mut self):
        self.state = ST_FREE
        self.inlen = 0
        self.outpos = 0
        self.stream_id = String("")
        # Capacity is kept: the kernel reuses fd numbers quickly, so the next
        # connection on this slot inherits warm buffers.
        self.inbuf.clear()
        self.outbuf.clear()


def make_listener(port: UInt16, reuse_port: Bool) raises -> Int32:
    var fd = sys_socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK, 0)
    if fd < 0:
        raise Error("socket() failed, errno=" + String(errno()))
    _ = set_sockopt_int(fd, SOL_SOCKET, SO_REUSEADDR, 1)
    if reuse_port:
        _ = set_sockopt_int(fd, SOL_SOCKET, SO_REUSEPORT, 1)
    _ = set_sockopt_int(fd, IPPROTO_TCP, TCP_NODELAY, 1)

    var sa = List[UInt8](length=SOCKADDR_IN_SIZE, fill=0)
    fill_sockaddr_in(sa, port, 0)
    if sys_bind(fd, sa) != 0:
        var e = errno()
        _ = sys_close(fd)
        raise Error("bind(:" + String(port) + ") failed, errno=" + String(e))
    if sys_listen(fd, 4096) != 0:
        var e = errno()
        _ = sys_close(fd)
        raise Error("listen() failed, errno=" + String(e))
    return fd


struct Loop(Movable):
    var listen_fd: Int32
    var epfd: Int32
    var conns: List[Conn]
    var events: List[UInt8]
    var streaming: List[Int32]
    var bridge: Bridge
    var worker_id: Int
    var served: Int

    def __init__(
        out self,
        listen_fd: Int32,
        worker_id: Int,
        interp_dir: StringSlice,
        site_packages: StringSlice,
    ) raises:
        self.listen_fd = listen_fd
        self.epfd = epoll_create()
        if self.epfd < 0:
            raise Error("epoll_create1 failed, errno=" + String(errno()))
        # Connection slots grow on demand. Preallocating MAX_FDS of them is
        # both wasteful (fds are handed out lowest-first, so the table only
        # ever reaches peak concurrency) and pathological to compile: a
        # comptime-bounded loop that large gets unrolled.
        self.conns = List[Conn]()
        self.events = List[UInt8](length=MAX_EVENTS * EPOLL_EVENT_SIZE, fill=0)
        self.streaming = List[Int32]()
        self.bridge = Bridge()
        self.worker_id = worker_id
        self.served = 0
        if epoll_add(self.epfd, self.listen_fd, EPOLLIN) != 0:
            raise Error("epoll_add(listener) failed")
        self.bridge.boot(interp_dir, site_packages)

    def shutdown(mut self):
        _ = sys_close(self.epfd)
        _ = sys_close(self.listen_fd)

    def _ensure_slot(mut self, fd: Int32):
        """Grow the connection table so `fd` is addressable."""
        var want = Int(fd) + 1
        while len(self.conns) < want:
            self.conns.append(Conn())

    def _slot_state(self, fd: Int32) -> Int:
        var i = Int(fd)
        if i >= len(self.conns):
            return ST_FREE
        return self.conns[i].state

    def accept_ready(mut self):
        while True:
            var cfd = sys_accept4(self.listen_fd, SOCK_NONBLOCK)
            if cfd < 0:
                return
            if Int(cfd) >= MAX_FDS:
                _ = sys_close(cfd)
                continue
            self._ensure_slot(cfd)
            _ = set_sockopt_int(cfd, IPPROTO_TCP, TCP_NODELAY, 1)
            self.conns[Int(cfd)].open()
            if epoll_add(self.epfd, cfd, EPOLLIN | EPOLLRDHUP) != 0:
                self.conns[Int(cfd)].close()
                _ = sys_close(cfd)

    def drop(mut self, fd: Int32):
        var i = Int(fd)
        if self.conns[i].state == ST_STREAM:
            self._unregister_stream(fd)
            try:
                self.bridge.stream_cancel(self.conns[i].stream_id)
            except:
                pass
        _ = epoll_del(self.epfd, fd)
        self.conns[i].close()
        _ = sys_close(fd)

    def _unregister_stream(mut self, fd: Int32):
        var keep = List[Int32]()
        for k in range(len(self.streaming)):
            if self.streaming[k] != fd:
                keep.append(self.streaming[k])
        self.streaming = keep^

    def readable(mut self, fd: Int32) raises:
        """Read, then serve every complete request sitting in the buffer.

        Deliberately iterative: an earlier version had `readable` and `flush`
        call each other, which both risked unbounded stack depth on a
        pipelined connection and made the optimizer chase a mutual-recursion
        cycle through the whole request path.
        """
        var i = Int(fd)
        if self.conns[i].state != ST_READ:
            return

        while True:
            var have = self.conns[i].inlen
            var cap = len(self.conns[i].inbuf)
            if cap < have + READ_CHUNK:
                for _ in range(have + READ_CHUNK - cap):
                    self.conns[i].inbuf.append(0)
            var n = sys_read(fd, self.conns[i].inbuf, have, READ_CHUNK)
            if n == 0:
                self.drop(fd)
                return
            if n < 0:
                var e = errno()
                if e == EAGAIN:
                    break
                if e == EINTR:
                    continue
                self.drop(fd)
                return
            self.conns[i].inlen = have + n
            if self.conns[i].inlen > MAX_BODY:
                self.drop(fd)
                return

        # Serve every complete request already buffered (HTTP pipelining).
        while self.conns[i].inlen > 0:
            var req = Request()
            var rc = parse_request(self.conns[i].inbuf, self.conns[i].inlen, req)
            if rc == PARSE_INCOMPLETE:
                return
            if rc == PARSE_ERROR:
                self._bad_request(fd)
                return

            var resp = Response()
            var handled = handle_fast(self.conns[i].inbuf, req, resp)

            if not handled and is_stream_path(self.conns[i].inbuf, req.path):
                self._begin_stream(fd, req)
                return

            if not handled:
                self._dispatch_python(fd, req, resp)

            security_headers(resp)
            resp.keepalive = req.keepalive and resp.keepalive

            self.conns[i].outbuf.clear()
            serialize(resp, self.conns[i].outbuf, req.method == 5)
            self.conns[i].outpos = 0
            self.conns[i].keepalive = resp.keepalive
            self.conns[i].requests += 1
            self.served += 1

            var consumed = req.head_len + req.content_length
            var leftover = self.conns[i].inlen - consumed
            if leftover > 0:
                for k in range(leftover):
                    self.conns[i].inbuf[k] = self.conns[i].inbuf[consumed + k]
            self.conns[i].inlen = leftover if leftover > 0 else 0

            self.conns[i].state = ST_WRITE
            self.flush(fd)

            # Stop unless the socket drained and the connection survives.
            if self.conns[i].state != ST_READ:
                return

    def _bad_request(mut self, fd: Int32) raises:
        var i = Int(fd)
        self.conns[i].outbuf.clear()
        append_str(
            self.conns[i].outbuf,
            "HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
            "Connection: close\r\n\r\n",
        )
        self.conns[i].outpos = 0
        self.conns[i].keepalive = False
        self.conns[i].state = ST_WRITE
        self.flush(fd)

    def _dispatch_python(mut self, fd: Int32, req: Request, mut resp: Response):
        var i = Int(fd)
        var buf = Span(self.conns[i].inbuf)
        try:
            var result = self.bridge.dispatch(
                method_name(req.method),
                slice_str(buf, req.path),
                slice_str(buf, req.query),
                slice_str(buf, req.body),
                headers_json(buf, req),
            )
            resp.status = result[0]
            resp.add_header("Content-Type", result[1])
            if result[2] != "" and result[2] != "{}":
                _append_extra_headers(resp, result[2])
            resp.set_body_str(result[3])
        except e:
            resp.status = 500
            resp.add_header("Content-Type", "application/json; charset=utf-8")
            resp.set_body_str('{"error":"bridge_failure"}')

    def _begin_stream(mut self, fd: Int32, req: Request) raises:
        """Upgrade this connection to SSE and hand the work to Python."""
        var i = Int(fd)
        var buf = Span(self.conns[i].inbuf)

        var sid = String("")
        try:
            sid = self.bridge.stream_start(
                slice_str(buf, req.path),
                slice_str(buf, req.query),
                slice_str(buf, req.body),
                headers_json(buf, req),
            )
        except e:
            self._bad_request(fd)
            return

        self.conns[i].stream_id = sid^
        self.conns[i].outbuf.clear()
        append_str(
            self.conns[i].outbuf,
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/event-stream; charset=utf-8\r\n"
            "Cache-Control: no-cache, no-transform\r\n"
            "X-Accel-Buffering: no\r\n"
            "Connection: close\r\n\r\n"
            ": stream open\n\n",
        )
        self.conns[i].outpos = 0
        self.conns[i].keepalive = False
        self.conns[i].state = ST_STREAM
        self.conns[i].inlen = 0
        self.streaming.append(fd)
        self.flush_stream(fd)

    def flush(mut self, fd: Int32) raises:
        var i = Int(fd)
        while self.conns[i].outpos < len(self.conns[i].outbuf):
            var remaining = len(self.conns[i].outbuf) - self.conns[i].outpos
            var n = sys_write(
                fd, self.conns[i].outbuf, self.conns[i].outpos, remaining
            )
            if n < 0:
                var e = errno()
                if e == EAGAIN:
                    _ = epoll_mod(self.epfd, fd, EPOLLIN | EPOLLOUT | EPOLLRDHUP)
                    return
                if e == EINTR:
                    continue
                self.drop(fd)
                return
            self.conns[i].outpos += n

        if not self.conns[i].keepalive:
            self.drop(fd)
            return

        self.conns[i].outbuf.clear()
        self.conns[i].outpos = 0
        self.conns[i].state = ST_READ
        _ = epoll_mod(self.epfd, fd, EPOLLIN | EPOLLRDHUP)

    def flush_stream(mut self, fd: Int32) raises:
        """Write pending SSE bytes without ever closing on a full socket."""
        var i = Int(fd)
        while self.conns[i].outpos < len(self.conns[i].outbuf):
            var remaining = len(self.conns[i].outbuf) - self.conns[i].outpos
            var n = sys_write(
                fd, self.conns[i].outbuf, self.conns[i].outpos, remaining
            )
            if n < 0:
                var e = errno()
                if e == EAGAIN:
                    _ = epoll_mod(self.epfd, fd, EPOLLIN | EPOLLOUT | EPOLLRDHUP)
                    return
                if e == EINTR:
                    continue
                self.drop(fd)
                return
            self.conns[i].outpos += n

        # Fully drained: reclaim the buffer rather than growing it forever.
        self.conns[i].outbuf.clear()
        self.conns[i].outpos = 0
        _ = epoll_mod(self.epfd, fd, EPOLLIN | EPOLLRDHUP)

    def pump_streams(mut self) raises:
        """Drain every active stream's buffered frames onto its socket."""
        if len(self.streaming) == 0:
            return

        var active = List[Int32]()
        for k in range(len(self.streaming)):
            var fd = self.streaming[k]
            var i = Int(fd)
            if self.conns[i].state != ST_STREAM:
                continue

            var chunk = String("")
            var done = False
            try:
                var polled = self.bridge.stream_poll(self.conns[i].stream_id)
                chunk = polled[0]
                done = polled[1]
            except e:
                done = True

            if chunk != "":
                append_str(self.conns[i].outbuf, chunk)
                self.flush_stream(fd)

            if done:
                if self.conns[i].outpos >= len(self.conns[i].outbuf):
                    self.conns[i].state = ST_WRITE
                    self.conns[i].keepalive = False
                    self.drop(fd)
                    continue
            if self.conns[i].state == ST_STREAM:
                active.append(fd)

        self.streaming = active^

    def run(mut self) raises:
        ignore_sigpipe()
        while True:
            var timeout = BUSY_TICK_MS if len(self.streaming) > 0 else IDLE_TICK_MS
            var n = epoll_wait(self.epfd, self.events, MAX_EVENTS, timeout)
            if n < 0:
                if errno() == EINTR:
                    continue
                raise Error("epoll_wait failed, errno=" + String(errno()))

            for k in range(Int(n)):
                var fd = event_fd(self.events, k)
                var mask = event_mask(self.events, k)
                if fd == self.listen_fd:
                    self.accept_ready()
                    continue
                if Int(fd) >= MAX_FDS:
                    continue
                if self._slot_state(fd) == ST_FREE:
                    continue
                if (mask & (EPOLLERR | EPOLLHUP)) != 0:
                    self.drop(fd)
                    continue
                if self._slot_state(fd) == ST_STREAM:
                    # A read event on a streaming socket means the client hung
                    # up; there is nothing else it can send us.
                    if (mask & EPOLLOUT) != 0:
                        self.flush_stream(fd)
                    elif (mask & EPOLLRDHUP) != 0:
                        self.drop(fd)
                    continue
                if (mask & EPOLLOUT) != 0 and self._slot_state(fd) == ST_WRITE:
                    self.flush(fd)
                    continue
                if (mask & EPOLLIN) != 0:
                    self.readable(fd)
                elif (mask & EPOLLRDHUP) != 0:
                    self.drop(fd)

            self.pump_streams()


def _append_extra_headers(mut resp: Response, extra: StringSlice):
    """Append headers the Python layer asked for, given as a flat JSON object.

    Values are emitted verbatim; the Python side is the only producer and it
    controls what goes in here.
    """
    var b = extra.as_bytes()
    var i = 0
    var name = String("")
    var value = String("")
    var in_string = False
    var reading_name = True
    var current = String("")

    while i < len(b):
        var c = b[i]
        if c == 34:  # quote
            if in_string:
                if reading_name:
                    name = current
                else:
                    value = current
                    if name != "" and _header_is_safe(value):
                        resp.add_header(name, value)
                    name = String("")
                current = String("")
                in_string = False
            else:
                in_string = True
                current = String("")
        elif in_string:
            current += chr(Int(c))
        elif c == 58:  # colon
            reading_name = False
        elif c == 44:  # comma
            reading_name = True
        i += 1


def _header_is_safe(value: StringSlice) -> Bool:
    """Reject CR/LF so a header value can never inject another header."""
    var b = value.as_bytes()
    for i in range(len(b)):
        if b[i] == 13 or b[i] == 10:
            return False
    return True
