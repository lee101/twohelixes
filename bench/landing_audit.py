"""Check homepage layouts and the real sample/signup funnel on a local test server.

Use a server with an isolated database. --query spends one anonymous sample
query and makes real model calls; signup uses a synthetic local-test identity.
"""

import argparse
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:7486")
    parser.add_argument("--out", type=Path, default=Path("visualbench/landing-refresh/funnel"))
    parser.add_argument("--query", action="store_true")
    args = parser.parse_args()
    if urlsplit(args.base).hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("Use an isolated local test server, not customer accounts.")
    args.out.mkdir(parents=True, exist_ok=True)
    report = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        for width in (390, 1440):
            for theme in ("light", "dark"):
                context = browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(args.base, wait_until="networkidle")
                page.screenshot(path=str(args.out / f"hero-{theme}-{width}.png"))
                assert page.locator("h1").count() == 1
                assert page.locator(".intelligence-helix svg").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                # Capture only fixed-label events in this test; no analytics delivery.
                page.evaluate("window.__events=[]; window.th=(...args)=>window.__events.push(args)")
                page.locator('[data-conversion="hero-signup"]').click()
                page.locator('#signin-overlay[open]').wait_for()
                assert page.locator('#signin-title').inner_text() == 'Create account'
                assert page.evaluate("window.__events.some(e => e[1] === 'landing_cta_clicked' && e[2].placement === 'hero-signup')")
                page.keyboard.press("Escape")
                assert not page.locator('#signin-overlay').is_visible()
                page.locator('[data-conversion="hero-sample"]').click()
                page.wait_for_url("**/app?**")
                page.locator("textarea.ask-input").wait_for()
                assert "net revenue" in page.locator("textarea.ask-input").input_value()
                assert not page.locator("input.signin-input").count()
                assert not errors, errors
                report.append({"width": width, "theme": theme, "overflow": False,
                               "sample_prefilled": True, "signup_opens": True, "console_errors": errors})
                context.close()

        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        page = context.new_page()
        page.goto(args.base, wait_until="networkidle")
        if args.query:
            page.locator('[data-conversion="hero-sample"]').click()
            page.locator("textarea.ask-input").wait_for()
            page.locator("textarea.ask-input").press("Enter")
            assert not page.locator("input.signin-input").count(), "Signup replaced the sample result"
            page.wait_for_selector(".chart-plot.js-plotly-plot", timeout=180000)
            page.wait_for_function("document.querySelectorAll('.chart-plot .scatterlayer .trace').length > 0", timeout=30000)
            page.screenshot(path=str(args.out / "sample-result.png"), full_page=True)
            assert page.get_by_role('button', name='Keep exploring — create account').is_visible()
            report.append({"journey": "anonymous-sample-to-chart", "passed": True})
            page.goto(args.base, wait_until="networkidle")

        page.locator('[data-conversion="hero-signup"]').click()
        page.locator('#signin-overlay[open]').wait_for()
        page.fill('#signin-email', f'landing-{time.time_ns()}@twohelixes.test')
        page.fill('#signin-password', 'Landing-audit-2026!')
        page.click('#signin-submit')
        page.wait_for_url('**/app', timeout=30000)
        page.locator('textarea.ask-input').wait_for()
        identity = page.request.get(args.base + '/v1/me').json()
        assert identity['signed_in']
        page.screenshot(path=str(args.out / "signup-complete.png"), full_page=True)
        report.append({"journey": "homepage-signup-to-workspace", "passed": True})
        context.close()
        browser.close()
    (args.out / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
