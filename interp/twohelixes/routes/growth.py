"""Machine-readable discovery files and comparison / use-case landing pages."""

from __future__ import annotations

import html
import json
from typing import Any

from twohelixes import config, router
from twohelixes.routes import pages
from twohelixes.routes.billing import PLANS

CACHE = {"Cache-Control": "public, max-age=300, stale-while-revalidate=3600"}

FAQ: tuple[tuple[str, str], ...] = (
    (
        "What is twoHelixes?",
        "An AI data analyst. Ask a question in plain language and it finds the data, "
        "joins it, shapes it, picks a chart and streams its reasoning while it works.",
    ),
    (
        "Do I need an account to try it?",
        "No. Ask one question on sample data without signing up. Free accounts get "
        f"{config.PLAN_ALLOWANCES['free']['chat_query']} AI charts a month with no card.",
    ),
    (
        "Which data sources does it connect to?",
        "CSV, TSV, JSON, Excel, OpenDocument and Parquet files, plus PostgreSQL, MySQL, "
        "SQL Server, Oracle, Snowflake, BigQuery, Redshift, ClickHouse, Trino, DuckDB, "
        "SQLite, MongoDB, Elasticsearch and HTTP APIs. All connections are read-only.",
    ),
    (
        "Can it replace Google Analytics, Mixpanel or Amplitude?",
        "For question-driven product analytics, yes: the first-party collector accepts "
        "GA4, Segment, Mixpanel and Amplitude shaped events. Audiences and ad exports "
        "are not supported; the parity map lists every gap.",
    ),
    (
        "Is there an API for agents?",
        "Yes. Every chart is one HTTP call. See /openapi.json, /llms.txt and /docs.",
    ),
    (
        "How is it priced?",
        "Free, then Plus at $19, Pro at $49 and Team at $149 a month, with pay-as-you-go "
        "credits (1 credit is 1 US cent) that never cut you off mid-month.",
    ),
)

ALTERNATIVES: dict[str, dict[str, Any]] = {
    "mixpanel": {
        "name": "Mixpanel",
        "title": "Mixpanel alternative for question-driven analytics",
        "desc": "twoHelixes accepts Mixpanel event and profile calls and answers product questions with charts instead of a report queue.",
        "lead": "Keep your Mixpanel instrumentation. Point it at a first-party collector and ask questions in plain language.",
        "rows": (
            ("Event ingestion", "Mixpanel event, profile, import and group calls are accepted unchanged", "Proprietary SDKs and project tokens"),
            ("Asking a question", "Plain language, with the reasoning trace and SQL visible", "Build a report in the report builder"),
            ("Data ownership", "First-party collector on your own domain", "Vendor-hosted project"),
            ("Funnels", "Ordered event flow endpoint and chat questions", "Funnels report"),
            ("Cohort activation and messaging", "Not supported", "Supported"),
            ("Other data", "Join events to CSV files and SQL databases in the same question", "Warehouse connectors on higher tiers"),
        ),
    },
    "google-analytics": {
        "name": "Google Analytics",
        "title": "Google Analytics alternative with a first-party collector",
        "desc": "A GA4-compatible first-party analytics collector that answers questions in plain language. Honest parity map included.",
        "lead": "GA-shaped event names and parameters, 30-minute sessions and session UTMs, with Do Not Track and Global Privacy Control respected.",
        "rows": (
            ("Event model", "GA4 Measurement Protocol shapes accepted; event_name plus JSON params", "GA4 event model"),
            ("Tracking script", "Small th.js, first-party endpoint", "gtag.js"),
            ("Privacy", "Random first-party ID; DNT and GPC disable it", "Google-hosted cookies and IDs"),
            ("Exploration", "Ask in chat, edit the chart by hand", "Explorations canvas"),
            ("Audiences and ad export", "Not supported", "Supported"),
            ("BigQuery export", "Bounded authenticated export batches", "Continuous linked export"),
        ),
        "link": ("/docs/analytics-parity", "Read the full GA4 parity map"),
    },
    "amplitude": {
        "name": "Amplitude",
        "title": "Amplitude alternative: ask, do not build reports",
        "desc": "Amplitude HTTP V2 event batches land in twoHelixes, where product questions become charts in seconds.",
        "lead": "Send Amplitude HTTP V2 batches to a collector you control, then ask the question instead of assembling a chart.",
        "rows": (
            ("Event ingestion", "Amplitude HTTP V2 batches accepted", "Amplitude SDKs"),
            ("Asking a question", "Plain language to chart, reasoning streamed", "Chart builder"),
            ("Ownership", "First-party collector", "Vendor-hosted"),
            ("Dashboards", "Pin any answer; share links need no account to read", "Dashboards and notebooks"),
            ("Experimentation and feature flags", "Not supported", "Supported"),
            ("Beyond events", "Join to files and SQL sources", "Warehouse-native add-ons"),
        ),
    },
}

USE_CASES: dict[str, dict[str, str]] = {
    "product-analytics": {
        "title": "Product analytics you can ask questions of",
        "desc": "Send events from your site or app and ask which pages convert, where sessions drop and what changed this week.",
        "lead": "Install the tracker or post events from your backend, then ask questions in chat.",
        "q": "which landing pages drive the most engaged sessions this month",
        "steps": "Add the th.js tracker or post to /v1/collect|Ask a question in plain language|Get a chart, the SQL and the reasoning",
    },
    "csv-to-chart": {
        "title": "Turn a CSV or spreadsheet into a chart",
        "desc": "Drop in CSV, Excel or Parquet and ask for the chart you want. No formulas, no pivot tables.",
        "lead": "Drag a file in and ask. Awkward delimiters and bad rows are retried automatically.",
        "q": "net revenue by month with a trend line",
        "steps": "Drop a CSV, Excel or Parquet file|Ask for the chart in words|Export SVG, PNG, CSV or a notebook",
    },
    "database-dashboards": {
        "title": "Dashboards from your SQL database",
        "desc": "Connect Postgres, MySQL, Snowflake, BigQuery and more read-only, ask questions and pin the answers to a shared dashboard.",
        "lead": "Read-only connections, generated SQL you can edit and rerun, and share links that need no account.",
        "q": "weekly signups by plan over the last quarter",
        "steps": "Add a read-only connection string|Ask or write SQL in the workbench|Pin charts to a dashboard and share the link",
    },
}


def _ld(data: Any) -> str:
    text = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{text}</script>'


def _crumbs(site: str, items: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": f"{site}{path}"}
            for i, (name, path) in enumerate(items)
        ],
    }


def faq_html() -> str:
    items = "".join(
        f"<details><summary>{html.escape(q)}</summary><p>{html.escape(a)}</p></details>"
        for q, a in FAQ
    )
    return (
        '<section class="band"><div class="shell"><h2>Frequently asked questions</h2>'
        f'<div class="faq">{items}</div></div></section>'
    )


def home_head() -> str:
    site = config.site_url().rstrip("/")
    offers = [
        {
            "@type": "Offer",
            "name": p["name"],
            "price": p["price"].lstrip("$"),
            "priceCurrency": "USD",
            "url": f"{site}/pricing",
        }
        for p in PLANS
    ]
    graph = [
        {
            "@type": "Organization",
            "@id": f"{site}/#org",
            "name": "twoHelixes",
            "url": site,
            "logo": f"{site}/favicon.svg",
        },
        {
            "@type": "WebSite",
            "@id": f"{site}/#site",
            "name": "twoHelixes",
            "url": site,
            "publisher": {"@id": f"{site}/#org"},
        },
        {
            "@type": "SoftwareApplication",
            "name": "twoHelixes",
            "applicationCategory": "BusinessApplication",
            "operatingSystem": "Web",
            "url": site,
            "description": "AI data analyst: ask a question, get a chart, SQL and the reasoning.",
            "publisher": {"@id": f"{site}/#org"},
            "offers": offers,
        },
        {
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": q,
                    "acceptedAnswer": {"@type": "Answer", "text": a},
                }
                for q, a in FAQ
            ],
        },
        _crumbs(site, [("Home", "/")]),
    ]
    return _ld({"@context": "https://schema.org", "@graph": graph})


def sitemap_entries() -> list[tuple[str, str, str]]:
    out = [
        ("/alternatives", "monthly", "0.7"),
        ("/use-cases", "monthly", "0.7"),
    ]
    out += [(f"/alternatives/{k}", "monthly", "0.7") for k in ALTERNATIVES]
    out += [(f"/use-cases/{k}", "monthly", "0.7") for k in USE_CASES]
    out += [("/llms.txt", "monthly", "0.3"), ("/openapi.json", "monthly", "0.3")]
    return out


def _page(title: str, desc: str, path: str, body: str, crumbs: list[tuple[str, str]], extra: Any = None) -> router.Result:
    site = config.site_url().rstrip("/")
    graph = [_crumbs(site, crumbs)]
    if extra:
        graph.append(extra)
    head = _ld({"@context": "https://schema.org", "@graph": graph})
    return router.html(pages._page(title, desc, body, path, head=head), **CACHE)


CTA = (
    '<section class="band"><div class="shell"><h2>Try it on sample data</h2>'
    '<p class="lead-in">No account needed for your first question.</p>'
    '<a class="btn btn-primary" href="/app?sample=orders">Explore sample data</a> '
    '<a class="btn btn-ghost" href="/pricing">See pricing</a></div></section>'
)


def _cards(base: str, entries: dict[str, dict[str, Any]], key: str) -> str:
    return "".join(
        f'<a class="card" href="{base}/{slug}"><h2 style="font-size:1.3rem">'
        f'{html.escape(e[key])}</h2><p>{html.escape(e["desc"])}</p></a>'
        for slug, e in entries.items()
    )


@router.get("/alternatives")
def alternatives_index(ctx: router.Context) -> router.Result:
    body = (
        '<main id="content"><section class="page-head"><div class="shell">'
        '<p class="kicker">Compare</p><h1>Analytics alternatives</h1>'
        '<p class="sub">Honest comparisons, including what twoHelixes does not do.</p>'
        f'</div></section><section><div class="shell"><div class="grid">'
        f'{_cards("/alternatives", ALTERNATIVES, "title")}</div></div></section>{CTA}</main>'
    )
    return _page(
        "Analytics alternatives — twoHelixes",
        "Compare twoHelixes with Mixpanel, Google Analytics and Amplitude.",
        "/alternatives", body, [("Home", "/"), ("Alternatives", "/alternatives")],
    )


@router.get("/alternatives/{slug}")
def alternative(ctx: router.Context) -> router.Result:
    slug = ctx.params.get("slug", "")
    e = ALTERNATIVES.get(slug)
    if not e:
        return router.error(404, "not_found")
    rows = "".join(
        f"<tr><td><b>{html.escape(a)}</b></td><td>{html.escape(b)}</td><td>{html.escape(c)}</td></tr>"
        for a, b, c in e["rows"]
    )
    link = e.get("link")
    more = (
        f'<p><a class="btn btn-ghost" href="{link[0]}">{html.escape(link[1])}</a></p>'
        if link else ""
    )
    path = f"/alternatives/{slug}"
    body = f"""<main id="content"><section class="page-head"><div class="shell">
<p class="kicker">Compare</p><h1>{html.escape(e['title'])}</h1>
<p class="sub">{html.escape(e['lead'])}</p></div></section>
<section><div class="shell"><div class="scroll-x"><table class="packs">
<thead><tr><th>Capability</th><th>twoHelixes</th><th>{html.escape(e['name'])}</th></tr></thead>
<tbody>{rows}</tbody></table></div>{more}
<p class="sub">Comparison reflects documented behaviour at time of writing. {html.escape(e['name'])} is a trademark of its owner.</p>
</div></section>{CTA}</main>"""
    return _page(
        f"{e['title']} — twoHelixes", e["desc"], path, body,
        [("Home", "/"), ("Alternatives", "/alternatives"), (e["name"], path)],
    )


@router.get("/use-cases")
def use_cases_index(ctx: router.Context) -> router.Result:
    body = (
        '<main id="content"><section class="page-head"><div class="shell">'
        '<p class="kicker">Use cases</p><h1>What people ask twoHelixes</h1>'
        '<p class="sub">From a spreadsheet to a production database.</p>'
        f'</div></section><section><div class="shell"><div class="grid">'
        f'{_cards("/use-cases", USE_CASES, "title")}</div></div></section>{CTA}</main>'
    )
    return _page(
        "Use cases — twoHelixes",
        "Product analytics, CSV to chart and SQL dashboards with twoHelixes.",
        "/use-cases", body, [("Home", "/"), ("Use cases", "/use-cases")],
    )


@router.get("/use-cases/{slug}")
def use_case(ctx: router.Context) -> router.Result:
    slug = ctx.params.get("slug", "")
    e = USE_CASES.get(slug)
    if not e:
        return router.error(404, "not_found")
    steps = e["steps"].split("|")
    lis = "".join(f"<li>{html.escape(s)}</li>" for s in steps)
    path = f"/use-cases/{slug}"
    howto = {
        "@type": "HowTo",
        "name": e["title"],
        "step": [{"@type": "HowToStep", "position": i + 1, "name": s} for i, s in enumerate(steps)],
    }
    body = f"""<main id="content"><section class="page-head"><div class="shell">
<p class="kicker">Use case</p><h1>{html.escape(e['title'])}</h1>
<p class="sub">{html.escape(e['lead'])}</p></div></section>
<section><div class="shell"><h2>How it works</h2><ol class="rules">{lis}</ol>
<p class="lead-in" style="margin-top:var(--s5)">Example question: <i>{html.escape(e['q'])}</i></p>
<a class="btn btn-primary" href="/app?sample=orders">Try a question</a>
<a class="btn btn-ghost" href="/docs">API docs</a></div></section>{CTA}</main>"""
    return _page(
        f"{e['title']} — twoHelixes", e["desc"], path, body,
        [("Home", "/"), ("Use cases", "/use-cases"), (e["title"], path)], howto,
    )


def _llms_text(full: bool) -> str:
    site = config.site_url().rstrip("/")
    lines = [
        "# twoHelixes",
        "",
        "> AI data analyst. Ask a question in plain language; it finds the data, joins it, "
        "picks a chart and streams its reasoning. Connects to files, SQL databases and a "
        "first-party analytics collector.",
        "",
        "## Pages",
        f"- [Home]({site}/): product overview and sample data",
        f"- [Features]({site}/features): capabilities and chart forms",
        f"- [Pricing]({site}/pricing): Free, Plus $19, Pro $49, Team $149 per month plus credits",
        f"- [API docs]({site}/docs): HTTP API with curl examples",
        f"- [OpenAPI]({site}/openapi.json): machine-readable API description",
        f"- [GA4 parity]({site}/docs/analytics-parity): what matches Google Analytics and what does not",
        f"- [Datasets]({site}/datasets): public datasets with worked charts",
        f"- [Alternatives]({site}/alternatives): comparisons with Mixpanel, Google Analytics, Amplitude",
        f"- [Use cases]({site}/use-cases): product analytics, CSV to chart, database dashboards",
        f"- [Blog]({site}/blog): field notes",
        "",
        "## API",
        f"- Base URL: {site}/v1",
        "- Auth: Authorization: Bearer <API key> (create one in the app under billing)",
        "- Ask: POST /v1/query with {\"q\": \"...\", \"source_id\": \"...\"}",
        "- Stream: POST /v1/query/stream (Server-Sent Events)",
        "- Collect events: POST /v1/collect with {\"site_id\": \"...\", \"events\": [...]}",
        "",
    ]
    if full:
        lines.append("## Endpoints")
        for method, route, title, note, _snippet in pages.ENDPOINTS:
            lines.append(f"- {method} {route}: {title}. {note}")
        lines += ["", "## FAQ"]
        for q, a in FAQ:
            lines += [f"### {q}", a, ""]
        lines += ["## Compare"]
        for slug, e in ALTERNATIVES.items():
            lines.append(f"### {e['name']}")
            for a, b, c in e["rows"]:
                lines.append(f"- {a}: twoHelixes {b}; {e['name']} {c}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@router.get("/llms.txt")
def llms(ctx: router.Context) -> router.Result:
    return router.Result(body=_llms_text(False), content_type="text/plain; charset=utf-8", headers=CACHE)


@router.get("/llms-full.txt")
def llms_full(ctx: router.Context) -> router.Result:
    return router.Result(body=_llms_text(True), content_type="text/plain; charset=utf-8", headers=CACHE)


@router.get("/openapi.json")
def openapi(ctx: router.Context) -> router.Result:
    site = config.site_url().rstrip("/")
    paths: dict[str, Any] = {}
    for method, route, title, note, _snippet in pages.ENDPOINTS:
        op: dict[str, Any] = {
            "summary": title,
            "description": note,
            "security": [{"bearer": []}],
            "responses": {"200": {"description": "OK"}},
        }
        params = [
            {"name": n, "in": "path", "required": True, "schema": {"type": "string"}}
            for n in __import__("re").findall(r"\{(\w+)\}", route)
        ]
        if params:
            op["parameters"] = params
        if method == "POST":
            op["requestBody"] = {"content": {"application/json": {"schema": {"type": "object"}}}}
        paths.setdefault(route, {})[method.lower()] = op
    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": "twoHelixes API",
            "version": "1",
            "description": "Ask questions, stream reasoning, generate SQL and export charts.",
        },
        "servers": [{"url": site}],
        "paths": paths,
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
    }
    return router.Result(body=doc, headers=CACHE)
