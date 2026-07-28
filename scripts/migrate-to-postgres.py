"""Copy a live SQLite database into Postgres, table by table.

One-way and idempotent-ish: it creates the schema through `store.init()` and
then inserts rows that are not already there, keyed on the primary key. Run it
with the server stopped - it does not try to be correct against a moving
database, and pretending otherwise would be the dangerous kind of convenient.

    ./.venv-13/bin/python scripts/migrate-to-postgres.py \\
        --sqlite var/twohelixes.db \\
        --dsn postgresql://twohelixes:...@127.0.0.1:5432/twohelixes

`--dry-run` reports what it would copy and touches nothing.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "interp"))

# Order matters only for readability here - there are no foreign key
# constraints in the schema, deliberately, because the rows outlive each other
# (a chart survives the dashboard it was on).
TABLES = (
    "users",
    "sessions",
    "data_sources",
    "datasets",
    "dashboards",
    "charts",
    "saved_queries",
    "query_history",
    "credit_ledger",
    "jobs",
    "meters",
    "rate_events",
)

# `rate_events` is a rolling window of counters, `sessions` are cookies that
# will be re-minted on the next sign-in. Copying either is churn, not data.
SKIP_BY_DEFAULT = {"rate_events"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default=str(ROOT / "var" / "twohelixes.db"))
    parser.add_argument("--dsn", default=os.environ.get("TWOHELIXES_PG_DSN", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--include-rate-events", action="store_true",
        help="copy the rate-limit counters too (rarely worth it)",
    )
    args = parser.parse_args()

    if not args.dsn:
        parser.error("--dsn or TWOHELIXES_PG_DSN is required")
    source_path = Path(args.sqlite)
    if not source_path.exists():
        print(f"no SQLite database at {source_path}; nothing to migrate")
        return 0

    os.environ["TWOHELIXES_PG_DSN"] = args.dsn
    from twohelixes import store
    from twohelixes.routes import teams

    store.init()
    store.connection().executescript(teams.SCHEMA)
    target = store.connection()

    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row

    skip = set() if args.include_rate_events else SKIP_BY_DEFAULT
    tables = [t for t in (*TABLES, "teams", "team_members", "team_objects", "shares")
              if t not in skip]

    total = 0
    for table in tables:
        try:
            rows = list(source.execute(f"SELECT * FROM {table}"))
        except sqlite3.OperationalError:
            # A table this database never had - an older deployment, or one of
            # the team tables that only exist once teams are used.
            continue
        if not rows:
            print(f"  {table:16} empty")
            continue

        columns = list(rows[0].keys())
        # ON CONFLICT DO NOTHING so a re-run after a partial migration is safe.
        # Tables without a primary key (rate_events) cannot use it.
        conflict = " ON CONFLICT DO NOTHING" if table != "rate_events" else ""

        if args.dry_run:
            print(f"  {table:16} would copy {len(rows)}")
            total += len(rows)
            continue

        copied = 0
        for row in rows:
            # Columns whose value is NULL are left out of the INSERT entirely,
            # so Postgres applies the column default. This is not cosmetic:
            # SQLite's `ALTER TABLE ADD COLUMN` cannot add a NOT NULL column
            # to a populated table, so columns added after launch
            # (`credit_dust`, `plan_usage`) are NULL in every row that predates
            # them - values the schema says cannot exist. Postgres is right to
            # refuse them; the default is what those rows always meant.
            present = [c for c in columns if row[c] is not None]
            if not present:
                continue
            target.execute(
                f"INSERT INTO {table} ({', '.join(present)}) "
                f"VALUES ({', '.join('?' for _ in present)}){conflict}",
                tuple(row[c] for c in present),
            )
            copied += 1
        print(f"  {table:16} copied {copied}")
        total += copied

    source.close()
    print(f"{'would copy' if args.dry_run else 'copied'} {total} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
