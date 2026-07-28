"""End-to-end: building and configuring a dashboard in a browser.

The unit tests prove the routes agree with each other. These prove the thing
a person actually does works: make a board, put a real chart on it, change how
that chart is drawn, change the board itself, and come back to find all of it
still true.

"Still true" is the point of nearly every assertion here. A dashboard is the
one surface in this product that is supposed to outlive the session that made
it, so every configuration change is checked twice - once on screen, and once
after a reload that re-reads it from the server. A control that redraws
locally and never persists looks perfect in a screenshot.

The panes are built through the builder rather than by asking questions, so
the suite costs no model calls and stays fast enough to keep. One test does
ask, because the streaming path onto a board is its own machinery.
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import sync_playwright  # noqa: E402

DESKTOP = {"width": 1440, "height": 900}
PHONE = {"width": 390, "height": 844}

# One pane, built by hand: the orders sample, summed by region. Every column
# named here is a real column of that sample - a config naming a column that
# does not exist still draws something, which would make this suite pass while
# charting nothing.
PANE = {
    "sample": "orders",
    "steps": [],
    "config": {
        "chart_type": "bar",
        "x": "region",
        "y": "net_amount",
        "agg": "sum",
        "title": "Revenue by region",
    },
}


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
    context = browser.new_context(
        viewport=viewport,
        color_scheme=theme,
        is_mobile=viewport["width"] < 500,
        has_touch=viewport["width"] < 500,
    )
    page = context.new_page()
    page.errors = []  # type: ignore[attr-defined]
    page.on("console", lambda m: page.errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: page.errors.append(str(e)))

    page.goto(f"{server.base}/app", wait_until="networkidle", timeout=30000)
    page.click("button:has-text('Sign in')")
    page.wait_for_selector("input.signin-input", timeout=10000)
    page.fill("input.signin-input", f"dash-{page.evaluate('Date.now()')}@twohelixes.test")
    page.click("button[type=submit]")
    page.wait_for_selector("textarea.ask-input", timeout=15000)
    return page


def _board_with_panes(page: Any, panes: list[dict]) -> str:
    """A board carrying real charts, built through the product's own routes.

    Through `fetch` rather than the composer because this is the *fixture*, not
    the thing under test; the composer gets driven by hand in its own test.
    """
    return page.evaluate(
        """async (panes) => {
          const post = (path, body) => fetch(path, {
            method: 'POST',
            credentials: 'same-origin',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
          }).then(r => r.json());

          const board = await post('/v1/dashboards', {title: 'Sales overview'});
          for (const pane of panes) {
            const preview = await post('/v1/builder/preview', pane);
            const saved = await post('/v1/builder/save', {
              ...pane, figure: preview.figure, dashboard_id: board.id,
            });
            await post(`/v1/dashboards/${board.id}/charts`, {chart_id: saved.chart_id});
          }
          return board.id;
        }""",
        panes,
    )


def _open(page: Any, board: str) -> None:
    page.evaluate("(id) => window.__thDashboard(id)", board)
    page.wait_for_selector(".dash-tile", timeout=30000)
    page.wait_for_timeout(1200)


def _marks(page: Any, tile: int = 0) -> int:
    return page.evaluate(
        "(i) => {const t = document.querySelectorAll('.dash-tile')[i];"
        " return t ? t.querySelectorAll('.main-svg path, .main-svg rect,"
        " .main-svg text').length : 0;}",
        tile,
    )


def _trace_type(page: Any, tile: int = 0) -> str:
    return page.evaluate(
        "(i) => {const p = document.querySelectorAll('.dash-plot')[i];"
        " return p && p.data ? String(p.data[0]?.type ?? '') : '';}",
        tile,
    )


# -- the board draws -------------------------------------------------------


def test_a_board_draws_the_charts_on_it(browser: Any, server: Any):
    """Plotly ignores a trace type it cannot draw without erroring, so an empty
    tile is the failure mode to assert against rather than an exception."""
    page = _app(browser, server, DESKTOP)
    _open(page, _board_with_panes(page, [PANE]))

    assert page.locator(".dash-tile").count() == 1
    assert _marks(page) > 20, f"the tile rendered {_marks(page)} marks"
    assert _trace_type(page) == "bar"
    assert page.errors == [], page.errors


def test_a_pane_is_a_display_not_a_workbench(browser: Any, server: Any):
    """No modebar on a tile. It sat over the data at phone width."""
    page = _app(browser, server, DESKTOP)
    _open(page, _board_with_panes(page, [PANE]))
    assert page.locator(".dash-tile .modebar").count() == 0


# -- configuring the chart in a tile ---------------------------------------


def test_changing_a_tiles_chart_type_redraws_it_and_sticks(browser: Any, server: Any):
    page = _app(browser, server, DESKTOP)
    board = _board_with_panes(page, [PANE])
    _open(page, board)

    page.click(".dash-tile button:has-text('Edit')")
    page.wait_for_selector(".dash-tile-editor [data-control=chart]", timeout=15000)
    page.select_option(".dash-tile-editor [data-control=chart]", "hbar")
    page.wait_for_timeout(2500)

    assert _trace_type(page) == "bar", "the tile lost its chart entirely"
    assert page.evaluate(
        "() => document.querySelector('.dash-plot')?.data?.[0]?.orientation ?? ''"
    ) == "h", "the horizontal bar came back vertical"

    # Reopened from the server, not from the view that just drew it.
    _open(page, board)
    assert page.evaluate(
        "() => document.querySelector('.dash-plot')?.data?.[0]?.orientation ?? ''"
    ) == "h", "the chart type did not survive a reload"
    assert page.errors == [], page.errors


def test_changing_a_tiles_columns_redraws_it_and_sticks(browser: Any, server: Any):
    """The x channel is the one people reach for first and the one that has the
    furthest to travel: it changes what the chart is *about*."""
    page = _app(browser, server, DESKTOP)
    board = _board_with_panes(page, [PANE])
    _open(page, board)

    page.click(".dash-tile button:has-text('Edit')")
    page.wait_for_selector(".dash-tile-editor [data-control=x]", timeout=15000)
    page.select_option(".dash-tile-editor [data-control=x]", "channel")
    page.wait_for_timeout(2500)

    categories = page.evaluate(
        "() => (document.querySelector('.dash-plot')?.data?.[0]?.x ?? []).map(String)"
    )
    assert categories, "the tile came back with no categories"
    assert "Europe" not in categories, "still grouped by region"

    # The caption follows. A tile captioned "Revenue by region" over a chart
    # of channels is worse than one with no caption at all.
    caption = page.text_content(".dash-tile-title")
    assert "region" not in (caption or "").lower(), caption

    _open(page, board)
    after = page.evaluate(
        "() => (document.querySelector('.dash-plot')?.data?.[0]?.x ?? []).map(String)"
    )
    assert after == categories, "the column change did not survive a reload"
    assert page.text_content(".dash-tile-title") == caption, "the rename was not saved"
    assert page.errors == [], page.errors


def test_a_tile_does_not_print_its_title_twice(browser: Any, server: Any):
    """The tile has a caption; the figure inside it must not repeat it - and
    a figure title is the copy that goes stale when the chart changes."""
    page = _app(browser, server, DESKTOP)
    _open(page, _board_with_panes(page, [PANE]))
    # The DOM, not `_fullLayout.title`: Plotly fills that with its own
    # "Click to enter Plot title" placeholder, which is never drawn.
    drawn = page.evaluate(
        "() => [...document.querySelectorAll('.dash-plot .gtitle')]"
        ".map(n => n.textContent.trim()).filter(Boolean)"
    )
    assert drawn == [], drawn


def test_a_manual_tile_edit_costs_nothing(browser: Any, server: Any):
    """Editing by hand is free; that is a product promise, not an optimisation."""
    page = _app(browser, server, DESKTOP)
    _open(page, _board_with_panes(page, [PANE]))

    balance = (
        "async () => (await (await fetch('/v1/billing/balance',"
        " {credentials: 'same-origin'})).json())"
    )
    before = page.evaluate(balance)
    page.click(".dash-tile button:has-text('Edit')")
    page.wait_for_selector(".dash-tile-editor [data-control=chart]", timeout=15000)
    page.select_option(".dash-tile-editor [data-control=chart]", "area")
    page.wait_for_timeout(2500)
    after = page.evaluate(balance)

    assert after["credits"] == before["credits"], "a manual tile edit charged the user"
    assert after["free_queries_left"] == before["free_queries_left"]


def test_a_tile_with_no_axes_and_back_does_not_break(browser: Any, server: Any):
    """`Plotly.react` cannot cross between forms that have cartesian axes and
    forms that do not. In the chart card that emptied the whole answer; a
    dashboard runs the same risk once per tile."""
    page = _app(browser, server, DESKTOP)
    _open(page, _board_with_panes(page, [PANE]))

    page.click(".dash-tile button:has-text('Edit')")
    page.wait_for_selector(".dash-tile-editor [data-control=chart]", timeout=15000)
    for chart_type in ("treemap", "bar", "pie", "line"):
        page.select_option(".dash-tile-editor [data-control=chart]", chart_type)
        page.wait_for_timeout(2200)

    assert _marks(page) > 10, f"the tile came back with {_marks(page)} marks"
    assert page.errors == [], page.errors


# -- configuring the board -------------------------------------------------


def test_renaming_a_board_sticks(browser: Any, server: Any):
    page = _app(browser, server, DESKTOP)
    board = _board_with_panes(page, [PANE])
    _open(page, board)

    page.fill(".dash-title", "Q3 review")
    page.press(".dash-title", "Tab")
    page.wait_for_timeout(1500)

    _open(page, board)
    assert page.input_value(".dash-title") == "Q3 review"


def test_resizing_a_tile_sticks(browser: Any, server: Any):
    """The saved layout is a column span, not pixel geometry, so it has to
    survive being reopened on a different screen - starting with this one."""
    page = _app(browser, server, DESKTOP)
    board = _board_with_panes(page, [PANE, PANE])
    _open(page, board)

    page.click(".dash-tile [data-size='2']")
    page.wait_for_timeout(1500)
    assert page.locator(".dash-tile").first.get_attribute("data-width") == "2"

    _open(page, board)
    assert page.locator(".dash-tile").first.get_attribute("data-width") == "2"
    assert "span-2" in (page.locator(".dash-tile").first.get_attribute("class") or "")


def test_removing_a_tile_sticks(browser: Any, server: Any):
    page = _app(browser, server, DESKTOP)
    board = _board_with_panes(page, [PANE, PANE])
    _open(page, board)
    assert page.locator(".dash-tile").count() == 2

    page.click(".dash-tile button[title='Remove from dashboard']")
    page.wait_for_timeout(1500)
    assert page.locator(".dash-tile").count() == 1

    _open(page, board)
    assert page.locator(".dash-tile").count() == 1


def test_a_new_board_is_created_and_opened_from_the_list(browser: Any, server: Any):
    """The nav is the feature. The grid shipped before there was any way to
    reach it, which is the same as it not shipping."""
    page = _app(browser, server, DESKTOP)
    page.click(".app-nav-item:has-text('Dashboards')")
    page.wait_for_selector(".dash-list", timeout=15000)
    page.click("button:has-text('New dashboard')")

    # Straight into the board, with the way to fill it already open.
    page.wait_for_selector(".dash-composer-ask", timeout=15000)
    assert page.locator(".dash-composer").is_visible()
    assert page.errors == [], page.errors


# -- theme -----------------------------------------------------------------


def test_a_board_built_in_daylight_is_readable_at_night(browser: Any, server: Any):
    """The tile's colours are baked into the saved figure. Without the server
    restyling for the viewer, a light-built board is white plates on a dark
    page - the same bug the dataset pages solve with theme variables."""
    page = _app(browser, server, DESKTOP, theme="light")
    board = _board_with_panes(page, [PANE])
    _open(page, board)
    light = page.evaluate(
        "() => document.querySelector('.dash-plot')?._fullLayout?.paper_bgcolor ?? ''"
    )

    page.click(".theme-toggle")
    _open(page, board)
    dark = page.evaluate(
        "() => document.querySelector('.dash-plot')?._fullLayout?.paper_bgcolor ?? ''"
    )

    assert light and dark, "a tile lost its layout across the switch"
    assert light != dark, f"the tile kept its light paper ({light}) on a dark board"
    assert _marks(page) > 20, "the tile stopped drawing after the switch"


# -- asking for a pane -----------------------------------------------------


def test_asking_in_the_composer_puts_a_chart_on_the_board(browser: Any, server: Any):
    """The one test here that spends a query: the streaming path onto a board
    is its own machinery, and the chart has to land in the *layout* as well as
    in the charts table or the board will not show what it just made."""
    page = _app(browser, server, DESKTOP)
    board = page.evaluate(
        """async () => (await (await fetch('/v1/dashboards', {
          method: 'POST', credentials: 'same-origin',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({title: 'Asked'}),
        })).json()).id"""
    )
    page.evaluate("(id) => window.__thDashboard(id)", board)
    page.wait_for_selector(".dash-composer-ask input", timeout=20000)

    page.fill(".dash-composer-ask input", "total net amount by region")
    page.click(".dash-composer-ask button[type=submit]")
    page.wait_for_selector(".dash-tile", timeout=240000)
    page.wait_for_timeout(1500)

    assert _marks(page) > 20, "the asked-for pane drew nothing"

    layout = page.evaluate(
        "async (id) => (await (await fetch(`/v1/dashboards/${id}`,"
        " {credentials: 'same-origin'})).json()).layout",
        board,
    )
    assert len(layout) == 1, f"the chart is not in the saved layout: {layout}"


# -- phone -----------------------------------------------------------------


def test_a_board_fits_a_phone(browser: Any, server: Any):
    page = _app(browser, server, PHONE)
    _open(page, _board_with_panes(page, [PANE, PANE]))

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"the board scrolls sideways by {overflow}px"

    # The scroller, not the plot: a table tile's plot is deliberately wider
    # than the tile and scrolls inside it. What must never overflow is the box
    # that holds it.
    spill = page.evaluate(
        "() => {let worst = 0;"
        " for (const t of document.querySelectorAll('.dash-tile')) {"
        "   const p = t.querySelector('.dash-plot-scroll'); if (!p) continue;"
        "   worst = Math.max(worst, Math.round("
        "     p.getBoundingClientRect().right - t.getBoundingClientRect().right));}"
        " return worst;}"
    )
    assert spill <= 1, f"a tile's plot overflows its card by {spill}px"

    # Width controls do nothing in a one-column grid, and a control that does
    # nothing is worse than no control.
    assert page.locator(".dash-tile .dash-sizes").first.is_visible() is False
    assert page.errors == [], page.errors


def test_a_table_tile_scrolls_itself_and_not_the_page(browser: Any, server: Any):
    """Ten columns do not fit a 390px tile, and squeezing them produces ten
    columns of ellipsis. The tile scrolls; the board does not."""
    page = _app(browser, server, PHONE)
    _open(page, _board_with_panes(page, [PANE]))

    page.click(".dash-tile button:has-text('Edit')")
    page.wait_for_selector(".dash-tile-editor [data-control=chart]", timeout=15000)
    page.select_option(".dash-tile-editor [data-control=chart]", "table")
    page.wait_for_timeout(2500)

    scrolls = page.evaluate(
        "() => {const s = document.querySelector('.dash-plot-scroll');"
        " return s ? s.scrollWidth - s.clientWidth : -1;}"
    )
    assert scrolls > 0, "the table was squeezed to fit instead of scrolling"

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"the table pushed the page sideways by {overflow}px"


def test_the_tile_editor_is_usable_on_a_phone(browser: Any, server: Any):
    page = _app(browser, server, PHONE)
    _open(page, _board_with_panes(page, [PANE]))

    page.click(".dash-tile button:has-text('Edit')")
    page.wait_for_selector(".dash-tile-editor [data-control=chart]", timeout=15000)

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1, f"the editor pushes the board sideways by {overflow}px"

    # Five channels, and every one of them reachable by thumb.
    small = page.evaluate(
        "() => [...document.querySelectorAll('.dash-tile-controls-grid .control-input')]"
        ".filter(n => n.getBoundingClientRect().height < 36).length"
    )
    assert page.locator(".dash-tile-controls-grid .control").count() == 5
    assert small == 0, f"{small} channel pickers are under 36px tall"
