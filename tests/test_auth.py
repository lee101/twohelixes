"""Password sign-in, signup, reset, and remembered sessions."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from twohelixes import auth, router, store
from twohelixes.routes import auth as auth_routes
from twohelixes.routes import query as query_routes


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TWOHELIXES_DEV", "1")
    store.close()
    store._initialised = False
    store.init()
    yield
    store.close()
    store._initialised = False


def _post(path: str, body: dict) -> router.Result:
    ctx = router.build_context(
        "POST",
        path,
        "",
        json.dumps(body),
        json.dumps({"user-agent": "test browser"}),
    )
    if path.endswith("/signup"):
        return auth_routes.signup(ctx)
    if path.endswith("/signin"):
        return auth_routes.signin(ctx)
    if path.endswith("/forgot-password"):
        return auth_routes.forgot_password(ctx)
    if path.endswith("/reset-password"):
        return auth_routes.reset_password(ctx)
    if path.endswith("/signout"):
        return auth_routes.signout(ctx)
    raise AssertionError(path)


def test_passwords_are_salted_and_verified() -> None:
    first = auth.hash_password("correct horse battery staple")
    second = auth.hash_password("correct horse battery staple")
    assert first != second
    assert auth.verify_password("correct horse battery staple", first)
    assert not auth.verify_password("wrong password", first)


def test_signup_creates_a_password_account_and_login_rejects_a_wrong_password() -> None:
    created = _post(
        "/v1/auth/signup",
        {"email": "person@test.local", "password": "correct horse battery staple"},
    )
    assert created.status == 200
    assert created.body["id"]
    assert "th_session=" in created.headers["Set-Cookie"]
    assert f"Max-Age={auth.SESSION_TTL}" in created.headers["Set-Cookie"]
    assert store.get_user_by_email("person@test.local")["password_hash"].startswith(
        "scrypt$"
    )

    again = _post(
        "/v1/auth/signup",
        {"email": "person@test.local", "password": "another-password"},
    )
    assert again.status == 409
    assert again.body["error"] == "account_exists"

    wrong = _post(
        "/v1/auth/signin",
        {"email": "person@test.local", "password": "this is the wrong password"},
    )
    assert wrong.status == 401
    assert wrong.body["error"] == "invalid_credentials"

    ok = _post(
        "/v1/auth/signin",
        {"email": "person@test.local", "password": "correct horse battery staple"},
    )
    assert ok.status == 200


def test_short_passwords_do_not_create_accounts() -> None:
    result = _post(
        "/v1/auth/signup", {"email": "short@test.local", "password": "short"}
    )
    assert result.status == 400
    assert result.body["error"] == "password_too_short"
    assert store.get_user_by_email("short@test.local") is None


def test_forgot_password_resets_and_signs_in() -> None:
    _post(
        "/v1/auth/signup",
        {"email": "reset@test.local", "password": "old-password"},
    )
    forgot = _post("/v1/auth/forgot-password", {"email": "reset@test.local"})
    assert forgot.status == 200
    assert forgot.body["ok"] is True
    assert "reset_url" in forgot.body
    token = forgot.body["reset_url"].rsplit("token=", 1)[-1]

    changed = _post(
        "/v1/auth/reset-password",
        {"token": token, "password": "new-password"},
    )
    assert changed.status == 200
    assert "th_session=" in changed.headers["Set-Cookie"]

    old = _post(
        "/v1/auth/signin",
        {"email": "reset@test.local", "password": "old-password"},
    )
    assert old.status == 401
    fresh = _post(
        "/v1/auth/signin",
        {"email": "reset@test.local", "password": "new-password"},
    )
    assert fresh.status == 200


def test_me_renews_the_remembered_session() -> None:
    signed_in = _post(
        "/v1/auth/signup",
        {"email": "remember@test.local", "password": "correct horse battery staple"},
    )
    cookie = signed_in.headers["Set-Cookie"].split(";", 1)[0]
    token = cookie.split("=", 1)[1]
    store.execute(
        "UPDATE sessions SET expires_at = ? WHERE token = ?",
        (time.time() + 60, token),
    )

    ctx = router.build_context(
        "GET", "/v1/me", "", "", json.dumps({"cookie": cookie})
    )
    result = query_routes.me(ctx)
    assert result.body["signed_in"] is True
    assert f"Max-Age={auth.SESSION_TTL}" in result.headers["Set-Cookie"]
    row = store.one("SELECT expires_at FROM sessions WHERE token = ?", (token,))
    assert float(row["expires_at"]) > time.time() + auth.SESSION_TTL - 10
