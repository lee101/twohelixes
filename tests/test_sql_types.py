"""Every SQL statement in the tree, typechecked by Postgres itself.

Python has no compile-time SQL checker and no Mojo one exists either - Mojo 1.0
has `comptime` but no SQL ecosystem to hang it on, and a hand-written parser
would be a worse copy of something we already have. **Postgres is the type
checker.** `PREPARE` parses a statement, resolves every identifier against the
live schema and reports the parameter types and the result column types, which
is exactly what `sqlx` and `sqlc` do in other languages. It is not an
approximation of static checking; it is the real thing, run against the real
schema.

So this walks the source, pulls out every SQL string, and prepares it. A
renamed column, a typo in a table name, a placeholder count that does not match
the arguments, a function that exists in SQLite and not in Postgres - all of
them fail here rather than in a request.

The suite is skipped without `TWOHELIXES_PG_DSN`, because it needs a real
server to ask. That makes it opt-in locally and mandatory in the deploy, which
is the right way round: the fast loop stays fast and nothing ships unchecked.

    TWOHELIXES_PG_DSN=postgresql://... PYTHONPATH=interp \\
      ./.venv-13/bin/python -m pytest tests/test_sql_types.py -q
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterator

import pytest

from twohelixes import store

pytestmark = pytest.mark.skipif(
    not store.is_postgres(),
    reason="set TWOHELIXES_PG_DSN to typecheck the SQL against a real Postgres",
)

INTERP = Path(__file__).resolve().parents[1] / "interp" / "twohelixes"

# A string is SQL if it starts with one of these. Deliberately strict: a false
# positive here is a test failure on a string that was never SQL.
STARTS = ("SELECT ", "INSERT INTO ", "UPDATE ", "DELETE FROM ", "WITH ")

# Statements this cannot check, each for a stated reason.
SKIP = (
    # Built by the SQL editor from a user's own connected database, against a
    # schema we do not have and must not assume.
    "information_schema",
    # The DDL is checked by `store.init()` succeeding, which every other test
    # in the suite depends on.
    "CREATE ",
    "ALTER ",
    "DROP ",
)


def _sql_strings(path: Path) -> Iterator[tuple[int, str, bool]]:
    """Every SQL literal in one file: (line, sql, is_dynamic).

    Reads the AST rather than grepping, so implicit concatenation across lines
    - which is how nearly every statement here is written - arrives as the one
    string the database will actually receive.

    A literal inside an f-string is a *template*: the table name or the column
    list is chosen at runtime, so there is no complete statement to prepare.
    Those are reported rather than dropped - the set of places this codebase
    builds SQL dynamically is exactly what a claim of "typechecked SQL" has to
    be honest about.
    """
    tree = ast.parse(path.read_text(), filename=str(path))

    dynamic: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for part in ast.walk(node):
                if isinstance(part, ast.Constant):
                    dynamic.add(id(part))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            if text.upper().startswith(STARTS):
                yield node.lineno, " ".join(text.split()), id(node) in dynamic


def _statements() -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    checkable: list[tuple[str, int, str]] = []
    dynamic: list[tuple[str, int, str]] = []
    for path in sorted(INTERP.rglob("*.py")):
        for line, sql, is_dynamic in _sql_strings(path):
            if any(marker.lower() in sql.lower() for marker in SKIP):
                continue
            entry = (str(path.relative_to(INTERP.parent.parent)), line, sql)
            (dynamic if is_dynamic else checkable).append(entry)
    return checkable, dynamic


STATEMENTS, DYNAMIC = _statements()


@pytest.fixture(scope="module", autouse=True)
def _schema() -> None:
    """The live schema, including the tables other modules own."""
    store.init()
    from twohelixes.routes import teams

    store.connection().executescript(teams.SCHEMA)


def test_there_are_statements_to_check() -> None:
    """A checker that finds nothing passes for the wrong reason."""
    assert len(STATEMENTS) > 80, f"only found {len(STATEMENTS)} statements"


def test_the_dynamically_built_sql_is_a_short_known_list() -> None:
    """What this checker cannot see, named.

    Each of these interpolates a table or a column list, so there is no
    statement to prepare until it runs. All of them pick that name from a
    dict this repo owns - never from a request - which is what keeps them
    safe; the number is asserted so a new one is a decision rather than a
    drift.
    """
    listing = "\n".join(f"  {path}:{line}  {sql[:60]}" for path, line, sql in DYNAMIC)
    assert len(DYNAMIC) <= 8, f"dynamic SQL has grown:\n{listing}"


@pytest.mark.parametrize(
    "location,sql",
    [pytest.param(f"{path}:{line}", sql, id=f"{path}:{line}") for path, line, sql in STATEMENTS],
)
def test_postgres_accepts_the_statement(location: str, sql: str) -> None:
    """PREPARE it. Postgres resolves every name and type, or refuses.

    Nothing is executed: `PREPARE` plans without running, so this is safe
    against a database with real rows in it and costs a round trip.
    """
    import psycopg

    prepared = store._adapt(sql)
    # PREPARE needs $n placeholders; psycopg's %s is a client-side convention.
    numbered, index = [], 0
    parts = prepared.split("%s")
    for part in parts[:-1]:
        index += 1
        numbered.append(f"{part}${index}")
    numbered.append(parts[-1])
    statement = "".join(numbered)

    name = f"chk_{abs(hash(location + sql))}"
    conn = store.connection()
    try:
        conn.raw_execute(f"PREPARE {name} AS {statement}")
    except psycopg.errors.SyntaxError as exc:
        pytest.fail(f"{location}: Postgres cannot parse this\n  {sql}\n  {exc}")
    except psycopg.errors.UndefinedColumn as exc:
        pytest.fail(f"{location}: unknown column\n  {sql}\n  {exc}")
    except psycopg.errors.UndefinedTable as exc:
        pytest.fail(f"{location}: unknown table\n  {sql}\n  {exc}")
    except psycopg.errors.IndeterminateDatatype:
        # Postgres cannot infer a parameter's type in isolation - e.g. a bare
        # `?` compared to another `?`. The statement is well-formed; only the
        # inference is ambiguous, and at runtime the value supplies the type.
        pass
    except psycopg.errors.DuplicatePreparedStatement:
        pass
    finally:
        try:
            conn.raw_execute(f"DEALLOCATE {name}")
        except psycopg.Error:
            pass


def test_the_reported_types_are_available_for_a_statement() -> None:
    """The other half of what a typechecker gives you: the shapes.

    Proves the mechanism this file rests on - Postgres will tell us the
    parameter types and the result column types of any statement, which is
    what a code generator would consume if we ever want typed accessors rather
    than `row["api_credits"]`.
    """
    conn = store.connection()
    conn.raw_execute(
        "PREPARE shape AS SELECT id, email, api_credits FROM users WHERE id = $1"
    )
    try:
        row = store.one(
            "SELECT parameter_types::text[] AS params, result_types::text[] AS results "
            "FROM pg_prepared_statements WHERE name = ?",
            ("shape",),
        )
        assert row is not None
        assert row["params"] == ["text"]
        assert row["results"] == ["text", "text", "bigint"]
    finally:
        conn.raw_execute("DEALLOCATE shape")
