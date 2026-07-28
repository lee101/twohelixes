"""Server-rendered pages: marketing, the app shell, sharing, and sign-in.

Marketing pages are rendered in Mojo's worker on demand rather than built by a
static generator, because they carry live values - the free-query allowance and
the credit pack prices come from config, so a pricing change is one constant.
"""

from __future__ import annotations

import html
import json
import time
from typing import Any

from twohelixes import auth, config, router, store
from twohelixes.charts import helix, palette
from twohelixes.routes import showcase
from twohelixes.routes.billing import CREDIT_PACKS, PLANS

NAV = (
    ("/", "Home"),
    ("/features", "Features"),
    ("/datasets", "Datasets"),
    ("/pricing", "Pricing"),
    ("/docs", "Docs"),
)


def _css() -> str:
    light = palette.as_css_variables("light")
    dark = palette.as_css_variables("dark")
    light_vars = ";".join(f"{k}:{v}" for k, v in light.items())
    dark_vars = ";".join(f"{k}:{v}" for k, v in dark.items())

    return f"""
/* ---------------------------------------------------------------------
   Design tokens. The series colours come from the validated chart palette
   and must not be hand-edited; everything else here is UI chrome and is
   free to change.
   --------------------------------------------------------------------- */
*,*::before,*::after{{box-sizing:border-box}}
body,h1,h2,h3,h4,p,figure,ul,ol{{margin:0}}
ul,ol{{padding:0;list-style:none}}
img,svg{{max-width:100%;display:block}}
button{{font:inherit;color:inherit}}

:root{{
  color-scheme:light;
  {light_vars};
  --panel:#ffffff;
  --panel-2:#f7f7f5;
  --border:#e6e5e1;
  --border-strong:#d5d4cf;
  /* One accent for the whole product, and it is the brand hue - which is the
     same colour as the first chart series, so the page and the chart in it
     belong to each other. Nothing outside data and status carries a second
     hue: an earlier version used the green series for bullets, badges and GET
     methods, which made a two-colour site out of a one-colour brand. */
  --accent:var(--brand);
  --accent-strong:var(--brand-deep);
  --accent-soft:color-mix(in srgb,var(--brand) 10%,transparent);
  --accent-ring:color-mix(in srgb,var(--brand) 35%,transparent);
  /* Reserved for real status. Not chrome. */
  --ok:var(--status-good);

  /* One spacing scale, used everywhere. */
  --s1:.25rem; --s2:.5rem; --s3:.75rem; --s4:1rem;
  --s5:1.5rem; --s6:2rem; --s7:3rem; --s8:4rem;

  --r-sm:8px; --r-md:12px; --r-lg:16px; --r-full:999px;

  /* Two-layer shadows: a tight contact shadow plus a soft ambient one.
     A single large blur reads as a smudge at these sizes. */
  --sh-1:0 1px 2px rgba(16,16,14,.05), 0 1px 3px rgba(16,16,14,.04);
  --sh-2:0 2px 4px rgba(16,16,14,.05), 0 6px 16px rgba(16,16,14,.06);
  --sh-3:0 8px 24px rgba(16,16,14,.10), 0 2px 6px rgba(16,16,14,.06);

  --shell:min(1140px,100% - 2.5rem);
  --font:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}}

@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{
    color-scheme:dark;
    {dark_vars};
    --panel:#1f1f1e;
    --panel-2:#252523;
    --border:#302f2d;
    --border-strong:#3d3c39;
    --accent:var(--brand);
    --accent-strong:var(--brand-light);
    --accent-soft:color-mix(in srgb,var(--brand) 16%,transparent);
    --accent-ring:color-mix(in srgb,var(--brand) 45%,transparent);
    --sh-1:0 1px 2px rgba(0,0,0,.4);
    --sh-2:0 2px 6px rgba(0,0,0,.45), 0 8px 20px rgba(0,0,0,.35);
    --sh-3:0 10px 30px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.4);
  }}
}}
:root[data-theme="dark"]{{
  color-scheme:dark;
  {dark_vars};
  --panel:#1f1f1e; --panel-2:#252523;
  --border:#302f2d; --border-strong:#3d3c39;
  --accent-strong:var(--brand-light);
  --accent-soft:color-mix(in srgb,var(--brand) 16%,transparent);
  --accent-ring:color-mix(in srgb,var(--brand) 45%,transparent);
  --sh-1:0 1px 2px rgba(0,0,0,.4);
  --sh-2:0 2px 6px rgba(0,0,0,.45), 0 8px 20px rgba(0,0,0,.35);
  --sh-3:0 10px 30px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.4);
}}

body{{margin:0;font-family:var(--font);background:var(--surface-1);
  color:var(--text-primary);line-height:1.55;-webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility}}
.shell{{width:var(--shell);margin-inline:auto}}
a{{color:var(--accent);text-decoration:none}}
a:hover{{text-decoration:underline}}
:focus-visible{{outline:2px solid var(--accent);outline-offset:2px;border-radius:4px}}

.visually-hidden{{position:absolute;width:1px;height:1px;overflow:hidden;
  clip-path:inset(50%);white-space:nowrap}}

/* Keyboard users reach the content without tabbing the whole nav. */
.skip{{position:absolute;left:-9999px;top:0}}
.skip:focus{{left:var(--s3);top:var(--s2);z-index:200;padding:.5rem .8rem;
  background:var(--panel);border:1px solid var(--border-strong);
  border-radius:var(--r-sm);box-shadow:var(--sh-2);font-weight:600}}

/* Body copy outside a card. Measure is capped here rather than at the call
   site, because an uncapped paragraph on a 1440px page runs past 140
   characters and the eye loses the line return. */
p.sub{{color:var(--text-secondary);max-width:62ch;font-size:1rem;
  line-height:1.6}}
p.sub + p.sub{{margin-top:var(--s3)}}

/* --- header ---------------------------------------------------------- */
header.site{{position:sticky;top:0;z-index:40;backdrop-filter:blur(10px);
  background:color-mix(in srgb,var(--surface-1) 86%,transparent);
  border-bottom:1px solid var(--border)}}
header.site .shell{{display:flex;align-items:center;gap:var(--s5);
  min-height:58px;flex-wrap:wrap}}
.brand{{display:flex;align-items:center;gap:.55rem;font-weight:660;
  color:var(--text-primary);font-size:1.02rem;letter-spacing:-.01em}}
.brand:hover{{text-decoration:none}}
.brand svg{{width:28px;height:28px}}
nav.main{{display:flex;gap:1.35rem;margin-left:auto;flex-wrap:wrap}}
nav.main a{{color:var(--text-secondary);font-size:.92rem;font-weight:500;
  transition:color .15s}}
nav.main a:hover{{color:var(--text-primary);text-decoration:none}}
nav.main a[aria-current]{{color:var(--text-primary);font-weight:600}}

/* The app has a theme toggle, so the marketing pages need one too - otherwise
   a visitor who chose dark inside the product lands back on a light homepage.
   Both write the same localStorage key. */
.icon-btn{{display:inline-grid;place-items:center;width:34px;height:34px;
  border-radius:var(--r-sm);border:1px solid var(--border);
  background:var(--panel);color:var(--text-secondary);cursor:pointer;
  padding:0;flex:0 0 auto;transition:border-color .15s,color .15s}}
.icon-btn:hover{{border-color:var(--border-strong);color:var(--text-primary)}}
.icon-btn svg{{width:17px;height:17px;stroke:currentColor;fill:none;
  stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}}
.icon-btn .i-sun{{display:none}}
:root[data-theme="dark"] .icon-btn .i-sun{{display:block}}
:root[data-theme="dark"] .icon-btn .i-moon{{display:none}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]) .icon-btn .i-sun{{display:block}}
  :root:not([data-theme="light"]) .icon-btn .i-moon{{display:none}}
}}

/* --- page heads (features, pricing, docs) ---------------------------- */
.page-head{{padding:clamp(1.75rem,4vw,3rem) 0 0}}
.page-head h1{{font-size:clamp(1.95rem,4.4vw,2.9rem);letter-spacing:-.03em;
  line-height:1.06;font-weight:700;max-width:21ch}}
.page-head .kicker{{margin-bottom:.5rem}}
.page-head p.sub{{margin-top:var(--s3);font-size:clamp(1rem,1.5vw,1.1rem);
  max-width:54ch}}

/* --- buttons --------------------------------------------------------- */
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:.45rem;
  padding:.58rem 1.05rem;border-radius:var(--r-sm);font-weight:600;
  font-size:.93rem;border:1px solid transparent;cursor:pointer;
  text-decoration:none;white-space:nowrap;
  transition:transform .12s ease,box-shadow .15s ease,background .15s,border-color .15s}}
.btn:hover{{text-decoration:none}}
.btn:active{{transform:translateY(1px)}}
.btn-primary{{background:var(--accent);color:#fff;box-shadow:var(--sh-1)}}
.btn-primary:hover{{box-shadow:var(--sh-2);filter:brightness(1.06)}}
.btn-ghost{{border-color:var(--border-strong);color:var(--text-primary);
  background:var(--panel)}}
.btn-ghost:hover{{border-color:var(--text-muted);box-shadow:var(--sh-1)}}
.btn-small{{padding:.36rem .7rem;font-size:.85rem}}
.btn[disabled]{{opacity:.55;pointer-events:none}}

/* --- layout ---------------------------------------------------------- */
section{{padding:clamp(2rem,4vw,3.25rem) 0}}
section h2{{font-size:clamp(1.45rem,3vw,2rem);letter-spacing:-.02em;
  margin-bottom:var(--s2);font-weight:670;line-height:1.15}}
.lead-in{{color:var(--text-secondary);max-width:58ch;margin-bottom:var(--s5)}}
.grid{{display:grid;gap:var(--s4);grid-template-columns:1fr}}
@media(min-width:640px){{.grid{{grid-template-columns:repeat(2,1fr)}}}}
@media(min-width:980px){{.grid.three{{grid-template-columns:repeat(3,1fr)}}}}
.split{{display:grid;gap:var(--s6);grid-template-columns:1fr;align-items:center}}
@media(min-width:940px){{.split{{grid-template-columns:1fr 1fr}}}}
/* Centring a short paragraph against a tall list leaves it floating in the
   middle of a column of air; these pair by their top edge instead. */
.split.top{{align-items:start}}

.card{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:var(--s5);box-shadow:var(--sh-1);
  transition:border-color .15s,box-shadow .2s,transform .2s}}
.card:hover{{border-color:var(--border-strong);box-shadow:var(--sh-2);
  transform:translateY(-1px)}}
.card h3{{font-size:1rem;margin-bottom:.4rem;font-weight:640;
  letter-spacing:-.01em}}
.card p{{color:var(--text-secondary);font-size:.93rem}}
.card.fill{{display:flex;flex-direction:column;justify-content:center}}

/* --- hero ------------------------------------------------------------ */
.hero{{padding:clamp(2.25rem,5vw,4rem) 0 clamp(1.25rem,2.5vw,2rem)}}
.hero-layout{{display:grid;gap:clamp(1.75rem,3.5vw,3rem);
  grid-template-columns:1fr;align-items:center}}
@media(min-width:900px){{.hero-layout{{grid-template-columns:1fr 1.1fr}}}}
.hero h1{{margin-top:var(--s3);max-width:15ch;
  font-size:clamp(2.15rem,5.2vw,3.6rem);line-height:1.03;
  letter-spacing:-.032em;font-weight:700}}
.hero p.lede{{margin-top:var(--s4);color:var(--text-secondary);
  font-size:clamp(1rem,1.8vw,1.18rem);max-width:46ch;line-height:1.5}}
.hero .actions{{margin-top:var(--s5);display:flex;gap:.65rem;flex-wrap:wrap}}
.hero-note{{margin-top:var(--s3);color:var(--text-muted);font-size:.86rem}}
.eyebrow{{display:inline-flex;align-items:center;gap:.4rem;
  padding:.28rem .68rem;border-radius:var(--r-full);
  background:var(--accent-soft);color:var(--accent);
  font-size:.78rem;font-weight:650;letter-spacing:.005em}}

/* --- figures --------------------------------------------------------- */
.figure{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:.55rem;overflow:hidden;
  box-shadow:var(--sh-2)}}
.figure svg{{width:100%;height:auto;display:block;border-radius:var(--r-sm)}}
.figure figcaption{{padding:.5rem .55rem .2rem;color:var(--text-muted);
  font-size:.82rem;line-height:1.45}}
.figure figcaption b{{color:var(--text-secondary);font-weight:620}}
.gallery{{display:grid;gap:var(--s4);grid-template-columns:1fr}}
@media(min-width:820px){{.gallery{{grid-template-columns:repeat(2,1fr)}}}}

/* --- trace demo ------------------------------------------------------ */
.trace-demo{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:var(--s4);display:grid;gap:.4rem;
  box-shadow:var(--sh-2)}}
.trace-demo .askbox{{background:var(--panel-2);border:1px solid var(--border);
  border-radius:var(--r-sm);padding:.55rem .7rem;margin-bottom:.25rem;
  font-size:.94rem;color:var(--text-primary)}}
.trace-demo .askbox span{{display:block;margin-bottom:.15rem;font-size:.7rem;
  color:var(--text-muted);text-transform:uppercase;letter-spacing:.07em;
  font-weight:660}}
.tstep{{border:1px solid var(--border);border-radius:var(--r-sm);
  padding:.48rem .65rem;display:grid;gap:.2rem;background:var(--panel)}}
.tstep .top{{display:flex;align-items:center;gap:.45rem}}
.tstep .nm{{font-weight:620;font-size:.88rem}}
.tstep .ms{{margin-left:auto;color:var(--text-muted);font-size:.76rem;
  font-variant-numeric:tabular-nums}}
.tstep .said{{color:var(--text-secondary);font-size:.845rem;line-height:1.45}}
.tstep .dot{{width:6px;height:6px;border-radius:50%;background:var(--accent);
  flex:0 0 auto}}
.tstep.now{{border-color:var(--accent-ring);background:var(--accent-soft)}}
.tstep.now .dot{{background:var(--accent)}}

/* --- stats & chips --------------------------------------------------- */
.stats{{display:grid;gap:var(--s4);grid-template-columns:repeat(2,1fr)}}
@media(min-width:780px){{.stats{{grid-template-columns:repeat(4,1fr)}}}}
.stat{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-md);padding:var(--s4);box-shadow:var(--sh-1)}}
.stat .n{{font-size:clamp(1.4rem,2.6vw,1.85rem);font-weight:700;
  letter-spacing:-.025em;font-variant-numeric:tabular-nums;line-height:1.1;
  color:var(--text-primary)}}
.stat .l{{color:var(--text-muted);font-size:.84rem;margin-top:.2rem;
  line-height:1.4}}
.chips{{display:flex;flex-wrap:wrap;gap:.4rem}}
.chip{{border:1px solid var(--border);background:var(--panel);
  border-radius:var(--r-sm);padding:.32rem .6rem;font-size:.83rem;
  color:var(--text-secondary);box-shadow:var(--sh-1)}}

/* --- pricing --------------------------------------------------------- */
.price-grid{{display:grid;gap:var(--s4);grid-template-columns:1fr}}
@media(min-width:700px){{.price-grid{{grid-template-columns:repeat(2,1fr)}}}}
/* Four plans in a three-column grid strands the fourth on a row of its own,
   which reads as an afterthought rather than a tier. */
@media(min-width:1080px){{.price-grid{{grid-template-columns:repeat(4,1fr)}}}}
.plan{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:var(--s5);display:flex;
  flex-direction:column;box-shadow:var(--sh-1);position:relative;
  transition:border-color .15s,box-shadow .2s,transform .2s}}
.plan:hover{{box-shadow:var(--sh-2);transform:translateY(-2px)}}
.plan.highlight{{border-color:var(--accent);box-shadow:var(--sh-2)}}
.plan.highlight::after{{content:"Most popular";position:absolute;top:-10px;
  left:var(--s5);background:var(--accent);color:#fff;font-size:.7rem;
  font-weight:660;padding:.18rem .55rem;border-radius:var(--r-full);
  letter-spacing:.02em}}
.plan h3{{font-size:1rem;font-weight:640}}
.plan-blurb{{color:var(--text-secondary);font-size:.88rem;margin-top:.45rem;
  line-height:1.45}}
.plan-rate{{margin-top:.5rem;font-size:.82rem;font-weight:620;
  color:var(--accent)}}
.plan .amount{{font-size:2.1rem;font-weight:700;letter-spacing:-.03em;
  margin-top:.35rem;line-height:1.05}}
.plan .cadence{{color:var(--text-muted);font-size:.85rem}}
.plan ul{{margin:var(--s4) 0;display:grid;gap:.5rem}}
.plan li{{font-size:.91rem;color:var(--text-secondary);padding-left:1.4rem;
  position:relative;line-height:1.45}}
.plan li::before{{content:"";position:absolute;left:0;top:.5em;width:7px;
  height:7px;border-radius:2px;background:var(--accent)}}
.plan .btn{{margin-top:auto;width:100%}}

table.packs{{width:100%;border-collapse:collapse;margin-top:var(--s3)}}
table.packs th,table.packs td{{text-align:left;padding:.62rem .75rem;
  border-bottom:1px solid var(--border);font-size:.91rem}}
table.packs th{{color:var(--text-muted);font-weight:620;font-size:.75rem;
  text-transform:uppercase;letter-spacing:.05em}}
table.packs tbody tr:hover{{background:var(--panel-2)}}
.scroll-x{{overflow-x:auto}}

.rules{{display:grid;gap:.6rem;margin-top:.2rem}}
.rules li{{position:relative;padding-left:1.45rem;font-size:.91rem;
  color:var(--text-secondary);line-height:1.5}}
.rules li::before{{content:"";position:absolute;left:0;top:.5em;width:7px;
  height:7px;border-radius:2px;background:var(--accent)}}
.rules li b{{color:var(--text-primary);font-weight:620}}

/* --- data tables and openable traces ---------------------------------
   Used by the dataset pages, where the point is that the numbers behind a
   chart are one click away rather than a claim. */
table.data{{width:100%;border-collapse:collapse;font-family:var(--mono);
  font-size:.8rem}}
table.data th,table.data td{{text-align:left;padding:.4rem .6rem;
  border-bottom:1px solid var(--border);white-space:nowrap}}
table.data th{{color:var(--text-muted);font-weight:620;font-size:.7rem;
  text-transform:uppercase;letter-spacing:.05em;position:sticky;top:0;
  background:var(--panel)}}
table.data td.num{{text-align:right;font-variant-numeric:tabular-nums}}
table.data tbody tr:hover{{background:var(--panel-2)}}

details.trace{{border:1px solid var(--border);border-radius:var(--r-md);
  background:var(--panel);margin-top:var(--s4)}}
details.trace > summary{{cursor:pointer;padding:.7rem var(--s4);
  font-size:.88rem;font-weight:620;list-style:none;display:flex;
  align-items:center;gap:.5rem}}
details.trace > summary::-webkit-details-marker{{display:none}}
details.trace > summary::after{{content:"Show";margin-left:auto;
  font-weight:600;font-size:.78rem;color:var(--accent)}}
details.trace[open] > summary::after{{content:"Hide"}}
details.trace > summary::before{{content:"";width:6px;height:6px;
  border-radius:50%;background:var(--accent);flex:0 0 auto}}
details.trace > summary:hover{{background:var(--panel-2)}}
details.trace .body{{padding:0 var(--s4) var(--s4);display:grid;gap:.5rem}}

.ds-grid{{display:grid;gap:var(--s5);grid-template-columns:1fr;
  margin-top:var(--s5)}}
.ds-card{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);padding:var(--s4);display:flex;
  flex-direction:column;gap:.6rem}}
.ds-card:hover{{border-color:var(--border-strong);box-shadow:var(--sh-2)}}
.ds-card h3{{font-size:1.02rem;font-weight:640;letter-spacing:-.015em}}
.ds-card h3 a{{color:var(--text-primary)}}
.ds-card p{{color:var(--text-secondary);font-size:.9rem}}
.ds-card .figure{{margin:0}}
.ds-meta{{display:flex;gap:.9rem;flex-wrap:wrap;color:var(--text-muted);
  font-size:.78rem;font-family:var(--mono)}}
.ds-questions{{display:grid;gap:.3rem;margin-top:auto}}
.ds-questions a{{font-size:.87rem;color:var(--text-secondary);
  padding:.28rem 0;border-top:1px solid var(--border)}}
.ds-questions a:hover{{color:var(--accent)}}
.example{{margin-top:var(--s6)}}
.example h3{{font-size:1.15rem;letter-spacing:-.02em;font-weight:650}}
.example > p{{color:var(--text-secondary);font-size:.93rem;
  max-width:64ch;margin-top:.3rem}}
.example .figure{{margin-top:var(--s4)}}
.example-actions{{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:var(--s3)}}
@media(min-width:720px){{
  .ds-grid{{grid-template-columns:repeat(2,1fr)}}
}}
@media(min-width:1040px){{
  .ds-grid{{grid-template-columns:repeat(3,1fr)}}
}}

code,pre{{font-family:var(--mono);font-size:.86rem}}
pre{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-md);padding:var(--s4);overflow-x:auto;
  box-shadow:var(--sh-1)}}
:not(pre) > code{{background:var(--panel-2);border:1px solid var(--border);
  border-radius:6px;padding:.08em .35em;font-size:.85em}}

/* --- endpoint blocks ------------------------------------------------- */
.endpoint{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);box-shadow:var(--sh-1);overflow:hidden;
  display:flex;flex-direction:column;
  transition:border-color .15s,box-shadow .2s}}
.endpoint:hover{{border-color:var(--border-strong);box-shadow:var(--sh-2)}}
.endpoint > header{{display:flex;align-items:center;gap:.55rem;flex-wrap:wrap;
  padding:var(--s4) var(--s5) 0}}
.endpoint h3{{font-size:1rem;font-weight:640;letter-spacing:-.01em;
  padding:0 var(--s5);margin-top:.5rem}}
.endpoint p{{color:var(--text-secondary);font-size:.91rem;max-width:62ch;
  padding:0 var(--s5);margin-top:.35rem}}
.method{{font-family:var(--mono);font-size:.7rem;font-weight:700;
  letter-spacing:.04em;padding:.18rem .42rem;border-radius:6px;
  background:var(--accent-soft);color:var(--accent);flex:0 0 auto}}
/* GET and POST differ by weight of the same hue, not by hue. */
.method.get{{background:transparent;color:var(--text-muted);
  border:1px solid var(--border-strong)}}
.endpoint .route{{font-family:var(--mono);font-size:.82rem;
  color:var(--text-muted);word-break:break-all}}
.codeblock{{position:relative;margin:var(--s3) var(--s5) var(--s5);
  margin-top:auto;padding-top:var(--s3)}}
/* A clipped curl command is worse than a wrapped one: the whole point of the
   block is that it can be copied and read, and a horizontal scrollbar inside
   a card hides the tail of the line from anyone skimming. */
.codeblock pre{{margin:0;background:var(--panel-2);padding-right:5rem;
  white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.55}}
.copy{{position:absolute;top:.5rem;right:.5rem;font-size:.74rem;
  font-weight:640;padding:.28rem .55rem;border-radius:var(--r-sm);
  border:1px solid var(--border-strong);background:var(--panel);
  color:var(--text-secondary);cursor:pointer;opacity:0;
  transition:opacity .15s,color .15s}}
.codeblock:hover .copy,.copy:focus-visible{{opacity:1}}
.copy:hover{{color:var(--text-primary)}}
.copy[data-done]{{opacity:1;color:var(--accent);border-color:var(--accent)}}
/* No hover on a touch screen, so the control has to be permanently visible. */
@media (hover:none){{.copy{{opacity:1}}}}
.badge{{display:inline-block;padding:.25rem .58rem;border-radius:var(--r-full);
  background:var(--accent-soft);color:var(--accent);font-size:.76rem;
  font-weight:650}}

footer.site{{border-top:1px solid var(--border);padding:var(--s6) 0;
  color:var(--text-muted);font-size:.88rem;margin-top:var(--s5)}}
footer.site .shell{{display:flex;gap:var(--s5);flex-wrap:wrap;
  justify-content:space-between;align-items:center}}
footer.site nav{{display:flex;gap:1.15rem;flex-wrap:wrap}}
footer.site a{{color:var(--text-secondary)}}
footer.site a:hover{{color:var(--text-primary)}}

/* --- overlay: sign-in and checkout, so neither costs a navigation ---- */
.overlay{{position:fixed;inset:0;z-index:100;display:none;
  align-items:center;justify-content:center;padding:var(--s4);
  background:color-mix(in srgb,#0b0b0b 55%,transparent);
  backdrop-filter:blur(3px)}}
.overlay[open]{{display:flex}}
.sheet{{background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-lg);box-shadow:var(--sh-3);width:100%;
  max-width:26rem;padding:var(--s5);position:relative;
  max-height:calc(100vh - 2rem);overflow-y:auto}}
.sheet.wide{{max-width:36rem}}
.sheet h2{{font-size:1.2rem;margin-bottom:.3rem;letter-spacing:-.015em}}
.sheet p.sub{{color:var(--text-secondary);font-size:.9rem;
  margin-bottom:var(--s4)}}
.sheet .close{{position:absolute;top:.7rem;right:.7rem;border:0;
  background:transparent;color:var(--text-muted);cursor:pointer;
  font-size:1.35rem;line-height:1;padding:.2rem .4rem;border-radius:var(--r-sm)}}
.sheet .close:hover{{background:var(--panel-2);color:var(--text-primary)}}
.field{{display:grid;gap:.3rem;margin-bottom:var(--s3)}}
.field label{{font-size:.78rem;font-weight:640;color:var(--text-muted);
  text-transform:uppercase;letter-spacing:.05em}}
.field input{{font:inherit;font-size:.95rem;padding:.6rem .7rem;
  border-radius:var(--r-sm);border:1px solid var(--border-strong);
  background:var(--surface-1);color:var(--text-primary);width:100%}}
.field input:focus-visible{{outline:2px solid var(--accent);outline-offset:1px}}
.form-note{{color:var(--text-muted);font-size:.82rem;margin-top:var(--s3);
  line-height:1.45}}
.form-error{{color:var(--status-critical);font-size:.86rem;margin-top:var(--s2)}}
#checkout-mount{{min-height:20rem}}

/* --- mobile: compact, thumb-reachable, no wasted vertical -------------
   A phone screen is the scarcest space we have, so below 720px the page
   tightens rather than merely reflowing: smaller section padding, a scrolling
   nav instead of a wrapping one, full-width primary actions, and sheets that
   dock to the bottom where a thumb already is. */
@media(max-width:719px){{
  :root{{--shell:min(100%, 100% - 1.75rem)}}
  header.site .shell{{gap:.5rem;padding-block:.45rem;min-height:52px}}
  .brand{{margin-right:auto;font-size:.98rem}}
  .brand svg{{width:24px;height:24px}}
  /* One scrolling row beats a wrapping block that pushes the headline down. */
  nav.main{{order:3;width:100%;margin-left:0;gap:1.1rem;padding:.15rem 0 .1rem;
    font-size:.88rem;overflow-x:auto;flex-wrap:nowrap;
    scrollbar-width:none;-ms-overflow-style:none}}
  nav.main::-webkit-scrollbar{{display:none}}

  section{{padding:1.5rem 0}}
  .hero{{padding:1.5rem 0 .5rem}}
  .hero h1{{font-size:clamp(1.95rem,8.5vw,2.5rem);max-width:100%}}
  .hero p.lede{{font-size:1rem;margin-top:.7rem}}
  .hero .actions{{margin-top:1.1rem;gap:.5rem}}
  .hero .actions .btn{{flex:1 1 auto;min-height:44px}}
  .hero-note{{margin-top:.6rem}}

  .card{{padding:1rem;border-radius:var(--r-md)}}
  .figure{{padding:.4rem;border-radius:var(--r-md)}}
  .stat{{padding:.85rem}}
  .stats{{gap:.6rem}}
  .gallery,.grid,.split{{gap:.85rem}}
  .lead-in{{margin-bottom:1rem}}
  section h2{{font-size:1.35rem}}
  .plan{{padding:1.15rem}}

  /* 44px minimum touch target on anything tappable. */
  .btn{{min-height:42px}}
  .btn-small{{min-height:36px}}
  .chip{{padding:.4rem .65rem}}

  /* Sheets dock to the bottom: reachable, and the keyboard does not cover
     the submit button when the email field focuses. */
  .overlay{{align-items:flex-end;padding:0}}
  .sheet{{max-width:100%;border-radius:var(--r-lg) var(--r-lg) 0 0;
    padding:1.25rem 1.1rem calc(1.25rem + env(safe-area-inset-bottom));
    max-height:88vh}}
  .sheet .close{{top:.5rem;right:.5rem;font-size:1.6rem;padding:.3rem .5rem}}
  .field input{{font-size:16px}}  /* 16px stops iOS zooming the viewport */

  .page-head{{padding:1.25rem 0 0}}
  .page-head h1{{font-size:clamp(1.8rem,7.5vw,2.3rem);max-width:100%}}
  .page-head p.sub{{font-size:1rem}}

  .endpoint > header,.endpoint p,.endpoint h3{{padding-inline:1rem}}
  .endpoint > header{{padding-top:1rem}}
  .codeblock{{margin:.75rem 1rem 1rem}}
  .codeblock pre{{padding:.85rem;padding-right:.85rem;font-size:.8rem}}
  /* Overlaying the copy control would sit it on top of the command, so on a
     phone it goes under the block at full tap width instead. */
  .copy{{position:static;display:block;width:100%;margin-top:.4rem;
    min-height:38px;opacity:1}}

  footer.site{{padding:1.5rem 0}}
  footer.site .shell{{gap:.6rem;justify-content:flex-start}}
  footer.site nav{{gap:.9rem}}
  table.packs th,table.packs td{{padding:.5rem .45rem;font-size:.86rem}}
  table.packs .btn-small{{min-height:38px}}
}}

@media(max-width:400px){{
  .stats{{grid-template-columns:1fr 1fr}}
  .hero h1{{font-size:1.85rem}}
}}
@media (prefers-reduced-motion:reduce){{
  *{{transition:none !important;animation:none !important}}
  .card:hover,.plan:hover{{transform:none}}
}}

/* --- film grain ------------------------------------------------------
   A fractal-noise plate over the page background only. It stops large flat
   fields banding on cheap panels and gives the bone surface a paper quality
   flat CSS colour cannot. It must never sit over a chart, a control or body
   text, so it is fixed, behind everything, and non-interactive. If it reads
   as texture rather than material, it is too strong. */
body::before{{
  content:"";position:fixed;inset:0;z-index:0;pointer-events:none;
  opacity:.045;mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.82' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='180' height='180' filter='url(%23n)'/%3E%3C/svg%3E");
  background-size:180px 180px}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]) body::before{{
    mix-blend-mode:screen;opacity:.05}}
}}
:root[data-theme="dark"] body::before{{mix-blend-mode:screen;opacity:.05}}
/* Not `.overlay`: it is already z-index 100, and `position:relative` here
   overrode its `position:fixed` - the sign-in and checkout sheets were laid
   out in the flow of the page instead of over it, so on a phone the sheet
   sat mid-scroll rather than docked to the bottom. */
header.site,main,footer.site{{position:relative;z-index:1}}

/* --- stage: the generated panels that replaced the photographic plates ---
   The old hero was a studio photograph of a marble column: a wide white slab
   with the subject in one corner, which had to be dimmed in dark mode, went
   soft on a 3x phone screen, and said nothing about the product. Everything
   here is drawn instead - one brand wash, the real mark, and real output from
   the live pipeline - so it is sharp at any density, correct in both themes,
   and cannot 404. */
.stage{{position:relative;overflow:hidden;border-radius:var(--r-lg);
  border:1px solid var(--border);box-shadow:var(--sh-2);
  padding:clamp(.85rem,2.2vw,1.35rem);
  background:
    radial-gradient(115% 105% at 88% -10%,
      color-mix(in srgb,var(--brand) 20%,transparent) 0%,transparent 62%),
    radial-gradient(90% 80% at 0% 100%,
      color-mix(in srgb,var(--brand-light) 16%,transparent) 0%,transparent 58%),
    var(--panel-2)}}
/* The mark, oversized and cropped, as the only decoration. Behind everything
   and inert: it is texture, not a second logo. */
.stage-mark{{position:absolute;left:-16%;top:-22%;width:min(58%,20rem);
  opacity:.16;pointer-events:none;z-index:0}}
.stage-mark svg{{width:100%;height:auto}}
.stage > *:not(.stage-mark){{position:relative;z-index:1}}
.stage .figure{{box-shadow:var(--sh-3)}}
.stage-caption{{display:flex;gap:.5rem;align-items:baseline;flex-wrap:wrap;
  margin-top:.7rem;color:var(--text-secondary);font-size:.85rem}}
.stage-caption b{{color:var(--text-primary);font-weight:620}}
.stage-caption .ms{{color:var(--text-muted);font-variant-numeric:tabular-nums;
  margin-left:auto}}
.askline{{display:flex;align-items:center;gap:.5rem;margin-bottom:.7rem;
  padding:.5rem .7rem;border-radius:var(--r-sm);background:var(--panel);
  border:1px solid var(--border);box-shadow:var(--sh-1);
  font-size:.92rem;color:var(--text-primary)}}
.askline .tag{{font-size:.66rem;font-weight:680;letter-spacing:.07em;
  text-transform:uppercase;color:var(--accent);flex:0 0 auto}}
.askline .q{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}

/* A panel of chips or small print over the same wash, for the sections that
   used the other two photographs. */
.stage.list{{display:grid;gap:var(--s3);align-content:center;
  min-height:15rem}}
.stage-row{{display:flex;align-items:center;gap:.6rem;padding:.55rem .7rem;
  background:var(--panel);border:1px solid var(--border);
  border-radius:var(--r-sm);box-shadow:var(--sh-1);font-size:.9rem}}
.stage-row .nm{{font-weight:600}}
.stage-row .meta{{margin-left:auto;color:var(--text-muted);font-size:.8rem;
  font-variant-numeric:tabular-nums}}
.stage-row .dot{{width:7px;height:7px;border-radius:50%;flex:0 0 auto;
  background:var(--accent)}}

.band{{background:var(--panel-2);border-block:1px solid var(--border)}}
.kicker{{font-size:.74rem;font-weight:660;letter-spacing:.09em;
  text-transform:uppercase;color:var(--text-muted);margin-bottom:.55rem}}
"""


def _overlays() -> str:
    """Sign-in and checkout sheets.

    Both live on every marketing page so neither costs a navigation: a visitor
    who decides to buy on the pricing page stays on the pricing page.
    """
    return """
<div class="overlay" id="signin-overlay" role="dialog" aria-modal="true"
     aria-labelledby="signin-title">
  <div class="sheet">
    <button class="close" data-action="close" aria-label="Close">&times;</button>
    <h2 id="signin-title">Sign in</h2>
    <p class="sub" id="signin-reason">Your email is enough. No password to
    forget.</p>
    <form id="signin-form" novalidate>
      <div class="field">
        <label for="signin-email">Email</label>
        <input id="signin-email" type="email" required autocomplete="email"
               placeholder="you@company.com" inputmode="email">
      </div>
      <button class="btn btn-primary" type="submit" style="width:100%">
        Continue</button>
      <p class="form-error" id="signin-error"></p>
    </form>
    <p class="form-note">New accounts get free queries straight away. We use
    your email to keep your dashboards; nothing else.</p>
  </div>
</div>

<div class="overlay" id="checkout-overlay" role="dialog" aria-modal="true"
     aria-labelledby="checkout-title">
  <div class="sheet wide">
    <button class="close" data-action="close" aria-label="Close">&times;</button>
    <h2 id="checkout-title">Add credits</h2>
    <p class="sub" id="checkout-status">Preparing secure checkout&hellip;</p>
    <div id="checkout-mount"></div>
    <p class="form-note">Payments are processed by Stripe. Card details never
    reach our servers.</p>
  </div>
</div>
"""


# Applied before first paint, so a visitor who chose dark inside the app never
# sees a white flash on the way back to a marketing page. Inline and blocking
# on purpose: deferring it *is* the flash.
_THEME_BOOT = (
    "<script>try{var t=localStorage.getItem('th-theme');"
    "if(t)document.documentElement.setAttribute('data-theme',t)}catch(e){}</script>"
)

_THEME_TOGGLE = """
<button class="icon-btn" id="theme-toggle" type="button"
        aria-label="Switch between light and dark">
  <svg class="i-moon" viewBox="0 0 24 24" aria-hidden="true"><path
    d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/></svg>
  <svg class="i-sun" viewBox="0 0 24 24" aria-hidden="true"><circle
    cx="12" cy="12" r="4.2"/><path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2
    M19.2 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6
    M6.9 17.1l-1.6 1.6"/></svg>
</button>"""


def _analytics_snippet() -> str:
    write_key = config.get("TWOHELIXES_ANALYTICS_WRITE_KEY") or ""
    if not write_key:
        return ""
    tracker_config = json.dumps(
        {"siteId": write_key, "endpoint": "/v1/collect"},
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    return (
        f"<script>window.__thConfig={tracker_config};</script>"
        '<script async src="/static/th.js"></script>'
    )


def _page(
    title: str, description: str, body: str, path: str = "/", head: str = ""
) -> str:
    """One page frame for every marketing page.

    `head` carries per-page markup - structured data, a page-specific og:image
    - rather than each page rebuilding the frame to add one tag, which is how
      canonical URLs and theme boot scripts drift apart.
    """
    nav = "".join(
        f'<a href="{href}"{" aria-current=\"page\"" if href == path else ""}>{label}</a>'
        for href, label in NAV
    )
    site = config.site_url().rstrip("/")
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{html.escape(title)}</title>
<meta name="description" content="{html.escape(description)}">
<meta name="theme-color" content="#fcfcfb" media="(prefers-color-scheme:light)">
<meta name="theme-color" content="#1a1a19" media="(prefers-color-scheme:dark)">
<link rel="canonical" href="{site}{html.escape(path)}">
<meta property="og:type" content="website">
<meta property="og:url" content="{site}{html.escape(path)}">
<meta property="og:site_name" content="twoHelixes">
<meta property="og:title" content="{html.escape(title)}">
<meta property="og:description" content="{html.escape(description)}">
<meta property="og:image" content="{site}/static/art/og-1200.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
{_THEME_BOOT}
<style>{_css()}</style>
{head}
</head><body data-stripe-key="{html.escape(config.stripe_publishable_key() or "")}">
<a class="skip" href="#content">Skip to content</a>
<header class="site"><div class="shell">
  <a class="brand" href="/">{helix.logo(28, uid="nav")}<span>twoHelixes</span></a>
  <nav class="main" aria-label="Main">{nav}</nav>
  <span class="chip" id="header-badge" hidden></span>
  {_THEME_TOGGLE}
  <a class="btn btn-primary" href="/app" id="header-cta"
     data-action="signin">Start free</a>
</div></header>
{body}
{_overlays()}
<footer class="site"><div class="shell">
  <span>twoHelixes — charts of your data, from a sentence</span>
  <nav aria-label="Footer">
    <a href="/features">Features</a><a href="/datasets">Datasets</a>
    <a href="/pricing">Pricing</a><a href="/docs">Docs</a><a href="/app">App</a>
  </nav>
</div></footer>
<script type="module" src="/static/marketing.js"></script>
{_analytics_snippet()}
</body></html>"""


@router.get("/")
def home(ctx: router.Context) -> router.Result:
    mode = ctx.q("mode", "light") or "light"

    # Every chart below is rendered by the same pipeline that serves customers.
    # If the chart defaults regress, this page changes with them.
    hero_chart = showcase.chart_svg("revenue", mode, *showcase.HERO_SIZE)
    channels = showcase.chart_svg("channels", mode, *showcase.GALLERY_SIZE)
    latency = showcase.chart_svg("latency", mode, *showcase.GALLERY_SIZE)
    retention = showcase.chart_svg("retention", mode, *showcase.GALLERY_SIZE)

    connectors = (
        "PostgreSQL", "MySQL", "SQL Server", "Snowflake", "BigQuery",
        "Redshift", "ClickHouse", "Trino", "Oracle", "DuckDB", "SQLite",
        "MongoDB", "Elasticsearch", "HTTP APIs",
        "CSV", "TSV", "JSON", "Parquet", "Excel", "OpenDocument", ".gz", ".zip",
    )
    chips = "".join(f'<span class="chip">{html.escape(c)}</span>' for c in connectors)

    # Internal links to the dataset pages from the page with the most
    # authority. Built from the catalogue rather than hand-listed so a new
    # dataset appears here the day it is added.
    from twohelixes.datasets import samples as sample_data

    dataset_links = "".join(
        f'<a class="chip" href="/datasets/{html.escape(s.key)}">'
        f"{html.escape(s.name)}</a>"
        for s in sample_data.SAMPLES
    )

    body = f"""
<main id="content">
<div class="hero"><div class="shell hero-layout">
  <div>
    <span class="eyebrow">Any data. One sentence. A chart.</span>
    <h1>Charts of your data, from a sentence</h1>
    <p class="lede">Drop in a spreadsheet or connect a database, then ask for
    what you want to see. It finds the columns, joins them, shapes them and
    draws the chart &mdash; every time, never an apology.</p>
    <div class="actions">
      <a class="btn btn-primary" href="/app" data-action="signin">Start free</a>
      <a class="btn btn-ghost" href="#how">See how it works</a>
    </div>
    <p class="hero-note">Ask one question now without an account. Sign in for
    {config.PLAN_ALLOWANCES['free']['chat_query']} free charts a month &mdash;
    no card, and the sample data is already loaded.</p>
  </div>
  <div class="stage">
    <span class="stage-mark" aria-hidden="true">{helix.logo(320, mode, uid="heromark")}</span>
    <div class="askline"><span class="tag">Ask</span>
      <span class="q">how did revenue trend by region?</span></div>
    <figure class="figure">{hero_chart}</figure>
    <div class="stage-caption"><b>Drawn by the live pipeline</b>
      <span class="ms">2.4 s</span></div>
  </div>
</div></div>

<section class="band" id="how" style="margin-top:clamp(2rem,5vw,4.5rem)">
 <div class="shell">
  <div class="stats">
    <div class="stat"><div class="n">19</div>
      <div class="l">chart forms it can pick from</div></div>
    <div class="stat"><div class="n">15</div>
      <div class="l">databases, warehouses and APIs</div></div>
    <div class="stat"><div class="n">100%</div>
      <div class="l">of answers come back as a chart</div></div>
    <div class="stat"><div class="n">0</div>
      <div class="l">dual axes, ever</div></div>
  </div>
 </div>
</section>

<section><div class="shell split">
  <div>
    <p class="kicker">How it works</p>
    <h2>It always gets you a chart</h2>
    <p class="lead-in">Most data assistants hand back a paragraph explaining
    why they could not help. This one draws something every time. If two tables
    share no values it drops the join and charts what is left. If its own
    shaping code fails it charts the cleaned data instead. If the question is
    vague it picks the most useful cut and names the one it rejected. You get a
    figure, plus a plain note about anything it had to approximate.</p>
    <div class="chips">
      <span class="chip">Degrades, never dead-ends</span>
      <span class="chip">Every approximation named</span>
      <span class="chip">Fix it and re-run</span>
    </div>
  </div>
  <div class="trace-demo">
    <div class="askbox"><span>Question</span>how did revenue trend by region?</div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Finding the data</span><span class="ms">4 ms</span></div>
      <p class="said">orders joined to regions &mdash; 3 columns are enough to
      answer this.</p></div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Joining</span><span class="ms">61 ms</span></div>
      <p class="said">region_id &rarr; id, 98% value overlap. Row count
      unchanged, so the key is unique.</p></div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Shaping the data</span><span class="ms">1.2 s</span></div>
      <p class="said">Monthly totals per region, sorted by date so the line
      reads left to right.</p></div>
    <div class="tstep now"><div class="top"><i class="dot"></i>
      <span class="nm">Choosing the chart</span><span class="ms">0.9 s</span></div>
      <p class="said">Time on x, one line per region. A bar chart would hide
      the crossover in August.</p></div>
    <div class="tstep"><div class="top"><i class="dot"></i>
      <span class="nm">Applying defaults</span><span class="ms">3 ms</span></div>
      <p class="said">Palette, spacing and labels applied. 0 issues found.</p></div>
  </div>
</div></section>

<section><div class="shell">
  <p class="kicker">Chart quality</p>
  <h2>Charts that do not mislead</h2>
  <p class="lead-in">The rules are enforced in code, not left to taste. Every
  chart is audited against them before you see it &mdash; including the three
  below.</p>
  <div class="gallery">
    <figure class="figure">{channels}
      <figcaption>Ranked categories, with the tail folded into
      <b>Other</b> rather than silently dropped.</figcaption></figure>
    <figure class="figure">{latency}
      <figcaption>A distribution, titled with <b>the finding</b> rather than
      the column names.</figcaption></figure>
  </div>
  <div class="gallery" style="margin-top:1.1rem">
    <figure class="figure">{retention}
      <figcaption>An area wash at 10% opacity &mdash; a hint of volume, never a
      saturated block.</figcaption></figure>
    <div class="card fill">
      <h3>What is enforced</h3>
      <ul class="rules">
        <li>Hues are assigned in <b>fixed order and never cycled</b> &mdash; a
        ninth series folds into Other rather than reusing a colour.</li>
        <li>Scatter and map forms <b>cap at three series</b>, because past that
        no palette stays colour-blind safe.</li>
        <li><b>Never two y-scales on one plot.</b> Mismatched measures get
        their own chart instead of a fake correlation.</li>
        <li>Light and dark are <b>separately validated</b>, not flipped.</li>
        <li>Every finished chart is <b>re-audited</b>; anything that slips
        through is reported beside it.</li>
      </ul>
    </div>
  </div>
</div></section>

<section class="band"><div class="shell">
  <p class="kicker">Worked examples</p>
  <h2>Nine datasets, already loaded, already answered</h2>
  <p class="lead-in">Every account starts with these. Each one has a page with
  its schema, its rows, and the charts the pipeline drew from them &mdash; with
  the reasoning behind every choice one click away. No account needed to read
  them.</p>
  <div class="chips">{dataset_links}</div>
  <div class="actions" style="margin-top:var(--s5)">
    <a class="btn btn-ghost" href="/datasets">Browse the datasets</a></div>
</div></section>

<section><div class="shell split">
  <div>
    <p class="kicker">Your data</p>
    <h2>Whatever the data lives in, it can chart it</h2>
    <p class="lead-in">Drag in a CSV, Excel file or Parquet extract and ask a
    question thirty seconds later. Or connect the warehouse and leave the rows
    where they are &mdash; everything is read-only, and generated SQL is checked
    before it ever reaches a driver.</p>
    <div class="chips">{chips}</div>
  </div>
  <div class="stage list">
    <span class="stage-mark" aria-hidden="true">{helix.logo(280, mode, uid="connmark")}</span>
    <div class="stage-row"><i class="dot"></i><span class="nm">orders.csv</span>
      <span class="meta">uploaded &middot; 41k rows</span></div>
    <div class="stage-row"><i class="dot"></i><span class="nm">warehouse &middot; Snowflake</span>
      <span class="meta">read-only</span></div>
    <div class="stage-row"><i class="dot"></i><span class="nm">regions</span>
      <span class="meta">joined on region_id, 98% overlap</span></div>
    <div class="stage-row"><i class="dot"></i><span class="nm">SELECT &hellip;</span>
      <span class="meta">checked read-only</span></div>
  </div>
</div></section>

<section><div class="shell">
  <p class="kicker">After the first chart</p>
  <h2>The chart is a starting point, not a screenshot</h2>
  <div class="grid">
    <div class="card"><h3>Change it for free</h3><p>Swap the axes, the chart
    type, the grouping or the aggregation by hand and it redraws &mdash; no
    model call, no credit spent. Or say what you want changed in words and let
    the edit stage apply it.</p></div>
    <div class="card"><h3>Real Python underneath</h3><p>The shaping runs in a
    sandboxed interpreter with pandas, polars, numpy, scipy, scikit-learn and
    statsmodels &mdash; so &ldquo;fit a trend and forecast the next quarter&rdquo;
    is a question, not a feature request. The code is shown and editable.</p></div>
    <div class="card"><h3>A real SQL editor</h3><p>For when you already know the
    query. Schema-aware completions answer instantly without waiting on a model,
    plus generated SQL, per-source suggested questions and saved history.</p></div>
    <div class="card"><h3>Take it with you</h3><p>SVG, PNG or CSV; pin it to a
    dashboard with a share link that needs no account; export the whole run as
    a Jupyter <code>.ipynb</code> or a marimo notebook &mdash; both run here as
    well as on your laptop; or call the same pipeline over HTTP.</p></div>
  </div>
</div></section>

<section class="band"><div class="shell split">
  <div class="stage" style="order:2">
    <span class="stage-mark" aria-hidden="true">{helix.logo(300, mode, uid="startmark")}</span>
    <div class="askline"><span class="tag">Ask</span>
      <span class="q">which channel brings the most signups?</span></div>
    <figure class="figure">{channels}</figure>
  </div>
  <div>
    <p class="kicker">Get started</p>
    <h2>Chart something you actually care about</h2>
    <p class="lead-in">Try it on the sample data without signing up. Then
    {config.PLAN_ALLOWANCES['free']['chat_query']} free charts a month with an
    email, and paid plans that start at
    ${config.PLAN_ALLOWANCES['plus']['price_cents'] // 100} when charting
    becomes a habit.</p>
    <a class="btn btn-primary" href="/app" data-action="signin">Open twoHelixes</a>
  </div>
</div></section>
</main>"""

    return router.html(
        _page(
            "twoHelixes — charts of your data, from a sentence",
            "Ask for a chart of any data you have - a spreadsheet, a database, "
            "a warehouse - and get one back in seconds, correct and readable.",
            body,
            "/",
        )
    )


STAGES = (
    ("Finding the data", "Which tables and columns can answer this at all, and "
     "how confident it is that they can."),
    ("Joining", "The key it picked, the value overlap it measured, and whether "
     "the row count survived - so a fan-out cannot hide."),
    ("Shaping the data", "The Python it ran, in full. Resampling respects the "
     "data's own granularity rather than snapping to months."),
    ("Choosing the chart", "The form, and the forms it rejected. Naming the "
     "rejected option is what makes the choice checkable."),
    ("Applying defaults", "Palette, spacing, labels and a re-audit of the "
     "finished figure. Anything that slipped through is printed beside it."),
)

CAPABILITIES = (
    ("Always a chart",
     "Every stage has a fallback, so a run cannot end in an apology. A join "
     "with no overlapping values is dropped and the rest is charted; failed "
     "shaping code falls back to the cleaned frame; a model outage falls back "
     "to heuristic chart selection. Whatever it had to approximate is printed "
     "beside the figure rather than hidden."),
    ("Nineteen chart forms",
     "Bar, horizontal bar, line, area, scatter, bubble, pie, histogram, "
     "heatmap, box, candlestick, sankey, treemap, sunburst, funnel, waterfall "
     "and maps - points from latitude and longitude, or countries filled by "
     "value - plus stat tiles and tables for the questions whose honest answer "
     "is a single number. The "
     "form is chosen from the data's shape, and the rejected alternative is "
     "named so you can disagree with the choice."),
    ("Any data you have",
     "CSV, TSV, JSON, Excel, OpenDocument and Parquet by drag and drop, "
     "gzipped or zipped if that is how it arrived; PostgreSQL, MySQL, SQL "
     "Server, Oracle, Snowflake, BigQuery, Redshift, ClickHouse, Trino, DuckDB "
     "and SQLite by connection string; MongoDB, Elasticsearch and HTTP APIs "
     "for everything else. All read-only. A file that will not parse with "
     "commas is retried with the delimiter sniffed and then with the bad rows "
     "skipped, because a rejected upload is the worst first minute there is."),
    ("Code interpreter",
     "pandas, polars, numpy, scipy, scikit-learn, statsmodels and plotly are "
     "pre-imported at worker boot, so the first query is not the slow one. "
     "Alongside them sit purpose-built tools: column-role inference, currency "
     "coercion, overlap-ranked join keys, granularity-aware resampling, "
     "outlier masks, trend fitting and forecasting."),
    ("SQL editor",
     "Completions answer from the schema synchronously, so typing never waits "
     "on a model. Generation, per-source suggested questions, saved queries "
     "and full history sit alongside. Every generated statement is checked "
     "read-only before it reaches a driver."),
    ("Charts you control",
     "Change x, y, z, colour, chart type, aggregation and stacking by hand at "
     "any time - no query spent. Or describe the change in words and let the "
     "edit stage apply it. The reasoning trace stays attached to the chart."),
    ("Long-running agents",
     "Deep research runs hypothesis-test-revise loops inside the code "
     "interpreter for minutes at a time, and reports only the findings it "
     "actually produced output for."),
    ("Export that stays sharp",
     "A native SVG exporter draws bar, line, area, scatter and pie directly - "
     "no headless browser, deterministic output, and markup a designer can "
     "open and edit. PNG, CSV and JSON too."),
    ("Notebooks and dashboards",
     "Any answer exports to a Jupyter .ipynb or a marimo notebook - the data, "
     "the transformation and the chart, in the order they ran. Host either one "
     "here, or pin the chart to a dashboard with a share link that needs no "
     "account to read."),
)


@router.get("/features")
def features(ctx: router.Context) -> router.Result:
    mode = ctx.q("mode", "light") or "light"
    stages = "".join(
        f'<li class="tstep"><div class="top"><i class="dot"></i>'
        f'<span class="nm">{html.escape(name)}</span>'
        f'<span class="ms">stage {i}</span></div>'
        f'<p class="said">{html.escape(said)}</p></li>'
        for i, (name, said) in enumerate(STAGES, 1)
    )
    cards = "".join(
        f"<div class=\"card\"><h3>{html.escape(title)}</h3>"
        f"<p>{html.escape(text)}</p></div>"
        for title, text in CAPABILITIES
    )

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker">Features</p>
  <h1>What you actually get</h1>
  <p class="sub">A chart of your data, every time you ask, from whatever the
  data happens to live in &mdash; then the controls, the code and the export
  formats to do something with it. All of this is shipped, none of it is
  planned.</p>
</div></section>

<section><div class="shell split top">
  <div>
    <p class="kicker">How a question becomes a chart</p>
    <h2>Five stages, none of which can dead-end</h2>
    <p class="sub">Each stage has a fallback: a failed transform charts the
    cleaned frame instead, a model outage falls back to heuristics, a join with
    no overlap is dropped rather than fatal. You get a chart and a note about
    what was approximated. Every stage can be stopped, edited or taken over by
    hand.</p>
  </div>
  <ul class="trace-demo">{stages}</ul>
</div></section>

<section class="band"><div class="shell">
  <p class="kicker">Chart quality</p>
  <h2>The rules are in code, not in a style guide</h2>
  <p class="lead-in">Hues are assigned in fixed order and never cycled, scatter
  forms cap at three series, and no chart ever gets two y-scales. Both charts
  below came off the same audited path your answers do.</p>
  <div class="gallery">
    <figure class="figure">{showcase.chart_svg("channels", mode,
        *showcase.GALLERY_SIZE)}
      <figcaption>The tail folds into <b>Other</b> rather than reusing a
      colour or being silently dropped.</figcaption></figure>
    <figure class="figure">{showcase.chart_svg("latency", mode,
        *showcase.GALLERY_SIZE)}
      <figcaption>Titled with <b>the finding</b>, not with the column
      names.</figcaption></figure>
  </div>
</div></section>

<section><div class="shell">
  <p class="kicker">In the product</p>
  <h2>Everything in the box</h2>
  <div class="grid three">{cards}</div>
</div></section>

<section class="band"><div class="shell split">
  <div>
    <p class="kicker">Get started</p>
    <h2>Make your first chart</h2>
    <p class="sub">One question with no account at all, then
    {config.PLAN_ALLOWANCES['free']['chat_query']} free charts a month with an
    email. Sample datasets are already loaded, so you can see it work before
    connecting anything of your own.</p>
    <div class="actions" style="margin-top:var(--s5);display:flex;gap:.65rem;
      flex-wrap:wrap">
      <a class="btn btn-primary" href="/app" data-action="signin">Start free</a>
      <a class="btn btn-ghost" href="/pricing">See pricing</a>
    </div>
  </div>
  <div class="stage">
    <span class="stage-mark" aria-hidden="true">{helix.logo(300, mode, uid="featmark")}</span>
    <div class="askline"><span class="tag">Ask</span>
      <span class="q">how long do requests take?</span></div>
    <figure class="figure">{showcase.chart_svg("latency", mode,
        *showcase.GALLERY_SIZE)}</figure>
  </div>
</div></section>
</main>"""
    return router.html(
        _page(
            "Features — twoHelixes",
            "Nineteen chart forms, fifteen data sources, a Python code "
            "interpreter, a SQL editor, free chart controls and sharp exports.",
            body,
            "/features",
        )
    )


def _plan_cta(plan: dict[str, Any]) -> str:
    """Free signs in; a paid plan opens checkout without leaving the page.

    The plan id goes to the server, which resolves the Stripe price. Putting
    the price id in the markup meant the page had to be redeployed to change a
    price, and a stale one charges the wrong amount.
    """
    if plan["id"] == "free":
        return ('<a class="btn btn-ghost" href="/app" data-action="signin">'
                f'{html.escape(plan["cta"])}</a>')
    style = "btn-primary" if plan.get("highlight") else "btn-ghost"
    return (f'<button class="btn {style}" data-action="buy" '
            f'data-plan="{html.escape(plan["id"])}">'
            f'{html.escape(plan["cta"])}</button>')


def _rate_suffix(plan: dict[str, Any]) -> str:
    cents = int(config.PLAN_ALLOWANCES[plan["id"]]["price_cents"])
    if not cents:
        return ""
    return f" &middot; {_effective_rate(plan)}"


def _effective_rate(plan: dict[str, Any]) -> str:
    """What a plan works out at per chart, if you use what you paid for.

    The number people actually compare. Leaving them to divide $19 by 500 in
    their head is how a fair price reads as an expensive one.
    """
    cents = int(config.PLAN_ALLOWANCES[plan["id"]]["price_cents"])
    included = int(plan["included"]["chat_query"] or 0)
    if not cents or not included:
        return "free"
    return f"{cents / included:.1f}c each"


@router.get("/pricing")
def pricing(ctx: router.Context) -> router.Result:
    plans_html = ""
    for plan in PLANS:
        features_html = "".join(f"<li>{html.escape(f)}</li>" for f in plan["features"])
        highlight = " highlight" if plan.get("highlight") else ""
        included = plan["included"]["chat_query"]
        plans_html += f"""
<div class="plan{highlight}">
  <h3 style="font-size:1.05rem;font-weight:640">{html.escape(plan['name'])}</h3>
  <div class="amount">{html.escape(plan['price'])}</div>
  <div class="cadence">{html.escape(plan['cadence'])}</div>
  <p class="plan-blurb">{html.escape(plan['blurb'])}</p>
  <p class="plan-rate">{included:,} charts a month{_rate_suffix(plan)}</p>
  <ul>{features_html}</ul>
  {_plan_cta(plan)}
</div>"""

    packs_html = "".join(
        f"<tr><td><b>{html.escape(p['label'])}</b></td>"
        f"<td>{p['credits']:,} credits</td>"
        f"<td>{html.escape(p['bonus'])}</td>"
        f'<td style="text-align:right"><button class="btn btn-ghost btn-small" '
        f"data-action=\"buy\" data-pack=\"{html.escape(p['id'])}\">Buy</button></td></tr>"
        for p in CREDIT_PACKS
    )

    tiers_html = "".join(
        f"<tr><td>{'$%s+' % f'{threshold // 100:,}' if threshold else 'Any'}</td>"
        f"<td>{int(rate * 100)}%</td>"
        f"<td>{config.CREDIT_COST['chat_query'] * (1 - rate):.1f} credits</td></tr>"
        for threshold, rate in config.VOLUME_TIERS
    )

    def _cost(value: int) -> str:
        # A schema-only completion costs nothing because it never calls a
        # model; "0 credits" reads like a rounding error, "Free" is the point.
        if value == 0:
            return '<span class="badge">Free</span>'
        return f"{value} credit{'' if value == 1 else 's'}"

    # Metered work is priced per minute, and a table that prints "long agent
    # minute - 10 credits" beside "chat query - 2 credits" invites the reader
    # to compare them as if they were the same unit.
    def _unit(name: str) -> str:
        return " / minute" if name.endswith("_minute") else ""

    costs_html = "".join(
        f"<tr><td>{html.escape(k.replace('_minute', '').replace('_', ' '))}"
        f"{_unit(k)}</td><td>{_cost(int(v))}{_unit(k)}</td></tr>"
        for k, v in config.CREDIT_COST.items()
    )

    # The machine table is built from the catalogue rather than written, for
    # the same reason the plan cards read their allowances from config: a page
    # that quotes a rate the biller does not charge is a refund.
    from twohelixes import machines

    def _machine_row(m: dict) -> str:
        spec = f"{m['vcpu']} vCPU, {m['ram_gb']} GB"
        if m["gpu"]:
            spec = f"{m['gpu']}, {m['gpu_ram_gb']} GB"
        included = ('<span class="badge">Included</span>' if m["included"]
                    else html.escape(m["min_plan"].title()))
        return (f"<tr><td><b>{html.escape(m['label'])}</b></td>"
                f"<td>{html.escape(spec)}</td>"
                f"<td>{m['credits_per_minute']} credit"
                f"{'' if m['credits_per_minute'] == 1 else 's'} / minute</td>"
                f"<td>{html.escape(m['price_per_hour'])} / hour</td>"
                f"<td>{included}</td></tr>")

    machines_html = "".join(_machine_row(m) for m in machines.catalog_for(""))
    included_hours = config.PLAN_ALLOWANCES["plus"]["notebook_minute"] // 60

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker">Pricing</p>
  <h1>Start free, pay for what runs</h1>
  <p class="sub">One credit is one US cent, and nothing is charged until a
  query succeeds &mdash; a failed pipeline does not spend your allowance.</p>
</div></section>

<section><div class="shell">
  <div class="price-grid">{plans_html}</div>
</div></section>

<section class="band"><div class="shell">
  <h2>Credit packs</h2>
  <p class="lead-in">Credits never expire and are shared across chat,
  dashboards, SQL generation and long-running agents.</p>
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>Pack</th><th>Credits</th><th>Bonus</th>
      <th><span class="visually-hidden">Buy</span></th></tr></thead>
    <tbody>{packs_html}</tbody>
  </table></div>
</div></section>

<section class="band"><div class="shell">
  <h2>Volume pricing, without asking</h2>
  <p class="lead-in">The unit price falls automatically with your trailing
  30-day spend. There is no contract to sign and no tier to request &mdash;
  <code>GET /v1/usage</code> reports the tier you are in and what the next one
  is worth.</p>
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>30-day spend</th><th>Discount</th>
      <th>Cost per chart</th></tr></thead>
    <tbody>{tiers_html}</tbody>
  </table></div>
  <p class="sub" style="margin-top:var(--s4);color:var(--text-muted);
    font-size:.9rem">Fractions of a credit are carried on the account rather
  than rounded, so a discount is exact however small each call is.</p>
</div></section>

<section class="band"><div class="shell">
  <h2>Machines</h2>
  <p class="lead-in">A hosted notebook runs on a machine you pick, billed by
  the minute while it is up. Every paid plan includes {included_hours} hours or
  more of CPU time a month &mdash; a Plus subscription comes with a machine, not
  just a seat &mdash; and GPUs are credits only, because one GPU hour costs more
  than the plan does.</p>
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>Machine</th><th>Specification</th><th>Rate</th>
      <th>Per hour</th><th>Plan</th></tr></thead>
    <tbody>{machines_html}</tbody>
  </table></div>
  <p class="sub" style="margin-top:var(--s4);color:var(--text-muted);
    font-size:.9rem">Sessions suspend when idle and stop at their ceiling, so a
  tab left open overnight does not spend the month. A session stops when the
  balance runs out rather than running up a debt, and the instance behind it is
  destroyed either way &mdash; we are paying for it too.</p>
</div></section>

<section><div class="shell">
  <h2>What things cost</h2>
  <p class="lead-in">Every priced operation, in credits. Work that holds a
  process &mdash; a hosted notebook, a long-running agent &mdash; is billed by
  the minute for as long as it runs, and <code>GET /v1/usage</code> shows what
  is running, what it has cost and how many minutes your balance covers.</p>
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>Operation</th><th>Cost</th></tr></thead>
    <tbody>{costs_html}</tbody>
  </table></div>
  <p class="sub" style="margin-top:var(--s5);color:var(--text-muted);
    font-size:.9rem">Included usage is spent before credits, so a plan's charts
  are never wasted. Nothing is charged until an operation succeeds, a metered
  session stops when the balance runs out rather than running up a debt, and an
  agent run that produces nothing is refunded in full &mdash; base fee and
  minutes. Credits do not expire.</p>
</div></section>
</main>"""
    return router.html(
        _page("Pricing — twoHelixes", "Plans and credit pricing.", body, "/pricing")
    )


ENDPOINTS = (
    (
        "POST", "/v1/query", "Get a chart",
        "Runs the whole pipeline and returns the finished figure, the chart "
        "config, the row count and every warning it raised on the way. It "
        "returns a figure even when a stage had to fall back.",
        'curl -X POST https://twohelixes.com/v1/query \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY" \\\n'
        '  -H "Content-Type: application/json" \\\n'
        '  -d \'{"q":"revenue by region this quarter","source_id":"..."}\'',
    ),
    (
        "POST", "/v1/query/stream", "Stream the run",
        "The same call as server-sent events, so a long run can show its "
        "progress and its partial chart as it goes. Event names: stage, "
        "thought, warning, partial, result, done.",
        'curl -N -X POST https://twohelixes.com/v1/query/stream \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY" \\\n'
        '  -d \'{"q":"which regions are shrinking?"}\'',
    ),
    (
        "POST", "/v1/sql/generate", "Generate SQL",
        "Schema-aware SQL for a question. The statement is checked read-only "
        "before it is returned, and again before any driver executes it.",
        'curl -X POST https://twohelixes.com/v1/sql/generate \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY" \\\n'
        '  -d \'{"source_id":"...","question":"top customers by lifetime value"}\'',
    ),
    (
        "GET", "/v1/chart/{id}/export", "Export a chart",
        "SVG, PNG or CSV. SVG is drawn by our own exporter - deterministic, "
        "no headless browser, and editable markup.",
        'curl "https://twohelixes.com/v1/chart/$ID/export?format=svg&mode=dark" \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY"',
    ),
    (
        "POST", "/v1/agent", "Run a deep-research agent",
        "Starts a long-running agent and returns a job id immediately - no SSE "
        "connection to hold open. Billed as a base fee plus credits per minute "
        "while it runs; a run that produces nothing is refunded in full. Poll "
        "/v1/jobs/{id} for progress, findings and an itemised bill.",
        'curl -X POST https://twohelixes.com/v1/agent \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY" \\\n'
        '  -d \'{"goal":"why did south shrink last quarter?","source_id":"..."}\'',
    ),
    (
        "GET", "/v1/usage", "See what is running and what it costs",
        "Every meter: what is open right now, the rate it is burning, the "
        "minutes already billed and how many minutes your balance covers. The "
        "per-minute charges in the ledger can all be traced back to a row here.",
        'curl https://twohelixes.com/v1/usage \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY"',
    ),
    (
        "GET", "/v1/chart/{id}/notebook", "Export a notebook",
        "The whole run as a notebook: the data, the transformation that was "
        "run and the chart rebuilt with the same palette. format=ipynb for "
        "Jupyter, or the default marimo file, which we can also host for you.",
        'curl -O -J "https://twohelixes.com/v1/chart/$ID/notebook?format=ipynb" \\\n'
        '  -H "Authorization: Bearer $TWOHELIXES_KEY"',
    ),
)


@router.get("/docs")
def docs(ctx: router.Context) -> router.Result:
    blocks = ""
    for method, route, title, note, snippet in ENDPOINTS:
        blocks += f"""
<article class="endpoint">
  <header><span class="method {method.lower()}">{method}</span>
    <span class="route">{html.escape(route)}</span></header>
  <h3>{html.escape(title)}</h3>
  <p>{html.escape(note)}</p>
  <div class="codeblock">
    <pre><code>{html.escape(snippet)}</code></pre>
    <button class="copy" type="button" data-copy>Copy</button>
  </div>
</article>"""

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker">API</p>
  <h1>Every chart is one HTTP call away</h1>
  <p class="sub">JSON in, JSON out. Authenticate with a bearer API key from the
  billing page, or with a session cookie if you are calling from a browser
  already signed in.</p>
</div></section>

<section><div class="shell grid">{blocks}</div></section>

<section class="band"><div class="shell">
  <h2>Analytics compatibility</h2>
  <p class="lead-in">The GA4 parity map states exactly what matches, what is
  partial, and what twoHelixes deliberately does differently.</p>
  <a class="btn btn-ghost" href="/docs/analytics-parity">Read the parity map</a>
</div></section>

<section><div class="shell">
  <h2>Rate limits</h2>
  <p class="lead-in">Anonymous callers get
  {config.ANON_QUERIES_PER_DAY} sample-data question a day, per address. Free
  accounts get {config.PLAN_ALLOWANCES['free']['chat_query']} AI charts a month
  under per-minute and per-day limits. Requests paid for from credits are not
  rate limited beyond a global per-account safety valve, and the unit price
  falls automatically with your trailing 30-day spend.</p>
  <ul class="rules">
    <li>Charging happens <b>after success</b> &mdash; a failed pipeline does
    not spend anything.</li>
    <li>Generated SQL is <b>checked read-only</b> before it reaches a
    driver, on every path.</li>
    <li>Every response carries the <b>warnings</b> the run produced, so a
    degraded answer is never silently a clean one.</li>
  </ul>
  <a class="btn btn-primary" href="/app" data-action="signin"
     style="margin-top:var(--s5)">Get an API key</a>
</div></section>
</main>"""
    return router.html(
        _page(
            "API docs — twoHelixes",
            "The twoHelixes HTTP API: ask a question, stream the reasoning, "
            "generate SQL and export a chart.",
            body,
            "/docs",
        )
    )


ANALYTICS_PARITY = (
    ("Property / data stream", "Analytics site", "Partial", "One owned web domain; no app streams or multi-stream properties."),
    ("Measurement ID", "Write key", "At parity", "A public key selects a registered site; unknown keys are discarded."),
    ("gtag / dataLayer", "th.js", "Partial", "Page, track, identify and queued th calls; no generic dataLayer plugin surface."),
    ("Event + parameters", "event_name + props", "At parity", "GA-shaped names and JSON parameters are retained within collector limits."),
    ("user_pseudo_id", "client_id", "Deliberately different", "Random first-party localStorage ID; DNT and GPC disable it."),
    ("Sessions / session_start", "Server session logic", "At parity", "30-minute inactivity boundary and one session_start on the first hit."),
    ("engagement_time_msec", "engagement_ms", "Partial", "_et and visible-tab deltas accumulate; the full GA lifecycle is not reproduced."),
    ("Conversions / key events", "Conventional key-event names", "Partial", "Four conventional names affect engagement; no per-site custom registry."),
    ("Audiences", "None", "Not supported", "No persistent membership, activation or advertising export."),
    ("UTM / traffic source", "Session-scoped UTM + referrer", "Partial", "UTMs persist and self-referrals are excluded; no channel grouping or cross-session attribution."),
    ("Explorations", "Chat + dashboards", "Deliberately different", "Questions and editable charts replace the exploration canvas."),
    ("BigQuery export", "Export endpoint + datasets", "Partial", "Bounded authenticated batches, not a continuous linked warehouse export."),
)


@router.get("/docs/analytics-parity")
def analytics_parity(ctx: router.Context) -> router.Result:
    rows = "".join(
        "<tr>"
        f"<td>{html.escape(ga4)}</td>"
        f"<td>{html.escape(ours)}</td>"
        f"<td><b>{html.escape(verdict)}</b></td>"
        f"<td>{html.escape(boundary)}</td>"
        "</tr>"
        for ga4, ours, verdict, boundary in ANALYTICS_PARITY
    )
    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker">Analytics</p>
  <h1>GA4 parity, without the wishful thinking</h1>
  <p class="sub">A current capability map. Partial rows say what is missing;
  deliberate differences say why the behavior does not copy Google Analytics.</p>
</div></section>
<section><div class="shell">
  <div class="scroll-x"><table class="packs">
    <thead><tr><th>GA4 concept</th><th>twoHelixes</th><th>Verdict</th><th>Boundary</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
</div></section>
<section class="band"><div class="shell">
  <h2>Collector decision</h2>
  <p class="lead-in">Unknown write keys are discarded with the normal forgiving
  collector response. Quarantine would retain unsolicited data and allow
  guessed hostnames to create storage namespaces.</p>
  <p class="sub">The HTTP parity suite covers session boundaries,
  <code>session_start</code>, engagement accumulation, page views,
  self-referrals, session UTM persistence, and engaged-session bounce rules.</p>
</div></section>
</main>"""
    return router.html(
        _page(
            "GA4 analytics parity — twoHelixes",
            "An honest GA4 to twoHelixes analytics capability map.",
            body,
            "/docs/analytics-parity",
        )
    )


@router.get("/app")
def app_shell(ctx: router.Context) -> router.Result:
    """The single-page app shell. The bundle takes over from here."""
    return router.html(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>twoHelixes</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{_css()}</style>
<link rel="stylesheet" href="/static/styles.css">
</head><body>
<div id="root" data-boot="{int(time.time())}">
  <div class="boot-splash" style="display:grid;place-items:center;min-height:100vh">
    {helix.spinner(72, uid="boot")}
  </div>
</div>
<script type="module" src="/static/app.js"></script>
{_analytics_snippet()}
</body></html>"""
    )


@router.get("/billing/complete")
def billing_complete(ctx: router.Context) -> router.Result:
    """Where Stripe's embedded checkout returns to.

    The webhook is what grants credits, so this page reports rather than
    decides - and it has to exist: `return_url` on the embedded session points
    here, and without the route a completed payment landed on a JSON 404,
    which reads as "my card was charged and the site broke".
    """
    session_id = ctx.q("session_id", "") or ""
    body = f"""
<main id="content"><section><div class="shell" style="max-width:34rem">
  <p class="kicker">Checkout</p>
  <h1 id="billing-heading">Confirming your payment&hellip;</h1>
  <p class="lead-in" id="billing-detail">Stripe has taken the payment. We are
  waiting for the confirmation that credits it to your account &mdash; this is
  usually a second or two.</p>
  <div class="actions"><a class="btn btn-primary" href="/app">Open the app</a>
  <a class="btn btn-ghost" href="/pricing">Back to pricing</a></div>
</div></section></main>
<script>
(function(){{
  var id = {json.dumps(session_id)};
  var heading = document.getElementById('billing-heading');
  var detail = document.getElementById('billing-detail');
  if (!id) {{
    heading.textContent = 'Nothing to confirm';
    detail.textContent = 'This page is where Stripe returns after a purchase.';
    return;
  }}
  var tries = 0;
  // The webhook and this redirect race, and the webhook usually loses. Polling
  // beats telling a paying customer that nothing happened.
  function poll() {{
    fetch('/v1/billing/session/' + encodeURIComponent(id), {{credentials:'same-origin'}})
      .then(function(r){{ return r.json(); }})
      .then(function(d){{
        if (d.payment_status === 'paid' || d.status === 'complete') {{
          heading.textContent = 'Payment received';
          detail.textContent = d.credits != null
            ? 'Your balance is now ' + d.credits.toLocaleString() + ' credits.'
            : 'Your account has been updated.';
          return;
        }}
        if (++tries < 10) setTimeout(poll, 1200);
        else detail.textContent = 'Still confirming. Your account updates as '
          + 'soon as Stripe confirms; nothing further is needed from you.';
      }})
      .catch(function(){{
        if (++tries < 10) setTimeout(poll, 1500);
      }});
  }}
  poll();
}})();
</script>"""
    return router.html(
        _page("Payment complete — twoHelixes", "Confirming your payment.",
              body, "/billing/complete",
              head='<meta name="robots" content="noindex">')
    )


@router.get("/share/{token}")
def shared_page(ctx: router.Context) -> router.Result:
    row = store.one(
        "SELECT title FROM dashboards WHERE share_token = ? AND is_public = 1",
        (ctx.params["token"],),
    )
    if row is None:
        return router.html(
            _page("Not found — twoHelixes", "", "<main><section><div class='shell'>"
                  "<h1>This dashboard is not shared</h1></div></section></main>"),
            status=404,
        )

    title = html.escape(str(row["title"]))
    return router.html(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — twoHelixes</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>{_css()}</style>
<link rel="stylesheet" href="/static/styles.css">
</head><body>
<div id="root" data-share="{html.escape(ctx.params['token'])}">
  <div style="display:grid;place-items:center;min-height:100vh">
    {helix.spinner(72, uid="shareboot")}
  </div>
</div>
<script type="module" src="/static/app.js"></script>
</body></html>"""
    )


# --------------------------------------------------------------------------
# Sign-in
# --------------------------------------------------------------------------


@router.post("/v1/auth/signin")
def signin(ctx: router.Context) -> router.Result:
    """Create or resume an account.

    In production this is fronted by the app.nz shared-cookie SSO; the email
    path exists so a self-hosted or local instance is usable on its own.
    """
    email = str(ctx.field("email") or "").strip().lower()
    if "@" not in email or len(email) > 320:
        return router.error(400, "invalid_email")

    user = store.get_user_by_email(email) or store.create_user(email)
    token = auth.mint_session(user["id"], ctx.header("user-agent"))

    identity = auth.Identity(
        user_id=user["id"],
        email=user["email"],
        plan=user.get("plan") or "free",
        api_credits=int(user.get("api_credits") or 0),
        free_queries_used=int(user.get("free_queries_used") or 0),
    )
    return router.Result(
        status=200,
        body=identity.to_public(),
        headers={"Set-Cookie": auth.cookie_header(token, secure=not config.is_dev())},
    )


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
