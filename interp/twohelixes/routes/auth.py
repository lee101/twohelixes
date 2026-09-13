"""Sign up, sign in, forgot / reset password."""

from __future__ import annotations

import logging
import threading
from typing import Any

from twohelixes import auth, config, emails, router, store

log = logging.getLogger("twohelixes.routes.auth")

_GENERIC_FORGOT = (
    "If an account exists for that email, a reset link is on its way."
)


def _email_ok(email: str) -> bool:
    return "@" in email and len(email) <= 320


def _password_error(password: str) -> router.Result | None:
    if not password:
        return router.error(400, "password_required", "Enter a password.")
    if len(password) < auth.PASSWORD_MIN_LENGTH:
        return router.error(
            400,
            "password_too_short",
            f"Use at least {auth.PASSWORD_MIN_LENGTH} characters.",
        )
    if len(password) > auth.PASSWORD_MAX_LENGTH:
        return router.error(400, "password_too_long")
    return None


def _session_result(user: dict[str, Any], ctx: router.Context) -> router.Result:
    token = auth.mint_session(user["id"], ctx.header("user-agent"))
    identity = auth._identity_from_user(user)
    return router.Result(
        status=200,
        body=identity.to_public(),
        headers={"Set-Cookie": auth.cookie_header(token, secure=not config.is_dev())},
    )


def _queue_welcome(email: str) -> None:
    threading.Thread(
        target=emails.send_welcome, args=(email,), daemon=True, name="welcome-mail"
    ).start()


@router.post("/v1/auth/signup")
def signup(ctx: router.Context) -> router.Result:
    email = str(ctx.field("email") or "").strip().lower()
    if not _email_ok(email):
        return router.error(400, "invalid_email")

    password = str(ctx.field("password") or "")
    bad = _password_error(password)
    if bad is not None:
        return bad

    existing = store.get_user_by_email(email)
    if existing is not None and existing.get("password_hash"):
        return router.error(
            409,
            "account_exists",
            "An account with that email already exists. Sign in instead.",
        )

    try:
        hashed = auth.hash_password(password)
    except ValueError as exc:
        return router.error(400, str(exc))

    if existing is None:
        user = store.create_user(email)
    else:
        user = existing
    store.touch_user(user["id"], password_hash=hashed)
    user = store.get_user(user["id"]) or user
    _queue_welcome(email)
    return _session_result(user, ctx)


@router.post("/v1/auth/signin")
def signin(ctx: router.Context) -> router.Result:
    """Resume an account, or create one when the email is new.

    The modal has an explicit signup mode, but treating an unknown email on
    sign-in as "create" matches the previous Continue button and keeps an
    older cached client from dead-ending on 401.
    """
    email = str(ctx.field("email") or "").strip().lower()
    if not _email_ok(email):
        return router.error(400, "invalid_email")

    password = str(ctx.field("password") or "")
    bad = _password_error(password)
    if bad is not None:
        return bad

    user = store.get_user_by_email(email)
    if user is None:
        try:
            hashed = auth.hash_password(password)
        except ValueError as exc:
            return router.error(400, str(exc))
        user = store.create_user(email)
        store.touch_user(user["id"], password_hash=hashed)
        user = store.get_user(user["id"]) or user
        _queue_welcome(email)
        return _session_result(user, ctx)

    saved = str(user.get("password_hash") or "")
    if not saved:
        # Legacy email-only rows: set a password on first successful sign-in.
        store.touch_user(user["id"], password_hash=auth.hash_password(password))
        user = store.get_user(user["id"]) or user
        return _session_result(user, ctx)

    if not auth.verify_password(password, saved):
        return router.error(401, "invalid_credentials", "Email or password is incorrect.")
    return _session_result(user, ctx)


@router.post("/v1/auth/forgot-password")
def forgot_password(ctx: router.Context) -> router.Result:
    """Always the same answer — never an account-discovery oracle."""
    email = str(ctx.field("email") or "").strip().lower()
    body: dict[str, Any] = {"ok": True, "detail": _GENERIC_FORGOT}
    if not _email_ok(email):
        return router.json_result(body)

    user = store.get_user_by_email(email)
    if user is None:
        return router.json_result(body)

    token = auth.mint_reset_token(user["id"])
    reset_url = f"{config.site_url().rstrip('/')}/reset-password?token={token}"
    emails.send_password_reset(email, reset_url)
    # Dev / e2e: surface the URL so the flow is testable without reading the
    # database or depending on SES. Production never sets TWOHELIXES_DEV.
    if config.is_dev():
        body["reset_url"] = reset_url
    return router.json_result(body)


@router.post("/v1/auth/reset-password")
def reset_password(ctx: router.Context) -> router.Result:
    token = str(ctx.field("token") or "").strip()
    password = str(ctx.field("password") or "")
    bad = _password_error(password)
    if bad is not None:
        return bad

    user = auth.user_for_reset_token(token)
    if user is None:
        return router.error(400, "invalid_token", "That reset link is invalid or expired.")

    try:
        hashed = auth.hash_password(password)
    except ValueError as exc:
        return router.error(400, str(exc))

    store.touch_user(user["id"], password_hash=hashed)
    auth.clear_reset_token(user["id"])
    auth.revoke_user_sessions(user["id"])
    user = store.get_user(user["id"]) or user
    return _session_result(user, ctx)


@router.post("/v1/auth/signout")
def signout(ctx: router.Context) -> router.Result:
    token = ctx.cookie(auth.COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    return router.Result(
        status=200,
        body={"signed_out": True},
        headers={"Set-Cookie": auth.clear_cookie_header()},
    )
