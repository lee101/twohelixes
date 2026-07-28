"""Render the social preview card.

A link preview is the one place the product cannot be a live SVG: every
scraper wants a raster, and none of them run JavaScript. So the card is drawn
here from the same mark, the same tokens and a real chart off the live
exporter, and written as a PNG the build copies into static/art/.

Regenerate after a brand change:

    PYTHONPATH=interp pixi run python bench/make_og.py

It is a build step rather than a route because it needs a browser, and the
server must never need one.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "assets" / "og-1200.png"

SIZE = (1200, 630)


def card(mode: str = "light") -> str:
    sys.path.insert(0, str(ROOT / "interp"))
    from twohelixes.charts import helix, palette
    from twohelixes.routes import showcase

    face = palette.surface(mode)
    brand = palette.brand(mode)
    chart = showcase.chart_svg("revenue", mode, 560, 340)

    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  *{{box-sizing:border-box;margin:0}}
  body{{width:{SIZE[0]}px;height:{SIZE[1]}px;overflow:hidden;
    font-family:Inter,-apple-system,'Segoe UI',Roboto,sans-serif;
    background:
      radial-gradient(70% 90% at 88% 0%,
        color-mix(in srgb,{brand} 22%,transparent) 0%,transparent 62%),
      {face.background};
    color:{face.text_primary};display:flex;align-items:center;gap:56px;
    padding:64px 72px;position:relative}}
  .mark{{position:absolute;left:-90px;top:-120px;width:420px;opacity:.14}}
  .mark svg{{width:100%;height:auto}}
  .copy{{position:relative;flex:1 1 46%}}
  .brand{{display:flex;align-items:center;gap:12px;font-size:26px;
    font-weight:660;letter-spacing:-.01em}}
  .brand svg{{width:38px;height:38px}}
  h1{{margin-top:28px;font-size:60px;line-height:1.04;letter-spacing:-.035em;
    font-weight:700;max-width:12ch}}
  p{{margin-top:22px;font-size:23px;line-height:1.45;color:{face.text_secondary};
    max-width:26ch}}
  .figure{{position:relative;flex:0 0 560px;background:{face.background};
    border:1px solid {face.grid};border-radius:18px;padding:12px;
    box-shadow:0 24px 60px rgba(16,16,14,.14)}}
  .figure svg{{width:100%;height:auto;display:block}}
</style></head><body>
  <span class="mark">{helix.logo(420, mode, uid="ogmark")}</span>
  <div class="copy">
    <div class="brand">{helix.logo(38, mode, uid="ogbrand")}<span>twoHelixes</span></div>
    <h1>Charts of your data, from a sentence</h1>
    <p>Ask in plain language. It finds the columns, joins them, shapes them and
    draws the chart.</p>
  </div>
  <div class="figure">{chart}</div>
</body></html>"""


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is not installed", file=sys.stderr)
        return 2

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(
            viewport={"width": SIZE[0], "height": SIZE[1]}, device_scale_factor=1
        )
        page.set_content(card("light"), wait_until="load")
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT))
        browser.close()

    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} kB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
