"""Public, shareable datasets with curated pages.

An owner publishes one of their datasets under a slug, writes it up, and links
their own charts and dashboards to it. Everything crosses one boundary here:
a row is only ever read for a public page through `_public_row`, which selects
named columns, so storage paths, source configs, raw uploads and user ids have
no way into a response by accident.

Publishing is a statement that the contents are public, so it needs
`confirm_public`, and unpublishing is a column flip: the page, the JSON and the
CSV all look the row up with `is_public = 1`, so the next request is a 404.

Routes are registered ahead of `datasets` (see `router.build`) because the
router is first-match and `/datasets/{key}` would otherwise swallow
`/datasets/community` and `/datasets/u/{slug}`.
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import pandas as pd

from twohelixes import auth, config, ratelimit, router, store
from twohelixes.charts import svg
from twohelixes.datasets import samples
from twohelixes.routes import dashboards as dashboard_routes
from twohelixes.routes.library import _records
from twohelixes.routes.pages import _page

log = logging.getLogger("twohelixes.routes.community")

PREVIEW_ROWS = 50
MAX_CSV_ROWS = 50_000
PAGE_SIZE = 24
MAX_TITLE = 120
MAX_DESCRIPTION = 10_000
MAX_CAPTION = 140
MAX_TAGS = 8
MAX_LINKS = 24
MAX_AUTHOR = 60
MAX_SITEMAP = 5000

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,28}[a-z0-9]$|^[a-z0-9]$")

RESERVED_SLUGS = frozenset(
    "u community new edit admin api v1 app static download search tags sample "
    "samples null undefined www login signin signup help docs blog features "
    "pricing schools share billing account robots sitemap".split()
)

LICENSES: dict[str, tuple[str, str]] = {
    "all-rights-reserved": ("All rights reserved", ""),
    "cc0-1.0": ("CC0 1.0", "https://creativecommons.org/publicdomain/zero/1.0/"),
    "cc-by-4.0": ("CC BY 4.0", "https://creativecommons.org/licenses/by/4.0/"),
    "cc-by-sa-4.0": ("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-sa/4.0/"),
    "odbl-1.0": ("ODbL 1.0", "https://opendatacommons.org/licenses/odbl/1-0/"),
    "pddl-1.0": ("PDDL 1.0", "https://opendatacommons.org/licenses/pddl/1-0/"),
    "mit": ("MIT", "https://opensource.org/license/mit"),
}

PUBLIC_COLUMNS = (
    "id, name, public_slug, public_title, public_description, public_tags, "
    "public_license, public_author, allow_download, cover_chart_id, "
    "row_count, published_at, updated_at, user_id, storage"
)


# --------------------------------------------------------------------------
# Markdown subset
# --------------------------------------------------------------------------

_LINK = re.compile(r"\[([^\]\n]{1,200})\]\((https?://[^\s()<>]{1,500})\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])")
_HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+(.*)$")


def _inline(text: str) -> str:
    parts = re.split(r"(`[^`\n]+`)", text)
    out: list[str] = []
    for part in parts:
        if len(part) > 1 and part.startswith("`") and part.endswith("`"):
            out.append(f"<code>{html.escape(part[1:-1], quote=True)}</code>")
            continue
        escaped = html.escape(part, quote=True)
        links: list[str] = []

        def stash(match: re.Match[str]) -> str:
            links.append(
                f'<a href="{match.group(2)}" rel="nofollow ugc noopener" '
                f'target="_blank">{match.group(1)}</a>'
            )
            return f"\x00{len(links) - 1}\x00"

        escaped = _LINK.sub(stash, escaped)
        escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
        escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
        escaped = re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], escaped)
        out.append(escaped)
    return "".join(out)


def render_markdown(source: str) -> str:
    """Headings, bold, italic, http(s) links, lists and code - nothing else.

    Everything is HTML-escaped before any markup is added, and a link only
    matches an http(s) URL, so there is no path from the text to a tag, an
    attribute, or a `javascript:` URL.
    """
    lines = (source or "")[:MAX_DESCRIPTION].replace("\x00", "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    items: list[str] = []
    list_tag = ""
    code: list[str] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    def flush_list() -> None:
        nonlocal list_tag
        if items:
            body = "".join(f"<li>{_inline(item)}</li>" for item in items)
            out.append(f"<{list_tag}>{body}</{list_tag}>")
            items.clear()
        list_tag = ""

    for line in lines:
        if code is not None:
            if line.strip().startswith("```"):
                body = html.escape("\n".join(code), quote=True)
                out.append(f"<pre><code>{body}</code></pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush_paragraph()
            flush_list()
            code = []
            continue
        if not line.strip():
            flush_paragraph()
            flush_list()
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_paragraph()
            flush_list()
            level = len(heading.group(1)) + 1
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            continue
        bullet, ordered = _BULLET.match(line), _ORDERED.match(line)
        if bullet or ordered:
            flush_paragraph()
            wanted = "ul" if bullet else "ol"
            if list_tag and list_tag != wanted:
                flush_list()
            list_tag = wanted
            items.append((bullet or ordered).group(1))  # type: ignore[union-attr]
            continue
        flush_list()
        paragraph.append(line.strip())

    if code is not None:
        out.append(f"<pre><code>{html.escape(chr(10).join(code), quote=True)}</code></pre>")
    flush_paragraph()
    flush_list()
    return "".join(out)


def _plain(text: str, limit: int = 160) -> str:
    cleaned = re.sub(r"[#*`>\[\]()]", "", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


class Invalid(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail

    def result(self) -> router.Result:
        return router.error(400, self.code, self.detail)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60].strip("-")
    return slug


def _check_slug(slug: str) -> str:
    slug = str(slug or "").strip().lower()
    if not SLUG_RE.match(slug) or "--" in slug:
        raise Invalid(
            "invalid_slug",
            "Use 3-60 characters: lowercase letters, digits and single hyphens.",
        )
    if slug in RESERVED_SLUGS or slug in samples.BY_KEY:
        raise Invalid("reserved_slug", "That slug is reserved.")
    return slug


def _clean_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, list):
        raise Invalid("invalid_tags", "tags must be a list of strings.")
    tags: list[str] = []
    for raw in value:
        tag = re.sub(r"\s+", "-", str(raw).strip().lower())
        if not tag:
            continue
        if not TAG_RE.match(tag):
            raise Invalid("invalid_tags", f"Tag {str(raw)[:30]!r} is not allowed.")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise Invalid("too_many_tags", f"At most {MAX_TAGS} tags.")
    return tags


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------


def _owned_dataset(identity: Any, dataset_id: str) -> dict[str, Any] | None:
    return store.row_to_dict(
        store.one(
            "SELECT id, user_id, name, description, storage, row_count, is_public, "
            "public_slug, public_title, public_description, public_tags, "
            "public_license, public_author, allow_download, cover_chart_id, "
            "published_at FROM datasets WHERE id = ? AND user_id = ?",
            (dataset_id, identity.user_id),
        )
    )


def _public_row(slug: str) -> dict[str, Any] | None:
    return store.row_to_dict(
        store.one(
            f"SELECT {PUBLIC_COLUMNS} FROM datasets "
            "WHERE public_slug = ? AND is_public = 1",
            (slug,),
        )
    )


def _owner_state(row: dict[str, Any]) -> dict[str, Any]:
    site = config.site_url().rstrip("/")
    slug = row.get("public_slug")
    public = bool(row.get("is_public"))
    return {
        "dataset_id": row["id"],
        "published": public,
        "slug": slug,
        "title": row.get("public_title") or row.get("name"),
        "description": row.get("public_description") or "",
        "tags": store.load_json(row.get("public_tags"), []),
        "license": row.get("public_license") or "all-rights-reserved",
        "author_name": row.get("public_author") or "",
        "allow_download": bool(row.get("allow_download")),
        "cover_chart_id": row.get("cover_chart_id"),
        "published_at": row.get("published_at"),
        "url": f"{site}/datasets/u/{slug}" if public and slug else None,
        "download_url": (
            f"{site}/v1/public/datasets/{slug}/download.csv"
            if public and slug and row.get("allow_download")
            else None
        ),
    }


def _load_frame(row: dict[str, Any]) -> pd.DataFrame | None:
    path = row.get("storage")
    if not path or not Path(path).exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:  # noqa: BLE001
        log.exception("public dataset %s unreadable", row.get("id"))
        return None


def _schema(frame: pd.DataFrame) -> list[dict[str, str]]:
    out = []
    for name in frame.columns:
        kind = getattr(frame[name].dtype, "kind", "O")
        label = (
            "date" if kind == "M" else "number" if kind in "ifu"
            else "boolean" if kind == "b" else "text"
        )
        non_null = frame[name].dropna()
        example = str(non_null.iloc[0])[:40] if len(non_null) else ""
        out.append({"name": str(name), "type": label, "example": example})
    return out


def _links(dataset_id: str, *, public_only: bool) -> list[dict[str, Any]]:
    rows = store.rows_to_dicts(
        store.query(
            "SELECT kind, object_id, caption, position FROM dataset_links "
            "WHERE dataset_id = ? ORDER BY position, created_at",
            (dataset_id,),
        )
    )
    out: list[dict[str, Any]] = []
    for link in rows:
        if link["kind"] == "chart":
            item = store.row_to_dict(
                store.one(
                    "SELECT id, title, spec, is_public FROM charts WHERE id = ?",
                    (link["object_id"],),
                )
            )
        else:
            item = store.row_to_dict(
                store.one(
                    "SELECT id, title, is_public, share_token FROM dashboards WHERE id = ?",
                    (link["object_id"],),
                )
            )
        if item is None:
            continue
        public = bool(item.get("is_public")) and (
            link["kind"] == "chart" or bool(item.get("share_token"))
        )
        if public_only and not public:
            continue
        entry = {
            "kind": link["kind"],
            "id": item["id"],
            "title": item["title"],
            "caption": link.get("caption") or "",
            "position": link["position"],
            "public": public,
        }
        if link["kind"] == "dashboard" and public:
            entry["share_url"] = f"/share/{item['share_token']}"
        if link["kind"] == "chart":
            entry["_spec"] = item["spec"]
        out.append(entry)
    return out


def _strip_private(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in link.items() if not k.startswith("_")} for link in links]


def _chart_svg(spec_json: str, mode: str = "light") -> str:
    spec = store.load_json(spec_json, {})
    if not spec.get("data"):
        return ""
    try:
        figure = dashboard_routes._themed(spec, mode)
        return svg.render(
            figure, width=560, height=360, mode=mode,
            transparent=True, theme_vars=True,
        )
    except Exception:  # noqa: BLE001
        log.exception("could not render a linked chart")
        return ""


def _throttled(ctx: router.Context, bucket: str = "public_read") -> router.Result | None:
    refusal = ratelimit.public_read(ctx, bucket)
    if refusal is None:
        return None
    return router.error(
        429, "rate_limited", refusal.detail,
        retry_after=refusal.retry_after,
    )


def _throttled_html(ctx: router.Context) -> router.Result | None:
    refusal = ratelimit.public_read(ctx)
    if refusal is None:
        return None
    return router.html(
        _page("Slow down — twoHelixes", "", "<main><section><div class='shell'>"
              "<h1>Too many requests</h1><p>Try again in a minute.</p>"
              "</div></section></main>", "/datasets/community",
              head='<meta name="robots" content="noindex">'),
        status=429,
        **{"Retry-After": "60"},
    )


# --------------------------------------------------------------------------
# Owner API
# --------------------------------------------------------------------------


@router.get("/v1/datasets/{dataset_id}/publish")
def publish_state(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned_dataset(identity, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    state = _owner_state(row)
    state["links"] = _strip_private(_links(row["id"], public_only=False))
    state["licenses"] = {key: label for key, (label, _url) in LICENSES.items()}
    return router.json_result(state)


@router.put("/v1/datasets/{dataset_id}/publish")
def publish(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned_dataset(identity, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    body = ctx.json if isinstance(ctx.json, dict) else {}

    if body.get("confirm_public") is not True:
        return router.error(
            400, "confirm_required",
            "Publishing makes the dataset's schema, a row preview and its "
            "linked analyses public. Send confirm_public=true to acknowledge.",
        )

    try:
        title = str(body.get("title") or row.get("public_title") or row["name"]).strip()
        if not 3 <= len(title) <= MAX_TITLE:
            raise Invalid("invalid_title", f"Title must be 3-{MAX_TITLE} characters.")
        description = str(
            body["description"] if "description" in body
            else row.get("public_description") or ""
        )
        if len(description) > MAX_DESCRIPTION:
            raise Invalid("description_too_long", f"At most {MAX_DESCRIPTION} characters.")
        slug_input = body.get("slug") or row.get("public_slug") or _slugify(title)
        slug = _check_slug(slug_input)
        tags = (
            _clean_tags(body["tags"]) if "tags" in body
            else store.load_json(row.get("public_tags"), [])
        )
        license_key = str(
            body.get("license") or row.get("public_license") or "all-rights-reserved"
        )
        if license_key not in LICENSES:
            raise Invalid("invalid_license", f"One of: {', '.join(LICENSES)}.")
        author = str(
            body["author_name"] if "author_name" in body
            else row.get("public_author") or ""
        ).strip()
        if len(author) > MAX_AUTHOR:
            raise Invalid("invalid_author", f"At most {MAX_AUTHOR} characters.")
        allow_download = (
            bool(body["allow_download"]) if "allow_download" in body
            else bool(row.get("allow_download"))
        )
        cover = body["cover_chart_id"] if "cover_chart_id" in body else row.get("cover_chart_id")
        cover = str(cover) if cover else None
        if cover and not store.one(
            "SELECT 1 FROM dataset_links WHERE dataset_id = ? AND kind = 'chart' "
            "AND object_id = ?",
            (row["id"], cover),
        ):
            raise Invalid("cover_not_linked", "The cover must be a chart linked to this dataset.")
    except Invalid as exc:
        return exc.result()

    if not row.get("storage") or not Path(str(row["storage"])).exists():
        return router.error(409, "dataset_unavailable", "The dataset file is missing.")

    taken = store.one(
        "SELECT id FROM datasets WHERE public_slug = ? AND id != ?", (slug, row["id"])
    )
    if taken:
        return router.error(409, "slug_taken", "That slug is already in use.")

    now = time.time()
    published_at = row.get("published_at") if row.get("is_public") else now
    try:
        store.execute(
            "UPDATE datasets SET is_public = 1, public_slug = ?, public_title = ?, "
            "public_description = ?, public_tags = ?, public_license = ?, "
            "public_author = ?, allow_download = ?, cover_chart_id = ?, "
            "published_at = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (
                slug, title, description, store.dump_json(tags), license_key,
                author, 1 if allow_download else 0, cover, published_at, now,
                row["id"], identity.user_id,
            ),
        )
    except Exception as exc:  # noqa: BLE001 - unique index lost a race
        if "public_slug" in str(exc) or "unique" in str(exc).lower():
            return router.error(409, "slug_taken", "That slug is already in use.")
        raise

    fresh = _owned_dataset(identity, row["id"]) or row
    state = _owner_state(fresh)
    state["links"] = _strip_private(_links(row["id"], public_only=False))
    return router.json_result(state)


@router.delete("/v1/datasets/{dataset_id}/publish")
def unpublish(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned_dataset(identity, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    store.execute(
        "UPDATE datasets SET is_public = 0, updated_at = ? WHERE id = ? AND user_id = ?",
        (time.time(), row["id"], identity.user_id),
    )
    return router.json_result(
        {
            "published": False,
            "dataset_id": row["id"],
            "note": (
                "The public page, JSON and CSV are gone. Linked charts and "
                "dashboards stay public until you unshare them."
            ),
        }
    )


@router.get("/v1/datasets/{dataset_id}/links")
def list_links(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned_dataset(identity, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    charts = store.rows_to_dicts(
        store.query(
            "SELECT id, title, is_public FROM charts WHERE user_id = ? "
            "ORDER BY updated_at DESC LIMIT 100",
            (identity.user_id,),
        )
    )
    dashboards = store.rows_to_dicts(
        store.query(
            "SELECT id, title, is_public FROM dashboards WHERE user_id = ? "
            "ORDER BY updated_at DESC LIMIT 100",
            (identity.user_id,),
        )
    )
    return router.json_result(
        {
            "links": _strip_private(_links(row["id"], public_only=False)),
            "available": {"charts": charts, "dashboards": dashboards},
        }
    )


def _owned_object(identity: Any, kind: str, object_id: str) -> dict[str, Any] | None:
    table = "charts" if kind == "chart" else "dashboards"
    return store.row_to_dict(
        store.one(
            f"SELECT id, title, is_public FROM {table} WHERE id = ? AND user_id = ?",
            (object_id, identity.user_id),
        )
    )


@router.post("/v1/datasets/{dataset_id}/links")
def add_link(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned_dataset(identity, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    body = ctx.json if isinstance(ctx.json, dict) else {}
    kind = str(body.get("kind") or "")
    object_id = str(body.get("id") or body.get("object_id") or "")
    if kind not in ("chart", "dashboard") or not object_id:
        return router.error(400, "invalid_link", "kind must be chart or dashboard, with an id.")
    caption = str(body.get("caption") or "").strip()
    if len(caption) > MAX_CAPTION:
        return router.error(400, "caption_too_long", f"At most {MAX_CAPTION} characters.")
    item = _owned_object(identity, kind, object_id)
    if item is None:
        return router.error(404, "not_found")

    existing = store.one(
        "SELECT id FROM dataset_links WHERE dataset_id = ? AND kind = ? AND object_id = ?",
        (row["id"], kind, object_id),
    )
    count = store.one(
        "SELECT COUNT(*) AS n, COALESCE(MAX(position), -1) AS top "
        "FROM dataset_links WHERE dataset_id = ?",
        (row["id"],),
    )
    if not existing and int(count["n"]) >= MAX_LINKS:
        return router.error(409, "too_many_links", f"At most {MAX_LINKS} linked analyses.")
    try:
        position = int(body["position"]) if body.get("position") is not None else None
    except (TypeError, ValueError):
        return router.error(400, "invalid_position")

    now = time.time()
    auto: dict[str, Any] = {"chart_public": False, "dashboard_share": False}
    share_url = None
    with store.transaction() as conn:
        if kind == "chart" and not item["is_public"]:
            conn.execute(
                "UPDATE charts SET is_public = 1, updated_at = ? WHERE id = ?",
                (now, object_id),
            )
            auto["chart_public"] = True
        if kind == "dashboard":
            current = conn.execute(
                "SELECT is_public, share_token FROM dashboards WHERE id = ?", (object_id,)
            ).fetchone()
            token = current["share_token"] if current else None
            if not current["is_public"] or not token:
                token = token or secrets.token_urlsafe(18)
                conn.execute(
                    "UPDATE dashboards SET is_public = 1, share_token = ?, "
                    "updated_at = ? WHERE id = ?",
                    (token, now, object_id),
                )
                auto["dashboard_share"] = True
            share_url = f"{config.site_url()}/share/{token}"
        if existing:
            conn.execute(
                "UPDATE dataset_links SET caption = ?, position = COALESCE(?, position) "
                "WHERE id = ?",
                (caption, position, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO dataset_links (id, dataset_id, kind, object_id, caption, "
                "position, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    store.new_id(), row["id"], kind, object_id, caption,
                    position if position is not None else int(count["top"]) + 1, now,
                ),
            )

    notice = ""
    if auto["chart_public"]:
        notice = "This chart was made public so it can appear on the dataset page."
    elif auto["dashboard_share"]:
        notice = "This dashboard's share link was enabled so it can appear on the dataset page."
    return router.json_result(
        {
            "linked": True,
            "kind": kind,
            "id": object_id,
            "caption": caption,
            "auto_enabled": auto,
            "share_url": share_url,
            "notice": notice,
            "links": _strip_private(_links(row["id"], public_only=False)),
        },
        status=200 if existing else 201,
    )


@router.delete("/v1/datasets/{dataset_id}/links")
def remove_link(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    row = _owned_dataset(identity, ctx.params["dataset_id"])
    if row is None:
        return router.error(404, "not_found")
    kind = str(ctx.field("kind") or "")
    object_id = str(ctx.field("id") or ctx.field("object_id") or "")
    if kind not in ("chart", "dashboard") or not object_id:
        return router.error(400, "invalid_link", "kind must be chart or dashboard, with an id.")
    revoke = str(ctx.field("revoke") or "").lower() in ("1", "true", "yes")
    with store.transaction() as conn:
        removed = conn.execute(
            "DELETE FROM dataset_links WHERE dataset_id = ? AND kind = ? AND object_id = ?",
            (row["id"], kind, object_id),
        ).rowcount
    if not removed:
        return router.error(404, "not_linked")
    if row.get("cover_chart_id") == object_id and kind == "chart":
        store.execute("UPDATE datasets SET cover_chart_id = NULL WHERE id = ?", (row["id"],))

    revoked = False
    if revoke and _owned_object(identity, kind, object_id):
        other = store.one(
            "SELECT 1 FROM dataset_links WHERE kind = ? AND object_id = ?",
            (kind, object_id),
        )
        if other is None:
            if kind == "chart":
                store.execute("UPDATE charts SET is_public = 0 WHERE id = ?", (object_id,))
            else:
                store.execute(
                    "UPDATE dashboards SET is_public = 0, share_token = NULL WHERE id = ?",
                    (object_id,),
                )
            revoked = True
    return router.json_result(
        {
            "unlinked": True,
            "still_public": not revoked,
            "links": _strip_private(_links(row["id"], public_only=False)),
        }
    )


# --------------------------------------------------------------------------
# Public JSON
# --------------------------------------------------------------------------


def _listing_query(ctx: router.Context) -> tuple[list[dict[str, Any]], int, int, str, str]:
    query = (ctx.q("q", "") or "").strip()[:100]
    tag = (ctx.q("tag", "") or "").strip().lower()[:30]
    page = max(1, ctx.q_int("page", 1))
    where = ["is_public = 1", "public_slug IS NOT NULL"]
    params: list[Any] = []
    if tag and TAG_RE.match(tag):
        where.append("public_tags LIKE ?")
        params.append(f'%"{tag}"%')
    for term in query.lower().split()[:5]:
        escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append(
            "(LOWER(COALESCE(public_title, name)) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(public_description, '')) LIKE ? ESCAPE '\\' "
            "OR LOWER(COALESCE(public_tags, '')) LIKE ? ESCAPE '\\')"
        )
        params.extend([f"%{escaped}%"] * 3)
    clause = " AND ".join(where)
    total = int(
        store.one(f"SELECT COUNT(*) AS n FROM datasets WHERE {clause}", params)["n"]
    )
    rows = store.rows_to_dicts(
        store.query(
            f"SELECT {PUBLIC_COLUMNS}, columns FROM datasets WHERE {clause} "
            "ORDER BY published_at DESC, id LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, (page - 1) * PAGE_SIZE],
        )
    )
    return rows, total, page, query, tag


def _card_entry(row: dict[str, Any]) -> dict[str, Any]:
    site = config.site_url().rstrip("/")
    slug = row["public_slug"]
    license_label = LICENSES.get(row.get("public_license") or "", LICENSES["all-rights-reserved"])[0]
    return {
        "slug": slug,
        "title": row.get("public_title") or row["name"],
        "summary": _plain(row.get("public_description") or ""),
        "tags": store.load_json(row.get("public_tags"), []),
        "license": row.get("public_license") or "all-rights-reserved",
        "license_label": license_label,
        "author": row.get("public_author") or "Community member",
        "row_count": int(row.get("row_count") or 0),
        "column_count": len(store.load_json(row.get("columns"), [])),
        "published_at": row.get("published_at"),
        "path": f"/datasets/u/{slug}",
        "url": f"{site}/datasets/u/{slug}",
        "allow_download": bool(row.get("allow_download")),
    }


@router.get("/v1/public/datasets")
def public_list(ctx: router.Context) -> router.Result:
    throttled = _throttled(ctx)
    if throttled:
        return throttled
    rows, total, page, query, tag = _listing_query(ctx)
    return router.json_result(
        {
            "datasets": [_card_entry(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": PAGE_SIZE,
            "query": query,
            "tag": tag,
        }
    )


def _public_detail(row: dict[str, Any], rows_wanted: int) -> dict[str, Any] | None:
    frame = _load_frame(row)
    if frame is None:
        return None
    site = config.site_url().rstrip("/")
    entry = _card_entry({**row, "columns": store.dump_json(list(map(str, frame.columns)))})
    entry["description"] = row.get("public_description") or ""
    entry["schema"] = _schema(frame)
    entry["preview"] = _records(frame.head(rows_wanted))
    entry["preview_rows"] = min(rows_wanted, len(frame))
    entry["row_count"] = int(len(frame))
    entry["analytics"] = _strip_private(_links(row["id"], public_only=True))
    entry["analyse_url"] = f"{site}/app?community={quote(row['public_slug'])}"
    entry["download_url"] = (
        f"{site}/v1/public/datasets/{row['public_slug']}/download.csv"
        if row.get("allow_download") else None
    )
    entry["download_row_limit"] = MAX_CSV_ROWS if row.get("allow_download") else None
    return entry


@router.get("/v1/public/datasets/{slug}")
def public_detail(ctx: router.Context) -> router.Result:
    throttled = _throttled(ctx)
    if throttled:
        return throttled
    row = _public_row(ctx.params["slug"])
    if row is None:
        return router.error(404, "not_found")
    wanted = max(1, min(PREVIEW_ROWS, ctx.q_int("rows", PREVIEW_ROWS)))
    detail = _public_detail(row, wanted)
    if detail is None:
        return router.error(404, "not_found")
    return router.json_result(detail)


@router.get("/v1/public/datasets/{slug}/download.csv")
def public_download(ctx: router.Context) -> router.Result:
    throttled = _throttled(ctx, "public_download")
    if throttled:
        return throttled
    slug = ctx.params["slug"]
    row = _public_row(slug)
    if row is None:
        return router.error(404, "not_found")
    if not row.get("allow_download"):
        return router.error(403, "download_disabled")
    frame = _load_frame(row)
    if frame is None:
        return router.error(404, "not_found")
    headers = {"Content-Disposition": f'attachment; filename="{slug}.csv"'}
    if len(frame) > MAX_CSV_ROWS:
        headers["X-Rows-Truncated"] = str(MAX_CSV_ROWS)
    return router.Result(
        status=200,
        body=frame.head(MAX_CSV_ROWS).to_csv(index=False),
        content_type="text/csv; charset=utf-8",
        headers=headers,
    )


@router.post("/v1/public/datasets/{slug}/import")
def public_import(ctx: router.Context) -> router.Result:
    """Copy a downloadable public dataset into the caller's own library."""
    identity = auth.require(ctx)
    throttled = _throttled(ctx, "public_download")
    if throttled:
        return throttled
    row = _public_row(ctx.params["slug"])
    if row is None:
        return router.error(404, "not_found")
    if not row.get("allow_download"):
        return router.error(403, "download_disabled")
    frame = _load_frame(row)
    if frame is None:
        return router.error(404, "not_found")
    frame = frame.head(MAX_CSV_ROWS)
    dataset_id = store.new_id()
    folder = config.data_dir() / "uploads" / str(identity.user_id)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{dataset_id}.parquet"
    frame.to_parquet(target, index=False)
    now = time.time()
    name = str(row.get("public_title") or row["name"])[:200]
    store.execute(
        "INSERT INTO datasets (id, user_id, source_id, folder_id, name, description, "
        "columns, row_count, storage, raw_storage, shape_report, sheet_name, "
        "created_at, updated_at) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)",
        (
            dataset_id, identity.user_id, name,
            _plain(row.get("public_description") or "", 500),
            store.dump_json([str(c) for c in frame.columns]),
            int(len(frame)), str(target), now, now,
        ),
    )
    return router.json_result(
        {"dataset_id": dataset_id, "name": name, "rows": int(len(frame))}, status=201
    )


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------

PAGE_STYLE = """<style>
.prose{max-width:46rem;line-height:1.65}
.prose h2,.prose h3,.prose h4{margin:1.4em 0 .4em}
.prose pre{overflow:auto;padding:.8rem;border:1px solid var(--border);
border-radius:var(--r-md);background:var(--panel-2)}
.prose code{font-family:ui-monospace,monospace;font-size:.92em}
.cm-gallery{display:grid;gap:var(--s5);grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.cm-note{color:var(--text-muted);font-size:.9rem}
.cm-pager{display:flex;gap:var(--s3);margin-top:var(--s5)}
</style>"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _safe_jsonld(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    return f'<script type="application/ld+json">{text}</script>'


def _table(columns: list[str], rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{_esc(c)}</th>" for c in columns)
    body = ""
    for record in rows:
        cells = ""
        for column in columns:
            value = record.get(column)
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            text = "" if value is None else value
            cells += f'<td class="{"num" if numeric else ""}">{_esc(text)}</td>'
        body += f"<tr>{cells}</tr>"
    return (
        f'<div class="scroll-x"><table class="data"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _not_found_page() -> router.Result:
    body = """
<main id="content"><section class="page-head"><div class="shell">
  <h1>No such dataset</h1>
  <p class="sub">It may be private or unpublished. Browse the
  <a href="/datasets/community">community datasets</a>.</p>
</div></section></main>"""
    return router.html(
        _page("Not found — twoHelixes", "", body, "/datasets/community",
              head='<meta name="robots" content="noindex">'),
        status=404,
    )


@router.get("/datasets/u/{slug}")
def public_page(ctx: router.Context) -> router.Result:
    throttled = _throttled_html(ctx)
    if throttled:
        return throttled
    slug = ctx.params["slug"]
    row = _public_row(slug)
    if row is None:
        return _not_found_page()
    detail = _public_detail(row, PREVIEW_ROWS)
    if detail is None:
        return _not_found_page()

    site = config.site_url().rstrip("/")
    mode = "dark" if (ctx.q("mode", "light") or "light") == "dark" else "light"
    path = f"/datasets/u/{slug}"
    title = detail["title"]
    description = _plain(detail["description"], 300) or f"{title} - a community dataset."
    license_label, license_url = LICENSES.get(
        detail["license"], LICENSES["all-rights-reserved"]
    )

    tags = "".join(
        f'<a class="chip" href="/datasets/community?tag={_esc(quote(t))}">{_esc(t)}</a>'
        for t in detail["tags"]
    )

    links = _links(row["id"], public_only=True)
    gallery = ""
    for link in links:
        caption = f"<figcaption>{_esc(link['caption'] or link['title'])}</figcaption>"
        if link["kind"] == "chart":
            markup = _chart_svg(link["_spec"], mode)
            if markup:
                gallery += (
                    f'<figure class="figure"><h3>{_esc(link["title"])}</h3>'
                    f"{markup}{caption}</figure>"
                )
        else:
            gallery += (
                f'<div class="figure"><h3>{_esc(link["title"])}</h3>'
                f'<p>{_esc(link["caption"])}</p>'
                f'<a class="btn btn-ghost btn-small" href="{_esc(link["share_url"])}" '
                f'rel="nofollow">Open dashboard</a></div>'
            )
    gallery_section = (
        f'<section class="band"><div class="shell"><h2>Analyses</h2>'
        f'<div class="cm-gallery">{gallery}</div></div></section>'
        if gallery else ""
    )

    columns = [c["name"] for c in detail["schema"]]
    schema_rows = "".join(
        f'<tr><td>{_esc(c["name"])}</td><td>{_esc(c["type"])}</td>'
        f'<td>{_esc(c["example"])}</td></tr>'
        for c in detail["schema"]
    )
    actions = ""
    if detail["download_url"]:
        actions += (
            f'<a class="btn btn-primary" href="{_esc(detail["download_url"])}" '
            f'rel="nofollow">Download CSV</a>'
            f'<a class="btn btn-ghost" href="/app?community={_esc(quote(slug))}" '
            f'data-action="signin">Analyse this dataset</a>'
        )
    cover = ""
    if row.get("cover_chart_id"):
        for link in links:
            if link["kind"] == "chart" and link["id"] == row["cover_chart_id"]:
                cover = f'<figure class="figure">{_chart_svg(link["_spec"], mode)}</figure>'

    meta_license = (
        f'<a href="{_esc(license_url)}" rel="nofollow noopener">{_esc(license_label)}</a>'
        if license_url else _esc(license_label)
    )
    published = time.strftime("%Y-%m-%d", time.gmtime(detail["published_at"] or 0))

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker"><a href="/datasets/community">Community datasets</a></p>
  <h1>{_esc(title)}</h1>
  <p class="cm-note">By {_esc(detail["author"])} &middot; published {published}
    &middot; License: {meta_license}</p>
  <div class="ds-meta"><span>{detail["row_count"]:,} rows</span>
    <span>{len(columns)} columns</span></div>
  <div class="chips" style="margin-top:var(--s3)">{tags}</div>
  <div class="actions" style="margin-top:var(--s4)">{actions}</div>
</div></section>
<section><div class="shell">
  <div class="prose">{render_markdown(detail["description"])}</div>
  {cover}
</div></section>
{gallery_section}
<section class="band"><div class="shell">
  <h2>Schema</h2>
  <div class="scroll-x"><table class="data"><thead><tr><th>Column</th>
    <th>Type</th><th>Example</th></tr></thead><tbody>{schema_rows}</tbody></table></div>
</div></section>
<section><div class="shell">
  <h2>First {detail["preview_rows"]} rows</h2>
  <p class="cm-note">Published by its owner; everything shown is public.</p>
  {_table(columns, detail["preview"])}
</div></section>
</main>"""

    structured: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Dataset",
        "name": title,
        "description": description,
        "url": f"{site}{path}",
        "identifier": slug,
        "isAccessibleForFree": True,
        "creator": {"@type": "Person", "name": detail["author"]},
        "keywords": detail["tags"],
        "variableMeasured": columns,
        "datePublished": published,
    }
    if license_url:
        structured["license"] = license_url
    if detail["download_url"]:
        structured["distribution"] = [
            {
                "@type": "DataDownload",
                "encodingFormat": "text/csv",
                "contentUrl": detail["download_url"],
            }
        ]
    return router.html(
        _page(
            f"{title} - community dataset - twoHelixes",
            description,
            body,
            path,
            head=PAGE_STYLE + _safe_jsonld(structured),
        )
    )


@router.get("/datasets/community")
def community_page(ctx: router.Context) -> router.Result:
    throttled = _throttled_html(ctx)
    if throttled:
        return throttled
    rows, total, page, query, tag = _listing_query(ctx)
    entries = [_card_entry(row) for row in rows]

    cards = ""
    for entry in entries:
        chips = "".join(
            f'<a class="chip" href="/datasets/community?tag={_esc(quote(t))}">{_esc(t)}</a>'
            for t in entry["tags"]
        )
        cards += f"""
<div class="ds-card">
  <h3><a href="{_esc(entry["path"])}">{_esc(entry["title"])}</a></h3>
  <p>{_esc(entry["summary"])}</p>
  <div class="ds-meta"><span>{entry["row_count"]:,} rows</span>
    <span>{entry["column_count"]} columns</span>
    <span>{_esc(entry["license_label"])}</span>
    <span>by {_esc(entry["author"])}</span></div>
  <div class="chips">{chips}</div>
</div>"""

    def page_link(number: int, label: str) -> str:
        params = {k: v for k, v in (("q", query), ("tag", tag), ("page", number)) if v}
        return f'<a class="btn btn-ghost btn-small" href="/datasets/community?{urlencode(params)}">{label}</a>'

    pages = max(1, -(-total // PAGE_SIZE))
    pager = ""
    if page > 1:
        pager += page_link(page - 1, "Previous")
    if page < pages:
        pager += page_link(page + 1, "Next")
    empty = '<p class="cm-note">No community datasets matched.</p>' if not entries else ""
    tag_note = f' tagged <b>{_esc(tag)}</b> (<a href="/datasets/community">clear</a>)' if tag else ""

    body = f"""
<main id="content">
<section class="page-head"><div class="shell">
  <p class="kicker"><a href="/datasets">Datasets</a></p>
  <h1>Community datasets</h1>
  <p class="sub">Datasets published by their owners, each with a write-up and
  the analyses built on it.</p>
</div></section>
<section><div class="shell">
  <form class="dataset-search" method="get" action="/datasets/community" role="search">
    <label for="cm-q">Find a dataset</label>
    <div><input id="cm-q" name="q" type="search" value="{_esc(query)}"
      placeholder="Search titles, descriptions and tags" autocomplete="off">
      <button class="btn btn-primary btn-small" type="submit">Search</button></div>
  </form>
  <p class="cm-note">{total} dataset{"" if total == 1 else "s"}{tag_note}.</p>
  <div class="ds-grid">{cards}</div>
  {empty}
  <div class="cm-pager">{pager}</div>
</div></section>
</main>"""

    robots = "" if page == 1 and not query and not tag else '<meta name="robots" content="noindex,follow">'
    return router.html(
        _page(
            "Community datasets - twoHelixes",
            "Datasets published by the community, with write-ups and linked analyses.",
            body,
            "/datasets/community",
            head=PAGE_STYLE + robots,
        )
    )


def sitemap_entries() -> list[tuple[str, str, str]]:
    try:
        rows = store.query(
            "SELECT public_slug FROM datasets WHERE is_public = 1 "
            "AND public_slug IS NOT NULL ORDER BY published_at DESC LIMIT ?",
            (MAX_SITEMAP,),
        )
    except Exception:  # noqa: BLE001 - a sitemap without these beats no sitemap
        log.exception("community sitemap entries unavailable")
        return []
    return [
        (f"/datasets/u/{quote(str(row['public_slug']))}", "weekly", "0.5") for row in rows
    ]
