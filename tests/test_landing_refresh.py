"""Regression checks for honest marketing, model migration, and graph assets."""

from datetime import date
from html.parser import HTMLParser
import json
import runpy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from twohelixes import config, llm, router
from twohelixes.charts import defaults
from twohelixes.datasets import samples
from twohelixes.pipeline import figures
from twohelixes.routes import pages, showcase, static_files


class Links(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.links = []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def test_primary_cta_reaches_a_real_sample_without_a_signup_gate(monkeypatch):
    monkeypatch.setattr(config, "stripe_publishable_key", lambda: "")
    body = pages.home(router.Context("GET", "/", "", "", {})).body
    links = Links(body).links
    cta = next(link for link in links if link.get("data-conversion") == "hero-sample")
    assert "data-action" not in cta
    target = urlsplit(cta["href"])
    assert target.path == "/app"
    query = parse_qs(target.query)
    assert query["sample"] == ["orders"]
    assert "net revenue" in query["q"][0]
    assert "Sample analysis" in body
    assert "SQL, sheets, and Python notebooks" in body
    assert "100%</div>" not in body
    assert "2.4 s" not in body


def test_showcase_findings_match_the_plotted_numbers():
    revenue, _ = showcase.BUILDERS["revenue"]()
    _, south, east = revenue["data"]
    crossing = next(i for i, (a, b) in enumerate(zip(east["y"], south["y"])) if a > b)
    month = date.fromisoformat(east["x"][crossing]).strftime("%B")
    assert revenue["layout"]["title"]["text"] == f"East overtook South in {month}"
    channels, _ = showcase.BUILDERS["channels"]()
    values = channels["data"][0]["y"]
    assert f"{values[0] / sum(values):.0%}" in channels["layout"]["title"]["text"]
    latency, _ = showcase.BUILDERS["latency"]()
    counts = latency["data"][0]["y"]
    assert f"{sum(counts[:3]) / sum(counts):.0%}" in latency["layout"]["title"]["text"]
    retention, _ = showcase.BUILDERS["retention"]()
    values = retention["data"][0]["y"]
    assert retention["layout"]["title"]["text"] == f"Retention reaches {values[-1]}% at week {len(values)}"


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("name", list(showcase.BUILDERS))
def test_every_homepage_chart_passes_audit(name, mode):
    spec, kind = showcase.BUILDERS[name]()
    assert defaults.audit(defaults.apply(spec, mode=mode, chart_type=kind), mode=mode) == []
    assert showcase.chart_svg(name, mode).startswith("<svg")


def test_nested_map_asset_is_routable_and_stays_under_static_root():
    router.build()
    status, content_type, _, body = router.handle(
        "GET", "/static/libs/topojson/world_110m.json", "", "", "{}"
    )
    assert status == "200"
    assert content_type.startswith("application/json")
    assert json.loads(body)["type"] == "Topology"
    assert static_files._resolve("../../../.env") is None


def test_model_defaults_and_contributor_consent(monkeypatch):
    for key in ("TWOHELIXES_MODEL", "TWOHELIXES_MODEL_FAST", "TWOHELIXES_MODEL_MINI",
                "TWOHELIXES_MODEL_DEEP", "TWOHELIXES_ALLOW_CONTRIBUTOR"):
        monkeypatch.delenv(key, raising=False)
    values = runpy.run_path(config.__file__)
    assert values["MODEL_PRIMARY"] == "muse-spark-1.3"
    assert values["MODEL_MINI"] == "deepseek-v4-flash"
    monkeypatch.setenv("TWOHELIXES_MODEL", "muse-spark-1.3-contributor")
    with pytest.raises(ValueError, match="Contributor"):
        runpy.run_path(config.__file__)
    monkeypatch.setenv("TWOHELIXES_ALLOW_CONTRIBUTOR", "1")
    assert runpy.run_path(config.__file__)["MODEL_PRIMARY"] == "muse-spark-1.3-contributor"


def test_empty_reasoning_completion_falls_back_and_accounts_for_both_calls(monkeypatch):
    called = []

    def create(**kwargs):
        called.append(kwargs["model"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None if len(called) == 1 else '{"ok":true}'))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )

    monkeypatch.setattr(llm, "client", lambda *_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm, "resolve_model", lambda model: model)
    monkeypatch.setattr(llm, "_circuits", {})
    with llm.measure() as spend:
        assert llm.call("test", model=config.MODEL_PRIMARY, attempts=1) == '{"ok":true}'
    assert called == [config.MODEL_PRIMARY, "deepseek-v4-flash"]
    assert spend.calls == 2
    assert all("contributor" not in model for model in llm.MODEL_FALLBACKS.values())


def test_empty_stream_falls_back_before_emitting_any_content(monkeypatch):
    called = []

    def create(**kwargs):
        called.append(kwargs["model"])
        piece = None if len(called) == 1 else "A real answer"
        return iter([SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=piece))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20),
        )])

    monkeypatch.setattr(llm, "client", lambda *_: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(llm, "resolve_model", lambda model: model)
    monkeypatch.setattr(llm, "_circuits", {})
    deltas = []
    with llm.measure() as spend:
        assert llm.stream("test", model=config.MODEL_PRIMARY, on_delta=deltas.append) == "A real answer"
    assert deltas == ["A real answer"]
    assert spend.calls == 2


def test_sample_is_published_only_after_parquet_write_completes(monkeypatch, tmp_path):
    import pandas as pd

    target = tmp_path / "tiny.parquet"
    original = pd.DataFrame.to_parquet

    def write(frame, path, **kwargs):
        assert path != target
        assert not target.exists()
        original(frame, path, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", write)
    monkeypatch.setattr(samples, "path_for", lambda _: target)
    monkeypatch.setattr(samples, "SAMPLES", [samples.Sample(
        "tiny", "Tiny", "Test data", lambda: pd.DataFrame({"value": [1, 2]})
    )])
    assert samples.materialise()["tiny"]["rows"] == 2
    assert pd.read_parquet(target)["value"].tolist() == [1, 2]
    assert list(tmp_path.iterdir()) == [target]


def test_time_series_title_may_name_its_color_grouping():
    import pandas as pd

    frame = pd.DataFrame({
        "month": pd.date_range("2026-01-01", periods=3, freq="MS"),
        "region": ["East", "West", "East"], "revenue": [10, 20, 30],
        "channel": ["Organic", "Paid", "Organic"],
    })
    cfg = {"chart_type": "line", "x": "month", "y": "revenue",
           "color": "region", "title": "Revenue over time by Region"}
    assert figures.validate_config(dict(cfg), frame)["title"] == cfg["title"]
    cfg["title"] = "Revenue by Channel"
    assert figures.validate_config(cfg, frame)["title"] != "Revenue by Channel"
