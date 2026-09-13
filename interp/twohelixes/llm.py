"""LLM access through the OpenPaths gateway.

Analysis runs on Muse Spark 1.3 through the OpenPaths gateway. Small structured
decisions use DeepSeek Flash and escalate when needed. Each model has its own
circuit breaker; a provider outage can fall back without blocking other routes.

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
_deepseek_client: Any = None

JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# The gateway serves aliases (deepseek-v4-flash, openpaths/stealth/ox-alpha).
# Talking straight to OpenAI, those do not exist, so map each to its nearest
# public model. Kept for the no-gateway fallback path only.
DIRECT_MODEL_FALLBACK = {
    config.MODEL_PRIMARY: "gpt-4.1-mini",
    config.MODEL_DEFAULT: "gpt-4.1-mini",
    config.MODEL_ESCALATE: "gpt-4.1",
    config.MODEL_DEEP: "gpt-4.1",
    config.MODEL_FAST: "gpt-4.1-nano",
    config.MODEL_MINI: "gpt-4.1-nano",
    "deepseek-v4-flash": "gpt-4.1-mini",
    "deepseek-v4-pro": "gpt-4.1",
    "gpt-5.6-luna": "gpt-4.1-mini",
    "gpt-5.6-terra": "gpt-4.1",
    "gpt-5.6-sol": "gpt-4.1",
}


# US dollars per million tokens, mirroring openpaths/config.yaml. Prices are
# duplicated rather than fetched because a pricing decision must not depend on
# a network call - and because a silent gateway price change should show up as
# a margin discrepancy here, not as an invisible one.
PRICES = {
    # Verified against the gateway catalogue, September 2026. Contributor is
    # opt-in (config); it must also be provisioned by the gateway operator.
    "muse-spark-1.3": (1.25, 4.25),
    # The stealth route is free at the gateway (openpaths config.yaml prices
    # it 0/0); the entry exists so spend accounting never silently falls back
    # to DEFAULT_PRICE for the model that serves most requests.
    "openpaths/stealth/ox-alpha": (0.0, 0.0),
    "stealth/ox-alpha": (0.0, 0.0),
    "deepseek-v4-flash": (0.14, 0.28),
    "deepseek-v4-pro": (0.435, 0.87),
    "gpt-5.6-luna": (1.00, 6.00),
    "gpt-5.6-terra": (2.50, 15.00),
    "gpt-5.6-sol": (5.00, 30.00),
    "auto-easy-task": (0.15, 0.60),
}

# Conservative estimate for custom/unpriced routes, not a provider price quote.
# dict.get evaluates its default eagerly, even for known models. Without this
# constant _record swallowed NameError and every run appeared to cost zero.
DEFAULT_PRICE = (5.00, 30.00)


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


_circuits: dict[str, Circuit] = {}
_circuits_lock = threading.Lock()


def _circuit_for(model: str) -> Circuit:
    """One breaker per model id, not per gateway: the stealth route and the
    legacy DeepSeek routes fail independently, and an ox-alpha outage must
    not stop a call that can complete on deepseek."""
    with _circuits_lock:
        circuit = _circuits.get(model)
        if circuit is None:
            circuit = Circuit()
            _circuits[model] = circuit
        return circuit


# Where each tier goes when its model's circuit is open or the calls keep
# failing. The legacy DeepSeek routes are the "current model" every tier
# migrated away from; they stay wired in permanently so the migration cannot
# turn into an outage.
MODEL_FALLBACKS: dict[str, str] = {
    config.MODEL_DEFAULT: "deepseek-v4-flash",
    config.MODEL_FAST: "deepseek-v4-flash",
    config.MODEL_MINI: "deepseek-v4-flash",
    config.MODEL_ESCALATE: "deepseek-v4-flash",
    config.MODEL_DEEP: "deepseek-v4-flash",
}
# The fast tier must also have a distinct fallback. Do not fall back from an
# explicitly chosen non-contributor route to a contributor model.
MODEL_FALLBACKS["deepseek-v4-flash"] = "deepseek-v4-pro"


def fallback_model(model: str) -> str | None:
    fallback = MODEL_FALLBACKS.get(model)
    return fallback if fallback and fallback != model else None


def _route_dead(exc: Exception) -> bool:
    """A 429 / no-healthy-provider reply means this route is out for now.

    Retrying within seconds only burns the caller's latency: the gateway has
    already marked the provider unhealthy, and the upstream pool is shared.
    One failure books on the circuit and the request moves to the fallback.
    """
    text = str(exc).lower()
    return (
        "429" in text
        or "rate-limited" in text
        or "no healthy provider" in text
        or "provider returned error" in text
    )


def client(model: str | None = None) -> Any:
    global _client, _deepseek_client
    # Prefer the openpaths gateway when configured - including for deepseek
    # ids - so local DEV hits `../openpaths` and production stays one path.
    # Direct DeepSeek is only for the no-gateway fallback.
    if (
        model
        and model.startswith("deepseek-")
        and not config.using_gateway()
    ):
        key, base_url = config.deepseek_credentials()
        if key:
            if _deepseek_client is not None:
                return _deepseek_client
            with _client_lock:
                if _deepseek_client is None:
                    from openai import OpenAI

                    _deepseek_client = OpenAI(
                        api_key=key,
                        base_url=base_url,
                        timeout=180.0,
                        max_retries=0,
                    )
                    log.info("llm provider=deepseek base=%s", base_url)
            return _deepseek_client
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
    if model.startswith("deepseek-") and config.deepseek_credentials()[0]:
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


def _candidates(model: str) -> list[str]:
    """The requested model first, then its circuit-break fallback."""
    fallback = fallback_model(model)
    return [model, fallback] if fallback else [model]


def call(
    prompt: str,
    *,
    system: str = "",
    model: str = config.MODEL_DEFAULT,
    temperature: float = 0.2,
    max_tokens: int = 4096,
    attempts: int = 3,
) -> str:
    """One completion, with backoff. Returns raw text.

    Each candidate model gets its own circuit breaker and its own backoff;
    when the primary is open or exhausted, the legacy route takes the request
    rather than the caller seeing an error.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last: Exception | None = None
    opened = False
    for candidate in _candidates(model):
        circuit = _circuit_for(candidate)
        for attempt in range(attempts):
            try:
                circuit.before()
            except CircuitOpen:
                opened = True
                break
            try:
                started = time.time()
                response = client(candidate).chat.completions.create(
                    model=resolve_model(candidate),
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                # Attribute spend to the model that actually served: after a
                # fallback the price is the legacy route's, not the primary's.
                _record(candidate, getattr(response, "usage", None))
                text = response.choices[0].message.content or ""
                if not text.strip():
                    raise LLMError("model returned no answer (possibly exhausted reasoning budget)")
                circuit.ok()
                log.debug(
                    "llm %s%s ok in %dms (%d chars)",
                    candidate,
                    "" if candidate == model else f" (fallback for {model})",
                    int((time.time() - started) * 1000),
                    len(text),
                )
                return text
            except Exception as exc:  # noqa: BLE001 - gateway errors are varied
                last = exc
                circuit.fail()
                if _route_dead(exc):
                    log.warning("llm %s route unavailable, falling back: %s", candidate, exc)
                    break
                if attempt + 1 < attempts:
                    time.sleep(0.5 * (2**attempt))
                log.warning("llm %s attempt %d failed: %s", candidate, attempt + 1, exc)

    if opened and last is None:
        raise CircuitOpen("LLM gateway circuit is open")
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
    delta straight onto the SSE stream. A candidate that fails before its
    first delta hands the request to the fallback; once deltas have been
    forwarded there is no clean replay, so a mid-stream failure surfaces.
    """
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last: Exception | None = None
    opened = False
    for candidate in _candidates(model):
        circuit = _circuit_for(candidate)
        try:
            circuit.before()
        except CircuitOpen:
            opened = True
            continue
        parts: list[str] = []
        try:
            response = client(candidate).chat.completions.create(
                model=resolve_model(candidate),
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=True,
                # A streamed call reports its usage only in a final, choice-less
                # chunk, and only if asked. Without this the trace - the most
                # visible thing the product does - would be the one call whose
                # cost we never counted.
                stream_options={"include_usage": True},
            )
            for chunk in response:
                if getattr(chunk, "usage", None) is not None:
                    _record(candidate, chunk.usage)
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                piece = getattr(delta, "content", None)
                if piece:
                    parts.append(piece)
                    if on_delta is not None:
                        on_delta(piece)
            answer = "".join(parts)
            if not answer.strip():
                raise LLMError("model stream returned no answer")
            circuit.ok()
            return answer
        except CircuitOpen:
            raise
        except Exception as exc:  # noqa: BLE001
            circuit.fail()
            last = exc
            if parts:
                # Deltas already went to the caller; replaying them on another
                # model would duplicate the answer half-formed.
                break
            if _route_dead(exc):
                log.warning("llm stream %s route unavailable, falling back: %s", candidate, exc)
                continue
            log.warning(
                "llm stream %s%s failed before first delta: %s",
                candidate,
                "" if candidate == model else f" (fallback for {model})",
                exc,
            )

    if opened and last is None:
        raise CircuitOpen("LLM gateway circuit is open")
    raise LLMError(f"streaming call failed: {last}")


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
