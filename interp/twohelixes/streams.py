"""Background jobs that feed Server-Sent Events.

A streaming request never blocks the Mojo event loop. `start()` spawns a
daemon thread and returns immediately; the loop drains buffered SSE frames
with `poll()`, which only touches a lock and a list. LLM and database calls
release the GIL while they wait on the network, so several streams progress
concurrently inside one worker.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("twohelixes.streams")

# A stream is dropped this long after completion so a client that reconnects
# late still gets the tail instead of a 404.
REAP_AFTER_SECONDS = 300
MAX_BUFFERED_FRAMES = 4096


def sse(event: str, data: Any) -> str:
    """Format one SSE frame. Data is always JSON so the client has one parser."""
    payload = json.dumps(data, default=str, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


@dataclass
class Stream:
    id: str
    frames: deque[str] = field(default_factory=deque)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: bool = False
    cancelled: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    dropped: int = 0

    def emit(self, event: str, data: Any) -> None:
        """Append a frame. Called from the worker thread."""
        if self.cancelled:
            raise Cancelled(self.id)
        frame = sse(event, data)
        with self.lock:
            if len(self.frames) >= MAX_BUFFERED_FRAMES:
                # Never grow without bound if a client stops reading. Drop the
                # oldest progress frame and record that we did.
                self.frames.popleft()
                self.dropped += 1
            self.frames.append(frame)

    def drain(self) -> tuple[str, bool]:
        with self.lock:
            if not self.frames:
                return ("", self.done)
            chunk = "".join(self.frames)
            self.frames.clear()
            return (chunk, self.done)

    def finish(self) -> None:
        with self.lock:
            self.done = True
            self.finished_at = time.time()


class Cancelled(Exception):
    """Raised inside a job thread when the client went away."""


_streams: dict[str, Stream] = {}
_streams_lock = threading.Lock()


def _reap() -> None:
    now = time.time()
    with _streams_lock:
        stale = [
            sid
            for sid, s in _streams.items()
            if s.done and s.finished_at and now - s.finished_at > REAP_AFTER_SECONDS
        ]
        for sid in stale:
            _streams.pop(sid, None)


def _register() -> Stream:
    _reap()
    stream = Stream(id=uuid.uuid4().hex)
    with _streams_lock:
        _streams[stream.id] = stream
    return stream


def _run(stream: Stream, fn: Callable[[Stream], None]) -> None:
    try:
        fn(stream)
    except Cancelled:
        log.info("stream %s cancelled by client", stream.id)
    except Exception as exc:
        log.exception("stream %s failed", stream.id)
        try:
            stream.emit(
                "error",
                {
                    "message": str(exc) or exc.__class__.__name__,
                    "type": exc.__class__.__name__,
                },
            )
        except Exception:
            pass
    finally:
        try:
            stream.emit("done", {"elapsed_ms": int((time.time() - stream.started_at) * 1000)})
        except Exception:
            pass
        stream.finish()


def spawn(fn: Callable[[Stream], None], name: str = "job") -> str:
    """Run `fn` on a daemon thread, returning the stream id immediately."""
    stream = _register()
    thread = threading.Thread(
        target=_run, args=(stream, fn), name=f"th-{name}-{stream.id[:8]}", daemon=True
    )
    thread.start()
    return stream.id


def start(path: str, query: str, body: str, headers: str) -> str:
    """Route a streaming request to its handler."""
    from twohelixes import router

    handler = router.stream_handler(path)
    if handler is None:
        return start_failed(f"no streaming route for {path}")

    ctx = router.build_context("POST", path, query, body, headers)

    def job(stream: Stream) -> None:
        handler(stream, ctx)

    return spawn(job, name=path.strip("/").replace("/", "-") or "root")


def start_failed(message: str) -> str:
    """Return a stream that immediately reports an error."""
    stream = _register()

    def job(s: Stream) -> None:
        s.emit("error", {"message": message.strip().splitlines()[-1]})

    threading.Thread(target=_run, args=(stream, job), daemon=True).start()
    return stream.id


def poll(stream_id: str) -> tuple[str, bool]:
    with _streams_lock:
        stream = _streams.get(stream_id)
    if stream is None:
        return (sse("error", {"message": "unknown stream"}), True)
    return stream.drain()


def cancel(stream_id: str) -> None:
    with _streams_lock:
        stream = _streams.get(stream_id)
    if stream is not None:
        stream.cancelled = True


def active_count() -> int:
    with _streams_lock:
        return sum(1 for s in _streams.values() if not s.done)
