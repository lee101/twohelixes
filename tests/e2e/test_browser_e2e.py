"""End-to-end through a browser: the app as a person uses it.

The API tests prove the server answers. These prove the answer arrives on
screen - which is a different claim, and the one that has broken here before:
a Plotly build that silently drew nothing for half the trace types returned
perfectly good JSON the whole time.

Everything drives the app's own code path (`window.__thAsk`, the controls, the
export buttons) rather than fetching in parallel, so a capture that passes is
evidence about the product rather than about the test.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

ROWS = [
    {"order_date": "2024-01-15", "region": "North", "revenue": 1200},
    {"order_date": "2024-02-15", "region": "North", "revenue": 1500},
    {"order_date": "2024-03-15", "region": "North", "revenue": 1800},
    {"order_date": "2024-01-15", "region": "South", "revenue": 900},
    {"order_date": "2024-02-15", "region": "South", "revenue": 850},
    {"order_date": "2024-03-15", "region": "South", "revenue": 700},
]

DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser() -> Any:
    with sync_playwright() as pw:
        instance = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        yield instance
        instance.close()


@pytest.fixture(autouse=True)
def _settled(quiet: None) -> None:
    """Every browser test starts on an idle box."""


def _app(browser: Any, server: Any, viewport: dict, theme: str = "light") -> Any:
    """A signed-in app page, with console errors collected."""
    context = browser.new_context(
        viewport=viewport,
        color_scheme=theme,
        is_mobile=viewport["width"] < 500,
        has_touch=viewport["width"] < 500,
        accept_downloads=True,
    )
    page = context.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))

    page.goto(f"{server.base}/app", wait_until="networkidle", timeout=30000)
    # An anonymous visitor now lands on the product rather than a form; the
    # header carries the way in for someone who has already decided.
    page.click("button:has-text('Sign in')")
    page.wait_for_selector("input.signin-input", timeout=10000)
    page.fill("input.signin-input", f"browser-{page.evaluate('Date.now()')}@twohelixes.test")
    page.click("button[type=submit]")
    page.wait_for_selector("textarea.ask-input", timeout=15000)
    return page


def _ask(page: Any) -> None:
    page.evaluate(
        "(payload) => window.__thAsk(payload.q, {data: payload.data})",
        {"q": "how did revenue trend by region?", "data": ROWS},
    )
    page.wait_for_selector(".chart-plot.js-plotly-plot", timeout=180000)
    page.wait_for_timeout(600)


def test_a_question_draws_a_chart_on_screen(browser: Any, server: Any):
    page = _app(browser, server, DESKTOP)
    _ask(page)

    marks = page.evaluate(
        "() => document.querySelectorAll('.chart-plot .main-svg path,"
        " .chart-plot .main-svg rect, .chart-plot .main-svg text').length"
    )
    # Plotly ignores a trace type it cannot draw without erroring, so an empty
    # plot is the failure mode to assert against, not an exception.
    assert marks > 20, f"the chart rendered {marks} marks"
    assert page.errors == [], page.errors


def test_changing_the_chart_type_redraws_without_a_model_call(browser: Any, server: Any):
    """Editing by hand is free; that is a product promise, not an optimisation."""
    page = _app(browser, server, DESKTOP)
    _ask(page)

    # Read the balance through the page's own session: the account under test
    # is the browser's, not the API client's.
    balance = (
        "async () => (await (await fetch('/v1/billing/balance',"
        " {credentials: 'same-origin'})).json())"
    )
    before = page.evaluate(balance)
    page.select_option("[data-control=chart]", "bar")
    page.wait_for_timeout(2000)
    after = page.evaluate(balance)

    kind = page.evaluate(
        "() => document.querySelector('.chart-plot')?.data?.[0]?.type ?? ''"
    )
    assert kind == "bar", kind
    assert after["credits"] == before["credits"], "a manual edit charged the user"
    assert after["free_queries_left"] == before["free_queries_left"]


def test_switching_to_a_form_with_no_axes_and_back_does_not_break_the_chart(
    browser: Any, server: Any
):
    """Two clicks in the controls used to empty the card.

    `Plotly.react` cannot cross between forms that have cartesian axes and
    forms that do not: treemap to scatter threw "Cannot read properties of
    undefined (reading 'range')" and left nothing on screen.
    """
    page = _app(browser, server, DESKTOP)
    _ask(page)

    for chart_type in ("treemap", "scatter", "table", "line"):
        page.select_option("[data-control=chart]", chart_type)
        page.wait_for_timeout(1800)

    marks = page.evaluate(
        "() => document.querySelectorAll('.chart-plot .main-svg path,"
        " .chart-plot .main-svg text').length"
    )
    assert marks > 10, f"the chart came back with {marks} marks"
    assert page.errors == [], page.errors


def test_the_answer_exports_as_a_notebook_from_the_page(browser: Any, server: Any):
    page = _app(browser, server, DESKTOP)
    _ask(page)

    with page.expect_download(timeout=30000) as download:
        page.click("button:has-text('Notebook (.ipynb)')")
    path = download.value.path()
    document = json.loads(open(path).read())

    assert download.value.suggested_filename.endswith(".ipynb")
    assert document["nbformat"] == 4
    # The rows travel with it, or the notebook opens to an empty frame.
    assert any("ROWS" in "".join(cell["source"]) for cell in document["cells"])


def test_the_chart_survives_a_theme_switch(browser: Any, server: Any):
    """Plotly bakes theme colours into the figure, so this is a full redraw."""
    page = _app(browser, server, DESKTOP)
    _ask(page)

    page.click(".theme-toggle")
    page.wait_for_timeout(1500)

    paper = page.evaluate(
        "() => document.querySelector('.chart-plot')?._fullLayout?.paper_bgcolor ?? ''"
    )
    assert paper, "the chart lost its layout across the switch"
    assert page.errors == [], page.errors


def test_the_app_fits_a_phone(browser: Any, server: Any):
    page = _app(browser, server, PHONE)
    _ask(page)

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"the app scrolls sideways by {overflow}px on a phone"

    # The chart card must not push its own plot outside itself either.
    spill = page.evaluate(
        "() => {const c = document.querySelector('.chart-view');"
        " const p = document.querySelector('.chart-plot');"
        " if (!c || !p) return 0;"
        " return Math.round(p.getBoundingClientRect().right - c.getBoundingClientRect().right);}"
    )
    assert spill <= 1, f"the plot overflows its card by {spill}px"
    assert page.errors == [], page.errors


def test_the_trace_shows_the_agent_working(browser: Any, server: Any):
    """The product's main feature: you watch it, rather than waiting on a spinner."""
    page = _app(browser, server, DESKTOP)
    page.evaluate(
        "(payload) => window.__thAsk(payload.q, {data: payload.data})",
        {"q": "how did revenue trend by region?", "data": ROWS},
    )
    page.wait_for_selector(".trace-stage", timeout=60000)
    stages = page.locator(".trace-stage").count()
    page.wait_for_selector(".chart-plot.js-plotly-plot", timeout=180000)

    assert stages >= 1
    assert page.locator(".trace-stage").count() >= stages
