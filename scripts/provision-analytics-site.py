#!/usr/bin/env python3
from __future__ import annotations

import argparse
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "interp"))

from twohelixes import store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision an analytics site for an existing twoHelixes account."
    )
    parser.add_argument("email")
    parser.add_argument("domain")
    parser.add_argument("--name", default="")
    parser.add_argument(
        "--adopt",
        default="",
        metavar="LEGACY_SITE_ID",
        help=(
            "Claim an existing event namespace instead of minting a new id. "
            "Before sites had owners a site_id was just the hostname, so rows "
            "already collected sit under names like 'netwrck.com'. Adopting "
            "that name keeps the history queryable and keeps any snippet "
            "already deployed with it collecting, which a fresh id does not."
        ),
    )
    args = parser.parse_args()

    email = args.email.strip().lower()
    domain = args.domain.strip().lower().removeprefix("www.").strip(".")
    if not email or not domain:
        parser.error("email and domain are required")

    store.init()
    user = store.get_user_by_email(email)
    if user is None:
        print(f"No account exists for {email}. Sign in once before provisioning.", file=sys.stderr)
        return 2

    existing = store.row_to_dict(
        store.one(
            "SELECT id, write_key FROM analytics_sites WHERE user_id = ? AND domain = ?",
            (user["id"], domain),
        )
    )
    if existing:
        print(f"site_id={existing['id']}")
        print(f"write_key={existing['write_key']}")
        return 0

    legacy = args.adopt.strip()
    if legacy:
        taken = store.one("SELECT user_id FROM analytics_sites WHERE id = ?", (legacy,))
        if taken is not None:
            print(f"Site id {legacy} is already claimed.", file=sys.stderr)
            return 2
        events = store.one(
            "SELECT COUNT(*) AS events FROM analytics_events WHERE site_id = ?",
            (legacy,),
        )
        print(f"adopting={legacy} existing_events={store.row_to_dict(events)['events']}")

    site_id = legacy or store.new_id()
    write_key = f"thw_{secrets.token_urlsafe(24)}"
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO analytics_sites"
            " (id, user_id, domain, name, write_key, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                site_id,
                user["id"],
                domain,
                (args.name.strip() or domain)[:120],
                write_key,
                time.time(),
            ),
        )
    print(f"site_id={site_id}")
    print(f"write_key={write_key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
