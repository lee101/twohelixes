"""World climate and energy: emissions, renewable electricity, access."""

from __future__ import annotations

from pathlib import Path

from ..examples import Example
from ..samples import Sample


def _build():
    import pandas as pd

    frame = pd.read_csv(Path(__file__).parents[1] / "fixtures" / "world_climate.csv")
    frame = frame.dropna(subset=["year"]).copy()
    frame["year"] = frame["year"].astype(int)
    return frame


def _make():
    sample = Sample(
        key="world_climate",
        name="World climate and energy",
        description=(
            "CO2 emissions per person, renewable share of electricity and "
            "electricity access by country and year."
        ),
        build=_build,
        source="Our World in Data (CC BY 4.0); built by scripts/build-world-fixtures.sh",
        questions=[
            "which countries emit the most CO2 per person in the latest year?",
            "how has CO2 per person changed since 1990 by country?",
            "is a higher renewable electricity share linked to lower CO2 per person?",
        ],
    )

    examples = [
        Example(
            dataset="world_climate",
            slug="highest-emissions-per-person",
            question="which countries emit the most CO2 per person in the latest year?",
            headline="Highest CO2 per person, latest year per country",
            finding=(
                "Each country contributes its own latest observed year, so the "
                "ranking compares most countries on recent data without filling "
                "missing years with zero."
            ),
            transform=(
                'df = df.dropna(subset=["co2_tonnes_per_person"])\n'
                'latest = df.groupby("country")["year"].transform("max")\n'
                'result = df[df["year"] == latest][["country", "co2_tonnes_per_person"]]\n'
                'result = result.sort_values("co2_tonnes_per_person", ascending=False)\n'
                'result = result.head(15).reset_index(drop=True)'
            ),
            config={
                "chart_type": "hbar",
                "x": "country",
                "y": "co2_tonnes_per_person",
                "title": "CO2 per person by country",
                "x_title": "Country",
                "y_title": "CO2 tonnes per person",
            },
            found="country and co2_tonnes_per_person; the year is each country's latest observed year.",
            reason="Country names are long labels and emissions are one measure, so sorted horizontal bars read best.",
        ),
        Example(
            dataset="world_climate",
            slug="emissions-change-since-1990",
            question="how has CO2 per person changed since 1990 by country?",
            headline="Change in CO2 per person since 1990",
            finding=(
                "The latest observed value minus the 1990 value, shown for the "
                "countries with the largest absolute change in either direction."
            ),
            transform=(
                'df = df.dropna(subset=["co2_tonnes_per_person"])\n'
                'base = df[df["year"] == 1990].set_index("country")["co2_tonnes_per_person"]\n'
                'last_rows = df.loc[df.groupby("country")["year"].idxmax()].set_index("country")\n'
                'last = last_rows["co2_tonnes_per_person"]\n'
                'change = (last - base).dropna()\n'
                'ranked = change.abs().sort_values(ascending=False).head(20)\n'
                'result = ranked.index.to_frame(index=False)\n'
                'result["co2_change"] = change.reindex(result["country"]).values\n'
                'result = result.sort_values("co2_change", ascending=False).reset_index(drop=True)'
            ),
            config={
                "chart_type": "hbar",
                "x": "country",
                "y": "co2_change",
                "title": "Change in CO2 per person since 1990",
                "x_title": "Country",
                "y_title": "Change since 1990 (tonnes per person)",
            },
            found="country, year and co2_tonnes_per_person; only countries with both a 1990 and a later value appear.",
            reason="A signed change across named countries is a ranking, so horizontal bars keep the largest movers legible.",
        ),
        Example(
            dataset="world_climate",
            slug="renewables-against-emissions",
            question="is a higher renewable electricity share linked to lower CO2 per person?",
            headline="Renewable share against CO2 per person, latest year per country",
            finding=(
                "One point per country in its latest year with both indicators "
                "observed, so the relationship is read across countries rather "
                "than over time."
            ),
            transform=(
                'df = df.dropna(subset=["renewable_electricity_pct", "co2_tonnes_per_person"])\n'
                'latest = df.groupby("country")["year"].transform("max")\n'
                'result = df[df["year"] == latest][["country", "renewable_electricity_pct", "co2_tonnes_per_person"]]\n'
                'result = result.sort_values("renewable_electricity_pct").reset_index(drop=True)'
            ),
            config={
                "chart_type": "scatter",
                "x": "renewable_electricity_pct",
                "y": "co2_tonnes_per_person",
                "title": "Renewable share against CO2 per person",
                "x_title": "Renewable electricity (%)",
                "y_title": "CO2 tonnes per person",
            },
            found="renewable_electricity_pct and co2_tonnes_per_person in each country's latest year with both values.",
            reason="Two measures with no time axis is a scatter; no colour column keeps the palette to a single series.",
        ),
        Example(
            dataset="world_climate",
            slug="major-emitters-trends",
            question="how has CO2 per person trended in China, the United States, India, the United Kingdom and Brazil?",
            headline="CO2 per person over time for five countries",
            finding=(
                "Five named countries on the same year axis, so the shapes and "
                "starting points of the longer-run trends can be compared "
                "without a field of unreadable lines."
            ),
            transform=(
                'names = ["China", "United States", "India", "United Kingdom", "Brazil"]\n'
                'df = df[df["country"].isin(names)].dropna(subset=["co2_tonnes_per_person"])\n'
                'df["year_date"] = pd.to_datetime(df["year"].astype(str) + "-01-01")\n'
                'result = df[["year_date", "country", "co2_tonnes_per_person"]].sort_values(["year_date", "country"])\n'
                'result = result.reset_index(drop=True)'
            ),
            config={
                "chart_type": "line",
                "x": "year_date",
                "y": "co2_tonnes_per_person",
                "color": "country",
                "title": "CO2 per person by country",
                "x_title": "Year",
                "y_title": "CO2 tonnes per person",
            },
            found="country, year and co2_tonnes_per_person for the five named countries.",
            reason="Time on x with one line per country is the shared-axis line chart; five series stays inside the palette limit.",
        ),
    ]

    return sample, examples


SAMPLE, EXAMPLES = _make()
