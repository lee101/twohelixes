"""Broad contract tests for search, transformation, and chart construction."""

from __future__ import annotations

import pandas as pd
import pytest

from twohelixes.pipeline import figures, semantic, transform
from twohelixes.routes import query


@pytest.fixture
def sales() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2024-01-01", "2024-01-15", "2024-02-01", None]),
        "region": ["North", "South", "North", "West"],
        "product": ["Alpha", "Beta", "Alpine", None],
        "revenue": [10.0, 20.0, 30.0, None],
        "cost": [4.0, 8.0, 9.0, 1.0],
    })


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("gt", 15, [20.0, 30.0]),
        ("in", ["North", "West"], ["North", "North", "West"]),
        ("not_in", ["North", "West"], ["South"]),
        ("contains", "alp", ["Alpha", "Alpine"]),
        ("starts_with", "Al", ["Alpha", "Alpine"]),
        ("ends_with", "ta", ["Beta"]),
        ("is_null", None, [None]),
        ("not_null", None, ["Alpha", "Beta", "Alpine"]),
    ],
)
def test_filter_operator_families(sales, op, value, expected):
    column = "revenue" if op == "gt" else ("region" if op in {"in", "not_in"} else "product")
    result, notes = transform.apply(
        sales, [transform.Step("filter", {"column": column, "op": op, "value": value}, id="f")]
    )
    if op == "is_null":
        assert result[column].isna().all() and len(result) == 1
    else:
        assert result[column].tolist() == expected
    assert notes[0].startswith("f: filter 4 ->")


def test_full_transform_pipeline_covers_common_query_shapes(sales):
    steps = transform.normalise([
        {"type": "derive", "params": {"as": "profit", "expr": "revenue - cost"}},
        {"type": "dropna", "params": {"columns": ["profit"]}},
        {"type": "aggregate", "params": {"by": ["region"], "metrics": [
            {"column": "revenue", "agg": "sum", "as": "sales"},
            {"column": "profit", "agg": "mean", "as": "margin"},
        ]}},
        {"type": "sort", "params": {"column": "sales", "desc": True}},
        {"type": "rename", "params": {"map": {"region": "market"}}},
        {"type": "select", "params": {"columns": ["market", "sales", "margin"]}},
        {"type": "limit", "params": {"n": 1}},
        {"type": "filter", "enabled": False, "params": {"column": "missing"}},
    ])
    assert transform.validate(steps, list(sales.columns)) == []
    result, notes = transform.apply(sales, steps)
    assert result.to_dict("records") == [{"market": "North", "sales": 40.0, "margin": 13.5}]
    assert notes[-1].endswith("skipped")
    source = transform.to_python(steps, "sales")
    assert "groupby" in source and "sort_values" in source and "disabled" in source


def test_resample_pivot_and_top_n_transform_shapes(sales):
    monthly, _ = transform.apply(sales, [transform.Step("resample", {
        "time_column": "date", "grain": "month", "by": ["region"],
        "metrics": [{"column": "revenue", "agg": "sum", "as": "sales"}],
    })])
    assert list(monthly.columns) == ["date", "region", "sales"]
    assert len(monthly) == 3

    pivoted, _ = transform.apply(monthly, [transform.Step("pivot", {
        "index": "date", "columns": "region", "values": "sales", "agg": "sum",
    })])
    assert {"date", "North", "South"}.issubset(map(str, pivoted.columns))

    ranked, _ = transform.apply(sales.dropna(subset=["revenue"]), [transform.Step("top_n", {
        "category": "region", "measure": "revenue", "n": 1,
    })])
    assert ranked["region"].tolist() == ["North", "Other"]


@pytest.mark.parametrize(
    "step,error",
    [
        ({"type": "filter", "params": {"column": "region", "op": "regex"}}, "unsupported operator"),
        ({"type": "aggregate", "params": {"by": [], "metrics": []}}, "one metric"),
        ({"type": "derive", "params": {"as": "x", "expr": "__import__('os')"}}, "not allowed"),
        ({"type": "limit", "params": {"n": 0}}, "positive"),
        ({"type": "resample", "params": {"time_column": "date", "grain": "decade", "metrics": []}}, "unknown grain"),
    ],
)
def test_invalid_transform_plans_are_actionable(step, error):
    normalised = transform.normalise([step])
    problems = transform.validate(normalised, ["date", "region"])
    assert error in problems[0]["error"]


@pytest.mark.parametrize(
    ("chart_type", "config", "trace_type"),
    [
        ("line", {"x": "date", "y": "revenue"}, "scatter"),
        ("area", {"x": "date", "y": "revenue"}, "scatter"),
        ("bar", {"x": "region", "y": "revenue"}, "bar"),
        ("hbar", {"x": "region", "y": "revenue"}, "bar"),
        ("scatter", {"x": "cost", "y": "revenue", "size": "revenue"}, "scatter"),
        ("pie", {"x": "region", "y": "revenue"}, "pie"),
        ("histogram", {"x": "revenue"}, "histogram"),
        ("stat", {"y": "revenue"}, "indicator"),
        ("table", {}, "table"),
    ],
)
def test_core_chart_type_matrix(sales, chart_type, config, trace_type):
    figure, _ = figures.build(sales, {"chart_type": chart_type, **config})
    assert figure["data"]
    assert figure["data"][0]["type"] == trace_type


def test_heatmap_candlestick_and_wide_query_results():
    heat = pd.DataFrame({"day": ["M", "M", "T"], "hour": [9, 10, 9], "value": [1, 2, 3]})
    figure, _ = figures.build(heat, {"chart_type": "heatmap", "x": "hour", "y": "day", "z": "value"})
    assert figure["data"][0]["z"][0] == [1.0, 2.0]
    assert figure["data"][0]["z"][1][0] == 3.0
    assert pd.isna(figure["data"][0]["z"][1][1])

    ohlc = pd.DataFrame({
        "time": pd.date_range("2024-01-01", periods=2), "Open": [10, 11],
        "High": [12, 13], "Low": [9, 10], "Close": [11, 12],
    })
    candle, warnings = figures.build(ohlc, {"chart_type": "candlestick", "x": "time"})
    assert candle["data"][0]["type"] == "candlestick"
    assert warnings == []

    wide = pd.DataFrame({"month": ["Jan", "Feb"], "North": [1, 2], "South": [3, 4]})
    grouped, _ = figures.build(wide, {"chart_type": "line", "x": "month", "y": ["North", "South"]})
    assert [trace["name"] for trace in grouped["data"]] == ["North", "South"]


def test_search_handles_plural_schema_terms_and_selects_close_matches(monkeypatch):
    rows = [
        {"id": "orders", "name": "Orders", "description": "Commerce", "columns": '["regions", "revenues"]'},
        {"id": "tickets", "name": "Tickets", "description": "Support", "columns": '["topic", "wait_time"]'},
        {"id": "archive", "name": "Archive", "description": "Historic sales", "columns": '["region", "revenue"]'},
    ]
    monkeypatch.setattr(semantic, "rank_documents", lambda _q, docs: [(key, 0.1) for key in docs])
    selected = query._select_datasets("show revenue by region", rows)
    assert {row["id"] for row in selected} == {"orders", "archive"}
    assert query._terms("Orders, ordered and REGIONS") == {"order", "ordered", "region"}


def test_search_single_dataset_and_missing_question_behaviour():
    row = {"id": "one", "name": "Only", "columns": "not-json"}
    assert query._select_datasets("", [row]) == [row]
    assert query._dataset_columns(row) == []
    with pytest.raises(query.DataSelectionError) as exc:
        query._select_datasets("", [row, {**row, "id": "two"}])
    assert exc.value.code == "dataset_not_selected"
