"""Hosted marimo sessions.

A notebook session is a long-lived process, which makes it the opposite of
every other request this server handles. Three consequences shape the design:

* **It runs out-of-process.** `marimo edit` is its own ASGI server; we spawn
  it on a private port and proxy to it. It never shares the worker's embedded
  interpreter, so a runaway notebook cannot take the site down.
* **It is billed per minute, not per request**, because it holds memory for as
  long as it is open. The meter is a row in SQLite (`metering.py`), not a timer
  in this process: four workers share one view of what is running, and a
  session survives - and keeps being billed for - a restart of the worker that
  happened to start it. Idle sessions are reaped.
* **It needs somewhere to run.** Locally that is this box; at scale it is a
  remote instance. `compute.py` provides that, and the two are deliberately
  the same interface so the local path is not a toy.

Notebooks execute arbitrary Python by definition. Locally that means a
separate process and a token-guarded port; multi-tenant hosting needs the
remote path, where each session gets its own instance.
"""

from __future__ import annotations

import json
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

from twohelixes import config, metering, store

log = logging.getLogger("twohelixes.notebooks.runner")

# Ports handed to local sessions. Small on purpose: this box is for
# development and a handful of sessions, not a fleet.
PORT_RANGE = range(7600, 7640)

IDLE_TIMEOUT = 30 * 60

# A session nobody closes runs until this, whatever its class says, so a
# forgotten tab cannot bill indefinitely. The class may shorten it - a GPU is
# capped tighter than a CPU box because the mistake costs 50x more per hour.
MAX_SESSION_SECONDS = 8 * 60 * 60
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
    meter_id: str = ""
    process: Any = None
    started_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    provider: str = "local"
    machine: str = "local"
    remote: dict[str, Any] = field(default_factory=dict)

    @property
    def credits_per_minute(self) -> int:
        from twohelixes import machines

        try:
            return machines.get(self.machine).credits_per_minute
        except machines.UnknownMachine:
            return int(config.CREDIT_COST.get("notebook_minute", 1))

    @property
    def idle_timeout(self) -> float:
        """Idle grace, from the class: a GPU left open is worth reaping sooner."""
        from twohelixes import machines

        try:
            return machines.get(self.machine).idle_timeout_minutes * 60.0
        except machines.UnknownMachine:
            return float(IDLE_TIMEOUT)

    @property
    def max_seconds(self) -> float:
        from twohelixes import machines

        try:
            return machines.get(self.machine).max_session_minutes * 60.0
        except machines.UnknownMachine:
            return float(MAX_SESSION_SECONDS)

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
            "credits_per_minute": self.credits_per_minute,
            "machine": self.machine,
            "meter_id": self.meter_id,
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


def start(user_id: str, source: str, title: str = "Notebook",
          machine_id: str = "", plan: str = "") -> dict[str, Any]:
    """Spawn a notebook session for `user_id` on the machine class they asked for.

    The class decides three things that used to be one flat constant: which
    provider and size gets provisioned, what a minute costs, and whether the
    plan's included minutes may pay for it.
    """
    from twohelixes import machines

    metering.start_sweeper()
    _ensure_reaper()

    machine = machines.get(machine_id or config.get("TWOHELIXES_NOTEBOOK_MACHINE", "") or None)
    if machine.provider != "local":
        machines.check_allowed(plan, machine)
        machines.start_orphan_reaper()

    # Counted from the meters, not from this worker's dict: with four workers
    # the per-user cap was per-worker, so the real ceiling was four times the
    # intended one - the same mistake the free-tier counters were built to
    # avoid.
    open_meters = [m for m in metering.open_for(user_id) if m["kind"] == "notebook"]
    if len(open_meters) >= MAX_SESSIONS_PER_USER:
        raise RunnerError(
            f"you already have {len(open_meters)} notebooks open; close one first"
        )

    session_id = secrets.token_hex(8)
    directory = session_dir(user_id, session_id)
    notebook = directory / "notebook.py"
    notebook.write_text(source)

    provider = machine.provider
    if provider != "local":
        from twohelixes.notebooks import compute

        meter = _open_meter(user_id, session_id, provider, {"title": title}, machine)
        try:
            remote = compute.start_remote(provider, user_id, session_id, source, machine=machine)
        except Exception:
            # Nothing was provisioned, so nothing is owed.
            metering.close_meter(meter["id"], detail="provisioning failed")
            raise
        store.execute(
            "UPDATE meters SET remote = ? WHERE id = ?",
            (json.dumps(remote), meter["id"]),
        )
        session = Session(
            meter_id=meter["id"],
            id=session_id,
            user_id=user_id,
            title=title,
            port=0,
            token=remote.get("token", ""),
            directory=str(directory),
            provider=provider,
            machine=machine.id,
            remote=remote,
        )
        with _lock:
            _sessions[session_id] = session
        return session.to_dict()

    meter = _open_meter(user_id, session_id, "local", {"title": title}, machine)
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
        metering.close_meter(meter["id"], detail="marimo would not start")
        raise RunnerError(f"could not start marimo: {exc}") from exc

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = (process.stderr.read() or b"").decode()[-400:] if process.stderr else ""
            metering.close_meter(meter["id"], detail="marimo exited immediately")
            raise RunnerError(f"marimo exited immediately: {stderr}")
        if _port_open(port):
            break
        time.sleep(0.25)
    else:
        process.kill()
        metering.close_meter(meter["id"], detail="marimo did not start listening")
        raise RunnerError("marimo did not start listening in time")

    session = Session(
        meter_id=meter["id"],
        id=session_id,
        user_id=user_id,
        title=title,
        port=port,
        token=token,
        directory=str(directory),
        machine=machine.id,
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


def _open_meter(
    user_id: str, session_id: str, provider: str, detail: dict[str, Any],
    machine: Any = None,
) -> dict[str, Any]:
    """Open the meter before anything is spawned.

    Deliberately first: a session that cannot be billed must never reach the
    point of holding a port, a process or - worst - a cloud instance.

    The machine id goes in `detail` because the charge path needs it to decide
    whether the plan's included minutes may pay: a meter that does not carry
    its class would let an H100 minute draw on an allowance sized for a €4 box.
    """
    from twohelixes import machines

    machine = machine or machines.get(None)
    return metering.open_meter(
        user_id,
        "notebook",
        session_id,
        machine.credits_per_minute,
        provider=provider,
        detail={**detail, "machine": machine.id},
        minimum_minutes=machine.minimum_minutes,
    )


def touch(session_id: str) -> None:
    """Mark a session as in use, so the sweeper leaves it alone.

    The heartbeat is on the meter, not on this process's copy of the session,
    because it is what tells the other workers the session still has an owner.
    """
    with _lock:
        session = _sessions.get(session_id)
    if session:
        session.last_seen = time.time()
        if session.meter_id:
            metering.heartbeat(session.meter_id)


def stop(user_id: str, session_id: str) -> bool:
    """Stop a session, whichever worker happens to be holding it.

    The meter is the source of truth: this worker may not own the process, and
    a request that lands on the wrong one of four workers used to report "not
    found" while the session kept running and kept billing. Closing the meter
    charges the final minute and marks it for reclamation; the worker that owns
    the process picks that up on its next sweep.
    """
    meter = metering.by_ref("notebook", session_id)
    if meter is None or meter["user_id"] != user_id:
        return False

    metering.close_meter(meter["id"], detail="stopped by the user")

    with _lock:
        session = _sessions.pop(session_id, None)
    if session is not None:
        _terminate(session)
    elif meter["provider"] not in ("local", "", None):
        # A remote instance can be stopped from anywhere; a local process
        # cannot, and its owner will reap it when the meter is gone.
        from twohelixes.notebooks import compute

        try:
            compute.stop_remote(str(meter["provider"]), json.loads(meter["remote"] or "{}"))
        except Exception:  # noqa: BLE001
            log.exception("could not stop remote notebook %s", session_id)
    return True


def reclaim(meter: dict[str, Any]) -> None:
    """Terminate the work behind a meter the sweeper gave up on."""
    session_id = str(meter.get("ref") or "")
    with _lock:
        session = _sessions.pop(session_id, None)
    if session is not None:
        _terminate(session)
        return
    if meter.get("provider") not in ("local", "", None):
        from twohelixes.notebooks import compute

        try:
            compute.stop_remote(
                str(meter["provider"]), json.loads(meter.get("remote") or "{}")
            )
        except Exception:  # noqa: BLE001
            log.exception("could not reclaim remote notebook %s", session_id)


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
    """Every session the user has open, on any worker.

    Read from the meters and enriched with the local process where this worker
    happens to hold it. Listing from `_sessions` alone meant the answer
    depended on which of four workers accepted the connection.
    """
    out: list[dict[str, Any]] = []
    for meter in metering.open_for(user_id):
        if meter["kind"] != "notebook":
            continue
        session_id = str(meter["ref"])
        with _lock:
            session = _sessions.get(session_id)
        if session is not None:
            row = session.to_dict()
        else:
            remote = json.loads(meter["remote"] or "{}")
            detail = json.loads(meter["detail"] or "{}")
            row = {
                "id": session_id,
                "title": str(detail.get("title") or "Notebook"),
                "provider": meter["provider"],
                "running": True,
                "started_at": meter["started_at"],
                "idle_seconds": int(time.time() - float(meter["last_seen"])),
                "url": str(remote.get("url", "")),
                "credits_per_minute": int(meter["rate"]),
                "machine": str(detail.get("machine") or meter["provider"]),
                "meter_id": meter["id"],
                # This worker is not holding the process, so it cannot proxy to
                # a local one. Saying so beats handing back a dead URL.
                "on_this_worker": False,
            }
        row["credits_charged"] = int(meter["charged"])
        row["minutes"] = int(meter["minutes"])
        out.append(row)
    return out


def active_count() -> int:
    with _lock:
        return sum(1 for s in _sessions.values() if s.alive())


# --------------------------------------------------------------------------
# Reaping and billing
# --------------------------------------------------------------------------


def sweep_local(now: float | None = None) -> None:
    """Close meters for sessions this worker owns that are gone or idle.

    Charging and orphan reclamation are `metering.sweep`'s job and run in every
    worker. This is the part only the owning worker can do: notice that its own
    marimo process exited, or that nobody has touched the session in half an
    hour, and stop the clock.
    """
    now = now or time.time()
    with _lock:
        sessions = list(_sessions.values())

    for session in sessions:
        if not session.alive():
            with _lock:
                _sessions.pop(session.id, None)
            if session.meter_id:
                metering.close_meter(session.meter_id, detail="process exited")
            continue

        if now - session.last_seen > session.idle_timeout:
            log.info("reaping idle notebook %s", session.id)
            with _lock:
                _sessions.pop(session.id, None)
            if session.meter_id:
                metering.close_meter(session.meter_id, detail="idle timeout")
            _terminate(session)
            continue

        if now - session.started_at > session.max_seconds:
            # The ceiling is not about idleness: a session can be busy and still
            # be something the person walked away from days ago.
            log.info("reaping notebook %s at its session ceiling", session.id)
            with _lock:
                _sessions.pop(session.id, None)
            if session.meter_id:
                metering.close_meter(session.meter_id, detail="maximum session length")
            _terminate(session)
            continue

        # Alive and in use: keep the meter's heartbeat fresh so another worker
        # does not reclaim a session that is working perfectly well.
        if session.meter_id:
            metering.heartbeat(session.meter_id)


def _reap_loop() -> None:
    while True:
        time.sleep(60)
        try:
            sweep_local()
        except Exception:  # noqa: BLE001 - the reaper must never die
            log.exception("notebook reap iteration failed")


def _ensure_reaper() -> None:
    global _reaper_started
    with _lock:
        if _reaper_started:
            return
        _reaper_started = True
    threading.Thread(target=_reap_loop, name="notebook-reaper", daemon=True).start()


def shutdown_all() -> None:
    """Stop every session this worker owns, and stop billing for them.

    A restart that killed the processes but left the meters open would keep
    charging for notebooks that no longer exist.
    """
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        if session.meter_id:
            try:
                metering.close_meter(session.meter_id, detail="server shutting down")
            except Exception:  # noqa: BLE001
                log.exception("could not close meter for %s", session.id)
        _terminate(session)
