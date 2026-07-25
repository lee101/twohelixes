"""Chart rules are the product's promise, so they get the most tests."""

from __future__ import annotations

import pandas as pd
import pytest

from twohelixes.charts import defaults, palette, svg
from twohelixes.interpreter import tools
from twohelixes.pipeline import figures


@pytest.fixture
def timeseries_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "order_date": pd.date_range("2024-01-01", periods=90),
            "revenue": range(90),
            "order_id": range(5000, 5090),
            "region": ["North", "South", "East"] * 30,
        }
    )


# -- palette ---------------------------------------------------------------


def test_palette_never_cycles():
    """A 9th series must be the neutral Other colour, not slot 1 again."""
    used = {palette.series_color(i) for i in range(palette.MAX_SERIES)}
    assert len(used) == palette.MAX_SERIES
    assert palette.series_color(palette.MAX_SERIES) == palette.OTHER_COLOR_LIGHT
    assert palette.series_color(99) == palette.OTHER_COLOR_LIGHT


def test_all_pairs_forms_cap_at_three():
    for form in ("scatter", "bubble", "choropleth", "small_multiples"):
        assert palette.max_series_for(form) == 3
    assert palette.max_series_for("bar") == palette.MAX_SERIES


def test_sequential_is_monotone_single_hue():
    ramp = palette.sequential(7)
    assert len(ramp) == 7
    assert len(set(ramp)) == 7
    # Light to dark: the last step must be darker than the first.
    assert sum(palette._to_rgb(ramp[0])) > sum(palette._to_rgb(ramp[-1]))


def test_diverging_has_neutral_midpoint():
    ramp = palette.diverging(9)
    assert len(ramp) == 9
    assert ramp[4] == palette.DIVERGING_MID_LIGHT
    assert ramp[0] == palette.DIVERGING_LOW
    assert ramp[-1] == palette.DIVERGING_HIGH


def test_dark_mode_is_its_own_steps():
    assert palette.categorical("dark") != palette.categorical("light")
    assert len(palette.categorical("dark")) == len(palette.categorical("light"))


# -- defaults --------------------------------------------------------------


def test_ninth_series_folds_into_other():
    traces = [
        {"type": "bar", "x": ["a", "b"], "y": [1, 2], "name": f"s{i}"}
        for i in range(11)
    ]
    figure = defaults.apply({"data": traces, "layout": {}}, chart_type="bar")

    names = [t.get("name") for t in figure["data"]]
    assert names[-1] == palette.OTHER_LABEL
    assert len(figure["data"]) == palette.MAX_SERIES + 1

    colors = [t["_color"] for t in figure["data"][:-1]]
    assert len(set(colors)) == len(colors)


def test_dual_axis_is_stripped():
    figure = defaults.apply(
        {
            "data": [
                {"type": "scatter", "x": [1], "y": [2], "name": "a"},
                {"type": "scatter", "x": [1], "y": [900000], "name": "b", "yaxis": "y2"},
            ],
            "layout": {"yaxis2": {"title": "second"}},
        },
        chart_type="line",
    )
    assert "yaxis2" not in figure["layout"]
    assert all(t.get("yaxis") != "y2" for t in figure["data"])
    assert defaults.audit(figure) == []


def test_line_charts_get_a_crosshair_and_bars_do_not():
    line = defaults.apply(
        {"data": [{"type": "scatter", "mode": "lines", "x": [1], "y": [2]}], "layout": {}},
        chart_type="line",
    )
    assert line["layout"]["hovermode"] == "x unified"

    bar = defaults.apply(
        {"data": [{"type": "bar", "x": ["a"], "y": [2]}], "layout": {}},
        chart_type="bar",
    )
    assert bar["layout"]["hovermode"] == "closest"


def test_legend_appears_only_from_two_series():
    one = defaults.apply(
        {"data": [{"type": "bar", "x": ["a"], "y": [1], "name": "only"}], "layout": {}},
        chart_type="bar",
    )
    assert one["layout"]["showlegend"] is False

    two = defaults.apply(
        {
            "data": [
                {"type": "bar", "x": ["a"], "y": [1], "name": "a"},
                {"type": "bar", "x": ["a"], "y": [2], "name": "b"},
            ],
            "layout": {},
        },
        chart_type="bar",
    )
    assert two["layout"]["showlegend"] is True


def test_bar_marks_carry_the_surface_spacer():
    figure = defaults.apply(
        {"data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}], "layout": {}},
        chart_type="bar",
    )
    marker = figure["data"][0]["marker"]
    assert marker["cornerradius"] == defaults.BAR_CORNER_RADIUS
    assert marker["line"]["width"] == defaults.SPACER


def test_audit_flags_a_one_bar_chart():
    figure = defaults.apply(
        {"data": [{"type": "bar", "x": ["only"], "y": [42]}], "layout": {}},
        chart_type="bar",
    )
    assert any(f["code"] == "one_bar" for f in defaults.audit(figure))


def test_audit_flags_a_crowded_pie():
    figure = defaults.apply(
        {
            "data": [
                {
                    "type": "pie",
                    "labels": [f"s{i}" for i in range(9)],
                    "values": list(range(9)),
                }
            ],
            "layout": {},
        },
        chart_type="pie",
    )
    assert any(f["code"] == "crowded_pie" for f in defaults.audit(figure))


# -- configuration repair --------------------------------------------------


def test_unknown_columns_are_dropped(timeseries_frame):
    config = figures.validate_config(
        {"chart_type": "line", "x": "order_date", "y": "nonexistent"},
        timeseries_frame,
    )
    assert config["y"] != "nonexistent"


def test_two_slice_pie_becomes_a_stat(timeseries_frame):
    frame = pd.DataFrame({"label": ["a", "b"], "value": [1, 2]})
    config = figures.validate_config(
        {"chart_type": "pie", "x": "label", "y": "value"}, frame
    )
    assert config["chart_type"] == "stat"


def test_many_time_buckets_become_a_line(timeseries_frame):
    config = figures.validate_config(
        {"chart_type": "bar", "x": "order_date", "y": "revenue"}, timeseries_frame
    )
    assert config["chart_type"] == "line"


def test_long_labels_flip_to_horizontal_bars():
    frame = pd.DataFrame(
        {
            "category": ["a very long category label indeed", "another lengthy one"],
            "value": [3, 4],
        }
    )
    config = figures.validate_config(
        {"chart_type": "bar", "x": "category", "y": "value"}, frame
    )
    assert config["chart_type"] == "hbar"


def test_mismatched_scales_do_not_share_an_axis():
    frame = pd.DataFrame({"x": [1, 2], "small": [3, 4], "huge": [900000, 950000]})
    config = figures.validate_config(
        {"chart_type": "line", "x": "x", "y": ["small", "huge"]}, frame
    )
    assert config["y"] == "small"


def test_heuristic_config_needs_no_model(timeseries_frame):
    config = figures.heuristic_config(timeseries_frame, "")
    assert config["chart_type"] == "line"
    assert config["x"] == "order_date"
    assert config["y"] == "revenue"
    # order_id is an identifier, not a measure.
    assert config["y"] != "order_id"


def test_bar_tail_folds_rather_than_truncating():
    frame = pd.DataFrame(
        {"category": [f"c{i}" for i in range(60)], "value": range(60)}
    )
    figure, warnings = figures.build(
        frame, {"chart_type": "bar", "x": "category", "y": "value"}
    )
    labels = figure["data"][0]["x"]
    assert palette.OTHER_LABEL in [str(v) for v in labels]
    assert any("Other" in w for w in warnings)


# -- column roles ----------------------------------------------------------


def test_unique_integers_are_measures_not_identifiers(timeseries_frame):
    assert tools.column_role(timeseries_frame, "revenue") == "measure"
    assert tools.column_role(timeseries_frame, "order_id") == "identifier"
    assert tools.column_role(timeseries_frame, "order_date") == "time"
    assert tools.column_role(timeseries_frame, "region") == "category"


def test_resample_never_goes_finer_than_the_data():
    """Resampling may coarsen toward the point target, never refine.

    The bug this guards: 40 daily rows spanning 39 days used to resample
    hourly, turning a clean daily line into ~900 buckets that were mostly
    empty.
    """
    sparse = pd.DataFrame(
        {"d": pd.date_range("2024-01-01", periods=40), "v": range(40)}
    )
    assert len(tools.timeseries(sparse, "d", "v")) == 40

    # More points than the target: coarsening is expected and correct.
    dense = pd.DataFrame(
        {"d": pd.date_range("2024-01-01", periods=400), "v": range(400)}
    )
    assert len(tools.timeseries(dense, "d", "v")) < 400

    # Sub-daily data keeps its own granularity rather than being flattened.
    hourly = pd.DataFrame(
        {"d": pd.date_range("2024-01-01", periods=48, freq="h"), "v": range(48)}
    )
    assert len(tools.timeseries(hourly, "d", "v")) == 48


def test_currency_strings_coerce():
    series = pd.Series(["$1,234.50", "(120)", "45%", "7"])
    numbers = tools.to_number(series)
    assert numbers.iloc[0] == pytest.approx(1234.50)
    assert numbers.iloc[1] == pytest.approx(-120.0)
    assert numbers.iloc[2] == pytest.approx(0.45)


def test_join_keys_rank_by_value_overlap_not_name():
    left = pd.DataFrame({"id": [1, 2, 3], "code": ["x", "y", "z"]})
    right = pd.DataFrame({"id": [90, 91, 92], "code": ["x", "y", "z"]})
    candidates = tools.suggest_join_keys(left, right)
    assert candidates[0]["left"] == "code"


def test_top_n_keeps_the_total():
    frame = pd.DataFrame({"cat": [f"c{i}" for i in range(30)], "v": [1] * 30})
    result = tools.top_n(frame, "cat", "v", n=5)
    assert result["v"].sum() == 30


# -- SVG export ------------------------------------------------------------


def test_native_svg_renders_without_a_browser():
    figure = defaults.apply(
        {
            "data": [
                {"type": "bar", "x": ["a", "b", "c"], "y": [3, 1, 2], "name": "s"}
            ],
            "layout": {"title": {"text": "Test"}},
        },
        chart_type="bar",
    )
    markup = svg.render(figure, width=600, height=360)
    assert markup.startswith("<svg")
    assert markup.endswith("</svg>")
    assert "<rect" in markup
    assert "Test" in markup


def test_svg_escapes_titles():
    figure = {
        "data": [{"type": "bar", "x": ["a"], "y": [1]}],
        "layout": {"title": {"text": "<script>alert(1)</script>"}},
    }
    markup = svg.render(figure)
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_unsupported_form_raises_for_the_fallback():
    with pytest.raises(svg.Unsupported):
        svg.render({"data": [{"type": "sankey"}], "layout": {}})


# -- column-role regressions -----------------------------------------------


def test_width_is_not_a_date_column():
    """`width` contains the substring "dt".

    Substring hint matching once classified `sepal_width` as time, and
    pandas confirmed it because `to_datetime` reads a float as an offset from
    the epoch. Iris was charted as a time series.
    """
    frame = pd.DataFrame(
        {
            "sepal_width_cm": [3.5, 3.0, 3.2, 3.1],
            "petal_depth_cm": [1.4, 1.5, 1.3, 1.6],
            "species": ["a", "b", "a", "b"],
        }
    )
    assert tools.column_role(frame, "sepal_width_cm") == "measure"
    assert tools.column_role(frame, "petal_depth_cm") == "measure"
    assert tools.find_time_column(frame) is None


def test_numeric_columns_are_never_dates_even_when_named_like_one():
    frame = pd.DataFrame({"year_value": [1.5, 2.5, 3.5], "n": [1, 2, 3]})
    assert not tools.looks_like_dates(frame["year_value"])
    assert tools.column_role(frame, "year_value") == "measure"


def test_real_date_columns_still_detected():
    frame = pd.DataFrame(
        {
            "order_date": pd.date_range("2024-01-01", periods=5),
            "created_at": ["2024-01-01", "2024-01-02", "2024-01-03",
                           "2024-01-04", "2024-01-05"],
            "amount": [1, 2, 3, 4, 5],
        }
    )
    assert tools.column_role(frame, "order_date") == "time"
    assert tools.column_role(frame, "created_at") == "time"
    assert tools.find_time_column(frame) == "order_date"


# -- sample datasets -------------------------------------------------------


def test_every_sample_builds_and_charts_cleanly():
    """The samples are a new user's first experience; a broken one is fatal."""
    from twohelixes.datasets import samples

    built = samples.materialise()
    assert len(built) == len(samples.SAMPLES)

    for sample in samples.SAMPLES:
        entry = built[sample.key]
        assert "error" not in entry, f"{sample.key}: {entry.get('error')}"
        assert entry["rows"] > 0, f"{sample.key} is empty"

        frame = samples.frame(sample.key)
        config = figures.validate_config(figures.heuristic_config(frame, ""), frame)
        figure, _ = figures.build(frame, config)
        figure = defaults.apply(figure, chart_type=config["chart_type"])
        assert defaults.audit(figure) == [], f"{sample.key} chart has findings"


def test_samples_are_queryable_with_sql():
    from twohelixes.connectors.document_sources import FilesConnector
    from twohelixes.datasets import samples

    samples.materialise()
    connector = FilesConnector({"root": str(samples.storage_dir())})
    assert connector.connect(), connector.error

    names = {t.name for t in connector.tables()}
    for sample in samples.SAMPLES:
        assert sample.key in names, f"{sample.key} is not a DuckDB view"
        result = connector.execute(f'SELECT count(*) AS n FROM "{sample.key}"')
        assert result.rows[0][0] > 0


def test_sample_sql_is_read_only():
    from twohelixes.connectors.base import UnsafeQuery
    from twohelixes.connectors.document_sources import FilesConnector
    from twohelixes.datasets import samples

    connector = FilesConnector({"root": str(samples.storage_dir())})
    connector.connect()
    with pytest.raises(UnsafeQuery):
        connector.execute("DROP VIEW iris")


# -- R2 presigning ---------------------------------------------------------


def test_presigned_url_shape(monkeypatch):
    """Signature correctness needs a live bucket; shape can be checked here."""
    from twohelixes.storage import r2

    monkeypatch.setattr(r2, "credentials", lambda: ("AKIATEST", "secrettest"))
    monkeypatch.setattr(r2, "endpoint", lambda: "https://acct.r2.cloudflarestorage.com")
    monkeypatch.setattr(r2, "bucket", lambda: "twohelixesstatic")

    url = r2.presign("uploads/u1/file.csv", "PUT", 900)
    assert url.startswith("https://acct.r2.cloudflarestorage.com/twohelixesstatic/")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=900" in url


def test_upload_keys_are_namespaced_per_user():
    """A guessed key must not reach another account's object."""
    from twohelixes.storage import r2

    key = r2.object_key("user-abc", "../../etc/passwd")
    assert key.startswith("uploads/user-abc/")
    assert ".." not in key
    assert "/etc/" not in key


def test_upload_ticket_rejects_bad_types_and_sizes(monkeypatch):
    from twohelixes.storage import r2

    monkeypatch.setattr(r2, "credentials", lambda: ("AKIATEST", "secrettest"))
    monkeypatch.setattr(r2, "endpoint", lambda: "https://acct.r2.cloudflarestorage.com")

    with pytest.raises(r2.R2Error):
        r2.upload_ticket("u1", "x.exe", "application/x-msdownload", 10)
    with pytest.raises(r2.R2Error):
        r2.upload_ticket("u1", "x.csv", "text/csv", 0)
    with pytest.raises(r2.R2Error):
        r2.upload_ticket("u1", "x.csv", "text/csv", r2.MAX_UPLOAD_BYTES + 1)

    ticket = r2.upload_ticket("u1", "data.csv", "text/csv", 1024)
    assert ticket["method"] == "PUT"
    assert ticket["key"].startswith("uploads/u1/")
