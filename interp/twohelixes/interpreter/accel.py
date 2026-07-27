"""Compile the agent's hot numeric functions to native code.

The agent writes loops. Not vectorized numpy — loops with branches in them,
because that is how a rolling window with a condition, a backtest, or a
custom score is naturally expressed, and because the model writes what reads
clearly. CPython runs those 50-100x slower than they need to run, and a
60-second pipeline budget notices.

`mojosub` (github.com/lee101/mojosub) transpiles the typed-numeric subset of
Python to Mojo, compiles a shared library, and dispatches into it in about a
microsecond. This module decides *which* functions get that treatment and
under what safety rules:

- Only top-level `def`s in agent code, and only ones whose body is inside the
  subset. Everything else is untouched.
- Nothing compiles until a function has been called `HOT_CALLS` times or has
  burned `HOT_SECONDS` of CPU. A compile costs ~5 seconds of wall clock on a
  background thread, which is only worth spending on something that repeats.
- The first native result is checked against CPython (`verify=True`) before
  the variant is trusted. Mojo's `Int` wraps where CPython promotes and a
  vectorized sum reassociates; a disagreement retires the variant instead of
  quietly returning a different number.
- Every failure path — no compiler, unsupported syntax, mismatch — falls back
  to the interpreter the agent already had.

The win compounds across requests: units are content-addressed on disk, so the
second time anyone runs the same function it is native from the first call.
"""

from __future__ import annotations

import ast
import logging
import os
from typing import Any

log = logging.getLogger("twohelixes.interpreter.accel")

HOT_CALLS = int(os.environ.get("TWOHELIXES_MOJO_HOT_CALLS", "4"))
HOT_SECONDS = float(os.environ.get("TWOHELIXES_MOJO_HOT_SECONDS", "0.05"))
DECORATOR = "__mojo_jit"

_available: bool | None = None
_jit = None


def enabled() -> bool:
    """True when acceleration is switched on and mojosub can actually build.

    `TWOHELIXES_MOJO=0` disables it outright. Otherwise the check is whether
    the toolchain answers, which is done once and cached — a worker without a
    Mojo compiler must not pay a subprocess launch per request to rediscover
    that.
    """
    global _available, _jit
    if os.environ.get("TWOHELIXES_MOJO", "1") == "0":
        return False
    if _available is not None:
        return _available
    # mojosub needs a compiler. This repo already pins one in its pixi
    # environment, so point it there rather than requiring `mojo` on the
    # server's PATH.
    if not os.environ.get("MOJOSUB_MOJO") and not os.environ.get("MOJOSUB_PIXI_PROJECT"):
        root = os.path.dirname(  # <repo>/interp/twohelixes/interpreter -> <repo>
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        if os.path.exists(os.path.join(root, "pixi.toml")):
            os.environ["MOJOSUB_PIXI_PROJECT"] = root
    try:
        import mojosub

        mojosub.mojo_version()
        _jit = mojosub.jit
        _available = True
        log.info("mojo acceleration available: %s", mojosub.mojo_version())
    except Exception as exc:  # noqa: BLE001 - absence is a normal state
        _available = False
        log.info("mojo acceleration unavailable: %s", exc)
    return _available


def jit(fn):
    """Wrap `fn` with mojosub under this module's policy.

    Exposed in the interpreter namespace as `mojo`, so agent code can ask for
    acceleration explicitly on a function the automatic pass skipped.
    """
    if not enabled():
        return fn
    try:
        return _jit(
            fn,
            mode="tiered",
            hot_calls=HOT_CALLS,
            hot_seconds=HOT_SECONDS,
            verify=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("jit wrap failed: %s", exc)
        return fn


# Statements that rule a function out before the transpiler is ever asked.
# This is a cheap pre-filter, not the real check — mojosub does that — but it
# keeps the common case (a function that touches a DataFrame) from spawning a
# compile thread that is only going to fail.
_REJECTED_NODES = (
    ast.With, ast.AsyncWith, ast.Try, ast.Raise, ast.Assert, ast.Import,
    ast.ImportFrom, ast.Global, ast.Nonlocal, ast.Lambda, ast.ListComp,
    ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.Yield, ast.YieldFrom,
    ast.Await, ast.ClassDef, ast.AsyncFunctionDef, ast.JoinedStr, ast.Starred,
    ast.Dict, ast.Set, ast.List, ast.Tuple,
)

_ALLOWED_CALLS = frozenset({
    "int", "float", "bool", "abs", "min", "max", "len", "range",
    "sqrt", "exp", "log", "log2", "log10", "sin", "cos", "tan", "tanh",
    "atan", "asin", "acos", "sinh", "cosh", "floor", "ceil", "erf", "atan2",
    "pow",
})


def eligible(fn: ast.FunctionDef, local_names: set[str]) -> bool:
    """Whether this function is worth handing to the transpiler."""
    args = fn.args
    if args.vararg or args.kwarg or args.kwonlyargs or args.defaults:
        return False
    if not args.args:
        return False
    if not any(isinstance(n, (ast.For, ast.While)) for n in ast.walk(fn)):
        return False  # no loop, nothing to win
    for node in ast.walk(fn):
        if isinstance(node, _REJECTED_NODES):
            return False
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node is not (fn.body[0].value if isinstance(fn.body[0], ast.Expr) else None):
                return False  # a string literal anywhere but the docstring
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id not in _ALLOWED_CALLS and func.id not in local_names:
                    return False
            elif isinstance(func, ast.Attribute):
                root = func.value
                if not (isinstance(root, ast.Name) and root.id in ("math", "np", "numpy")):
                    return False
                if func.attr not in _ALLOWED_CALLS:
                    return False
            else:
                return False
    return True


def accelerate(tree: ast.Module) -> list[str]:
    """Decorate eligible top-level functions in `tree`, in place.

    Returns the names touched, for logging. The decorator name is injected
    into the namespace by `namespace()`; the AST is rewritten rather than the
    functions being wrapped afterwards, because a function that has already
    been called through a plain reference would keep the unwrapped one.
    """
    if not enabled():
        return []
    local_names = {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    touched: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.decorator_list:
            continue
        if not eligible(node, local_names):
            continue
        node.decorator_list = [ast.Name(id=DECORATOR, ctx=ast.Load())]
        touched.append(node.name)
    if touched:
        ast.fix_missing_locations(tree)
    return touched


def namespace() -> dict[str, Any]:
    """Names this module contributes to every interpreter run."""
    return {DECORATOR: jit, "mojo": jit}


def stats(namespace_after: dict[str, Any]) -> dict[str, Any]:
    """Summarize what acceleration did, for the run trace."""
    compiled = 0
    native_calls = 0
    fallbacks = 0
    for value in namespace_after.values():
        st = getattr(value, "stats", None)
        if st is None or not hasattr(st, "mojo_calls"):
            continue
        compiled += st.compiles
        native_calls += st.mojo_calls
        fallbacks += st.verify_failures
    if not (compiled or native_calls):
        return {}
    return {
        "mojo_compiled": compiled,
        "mojo_calls": native_calls,
        "mojo_verify_failures": fallbacks,
    }
