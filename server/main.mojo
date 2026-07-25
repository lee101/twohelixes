"""twoHelixes server entrypoint.

Spawns N worker processes that each run their own epoll loop over a shared
SO_REUSEPORT listener. Each worker embeds its own Python interpreter, so a
long interpreter run blocks that worker only - worker count is what sets
concurrency for interpreter-heavy requests, and streaming work is handed to
Python threads that release the GIL on network I/O.
"""

from std.ffi import external_call
from std.os import getenv
from th.server import Loop, make_listener


def env_str(name: StringSlice, default: StringSlice) -> String:
    var raw = getenv(String(name))
    if raw == "":
        return String(default)
    return raw


def env_int(name: StringSlice, default: Int) -> Int:
    var raw = getenv(String(name))
    if raw == "":
        return default
    try:
        return Int(raw)
    except:
        return default


def fork() -> Int32:
    return external_call["fork", Int32]()


def serve(port: UInt16, worker_id: Int, interp: String, site: String) raises:
    var fd = make_listener(port, True)
    var loop = Loop(fd, worker_id, interp, site)
    loop.run()


def main() raises:
    var port = UInt16(env_int("TWOHELIXES_PORT", 7474))
    var workers = env_int("TWOHELIXES_WORKERS", 4)
    if workers < 1:
        workers = 1

    var interp = env_str("TWOHELIXES_INTERP", "interp")
    var site = env_str("TWOHELIXES_SITE_PACKAGES", "")

    print("twohelixes :" + String(port), "workers=", workers)

    for w in range(1, workers):
        var pid = fork()
        if pid == 0:
            serve(port, w, interp, site)
            return

    serve(port, 0, interp, site)
