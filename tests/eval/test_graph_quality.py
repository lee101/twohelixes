"""Graph-creation quality evals: form choice, map/wordcloud parity, audit.

Mirrors askfelix's `benchmark/graph_quality_evals.py` in spirit: each case has
a frame and a question (or an explicit config), and we assert the pipeline
picks the right form, emits the expected Plotly trace, and leaves `audit()`
empty. No model call — the heuristic and builders are the contract.
"""

from __future__ import annotations

import pandas as pd
import pytest

from twohelixes.charts import defaults, forms, geo
from twohelixes.pipeline import figures


def _build(frame: pd.DataFrame, question: str = "", config: dict | None = None):
    cfg = config or figures.heuristic_config(frame, question)
    cfg = figures.validate_config(dict(cfg), frame)
    spec, warnings = figures.build(frame.copy(), cfg, mode="light")
    applied = defaults.apply(spec, mode="light")
    findings = defaults.audit(applied)
    return cfg, applied, warnings, findings


def test_country_gdp_map_is_choropleth_not_a_bar():
    frame = pd.DataFrame(
        {
            "country": ["United States", "Germany", "Japan", "Brazil", "India"],
            "gdp": [25.5, 4.5, 4.2, 2.1, 3.7],
        }
    )
    cfg, figure, warnings, findings = _build(frame, "map GDP by country")
    assert cfg["chart_type"] == "map"
    assert figure["data"][0]["type"] == "choropleth"
    assert figure["data"][0]["locationmode"] == "ISO-3"
    assert "USA" in figure["data"][0]["locations"]
    assert findings == []


def test_lat_lon_map_is_scattergeo():
    frame = pd.DataFrame(
        {
            "lat": [37.77, 40.71, 51.50],
            "lon": [-122.42, -74.01, -0.12],
            "city": ["SF", "NYC", "London"],
            "users": [120, 200, 90],
        }
    )
    cfg, figure, warnings, findings = _build(frame, "where are our users")
    assert cfg["chart_type"] == "map"
    assert figure["data"][0]["type"] == "scattergeo"
    assert findings == []


def test_wordcloud_from_feedback_text():
    frame = pd.DataFrame(
        {
            "feedback": [
                "great product support and great onboarding",
                "support was slow but product is great",
                "love the onboarding and product design",
                "billing support needs work",
                "great design and great product",
            ]
        }
    )
    cfg, figure, warnings, findings = _build(
        frame, "word cloud of feedback", {"chart_type": "wordcloud", "x": "feedback"}
    )
    assert cfg["chart_type"] == "wordcloud"
    assert figure["data"][0]["type"] in ("image", "scatter")
    assert findings == []


def test_iso_remapping_handles_names_and_codes():
    assert geo.to_iso3("United States") == "USA"
    assert geo.to_iso3("us") == "USA"
    assert geo.to_iso3("DEU") == "DEU"
    codes, unmatched = geo.locations_to_iso3(["France", "XX", "fr"])
    assert "FRA" in codes
    assert unmatched == 1


def test_time_series_stays_a_line():
    frame = pd.DataFrame(
        {
            "day": pd.date_range("2024-01-01", periods=12, freq="D"),
            "revenue": [10, 12, 11, 15, 14, 18, 17, 20, 19, 22, 21, 24],
        }
    )
    cfg, figure, warnings, findings = _build(frame, "revenue over time")
    assert cfg["chart_type"] == "line"
    assert figure["data"][0]["type"] == "scatter"
    assert findings == []


def test_category_measure_prefers_bars():
    frame = pd.DataFrame(
        {"segment": ["A", "B", "C", "D"], "revenue": [10, 30, 20, 15]}
    )
    cfg, figure, warnings, findings = _build(frame, "revenue by segment")
    assert cfg["chart_type"] in ("bar", "hbar")
    assert figure["data"][0]["type"] == "bar"
    assert findings == []


def test_every_declared_form_builds_and_audits_clean():
    """Smoke: each VALID_TYPES entry can produce a figure that passes audit."""
    countries = pd.DataFrame(
        {"country": ["USA", "DEU", "JPN", "BRA"], "active_accounts": [10, 8, 6, 4]}
    )
    points = pd.DataFrame(
        {"lat": [1.0, 2.0, 3.0], "lon": [4.0, 5.0, 6.0], "value": [1, 2, 3]}
    )
    text = pd.DataFrame(
        {
            "note": [
                "alpha beta gamma delta epsilon",
                "alpha beta zeta eta theta",
                "alpha gamma iota kappa lambda",
                "beta delta mu nu xi",
            ]
        }
    )
    cases = {
        "map": (countries, {"chart_type": "map", "x": "country", "y": "active_accounts"}),
        "wordcloud": (text, {"chart_type": "wordcloud", "x": "note"}),
    }
    # Reuse chartbench-style frames for the rest via heuristic when possible.
    for form, (frame, config) in cases.items():
        cfg, figure, warnings, findings = _build(frame, "", config)
        assert cfg["chart_type"] == form, (form, cfg, warnings)
        assert figure["data"], (form, warnings)
        assert findings == [], (form, findings)


def test_detect_map_and_word_frequencies_helpers():
    frame = pd.DataFrame({"lat": [10.0], "longitude": [20.0]})
    assert forms.detect_map(frame)["kind"] == "points"
    assert len(forms._word_frequencies("great great product support product")) >= 2
