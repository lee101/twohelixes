"""Published school directory and optional result index."""
import json
from pathlib import Path

DIRECTORY = Path(__file__).parent / "fixtures"


def dataset(state="qld"):
    filename = {"qld": "schools.json", "nsw": "nsw_schools.json"}[state]
    return json.loads((DIRECTORY / filename).read_text())


def frame(state="qld"):
    import pandas as pd
    return pd.DataFrame(dataset(state)["schools"])


def map_html(state="qld"):
    return (DIRECTORY / {"qld": "schools.html", "nsw": "nsw_schools.html"}[state]).read_text()


def naplan_results():
    return json.loads((DIRECTORY / "nsw_naplan_results.json").read_text())


def naplan_frame():
    import pandas as pd
    return pd.DataFrame(naplan_results()["results"])
