"""World food and health indicators (Our World in Data, CC BY 4.0)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..examples import Example
from ..samples import Sample


def _build() -> Any:
    import pandas as pd

    return pd.read_csv(Path(__file__).parents[1] / "fixtures" / "world_food.csv")


SAMPLE = Sample(
    key="world_food",
    name="World food and health",
    description=(
        "Daily calories, meat eaten per person, GDP per capita and obesity "
        "by country and year. Empty cells mean missing, never zero."
    ),
    build=_build,
    source="Our World in Data (CC BY 4.0); built by scripts/build-world-fixtures.sh",
    questions=[
        "which countries eat the most meat per person in the latest year?",
        "how has obesity changed since 1990 by country?",
        "does income track obesity in the latest year?",
        "how have daily calories trended in five countries?",
    ],
)

EXAMPLES: list[Example] = [
    Example(
        dataset="world_food",
        slug="meat-per-person-latest-year",
        question="which countries eat the most meat per person in the latest year?",
        headline="Meat eaten per person, latest year with data",
        finding=(
            "A ranking, not a full map: only the latest year on record for each "
            "meat series is used, and countries with no observation that year are "
            "left out rather than plotted as zero."
        ),
        transform=(
            'latest = df.dropna(subset=["meat_kg_per_person"])["year"].max()\n'
            'result = (\n'
            '    df[df["year"] == latest]\n'
            '    .dropna(subset=["meat_kg_per_person"])\n'
            '    .sort_values("meat_kg_per_person", ascending=False)\n'
            '    .head(20)[["country", "meat_kg_per_person"]]\n'
            ")"
        ),
        config={
            "chart_type": "hbar",
            "x": "country",
            "y": "meat_kg_per_person",
            "title": "Meat per person, latest year with data",
            "x_title": "Country",
            "y_title": "Meat (kg per person)",
        },
        found="country, year and meat_kg_per_person; income, calories and obesity are not in this cut.",
        reason=(
            "Country names are long labels and the question is a ranking, so "
            "horizontal bars sorted descending read best."
        ),
    ),
    Example(
        dataset="world_food",
        slug="obesity-change-since-1990",
        question="how has obesity changed since 1990 by country?",
        headline="Change in obesity since 1990, by country",
        finding=(
            "The change is latest minus 1990 for each country separately, so a "
            "country that only appears late in the series cannot invent a change. "
            "Countries missing either endpoint stay out of the ranking."
        ),
        transform=(
            'base = df[df["year"] == 1990].dropna(subset=["obesity_pct"])[["country", "obesity_pct"]].rename(columns={"obesity_pct": "obesity_1990"})\n'
            'latest_year = df.dropna(subset=["obesity_pct"])["year"].max()\n'
            'now = df[df["year"] == latest_year].dropna(subset=["obesity_pct"])[["country", "obesity_pct"]].rename(columns={"obesity_pct": "obesity_latest"})\n'
            'joined = base.merge(now, on="country", how="inner")\n'
            'joined["change_pp"] = joined["obesity_latest"] - joined["obesity_1990"]\n'
            'result = joined.sort_values("change_pp", ascending=False).head(20)[["country", "change_pp"]]'
        ),
        config={
            "chart_type": "hbar",
            "x": "country",
            "y": "change_pp",
            "title": "Obesity change since 1990 (percentage points)",
            "x_title": "Country",
            "y_title": "Change (percentage points)",
        },
        found="country, year and obesity_pct at the 1990 and latest endpoints.",
        reason=(
            "One signed change per country is a ranking, and horizontal bars keep "
            "the country labels legible at this length."
        ),
    ),
    Example(
        dataset="world_food",
        slug="income-against-obesity-latest",
        question="does income track obesity in the latest year?",
        headline="Income against obesity, latest year with both",
        finding=(
            "Both measures come from the one latest year where a country has both "
            "an income and an obesity figure, so the scatter compares countries "
            "in the same year instead of mixing vintages."
        ),
        transform=(
            'usable = df.dropna(subset=["gdp_per_capita_ppp", "obesity_pct"])\n'
            'latest = usable["year"].max()\n'
            'result = usable[usable["year"] == latest][["country", "gdp_per_capita_ppp", "obesity_pct"]]'
        ),
        config={
            "chart_type": "scatter",
            "x": "gdp_per_capita_ppp",
            "y": "obesity_pct",
            "title": "GDP per capita against obesity",
            "x_title": "GDP per capita (PPP)",
            "y_title": "Obesity (%)",
        },
        found="country, gdp_per_capita_ppp and obesity_pct in the latest shared year.",
        reason=(
            "Two measures, no category and no time: a scatter of one point per "
            "country makes the relationship itself the chart."
        ),
    ),
    Example(
        dataset="world_food",
        slug="daily-calories-five-countries",
        question="how have daily calories trended in five countries?",
        headline="Daily calories over time, five countries",
        finding=(
            "Five named countries only, so the lines stay distinguishable. Their "
            "different starting points and directions are the comparison; a "
            "country missing a year simply has no point there."
        ),
        transform=(
            'picked = ["United States", "Australia", "Brazil", "South Africa", "China"]\n'
            'result = df[df["country"].isin(picked)].dropna(subset=["daily_calories"])[["year", "country", "daily_calories"]].sort_values(["country", "year"])'
        ),
        config={
            "chart_type": "line",
            "x": "year",
            "y": "daily_calories",
            "color": "country",
            "title": "Daily calories per person, five countries",
            "x_title": "Year",
            "y_title": "Calories per person per day",
        },
        found="country, year and daily_calories for the five named countries.",
        reason=(
            "A shared year axis with one line per country shows divergence and "
            "convergence; five lines stay under the eight-line limit."
        ),
    ),
]
