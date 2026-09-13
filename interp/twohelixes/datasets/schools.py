"""Published school directory and optional result index."""
import json
from pathlib import Path

DIRECTORY = Path(__file__).parent / "fixtures"


def dataset():
    return json.loads((DIRECTORY / "schools.json").read_text())


def frame():
    import pandas as pd
    return pd.DataFrame(dataset()["schools"])


def map_html():
    return (DIRECTORY / "schools.html").read_text()
