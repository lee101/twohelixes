"""Small live, synthetic-data eval for route availability and numeric insight quality.

Run through monitoring/run_agent.py. Uses only the standard library; no model
weights or GPU. Credentials stay in headers and are never written to artifacts.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "interp"))
from twohelixes import config
from twohelixes.pipeline.prompts import GRAPH_SYSTEM

CASES = {
    "time": {"question": "How did revenue change over time?", "data": {"month": ["Jan", "Feb", "Mar"], "revenue": [12, 20, 42]}, "facts": {"absolute_change": 30, "ratio": 3.5}, "charts": ["line", "area"]},
    "category": {"question": "Compare regional sales and report the total.", "data": {"region": ["North", "South", "East"], "sales": [12, 8, 5]}, "facts": {"total": 25, "leader_value": 12}, "charts": ["bar", "hbar"]},
    "relationship": {"question": "Show the relationship between x and y, without claiming causation.", "data": {"x": [1, 2, 3, 4], "y": [2, 4, 6, 8]}, "facts": {"pearson_r": 1}, "charts": ["scatter"]},
    "distribution": {"question": "Show the distribution, median and maximum.", "data": {"latency_ms": [1, 2, 2, 3, 100]}, "facts": {"median": 2, "maximum": 100}, "charts": ["histogram", "box"]},
    "missing": {"question": "Compare the observed values; report missing count and the mean of observed values only.", "data": {"group": ["A", "B", "C", "D"], "value": [10, None, 30, None]}, "facts": {"missing_count": 2, "mean": 20}, "charts": ["bar", "hbar"]},
    "geographic": {"question": "Map population by country, report the total and the largest population.", "data": {"country": ["France", "Germany", "Italy"], "population_m": [68, 84, 59]}, "facts": {"total": 211, "maximum": 84}, "charts": ["map", "choropleth"]},
}


def evaluate(answer: dict) -> list[dict]:
    results = []
    for name, case in CASES.items():
        row = answer.get(name, {})
        row = row if isinstance(row, dict) else {}
        facts = row.get("facts", {})
        facts = facts if isinstance(facts, dict) else {}
        errors = []
        for key, expected in case["facts"].items():
            observed = facts.get(key)
            if not isinstance(observed, (int, float)) or isinstance(observed, bool) or not math.isclose(observed, expected, abs_tol=1e-6):
                errors.append(f"{key}: expected {expected}, got {observed}")
        if row.get("chart_type") not in case["charts"]:
            errors.append(f"unexpected chart type: {row.get('chart_type')}")
        results.append({"case": name, "passed": not errors, "errors": errors})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--models", nargs="+", default=["deepseek-v4-flash", "openpaths/free", "gpt-6-luna"])
    parser.add_argument("--gateway-luna", action="store_true", help="Explicitly check Luna through the gateway")
    args = parser.parse_args()
    prompt = {
        name: {"question": case["question"], "data": case["data"], "fact_keys": list(case["facts"])}
        for name, case in CASES.items()
    }
    report = []
    for model in args.models:
        key, base, provider = config.llm_credentials()
        if model == "gpt-6-luna" and not args.gateway_luna:
            key, base, provider = config.get("OPENAI_API_KEY"), "https://api.openai.com/v1", "openai"
        elif model.startswith("deepseek-") and config.deepseek_credentials()[0]:
            key, base = config.deepseek_credentials()
            provider = "deepseek"
        started = time.monotonic()
        row = {"requested_model": model, "provider": provider}
        try:
            if not key or not base:
                raise ValueError("provider is not configured")
            payload = {"model": model, "messages": [
                {"role": "system", "content": GRAPH_SYSTEM + '\nFor this evaluation return one JSON object keyed by case name. Each value must contain chart_type and facts (numeric values for every requested fact_key).'},
                {"role": "user", "content": json.dumps(prompt)},
            ]}
            payload.update({"max_completion_tokens": 2048, "reasoning_effort": "low"} if model == "gpt-6-luna" else {"max_tokens": 2048})
            request = urllib.request.Request(base.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=25) as response:
                body = json.load(response)
            text = body["choices"][0]["message"]["content"] or ""
            answer = json.loads(text[text.index("{") : text.rindex("}") + 1])
            row.update(served_model=body.get("model"), checks=evaluate(answer), answer=answer)
            row["passed"] = all(check["passed"] for check in row["checks"])
        except Exception as exc:
            row.update(passed=False, error_type=type(exc).__name__, status=getattr(exc, "code", None))
        row["seconds"] = round(time.monotonic() - started, 3)
        report.append(row)
        print(json.dumps({key: value for key, value in row.items() if key != "answer"}), flush=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2))
    return 0 if all(row["passed"] for row in report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
