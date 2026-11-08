"""World health and longevity: life expectancy, child mortality, fertility,
obesity and smoking by country and year (Our World in Data, CC BY 4.0).

Empty cells in the fixture are missing values, never zero; every transform
below drops them explicitly with dropna before ranking or plotting.
Imported at the bottom of samples.py / examples.py, after Sample and Example
exist, so top-level imports here are safe.
"""

from pathlib import Path

from ..samples import Sample
from ..examples import Example


def _build():
    import pandas as pd

    path = Path(__file__).parents[1] / "fixtures" / "world_health.csv"
    return pd.read_csv(path)


SAMPLE = Sample(
    key="world_health",
    name="World health and longevity",
    description=(
        "Life expectancy, child mortality, fertility, obesity and smoking "
        "by country and year. Empty cells are missing, never zero."
    ),
    build=_build,
    source="Our World in Data (CC BY 4.0); built by scripts/build-world-fixtures.sh",
    questions=[
        "which countries have the highest life expectancy in the latest year?",
        "how has life expectancy changed since 1990 by country?",
        "is child mortality related to fertility in the latest year?",
        "how did life expectancy trend for Canada, Switzerland and Zimbabwe?",
    ],
)

EXAMPLES = [
    Example(
        dataset="world_health",
        slug="highest-life-expectancy-latest-year",
        question="which countries have the highest life expectancy in the latest year?",
        headline="Highest life expectancy, latest year per country",
        finding=(
            "Each country contributes its own latest observed value, so the "
            "ranking is not distorted by the different years in which each "
            "series ends."
        ),
        transform=(
            'latest = df.dropna(subset=["life_expectancy"]).sort_values("year")\n'
            'latest = latest.groupby("country", as_index=False).tail(1)\n'
            'result = (latest[["country", "life_expectancy"]]\n'
            '          .sort_values("life_expectancy", ascending=False)\n'
            '          .head(25)\n'
            '          .reset_index(drop=True))'
        ),
        config={
            "chart_type": "hbar",
            "x": "country",
            "y": "life_expectancy",
            "title": "Highest life expectancy",
            "x_title": "Country",
            "y_title": "Life expectancy (years)",
        },
        found="country, year and life_expectancy; missing values are dropped, never read as zero.",
        reason=(
            "A ranking of long country names is a horizontal bar chart, "
            "capped at twenty-five rows so the top of the distribution "
            "stays readable."
        ),
    ),
    Example(
        dataset="world_health",
        slug="life-expectancy-change-since-1990",
        question="how has life expectancy changed since 1990 by country?",
        headline="Change in life expectancy since 1990",
        finding=(
            "Change is measured per country between its 1990 value and its "
            "latest observed value, so countries with different coverage "
            "are still compared on their own history."
        ),
        transform=(
            'base = df[df["year"] == 1990][["country", "life_expectancy"]].dropna()\n'
            'base = base.rename(columns={"life_expectancy": "life_1990"})\n'
            'latest = df.dropna(subset=["life_expectancy"]).sort_values("year")\n'
            'latest = latest.groupby("country", as_index=False).tail(1)\n'
            'latest = latest[["country", "life_expectancy"]].rename(\n'
            '    columns={"life_expectancy": "life_latest"})\n'
            'joined = base.merge(latest, on="country", how="inner")\n'
            'joined["change"] = joined["life_latest"] - joined["life_1990"]\n'
            'result = (joined.sort_values("change", ascending=False)\n'
            '          .head(25)\n'
            '          .reset_index(drop=True))'
        ),
        config={
            "chart_type": "hbar",
            "x": "country",
            "y": "change",
            "title": "Life expectancy change since 1990",
            "x_title": "Country",
            "y_title": "Change (years)",
        },
        found="country, year and life_expectancy in 1990 and in the latest year per country.",
        reason=(
            "A per-country before-and-after difference is one bar per "
            "country, sorted by the size of the change."
        ),
    ),
    Example(
        dataset="world_health",
        slug="child-mortality-against-fertility",
        question="is child mortality related to fertility in the latest year?",
        headline="Child mortality against fertility, latest year per country",
        finding=(
            "Each country is one point at its latest observed pair, so the "
            "shape of the relationship across countries is visible without "
            "mixing years."
        ),
        transform=(
            'latest = df.dropna(subset=["child_mortality_pct", "fertility_rate"])\n'
            'latest = latest.sort_values("year")\n'
            'latest = latest.groupby("country", as_index=False).tail(1)\n'
            'result = latest[["fertility_rate", "child_mortality_pct"]].dropna()'
        ),
        config={
            "chart_type": "scatter",
            "x": "fertility_rate",
            "y": "child_mortality_pct",
            "title": "Child mortality against fertility",
            "x_title": "Fertility rate (children per woman)",
            "y_title": "Child mortality (%)",
        },
        found="fertility_rate and child_mortality_pct at the latest year per country; missing pairs are dropped.",
        reason=(
            "Two measures with no time axis is a scatter; one point per "
            "country keeps every country visible."
        ),
    ),
    Example(
        dataset="world_health",
        slug="life-expectancy-trends-five-countries",
        question="how did life expectancy trend for Canada, Switzerland, Zimbabwe, Portugal and Cambodia?",
        headline="Life expectancy over time for five countries",
        finding=(
            "Five named countries with very different starting points on one "
            "year axis show how their paths diverge over the same period."
        ),
        transform=(
            'result = (df[df["country"].isin('
            '["Canada", "Switzerland", "Zimbabwe", "Portugal", "Cambodia"])].dropna(\n'
            '    subset=["life_expectancy"])\n'
            '          [["year", "country", "life_expectancy"]])'
        ),
        config={
            "chart_type": "line",
            "x": "year",
            "y": "life_expectancy",
            "color": "country",
            "title": "Life expectancy by country",
            "x_title": "Year",
            "y_title": "Life expectancy (years)",
        },
        found="year, country and life_expectancy for the five named countries; missing years are dropped.",
        reason=(
            "A measure over time with one line per country, capped at "
            "five series so the lines stay distinguishable."
        ),
    ),
]
