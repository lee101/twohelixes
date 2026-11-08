#!/bin/bash
set -euo pipefail
RAW=var/owid-raw
mkdir -p "$RAW" interp/twohelixes/datasets/fixtures
fetch() {
    local slug="$1"
    local dest="$RAW/$slug.csv"
    curl -fsSL "https://ourworldindata.org/grapher/$slug.csv?useColumnShortNames=true" -o "$dest"
}
for slug in \
    life-expectancy \
    child-mortality \
    children-per-woman-un \
    share-of-adults-defined-as-obese \
    share-of-adults-who-smoke \
    co2-emissions-per-capita \
    share-electricity-renewables \
    share-of-the-population-with-access-to-electricity \
    gdp-per-capita-worldbank \
    share-of-individuals-using-the-internet \
    happiness-cantril-ladder \
    share-of-population-in-extreme-poverty \
    daily-per-capita-caloric-supply \
    meat-supply-per-person
do
    fetch "$slug"
done
BIN=build/owid_bundle
(cd server && pixi run mojo build ../interp/twohelixes/datasets/mojo/owid_bundle.mojo -o ../$BIN)
./$BIN interp/twohelixes/datasets/fixtures/world_health.csv 1990 \
    "$RAW/life-expectancy.csv=life_expectancy" \
    "$RAW/child-mortality.csv=child_mortality_pct" \
    "$RAW/children-per-woman-un.csv=fertility_rate" \
    "$RAW/share-of-adults-defined-as-obese.csv=obesity_pct" \
    "$RAW/share-of-adults-who-smoke.csv=smoking_pct"
./$BIN interp/twohelixes/datasets/fixtures/world_climate.csv 1990 \
    "$RAW/co2-emissions-per-capita.csv=co2_tonnes_per_person" \
    "$RAW/share-electricity-renewables.csv=renewable_electricity_pct" \
    "$RAW/share-of-the-population-with-access-to-electricity.csv=electricity_access_pct"
./$BIN interp/twohelixes/datasets/fixtures/world_prosperity.csv 1990 \
    "$RAW/gdp-per-capita-worldbank.csv=gdp_per_capita_ppp" \
    "$RAW/share-of-individuals-using-the-internet.csv=internet_users_pct" \
    "$RAW/happiness-cantril-ladder.csv=happiness_score" \
    "$RAW/share-of-population-in-extreme-poverty.csv=extreme_poverty_pct" \
    "$RAW/life-expectancy.csv=life_expectancy"
./$BIN interp/twohelixes/datasets/fixtures/world_food.csv 1990 \
    "$RAW/daily-per-capita-caloric-supply.csv=daily_calories" \
    "$RAW/meat-supply-per-person.csv=meat_kg_per_person" \
    "$RAW/gdp-per-capita-worldbank.csv=gdp_per_capita_ppp" \
    "$RAW/share-of-adults-defined-as-obese.csv=obesity_pct"
wc -l interp/twohelixes/datasets/fixtures/world_*.csv
