"""LLM access through the OpenPaths gateway.

Interactive chart work runs on `gpt-5.6-luna` (the cheap tier) because every
interactive request is rate limited and the pipeline calls the model several
times per query. Long-running and deep-research agents escalate to terra/sol,
which only paying callers can reach.

Every call is priced. `usage` comes back on the response, `PRICES` mirrors the
gateway's own table, and `spend()` accumulates the cost of a whole pipeline run
into thread-local state. Without that, the credit prices in `config` would be a
guess: nobody can set a margin on a number they have never measured, and a
query that quietly costs more than it charges is a subscription to a loss.

Two behaviours matter for this product:

* `stream_json` surfaces partial reasoning as it arrives so the UI can show
  the trace building, rather than a spinner followed by a wall of output; and
* `json_call` is strict about returning a dict - a pipeline stage that gets
  prose back should retry, not crash three stages later.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from twohelixes import config

log = logging.getLogger("twohelixes.llm")

_client_lock = threading.Lock()
_client: Any = None

JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# The gateway serves aliases (gpt-5.6-luna, auto-easy-task). Talking straight
# to OpenAI, those do not exist, so map each to its nearest public model.
DIRECT_MODEL_FALLBACK = {
    config.MODEL_DEFAULT: "gpt-4.1-mini",
    config.MODEL_ESCALATE: "gpt-4.1",
    config.MODEL_DEEP: "gpt-4.1",
    config.MODEL_FAST: "gpt-4.1-nano",
    config.MODEL_MINI: "gpt-4.1-nano",
}


# US dollars per million tokens, mirroring openpaths/config.yaml. Prices are
# duplicated rather than fetched because a pricing decision must not depend on
# a network call - and because a silent gateway price change should show up as
# a margin discrepancy here, not as an invisible one.
PRICES = {
    "gpt-5.6-luna": (1.00, 6.00),
    "gpt-5.6-terra": (2.50, 15.00),
    "gpt-5.6-sol": (5.00, 30.00),
    "auto-easy-task": (0.15, 0.60),
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
}
DEFAULT_PRICE = (1.00, 6.00)


@dataclass
class Spend:
    """What a run cost us, in tokens and in millionths of a dollar."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    micros: int = 0
    by_model: dict[str, int] = field(default_factory=dict)

    @property
    def cents(self) -> float:
        return self.micros / 10_000.0

    def add(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        input_price, output_price = PRICES.get(model, DEFAULT_PRICE)
        micros = round(
            prompt_tokens * input_price + completion_tokens * output_price
        )
        self.calls += 1
        self.input_tokens += prompt_tokens
        self.output_tokens += completion_tokens
        self.micros += micros
        self.by_model[model] = self.by_model.get(model, 0) + micros

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "micros": self.micros,
            "cents": round(self.cents, 4),
            "by_model": self.by_model,
        }


_spend = threading.local()


@contextmanager
def measure() -> Iterator[Spend]:
    """Accumulate the model cost of everything called inside the block.

    Thread-local because a pipeline run owns a thread here: the streaming
    handlers run on their own, and two concurrent runs must not pool their
    spend into one number.
    """
    previous = getattr(_spend, "current", None)
    current = Spend()
    _spend.current = current
    try:
        yield current
    finally:
        _spend.current = previous


def _record(model: str, usage: Any) -> None:
    current = getattr(_spend, "current", None)
    if current is None or usage is None:
        return
    try:
        current.add(
            model,
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        )
    except Exception:  # noqa: BLE001 - accounting must never break a request
        log.debug("could not record usage for %s", model)


class LLMError(Exception):
    pass


class CircuitOpen(LLMError):
    pass


@dataclass
class Circuit:
    """Trip after repeated failures so a dead gateway fails fast."""

    failures: int = 0
    opened_at: float = 0.0
    threshold: int = 5
    cooldown: float = 30.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def before(self) -> None:
        with self.lock:
            if self.opened_at and time.time() - self.opened_at < self.cooldown:
                raise CircuitOpen("LLM gateway circuit is open")
            if self.opened_at:
                self.opened_at = 0.0
                self.failures = 0

    def ok(self) -> None:
        with self.lock:
            self.failures = 0

    def fail(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.opened_at = time.time()
                log.warning("LLM circuit opened after %d failures", self.failures)


_circuit = Circuit()


def client() -> Any:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        from openai import OpenAI

        key, base_url, provider = config.llm_credentials()
        if not key:
            raise LLMError("no OPENPATHS_API_KEY / OPENAI_API_KEY configured")

        kwargs: dict[str, Any] = {
            "api_key": key,
            "timeout": 180.0,
            # Retries are handled here so the backoff and the circuit breaker
            # see every attempt.
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url

        log.info("llm provider=%s base=%s", provider, base_url or "default")
        _client = OpenAI(**kwargs)
        return _client


def resolve_model(model: str) -> str:
    """Map a gateway alias to a public model when talking to OpenAI directly."""
    if config.using_gateway():
        return model
    return DIRECT_MODEL_FALLBACK.get(model, model)


def extract_json(text: str) -> Any:
    """Pull a JSON value out of a model response.

    Models wrap JSON in fences, prepend a sentence, or emit trailing commas.
    Each of those is cheap to recover from and expensive to fail on.
    """
    if not text:
        raise LLMError("empty response")
    text = text.strip()

    try:
        return json.loads(text)
    except ValueError:
        pass

    match = JSON_BLOCK.search(text)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except ValueError:
            pass

    # Fall back to the outermost balanced braces or brackets.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
            try:
                return json.loads(candidate)
            except ValueError:
                stripped = re.sub(r",\s*([}\]])", r"\1", candidate)
                try:
                    return json.loads(stripped)
                except ValueError:
                    continue

    raise LLMError(f"no JSON in response: {text[:200]}")


def call(
    prompt: str,
    *,
    system: str = "",
    model: str = config.MODEL_DEFAULT,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    attempts: int = 3,
) -> str:
    """One completion, with backoff. Returns raw text."""
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last: Exception | None = None
    for attempt in range(attempts):
        _circuit.before()
        try:
            started = time.time()
            response = client().chat.completions.create(
                model=resolve_model(model),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            _circuit.ok()
            _record(model, getattr(response, "usage", None))
            text = response.choices[0].message.content or ""
            log.debug(
                "llm %s ok in %dms (%d chars)",
                model,
                int((time.time() - started) * 1000),
                len(text),
            )
            return text
        except CircuitOpen:
            raise
        except Exception as exc:  # noqa: BLE001 - gateway errors are varied
            last = exc
            _circuit.fail()
            if attempt + 1 < attempts:
                time.sleep(0.5 * (2**attempt))
            log.warning("llm %s attempt %d failed: %s", model, attempt + 1, exc)

    raise LLMError(f"LLM call failed after {attempts} attempts: {last}")


def json_call(
    prompt: str,
    *,
    system: str = "",
    model: str = config.MODEL_DEFAULT,
    temperature: float = 0.1,
    max_tokens: int = 4096,
    attempts: int = 3,
    expect: type = dict,
) -> Any:
    """A completion that must parse as JSON of the expected shape."""
    system = (system + "\n\nRespond with JSON only. No prose, no code fences.").strip()

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            text = call(
                prompt,
                system=system,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                attempts=1,
            )
            value = extract_json(text)
            if expect is not None and not isinstance(value, expect):
                raise LLMError(f"expected {expect.__name__}, got {type(value).__name__}")
            return value
        except CircuitOpen:
            raise
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("json_call attempt %d failed: %s", attempt + 1, exc)
            if attempt + 1 < attempts:
                time.sleep(0.4 * (2**attempt))

    raise LLMError(f"json_call failed after {attempts} attempts: {last}")


def stream(
    prompt: str,
    *,
    system: str = "",
    model: str = config.MODEL_DEFAULT,
    temperature: float = 0.3,
    max_tokens: int = 4096,
    on_delta: Callable[[str], None] | None = None,
) -> str:
    """Stream a completion, invoking `on_delta` per token chunk.

    This is what makes the reasoning trace feel live: the caller forwards each
    delta straight onto the SSE stream.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    _circuit.before()
    parts: list[str] = []
    try:
        response = client().chat.completions.create(
            model=resolve_model(model),
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            # A streamed call reports its usage only in a final, choice-less
            # chunk, and only if asked. Without this the trace - the most
            # visible thing the product does - would be the one call whose cost
            # we never counted.
            stream_options={"include_usage": True},
        )
        for chunk in response:
            if getattr(chunk, "usage", None) is not None:
                _record(model, chunk.usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                parts.append(piece)
                if on_delta is not None:
                    on_delta(piece)
        _circuit.ok()
    except CircuitOpen:
        raise
    except Exception as exc:  # noqa: BLE001
        _circuit.fail()
        raise LLMError(f"streaming call failed: {exc}") from exc

    return "".join(parts)


def parallel(calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Run independent LLM calls concurrently.

    The pipeline's stages are sequential, but within a stage the questions
    ("what chart type?", "which columns?") are independent, and the gateway
    round trip dominates. Threads work here because the SDK blocks on network
    I/O with the GIL released.
    """
    results: dict[str, Any] = {}
    errors: dict[str, Exception] = {}
    # Spend is thread-local, so a worker thread starts with none and its cost
    # would vanish - and the stages that fan out are the expensive ones.
    parent_spend = getattr(_spend, "current", None)

    def worker(name: str, kwargs: dict[str, Any]) -> None:
        _spend.current = parent_spend
        try:
            results[name] = json_call(**kwargs)
        except Exception as exc:  # noqa: BLE001
            errors[name] = exc

    threads = [
        threading.Thread(target=worker, args=(name, kwargs), daemon=True)
        for name, kwargs in calls
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=200)

    for name, exc in errors.items():
        log.warning("parallel call %s failed: %s", name, exc)
    return results
