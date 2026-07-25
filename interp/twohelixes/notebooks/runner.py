"""Hosted marimo sessions.

A notebook session is a long-lived process, which makes it the opposite of
every other request this server handles. Three consequences shape the design:

* **It runs out-of-process.** `marimo edit` is its own ASGI server; we spawn
  it on a private port and proxy to it. It never shares the worker's embedded
  interpreter, so a runaway notebook cannot take the site down.
* **It is billed per minute, not per request**, because it holds memory for as
  long as it is open. Idle sessions are reaped.
* **It needs somewhere to run.** Locally that is this box; at scale it is a
  remote instance. `compute.py` provides that, and the two are deliberately
  the same interface so the local path is not a toy.

Notebooks execute arbitrary Python by definition. Locally that means a
separate process and a token-guarded port; multi-tenant hosting needs the
remote path, where each session gets its own instance.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from twohelixes import config

log = logging.getLogger("twohelixes.notebooks.runner")

# Ports handed to local sessions. Small on purpose: this box is for
# development and a handful of sessions, not a fleet.
PORT_RANGE = range(7600, 7640)

IDLE_TIMEOUT = 30 * 60
MAX_SESSIONS_PER_USER = 3
STARTUP_TIMEOUT = 30

_sessions: dict[str, "Session"] = {}
_lock = threading.Lock()
_reaper_started = False


class RunnerError(Exception):
    pass


@dataclass
class Session:
    id: str
    user_id: str
    title: str
    port: int
    token: str
    directory: str
    process: Any = None
    started_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    provider: str = "local"
    remote: dict[str, Any] = field(default_factory=dict)

    def alive(self) -> bool:
        if self.provider != "local":
            return bool(self.remote.get("running"))
        return self.process is not None and self.process.poll() is None

    def url(self) -> str:
        if self.provider != "local":
            return str(self.remote.get("url", ""))
        return f"http://127.0.0.1:{self.port}/?access_token={self.token}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "provider": self.provider,
            "running": self.alive(),
            "started_at": self.started_at,
            "idle_seconds": int(time.time() - self.last_seen),
            "url": self.url(),
            "credits_per_minute": config.CREDIT_COST.get("notebook_minute", 2),
        }


def marimo_binary() -> str:
    """The marimo entry point.

    Its own virtualenv, deliberately: the notebook environment should be able
    to move without dragging the server's dependencies along.
    """
    explicit = config.get("TWOHELIXES_MARIMO_BIN")
    if explicit and Path(explicit).exists():
        return explicit

    local = config.REPO_ROOT / ".venv-nb" / "bin" / "marimo"
    if local.exists():
        return str(local)

    found = shutil.which("marimo")
    if found:
        return found
    raise RunnerError(
        "marimo is not installed; create .venv-nb or set TWOHELIXES_MARIMO_BIN"
    )


def _free_port() -> int:
    used = {s.port for s in _sessions.values() if s.alive()}
    for port in PORT_RANGE:
        if port in used:
            continue
        # Confirm nothing else holds it: another service on this box would
        # otherwise silently receive the session's traffic.
        import socket

        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RunnerError("no free notebook port on this host")


def session_dir(user_id: str, session_id: str) -> Path:
    path = config.data_dir() / "notebooks" / user_id / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def start(user_id: str, source: str, title: str = "Notebook") -> dict[str, Any]:
    """Spawn a notebook session for `user_id`."""
    _ensure_reaper()

    with _lock:
        mine = [s for s in _sessions.values() if s.user_id == user_id and s.alive()]
        if len(mine) >= MAX_SESSIONS_PER_USER:
            raise RunnerError(
                f"you already have {len(mine)} notebooks open; close one first"
            )

    session_id = secrets.token_hex(8)
    directory = session_dir(user_id, session_id)
    notebook = directory / "notebook.py"
    notebook.write_text(source)

    provider = config.get("TWOHELIXES_NOTEBOOK_PROVIDER", "local") or "local"
    if provider != "local":
        from twohelixes.notebooks import compute

        remote = compute.start_remote(provider, user_id, session_id, source)
        session = Session(
            id=session_id,
            user_id=user_id,
            title=title,
            port=0,
            token=remote.get("token", ""),
            directory=str(directory),
            provider=provider,
            remote=remote,
        )
        with _lock:
            _sessions[session_id] = session
        return session.to_dict()

    port = _free_port()
    token = secrets.token_urlsafe(24)

    # `--headless` because nothing here can open a browser, and a token so the
    # port is not an open door on a shared box.
    command = [
        marimo_binary(), "edit", str(notebook),
        "--headless",
        "--host", "127.0.0.1",
        "--port", str(port),
        "--token-password", token,
        "--no-sandbox",
    ]

    # Strip the server's interpreter paths. marimo runs on its own venv, and
    # inheriting PYTHONPATH puts this process's site-packages on a different
    # Python's path - the exact numpy "do not import from its source
    # directory" failure that the 3.12/3.13 split already caused once.
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
    }
    env["MARIMO_SKIP_UPDATE_CHECK"] = "1"

    try:
        process = subprocess.Popen(
            command,
            cwd=str(directory),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,  # its own process group, so stop() is clean
        )
    except OSError as exc:
        raise RunnerError(f"could not start marimo: {exc}") from exc

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = (process.stderr.read() or b"").decode()[-400:] if process.stderr else ""
            raise RunnerError(f"marimo exited immediately: {stderr}")
        if _port_open(port):
            break
        time.sleep(0.25)
    else:
        process.kill()
        raise RunnerError("marimo did not start listening in time")

    session = Session(
        id=session_id,
        user_id=user_id,
        title=title,
        port=port,
        token=token,
        directory=str(directory),
        process=process,
    )
    with _lock:
        _sessions[session_id] = session

    log.info("notebook %s started for %s on :%d", session_id, user_id, port)
    return session.to_dict()


def _port_open(port: int) -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def touch(session_id: str) -> None:
    """Mark a session as in use, so the reaper leaves it alone."""
    with _lock:
        session = _sessions.get(session_id)
    if session:
        session.last_seen = time.time()


def stop(user_id: str, session_id: str) -> bool:
    with _lock:
        session = _sessions.get(session_id)
        if session is None or session.user_id != user_id:
            return False
        _sessions.pop(session_id, None)

    _terminate(session)
    return True


def _terminate(session: Session) -> None:
    if session.provider != "local":
        try:
            from twohelixes.notebooks import compute

            compute.stop_remote(session.provider, session.remote)
        except Exception:  # noqa: BLE001
            log.exception("could not stop remote notebook %s", session.id)
        return

    process = session.process
    if process is None or process.poll() is not None:
        return
    try:
        # The whole group: marimo spawns kernel subprocesses of its own.
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    log.info("notebook %s stopped", session.id)


def list_for_user(user_id: str) -> list[dict[str, Any]]:
    with _lock:
        mine = [s for s in _sessions.values() if s.user_id == user_id]
    return [s.to_dict() for s in mine]


def active_count() -> int:
    with _lock:
        return sum(1 for s in _sessions.values() if s.alive())


# --------------------------------------------------------------------------
# Reaping and billing
# --------------------------------------------------------------------------


def _ensure_reaper() -> None:
    global _reaper_started
    with _lock:
        if _reaper_started:
            return
        _reaper_started = True
    thread = threading.Thread(target=_reap_loop, name="notebook-reaper", daemon=True)
    thread.start()


def _reap_loop() -> None:
    """Charge for running sessions and close idle ones.

    Billing happens here rather than at start, because the cost of a notebook
    is the time it stays open. A user who closes the tab stops paying within a
    minute.
    """
    from twohelixes import credits

    while True:
        time.sleep(60)
        try:
            with _lock:
                sessions = list(_sessions.values())

            for session in sessions:
                if not session.alive():
                    with _lock:
                        _sessions.pop(session.id, None)
                    continue

                if time.time() - session.last_seen > IDLE_TIMEOUT:
                    log.info("reaping idle notebook %s", session.id)
                    with _lock:
                        _sessions.pop(session.id, None)
                    _terminate(session)
                    continue

                cost = config.CREDIT_COST.get("notebook_minute", 2)
                try:
                    credits.deduct(session.user_id, cost, reason="notebook_minute",
                                   ref=session.id)
                except Exception:  # noqa: BLE001
                    # Out of credits: stop the session rather than run it free.
                    log.info("stopping notebook %s: cannot charge", session.id)
                    with _lock:
                        _sessions.pop(session.id, None)
                    _terminate(session)
        except Exception:  # noqa: BLE001 - the reaper must never die
            log.exception("notebook reaper iteration failed")


def shutdown_all() -> None:
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        _terminate(session)
