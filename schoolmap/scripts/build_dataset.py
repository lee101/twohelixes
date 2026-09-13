#!/usr/bin/env python3
"""Build school locations and optional achievement scores; never impute results."""
import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, pstdev

ROOT = Path(__file__).resolve().parents[1]
WEIGHTS = {"reading": .30, "numeracy": .30, "writing": .20,
           "spelling": .10, "grammar_punctuation": .10}
YEARS = {3, 5, 7, 9}


def rank_results(rows):
    """One release only, equal year-level weights, comparable within each cell."""
    seen, cells, schools, releases = set(), {}, {}, set()
    for row in rows:
        sid = str(row["school_id"]).strip()
        year, domain = int(row["year_level"]), row["domain"]
        release = int(row["assessment_year"])
        if not sid or year not in YEARS or domain not in WEIGHTS:
            raise ValueError("Invalid school ID, year level or domain")
        releases.add(release)
        cell = (sid, year, domain)
        if cell in seen:
            raise ValueError(f"Duplicate result: {cell}")
        seen.add(cell)
        schools.setdefault(sid, {}).setdefault(year, {})
        if row["mean_score"].strip() in {"", "*", "-", "NA", "suppressed"}:
            continue
        value = float(row["mean_score"])
        if not math.isfinite(value) or not 0 <= value <= 1000:
            raise ValueError("Score must be finite and between 0 and 1000")
        schools[sid][year][domain] = value
        cells.setdefault((year, domain), []).append(value)
    if len(releases) > 1:
        raise ValueError("Build one assessment year at a time")
    stats = {key: (mean(v), pstdev(v)) for key, v in cells.items() if len(v) >= 2}
    scores = {}
    for sid, years in schools.items():
        zs, coverage = [], 0
        for year, domains in years.items():
            valid = {d: v for d, v in domains.items() if (year, d) in stats}
            weight = sum(WEIGHTS[d] for d in valid)
            coverage += weight
            if weight >= .8:
                zs.append(sum(WEIGHTS[d] * ((v - stats[year, d][0]) / stats[year, d][1]
                             if stats[year, d][1] else 0) for d, v in valid.items()) / weight)
        # Require adequate data in every represented year level.
        if zs and len(zs) == len(years):
            z = mean(zs)
            scores[sid] = {"achievement_score": round(50 * (1 + math.erf(z / math.sqrt(2))), 2),
                           "coverage": round(coverage / len(years), 3),
                           "assessment_year": next(iter(releases)),
                           "year_levels": ",".join(map(str, sorted(years)))}
    last, rank = None, 0
    for i, (_, result) in enumerate(sorted(scores.items(), key=lambda p: -p[1]["achievement_score"]), 1):
        if result["achievement_score"] != last:
            rank, last = i, result["achievement_score"]
        result["rank"] = rank
    return scores


def build(location_path, result_path=None):
    raw = json.loads(location_path.read_text())
    records = raw["result"]["records"]
    if not raw.get("success") or len(records) != raw["result"]["total"]:
        raise ValueError("Location download is incomplete")
    scores = rank_results(list(csv.DictReader(result_path.open()))) if result_path else {}
    rows, seen = [], set()
    for row in records:
        sid = str(row["Centre Code"])
        if sid in seen:
            raise ValueError(f"Duplicate location ID {sid}")
        seen.add(sid)
        lat, lon = row.get("Latitude"), row.get("Longitude")
        located = isinstance(lat, (float, int)) and isinstance(lon, (float, int)) and -30 < lat < -9 and 137 < lon < 155
        item = {"school_id": sid, "school_name": row["Centre Name"],
                "address": ", ".join(str(row.get(f"Actual Address Line {i}") or "") for i in (1, 2) if row.get(f"Actual Address Line {i}")),
                "suburb": row.get("Actual Address Line 3", ""), "postcode": row.get("Actual Address Post Code"),
                "state": "QLD", "sector": row.get("Sector"), "school_type": row.get("Centre Type"),
                "latitude": lat if located else None, "longitude": lon if located else None,
                "location_year": 2020, "achievement_score": None, "rank": None,
                "coverage": None, "assessment_year": None, "year_levels": ""}
        item.update(scores.get(sid, {}))
        rows.append(item)
    unmatched = sorted(set(scores) - seen)
    return {"schools": rows, "metadata": {
        "scope": "Queensland school directory, May 2020 snapshot",
        "results_status": "available" if scores else "not_available",
        "unmatched_result_ids": unmatched,
        "location_source": "https://www.data.qld.gov.au/dataset/state-and-non-state-school-details",
        "licence": "CC BY 4.0", "attribution": "Queensland Department of Education; modified by Twohelixes",
        "source_sha256": hashlib.sha256(location_path.read_bytes()).hexdigest(),
        "weights": WEIGHTS, "method": "Domain/year-level z scores across supplied schools; weighted domains, equal represented year levels; normal-CDF index. Minimum 80% domain weight per year. Not a measure of overall school quality.",
        "coverage_note": "Coverage describes supplied year levels; it does not establish whether all school year levels were supplied."
    }}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path)
    parser.add_argument("--locations", type=Path, default=ROOT / "data/raw/qld_locations_2020.json")
    parser.add_argument("--output", type=Path, default=ROOT / "public/schools.json")
    args = parser.parse_args()
    dataset = build(args.locations, args.results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(dataset, allow_nan=False))
    print(f"Built {len(dataset['schools'])} school rows; results: {dataset['metadata']['results_status']}")
