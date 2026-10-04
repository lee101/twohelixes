from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from twohelixes import config, llm
from twohelixes.pipeline import semantic


def test_batched_cpu_scores_preserve_cosine_and_document_max(monkeypatch):
    rng = np.random.default_rng(52)
    vectors = rng.normal(size=(2201, 32)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    lookup = {str(i): v for i, v in enumerate(vectors)}
    monkeypatch.setattr(semantic, "available", lambda: True)
    monkeypatch.setattr(semantic, "embed", lambda text: lookup[text])
    scores = semantic._score_phrases(vectors[0], list(lookup))
    np.testing.assert_allclose([v for _, v in scores], vectors @ vectors[0], atol=1e-6)
    documents = {"a": ["3", "4", "4"], "b": ["0", "1"], "empty": []}
    ranked = semantic.rank_documents("0", documents)
    assert [name for name, _ in ranked] == ["b", "a", "empty"]
    assert dict(ranked)["a"] == pytest.approx(max(vectors[3] @ vectors[0], vectors[4] @ vectors[0]), abs=1e-6)
    assert dict(ranked)["empty"] == -1


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "openpaths/free"])
def test_fallback_uses_luna_low_for_calls_and_streams(monkeypatch, model):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if kwargs["model"] == model:
            raise RuntimeError("429 route unavailable")
        if kwargs.get("stream"):
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok"))])])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(llm, "_circuits", {})
    monkeypatch.setattr(llm, "client", lambda _: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm, "resolve_model", lambda model: model)
    assert llm.call("question", model=model, attempts=1) == "ok"
    assert llm.stream("question", model=model) == "ok"
    assert [c["model"] for c in calls] == (
        [model, "gpt-6-luna", "gpt-6-luna"] if model == "openpaths/free"
        else [model, "gpt-6-luna", model, "gpt-6-luna"]
    )
    for call in (c for c in calls if c["model"] == "gpt-6-luna"):
        assert call["reasoning_effort"] == "low"
        assert "max_completion_tokens" in call
        assert "temperature" not in call


def test_luna_direct_provider_is_independent_of_gateway(monkeypatch):
    import sys
    factory = Mock()
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
    monkeypatch.setattr(config, "get", lambda key: "test-openai" if key == "OPENAI_API_KEY" else "")
    monkeypatch.setattr(config, "using_gateway", lambda: True)
    monkeypatch.setattr(llm, "_direct_openai_client", None)
    assert llm.client("gpt-6-luna") is factory.return_value
    assert factory.call_args.kwargs["base_url"] == "https://api.openai.com/v1"


def test_circuit_allows_only_one_recovery_probe(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(llm.time, "time", lambda: now[0])
    circuit = llm.Circuit(threshold=1, cooldown=30)
    circuit.fail()
    with pytest.raises(llm.CircuitOpen):
        circuit.before()
    now[0] = 32
    circuit.before()
    with pytest.raises(llm.CircuitOpen):
        circuit.before()
    circuit.ok()
    circuit.before()


def test_invalid_direct_openai_key_recovers_through_gateway_luna(monkeypatch):
    calls = []
    def client(route):
        def create(**kwargs):
            calls.append((route, kwargs["model"]))
            if route == "gpt-6-luna":
                raise RuntimeError("401 invalid credential")
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="gateway answer"))])
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "client", client)
    monkeypatch.setattr(llm, "_circuits", {})
    monkeypatch.setattr(config, "get", lambda key: "key")
    monkeypatch.setattr(config, "using_gateway", lambda: True)
    assert llm.call("question", model="gpt-6-luna", attempts=1) == "gateway answer"
    assert calls == [("gpt-6-luna", "gpt-6-luna"), ("openpaths:gpt-6-luna", "gpt-6-luna")]
