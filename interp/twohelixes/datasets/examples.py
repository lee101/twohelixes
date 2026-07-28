"""Worked examples for the sample datasets: a real chart and a real trace.

Every dataset page shows what this product does before asking for an email, so
the charts on it have to be the product's charts - built by `figures.build`,
styled by `defaults.apply`, audited by `defaults.audit` and drawn by the same
SVG exporter that serves customers. A screenshot would drift the first time
the palette changed; these cannot.

The reasoning trace shown alongside each one is *derived from the run that just
happened*, not written prose: the row counts, the columns, the chart form and
the timings are read off the execution. The only authored parts are the two
sentences explaining why this cut of the data answers the question, which is
the one thing arithmetic cannot supply.

One transform string per example, executed here and emitted into the notebook,
so "what the page shows" and "what you download" cannot disagree. The code is
ours - never a model's and never a user's - which is what makes running it
here reasonable.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from twohelixes.charts import defaults, svg
from twohelixes.datasets import samples
from twohelixes.pipeline import figures

log = logging.getLogger("twohelixes.datasets.examples")

PREVIEW_ROWS = 8


@dataclass(frozen=True)
class Example:
    dataset: str
    slug: str
    question: str
    headline: str
    finding: str
    transform: str
    config: dict[str, Any]
    found: str
    reason: str

    @property
    def path(self) -> str:
        return f"/datasets/{self.dataset}/{self.slug}"


@dataclass
class Rendered:
    example: Example
    figure: dict[str, Any]
    markup: str
    trace: list[dict[str, Any]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    source_rows: int = 0
    chart_type: str = ""
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# The examples
# --------------------------------------------------------------------------

EXAMPLES: list[Example] = [
    # -- orders ------------------------------------------------------------
    Example(
        dataset="orders",
        slug="net-revenue-by-region-over-time",
        question="how did net revenue trend by region over time?",
        headline="Net revenue by region, monthly",
        finding=(
            "Three regions moving in different directions: the crossover is the "
            "answer, and it is only visible once the daily orders are rolled up "
            "to months."
        ),
        transform=(
            'df["month"] = df["order_date"].astype("datetime64[ns]")'
            '.dt.to_period("M").dt.to_timestamp()\n'
            'result = df.groupby(["month", "region"], as_index=False)'
            '["net_amount"].sum()'
        ),
        config={
            "chart_type": "line",
            "x": "month",
            "y": "net_amount",
            "color": "region",
            "title": "Net revenue by region",
            "x_title": "Month",
            "y_title": "Net revenue",
        },
        found=(
            "order_date, region and net_amount answer this; the other six columns "
            "do not enter the chart."
        ),
        reason=(
            "Time on x and one line per region. Grouped bars would put twelve "
            "months side by side and hide the crossing."
        ),
    ),
    Example(
        dataset="orders",
        slug="revenue-per-order-by-channel",
        question="which channel brings the most revenue per order?",
        headline="Average net revenue per order, by channel",
        finding=(
            "A per-order average, not a total: the channel with the most revenue "
            "and the channel with the most valuable order are usually not the "
            "same channel."
        ),
        transform=(
            'result = df.groupby("channel", as_index=False)["net_amount"].mean()'
            '.sort_values("net_amount", ascending=False)'
        ),
        config={
            "chart_type": "bar",
            "x": "channel",
            "y": "net_amount",
            "title": "Net revenue per order, by channel",
            "x_title": "Channel",
            "y_title": "Net revenue per order",
        },
        found="channel and net_amount; order counts come from the grouping itself.",
        reason=(
            "A handful of named categories being compared on one measure is a "
            "bar chart, sorted so the ranking is the shape."
        ),
    ),
    Example(
        dataset="orders",
        slug="refund-rate-by-category",
        question="what share of orders get refunded, by category?",
        headline="Refund rate by product category",
        finding=(
            "A rate, computed from the flag rather than counted twice: the mean "
            "of a boolean is the share that are true."
        ),
        transform=(
            'result = df.groupby("category", as_index=False)["refunded"].mean()\n'
            'result["refund_rate"] = result["refunded"] * 100\n'
            'result = result[["category", "refund_rate"]]'
            '.sort_values("refund_rate", ascending=False)'
        ),
        config={
            "chart_type": "hbar",
            "x": "category",
            "y": "refund_rate",
            "title": "Refund rate by category",
            "x_title": "Category",
            "y_title": "Refund rate (%)",
        },
        found="category and the refunded flag. Amounts are irrelevant to a rate.",
        reason=(
            "Category names are long, so the bars run horizontally and the "
            "labels stay readable rather than rotating."
        ),
    ),
    # -- web traffic -------------------------------------------------------
    Example(
        dataset="web_traffic",
        slug="sessions-by-source-over-time",
        question="which traffic source grew the most this year?",
        headline="Monthly sessions by acquisition source",
        finding=(
            "Growth is a comparison of slopes, so every source is drawn on one "
            "axis at one scale - not one panel each."
        ),
        transform=(
            'df["month"] = df["day"].astype("datetime64[ns]")'
            '.dt.to_period("M").dt.to_timestamp()\n'
            'result = df.groupby(["month", "source"], as_index=False)'
            '["sessions"].sum()'
        ),
        config={
            "chart_type": "line",
            "x": "month",
            "y": "sessions",
            "color": "source",
            "title": "Monthly sessions by source",
            "x_title": "Month",
            "y_title": "Sessions",
        },
        found="day, source and sessions. Bounce rate is a different question.",
        reason=(
            "One line per source on a shared axis. Small multiples would make "
            "the slopes incomparable, which is the whole question."
        ),
    ),
    Example(
        dataset="web_traffic",
        slug="bounce-rate-by-source",
        question="how does bounce rate compare across sources?",
        headline="Average bounce rate by source",
        finding=(
            "Bounce rate is already a rate, so the sessions are averaged, never "
            "summed - a summed rate is a meaningless number."
        ),
        transform=(
            'result = df.groupby("source", as_index=False)["bounce_rate"].mean()\n'
            'result["bounce_rate"] = result["bounce_rate"] * 100\n'
            'result = result.sort_values("bounce_rate")'
        ),
        config={
            "chart_type": "bar",
            "x": "source",
            "y": "bounce_rate",
            "title": "Bounce rate by source",
            "x_title": "Source",
            "y_title": "Bounce rate (%)",
        },
        found="source and bounce_rate.",
        reason="One measure across a few named categories: bars, sorted.",
    ),
    # -- subscriptions -----------------------------------------------------
    Example(
        dataset="subscriptions",
        slug="retention-curve",
        question="what does retention look like by months since signup?",
        headline="Accounts still active, by months since signup",
        finding=(
            "Every cohort is put on the same x axis - months since signup, not "
            "calendar months - which is the only way curves of different ages "
            "can be compared."
        ),
        transform=(
            'result = df.groupby("months_since_signup", as_index=False)'
            '["active_accounts"].sum()'
        ),
        config={
            "chart_type": "area",
            "x": "months_since_signup",
            "y": "active_accounts",
            "title": "Retention by months since signup",
            "x_title": "Months since signup",
            "y_title": "Active accounts",
        },
        found="months_since_signup and active_accounts, summed over every cohort.",
        reason=(
            "A single decaying series reads as an area: the fill carries the "
            "magnitude that is being lost, which a bare line does not."
        ),
    ),
    Example(
        dataset="subscriptions",
        slug="mrr-by-plan",
        question="how does MRR split across plans?",
        headline="Share of monthly recurring revenue by plan",
        finding=(
            "A share of a whole with three parts is one of the few honest uses "
            "of a pie; four or more slices and this becomes bars."
        ),
        transform=(
            'latest = df["activity_month"].max()\n'
            'result = df[df["activity_month"] == latest]'
            '.groupby("plan", as_index=False)["mrr"].sum()'
        ),
        config={
            "chart_type": "pie",
            "x": "plan",
            "y": "mrr",
            "title": "MRR by plan, latest month",
        },
        found="plan and mrr for the most recent activity month only.",
        reason=(
            "Parts of one total, few enough to read as slices. Every slice is "
            "labelled, because angle alone is not a readable quantity."
        ),
    ),
    # -- support -----------------------------------------------------------
    Example(
        dataset="support_tickets",
        slug="first-response-by-priority",
        question="what is the median first response time by priority?",
        headline="Median first response, by priority",
        finding=(
            "The median, not the mean: response times have a long tail and one "
            "ticket left over a weekend moves an average and not a median."
        ),
        transform=(
            'result = df.groupby("priority", as_index=False)'
            '["first_response_hours"].median()'
            '.sort_values("first_response_hours")'
        ),
        config={
            "chart_type": "bar",
            "x": "priority",
            "y": "first_response_hours",
            "title": "Median first response by priority",
            "x_title": "Priority",
            "y_title": "Hours to first response",
        },
        found="priority and first_response_hours.",
        reason="Few categories, one measure, an obvious ordering: bars.",
    ),
    Example(
        dataset="support_tickets",
        slug="tickets-by-topic",
        question="which topics generate the most tickets?",
        headline="Ticket volume by topic",
        finding=(
            "A count of rows per topic - the question asks about volume, so "
            "nothing is averaged and nothing is weighted."
        ),
        transform=(
            'result = df.groupby("topic", as_index=False)'
            '.agg(tickets=("ticket_id", "count"))'
            '.sort_values("tickets", ascending=False)'
        ),
        config={
            "chart_type": "hbar",
            "x": "topic",
            "y": "tickets",
            "title": "Tickets by topic",
            "x_title": "Topic",
            "y_title": "Tickets",
        },
        found="topic, counted. No other column is needed for a volume question.",
        reason=(
            "Topic names are sentences, so the bars run horizontally rather "
            "than tilting the labels 45 degrees."
        ),
    ),
    # -- energy ------------------------------------------------------------
    Example(
        dataset="energy",
        slug="daily-energy-by-site",
        question="show daily energy use per site",
        headline="Daily kWh by site",
        finding=(
            "Hourly readings are rolled up to days: at hourly resolution three "
            "sites over a quarter is 6,000 points of noise, not a trend."
        ),
        transform=(
            'df["day"] = df["reading_at"].astype("datetime64[ns]").dt.floor("D")\n'
            'result = df.groupby(["day", "site"], as_index=False)["kwh"].sum()'
        ),
        config={
            "chart_type": "line",
            "x": "day",
            "y": "kwh",
            "color": "site",
            "title": "Daily energy use by site",
            "x_title": "Day",
            "y_title": "kWh",
        },
        found="reading_at, site and kwh.",
        reason="A measure over time, one line per site.",
    ),
    Example(
        dataset="energy",
        slug="energy-against-temperature",
        question="is consumption correlated with temperature?",
        headline="Daily energy against average temperature",
        finding=(
            "Correlation is a question about two measures, so both go on axes "
            "and neither goes on time - the days become the points."
        ),
        transform=(
            'df["day"] = df["reading_at"].astype("datetime64[ns]").dt.floor("D")\n'
            'result = df.groupby(["day", "site"], as_index=False).agg(\n'
            '    kwh=("kwh", "sum"), temperature_c=("temperature_c", "mean")\n'
            ")"
        ),
        config={
            "chart_type": "scatter",
            "x": "temperature_c",
            "y": "kwh",
            "color": "site",
            "title": "Energy use against temperature",
            "x_title": "Average temperature (C)",
            "y_title": "kWh per day",
        },
        found="temperature_c and kwh per day, split by site.",
        reason=(
            "Two measures and no time axis is a scatter. Three sites is the "
            "cap for a scatter here, because adjacent points must stay "
            "separable for a colour-blind reader."
        ),
    ),
    # -- iris --------------------------------------------------------------
    Example(
        dataset="iris",
        slug="petal-length-against-width",
        question="how do petal length and width separate the species?",
        headline="Petal length against petal width, by species",
        finding=(
            "The classic result: two of the three species separate cleanly on "
            "petals alone, and the pair that overlaps is the pair that always "
            "overlaps."
        ),
        transform='result = df[["petal_length_cm", "petal_width_cm", "species"]]',
        config={
            "chart_type": "scatter",
            "x": "petal_length_cm",
            "y": "petal_width_cm",
            "color": "species",
            "title": "Petal length against width, by species",
            "x_title": "Petal length (cm)",
            "y_title": "Petal width (cm)",
        },
        found="the two petal measurements and the species label.",
        reason="Two measures, a categorical split, no time: a scatter.",
    ),
    Example(
        dataset="iris",
        slug="sepal-length-by-species",
        question="compare sepal length across species",
        headline="Mean sepal length by species",
        finding=(
            "Three groups, one measurement: the comparison the question asks "
            "for is a ranking, so it is drawn as one."
        ),
        transform=(
            'result = df.groupby("species", as_index=False)["sepal_length_cm"].mean()'
        ),
        config={
            "chart_type": "bar",
            "x": "species",
            "y": "sepal_length_cm",
            "title": "Mean sepal length by species",
            "x_title": "Species",
            "y_title": "Sepal length (cm)",
        },
        found="species and sepal_length_cm.",
        reason="A measure across three named groups: bars.",
    ),
    # -- wine --------------------------------------------------------------
    Example(
        dataset="wine",
        slug="alcohol-against-colour-intensity",
        question="show alcohol against colour intensity by cultivar",
        headline="Alcohol against colour intensity, by cultivar",
        finding=(
            "Two chemical measures with the cultivar as colour: the clusters "
            "are the answer, not any single axis."
        ),
        transform='result = df[["alcohol", "color_intensity", "cultivar"]]',
        config={
            "chart_type": "scatter",
            "x": "color_intensity",
            "y": "alcohol",
            "color": "cultivar",
            "title": "Alcohol against colour intensity",
            "x_title": "Colour intensity",
            "y_title": "Alcohol (%)",
        },
        found="alcohol, color_intensity and cultivar.",
        reason="Two measures split by a category: a scatter, capped at three series.",
    ),
    Example(
        dataset="wine",
        slug="measurements-that-differ-most",
        question="which measurements differ most between cultivars?",
        headline="Measurements ranked by spread between cultivars",
        finding=(
            "Each measurement is scored by how far the cultivar means spread "
            "relative to the overall mean, so measures on wildly different "
            "units can be ranked against each other."
        ),
        transform=(
            'means = df.groupby("cultivar").mean(numeric_only=True)\n'
            'spread = (means.max() - means.min()) / means.mean().abs()\n'
            'result = (\n'
            '    spread.sort_values(ascending=False).head(8)\n'
            '    .rename("relative_spread").reset_index()\n'
            '    .rename(columns={"index": "measurement"})\n'
            ")"
        ),
        config={
            "chart_type": "hbar",
            "x": "measurement",
            "y": "relative_spread",
            "title": "Measurements that separate the cultivars",
            "x_title": "Measurement",
            "y_title": "Relative spread between cultivar means",
        },
        found="every numeric column, grouped by cultivar.",
        reason=(
            "A ranking of long-named things is a horizontal bar chart. The "
            "top eight are shown because the tail carries no signal."
        ),
    ),
    # -- diabetes ----------------------------------------------------------
    Example(
        dataset="diabetes",
        slug="strongest-correlates-of-progression",
        question="which variable correlates most with progression?",
        headline="Baseline variables ranked by correlation with progression",
        finding=(
            "Absolute correlation, so a strong negative relationship is not "
            "sorted to the bottom as though it were weak."
        ),
        transform=(
            'correlations = df.corr(numeric_only=True)["progression"]'
            '.drop("progression")\n'
            'result = (\n'
            '    correlations.abs().sort_values(ascending=False)\n'
            '    .rename("correlation").reset_index()\n'
            '    .rename(columns={"index": "variable"})\n'
            ")"
        ),
        config={
            "chart_type": "hbar",
            "x": "variable",
            "y": "correlation",
            "title": "Correlation with disease progression",
            "x_title": "Variable",
            "y_title": "Absolute correlation",
        },
        found="all ten baseline variables against progression.",
        reason="A ranking across ten items: horizontal bars, strongest first.",
    ),
    Example(
        dataset="diabetes",
        slug="bmi-against-progression",
        question="show BMI against progression",
        headline="BMI against disease progression",
        finding=(
            "One point per patient, no aggregation: the spread around the "
            "trend is as much of the answer as the trend."
        ),
        transform='result = df[["bmi", "progression"]]',
        config={
            "chart_type": "scatter",
            "x": "bmi",
            "y": "progression",
            "title": "BMI against progression",
            "x_title": "BMI (standardised)",
            "y_title": "Progression after one year",
        },
        found="bmi and progression.",
        reason="Two measures, no category and no time: a scatter of the raw rows.",
    ),
    # -- breast cancer -----------------------------------------------------
    Example(
        dataset="breast_cancer",
        slug="measurements-that-separate-diagnoses",
        question="which measurements best separate malignant from benign?",
        headline="Measurements ranked by separation between diagnoses",
        finding=(
            "The gap between the two group means, divided by their pooled "
            "spread - the same effect size a statistician would reach for, so "
            "measurements in different units can be ranked together."
        ),
        transform=(
            'groups = df.groupby("diagnosis")\n'
            'means, spreads = groups.mean(numeric_only=True), '
            'groups.std(numeric_only=True)\n'
            'effect = (means.loc["malignant"] - means.loc["benign"]).abs() / (\n'
            '    (spreads.loc["malignant"] + spreads.loc["benign"]) / 2\n'
            ")\n"
            'result = (\n'
            '    effect.sort_values(ascending=False).head(8)\n'
            '    .rename("separation").reset_index()\n'
            '    .rename(columns={"index": "measurement"})\n'
            ")"
        ),
        config={
            "chart_type": "hbar",
            "x": "measurement",
            "y": "separation",
            "title": "Measurements that separate the diagnoses",
            "x_title": "Measurement",
            "y_title": "Effect size between diagnoses",
        },
        found="all thirty measurements, grouped by diagnosis.",
        reason="A ranking of long-named measurements: horizontal bars.",
    ),
    Example(
        dataset="breast_cancer",
        slug="mean-radius-by-diagnosis",
        question="compare mean radius by diagnosis",
        headline="Mean nucleus radius by diagnosis",
        finding=(
            "Two groups and one measure. Two bars is close to a number, and "
            "the chart earns its place only because the gap is the point."
        ),
        transform=(
            'result = df.groupby("diagnosis", as_index=False)["mean_radius"].mean()'
        ),
        config={
            "chart_type": "bar",
            "x": "diagnosis",
            "y": "mean_radius",
            "title": "Mean radius by diagnosis",
            "x_title": "Diagnosis",
            "y_title": "Mean nucleus radius",
        },
        found="diagnosis and mean_radius.",
        reason="One measure across two groups: bars.",
    ),
]

BY_DATASET: dict[str, list[Example]] = {}
for _example in EXAMPLES:
    BY_DATASET.setdefault(_example.dataset, []).append(_example)

BY_PATH: dict[tuple[str, str], Example] = {(e.dataset, e.slug): e for e in EXAMPLES}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_cache: dict[tuple[str, str, str, int, int], Rendered] = {}
_lock = threading.Lock()


def _run_transform(frame: Any, code: str) -> Any:
    """Execute one of our own transform strings against a copy of the frame.

    The copy matters: the transforms assign derived columns, and the sample
    frame is cached and shared with every other example on the page.
    """
    import pandas as pd

    scope: dict[str, Any] = {"pd": pd, "df": frame.copy()}
    exec(code, scope)  # noqa: S102 - the source is a literal in this module
    result = scope.get("result")
    if result is None:
        raise ValueError("transform bound no `result`")
    return result


def _preview(frame: Any) -> list[dict[str, Any]]:
    head = frame.head(PREVIEW_ROWS)
    rows: list[dict[str, Any]] = []
    for record in head.to_dict("records"):
        rows.append({str(k): _scalar(v) for k, v in record.items()})
    return rows


def _scalar(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        text = value.isoformat()
        return text[:10] if text.endswith("T00:00:00") else text
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def render(
    dataset: str,
    slug: str,
    mode: str = "light",
    width: int = 640,
    height: int = 400,
) -> Rendered | None:
    """Build one example end to end. Cached per worker."""
    example = BY_PATH.get((dataset, slug))
    if example is None:
        return None

    key = (dataset, slug, mode, width, height)
    with _lock:
        hit = _cache.get(key)
    if hit is not None:
        return hit

    trace: list[dict[str, Any]] = []

    def step(name: str, said: str, started: float) -> None:
        trace.append(
            {"name": name, "said": said, "ms": max(1, round((time.perf_counter() - started) * 1000))}
        )

    started = time.perf_counter()
    samples.materialise()
    frame = samples.frame(dataset)
    sample = samples.BY_KEY[dataset]
    source_rows = int(len(frame))
    step(
        "Finding the data",
        f"{sample.name}: {source_rows:,} rows, {len(frame.columns)} columns. "
        f"{example.found}",
        started,
    )

    started = time.perf_counter()
    shaped = _run_transform(frame, example.transform)
    step(
        "Shaping the data",
        f"{source_rows:,} rows in, {len(shaped):,} out across "
        f"{len(shaped.columns)} columns. {example.finding}",
        started,
    )

    started = time.perf_counter()
    config = figures.validate_config(dict(example.config), shaped)
    spec, warnings = figures.build(shaped, config, mode=mode)
    step(
        "Choosing the chart",
        f"{config['chart_type']}. {example.reason}",
        started,
    )

    started = time.perf_counter()
    figure = defaults.apply(spec, mode=mode, chart_type=str(config["chart_type"]))
    findings = defaults.audit(figure, mode=mode)
    if findings:
        # Same contract as the marketing charts: a dataset page that trips our
        # own audit is a real regression, not a cosmetic one.
        log.warning("example %s/%s has audit findings: %s", dataset, slug, findings)
    step(
        "Applying defaults",
        "Palette, spacing, axis titles and legend placement applied. "
        f"{len(findings)} issues found.",
        started,
    )

    try:
        markup = svg.render(
            figure, width=width, height=height, mode=mode,
            transparent=True, theme_vars=True,
        )
    except Exception:  # noqa: BLE001 - a page without a chart still has a dataset
        log.exception("could not render example %s/%s", dataset, slug)
        markup = ""

    rendered = Rendered(
        example=example,
        figure=figure,
        markup=markup,
        trace=trace,
        columns=[str(c) for c in shaped.columns],
        rows=_preview(shaped),
        row_count=int(len(shaped)),
        source_rows=source_rows,
        chart_type=str(config["chart_type"]),
        warnings=list(warnings),
    )
    with _lock:
        _cache[key] = rendered
    return rendered


def lead(dataset: str) -> Example | None:
    """The example that represents a dataset in a listing."""
    found = BY_DATASET.get(dataset)
    return found[0] if found else None


def warm() -> None:
    """Pre-render the lead example of every dataset, both themes.

    Only the leads: the index page shows those, and rendering all nineteen in
    both themes at boot would delay the first request for charts most visitors
    never scroll to.
    """
    for dataset, group in BY_DATASET.items():
        for mode in ("light", "dark"):
            try:
                render(dataset, group[0].slug, mode)
            except Exception:  # noqa: BLE001 - never let a sample break boot
                log.exception("could not warm example for %s", dataset)
