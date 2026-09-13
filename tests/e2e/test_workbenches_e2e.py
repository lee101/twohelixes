"""The SQL and Sheets chunks load and the spreadsheet survives persistence."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_playwright() as pw:
        instance = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        yield instance
        instance.close()


def _signed_in_page(browser: Any, server: Any) -> Any:
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.errors = []
    page.on("console", lambda message: page.errors.append(message.text) if message.type == "error" else None)
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.goto(f"{server.base}/app", wait_until="networkidle")
    page.click("button:has-text('Sign in')")
    page.fill("input[type=email]", "workbenches@twohelixes.test")
    page.fill("input[type=password]", "test-password-123")
    page.click("button[type=submit]")
    page.wait_for_selector("nav.app-nav")
    return page


def test_sql_and_sheets_are_reachable_and_a_formula_persists(browser: Any, server: Any) -> None:
    page = _signed_in_page(browser, server)

    page.click("nav.app-nav button:has-text('SQL')")
    page.wait_for_selector(".sql-workbench .cm-editor")
    assert page.locator(".sql-workbench").is_visible()

    page.click("nav.app-nav button:has-text('Sheets')")
    page.wait_for_selector(".sheets-workbench [data-ref=A1]")
    page.fill("[data-ref=A1]", "10")
    page.locator("[data-ref=A1]").press("Enter")
    page.fill("[data-ref=A2]", "20")
    page.locator("[data-ref=A2]").press("Enter")
    page.fill("[data-ref=A3]", "=SUM(A1:A2)")
    page.locator("[data-ref=A3]").press("Enter")
    assert page.input_value("[data-ref=A3]") == "30"

    page.fill(".sheet-title", "Formula check")
    page.click(".sheet-toolbar button:has-text('Save')")
    page.wait_for_function("document.querySelector('.workbench-status')?.textContent === 'Saved.'")

    page.reload(wait_until="networkidle")
    page.click("nav.app-nav button:has-text('Sheets')")
    page.wait_for_selector(".sheets-workbench [data-ref=A1]")
    page.select_option(".sheet-toolbar select", label="Formula check")
    page.wait_for_function("document.querySelector('.sheet-title')?.value === 'Formula check'")
    assert page.input_value("[data-ref=A3]") == "30"
    assert page.errors == [], page.errors
