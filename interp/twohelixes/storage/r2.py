"""Cloudflare R2 storage with presigned uploads.

Presigning matters more here than it usually would. The Mojo/Python bridge
carries UTF-8 strings, so a binary body cannot cross it intact - a 200 MB
Parquet file has nowhere to go through `/v1/upload`. A presigned PUT lets the
browser send the file straight to R2 and never touch the app server, which is
both the only correct route for large files and the cheaper one.

SigV4 is implemented here rather than pulled from boto3: the signing is a
couple of HMACs, and this avoids a heavyweight dependency in the request path.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from twohelixes import config

log = logging.getLogger("twohelixes.storage.r2")

SERVICE = "s3"
REGION = "auto"
ALGORITHM = "AWS4-HMAC-SHA256"

# Anything a data tool legitimately ingests. Deliberately no archives or
# executables: an upload URL is a capability, so it should only ever point at
# a shape we can actually read.
ALLOWED_CONTENT_TYPES = {
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "text/plain": ".txt",
    "application/json": ".json",
    "application/x-ndjson": ".ndjson",
    "application/vnd.apache.parquet": ".parquet",
    "application/octet-stream": ".parquet",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}

MAX_UPLOAD_BYTES = 512 * 1024 * 1024
DEFAULT_EXPIRY = 900  # 15 minutes: long enough for a slow upload, short enough to matter


class R2Error(Exception):
    pass


class NotConfigured(R2Error):
    pass


def account_id() -> str:
    return config.get("CLOUDFLARE_ACCOUNT_ID", "") or ""


def bucket() -> str:
    return config.get("R2_BUCKET", "twohelixesstatic") or "twohelixesstatic"


def endpoint() -> str:
    explicit = config.get("R2_ENDPOINT")
    if explicit:
        return explicit.rstrip("/")
    account = account_id()
    if not account:
        raise NotConfigured("CLOUDFLARE_ACCOUNT_ID is not set")
    return f"https://{account}.r2.cloudflarestorage.com"


def public_host() -> str:
    return config.get("R2_PUBLIC_HOST", "") or ""


def credentials() -> tuple[str, str]:
    key = config.get("R2_ACCESS_KEY_ID") or config.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
    secret = (
        config.get("R2_SECRET_ACCESS_KEY")
        or config.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
    )
    if not key or not secret:
        raise NotConfigured(
            "R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are not set"
        )
    return key, secret


def is_configured() -> bool:
    try:
        credentials()
        endpoint()
        return True
    except NotConfigured:
        return False


# --------------------------------------------------------------------------
# SigV4
# --------------------------------------------------------------------------


def _sign(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str) -> bytes:
    k = _sign(f"AWS4{secret}".encode(), datestamp)
    k = _sign(k, REGION)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


def _uri_encode(value: str, encode_slash: bool = True) -> str:
    safe = "-_.~" if encode_slash else "-_.~/"
    return quote(value, safe=safe)


def presign(
    key: str,
    method: str = "PUT",
    expires: int = DEFAULT_EXPIRY,
    content_type: str = "",
) -> str:
    """A presigned URL valid for `expires` seconds.

    Query-string signing (not headers) so the browser can issue a bare PUT
    with no Authorization header and no preflight surprises.
    """
    access_key, secret_key = credentials()
    host_url = endpoint()
    host = host_url.split("://", 1)[1]

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{REGION}/{SERVICE}/aws4_request"

    canonical_uri = f"/{bucket()}/{_uri_encode(key, encode_slash=False)}"

    query = {
        "X-Amz-Algorithm": ALGORITHM,
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(max(1, min(expires, 604800))),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{_uri_encode(k)}={_uri_encode(v)}" for k, v in sorted(query.items())
    )

    canonical_headers = f"host:{host}\n"
    canonical_request = "\n".join([
        method,
        canonical_uri,
        canonical_query,
        canonical_headers,
        "host",
        "UNSIGNED-PAYLOAD",
    ])

    string_to_sign = "\n".join([
        ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    signature = hmac.new(
        _signing_key(secret_key, datestamp), string_to_sign.encode(), hashlib.sha256
    ).hexdigest()

    return f"{host_url}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


# --------------------------------------------------------------------------
# Object operations
# --------------------------------------------------------------------------


def object_key(user_id: str, filename: str) -> str:
    """A namespaced, sanitised key.

    The user id prefix is what stops one account's presigned URL from being
    reusable against another's objects.
    """
    safe = "".join(
        c if (c.isalnum() or c in "._-") else "_" for c in filename.strip()
    )
    # Collapse dot runs. Slashes are already gone, so this is not a traversal
    # fix - the key is also used to name a file on disk after ingest, and a
    # ".." in a filename is the kind of thing that becomes a bug later.
    while ".." in safe:
        safe = safe.replace("..", ".")
    safe = safe.strip("._-")[:120]
    return f"uploads/{user_id}/{int(time.time())}-{safe or 'upload'}"


def upload_ticket(
    user_id: str, filename: str, content_type: str, size: int
) -> dict[str, Any]:
    """Everything the browser needs to PUT one file directly to R2."""
    if size <= 0 or size > MAX_UPLOAD_BYTES:
        raise R2Error(f"size must be between 1 and {MAX_UPLOAD_BYTES} bytes")

    normalised = (content_type or "application/octet-stream").split(";")[0].strip()
    if normalised not in ALLOWED_CONTENT_TYPES:
        raise R2Error(f"unsupported content type '{normalised}'")

    key = object_key(user_id, filename)
    return {
        "key": key,
        "url": presign(key, "PUT", DEFAULT_EXPIRY, normalised),
        "method": "PUT",
        "headers": {},
        "expires_in": DEFAULT_EXPIRY,
        "max_bytes": MAX_UPLOAD_BYTES,
    }


def download_url(key: str, expires: int = DEFAULT_EXPIRY) -> str:
    host = public_host()
    if host:
        return f"https://{host}/{key}"
    return presign(key, "GET", expires)


def fetch(key: str) -> bytes:
    """Read an object server-side, for ingest after an upload completes."""
    import httpx

    url = presign(key, "GET", 300)
    response = httpx.get(url, timeout=120, follow_redirects=True)
    response.raise_for_status()
    return response.content


def head(key: str) -> dict[str, Any]:
    """Confirm an object exists and how big it is, before ingesting it."""
    import httpx

    url = presign(key, "HEAD", 120)
    response = httpx.request("HEAD", url, timeout=30, follow_redirects=True)
    if response.status_code == 404:
        raise R2Error("object not found")
    response.raise_for_status()
    return {
        "size": int(response.headers.get("content-length", 0)),
        "content_type": response.headers.get("content-type", ""),
        "etag": response.headers.get("etag", "").strip('"'),
    }


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Server-side upload, used for generated artefacts such as chart exports."""
    import httpx

    url = presign(key, "PUT", 300)
    response = httpx.put(
        url, content=data, headers={"Content-Type": content_type}, timeout=120
    )
    response.raise_for_status()
    return key


def delete(key: str) -> None:
    import httpx

    url = presign(key, "DELETE", 120)
    response = httpx.delete(url, timeout=30)
    if response.status_code not in (204, 200, 404):
        response.raise_for_status()
