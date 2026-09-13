"""Signup → account paywall → logout → login again, in a real browser."""

from __future__ import annotations

import time
from typing import Any

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

DESKTOP = {"width": 1440, "height": 900}


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_playwright() as pw:
        instance = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        yield instance
        instance.close()


def _email() -> str:
    return f"account-{int(time.time() * 1000)}@twohelixes.test"


def test_account_paywall_logout_and_login_again(browser: Any, server: Any):
    email = _email()
    password = "test-password-123"
    context = browser.new_context(viewport=DESKTOP)
    page = context.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))

    page.goto(f"{server.base}/pricing", wait_until="networkidle", timeout=30000)
    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)

    # Ensure signup mode (Start free defaults to it).
    if page.locator("#signin-title").inner_text() != "Create account":
        page.click('[data-auth-mode="signup"]')
        page.wait_for_function(
            "() => document.querySelector('#signin-title').textContent === 'Create account'",
            timeout=5000,
        )
    page.fill("#signin-email", email)
    page.fill("#signin-password", password)
    page.click("#signin-form button[type=submit]")
    page.wait_for_url("**/app", timeout=20000)

    stored = page.evaluate("() => JSON.parse(localStorage.getItem('th_user') || 'null')")
    assert stored and stored["email"] == email and stored["id"]

    page.goto(f"{server.base}/account", wait_until="networkidle", timeout=30000)
    assert page.locator("#account-email").inner_text() == email
    assert page.is_visible("#account-plan")
    assert page.is_visible("#account-upgrade") or page.is_visible(".account-paywall")
    assert page.is_visible("#account-logout")

    page.click("#account-logout")
    page.wait_for_url("**/", timeout=20000)
    cleared = page.evaluate("() => localStorage.getItem('th_user')")
    assert cleared is None
    cookies = {c["name"]: c for c in context.cookies()}
    assert "th_session" not in cookies or not cookies.get("th_session", {}).get("value")

    # Log back in via the modal on login mode.
    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)
    if page.locator("#signin-title").inner_text() != "Sign in":
        page.click('[data-auth-mode="login"]')
        page.wait_for_function(
            "() => document.querySelector('#signin-title').textContent === 'Sign in'",
            timeout=5000,
        )
    page.fill("#signin-email", email)
    page.fill("#signin-password", password)
    page.click("#signin-form button[type=submit]")
    page.wait_for_url("**/app", timeout=20000)

    page.goto(f"{server.base}/account", wait_until="networkidle", timeout=30000)
    assert page.locator("#account-email").inner_text() == email
    restored = page.evaluate("() => JSON.parse(localStorage.getItem('th_user') || 'null')")
    assert restored and restored["email"] == email
    assert page.errors == [], page.errors
