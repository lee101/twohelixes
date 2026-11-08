"""World prosperity and wellbeing: income, internet, happiness, poverty, life expectancy.

Real Our World in Data series (CC BY 4.0). Empty cells are missing, never
zero, so every worked example drops missing values explicitly before it
ranks or plots anything.
"""

from __future__ import annotations

from pathlib import Path

from ..examples import Example
from ..samples import Sample

_SOURCE = "Our World in Data (CC BY 4.0); built by scripts/build-world-fixtures.sh"


def _build():
    import pandas as pd

    frame = pd.read_csv(Path(__file__).parents[1] / "fixtures" / "world_prosperity.csv")
    return frame


SAMPLE = Sample(
    key="world_prosperity",
    name="World prosperity",
    description=(
        "GDP per capita (PPP), internet use, happiness, extreme poverty and "
        "life expectancy by country and year."
    ),
    build=_build,
    source=_SOURCE,
    questions=[
        "which countries have the highest GDP per capita in the latest year?",
        "how much has GDP per capita changed since 1990 by country?",
        "is life expectancy related to GDP per capita in the latest year?",
    ],
)

EXAMPLES = [
    Example(
        dataset="world_prosperity",
        slug="richest-countries-latest-year",
        question="which countries have the highest GDP per capita in the latest year?",
        headline="GDP per capita in the latest year, top 20",
        finding=(
            "Each country contributes a single row: its most recent year with a "
            "GDP per capita value, so the ranking compares like with like "
            "instead of mixing years."
        ),
        transform=(
            'd = df.dropna(subset=["gdp_per_capita_ppp"])\n'
            'latest = d.loc[d.groupby("country")["year"].idxmax()]\n'
            'result = (latest.sort_values("gdp_per_capita_ppp", ascending=False)\n'
            '          .head(20)[["country", "gdp_per_capita_ppp"]])'
        ),
        config={
            "chart_type": "hbar",
            "x": "country",
            "y": "gdp_per_capita_ppp",
            "title": "GDP per capita in the latest year",
            "x_title": "Country",
            "y_title": "GDP per capita (PPP)",
        },
        found="country, year and gdp_per_capita_ppp; missing values are dropped, never read as zero.",
        reason="Country names are labels and income is one measure, so sorted horizontal bars read best.",
    ),
    Example(
        dataset="world_prosperity",
        slug="gdp-change-since-1990",
        question="how much has GDP per capita changed since 1990 by country?",
        headline="Change in GDP per capita since 1990, top 20",
        finding=(
            "Every bar is a difference between a country's 1990 value and its "
            "most recent value, so the chart shows how far each country moved "
            "over the period rather than where it ended up."
        ),
        transform=(
            'd = df.dropna(subset=["gdp_per_capita_ppp"])\n'
            'early = (d[d["year"] == 1990][["country", "gdp_per_capita_ppp"]]\n'
            '         .rename(columns={"gdp_per_capita_ppp": "gdp_1990"}))\n'
            'latest = d.loc[d.groupby("country")["year"].idxmax(), ["country", "gdp_per_capita_ppp"]]\n'
            'latest = latest.rename(columns={"gdp_per_capita_ppp": "gdp_latest"})\n'
            'merged = early.merge(latest, on="country")\n'
            'merged["change"] = merged["gdp_latest"] - merged["gdp_1990"]\n'
            'result = (merged.sort_values("change", ascending=False)\n'
            '          .head(20)[["country", "change"]].reset_index(drop=True))'
        ),
        config={
            "chart_type": "hbar",
            "x": "country",
            "y": "change",
            "title": "Change in GDP per capita since 1990",
            "x_title": "Country",
            "y_title": "Change since 1990",
        },
        found="country, year and gdp_per_capita_ppp for 1990 and the latest year; missing values are dropped.",
        reason="A difference per country is one measure with text labels, so sorted horizontal bars are clearest.",
    ),
    Example(
        dataset="world_prosperity",
        slug="gdp-vs-life-expectancy-latest-year",
        question="is life expectancy related to GDP per capita in the latest year?",
        headline="Life expectancy against GDP per capita, latest year",
        finding=(
            "One point per country, each at its most recent year where both "
            "measures are present, so gaps in either series cannot masquerade "
            "as zeros and pull a country to the corner of the plot."
        ),
        transform=(
            'd = df.dropna(subset=["gdp_per_capita_ppp", "life_expectancy"])\n'
            'latest = d.loc[d.groupby("country")["year"].idxmax()]\n'
            'result = latest[["gdp_per_capita_ppp", "life_expectancy"]].reset_index(drop=True)'
        ),
        config={
            "chart_type": "scatter",
            "x": "gdp_per_capita_ppp",
            "y": "life_expectancy",
            "title": "Life expectancy against GDP per capita",
            "x_title": "GDP per capita (PPP)",
            "y_title": "Life expectancy",
        },
        found="gdp_per_capita_ppp and life_expectancy in each country's latest year with both; missing values are dropped.",
        reason="Two measures with no grouping is a scatter: one point per country shows the relationship directly.",
    ),
    Example(
        dataset="world_prosperity",
        slug="gdp-trends-five-countries",
        question="how has GDP per capita changed over time in Canada, China, Chile, South Africa and Zimbabwe?",
        headline="GDP per capita over time for five countries",
        finding=(
            "The same five countries are followed across every year they "
            "report, so the lines stay comparable and no country enters or "
            "leaves the story halfway through."
        ),
        transform=(
            'df["year_date"] = pd.to_datetime(df["year"].astype(str) + "-01-01")\n'
            'picked = df[df["country"].isin(["Canada", "China", "Chile", "South Africa", "Zimbabwe"])]\n'
            'picked = picked.dropna(subset=["gdp_per_capita_ppp"])\n'
            'result = picked[["year_date", "country", "gdp_per_capita_ppp"]].sort_values(["country", "year_date"])'
        ),
        config={
            "chart_type": "line",
            "x": "year_date",
            "y": "gdp_per_capita_ppp",
            "color": "country",
            "title": "GDP per capita over time",
            "x_title": "Year",
            "y_title": "GDP per capita (PPP)",
        },
        found="country, year and gdp_per_capita_ppp for Canada, China, Chile, South Africa and Zimbabwe; missing values are dropped.",
        reason="A measure over time with one line per country is a shared-axis line chart.",
    ),
]
