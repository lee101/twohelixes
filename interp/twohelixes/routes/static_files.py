"""Static asset serving for the built frontend.

Files are read once and cached in the worker: the bundle is a handful of files
and re-reading them per request would put disk I/O on the hot path. A build
replaces the files, so the cache keys on mtime and size rather than path alone.
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import threading
from pathlib import Path

from twohelixes import config, router

log = logging.getLogger("twohelixes.routes.static")

STATIC_ROOT = (config.REPO_ROOT / "static").resolve()
MAX_CACHED_BYTES = 8 * 1024 * 1024

TEXT_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}

_cache: dict[str, tuple[float, int, str, str]] = {}
_lock = threading.Lock()


def _resolve(relative: str) -> Path | None:
    """Resolve under STATIC_ROOT, refusing anything that escapes it."""
    if not relative or "\x00" in relative:
        return None
    candidate = (STATIC_ROOT / relative).resolve()
    try:
        candidate.relative_to(STATIC_ROOT)
    except ValueError:
        log.warning("blocked traversal attempt: %s", relative)
        return None
    if not candidate.is_file():
        return None
    return candidate


@router.get("/static/{path}")
def serve(ctx: router.Context) -> router.Result:
    return _serve(ctx.params["path"])


@router.get("/static/{folder}/{path}")
def serve_nested(ctx: router.Context) -> router.Result:
    return _serve(f"{ctx.params['folder']}/{ctx.params['path']}")


def _serve(relative: str) -> router.Result:
    path = _resolve(relative)
    if path is None:
        return router.error(404, "not_found", relative)

    stat = path.stat()
    key = str(path)

    with _lock:
        cached = _cache.get(key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        _, _, content_type, body = cached
        return _result(body, content_type, path.suffix)

    suffix = path.suffix.lower()
    content_type = TEXT_TYPES.get(suffix) or (
        mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    )

    raw = path.read_bytes()
    if not (content_type.startswith(("text/", "application/json")) or suffix == ".svg"):
        # The Mojo bridge carries UTF-8 strings, so a binary body cannot cross
        # it intact. nginx serves /static/ straight from disk in production
        # (see deploy/nginx-twohelixes.com.conf), which is the only path these
        # files should ever take. Failing loudly here beats emitting a bogus
        # "image/webp;base64" that no browser can decode.
        log.warning(
            "refusing to serve binary %s through the Python layer; "
            "nginx must serve /static/",
            relative,
        )
        return router.error(
            501,
            "binary_static_unsupported",
            f"{suffix} assets are served by nginx, not the app server",
        )

    body = raw.decode("utf-8", "replace")

    if stat.st_size <= MAX_CACHED_BYTES:
        with _lock:
            _cache[key] = (stat.st_mtime, stat.st_size, content_type, body)

    return _result(body, content_type, suffix)


def _result(body: str, content_type: str, suffix: str) -> router.Result:
    # Hashed chunk names are immutable; entry points are not.
    immutable = "chunk-" in body[:0] or suffix in (".woff2",)
    cache_control = (
        "public, max-age=31536000, immutable"
        if immutable
        else "public, max-age=300"
    )
    return router.Result(
        status=200,
        body=body,
        content_type=content_type,
        headers={"Cache-Control": cache_control},
    )
