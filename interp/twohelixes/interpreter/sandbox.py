"""The code interpreter the agent writes into.

Design notes that matter for latency: the pipeline runs several exec steps per
user query, so the namespace is built once at worker boot and *copied* per
run (a dict copy, not re-imports). A cold `import pandas` inside a request
would cost more than the LLM call that produced the code.

Safety here is containment, not a security boundary: the interpreter runs
agent-authored code inside the worker process. Untrusted multi-tenant code
would need process isolation - that is what the long-running agent runner is
for.
"""

from __future__ import annotations

import ast
import builtins
import contextlib
import importlib
import io
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from twohelixes.interpreter import accel

log = logging.getLogger("twohelixes.interpreter")

_BASE_NAMESPACE: dict[str, Any] = {}
_warm_lock = threading.Lock()
_warmed = False

# Modules pre-imported into every run. Name -> import path. Anything the agent
# reaches for that is missing gets auto-imported on demand (see _auto_import).
PRELOAD: dict[str, str] = {
    # core numerics
    "np": "numpy",
    "numpy": "numpy",
    "pd": "pandas",
    "pandas": "pandas",
    "pl": "polars",
    "polars": "polars",
    # plotting
    "px": "plotly.express",
    "go": "plotly.graph_objects",
    "pio": "plotly.io",
    "ff": "plotly.figure_factory",
    "plotly": "plotly",
    # stats / ml
    "scipy": "scipy",
    "stats": "scipy.stats",
    "signal": "scipy.signal",
    "optimize": "scipy.optimize",
    "interpolate": "scipy.interpolate",
    "linalg": "scipy.linalg",
    "sm": "statsmodels.api",
    "tsa": "statsmodels.tsa.api",
    "sklearn": "sklearn",
    # stdlib the agent reaches for constantly
    "json": "json",
    "math": "math",
    "re": "re",
    "datetime": "datetime",
    "time": "time",
    "random": "random",
    "itertools": "itertools",
    "functools": "functools",
    "collections": "collections",
    "statistics": "statistics",
    "decimal": "decimal",
    "textwrap": "textwrap",
    "unicodedata": "unicodedata",
    "hashlib": "hashlib",
    "base64": "base64",
    "csv": "csv",
    "io": "io",
    "operator": "operator",
    "warnings": "warnings",
    # data access
    "duckdb": "duckdb",
    "sqlalchemy": "sqlalchemy",
}

# Lazily importable on first reference. These are heavy, so they are not part
# of boot: a query that never mentions them should not pay for them.
LAZY: dict[str, str] = {
    "sns": "seaborn",
    "seaborn": "seaborn",
    "plt": "matplotlib.pyplot",
    "matplotlib": "matplotlib",
    "nx": "networkx",
    "networkx": "networkx",
    "geopandas": "geopandas",
    "gpd": "geopandas",
    "shapely": "shapely",
    "pyarrow": "pyarrow",
    "pa": "pyarrow",
    "xgboost": "xgboost",
    "xgb": "xgboost",
    "lightgbm": "lightgbm",
    "torch": "torch",
    "transformers": "transformers",
    "nltk": "nltk",
    "spacy": "spacy",
    "TextBlob": "textblob.TextBlob",
    "requests": "requests",
    "httpx": "httpx",
    "bs4": "bs4",
    "PIL": "PIL",
    "Image": "PIL.Image",
    "cv2": "cv2",
    "prophet": "prophet",
    "statsmodels": "statsmodels",
    "openpyxl": "openpyxl",
    "yaml": "yaml",
}

# Names the agent must not touch. Not a sandbox escape barrier - a guard rail
# against an agent "cleaning up" the worker's filesystem or exiting the loop.
BANNED_NAMES = frozenset(
    {
        "exit",
        "quit",
        "compile",
        "memoryview",
    }
)

BANNED_CALLS = (
    ("os", "system"),
    ("os", "remove"),
    ("os", "rmdir"),
    ("os", "unlink"),
    ("os", "kill"),
    ("os", "_exit"),
    ("shutil", "rmtree"),
    ("subprocess", "run"),
    ("subprocess", "Popen"),
    ("subprocess", "call"),
    ("sys", "exit"),
)


class InterpreterError(Exception):
    pass


class UnsafeCode(InterpreterError):
    pass


@dataclass
class RunResult:
    ok: bool
    stdout: str = ""
    error: str = ""
    error_type: str = ""
    traceback: str = ""
    duration_ms: int = 0
    value: Any = None
    variables: dict[str, Any] = field(default_factory=dict)
    accel: dict[str, Any] = field(default_factory=dict)

    def to_payload(self, include_vars: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": self.ok,
            "stdout": self.stdout[-8000:],
            "duration_ms": self.duration_ms,
        }
        if not self.ok:
            payload["error"] = self.error
            payload["error_type"] = self.error_type
            payload["traceback"] = self.traceback[-4000:]
        if include_vars:
            payload["variables"] = sorted(self.variables)
        if self.accel:
            payload["accel"] = self.accel
        return payload


def warm() -> None:
    """Import the preload set once per worker."""
    global _warmed
    with _warm_lock:
        if _warmed:
            return
        started = time.time()
        namespace: dict[str, Any] = {"__builtins__": builtins}

        for alias, path in PRELOAD.items():
            try:
                namespace[alias] = _import(path)
            except Exception as exc:  # noqa: BLE001 - optional deps are fine
                log.debug("preload %s (%s) unavailable: %s", alias, path, exc)

        # Frequently used symbols, so the agent can write idiomatic code.
        try:
            import pandas as pd

            namespace["DataFrame"] = pd.DataFrame
            namespace["Series"] = pd.Series
            namespace["read_csv"] = pd.read_csv
            namespace["to_datetime"] = pd.to_datetime
            namespace["date_range"] = pd.date_range
            namespace["concat"] = pd.concat
            namespace["merge"] = pd.merge
        except Exception:  # noqa: BLE001
            pass

        try:
            from datetime import date, datetime, timedelta, timezone

            namespace.update(
                date=date, datetime=datetime, timedelta=timedelta, timezone=timezone
            )
        except Exception:  # noqa: BLE001
            pass

        from twohelixes.interpreter import accel, tools

        namespace.update(tools.namespace())
        namespace.update(accel.namespace())

        _BASE_NAMESPACE.clear()
        _BASE_NAMESPACE.update(namespace)
        _warmed = True
        log.info(
            "interpreter warm: %d names in %dms",
            len(namespace),
            int((time.time() - started) * 1000),
        )


def _import(path: str) -> Any:
    """Import `a.b.c` or `a.b:attr`-style dotted paths."""
    try:
        return importlib.import_module(path)
    except ImportError:
        module_path, _, attr = path.rpartition(".")
        if not module_path:
            raise
        module = importlib.import_module(module_path)
        return getattr(module, attr)


def base_namespace() -> dict[str, Any]:
    if not _warmed:
        warm()
    return dict(_BASE_NAMESPACE)


def _auto_import(code: str, namespace: dict[str, Any]) -> list[str]:
    """Import names the code references but the namespace does not define.

    Cheaper and more reliable than asking the model to remember every import;
    the failure mode we are avoiding is a NameError three stages deep.
    """
    added: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return added

    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = node
            while isinstance(root, ast.Attribute):
                root = root.value  # type: ignore[assignment]
            if isinstance(root, ast.Name):
                referenced.add(root.id)

    for name in referenced:
        if name in namespace or hasattr(builtins, name):
            continue
        path = LAZY.get(name) or PRELOAD.get(name)
        if not path:
            continue
        try:
            namespace[name] = _import(path)
            added.append(name)
        except Exception as exc:  # noqa: BLE001
            log.debug("auto-import %s failed: %s", name, exc)
    return added


def check_safety(code: str) -> None:
    """Reject the handful of calls that would damage the host."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise InterpreterError(f"syntax error: {exc}") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in BANNED_NAMES:
            raise UnsafeCode(f"use of {node.id} is not allowed")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func = node.func
            if isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in BANNED_CALLS:
                    raise UnsafeCode(f"{pair[0]}.{pair[1]} is not allowed")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                root = name.split(".")[0]
                if root in ("subprocess", "ctypes", "multiprocessing"):
                    raise UnsafeCode(f"importing {root} is not allowed")


def run(
    code: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
    capture_last: bool = True,
    check: bool = True,
) -> RunResult:
    """Execute agent-authored code against a fresh namespace.

    `variables` are injected (typically `df`, or several named frames for a
    join step) and any names the code defines are returned so the next stage
    can pick them up.
    """
    if check:
        try:
            check_safety(code)
        except InterpreterError as exc:
            return RunResult(
                ok=False, error=str(exc), error_type=exc.__class__.__name__
            )

    namespace = base_namespace()
    if variables:
        namespace.update(variables)
    _auto_import(code, namespace)

    stdout = io.StringIO()
    started = time.time()
    value: Any = None

    # Evaluate a trailing expression so the agent can end with `fig` and have
    # it come back, notebook style. The body stays an AST rather than being
    # unparsed back to text, because `accel` rewrites it before it is compiled.
    body_tree: ast.Module | None = None
    body = code
    tail_expr: str | None = None
    try:
        tree = ast.parse(code)
        if capture_last and tree.body and isinstance(tree.body[-1], ast.Expr):
            tail_expr = ast.unparse(tree.body[-1].value)
            body_tree = ast.Module(body=tree.body[:-1], type_ignores=[])
        else:
            body_tree = tree
    except (SyntaxError, ValueError):
        body_tree = None
        tail_expr = None
        body = code

    accelerated: list[str] = []
    if body_tree is not None:
        # Kept so the run can be repeated without acceleration if the
        # accelerated one raises. `accelerate` rewrites in place, so this has
        # to be a separate parse rather than the same object.
        plain_tree = ast.parse(code)
        if tail_expr is not None:
            plain_tree = ast.Module(body=plain_tree.body[:-1], type_ignores=[])
        try:
            accelerated = accel.accelerate(body_tree)
        except Exception as exc:  # noqa: BLE001 - acceleration is never required
            log.debug("acceleration pass failed: %s", exc)
        if accelerated:
            # The transpiler reads the function's source, and `inspect.getsource`
            # cannot see anything defined by `exec` of a string - which is all
            # agent code. Without this every accelerated function failed with
            # "could not get source code" and silently ran interpreted, so the
            # whole pass was decoration. mojosub looks for this name in the
            # namespace the function was defined in.
            namespace["__mojosub_source__"] = ast.unparse(body_tree)

    def _execute(tree: ast.Module | None, ns: dict[str, Any], out: io.StringIO):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            if tree is not None:
                if tree.body:
                    exec(compile(tree, "<agent>", "exec"), ns, ns)  # noqa: S102
            elif body.strip():
                exec(compile(body, "<agent>", "exec"), ns, ns)  # noqa: S102
            if tail_expr:
                return eval(compile(tail_expr, "<agent>", "eval"), ns, ns)  # noqa: S307
        return None

    fell_back = False
    watchdog = _Watchdog(timeout)
    try:
        watchdog.start()
        try:
            value = _execute(body_tree, namespace, stdout)
        except BaseException:
            # Acceleration is an optimisation, so it is never allowed to be the
            # reason a run fails. mojosub falls back per function, but the
            # decoration itself, the transpiler, or a compiled variant can
            # still raise something the interpreter would not have - and the
            # agent must not see an error the plain Python it wrote does not
            # produce. Re-run once, unaccelerated, from a clean namespace.
            # Not on a timeout: the watchdog's exception means the budget is
            # already spent, and a second run would spend it twice.
            if not accelerated or watchdog.expired:
                raise
            log.info("accelerated run failed, retrying interpreted: %s",
                     _first_line(traceback.format_exc()))
            fell_back = True
            stdout = io.StringIO()
            namespace = base_namespace()
            if variables:
                namespace.update(variables)
            _auto_import(code, namespace)
            value = _execute(plain_tree, namespace, stdout)
    except BaseException as exc:  # noqa: BLE001 - report everything to the agent
        return RunResult(
            ok=False,
            stdout=stdout.getvalue(),
            error=str(exc) or exc.__class__.__name__,
            error_type=exc.__class__.__name__,
            traceback=_clean_traceback(),
            duration_ms=int((time.time() - started) * 1000),
            variables=_user_names(namespace),
            accel={"mojo_fallback": True} if fell_back else {},
        )
    finally:
        watchdog.stop()

    names = _user_names(namespace)
    if fell_back:
        # The names in this namespace came from the interpreted run, so the
        # jit wrappers' own counters are gone with the namespace that held
        # them. Report the fallback, not stale numbers.
        stats = {"mojo_candidates": accelerated, "mojo_fallback": True}
    else:
        stats = _accel_stats(names, accelerated)
    return RunResult(
        ok=True,
        stdout=stdout.getvalue(),
        duration_ms=int((time.time() - started) * 1000),
        value=value,
        variables=names,
        accel=stats,
    )


def _first_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _accel_stats(names: dict[str, Any], accelerated: list[str]) -> dict[str, Any]:
    if not accelerated:
        return {}
    summary = accel.stats(names)
    summary["mojo_candidates"] = accelerated
    return summary


def _user_names(namespace: dict[str, Any]) -> dict[str, Any]:
    """Names the code introduced, minus the preloaded scaffolding."""
    return {
        key: val
        for key, val in namespace.items()
        if not key.startswith("_") and key not in _BASE_NAMESPACE
    }


def _clean_traceback() -> str:
    """Trim interpreter frames so the agent sees only its own code."""
    lines = traceback.format_exc().splitlines()
    keep = [
        line
        for line in lines
        if "sandbox.py" not in line and "importlib" not in line
    ]
    return "\n".join(keep)


class Timeout(InterpreterError):
    """Raised into the running thread when a run outlives its budget."""


class _Watchdog:
    """Best-effort wall-clock guard.

    An earlier version only set a flag, and nothing read it - so an agent loop
    that never terminated held the request open indefinitely and the user got
    no chart at all. This raises `Timeout` into the executing thread instead,
    which unwinds at the next bytecode boundary and lets the transform stage
    fall back to the cleaned frame.

    It still cannot interrupt a single long C kernel (a numpy call runs to
    completion with the GIL held), so the worker-level timeout remains the
    outer bound.
    """

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.timer: threading.Timer | None = None
        self.expired = False
        self.thread_id = threading.get_ident()

    def start(self) -> None:
        if self.timeout <= 0:
            return
        self.timer = threading.Timer(self.timeout, self._fire)
        self.timer.daemon = True
        self.timer.start()

    def _fire(self) -> None:
        self.expired = True
        log.warning("interpreter run exceeded %.1fs; interrupting", self.timeout)
        import ctypes

        ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(self.thread_id), ctypes.py_object(Timeout)
        )

    def stop(self) -> None:
        if self.timer is not None:
            self.timer.cancel()
        if self.expired:
            # The exception may not have landed yet - if it arrives after the
            # run returns it would surface in unrelated code. Clear it.
            import ctypes

            # An empty py_object is the NULL that clears a pending exception.
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(self.thread_id), ctypes.py_object()
            )
