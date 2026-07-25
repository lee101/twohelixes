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


@pytest.fixture
def pivoted_frame() -> pd.DataFrame:
    """What the transform stage leaves behind when it pivots by a category."""
    return pd.DataFrame(
        {
            "order_date": pd.to_datetime(["2024-01-15", "2024-02-15", "2024-03-15"]),
            "East": [400, 650, 980],
            "North": [1200, 1500, 1800],
            "South": [900, 850, 700],
        }
    )


def test_pivoted_frame_draws_one_series_per_column(pivoted_frame):
    """A list-valued y used to index the frame by str(list) and draw nothing."""
    for chart_type in ("line", "bar", "area", "hbar"):
        figure, _ = figures.build(
            pivoted_frame,
            {
                "chart_type": chart_type,
                "x": "order_date",
                "y": ["East", "North", "South"],
            },
        )
        traces = figure["data"]
        assert len(traces) == 3, chart_type
        assert [t["name"] for t in traces] == ["East", "North", "South"]
        # Slots are pinned in the order asked for, never cycled.
        assert [t["_series_index"] for t in traces] == [0, 1, 2]
        values = "x" if chart_type == "hbar" else "y"
        assert all(len(t[values]) == 3 for t in traces), chart_type


def test_pivoted_tail_folds_into_other():
    frame = pd.DataFrame({"day": range(4)} | {f"m{i}": [i] * 4 for i in range(12)})
    figure, warnings = figures.build(
        frame,
        {"chart_type": "line", "x": "day", "y": [f"m{i}" for i in range(12)]},
    )
    names = [t["name"] for t in figure["data"]]
    assert len(names) == palette.MAX_SERIES + 1
    assert names[-1] == palette.OTHER_LABEL
    assert any("Other" in w for w in warnings)


def test_single_element_y_list_keeps_its_name(pivoted_frame):
    figure, _ = figures.build(
        pivoted_frame, {"chart_type": "line", "x": "order_date", "y": ["North"]}
    )
    assert figure["data"][0]["name"] == "North"
    assert len(figure["data"][0]["y"]) == 3


def test_single_element_y_list_still_groups_by_colour(timeseries_frame):
    """The shape that drew a blank chart: y=["revenue"] alongside a colour.

    Every group got its x values and a correctly spelled name, and an empty y,
    because str(["revenue"]) is not a column. Three labelled lines with no data
    in them look like a rendering failure, not a data one.
    """
    figure, _ = figures.build(
        timeseries_frame,
        {"chart_type": "line", "x": "order_date", "y": ["revenue"], "color": "region"},
    )
    traces = figure["data"]
    assert len(traces) == 3
    assert all(len(t["y"]) == len(t["x"]) > 0 for t in traces)
    # The axis title comes off the same y, so it must be normalised too.
    assert figure["layout"]["yaxis"]["title"]["text"] == "Revenue"


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


# -- transformation plans --------------------------------------------------


def _orders():
    from twohelixes.datasets import samples

    samples.materialise()
    return samples.frame("orders")


def test_plan_runs_and_matches_hand_written_pandas():
    """The plan is the shared object, so it must do exactly what it says."""
    from twohelixes.pipeline import transform

    frame = _orders()
    steps = transform.normalise([
        {"type": "filter", "params": {"column": "refunded", "op": "eq", "value": False}},
        {"type": "aggregate", "params": {
            "by": ["region"],
            "metrics": [{"column": "net_amount", "agg": "sum", "as": "revenue"}],
        }},
        {"type": "sort", "params": {"column": "revenue", "desc": True}},
    ])
    out, notes = transform.apply(frame, steps)

    expected = (
        frame[frame["refunded"] == False]  # noqa: E712 - matching the step exactly
        .groupby(["region"], dropna=False)
        .agg({"net_amount": "sum"})
        .reset_index()
        .rename(columns={"net_amount": "revenue"})
        .sort_values("revenue", ascending=False)
    )
    assert list(out["region"]) == list(expected["region"])
    assert out["revenue"].round(2).tolist() == expected["revenue"].round(2).tolist()
    assert len(notes) == 3


def test_disabled_step_is_skipped_without_being_lost():
    """Disabling is how a user tests 'what if this step were not here'."""
    from twohelixes.pipeline import transform

    frame = _orders()
    steps = transform.normalise([
        {"type": "filter", "params": {"column": "refunded", "op": "eq", "value": False},
         "enabled": False},
    ])
    out, notes = transform.apply(frame, steps)
    assert len(out) == len(frame)
    assert "skipped" in notes[0]
    assert len(steps) == 1  # still in the plan


def test_validation_rejects_an_unknown_column_before_running():
    from twohelixes.pipeline import transform

    steps = transform.normalise([
        {"type": "filter", "params": {"column": "nonexistent", "op": "eq", "value": 1}},
    ])
    problems = transform.validate(steps, ["region", "net_amount"])
    assert problems and "nonexistent" in problems[0]["error"]


def test_unknown_step_type_is_refused():
    from twohelixes.pipeline import transform

    with pytest.raises(transform.TransformError):
        transform.normalise([{"type": "drop_table", "params": {}}])


def test_derive_expressions_reject_code():
    """`expr` is the only free text in a plan, and it comes from a model."""
    from twohelixes.pipeline import transform

    for hostile in (
        "__import__('os').system('ls')",
        "eval('1+1')",
        "lambda: 1",
        "df.__class__",
    ):
        steps = transform.normalise([
            {"type": "derive", "params": {"as": "x", "expr": hostile}},
        ])
        problems = transform.validate(steps, ["a", "b"])
        assert problems, f"expression was allowed: {hostile}"


def test_derive_allows_ordinary_arithmetic():
    from twohelixes.pipeline import transform

    frame = _orders()
    steps = transform.normalise([
        {"type": "derive", "params": {"as": "margin", "expr": "amount - discount"}},
    ])
    assert transform.validate(steps, [str(c) for c in frame.columns]) == []
    out, _ = transform.apply(frame, steps)
    assert "margin" in out.columns
    assert out["margin"].round(2).tolist() == (
        frame["amount"] - frame["discount"]
    ).round(2).tolist()


def test_rendered_python_reproduces_the_plan():
    """The notebook runs the rendered source, so it must match the executed plan."""
    from twohelixes.pipeline import transform

    frame = _orders()
    steps = transform.normalise([
        {"type": "filter", "params": {"column": "region", "op": "eq", "value": "APAC"}},
        {"type": "aggregate", "params": {
            "by": ["channel"],
            "metrics": [{"column": "net_amount", "agg": "sum", "as": "revenue"}],
        }},
    ])
    direct, _ = transform.apply(frame, steps)

    source = transform.to_python(steps, frame_name="df")
    scope: dict = {"df": frame}
    import pandas as pd

    scope["pd"] = pd
    exec(compile(source, "<plan>", "exec"), scope, scope)  # noqa: S102
    rendered = scope["result"]

    assert list(rendered.columns) == list(direct.columns)
    assert len(rendered) == len(direct)
    assert rendered["revenue"].round(2).tolist() == direct["revenue"].round(2).tolist()


def test_every_step_type_describes_itself():
    """The description is what the user reads instead of the params blob."""
    from twohelixes.pipeline import transform

    samples = {
        "filter": {"column": "a", "op": "gt", "value": 1},
        "aggregate": {"by": ["a"], "metrics": [{"column": "b", "agg": "sum"}]},
        "derive": {"as": "c", "expr": "a + b"},
        "sort": {"column": "a", "desc": True},
        "limit": {"n": 10},
        "select": {"columns": ["a"]},
        "rename": {"map": {"a": "b"}},
        "dropna": {"columns": ["a"]},
        "resample": {"time_column": "t", "grain": "month",
                     "metrics": [{"column": "b", "agg": "sum"}]},
        "top_n": {"category": "a", "measure": "b", "n": 5},
        "pivot": {"index": "a", "columns": "b", "values": "c"},
    }
    for step_type, params in samples.items():
        step = transform.Step(type=step_type, params=params, id="s1")
        text = transform.describe(step)
        assert text and text != step_type, f"{step_type} has no description"


def test_plan_length_is_capped():
    from twohelixes.pipeline import transform

    too_many = [{"type": "limit", "params": {"n": 5}}] * (transform.MAX_STEPS + 1)
    with pytest.raises(transform.TransformError):
        transform.normalise(too_many)


def test_stale_title_is_refreshed_when_the_plan_renames_the_measure():
    """A title naming a dropped column is confidently wrong.

    Seen in the builder: resampling `net_amount` into `revenue` left the
    heuristic title "Units over time" over a revenue series.
    """
    from twohelixes.datasets import samples
    from twohelixes.pipeline import transform

    samples.materialise()
    frame = samples.frame("orders")
    initial = figures.heuristic_config(frame, "")
    assert "units" in initial["title"].lower()

    steps = transform.normalise([
        {"type": "resample", "params": {
            "time_column": "order_date", "grain": "month", "by": ["region"],
            "metrics": [{"column": "net_amount", "agg": "sum", "as": "revenue"}],
        }},
    ])
    shaped, _ = transform.apply(frame, steps)

    config = {**initial, "y": "revenue"}
    refreshed = figures.validate_config(config, shaped)
    assert "revenue" in refreshed["title"].lower()
    assert "units" not in refreshed["title"].lower()
    assert refreshed["y_title"].lower() == "revenue"


def test_a_user_written_title_is_never_overwritten():
    from twohelixes.datasets import samples
    from twohelixes.pipeline import transform

    samples.materialise()
    frame = samples.frame("orders")
    steps = transform.normalise([
        {"type": "resample", "params": {
            "time_column": "order_date", "grain": "month",
            "metrics": [{"column": "net_amount", "agg": "sum", "as": "revenue"}],
        }},
    ])
    shaped, _ = transform.apply(frame, steps)

    config = {
        "chart_type": "line", "x": "order_date", "y": "revenue",
        "title": "Units of anything I like", "title_locked": True,
    }
    assert figures.validate_config(config, shaped)["title"] == "Units of anything I like"


def test_correct_titles_are_left_alone():
    """Refreshing must not churn a title that already names the measure."""
    frame = pd.DataFrame(
        {"month": pd.date_range("2024-01-01", periods=6, freq="ME"),
         "revenue": [1, 2, 3, 4, 5, 6]}
    )
    config = {
        "chart_type": "line", "x": "month", "y": "revenue",
        "title": "Revenue grew every month", "y_title": "Revenue",
    }
    out = figures.validate_config(config, frame)
    assert out["title"] == "Revenue grew every month"


def test_dense_bars_drop_the_spacer_instead_of_vanishing():
    """A 2px surface stroke erases sub-pixel bars.

    Seen on a dashboard: a bar chart over 11k unaggregated rows rendered as
    an empty plot with axes and gridlines, because every mark was thinner
    than its own background-coloured border.
    """
    frame = pd.DataFrame({
        "channel": [f"c{i % 6}" for i in range(5000)],
        "value": [float(i) for i in range(5000)],
    })
    figure, _ = figures.build(frame, {"chart_type": "bar", "x": "channel", "y": "value"})
    figure = defaults.apply(figure, chart_type="bar")
    marker = figure["data"][0]["marker"]
    assert marker["line"]["width"] == 0
    assert marker["cornerradius"] == 0


def test_ordinary_bars_keep_the_spacer():
    frame = pd.DataFrame({"c": list("abcde"), "v": [1.0, 2, 3, 4, 5]})
    figure, _ = figures.build(frame, {"chart_type": "bar", "x": "c", "y": "v"})
    figure = defaults.apply(figure, chart_type="bar")
    marker = figure["data"][0]["marker"]
    assert marker["line"]["width"] == defaults.SPACER
    assert marker["cornerradius"] == defaults.BAR_CORNER_RADIUS


def test_audit_flags_unaggregated_bars():
    frame = pd.DataFrame({
        "c": [f"c{i % 4}" for i in range(900)],
        "v": [float(i) for i in range(900)],
    })
    figure, _ = figures.build(frame, {"chart_type": "bar", "x": "c", "y": "v"})
    figure = defaults.apply(figure, chart_type="bar")
    assert any(f["code"] == "unaggregated_bars" for f in defaults.audit(figure))


def test_audit_catches_a_spacer_that_would_erase_bars():
    """Guard the fix directly: a dense bar chart that kept its stroke."""
    figure = {
        "data": [{
            "type": "bar",
            "x": [f"c{i}" for i in range(200)],
            "y": [float(i) for i in range(200)],
            "marker": {"line": {"width": 2, "color": "#fff"}},
            "_series_index": 0,
        }],
        "layout": {},
    }
    findings = defaults.audit(figure)
    assert any(f["code"] == "spacer_erases_dense_bars" for f in findings)
