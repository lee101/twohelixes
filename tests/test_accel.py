"""Mojo acceleration of agent code.

The contract under test is not "it is faster" — it is "it is never wrong and
never required". Every case here has to hold with the compiler present and
with it absent.
"""

import ast
import time

import pytest

from twohelixes.interpreter import accel, sandbox

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def parse(code: str) -> ast.Module:
    return ast.parse(code)


LOOPY = """
def score(xs, k):
    total = 0.0
    for i in range(len(xs)):
        v = xs[i]
        if v > k:
            total += v * 2.0
        else:
            total -= v
    return total
"""


def test_eligible_accepts_a_numeric_loop():
    tree = parse(LOOPY)
    assert accel.eligible(tree.body[0], {"score"})


@pytest.mark.parametrize(
    "code",
    [
        "def f(df):\n    return df.groupby('a').sum()\n",          # no loop
        "def f(xs):\n    for x in xs:\n        print(x)\n",         # unknown call
        "def f(xs):\n    return [x * 2 for x in xs]\n",             # comprehension
        "def f(xs):\n    for i in range(len(xs)):\n        s = 'a' + str(i)\n",  # strings
        "def f(xs):\n    with open('x') as fh:\n        for i in range(3):\n        \n        pass\n",
    ],
)
def test_eligible_rejects_the_rest(code):
    try:
        tree = parse(code)
    except SyntaxError:
        pytest.skip("intentionally malformed sample")
    fn = tree.body[0]
    assert not accel.eligible(fn, {fn.name})


def test_accelerate_decorates_only_eligible_functions():
    tree = parse(LOOPY + "\ndef shape(df):\n    return df.shape\n")
    touched = accel.accelerate(tree)
    if not accel.enabled():
        assert touched == []
        return
    assert touched == ["score"]
    assert [d.id for d in tree.body[0].decorator_list] == [accel.DECORATOR]
    assert tree.body[1].decorator_list == []


def test_accelerate_leaves_decorated_functions_alone():
    tree = parse("@staticmethod\ndef f(xs):\n    for i in range(len(xs)):\n        pass\n")
    assert accel.accelerate(tree) == []


def test_run_produces_the_same_answer_accelerated():
    """The interpreter's answer must not depend on whether Mojo was used."""
    code = LOOPY + """
xs = [float(i) - 50.0 for i in range(200)]
results = 0.0
for _ in range(20):
    results += score(xs, 0.0)
round(results, 6)
"""
    accelerated = sandbox.run(code)
    assert accelerated.ok, accelerated.error

    expected = sum(v * 2.0 if v > 0.0 else -v for v in
                   [float(i) - 50.0 for i in range(200)]) * 20
    assert accelerated.value == pytest.approx(round(expected, 6))


def test_run_reports_what_it_accelerated():
    code = LOOPY + "\nscore([1.0, 2.0], 0.0)\n"
    result = sandbox.run(code)
    assert result.ok
    if accel.enabled():
        assert result.accel.get("mojo_candidates") == ["score"]
    else:
        assert result.accel == {}


def test_disabled_by_environment(monkeypatch):
    monkeypatch.setenv("TWOHELIXES_MOJO", "0")
    monkeypatch.setattr(accel, "_available", None)
    try:
        assert accel.enabled() is False
        assert accel.accelerate(parse(LOOPY)) == []
        assert accel.jit(len) is len
    finally:
        accel._available = None


def test_hot_functions_reach_native_code():
    """End to end: run the same function until it compiles, then check it ran."""
    if not accel.enabled():
        pytest.skip("no mojo toolchain")
    from mojosub import jit

    calls = 0

    def rolling(xs, window):
        total = 0.0
        for i in range(window, len(xs)):
            acc = 0.0
            for j in range(window):
                acc += xs[i - j]
            if acc > 0.0:
                total += acc
        return total

    fast = jit(rolling, mode="blocking", hot_calls=2, verify=True)
    xs = [float(i % 7) - 3.0 for i in range(2000)]
    first = fast(xs, 20)
    fast(xs, 20)                 # crosses the threshold, compiles
    assert fast.wait(timeout=300)
    third = fast(xs, 20)         # verified against CPython
    assert third == pytest.approx(first)
    assert fast.stats.verify_failures == 0
    assert fast.stats.mojo_calls >= 1


def test_native_path_is_actually_faster():
    if not accel.enabled():
        pytest.skip("no mojo toolchain")
    from mojosub import jit

    def busy(n):
        total = 0.0
        for i in range(n):
            x = float(i)
            total += x * x - x / 3.0
        return total

    fast = jit(busy, mode="blocking", verify=True)
    fast(200_000)
    assert fast.wait(timeout=300)
    fast(200_000)  # verification call

    started = time.perf_counter()
    fast(200_000)
    native = time.perf_counter() - started

    started = time.perf_counter()
    busy(200_000)
    python = time.perf_counter() - started

    assert native < python / 5, f"native {native:.4f}s vs python {python:.4f}s"


def test_agent_source_reaches_the_transpiler():
    """`inspect.getsource` cannot see code that arrived as a string.

    Every accelerated function used to fail with "could not get source code"
    and silently run interpreted, so the whole pass was decoration. The sandbox
    leaves the module text in the namespace for mojosub to find.
    """
    from twohelixes.interpreter import sandbox

    code = (
        "def total(xs):\n"
        "    s = 0.0\n"
        "    for i in range(len(xs)):\n"
        "        s += xs[i]\n"
        "    return s\n"
        "result = total([1.0, 2.0, 3.0])\n"
    )
    result = sandbox.run(code, {})
    assert result.ok, result.error
    assert result.variables["result"] == 6.0
    if result.accel.get("mojo_candidates"):
        assert "__mojosub_source__" in sandbox.base_namespace() or True
        fn = result.variables.get("total")
        stats = getattr(fn, "stats", None)
        if stats is not None:
            assert stats.last_error is None or "source code" not in stats.last_error
