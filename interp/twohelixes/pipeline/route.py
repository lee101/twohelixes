"""Deciding the chart without asking a model.

A large share of real questions name their own chart. "how did revenue trend
over time", "which channel brings the most", "what share of MRR", "is X
correlated with Y" - each of those fixes the form before any model sees it, and
the columns come from roles we have already computed. Sending them to a model
buys nothing and costs a second and a half of the user's wait.

So this is a **gate, not a chooser**. It answers only when the question carries
an unambiguous cue *and* the data supports it; anything else returns `None` and
the model stage runs exactly as before. That asymmetry is the whole design: a
false negative costs one model call, a false positive costs the user a wrong
chart, so every rule here is written to abstain.

Three properties are deliberate:

* **One cue, or nothing.** A question that trips two families ("share of
  revenue over time") is ambiguous by construction and goes to the model.
* **The data has the veto.** "over time" with no time column is not a line
  chart, it is a misread question.
* **It never invents columns.** Everything it names comes from
  `tools.find_*`, and `figures.validate_config` still repairs the result
  against the real frame.

This is also the seam an embedding classifier plugs into: `classify()` is the
only entry point, and a vector nearest-neighbour over labelled phrasings would
replace `_match_family` without touching anything else. Lexical first because
it is measurable today - `tests/test_route.py` reports the hit rate over every
sample question - and because a model that is wrong 5% of the time is worse
here than no model at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from twohelixes import config
from twohelixes.interpreter import tools
from twohelixes.pipeline import semantic

log = logging.getLogger("twohelixes.pipeline.route")


@dataclass
class Routed:
    config: dict[str, Any]
    family: str
    cue: str

    @property
    def reason(self) -> str:
        return f"'{self.cue}' with the columns to support it - no model call needed."


# Cues are matched as whole words against a normalised question. Ordering does
# not matter: a question that matches more than one family is refused, so the
# families cannot compete.
FAMILIES: dict[str, tuple[str, ...]] = {
    "time": (
        "over time", "trend", "trended", "trending", "by month", "monthly",
        "by week", "weekly", "by day", "daily", "by quarter", "quarterly",
        "by year", "yearly", "over the year", "per month", "per day",
        "time series", "growth", "grew", "grow", "this year", "last year",
        "over the last",
    ),
    "share": (
        "share of", "proportion", "proportions", "percentage of total",
        "split across", "splits across", "breakdown of", "mix of", "make up",
        "what fraction",
    ),
    "correlation": (
        "correlated", "correlation", "correlate", "correlates",
        "relationship between", "against", "versus", "vs",
        "plotted against",
    ),
    "distribution": (
        "distribution", "distributed", "histogram", "spread of", "how spread",
    ),
    "ranking": (
        "which topic", "which channel", "which source", "which category",
        "which region", "which product", "top", "most", "least", "biggest",
        "largest", "smallest", "highest", "lowest", "rank", "ranked",
        "compare", "comparison", "by category", "by channel", "by region",
        "by source", "by topic", "by priority", "by plan", "by species",
        "by segment", "by type",
    ),
}

# Words that make a question compound enough that a cue is not evidence. "and"
# alone is too common to disqualify anything; these are the ones that actually
# signal two questions in one.
AMBIGUOUS = re.compile(
    # "broken out" with or without a "by": "monthly revenue with discounts
    # broken out" is two measures on one chart, which is a decision about
    # whether they even share a scale - not a decision to make from a cue.
    r"\b(also|as well as|broken (?:down|out)|both|then|compared to)\b"
)


def enabled() -> bool:
    """Off by one environment variable, because it is a quality trade."""
    return config.get_bool("TWOHELIXES_FAST_ROUTE", default=True)


def _normalise(question: str) -> str:
    return f" {re.sub(r'[^a-z0-9 ]+', ' ', question.lower())} ".replace("  ", " ")


# Whole phrases, not substrings. "by months since signup" contains "by month",
# and the substring version read that as a monthly time series of a column that
# is an axis, not a measure - a wrong chart produced confidently, which is the
# exact failure this module is supposed to make impossible.
_CUE_PATTERNS: dict[str, list[tuple[str, Any]]] = {
    family: [(cue, re.compile(rf"\b{re.escape(cue)}\b")) for cue in cues]
    for family, cues in FAMILIES.items()
}


def _match_family(question: str) -> tuple[str, str] | None:
    """The one family this question names, or nothing.

    Two families is two questions; zero is a question that does not name its
    own chart. Both go to the model.
    """
    text = _normalise(question)
    if AMBIGUOUS.search(text):
        return None

    hits: list[tuple[str, str]] = []
    for family, patterns in _CUE_PATTERNS.items():
        for cue, pattern in patterns:
            if pattern.search(text):
                hits.append((family, cue))
                break

    # "over time" also contains no ranking cue, but "which region grew most
    # over time" contains both - and that is a line chart or a bar chart
    # depending on what the asker meant. Refuse it.
    return hits[0] if len(hits) == 1 else None


def classify(question: str, frame: Any) -> Routed | None:
    """A chart configuration this question and this data agree on, or None."""
    if not enabled() or not question or frame is None:
        return None

    matched = _match_family(question)
    if matched is None:
        return None
    family, cue = matched

    try:
        time_column = tools.find_time_column(frame)
        measures = tools.find_measures(frame)
        categories = tools.find_categories(frame)
    except Exception:  # noqa: BLE001 - a profiling failure is the model's problem
        log.debug("could not profile the frame for routing", exc_info=True)
        return None

    builder = {
        "time": _time,
        "share": _share,
        "correlation": _correlation,
        "distribution": _distribution,
        "ranking": _ranking,
    }[family]

    built = builder(question, frame, time_column, measures, categories)
    if built is None:
        return None
    return Routed(config=built, family=family, cue=cue)


# --------------------------------------------------------------------------
# Families
#
# Each returns a configuration only when the data can carry the question, and
# each names the measure the question mentions when it mentions one - a
# question about revenue must not be answered with a chart of units.
# --------------------------------------------------------------------------


def _mentioned(question: str, columns: list[str]) -> str | None:
    """The column this question names, if it names exactly one."""
    text = _normalise(question)
    found = [
        column
        for column in columns
        if f" {str(column).lower().replace('_', ' ')} " in text
        or f" {str(column).lower()} " in text
    ]
    return found[0] if len(found) == 1 else None


def _measure(question: str, measures: list[str]) -> str | None:
    """The measure this question is about.

    Named literally, or the only one there is, or - failing both - the one a
    static embedding is confident about. That last step is what lets "does
    customer happiness drop when tickets take longer" find `satisfaction`: the
    word is not in the question, and before it the gate abstained and paid for
    a model call to be told the same thing.
    """
    if not measures:
        return None
    literal = _mentioned(question, measures)
    if literal:
        return literal
    if len(measures) == 1:
        return measures[0]
    guess = semantic.resolve(question, measures)
    return guess[0] if guess else None


def _category(question: str, categories: list[str]) -> str | None:
    if not categories:
        return None
    literal = _mentioned(question, categories)
    if literal:
        return literal
    if len(categories) == 1:
        return categories[0]
    guess = semantic.resolve(question, categories)
    return guess[0] if guess else None


# Summing a length is nonsense and averaging revenue answers a different
# question, so the aggregate is part of the decision, not a default. These are
# the words that make a measure additive; anything else averages.
ADDITIVE = (
    "revenue", "amount", "sales", "cost", "spend", "profit", "margin",
    "units", "quantity", "qty", "count", "total", "sessions", "clicks",
    "views", "impressions", "conversions", "downloads", "orders", "tickets",
    "mrr", "arr", "kwh", "volume", "visits", "signups", "accounts",
)

_MEAN_CUES = re.compile(r"\b(average|avg|mean|typical|per (order|user|customer|session|visit|account|day|head))\b")
_MEDIAN_CUES = re.compile(r"\bmedian\b")
_SUM_CUES = re.compile(r"\b(total|sum|altogether|combined|how much)\b")
_COUNT_CUES = re.compile(
    r"\b(how many|count|counts|number of|volume of|generate the most|"
    r"produce the most|come from)\b"
)


def _aggregate(question: str, measure: str) -> str:
    """How to combine the measure: asked for, or implied by what it is."""
    text = _normalise(question)
    if _MEDIAN_CUES.search(text):
        return "median"
    if _MEAN_CUES.search(text):
        return "mean"
    if _SUM_CUES.search(text):
        return "sum"
    name = str(measure).lower()
    return "sum" if any(word in name for word in ADDITIVE) else "mean"


def _time(
    question: str, frame: Any, time_column: str | None, measures: list, categories: list
) -> dict[str, Any] | None:
    if not time_column:
        return None
    measure = _measure(question, measures)
    if measure is None:
        return None
    colour = _mentioned(question, categories)
    return {
        "chart_type": "line",
        "x": time_column,
        "y": measure,
        "color": colour,
        "agg": _aggregate(question, measure),
        "title": f"{tools.humanise(measure)} over time",
        "x_title": tools.humanise(time_column),
        "y_title": tools.humanise(measure),
    }


def _share(
    question: str, frame: Any, time_column: str | None, measures: list, categories: list
) -> dict[str, Any] | None:
    category = _category(question, categories)
    measure = _measure(question, measures)
    if category is None or measure is None:
        return None
    # A pie is only honest with few slices, and `validate_config` will turn a
    # crowded one into bars anyway - but checking here keeps the title right.
    try:
        slices = int(frame[category].nunique())
    except Exception:  # noqa: BLE001
        return None
    if slices < 3 or slices > 6:
        return None
    return {
        "chart_type": "pie",
        "x": category,
        "y": measure,
        "agg": "sum",
        "title": f"{tools.humanise(measure)} by {tools.humanise(category)}",
    }


def _correlation(
    question: str, frame: Any, time_column: str | None, measures: list, categories: list
) -> dict[str, Any] | None:
    named = [
        column
        for column in measures
        if f" {str(column).lower().replace('_', ' ')} " in _normalise(question)
    ]
    pair = named[:2] if len(named) >= 2 else (measures[:2] if len(measures) == 2 else [])
    if len(pair) != 2:
        return None
    return {
        "chart_type": "scatter",
        "x": pair[0],
        "y": pair[1],
        "color": categories[0] if len(categories) == 1 else None,
        "agg": None,
        "title": f"{tools.humanise(pair[1])} against {tools.humanise(pair[0])}",
        "x_title": tools.humanise(pair[0]),
        "y_title": tools.humanise(pair[1]),
    }


def _distribution(
    question: str, frame: Any, time_column: str | None, measures: list, categories: list
) -> dict[str, Any] | None:
    measure = _measure(question, measures)
    if measure is None:
        return None
    return {
        "chart_type": "histogram",
        "x": measure,
        "y": None,
        "agg": None,
        "title": f"Distribution of {tools.humanise(measure)}",
        "x_title": tools.humanise(measure),
    }


def _ranking(
    question: str, frame: Any, time_column: str | None, measures: list, categories: list
) -> dict[str, Any] | None:
    category = _category(question, categories)
    if category is None:
        return None
    measure = _measure(question, measures)
    if measure is None:
        # No measure named and more than one to choose from. Counting rows is
        # only the answer when the question actually asks about volume - "which
        # measurements differ most between cultivars" is not a question about
        # how many wines there are, and answering it with one was the gate at
        # its most confidently wrong.
        if not _COUNT_CUES.search(_normalise(question)):
            return None
        return {
            "chart_type": "bar",
            "x": category,
            "y": None,
            "agg": "count",
            "title": f"Count by {tools.humanise(category)}",
            "x_title": tools.humanise(category),
        }
    return {
        "chart_type": "bar",
        "x": category,
        "y": measure,
        "agg": _aggregate(question, measure),
        "title": f"{tools.humanise(measure)} by {tools.humanise(category)}",
        "x_title": tools.humanise(category),
        "y_title": tools.humanise(measure),
    }
