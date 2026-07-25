"""Sample datasets every account gets on day one.

An analytics product with nothing to analyse is a dead end: a new user has no
warehouse connected and nothing to ask about. These give them something real
in the first thirty seconds.

Two kinds, deliberately:

* **Open reference data** (iris, wine, diabetes, breast cancer) - bundled with
  scikit-learn, so no network fetch and no licence question. These are what
  people benchmark against and recognise.
* **Business-shaped data** (orders, web traffic, subscriptions, support) -
  generated deterministically from a fixed seed, because the shapes that
  matter for this product (a time series with seasonality, a long-tailed
  category, a funnel, a cohort) are exactly what the reference sets lack.

Everything is materialised once to Parquet and registered as DuckDB views, so
the SQL editor and the chart pipeline both see them through the same
connector as a real warehouse.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from twohelixes import config

log = logging.getLogger("twohelixes.datasets")

SEED = 20260725


@dataclass
class Sample:
    key: str
    name: str
    description: str
    build: Callable[[], Any]
    questions: list[str] = field(default_factory=list)
    source: str = ""

    def to_dict(self, rows: int = 0, columns: list[str] | None = None) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "questions": self.questions,
            "source": self.source,
            "rows": rows,
            "columns": columns or [],
        }


# --------------------------------------------------------------------------
# Open reference data
# --------------------------------------------------------------------------


def _sklearn_frame(loader: str, target_name: str, target_labels: bool = True) -> Any:
    from sklearn import datasets as sk

    bundle = getattr(sk, loader)(as_frame=True)
    frame = bundle.frame.copy()

    # The raw frames name the label column "target" and encode it as an int,
    # which charts as a meaningless measure. Give it a real name and its
    # category labels back.
    if "target" in frame.columns:
        if target_labels and getattr(bundle, "target_names", None) is not None:
            names = list(bundle.target_names)
            frame[target_name] = [
                names[int(v)] if 0 <= int(v) < len(names) else str(v)
                for v in frame["target"]
            ]
        else:
            frame[target_name] = frame["target"]
        frame = frame.drop(columns=["target"])

    frame.columns = [
        str(c).strip().replace(" (cm)", "_cm").replace(" ", "_").lower()
        for c in frame.columns
    ]
    return frame


def _iris() -> Any:
    return _sklearn_frame("load_iris", "species")


def _wine() -> Any:
    return _sklearn_frame("load_wine", "cultivar")


def _breast_cancer() -> Any:
    return _sklearn_frame("load_breast_cancer", "diagnosis")


def _diabetes() -> Any:
    frame = _sklearn_frame("load_diabetes", "progression", target_labels=False)
    return frame


# --------------------------------------------------------------------------
# Business-shaped data
# --------------------------------------------------------------------------


def _rng():
    import numpy as np

    return np.random.default_rng(SEED)


def _orders() -> Any:
    """Two years of orders: seasonality, a long-tailed product mix, refunds."""
    import numpy as np
    import pandas as pd

    rng = _rng()
    days = pd.date_range("2024-01-01", "2025-12-31", freq="D")

    regions = ["North America", "Europe", "APAC", "LatAm", "MEA"]
    region_weight = np.array([0.38, 0.29, 0.19, 0.09, 0.05])
    channels = ["Organic", "Paid search", "Email", "Referral", "Social", "Partner"]
    channel_weight = np.array([0.34, 0.22, 0.16, 0.12, 0.09, 0.07])
    categories = ["Software", "Hardware", "Services", "Training", "Support"]

    rows: list[dict[str, Any]] = []
    order_id = 100000

    for day in days:
        # Weekly rhythm plus a slow annual swell, so time charts have something
        # to actually show.
        weekday = 0.72 if day.dayofweek >= 5 else 1.0
        season = 1.0 + 0.28 * math.sin((day.dayofyear / 365.0) * 2 * math.pi - 1.2)
        growth = 1.0 + 0.45 * ((day - days[0]).days / max(1, len(days)))
        count = max(1, int(rng.poisson(14 * weekday * season * growth)))

        for _ in range(count):
            order_id += 1
            category = str(rng.choice(categories))
            base = {
                "Software": 420.0, "Hardware": 1180.0, "Services": 2600.0,
                "Training": 780.0, "Support": 240.0,
            }[category]
            # Log-normal: a few large orders dominate revenue, as in real data.
            amount = float(np.round(base * rng.lognormal(0.0, 0.55), 2))
            rows.append({
                "order_id": order_id,
                "order_date": day,
                "region": str(rng.choice(regions, p=region_weight)),
                "channel": str(rng.choice(channels, p=channel_weight)),
                "category": category,
                "units": int(rng.integers(1, 9)),
                "amount": amount,
                "discount": float(np.round(amount * rng.choice([0, 0, 0, 0.05, 0.1, 0.2]), 2)),
                "refunded": bool(rng.random() < 0.031),
            })

    frame = pd.DataFrame(rows)
    frame["net_amount"] = (frame["amount"] - frame["discount"]).round(2)
    return frame


def _web_traffic() -> Any:
    """Daily sessions per source, with a step change and one bad-deploy dip."""
    import numpy as np
    import pandas as pd

    rng = _rng()
    days = pd.date_range("2025-01-01", "2025-12-31", freq="D")
    sources = ["Organic", "Direct", "Paid", "Social", "Referral"]
    base = {"Organic": 5200, "Direct": 2400, "Paid": 1800, "Social": 900, "Referral": 620}

    rows: list[dict[str, Any]] = []
    for day in days:
        weekday = 0.68 if day.dayofweek >= 5 else 1.0
        for source in sources:
            level = base[source] * weekday
            # A content launch in June lifts organic for good.
            if source == "Organic" and day >= pd.Timestamp("2025-06-15"):
                level *= 1.42
            # A three-day outage in September, the kind an analyst has to find.
            if pd.Timestamp("2025-09-08") <= day <= pd.Timestamp("2025-09-10"):
                level *= 0.24
            sessions = max(0, int(rng.normal(level, level * 0.12)))
            rows.append({
                "day": day,
                "source": source,
                "sessions": sessions,
                "bounce_rate": round(float(np.clip(rng.normal(0.42, 0.07), 0.05, 0.95)), 4),
                "avg_seconds": round(float(max(5, rng.normal(168, 45))), 1),
                "conversions": int(max(0, rng.binomial(sessions, 0.021))),
            })
    return pd.DataFrame(rows)


def _subscriptions() -> Any:
    """Monthly cohorts with realistic decay - the shape retention questions need."""
    import numpy as np
    import pandas as pd

    rng = _rng()
    cohorts = pd.date_range("2024-01-01", "2025-06-01", freq="MS")
    plans = ["Free", "Starter", "Pro", "Team"]
    plan_price = {"Free": 0.0, "Starter": 19.0, "Pro": 49.0, "Team": 149.0}

    rows: list[dict[str, Any]] = []
    for cohort in cohorts:
        size = int(rng.integers(160, 420))
        for month in range(0, 19):
            period = cohort + pd.DateOffset(months=month)
            if period > pd.Timestamp("2025-12-01"):
                break
            # Steep early churn that flattens - the classic retention curve.
            retention = math.exp(-0.34 * month) * 0.55 + 0.45 * math.exp(-0.03 * month)
            active = int(size * min(1.0, retention))
            for plan in plans:
                share = {"Free": 0.46, "Starter": 0.27, "Pro": 0.19, "Team": 0.08}[plan]
                count = int(active * share)
                if count <= 0:
                    continue
                rows.append({
                    "cohort_month": cohort,
                    "activity_month": period,
                    "months_since_signup": month,
                    "plan": plan,
                    "active_accounts": count,
                    "mrr": round(count * plan_price[plan], 2),
                })
    return pd.DataFrame(rows)


def _support_tickets() -> Any:
    """Tickets with priorities, response times and a long tail of topics."""
    import numpy as np
    import pandas as pd

    rng = _rng()
    days = pd.date_range("2025-01-01", "2025-12-31", freq="D")
    topics = [
        "Billing", "Login", "Data import", "Charts", "Performance",
        "SQL editor", "Connectors", "Exports", "API", "Other",
    ]
    weights = np.array([0.21, 0.18, 0.14, 0.11, 0.09, 0.08, 0.07, 0.05, 0.04, 0.03])
    priorities = ["Low", "Normal", "High", "Urgent"]

    rows: list[dict[str, Any]] = []
    ticket = 5000
    for day in days:
        for _ in range(int(rng.poisson(11 if day.dayofweek < 5 else 4))):
            ticket += 1
            priority = str(rng.choice(priorities, p=[0.34, 0.42, 0.18, 0.06]))
            # Higher priority is answered faster, which is what makes this
            # dataset useful for a "does our SLA hold?" question.
            scale = {"Low": 14.0, "Normal": 6.5, "High": 2.4, "Urgent": 0.8}[priority]
            rows.append({
                "ticket_id": ticket,
                "opened_at": day,
                "topic": str(rng.choice(topics, p=weights)),
                "priority": priority,
                "first_response_hours": round(float(rng.exponential(scale)), 2),
                "resolution_hours": round(float(rng.exponential(scale * 4.2)), 2),
                "satisfaction": int(np.clip(rng.normal(4.1, 0.9), 1, 5)),
                "reopened": bool(rng.random() < 0.08),
            })
    return pd.DataFrame(rows)


def _energy() -> Any:
    """Hourly readings: the sub-daily case, where resampling matters."""
    import numpy as np
    import pandas as pd

    rng = _rng()
    hours = pd.date_range("2025-01-01", "2025-04-01", freq="h")
    rows: list[dict[str, Any]] = []
    for stamp in hours:
        # Two daily peaks and a seasonal drift.
        daily = 1.0 + 0.42 * math.sin((stamp.hour / 24.0) * 4 * math.pi - 1.6)
        seasonal = 1.0 + 0.22 * math.cos((stamp.dayofyear / 365.0) * 2 * math.pi)
        for site in ("Plant A", "Plant B", "Plant C"):
            base = {"Plant A": 820.0, "Plant B": 460.0, "Plant C": 1240.0}[site]
            rows.append({
                "reading_at": stamp,
                "site": site,
                "kwh": round(float(max(0, rng.normal(base * daily * seasonal, base * 0.08))), 2),
                "temperature_c": round(float(rng.normal(11 + 8 * math.sin(stamp.hour / 24 * 2 * math.pi), 3)), 1),
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Catalogue
# --------------------------------------------------------------------------

SAMPLES: list[Sample] = [
    Sample(
        key="orders",
        name="Sales orders",
        description="Two years of orders with regions, channels, discounts and refunds.",
        build=_orders,
        source="Generated",
        questions=[
            "how did net revenue trend by region over time?",
            "which channel brings the most revenue per order?",
            "what share of orders get refunded, by category?",
            "show monthly revenue with discounts broken out",
        ],
    ),
    Sample(
        key="web_traffic",
        name="Web traffic",
        description="Daily sessions, bounce rate and conversions per source for 2025.",
        build=_web_traffic,
        source="Generated",
        questions=[
            "which traffic source grew the most this year?",
            "was there a drop in sessions, and when?",
            "how does bounce rate compare across sources?",
            "what is the conversion rate by source over time?",
        ],
    ),
    Sample(
        key="subscriptions",
        name="Subscription cohorts",
        description="Monthly signup cohorts with retention and MRR by plan.",
        build=_subscriptions,
        source="Generated",
        questions=[
            "what does retention look like by months since signup?",
            "how does MRR split across plans?",
            "which cohort retained best?",
        ],
    ),
    Sample(
        key="support_tickets",
        name="Support tickets",
        description="A year of tickets with topics, priorities and response times.",
        build=_support_tickets,
        source="Generated",
        questions=[
            "what is the median first response time by priority?",
            "which topics generate the most tickets?",
            "does satisfaction fall when resolution takes longer?",
        ],
    ),
    Sample(
        key="energy",
        name="Energy readings",
        description="Hourly kWh and temperature for three sites over a quarter.",
        build=_energy,
        source="Generated",
        questions=[
            "show daily energy use per site",
            "what time of day peaks hardest?",
            "is consumption correlated with temperature?",
        ],
    ),
    Sample(
        key="iris",
        name="Iris",
        description="Fisher's iris measurements. 150 rows, 3 species.",
        build=_iris,
        source="scikit-learn / UCI",
        questions=[
            "how do petal length and width separate the species?",
            "compare sepal length across species",
        ],
    ),
    Sample(
        key="wine",
        name="Wine recognition",
        description="Chemical analysis of wines from three cultivars.",
        build=_wine,
        source="scikit-learn / UCI",
        questions=[
            "which measurements differ most between cultivars?",
            "show alcohol against colour intensity by cultivar",
        ],
    ),
    Sample(
        key="diabetes",
        name="Diabetes progression",
        description="Ten baseline variables against disease progression after a year.",
        build=_diabetes,
        source="scikit-learn",
        questions=[
            "which variable correlates most with progression?",
            "show BMI against progression",
        ],
    ),
    Sample(
        key="breast_cancer",
        name="Breast cancer diagnostic",
        description="Cell nucleus measurements with a malignant/benign diagnosis.",
        build=_breast_cancer,
        source="scikit-learn / UCI",
        questions=[
            "which measurements best separate malignant from benign?",
            "compare mean radius by diagnosis",
        ],
    ),
]

BY_KEY = {s.key: s for s in SAMPLES}


def storage_dir() -> Path:
    path = config.data_dir() / "samples"
    path.mkdir(parents=True, exist_ok=True)
    return path


def path_for(key: str) -> Path:
    return storage_dir() / f"{key}.parquet"


def materialise(force: bool = False) -> dict[str, dict[str, Any]]:
    """Build every sample to Parquet once. Returns a catalogue with real shapes."""
    catalogue: dict[str, dict[str, Any]] = {}

    for sample in SAMPLES:
        target = path_for(sample.key)
        try:
            if force or not target.exists():
                frame = sample.build()
                frame.to_parquet(target, index=False)
                log.info("built sample %s: %d rows", sample.key, len(frame))
            else:
                import pandas as pd

                frame = pd.read_parquet(target)
            catalogue[sample.key] = sample.to_dict(
                rows=int(len(frame)), columns=[str(c) for c in frame.columns]
            )
        except Exception as exc:  # noqa: BLE001 - one bad sample must not break boot
            log.exception("could not build sample %s", sample.key)
            catalogue[sample.key] = sample.to_dict()
            catalogue[sample.key]["error"] = str(exc)

    return catalogue


def catalogue() -> list[dict[str, Any]]:
    built = materialise()
    return [built[s.key] for s in SAMPLES if s.key in built]


def frame(key: str) -> Any:
    """Load one sample as a dataframe."""
    import pandas as pd

    if key not in BY_KEY:
        raise KeyError(f"unknown sample dataset '{key}'")
    target = path_for(key)
    if not target.exists():
        materialise()
    return pd.read_parquet(target)
