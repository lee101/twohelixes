"""Thin libc bindings for the twoHelixes HTTP core.

Everything on the request hot path goes through these calls: no Python, no
allocations beyond the buffers the caller already owns.
"""

from std.ffi import external_call
from std.memory import UnsafePointer

comptime AF_INET: Int32 = 2
comptime SOCK_STREAM: Int32 = 1
comptime SOCK_NONBLOCK: Int32 = 0o4000
comptime SOL_SOCKET: Int32 = 1
comptime SO_REUSEADDR: Int32 = 2
comptime SO_REUSEPORT: Int32 = 15
comptime IPPROTO_TCP: Int32 = 6
comptime TCP_NODELAY: Int32 = 1

comptime EPOLL_CTL_ADD: Int32 = 1
comptime EPOLL_CTL_DEL: Int32 = 2
comptime EPOLL_CTL_MOD: Int32 = 3

comptime EPOLLIN: UInt32 = 0x001
comptime EPOLLOUT: UInt32 = 0x004
comptime EPOLLERR: UInt32 = 0x008
comptime EPOLLHUP: UInt32 = 0x010
comptime EPOLLRDHUP: UInt32 = 0x2000
comptime EPOLLET: UInt32 = 0x80000000

comptime EAGAIN: Int32 = 11
comptime EINTR: Int32 = 4
comptime EINPROGRESS: Int32 = 115

comptime SIGPIPE: Int32 = 13
comptime SIG_IGN: Int = 1

# sizeof(struct epoll_event) on x86-64 with __attribute__((packed)): 12 bytes.
comptime EPOLL_EVENT_SIZE: Int = 12
# sizeof(struct sockaddr_in)
comptime SOCKADDR_IN_SIZE: Int = 16


def errno() -> Int32:
    """Read the calling thread's errno."""
    var addr = external_call["__errno_location", Int]()
    if addr == 0:
        return 0
    return UnsafePointer[Int32, ImmStaticOrigin](unsafe_from_address=addr)[]


def sys_socket(domain: Int32, type: Int32, protocol: Int32) -> Int32:
    return external_call["socket", Int32](domain, type, protocol)


def sys_close(fd: Int32) -> Int32:
    return external_call["close", Int32](fd)


def sys_listen(fd: Int32, backlog: Int32) -> Int32:
    return external_call["listen", Int32](fd, backlog)


def sys_shutdown(fd: Int32, how: Int32) -> Int32:
    return external_call["shutdown", Int32](fd, how)


def ignore_sigpipe():
    """Writing to a peer that vanished must return EPIPE, not kill the worker."""
    _ = external_call["signal", Int](SIGPIPE, SIG_IGN)


def set_sockopt_int(fd: Int32, level: Int32, name: Int32, value: Int32) -> Int32:
    var v = value
    return external_call["setsockopt", Int32](
        fd,
        level,
        name,
        UnsafePointer(to=v).bitcast[UInt8](),
        Int32(4),
    )


def htons(port: UInt16) -> UInt16:
    """Host to network short. x86-64 is little endian, so always byte-swap."""
    return ((port & 0x00FF) << 8) | ((port & 0xFF00) >> 8)


def fill_sockaddr_in(mut sa: List[UInt8], port: UInt16, addr_be: UInt32):
    """Write a `struct sockaddr_in` into the first 16 bytes of `sa`.

    Layout: u16 family, u16 port (network order), u32 addr, 8 pad bytes.
    """
    for i in range(SOCKADDR_IN_SIZE):
        sa[i] = 0
    sa[0] = UInt8(AF_INET & 0xFF)
    sa[1] = UInt8((AF_INET >> 8) & 0xFF)
    var p = htons(port)
    sa[2] = UInt8(p & 0xFF)
    sa[3] = UInt8((p >> 8) & 0xFF)
    sa[4] = UInt8(addr_be & 0xFF)
    sa[5] = UInt8((addr_be >> 8) & 0xFF)
    sa[6] = UInt8((addr_be >> 16) & 0xFF)
    sa[7] = UInt8((addr_be >> 24) & 0xFF)


def sys_bind(fd: Int32, mut sa: List[UInt8]) -> Int32:
    return external_call["bind", Int32](
        fd, sa.unsafe_ptr(), Int32(SOCKADDR_IN_SIZE)
    )


def sys_accept4(fd: Int32, flags: Int32) -> Int32:
    """Accept with SOCK_NONBLOCK applied atomically; peer address discarded."""
    return external_call["accept4", Int32](fd, Int(0), Int(0), flags)


def sys_read(fd: Int32, mut buf: List[UInt8], offset: Int, count: Int) -> Int:
    # Signature must match the stdlib's own `read`/`write` externals
    # (index, pointer, index) or the linker sees conflicting declarations.
    var tail = Span(buf)[offset:]
    return external_call["read", Int](Int(fd), tail.unsafe_ptr(), count)


def sys_write(fd: Int32, buf: Span[UInt8, _], offset: Int, count: Int) -> Int:
    var tail = buf[offset:]
    return external_call["write", Int](Int(fd), tail.unsafe_ptr(), count)


def epoll_create() -> Int32:
    return external_call["epoll_create1", Int32](Int32(0))


def epoll_add(epfd: Int32, fd: Int32, events: UInt32) -> Int32:
    var ev = List[UInt8](length=EPOLL_EVENT_SIZE, fill=0)
    _write_event(ev, events, fd)
    return external_call["epoll_ctl", Int32](
        epfd, EPOLL_CTL_ADD, fd, ev.unsafe_ptr()
    )


def epoll_mod(epfd: Int32, fd: Int32, events: UInt32) -> Int32:
    var ev = List[UInt8](length=EPOLL_EVENT_SIZE, fill=0)
    _write_event(ev, events, fd)
    return external_call["epoll_ctl", Int32](
        epfd, EPOLL_CTL_MOD, fd, ev.unsafe_ptr()
    )


def epoll_del(epfd: Int32, fd: Int32) -> Int32:
    var ev = List[UInt8](length=EPOLL_EVENT_SIZE, fill=0)
    return external_call["epoll_ctl", Int32](
        epfd, EPOLL_CTL_DEL, fd, ev.unsafe_ptr()
    )


def _write_event(mut ev: List[UInt8], events: UInt32, fd: Int32):
    """struct epoll_event { uint32 events; epoll_data_t data; } __packed__.

    We stash the fd in the low 4 bytes of the union, which is how epoll_data
    is used when the caller sticks to `data.fd`.
    """
    ev[0] = UInt8(events & 0xFF)
    ev[1] = UInt8((events >> 8) & 0xFF)
    ev[2] = UInt8((events >> 16) & 0xFF)
    ev[3] = UInt8((events >> 24) & 0xFF)
    var u = UInt32(fd)
    ev[4] = UInt8(u & 0xFF)
    ev[5] = UInt8((u >> 8) & 0xFF)
    ev[6] = UInt8((u >> 16) & 0xFF)
    ev[7] = UInt8((u >> 24) & 0xFF)


def epoll_wait(
    epfd: Int32, mut events: List[UInt8], max_events: Int, timeout_ms: Int32
) -> Int32:
    return external_call["epoll_wait", Int32](
        epfd, events.unsafe_ptr(), Int32(max_events), timeout_ms
    )


def event_fd(events: Span[UInt8, _], index: Int) -> Int32:
    var b = index * EPOLL_EVENT_SIZE + 4
    var u = (
        UInt32(events[b])
        | (UInt32(events[b + 1]) << 8)
        | (UInt32(events[b + 2]) << 16)
        | (UInt32(events[b + 3]) << 24)
    )
    return Int32(u)


def event_mask(events: Span[UInt8, _], index: Int) -> UInt32:
    var b = index * EPOLL_EVENT_SIZE
    return (
        UInt32(events[b])
        | (UInt32(events[b + 1]) << 8)
        | (UInt32(events[b + 2]) << 16)
        | (UInt32(events[b + 3]) << 24)
    )
