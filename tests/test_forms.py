"""The chart forms that need real reshaping.

Each of these previously fell through to a bar chart: asking for a sankey
silently got you something else. These tests assert the actual trace shape,
because "it returned a figure" is not evidence it drew the right thing.
"""

from __future__ import annotations

import pandas as pd
import pytest

from twohelixes.charts import defaults, forms, palette
from twohelixes.pipeline import figures


@pytest.fixture
def flows() -> pd.DataFrame:
    return pd.DataFrame({
        "channel": ["Paid", "Paid", "Organic", "Organic", "Email"],
        "region": ["EU", "US", "EU", "US", "EU"],
        "revenue": [100.0, 240.0, 310.0, 180.0, 60.0],
    })


@pytest.fixture
def stages() -> pd.DataFrame:
    return pd.DataFrame({
        "stage": ["Visited", "Signed up", "Activated", "Paid"],
        "users": [10000, 3200, 1400, 520],
    })


# -- sankey ----------------------------------------------------------------


def test_sankey_builds_a_real_sankey(flows):
    figure, _ = figures.build(
        flows, {"chart_type": "sankey", "x": "channel", "color": "region", "y": "revenue"}
    )
    assert len(figure["data"]) == 1
    trace = figure["data"][0]
    assert trace["type"] == "sankey"
    assert len(trace["link"]["value"]) == 5
    assert sum(trace["link"]["value"]) == pytest.approx(890.0)


def test_sankey_keeps_source_and_target_names_distinct():
    """A name on both sides must not collapse into a self-loop."""
    frame = pd.DataFrame({
        "from": ["EU", "US"], "to": ["US", "EU"], "n": [5.0, 7.0],
    })
    traces, _ = forms.sankey(frame, {"x": "from", "color": "to", "y": "n"})
    trace = traces[0]
    # Four slots: EU and US each appear as a source and as a target.
    assert len(trace["node"]["label"]) == 4
    assert all(s != t for s, t in zip(trace["link"]["source"], trace["link"]["target"]))


def test_sankey_caps_and_says_so():
    frame = pd.DataFrame({
        "a": [f"s{i}" for i in range(200)],
        "b": [f"t{i}" for i in range(200)],
        "v": [float(i + 1) for i in range(200)],
    })
    traces, warnings = forms.sankey(frame, {"x": "a", "color": "b", "y": "v"})
    assert len(traces[0]["link"]["value"]) == forms.MAX_SANKEY_LINKS
    assert any("largest" in w for w in warnings)


def test_sankey_without_a_target_explains_itself(flows):
    traces, warnings = forms.sankey(flows, {"x": "channel"})
    assert traces == []
    assert warnings and "target" in warnings[0]


# -- treemap ---------------------------------------------------------------


def test_treemap_nests_parents_and_totals(flows):
    traces, _ = forms.treemap(
        flows, {"x": "region", "y": "revenue", "color": "channel"}
    )
    trace = traces[0]
    assert trace["type"] == "treemap"
    assert trace["branchvalues"] == "total"
    # Parents carry an empty parent string; children name theirs.
    roots = [l for l, p in zip(trace["labels"], trace["parents"]) if p == ""]
    assert set(roots) == {"Paid", "Organic", "Email"}
    # A parent's value equals the sum of its children.
    for root in roots:
        children = [
            v for l, p, v in zip(trace["labels"], trace["parents"], trace["values"])
            if p == root
        ]
        parent_value = next(
            v for l, p, v in zip(trace["labels"], trace["parents"], trace["values"])
            if l == root and p == ""
        )
        assert parent_value == pytest.approx(sum(children))


def test_treemap_flat_when_no_parent(flows):
    traces, _ = forms.treemap(flows, {"x": "channel", "y": "revenue"})
    assert all(p == "" for p in traces[0]["parents"])


# -- sunburst --------------------------------------------------------------


def test_sunburst_uses_the_treemap_hierarchy(flows):
    config = {"x": "region", "y": "revenue", "color": "channel"}
    treemap, _ = forms.treemap(flows, config)
    figure, _ = figures.build(flows, {"chart_type": "sunburst", **config})
    trace = figure["data"][0]
    assert trace["type"] == "sunburst"
    assert trace["labels"] == treemap[0]["labels"]
    assert trace["parents"] == treemap[0]["parents"]
    assert trace["values"] == treemap[0]["values"]
    assert defaults.audit(defaults.apply(figure, chart_type="sunburst")) == []


# -- bubble ----------------------------------------------------------------


def test_bubble_builds_with_area_scaled_sizes():
    frame = pd.DataFrame({
        "x": [1, 2, 3],
        "y": [3, 2, 1],
        "population": [1, 4, 100],
    })
    figure, _ = figures.build(
        frame, {"chart_type": "bubble", "x": "x", "y": "y", "size": "population"}
    )
    trace = figure["data"][0]
    assert trace["type"] == "scatter"
    assert trace["mode"] == "markers"
    assert trace["marker"]["size"] == pytest.approx([6.0, 8.4, 42.0])
    assert defaults.audit(defaults.apply(figure, chart_type="bubble")) == []


def test_bubble_caps_groups_at_three():
    frame = pd.DataFrame({
        "x": range(5),
        "y": range(5),
        "size": [1, 2, 3, 4, 5],
        "group": list("abcde"),
    })
    figure, warnings = figures.build(
        frame,
        {"chart_type": "bubble", "x": "x", "y": "y", "size": "size", "color": "group"},
    )
    assert len(figure["data"]) == palette.ALL_PAIRS_MAX_SERIES
    assert any("showing" in warning for warning in warnings)


# -- map -------------------------------------------------------------------


def test_coordinate_map_builds_street_map_and_preserves_other_locations():
    frame = pd.DataFrame({
        "latitude": [51.5, 48.9, 40.7, 35.7, -33.9],
        "longitude": [-0.1, 2.3, -74.0, 139.7, 151.2],
        "value": [10, 20, 30, 40, 50],
        "group": list("abcde"),
    })
    figure, warnings = figures.build(
        frame, {"chart_type": "map", "y": "value", "color": "group"}
    )
    assert len(figure["data"]) == palette.ALL_PAIRS_MAX_SERIES + 1
    assert all(trace["type"] == "scattermap" for trace in figure["data"])
    assert any("showing" in warning for warning in warnings)
    styled = defaults.apply(figure, chart_type="map")
    assert sum(len(t["lat"]) for t in styled["data"]) == len(frame)
    assert styled["layout"]["map"]["style"] == "open-street-map"
    assert defaults.audit(styled) == []


def test_country_map_builds_choropleth_with_our_sequential_scale():
    frame = pd.DataFrame({
        "country": ["France", "Germany", "Japan"],
        "value": [10, 20, 30],
    })
    figure, _ = figures.build(
        frame, {"chart_type": "map", "x": "country", "y": "value"}
    )
    trace = figure["data"][0]
    assert trace["type"] == "choropleth"
    assert trace["locationmode"] == "ISO-3"
    assert [stop[1] for stop in trace["colorscale"]] == palette.sequential(9)
    assert defaults.audit(defaults.apply(figure, chart_type="map")) == []


# -- funnel ----------------------------------------------------------------


def test_funnel_keeps_stage_order_not_size_order(stages):
    traces, warnings = forms.funnel(stages, {"x": "stage", "y": "users"})
    trace = traces[0]
    assert trace["type"] == "funnel"
    assert list(trace["y"]) == ["Visited", "Signed up", "Activated", "Paid"]
    assert warnings == []


def test_funnel_flags_a_stage_that_grows(stages):
    """A funnel that increases is nearly always a data error."""
    broken = stages.copy()
    broken.loc[2, "users"] = 99999
    _, warnings = forms.funnel(broken, {"x": "stage", "y": "users"})
    assert any("decrease" in w for w in warnings)


def test_funnel_uses_an_ordinal_ramp_not_categorical_hues(stages):
    """Stages are ordered, so the colour must carry that order."""
    traces, _ = forms.funnel(stages, {"x": "stage", "y": "users"})
    colours = traces[0]["marker"]["color"]
    assert len(colours) == 4
    # One hue: an ordinal ramp, not four unrelated categorical slots.
    assert colours[0] != colours[-1]
    assert palette.CATEGORICAL_LIGHT[1] not in colours


def test_funnel_respects_an_explicit_order(stages):
    order = ["Paid", "Activated", "Signed up", "Visited"]
    traces, _ = forms.funnel(stages, {"x": "stage", "y": "users", "stage_order": order})
    assert list(traces[0]["y"]) == order


# -- waterfall -------------------------------------------------------------


def test_waterfall_marks_the_last_bar_as_a_total():
    frame = pd.DataFrame({
        "item": ["Opening", "New", "Churn", "Closing"],
        "delta": [100.0, 40.0, -15.0, 125.0],
    })
    traces, _ = forms.waterfall(frame, {"x": "item", "y": "delta"})
    trace = traces[0]
    assert trace["measure"][-1] == "total"
    assert trace["measure"][:-1] == ["relative"] * 3


def test_waterfall_labels_carry_the_sign():
    """Direction is meaning here, so it must not be colour alone."""
    frame = pd.DataFrame({"item": ["Up", "Down"], "delta": [40.0, -15.0]})
    traces, _ = forms.waterfall(frame, {"x": "item", "y": "delta"})
    assert traces[0]["text"][0].startswith("+")
    assert traces[0]["text"][1].startswith("-")


# -- box -------------------------------------------------------------------


def test_box_makes_one_trace_per_group(flows):
    figure, _ = figures.build(
        flows, {"chart_type": "box", "x": "region", "y": "revenue"}
    )
    assert len(figure["data"]) == 2
    assert {t["name"] for t in figure["data"]} == {"EU", "US"}
    assert all(t["type"] == "box" for t in figure["data"])


def test_box_caps_groups_at_the_palette_limit():
    frame = pd.DataFrame({
        "g": [f"g{i % 30}" for i in range(300)],
        "v": [float(i) for i in range(300)],
    })
    traces, warnings = forms.box(frame, {"x": "g", "y": "v"})
    assert len(traces) <= palette.MAX_SERIES
    assert any("groups" in w for w in warnings)


# -- every form ------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(forms.BUILDERS))
def test_every_form_passes_the_chart_audit(kind, flows, stages):
    frame = stages if kind in ("funnel", "waterfall") else flows
    config = (
        {"chart_type": kind, "x": "stage", "y": "users"}
        if kind in ("funnel", "waterfall")
        else {"chart_type": kind, "x": "channel", "color": "region", "y": "revenue"}
    )
    figure, _ = figures.build(frame, config)
    figure = defaults.apply(figure, chart_type=kind)
    assert defaults.audit(figure) == []


@pytest.mark.parametrize("kind", sorted(forms.BUILDERS))
def test_a_form_given_nothing_usable_degrades_with_a_reason(kind):
    empty = pd.DataFrame({"only": [1, 2, 3]})
    traces, warnings = forms.build(kind, empty, {"chart_type": kind})
    if not traces:
        assert warnings, f"{kind} returned nothing and said nothing"


def test_unknown_form_is_reported():
    traces, warnings = forms.build("hologram", pd.DataFrame({"a": [1]}), {})
    assert traces == []
    assert "hologram" in warnings[0]


def test_map_missing_values_stay_neutral_and_names_are_escaped():
    frame = pd.DataFrame({'latitude': [-33.8, -33.9, None], 'longitude': [151.1, 151.2, 150.0],
                          'school_name': ['<School>', 'Missing', 'Unlocated'], 'reading_y3': [410, None, 500]})
    figure, warnings = figures.build(frame, {'chart_type': 'map', 'y': 'reading_y3'})
    styled = defaults.apply(figure, chart_type='map')
    assert sum(len(t['lat']) for t in styled['data']) == 2
    scored = next(t for t in styled['data'] if not t.get('_neutral'))
    missing = next(t for t in styled['data'] if t.get('_neutral'))
    assert scored['marker']['color'] == [410.0]
    assert '&lt;School&gt;' in scored['text'][0]
    assert missing['marker']['color'] == palette.OTHER_COLOR_LIGHT
    assert any('coordinates' in w for w in warnings)


def test_school_location_heuristic_does_not_color_by_calendar_or_ids():
    frame = pd.DataFrame({'school_id':['1','2'], 'latitude':[-33.,-34.], 'longitude':[151.,150.],
                          'profile_year':[2025,2025], 'postcode':[2000,2001], 'lbote_yes_pct':[96.,4.]})
    assert figures.heuristic_config(frame,'Show school locations on a map')['y'] is None


def test_numeric_map_color_is_continuous_not_truncated_categories():
    frame = pd.DataFrame({'latitude': [-33.]*5, 'longitude': [150.]*5, 'reading_y3': [350,400,450,500,550]})
    traces,_=forms.map_chart(frame,{'y':'latitude','color':'reading_y3'})
    assert len(traces)==1
    assert traces[0]['marker']['color']==[350,400,450,500,550]
    assert traces[0]['marker']['showscale']


# -- gantt -----------------------------------------------------------------


@pytest.fixture
def plan() -> pd.DataFrame:
    return pd.DataFrame({
        "task": ["Design", "Build", "Test", "Launch"],
        "start": ["2026-01-01", "2026-01-15", "2026-02-15", "2026-03-10"],
        "end": ["2026-01-14", "2026-02-14", "2026-03-09", "2026-03-10"],
        "team": ["UX", "Eng", "QA", "Ops"],
        "percent_complete": [100, 60, 0, 0],
    })


def test_gantt_draws_horizontal_bars_from_the_start(plan):
    traces, warnings = forms.gantt(plan, {})
    assert warnings == []
    bars = [t for t in traces if t["type"] == "bar" and t.get("hoverinfo") != "skip"]
    assert bars and all(t["orientation"] == "h" for t in bars)
    first = next(t for t in bars if t["y"][0] == "Design")
    assert first["base"][0].startswith("2026-01-01")
    assert first["x"][0] == pytest.approx(13 * 86_400_000)


def test_gantt_zero_length_task_is_a_milestone(plan):
    traces, _ = forms.gantt(plan, {})
    marks = [t for t in traces if t["type"] == "scatter"]
    assert marks and marks[0]["marker"]["symbol"] == "diamond"
    assert "Launch" in marks[0]["y"]


def test_gantt_progress_overlay_is_proportional(plan):
    traces, _ = forms.gantt(plan, {})
    base = next(t for t in traces if t["type"] == "bar" and t["y"] == ["Build"])
    done = next(t for t in traces if t.get("hoverinfo") == "skip" and t["y"] == ["Build"])
    assert done["x"][0] == pytest.approx(base["x"][0] * 0.6)


def test_gantt_numeric_schedule_with_duration():
    frame = pd.DataFrame({"phase": ["a", "b"], "start_week": [0, 3], "duration_weeks": [3, 4]})
    figure, warnings = figures.build(frame, {"chart_type": "gantt"})
    assert figure["layout"]["xaxis"]["type"] == "linear"
    assert figure["data"][0]["base"] == [0, 3]
    assert figure["data"][0]["x"] == [3, 4]


def test_gantt_layout_reads_top_to_bottom_on_a_date_axis(plan):
    figure, _ = figures.build(plan, {"chart_type": "gantt"})
    assert figure["layout"]["xaxis"]["type"] == "date"
    assert figure["layout"]["yaxis"]["autorange"] == "reversed"
    assert figure["layout"]["yaxis"]["categoryarray"] == ["Design", "Build", "Test", "Launch"]


def test_gantt_repairs_swapped_dates_and_reports_it():
    frame = pd.DataFrame({"task": ["a"], "start": ["2026-02-10"], "end": ["2026-02-01"]})
    traces, warnings = forms.gantt(frame, {})
    assert traces[0]["x"][0] > 0
    assert any("swapped" in w for w in warnings)


def test_gantt_mixed_date_and_number_is_refused_with_a_reason():
    frame = pd.DataFrame({"task": ["a"], "start": ["2026-02-10"], "end": [5]})
    traces, warnings = forms.gantt(frame, {"start": "start", "end": "end"})
    assert traces == [] and warnings


def test_gantt_folds_extra_groups_instead_of_cycling_colours():
    n = 12
    frame = pd.DataFrame({
        "task": [f"t{i}" for i in range(n)],
        "start": pd.date_range("2026-01-01", periods=n).astype(str),
        "end": pd.date_range("2026-01-05", periods=n).astype(str),
        "team": [f"team{i}" for i in range(n)],
    })
    figure, warnings = figures.build(frame, {"chart_type": "gantt"})
    styled = defaults.apply(figure, chart_type="gantt")
    assert any("Other" in w for w in warnings)
    assert {t["name"] for t in styled["data"]} <= {f"team{i}" for i in range(n)} | {"Other"}
    assert defaults.audit(styled) == []


def test_heuristic_chooses_gantt_for_a_schedule_but_not_a_plain_pair_of_dates(plan):
    assert figures.heuristic_config(plan, "what is the plan")["chart_type"] == "gantt"
    sales = pd.DataFrame({"day": ["2026-01-01", "2026-01-02"], "revenue": [1.0, 2.0]})
    assert figures.heuristic_config(sales, "revenue")["chart_type"] != "gantt"
