"""The single entry point the Mojo event loop calls into.

Contract, kept deliberately narrow so the FFI surface stays cheap:

    boot()                                          -> None
    dispatch(method, path, query, body, headers)    -> (status, ctype, hdrs, body)
    stream_start(path, query, body, headers)        -> stream_id
    stream_poll(stream_id)                          -> (chunk, "1" | "0")
    stream_cancel(stream_id)                        -> None

Every value crossing the boundary is a `str`. Status is stringified so the
Mojo side does one uniform conversion.
"""

from __future__ import annotations

import json
import logging
import sys
import traceback

log = logging.getLogger("twohelixes.api")

_BOOTED = False


def _widen_dlopen() -> None:
    """Load C extensions with RTLD_GLOBAL.

    We are an *embedded* interpreter inside the Mojo binary, not the `python`
    executable, and libpython here is loaded RTLD_LOCAL. Under that default,
    numpy's `_multiarray_umath` cannot resolve libpython symbols and numpy
    reports the misleading "do not import numpy from its source directory";
    pandas then fails behind it. Widening the flag before the first heavy
    import is what makes the shared askfelix environment usable from Mojo.
    """
    import os

    if not hasattr(sys, "setdlopenflags"):
        return
    flags = getattr(os, "RTLD_NOW", 2) | getattr(os, "RTLD_GLOBAL", 256)
    try:
        sys.setdlopenflags(flags)
    except (ValueError, OSError):
        log.warning("could not widen dlopen flags; native extensions may fail")


def boot() -> str:
    """Warm every import that a request would otherwise pay for.

    Called once per worker at startup. Import cost lands here instead of on
    the first user request.
    """
    global _BOOTED
    if _BOOTED:
        return "1"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    _widen_dlopen()



    from twohelixes import router, store
    from twohelixes.interpreter import sandbox

    store.init()
    router.build()
    sandbox.warm()

    from twohelixes.routes import showcase

    showcase.warm()

    _BOOTED = True
    log.info("twohelixes worker booted")
    return "1"


def dispatch(
    method: str, path: str, query: str, body: str, headers: str
) -> tuple[str, str, str, str]:
    """Handle one non-streaming request."""
    from twohelixes import router

    try:
        return router.handle(method, path, query, body, headers)
    except Exception:
        log.exception("dispatch failed: %s %s", method, path)
        payload = json.dumps(
            {
                "error": "internal_error",
                "detail": traceback.format_exc(limit=3).splitlines()[-1],
            }
        )
        return ("500", "application/json; charset=utf-8", "", payload)


def stream_start(path: str, query: str, body: str, headers: str) -> str:
    from twohelixes import streams

    try:
        return streams.start(path, query, body, headers)
    except Exception:
        log.exception("stream_start failed: %s", path)
        return streams.start_failed(traceback.format_exc(limit=3))


def stream_poll(stream_id: str) -> tuple[str, str]:
    from twohelixes import streams

    chunk, done = streams.poll(stream_id)
    return (chunk, "1" if done else "0")


def stream_cancel(stream_id: str) -> str:
    from twohelixes import streams

    streams.cancel(stream_id)
    return "1"


def active_streams() -> str:
    from twohelixes import streams

    return str(streams.active_count())
