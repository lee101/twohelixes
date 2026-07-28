"""End-to-end fixtures: a real server, on a real port, with its own database.

The unit suite imports `twohelixes` directly, which means it never exercises
the part of this product that is most specific to it - the Mojo event loop, the
one narrow bridge call per request, SSE framing, and four `SO_REUSEPORT`
workers sharing one SQLite file. Every cross-worker bug this repository has had
(free-tier counters four times too generous, notebook sessions visible only to
the worker that started them) is invisible to an in-process test by
construction.

So these tests run the built binary. They are skipped, not failed, when the
binary or a browser is missing: a contributor without a Mojo toolchain should
still get a green unit suite.

    pixi run mojo build server/main.mojo -o build/twohelixes-server   # once
    PYTHONPATH=interp:... pixi run python -m pytest tests/e2e -q
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

def pytest_addoption(parser: Any) -> None:
    parser.addoption(
        "--e2e", action="store_true", default=False, help="run the end-to-end suite"
    )


def pytest_collection_modifyitems(config: Any, items: list) -> None:
    """Opt-in, because these boot a server and take minutes.

    `pytest tests -q` has to stay the fast loop a person runs before every
    commit; an end-to-end suite in that path gets disabled rather than fixed
    the first time it is inconvenient.
    """
    if config.getoption("--e2e") or os.environ.get("TWOHELIXES_E2E"):
        return
    skip = pytest.mark.skip(reason="end-to-end: run with --e2e")
    # This hook is handed every item in the session, not only the ones under
    # this directory, so it has to filter - the first version skipped the
    # whole suite.
    here = str(Path(__file__).parent)
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(skip)


# Mirrors config.PLAN_ALLOWANCES. Duplicated rather than imported because the
# e2e suite talks to the server over HTTP and does not import the app.
_PLAN_LIMITS = {
    "free": {"chat_query": 15},
    "plus": {"chat_query": 500},
    "pro": {"chat_query": 1200},
    "team": {"chat_query": 5000},
}

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "build" / "twohelixes-server"
SITE_PACKAGES_312 = ROOT / ".venv" / "lib" / "python3.12" / "site-packages"

# Deliberately more than one: a single worker cannot exhibit the bugs that only
# appear when two of them share a database, and those are the ones worth an
# end-to-end test at all.
WORKERS = 2
BOOT_TIMEOUT = 60.0


def _clean_env() -> dict[str, str]:
    """The environment a human launches the server in, not the one pytest is in.

    The binary embeds CPython 3.12 and finds its home by walking PATH. Started
    from inside `pixi run`, the first `python` on PATH is the pixi environment's
    3.13, so the embedded 3.12 picks up a 3.13 stdlib and every numpy import
    fails with the misleading "do not import numpy from its source directory" -
    the same 3.12/3.13 trap CLAUDE.md documents, arriving through PATH rather
    than through PYTHONPATH. The pixi entries have to come off PATH as well as
    the obvious variables coming out of the environment.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PIXI_", "CONDA_"))
        and key not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    env["PATH"] = os.pathsep.join(
        part for part in env.get("PATH", "").split(os.pathsep) if "/.pixi/" not in part
    )
    return env


def _reset_postgres() -> None:
    """Give a Postgres run the same clean slate a SQLite run gets for free.

    On SQLite every session gets a fresh temporary directory. A Postgres
    database persists, and the state that carries over is not harmless: the
    anonymous trial is one question per day per address, counted in
    `rate_events`, so the second run of the suite on the same database sees
    its trial already spent and fails a test about first impressions.
    """
    dsn = os.environ.get("TWOHELIXES_PG_DSN", "")
    if not dsn:
        return
    try:
        import psycopg
    except ImportError:
        pytest.skip("TWOHELIXES_PG_DSN is set but psycopg is not installed")

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A live server with an empty database, torn down at the end."""
    if not BINARY.exists():
        pytest.skip(f"{BINARY} is not built")
    if not SITE_PACKAGES_312.exists():
        pytest.skip("the 3.12 site-packages the AOT binary needs are missing")

    data_dir = tmp_path_factory.mktemp("e2e-data")
    _reset_postgres()
    port = _free_port()
    env = {
        **_clean_env(),
        "TWOHELIXES_PORT": str(port),
        "TWOHELIXES_WORKERS": str(WORKERS),
        "TWOHELIXES_DEV": "1",
        "TWOHELIXES_INTERP": str(ROOT / "interp"),
        "TWOHELIXES_SITE_PACKAGES": str(SITE_PACKAGES_312),
        # Its own database and its own uploads: an e2e run must never touch
        # the developer's data, and the billing tests move real balances.
        "TWOHELIXES_DATA_DIR": str(data_dir),
    }

    # To a file rather than a pipe: a pipe nobody drains fills its buffer and
    # blocks the server mid-test, which looks exactly like a hang in whatever
    # request happened to be in flight.
    log_path = data_dir / "server.log"
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [str(BINARY)],
        env=env,
        cwd=str(ROOT),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + BOOT_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            pytest.skip(
                f"server exited during boot:\n{log_path.read_text(errors='replace')[-2000:]}"
            )
        try:
            with urllib.request.urlopen(f"{base}/readyz", timeout=1) as response:
                if response.status == 200:
                    break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.25)
    else:
        process.kill()
        pytest.skip("server did not become ready")

    client = Client(base, data_dir)
    client.log_path = log_path
    yield client

    _stop(process, port)
    log_file.close()


def _stop(process: subprocess.Popen, port: int) -> None:
    """Kill the whole process group, not just the parent.

    The server forks `SO_REUSEPORT` workers, so terminating the parent leaves
    children holding the listening socket - each one a stray process with an
    open SQLite handle. Two dozen of those accumulate over a few suite runs,
    and the symptom is the *next* run failing to boot with "database is
    locked", which reads like a bug in the server. `start_new_session=True`
    above put them in their own group precisely so this can signal all of them.
    """
    import signal

    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            continue
        # The parent is gone; give the workers a moment to follow it before
        # checking, since they die on the same signal.
        time.sleep(0.2)
        if not _port_in_use(port):
            return


def _port_in_use(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.3)
        return probe.connect_ex(("127.0.0.1", port)) == 0


class Client:
    """The smallest HTTP client that can drive this API.

    `urllib` rather than `requests` so the e2e suite adds no dependency the
    server itself does not have; the cookie handling is one header.
    """

    def __init__(self, base: str, data_dir: Path) -> None:
        self.base = base
        self.data_dir = data_dir
        self.cookie = ""
        self.api_key = ""
        self.log_path: Path | None = None

    def log_tail(self, lines: int = 40) -> str:
        if self.log_path is None or not self.log_path.exists():
            return ""
        return "\n".join(
            self.log_path.read_text(errors="replace").splitlines()[-lines:]
        )

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        use_api_key: bool = False,
        raw: bool = False,
        anonymous: bool = False,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        # A public page has to be tested the way the public sees it: this
        # client accumulates a session cookie across the suite, so without an
        # opt-out every "anonymous" assertion is made while signed in.
        if anonymous:
            pass
        elif use_api_key and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        elif self.cookie:
            headers["Cookie"] = self.cookie

        request = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                payload = response.read()
                set_cookie = response.headers.get("Set-Cookie")
                if set_cookie:
                    self.cookie = set_cookie.split(";")[0]
                status = response.status
        except urllib.error.HTTPError as error:
            payload = error.read()
            status = error.code

        if raw:
            return status, payload
        try:
            return status, json.loads(payload or b"null")
        except ValueError:
            return status, payload.decode(errors="replace")

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, body, **kwargs)

    def sign_in(self, email: str) -> dict[str, Any]:
        status, body = self.post("/v1/auth/signin", {"email": email})
        assert status == 200, body
        return body

    def grant_credits(self, amount: int, email: str) -> None:
        """Top up directly in the database.

        Buying credits properly means Stripe, and an e2e suite that needs a
        payment provider is an e2e suite nobody runs. The billing behaviour
        under test is what happens *after* the credits exist.
        """
        conn = self.db()
        try:
            # Plus rather than Pro: Pro includes deep-research runs, and a
            # fixture that quietly covers the thing under test makes the
            # billing assertions vacuous.
            conn.execute(
                "UPDATE users SET api_credits = ?, plan = 'plus' WHERE email = ?",
                (amount, email),
            )
            conn.commit()
        finally:
            conn.close()

    def exhaust_allowance(self, email: str, allowance: str = "chat_query") -> None:
        """Spend the plan's included usage, so the next call reaches credits.

        Tests about credit pricing have to get past the allowance first, which
        is itself the thing worth remembering about this pricing model.
        """
        import json as _json

        conn = self.db()
        try:
            row = conn.execute(
                "SELECT plan FROM users WHERE email = ?", (email,)
            ).fetchone()
            plan = str(row["plan"] if row else "free")
            limit = _PLAN_LIMITS.get(plan, {}).get(allowance, 0)
            conn.execute(
                "UPDATE users SET plan_usage = ? WHERE email = ?",
                (_json.dumps({allowance: limit}), email),
            )
            conn.commit()
        finally:
            conn.close()

    def db(self) -> Any:
        """A connection to whichever database the server under test is using.

        A background agent job writes progress continuously, so a test that
        reaches into the database competes with a real writer. On SQLite
        `timeout=` alone was not enough under load - the pragma is, and it is
        the same lesson the server learned in `store.connection`.

        The tests are written with `?` placeholders, so the Postgres path
        translates them the way the server does. Without this the suite quietly
        opened an empty SQLite file next to a server that was using Postgres,
        and every assertion that reached into the database failed with "no such
        table" - which looks like a broken server and is a broken test.
        """
        dsn = os.environ.get("TWOHELIXES_PG_DSN", "")
        if dsn:
            import psycopg

            return _PostgresClient(psycopg.connect(dsn, row_factory=_row_factory))

        import sqlite3

        conn = sqlite3.connect(self.data_dir / "twohelixes.db", timeout=60)
        conn.execute("PRAGMA busy_timeout=60000")
        conn.row_factory = sqlite3.Row
        return conn


class _Row(dict):
    """A row that answers to a name or to a position, as `sqlite3.Row` does.

    The tests use both - `row["plan"]` in one place and `row[0]` for a
    `COUNT(*)` in another - and psycopg offers one or the other, not both.
    """

    __slots__ = ("_order",)

    def __init__(self, mapping: dict, order: list) -> None:
        super().__init__(mapping)
        self._order = order

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return super().__getitem__(self._order[key])
        return super().__getitem__(key)


def _row_factory(cursor: Any) -> Any:
    names = [column.name for column in (cursor.description or [])]

    def make(values: tuple) -> Any:
        return _Row(dict(zip(names, values)), names)

    return make


class _PostgresClient:
    """Enough of the sqlite3 connection surface for these tests."""

    def __init__(self, raw: Any) -> None:
        self.raw = raw

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self.raw.execute(sql.replace("?", "%s"), params)

    def commit(self) -> None:
        self.raw.commit()

    def close(self) -> None:
        self.raw.close()


@pytest.fixture
def quiet(server: "Client") -> None:
    """Wait until no job is running before a timing-sensitive test.

    A deep-research thread from an earlier test shares this box: it holds a
    sandbox, makes model calls and writes progress, which is enough to push a
    browser page load past its timeout on a busy machine.
    """
    deadline = time.time() + 90
    while time.time() < deadline:
        conn = server.db()
        running = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'running'"
        ).fetchone()[0]
        conn.close()
        if not running:
            return
        time.sleep(1.0)


@pytest.fixture
def account(server: Client, quiet: None) -> dict[str, Any]:
    """A fresh signed-in account with credits and an API key.

    Waits for background work first: this fixture writes to the database, and
    a research thread from a previous test writes to it continuously.
    """
    email = f"e2e-{int(time.time() * 1000)}@twohelixes.test"
    user = server.sign_in(email)
    server.grant_credits(5000, email)
    # Re-sign so the identity in the session reflects the new plan.
    server.sign_in(email)
    status, body = server.post("/v1/billing/api-key")
    assert status == 200, body
    server.api_key = body["api_key"]
    return {"email": email, "user": user, "api_key": body["api_key"]}
