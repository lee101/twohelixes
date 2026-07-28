"""The path from a marketing page to a paid account, in a real browser.

Everything before the chart is where a product loses people, and it is exactly
the part the API tests cannot see: the sign-in sheet and the checkout sheet
are DOM, not endpoints. The failures worth catching here are silent ones - a
sheet that opens over a dead form, a buy button that does nothing because the
visitor is signed out, a modal that sits on "Preparing secure checkout" for
ever because Stripe is not configured and nothing says so.

Stripe test keys are used when the environment has them and the graceful
degradation is asserted when it does not, so this suite is green on a laptop
with no billing configured and stricter on a box that has it.
"""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

STRIPE_CONFIGURED = bool(
    os.environ.get("STRIPE_SECRET_KEY") or os.environ.get("STRIPE_API_KEY")
)


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_playwright() as pw:
        instance = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        yield instance
        instance.close()


def _page(browser: Any, server: Any, path: str, viewport: dict = DESKTOP) -> Any:
    context = browser.new_context(viewport=viewport, is_mobile=viewport["width"] < 500)
    page = context.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))
    page.goto(f"{server.base}{path}", wait_until="networkidle", timeout=30000)
    return page


def _email() -> str:
    return f"signup-{int(time.time() * 1000)}@twohelixes.test"


# --------------------------------------------------------------------------
# Sign-in, without leaving the page
# --------------------------------------------------------------------------


def test_the_header_cta_signs_you_in_without_a_navigation(browser: Any, server: Any):
    page = _page(browser, server, "/pricing")

    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)
    assert page.url.endswith("/pricing"), "the sheet must not cost a navigation"

    email = _email()
    page.fill("#signin-email", email)
    page.click("#signin-form button[type=submit]")

    # The app is where "Start free" goes once there is an account.
    page.wait_for_url("**/app", timeout=20000)
    assert page.errors == [], page.errors

    cookies = {c["name"] for c in page.context.cookies()}
    assert "th_session" in cookies, cookies


def test_a_bad_email_is_reported_in_the_sheet_not_swallowed(browser: Any, server: Any):
    page = _page(browser, server, "/pricing")
    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)

    page.fill("#signin-email", "not-an-email")
    page.click("#signin-form button[type=submit]")

    page.wait_for_function(
        "() => document.querySelector('#signin-error').textContent.trim().length > 0",
        timeout=10000,
    )
    assert page.is_visible("#signin-overlay[open]"), "the sheet stayed open to be fixed"
    # And the button is usable again rather than stuck on "Signing in...".
    assert page.is_enabled("#signin-form button[type=submit]")


def test_the_sheet_closes_on_escape_and_on_the_backdrop(browser: Any, server: Any):
    page = _page(browser, server, "/pricing")

    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)
    page.keyboard.press("Escape")
    # A closed overlay is display:none, so it is "hidden" rather than absent.
    page.wait_for_selector("#signin-overlay", state="hidden", timeout=5000)

    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)
    # The backdrop is the overlay itself; clicking its corner must dismiss.
    page.mouse.click(8, 8)
    page.wait_for_selector("#signin-overlay", state="hidden", timeout=5000)
    assert page.errors == [], page.errors


def test_the_sheet_docks_to_the_bottom_on_a_phone(browser: Any, server: Any):
    """A centred modal on a 390px screen puts the field under the keyboard."""
    page = _page(browser, server, "/pricing", PHONE)
    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)

    box = page.locator("#signin-overlay .sheet").bounding_box()
    viewport = page.viewport_size
    assert box is not None
    assert box["width"] >= viewport["width"] - 2, "the sheet is full width on a phone"
    assert box["y"] + box["height"] >= viewport["height"] - 2, "it docks to the bottom"


def test_a_dataset_page_hands_its_question_to_the_app(browser: Any, server: Any):
    """"Ask this yourself" has to arrive with the question already in the box.

    A visitor who has just read a worked example and clicks through to an
    empty ask box has to retype what they were shown, which is the point at
    which most of them stop.
    """
    page = _page(browser, server, "/datasets/orders")
    question = page.locator(".example figcaption b").first.text_content().strip()

    page.locator("a:has-text('Ask this yourself')").first.click()
    page.wait_for_selector("textarea.ask-input", timeout=20000)

    assert page.input_value("textarea.ask-input").strip() == question
    # Prefilled, not submitted, and the query string is tidied away so a reload
    # does not re-arm it.
    assert "?" not in page.url
    assert not page.is_visible(".chart-plot.js-plotly-plot")


# --------------------------------------------------------------------------
# Checkout, embedded in the page
# --------------------------------------------------------------------------


def test_buying_while_signed_out_asks_for_an_account_first(browser: Any, server: Any):
    page = _page(browser, server, "/pricing")

    page.click("[data-action='buy']")
    # Not a redirect to a sign-in page: the purchase has to survive the detour.
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)
    assert page.url.endswith("/pricing")

    page.fill("#signin-email", _email())
    page.click("#signin-form button[type=submit]")

    # Sign-in resolves the pending purchase, so checkout opens on its own.
    page.wait_for_selector("#checkout-overlay[open]", timeout=20000)


def test_the_checkout_sheet_never_sits_on_a_blank_modal(browser: Any, server: Any):
    """Whatever happens, the sheet says something true within a few seconds.

    The failure this guards is the worst kind: an empty white box with a
    spinner caption, which a buyer reads as "my money went somewhere".
    """
    page = _page(browser, server, "/pricing")
    page.click("#header-cta")
    page.wait_for_selector("#signin-overlay[open]", timeout=10000)
    page.fill("#signin-email", _email())
    page.click("#signin-form button[type=submit]")
    page.wait_for_url("**/app", timeout=20000)

    # Back to pricing in the same context, so the session cookie comes with us
    # and the buy button goes straight to checkout.
    page.goto(f"{server.base}/pricing", wait_until="networkidle", timeout=30000)
    page.click("[data-action='buy']")
    page.wait_for_selector("#checkout-overlay[open]", timeout=15000)

    page.wait_for_function(
        """() => {
            const status = document.querySelector('#checkout-status');
            const mount = document.querySelector('#checkout-mount');
            const mounted = mount && mount.childElementCount > 0;
            const said = status && status.textContent.trim();
            return mounted || (said && !said.startsWith('Preparing'));
        }""",
        timeout=30000,
    )

    status = page.text_content("#checkout-status").strip()
    mounted = page.evaluate(
        "() => document.querySelector('#checkout-mount').childElementCount > 0"
    )

    if STRIPE_CONFIGURED:
        assert mounted, f"Stripe is configured but nothing mounted: {status!r}"
    else:
        # Unconfigured is a legitimate state; a lie about it is not.
        assert not mounted
        assert status, "the sheet must say why there is no payment form"
        assert "checkout" in status.lower() or "unavailable" in status.lower(), status


def test_the_checkout_return_page_reports_rather_than_grants(browser: Any, server: Any):
    """Stripe's embedded return_url points here; it must not be a 404."""
    page = _page(browser, server, "/billing/complete?session_id=cs_test_missing")
    assert page.title().startswith("Payment complete")
    # Credits are granted by the webhook. This page reports, and when it cannot
    # confirm anything it still has to leave the buyer somewhere to go.
    assert page.is_visible("a[href='/app']")
