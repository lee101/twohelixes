"""Sessions and identity.

Two ways in:

* a signed session cookie minted here, and
* an `Authorization: Bearer <api key>` header for programmatic use, which is
  the paid path and skips the free-tier counters entirely.

The cookie is HMAC-signed rather than random-only so a forged token is
rejected without a database round trip.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from twohelixes import config, store

log = logging.getLogger("twohelixes.auth")

COOKIE_NAME = "th_session"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days


@dataclass
class Identity:
    """Who is making this request, and what they are allowed to spend."""

    user_id: str | None = None
    email: str = ""
    plan: str = "anon"
    api_credits: int = 0
    free_queries_used: int = 0
    # Included usage taken in the current allowance period. Read from the
    # user row rather than counted here, because it is shared across workers.
    included_used: int = 0
    is_admin: bool = False
    via_api_key: bool = False
    ip: str = "0.0.0.0"

    @property
    def signed_in(self) -> bool:
        return self.user_id is not None

    @property
    def paid(self) -> bool:
        """Paid means credits on hand or an active subscription."""
        return self.api_credits > 0 or self.plan in config.PAID_PLANS

    @property
    def key(self) -> str:
        """Rate-limit identity: per user when known, per IP otherwise."""
        return f"user:{self.user_id}" if self.user_id else f"ip:{self.ip}"

    def to_public(self) -> dict[str, Any]:
        return {
            "signed_in": self.signed_in,
            "email": self.email,
            "plan": self.plan,
            "api_credits": self.api_credits,
            "free_queries_used": self.free_queries_used,
            "free_queries_total": config.PLAN_ALLOWANCES.get(
                self.plan, config.PLAN_ALLOWANCES["free"]
            )["chat_query"],
            "free_queries_left": max(
                0,
                config.PLAN_ALLOWANCES.get(self.plan, config.PLAN_ALLOWANCES["free"])[
                    "chat_query"
                ]
                - self.included_used,
            ),
            "paid": self.paid,
            "is_admin": self.is_admin,
        }


def _sign(value: str) -> str:
    return hmac.new(
        config.session_secret().encode(), value.encode(), hashlib.sha256
    ).hexdigest()[:32]


def mint_session(user_id: str, user_agent: str = "") -> str:
    raw = secrets.token_urlsafe(32)
    token = f"{raw}.{_sign(raw)}"
    now = time.time()
    store.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, user_agent) "
        "VALUES (?, ?, ?, ?, ?)",
        (token, user_id, now, now + SESSION_TTL, user_agent[:400]),
    )
    return token


def revoke_session(token: str) -> None:
    store.execute("DELETE FROM sessions WHERE token = ?", (token,))


def cookie_header(token: str, secure: bool = True) -> str:
    parts = [
        f"{COOKIE_NAME}={token}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={SESSION_TTL}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_cookie_header() -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"


def _identity_from_user(row: dict[str, Any], via_api_key: bool = False) -> Identity:
    usage = row.get("plan_usage")
    if isinstance(usage, str):
        try:
            usage = json.loads(usage)
        except ValueError:
            usage = {}
    used = int((usage or {}).get("chat_query", 0))

    return Identity(
        user_id=row["id"],
        email=row.get("email") or "",
        plan=row.get("plan") or "free",
        api_credits=int(row.get("api_credits") or 0),
        free_queries_used=int(row.get("free_queries_used") or 0),
        included_used=used,
        is_admin=bool(row.get("is_admin")),
        via_api_key=via_api_key,
    )


def _from_api_key(raw: str) -> Identity | None:
    """API keys are `th_<user_id>_<secret>`; the secret is HMAC-verified."""
    if not raw.startswith("th_"):
        return None
    try:
        _, user_id, secret = raw.split("_", 2)
    except ValueError:
        return None
    if not hmac.compare_digest(secret, _sign(f"apikey:{user_id}")):
        return None
    row = store.get_user(user_id)
    if row is None:
        return None
    return _identity_from_user(row, via_api_key=True)


def api_key_for(user_id: str) -> str:
    return f"th_{user_id}_{_sign(f'apikey:{user_id}')}"


def identify(ctx: Any) -> Identity:
    """Resolve the caller. Never raises; an unknown caller is anonymous."""
    ip = getattr(ctx, "client_ip", "0.0.0.0")

    bearer = ctx.header("authorization")
    if bearer.lower().startswith("bearer "):
        identity = _from_api_key(bearer[7:].strip())
        if identity is not None:
            identity.ip = ip
            return identity

    token = ctx.cookie(COOKIE_NAME)
    if token:
        raw, _, signature = token.partition(".")
        if signature and hmac.compare_digest(signature, _sign(raw)):
            row = store.one(
                "SELECT * FROM sessions WHERE token = ? AND expires_at > ?",
                (token, time.time()),
            )
            if row is not None:
                user = store.get_user(row["user_id"])
                if user is not None:
                    identity = _identity_from_user(user)
                    identity.ip = ip
                    return identity

    return Identity(ip=ip)


def require(ctx: Any) -> Identity:
    """Identity for endpoints that need a signed-in user."""
    identity = ctx.user
    if identity is None or not identity.signed_in:
        raise Unauthorized()
    return identity


class Unauthorized(Exception):
    pass
