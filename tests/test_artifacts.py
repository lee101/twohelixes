"""Coding-agent artifacts: the validation contract, the model allowlist, ownership,
the repair turn, and the headers that make the view route a security boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from twohelixes import artifacts, auth, config, llm, router, store
from twohelixes.routes import artifacts as routes

GOOD = (
    "<!doctype html><html><head><title>Sales &amp; Costs</title></head><body>"
    "<div id=a></div><script>document.getElementById('a').textContent='ok';</script>"
    "</body></html>"
)


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    store.init()
    yield
    store.close()
    store._initialised = False


def identity() -> auth.Identity:
    email = f"art-{store.new_id()}@test.local"
    user = store.create_user(email)
    return auth.Identity(user_id=user["id"], email=email, plan="free")


def context(method: str, path: str, user: auth.Identity, body: dict | None = None, **params: str):
    ctx = router.build_context(method, path, "", json.dumps(body or {}), "{}")
    ctx.user = user
    ctx.params = params
    return ctx


def fenced(source: str, reply: str = "Built it.") -> str:
    return f"{reply}\n```html\n{source}\n```"


# -- extraction and validation --------------------------------------------


def test_extract_takes_the_largest_fenced_block_and_the_reply():
    reply, source = artifacts.extract("Here.\n```html\n<b>x</b>\n```\nand\n```html\n" + GOOD + "\n```")
    assert source == GOOD and "Here." in reply and "```" not in reply


def test_extract_accepts_a_bare_document_and_reports_truncation_as_empty():
    assert artifacts.extract(GOOD) == ("", GOOD)
    reply, source = artifacts.extract("Sure!\n```html\n<html><body><script>var a=")
    assert source == ""


def test_title_is_unescaped_and_bounded():
    assert artifacts.title_of(GOOD) == "Sales & Costs"
    assert artifacts.title_of("<p>x</p>", "fallback") == "fallback"
    assert len(artifacts.title_of("<title>" + "t" * 999 + "</title>")) == artifacts.MAX_TITLE_CHARS


def test_a_clean_file_passes():
    assert artifacts.validate(GOOD) == []


@pytest.mark.parametrize(
    "snippet, fragment",
    [
        ('<script src="https://cdn.example/x.js"></script>', "remote <script src>"),
        ('<link rel="stylesheet" href="https://x/y.css">', "<link>"),
        ("<iframe src='about:blank'></iframe>", "nested frames"),
        ("<script>fetch('/v1/me')</script>", "no network"),
        ("<script>new WebSocket('wss://x')</script>", "no network"),
        ("<script>localStorage.setItem('a','b')</script>", "no storage"),
        ("<style>@import url(x.css);</style>", "@import"),
        ("<img src='//evil.example/p.png'>", "remote resources"),
        ("<div style=\"background:url(https://evil.example/a.png)\"></div>", "remote url()"),
        ("<form action='https://evil.example'></form>", "forms cannot submit"),
        ("<meta http-equiv='refresh' content='0;url=https://x'>", "meta refresh"),
        ("<base href='https://evil.example/'>", "<base>"),
    ],
)
def test_forbidden_constructs_are_named(snippet: str, fragment: str):
    problems = artifacts.validate(GOOD.replace("</body>", snippet + "</body>"))
    assert any(fragment in p for p in problems), problems


def test_data_uris_and_inline_svg_are_allowed():
    page = GOOD.replace("</body>", '<img src="data:image/png;base64,AAAA"><svg><rect/></svg></body>')
    assert artifacts.validate(page) == []


def test_empty_oversized_and_truncated_files_are_rejected():
    assert artifacts.validate("")
    assert any("larger" in p for p in artifacts.validate(GOOD + "<!--" + "x" * artifacts.MAX_SOURCE_BYTES + "-->"))
    assert any("unbalanced" in p for p in artifacts.validate("<html><body><script>var a = 1;"))


def test_a_js_syntax_error_is_reported_when_node_exists():
    import shutil

    if shutil.which("node") is None:
        pytest.skip("no JS engine to parse with")
    bad = GOOD.replace("textContent='ok'", "textContent=(")
    assert any("does not parse" in p for p in artifacts.validate(bad))


# -- the boundary ---------------------------------------------------------


def test_csp_blocks_network_and_isolates_origin():
    csp = artifacts.VIEW_HEADERS["Content-Security-Policy"]
    for needle in ("default-src 'none'", "connect-src 'none'", "form-action 'none'", "base-uri 'none'"):
        assert needle in csp
    # Without allow-same-origin the frame gets an opaque origin: no cookie access.
    assert "sandbox allow-scripts" in csp and "allow-same-origin" not in csp
    assert "http" not in csp


# -- model allowlist ------------------------------------------------------


def test_default_model_is_deepseek(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("TWOHELIXES_AGENT_MODEL", raising=False)
    assert config.resolve_agent_model(None) == "deepseek-v4-flash"
    assert config.resolve_agent_model("") == "deepseek-v4-flash"


def test_aliases_and_full_ids_resolve_and_nothing_else_does():
    assert config.resolve_agent_model("sonnet") == "claude-sonnet-5-5"
    assert config.resolve_agent_model("TERRA") == config.AGENT_MODEL_ALIASES["terra"]
    assert config.resolve_agent_model("claude-sonnet-5-5") == "claude-sonnet-5-5"
    for hostile in ("muse-spark-1.3", "gpt-5.6-sol", "../../etc", "contributor/x", 42, {"a": 1}):
        assert config.resolve_agent_model(hostile) == "deepseek-v4-flash"


def test_every_offered_model_is_priced():
    for model in config.AGENT_MODEL_ALIASES.values():
        assert model in llm.PRICES, f"{model} would be metered at the conservative default"


# -- routes ---------------------------------------------------------------


class FakeLLM:
    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append({"prompt": prompt, **kwargs})
        return self.replies.pop(0)


def test_create_view_edit_and_ownership(monkeypatch: pytest.MonkeyPatch):
    owner = identity()
    fake = FakeLLM(fenced(GOOD), fenced(GOOD.replace("ok", "changed")))
    monkeypatch.setattr(llm, "call", fake)

    made = routes.code_agent(context("POST", "/v1/code-agent", owner, {"prompt": "a page"}))
    assert made.status == 201
    art = made.body["artifact"]
    assert made.body["model"] == "deepseek-v4-flash" and fake.calls[0]["model"] == "deepseek-v4-flash"
    assert art["version"] == 1 and art["title"] == "Sales & Costs"

    shown = routes.view_artifact(context("GET", "x", owner, artifact_id=art["id"]))
    assert shown.status == 200 and shown.content_type.startswith("text/html")
    assert "default-src 'none'" in shown.headers["Content-Security-Policy"]
    assert shown.headers["Cache-Control"] == "private, no-store"

    edited = routes.code_agent(
        context("POST", "/v1/code-agent", owner, {"prompt": "make it say changed", "artifact_id": art["id"]})
    )
    assert edited.status == 200 and edited.body["artifact"]["version"] == 2
    assert GOOD in fake.calls[1]["prompt"], "an edit must send the current file"
    source = routes.get_artifact(context("GET", "x", owner, artifact_id=art["id"])).body["artifact"]["source"]
    assert "changed" in source

    stranger = identity()
    for handler in (routes.view_artifact, routes.get_artifact, routes.delete_artifact):
        assert handler(context("GET", "x", stranger, artifact_id=art["id"])).status == 404
    assert routes.code_agent(
        context("POST", "/v1/code-agent", stranger, {"prompt": "steal", "artifact_id": art["id"]})
    ).status == 404
    assert routes.list_artifacts(context("GET", "/v1/artifacts", stranger)).body["artifacts"] == []


def test_a_bad_first_file_gets_exactly_one_repair_turn(monkeypatch: pytest.MonkeyPatch):
    bad = GOOD.replace("</body>", "<script>fetch('/x')</script></body>")
    fake = FakeLLM(fenced(bad), fenced(GOOD))
    monkeypatch.setattr(llm, "call", fake)
    result = routes.code_agent(context("POST", "/v1/code-agent", identity(), {"prompt": "p", "model": "terra"}))
    assert result.status == 201 and len(fake.calls) == 2
    assert "no network" in fake.calls[1]["prompt"]
    assert result.body["notes"] and result.body["model"] == config.AGENT_MODEL_ALIASES["terra"]


def test_a_file_that_stays_invalid_is_not_saved_or_charged(monkeypatch: pytest.MonkeyPatch):
    bad = GOOD.replace("</body>", "<iframe></iframe></body>")
    fake = FakeLLM(fenced(bad), fenced(bad))
    monkeypatch.setattr(llm, "call", fake)
    user = identity()
    result = routes.code_agent(context("POST", "/v1/code-agent", user, {"prompt": "p"}))
    assert result.status == 422 and result.body["error"] == "artifact_invalid"
    assert len(fake.calls) == 2, "never more than one repair turn"
    assert routes.list_artifacts(context("GET", "/v1/artifacts", user)).body["artifacts"] == []


def test_model_outage_is_a_503_not_a_crash(monkeypatch: pytest.MonkeyPatch):
    def boom(*_a: Any, **_k: Any) -> str:
        raise llm.LLMError("down")

    monkeypatch.setattr(llm, "call", boom)
    assert routes.code_agent(context("POST", "/v1/code-agent", identity(), {"prompt": "p"})).status == 503


def test_input_limits():
    user = identity()
    assert routes.code_agent(context("POST", "/v1/code-agent", user, {})).status == 400
    huge = {"prompt": "x" * (artifacts.MAX_PROMPT_CHARS + 1)}
    assert routes.code_agent(context("POST", "/v1/code-agent", user, huge)).status == 400
