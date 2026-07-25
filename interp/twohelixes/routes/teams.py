"""Teams and sharing.

Two separate ideas that are easy to conflate:

* **A team** is a durable group with members and roles. Work owned by a team
  is visible to every member.
* **A share link** is a capability: whoever holds the token can see one thing,
  with no account. Revocable, optionally expiring, and never granting write.

Both are enforced in one place - `can_read` / `can_write` - because an access
check scattered across handlers is how a leak happens. Every route that
touches someone else's object goes through them.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from twohelixes import auth, config, router, store

log = logging.getLogger("twohelixes.routes.teams")

ROLES = ("owner", "admin", "member", "viewer")
# Rank matters: a check is "at least this role", so the order is the policy.
ROLE_RANK = {role: index for index, role in enumerate(reversed(ROLES))}

SHAREABLE = ("dashboard", "chart", "query")
MAX_TEAMS_PER_USER = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'free',
    credits     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS team_members (
    team_id     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'member',
    added_at    REAL NOT NULL,
    PRIMARY KEY (team_id, user_id)
);
CREATE INDEX IF NOT EXISTS team_members_user ON team_members(user_id);

CREATE TABLE IF NOT EXISTS team_invites (
    token       TEXT PRIMARY KEY,
    team_id     TEXT NOT NULL,
    email       TEXT,
    role        TEXT NOT NULL DEFAULT 'member',
    invited_by  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    accepted_by TEXT
);

CREATE TABLE IF NOT EXISTS shares (
    token       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    owner_id    TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    views       INTEGER NOT NULL DEFAULT 0,
    label       TEXT
);
CREATE INDEX IF NOT EXISTS shares_object ON shares(kind, object_id);
CREATE INDEX IF NOT EXISTS shares_owner ON shares(owner_id);

-- Team ownership of an object is a separate row rather than a column on
-- every table, so adding a shareable type does not need a migration.
CREATE TABLE IF NOT EXISTS team_objects (
    team_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    object_id   TEXT NOT NULL,
    added_at    REAL NOT NULL,
    PRIMARY KEY (kind, object_id)
);
CREATE INDEX IF NOT EXISTS team_objects_team ON team_objects(team_id);
"""

_ready = False


def ensure_schema() -> None:
    global _ready
    if _ready:
        return
    store.connection().executescript(SCHEMA)
    _ready = True


# --------------------------------------------------------------------------
# Access control - the only place that decides
# --------------------------------------------------------------------------

OWNER_COLUMN = {"dashboard": "dashboards", "chart": "charts", "query": "saved_queries"}


def _owner_of(kind: str, object_id: str) -> str | None:
    table = OWNER_COLUMN.get(kind)
    if not table:
        return None
    row = store.one(f"SELECT user_id FROM {table} WHERE id = ?", (object_id,))
    return str(row["user_id"]) if row else None


def team_of(kind: str, object_id: str) -> str | None:
    ensure_schema()
    row = store.one(
        "SELECT team_id FROM team_objects WHERE kind = ? AND object_id = ?",
        (kind, object_id),
    )
    return str(row["team_id"]) if row else None


def role_in(team_id: str, user_id: str) -> str | None:
    ensure_schema()
    row = store.one(
        "SELECT role FROM team_members WHERE team_id = ? AND user_id = ?",
        (team_id, user_id),
    )
    return str(row["role"]) if row else None


def can_read(user_id: str | None, kind: str, object_id: str, token: str = "") -> bool:
    """Read access: owner, team member, or a live share token."""
    ensure_schema()

    if user_id and _owner_of(kind, object_id) == user_id:
        return True

    team_id = team_of(kind, object_id)
    if user_id and team_id and role_in(team_id, user_id):
        return True

    if token:
        share = resolve_share(token)
        if share and share["kind"] == kind and share["object_id"] == object_id:
            return True

    return False


def can_write(user_id: str | None, kind: str, object_id: str) -> bool:
    """Write access. A share token never grants it, by design."""
    ensure_schema()
    if not user_id:
        return False
    if _owner_of(kind, object_id) == user_id:
        return True

    team_id = team_of(kind, object_id)
    if team_id:
        role = role_in(team_id, user_id)
        # A viewer is exactly that: membership alone is not write access.
        return bool(role and ROLE_RANK.get(role, 0) >= ROLE_RANK["member"])
    return False


def resolve_share(token: str) -> dict[str, Any] | None:
    """A share row, if the token is live. Expiry and revocation checked here."""
    ensure_schema()
    row = store.one("SELECT * FROM shares WHERE token = ?", (token,))
    if row is None:
        return None
    share = store.row_to_dict(row) or {}
    if share.get("revoked"):
        return None
    expires = share.get("expires_at")
    if expires and float(expires) < time.time():
        return None
    return share


# --------------------------------------------------------------------------
# Teams
# --------------------------------------------------------------------------


@router.get("/v1/teams")
def list_teams(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    ensure_schema()
    rows = store.query(
        "SELECT t.id, t.name, t.owner_id, t.created_at, m.role "
        "FROM teams t JOIN team_members m ON m.team_id = t.id "
        "WHERE m.user_id = ? ORDER BY t.created_at",
        (identity.user_id,),
    )
    teams = store.rows_to_dicts(rows)
    for team in teams:
        count = store.one(
            "SELECT COUNT(*) AS n FROM team_members WHERE team_id = ?", (team["id"],)
        )
        team["members"] = int(count["n"]) if count else 0
    return router.json_result({"teams": teams})


@router.post("/v1/teams")
def create_team(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    ensure_schema()

    name = str(ctx.field("name") or "").strip()
    if not name:
        return router.error(400, "name_required")

    mine = store.one(
        "SELECT COUNT(*) AS n FROM team_members WHERE user_id = ?", (identity.user_id,)
    )
    if mine and int(mine["n"]) >= MAX_TEAMS_PER_USER:
        return router.error(400, "too_many_teams")

    team_id = store.new_id()
    now = time.time()
    store.execute(
        "INSERT INTO teams (id, name, owner_id, created_at) VALUES (?, ?, ?, ?)",
        (team_id, name[:120], identity.user_id, now),
    )
    store.execute(
        "INSERT INTO team_members (team_id, user_id, role, added_at) VALUES (?, ?, 'owner', ?)",
        (team_id, identity.user_id, now),
    )
    return router.json_result({"id": team_id, "name": name, "role": "owner"}, status=201)


@router.get("/v1/teams/{team_id}/members")
def list_members(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    team_id = ctx.params["team_id"]
    if not role_in(team_id, identity.user_id):
        return router.error(403, "not_a_member")

    rows = store.query(
        "SELECT m.user_id, m.role, m.added_at, u.email, u.display_name "
        "FROM team_members m LEFT JOIN users u ON u.id = m.user_id "
        "WHERE m.team_id = ? ORDER BY m.added_at",
        (team_id,),
    )
    return router.json_result({"members": store.rows_to_dicts(rows)})


@router.post("/v1/teams/{team_id}/invite")
def invite(ctx: router.Context) -> router.Result:
    """Create an invite link. Admins and owners only."""
    identity = auth.require(ctx)
    team_id = ctx.params["team_id"]
    role = role_in(team_id, identity.user_id)
    if not role or ROLE_RANK.get(role, 0) < ROLE_RANK["admin"]:
        return router.error(403, "admin_required")

    invited_role = str(ctx.field("role") or "member")
    if invited_role not in ROLES or invited_role == "owner":
        return router.error(400, "invalid_role")

    token = secrets.token_urlsafe(24)
    days = int(ctx.field("expires_days") or 14)
    store.execute(
        "INSERT INTO team_invites (token, team_id, email, role, invited_by, "
        "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            token,
            team_id,
            str(ctx.field("email") or "").strip().lower() or None,
            invited_role,
            identity.user_id,
            time.time(),
            time.time() + days * 86400,
        ),
    )
    return router.json_result(
        {"token": token, "url": f"{config.site_url()}/join/{token}", "role": invited_role},
        status=201,
    )


@router.post("/v1/teams/join/{token}")
def accept_invite(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    ensure_schema()

    row = store.one("SELECT * FROM team_invites WHERE token = ?", (ctx.params["token"],))
    if row is None:
        return router.error(404, "unknown_invite")

    invite_row = store.row_to_dict(row) or {}
    if invite_row.get("accepted_by"):
        return router.error(409, "already_used")
    expires = invite_row.get("expires_at")
    if expires and float(expires) < time.time():
        return router.error(410, "invite_expired")

    # An invite addressed to an email is for that person, not whoever finds
    # the link.
    wanted = (invite_row.get("email") or "").strip().lower()
    if wanted and wanted != (identity.email or "").strip().lower():
        return router.error(403, "invite_is_for_another_email")

    if role_in(invite_row["team_id"], identity.user_id):
        return router.json_result({"joined": True, "already": True})

    store.execute(
        "INSERT INTO team_members (team_id, user_id, role, added_at) VALUES (?, ?, ?, ?)",
        (invite_row["team_id"], identity.user_id, invite_row["role"], time.time()),
    )
    store.execute(
        "UPDATE team_invites SET accepted_by = ? WHERE token = ?",
        (identity.user_id, ctx.params["token"]),
    )
    return router.json_result({"joined": True, "team_id": invite_row["team_id"]})


@router.delete("/v1/teams/{team_id}/members/{user_id}")
def remove_member(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    team_id, target = ctx.params["team_id"], ctx.params["user_id"]

    role = role_in(team_id, identity.user_id)
    # Leaving is always allowed; removing someone else needs admin.
    if target != identity.user_id:
        if not role or ROLE_RANK.get(role, 0) < ROLE_RANK["admin"]:
            return router.error(403, "admin_required")

    team = store.one("SELECT owner_id FROM teams WHERE id = ?", (team_id,))
    if team and team["owner_id"] == target:
        return router.error(400, "cannot_remove_the_owner")

    store.execute(
        "DELETE FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, target)
    )
    return router.json_result({"removed": True})


@router.post("/v1/teams/{team_id}/objects")
def share_with_team(ctx: router.Context) -> router.Result:
    """Hand an object to a team, so every member can see it."""
    identity = auth.require(ctx)
    team_id = ctx.params["team_id"]
    if not role_in(team_id, identity.user_id):
        return router.error(403, "not_a_member")

    kind = str(ctx.field("kind") or "")
    object_id = str(ctx.field("object_id") or "")
    if kind not in SHAREABLE:
        return router.error(400, "unshareable_kind")
    if _owner_of(kind, object_id) != identity.user_id:
        return router.error(403, "not_yours_to_share")

    store.execute(
        "INSERT OR REPLACE INTO team_objects (team_id, kind, object_id, added_at) "
        "VALUES (?, ?, ?, ?)",
        (team_id, kind, object_id, time.time()),
    )
    return router.json_result({"shared": True})


# --------------------------------------------------------------------------
# Share links
# --------------------------------------------------------------------------


@router.post("/v1/shares")
def create_share(ctx: router.Context) -> router.Result:
    """Mint a read-only link."""
    identity = auth.require(ctx)
    ensure_schema()

    kind = str(ctx.field("kind") or "")
    object_id = str(ctx.field("object_id") or "")
    if kind not in SHAREABLE:
        return router.error(400, "unshareable_kind")
    if not can_write(identity.user_id, kind, object_id):
        return router.error(403, "not_yours_to_share")

    expires_days = ctx.field("expires_days")
    expires_at = (
        time.time() + int(expires_days) * 86400
        if expires_days not in (None, "", 0)
        else None
    )

    token = secrets.token_urlsafe(24)
    store.execute(
        "INSERT INTO shares (token, kind, object_id, owner_id, created_at, "
        "expires_at, label) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            token,
            kind,
            object_id,
            identity.user_id,
            time.time(),
            expires_at,
            str(ctx.field("label") or "")[:120] or None,
        ),
    )
    return router.json_result(
        {
            "token": token,
            "url": f"{config.site_url()}/share/{token}",
            "expires_at": expires_at,
        },
        status=201,
    )


@router.get("/v1/shares")
def list_shares(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    ensure_schema()
    rows = store.query(
        "SELECT token, kind, object_id, created_at, expires_at, revoked, views, label "
        "FROM shares WHERE owner_id = ? ORDER BY created_at DESC LIMIT 200",
        (identity.user_id,),
    )
    shares = store.rows_to_dicts(rows)
    for share in shares:
        share["url"] = f"{config.site_url()}/share/{share['token']}"
        share["live"] = not share["revoked"] and (
            not share["expires_at"] or float(share["expires_at"]) > time.time()
        )
    return router.json_result({"shares": shares})


@router.delete("/v1/shares/{token}")
def revoke_share(ctx: router.Context) -> router.Result:
    """Revoke rather than delete, so the row remains as an audit trail."""
    identity = auth.require(ctx)
    ensure_schema()
    row = store.one(
        "SELECT owner_id FROM shares WHERE token = ?", (ctx.params["token"],)
    )
    if row is None:
        return router.error(404, "not_found")
    if row["owner_id"] != identity.user_id:
        return router.error(403, "not_yours")

    store.execute("UPDATE shares SET revoked = 1 WHERE token = ?", (ctx.params["token"],))
    return router.json_result({"revoked": True})


@router.get("/v1/shared/{token}")
def read_share(ctx: router.Context) -> router.Result:
    """Read a shared object. No account needed, read only."""
    share = resolve_share(ctx.params["token"])
    if share is None:
        return router.error(404, "not_found")

    store.execute(
        "UPDATE shares SET views = views + 1 WHERE token = ?", (ctx.params["token"],)
    )

    kind, object_id = share["kind"], share["object_id"]
    table = OWNER_COLUMN[kind]
    row = store.one(f"SELECT * FROM {table} WHERE id = ?", (object_id,))
    if row is None:
        return router.error(404, "object_gone")

    data = store.row_to_dict(row) or {}
    # Never leak who owns it, what it was asked of, or which source it came
    # from: a share link grants the rendered object and nothing else.
    for secret in ("user_id", "source_id", "query", "sql"):
        data.pop(secret, None)
    for field in ("spec", "graph_args", "trace", "layout", "params"):
        if field in data:
            data[field] = store.load_json(data[field], {} if field != "trace" else [])

    if kind == "dashboard":
        charts = store.query(
            "SELECT id, title, spec, graph_args FROM charts WHERE dashboard_id = ?",
            (object_id,),
        )
        tiles = store.rows_to_dicts(charts)
        for tile in tiles:
            tile["spec"] = store.load_json(tile.get("spec"), {})
            tile["graph_args"] = store.load_json(tile.get("graph_args"), {})
        data["charts"] = tiles

    return router.json_result({"kind": kind, "object": data})
