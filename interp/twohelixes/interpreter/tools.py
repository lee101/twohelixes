"""Tools callable from inside the code interpreter.

These exist because the agent kept re-deriving the same twenty lines of pandas
for every query: infer which column is a date, coerce a currency string to a
float, pick a sensible aggregation, find the join key between two frames. Each
one is deterministic Python, so moving it out of the model's output removes a
whole class of generated-code bugs.

Everything here is exposed by name in the interpreter namespace.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Iterable, Sequence

log = logging.getLogger("twohelixes.tools")

CURRENCY = re.compile(r"[^\d.\-eE]")
DATE_HINTS = (
    "date",
    "time",
    "day",
    "month",
    "year",
    "week",
    "quarter",
    "timestamp",
    "created",
    "updated",
    "period",
    "dt",
)
MEASURE_HINTS = (
    "amount",
    "total",
    "sum",
    "count",
    "qty",
    "quantity",
    "revenue",
    "sales",
    "price",
    "cost",
    "value",
    "score",
    "rate",
    "profit",
    "spend",
    "volume",
    "balance",
)
ID_HINTS = ("id", "uuid", "guid", "key", "code", "sku", "hash")


# --------------------------------------------------------------------------
# Profiling
# --------------------------------------------------------------------------


def profile(df: Any, max_uniques: int = 12) -> dict[str, Any]:
    """A compact description of a frame, small enough to put in a prompt."""
    import pandas as pd

    columns: list[dict[str, Any]] = []
    for name in df.columns:
        series = df[name]
        info: dict[str, Any] = {
            "name": str(name),
            "dtype": str(series.dtype),
            "nulls": int(series.isna().sum()),
            "unique": int(series.nunique(dropna=True)),
            "role": column_role(df, name),
        }
        non_null = series.dropna()
        if len(non_null):
            if pd.api.types.is_numeric_dtype(series):
                info["min"] = _finite(non_null.min())
                info["max"] = _finite(non_null.max())
                info["mean"] = _finite(non_null.mean())
                info["median"] = _finite(non_null.median())
            elif pd.api.types.is_datetime64_any_dtype(series):
                info["min"] = str(non_null.min())
                info["max"] = str(non_null.max())
            else:
                if info["unique"] <= max_uniques:
                    info["values"] = [str(v) for v in non_null.unique()[:max_uniques]]
                else:
                    info["sample"] = [str(v) for v in non_null.head(3)]
        columns.append(info)

    return {
        "rows": int(len(df)),
        "columns": columns,
        "memory_mb": round(df.memory_usage(deep=True).sum() / 1e6, 2),
    }


def _finite(value: Any) -> Any:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(numeric) or math.isinf(numeric):
        return None
    return round(numeric, 6)


def column_role(df: Any, column: str) -> str:
    """Classify a column as time / measure / category / identifier.

    Chart selection downstream depends on this far more than on dtype alone:
    an integer `order_id` is not a measure, and a string `2024-01-03` is time.
    """
    import pandas as pd

    series = df[column]
    lowered = str(column).lower()

    if pd.api.types.is_datetime64_any_dtype(series):
        return "time"
    if any(hint in lowered for hint in DATE_HINTS) and looks_like_dates(series):
        return "time"

    if pd.api.types.is_bool_dtype(series):
        return "category"

    if pd.api.types.is_numeric_dtype(series):
        # Only a name hint demotes a numeric column to an identifier. High
        # cardinality alone is not evidence: a measurement column is usually
        # near-unique too, and misclassifying one drops it from charting
        # entirely - a worse failure than charting an id by mistake.
        if any(
            lowered == hint or lowered.endswith("_" + hint) or lowered.startswith(hint + "_")
            for hint in ID_HINTS
        ):
            return "identifier"
        return "measure"

    if any(hint in lowered for hint in ID_HINTS):
        return "identifier"
    if len(series) and series.nunique(dropna=True) / max(1, len(series)) > 0.7:
        return "identifier" if series.nunique() > 200 else "category"
    return "category"


def looks_like_dates(series: Any, sample: int = 40) -> bool:
    import pandas as pd

    values = series.dropna().head(sample)
    if values.empty:
        return False
    try:
        parsed = pd.to_datetime(values, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        try:
            parsed = pd.to_datetime(values, errors="coerce")
        except Exception:  # noqa: BLE001
            return False
    return float(parsed.notna().mean()) > 0.8


def find_time_column(df: Any) -> str | None:
    for name in df.columns:
        if column_role(df, name) == "time":
            return str(name)
    return None


def find_measures(df: Any) -> list[str]:
    return [str(c) for c in df.columns if column_role(df, c) == "measure"]


def find_categories(df: Any, max_cardinality: int = 50) -> list[str]:
    out = []
    for name in df.columns:
        if column_role(df, name) == "category":
            if df[name].nunique(dropna=True) <= max_cardinality:
                out.append(str(name))
    return out


# --------------------------------------------------------------------------
# Cleaning and coercion
# --------------------------------------------------------------------------


def to_number(series: Any) -> Any:
    """Coerce '$1,234.50', '45%', '(120)' and friends to floats."""
    import pandas as pd

    if pd.api.types.is_numeric_dtype(series):
        return series

    text = series.astype(str).str.strip()
    negative = text.str.match(r"^\(.*\)$")
    percent = text.str.endswith("%")
    cleaned = text.str.replace(CURRENCY, "", regex=True)
    numbers = pd.to_numeric(cleaned, errors="coerce")
    numbers = numbers.where(~negative, -numbers.abs())
    numbers = numbers.where(~percent, numbers / 100.0)
    return numbers


def to_datetime(series: Any) -> Any:
    import pandas as pd

    try:
        return pd.to_datetime(series, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return pd.to_datetime(series, errors="coerce")


def clean_frame(df: Any, aggressive: bool = False) -> Any:
    """Normalise column names, coerce obvious types, drop empty columns."""
    import pandas as pd

    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]

    # Drop columns that carry no information at all.
    empty = [c for c in out.columns if out[c].isna().all()]
    if empty:
        out = out.drop(columns=empty)

    for name in list(out.columns):
        series = out[name]
        if pd.api.types.is_object_dtype(series):
            if looks_like_dates(series):
                out[name] = to_datetime(series)
                continue
            coerced = to_number(series)
            # Only accept the numeric reading if it explains most values.
            if coerced.notna().mean() > 0.9 and series.notna().mean() > 0:
                out[name] = coerced

    if aggressive:
        out = out.drop_duplicates()
    return out


def drop_constant_columns(df: Any) -> Any:
    keep = [c for c in df.columns if df[c].nunique(dropna=False) > 1]
    return df[keep]


# --------------------------------------------------------------------------
# Joining
# --------------------------------------------------------------------------


def suggest_join_keys(left: Any, right: Any, min_overlap: float = 0.3) -> list[dict[str, Any]]:
    """Rank candidate join keys by value overlap, not just matching names.

    Name equality is a weak signal (`id` is in every table); shared values are
    a strong one.
    """
    candidates: list[dict[str, Any]] = []
    for lcol in left.columns:
        lvals = set(left[lcol].dropna().astype(str).head(20000))
        if not lvals:
            continue
        for rcol in right.columns:
            rvals = set(right[rcol].dropna().astype(str).head(20000))
            if not rvals:
                continue
            shared = lvals & rvals
            if not shared:
                continue
            overlap = len(shared) / min(len(lvals), len(rvals))
            if overlap < min_overlap:
                continue
            candidates.append(
                {
                    "left": str(lcol),
                    "right": str(rcol),
                    "overlap": round(overlap, 3),
                    "shared_values": len(shared),
                    "name_match": str(lcol).lower() == str(rcol).lower(),
                }
            )

    candidates.sort(key=lambda c: (c["name_match"], c["overlap"]), reverse=True)
    return candidates[:10]


def smart_join(left: Any, right: Any, how: str = "left") -> tuple[Any, dict[str, Any]]:
    """Join two frames on the best-scoring key, reporting what it chose."""
    import pandas as pd

    keys = suggest_join_keys(left, right)
    if not keys:
        return (
            pd.concat([left.reset_index(drop=True), right.reset_index(drop=True)], axis=1),
            {"strategy": "concat", "reason": "no shared key values"},
        )

    best = keys[0]
    merged = left.merge(
        right,
        left_on=best["left"],
        right_on=best["right"],
        how=how,
        suffixes=("", "_right"),
    )
    return merged, {
        "strategy": "merge",
        "left_key": best["left"],
        "right_key": best["right"],
        "how": how,
        "overlap": best["overlap"],
        "rows_before": int(len(left)),
        "rows_after": int(len(merged)),
        "alternatives": keys[1:4],
    }


# --------------------------------------------------------------------------
# Aggregation and time
# --------------------------------------------------------------------------


def pick_frequency(series: Any, target_points: int = 60) -> str:
    """Choose a resample frequency that lands near `target_points` buckets."""
    import pandas as pd

    values = pd.to_datetime(series, errors="coerce").dropna().sort_values()
    if len(values) < 2:
        return "D"
    span_days = (values.max() - values.min()).total_seconds() / 86400.0
    if span_days <= 0:
        return "h"

    ladder = (
        (0.0007, "min"),
        (0.042, "h"),
        (1.0, "D"),
        (7.0, "W"),
        (31.0, "ME"),
        (92.0, "QE"),
        (366.0, "YE"),
    )

    # Never resample finer than the data's own spacing: doing so manufactures
    # empty buckets and turns a clean daily line into a gap-ridden one.
    native_days = float(values.diff().dropna().median().total_seconds()) / 86400.0
    wanted_days = span_days / max(1, target_points)
    per_bucket = max(wanted_days, native_days)

    for threshold, freq in ladder:
        if per_bucket <= threshold:
            return freq
    return "YE"


def timeseries(
    df: Any, time_column: str, value_column: str, agg: str = "sum", freq: str | None = None
) -> Any:
    """Resample to a regular grid so line charts do not lie about gaps."""
    import pandas as pd

    frame = df[[time_column, value_column]].copy()
    frame[time_column] = to_datetime(frame[time_column])
    frame = frame.dropna(subset=[time_column])
    if frame.empty:
        return frame

    freq = freq or pick_frequency(frame[time_column])
    grouped = (
        frame.set_index(time_column)[value_column].resample(freq).agg(agg).reset_index()
    )
    return grouped


def top_n(df: Any, category: str, measure: str, n: int = 12, agg: str = "sum") -> Any:
    """Top N by measure, with the remainder folded into an 'Other' row.

    Dropping the tail silently is the single most common way a generated bar
    chart misrepresents the data.
    """
    import pandas as pd

    grouped = df.groupby(category, dropna=False)[measure].agg(agg).sort_values(ascending=False)
    if len(grouped) <= n:
        return grouped.reset_index()

    head = grouped.head(n)
    other = grouped.iloc[n:].sum()
    combined = pd.concat([head, pd.Series({"Other": other})])
    return combined.reset_index().rename(columns={"index": category, 0: measure})


def summarise(df: Any, by: str | Sequence[str], measure: str, agg: str = "sum") -> Any:
    keys = [by] if isinstance(by, str) else list(by)
    return df.groupby(keys, dropna=False)[measure].agg(agg).reset_index()


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


def outliers(series: Any, method: str = "iqr", factor: float = 1.5) -> Any:
    """Boolean mask of outliers. Used to annotate, not to silently drop."""
    import numpy as np
    import pandas as pd

    values = pd.to_numeric(series, errors="coerce")
    if method == "zscore":
        std = values.std()
        if not std or math.isnan(std):
            return pd.Series([False] * len(values), index=values.index)
        return (values - values.mean()).abs() / std > factor + 1.5

    q1, q3 = values.quantile(0.25), values.quantile(0.75)
    iqr = q3 - q1
    if iqr == 0 or math.isnan(iqr):
        return pd.Series([False] * len(values), index=values.index)
    return (values < q1 - factor * iqr) | (values > q3 + factor * iqr)


def correlations(df: Any, threshold: float = 0.5) -> list[dict[str, Any]]:
    """Numeric pairs worth charting, strongest first."""
    numeric = df.select_dtypes("number")
    if numeric.shape[1] < 2:
        return []
    matrix = numeric.corr(numeric_only=True)
    pairs: list[dict[str, Any]] = []
    seen: set[frozenset[str]] = set()
    for a in matrix.columns:
        for b in matrix.columns:
            if a == b:
                continue
            key = frozenset((str(a), str(b)))
            if key in seen:
                continue
            seen.add(key)
            value = matrix.loc[a, b]
            if value is None or math.isnan(value):
                continue
            if abs(value) >= threshold:
                pairs.append({"x": str(a), "y": str(b), "r": round(float(value), 3)})
    pairs.sort(key=lambda p: abs(p["r"]), reverse=True)
    return pairs


def trend(series: Any) -> dict[str, Any]:
    """Least-squares slope plus a plain-language direction."""
    import numpy as np
    import pandas as pd

    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) < 3:
        return {"direction": "flat", "slope": 0.0, "confidence": 0.0}

    x = np.arange(len(values), dtype=float)
    y = values.to_numpy(dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(((y - predicted) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 0.0

    spread = float(y.std()) or 1.0
    if abs(slope) * len(values) < spread * 0.25:
        direction = "flat"
    else:
        direction = "rising" if slope > 0 else "falling"

    return {
        "direction": direction,
        "slope": round(float(slope), 6),
        "confidence": round(max(0.0, min(1.0, r2)), 3),
        "change_pct": round(float((y[-1] - y[0]) / y[0] * 100), 2) if y[0] else None,
    }


def forecast(series: Any, periods: int = 12) -> Any:
    """Holt-Winters when statsmodels is available, linear extrapolation if not."""
    import numpy as np
    import pandas as pd

    values = pd.to_numeric(series, errors="coerce").dropna()
    if len(values) < 6:
        return pd.Series(dtype=float)

    try:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        model = ExponentialSmoothing(values, trend="add", seasonal=None).fit()
        return model.forecast(periods)
    except Exception as exc:  # noqa: BLE001
        log.debug("holt-winters unavailable (%s); using linear fit", exc)
        x = np.arange(len(values), dtype=float)
        slope, intercept = np.polyfit(x, values.to_numpy(dtype=float), 1)
        future = np.arange(len(values), len(values) + periods, dtype=float)
        return pd.Series(slope * future + intercept)


def describe_distribution(series: Any) -> dict[str, Any]:
    import pandas as pd

    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return {}
    skew = float(values.skew()) if len(values) > 2 else 0.0
    return {
        "count": int(len(values)),
        "mean": _finite(values.mean()),
        "median": _finite(values.median()),
        "std": _finite(values.std()),
        "p05": _finite(values.quantile(0.05)),
        "p95": _finite(values.quantile(0.95)),
        "skew": round(skew, 3),
        "shape": "right-skewed" if skew > 1 else "left-skewed" if skew < -1 else "symmetric",
        "suggest_log_scale": bool(skew > 2 and values.min() > 0),
    }


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------


def normalise_labels(series: Any) -> Any:
    """Title-case and trim category labels so legends read cleanly."""
    return (
        series.astype(str)
        .str.strip()
        .str.replace(r"[_\-]+", " ", regex=True)
        .str.replace(r"\s+", " ", regex=True)
        .str.title()
    )


def shorten(text: Any, limit: int = 28) -> str:
    value = str(text)
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def humanise(name: str) -> str:
    """`total_revenue_usd` -> `Total Revenue USD`."""
    words = re.split(r"[_\s\-]+", str(name).strip())
    out = []
    for word in words:
        if not word:
            continue
        if word.isupper() and len(word) <= 4:
            out.append(word)
        else:
            out.append(word.capitalize())
    return " ".join(out) or str(name)


def format_number(value: Any, unit: str = "") -> str:
    """Compact axis/label formatting: 1.2M, 45.3k, 0.8%."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "%":
        return f"{number:.1f}%"
    magnitude = abs(number)
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "k")):
        if magnitude >= threshold:
            return f"{number / threshold:.1f}{suffix}{unit}"
    if magnitude < 1 and magnitude > 0:
        return f"{number:.3g}{unit}"
    return f"{number:,.0f}{unit}"


def namespace() -> dict[str, Any]:
    """Names injected into the interpreter."""
    return {
        "profile": profile,
        "column_role": column_role,
        "looks_like_dates": looks_like_dates,
        "find_time_column": find_time_column,
        "find_measures": find_measures,
        "find_categories": find_categories,
        "to_number": to_number,
        "coerce_datetime": to_datetime,
        "clean_frame": clean_frame,
        "drop_constant_columns": drop_constant_columns,
        "suggest_join_keys": suggest_join_keys,
        "smart_join": smart_join,
        "pick_frequency": pick_frequency,
        "timeseries": timeseries,
        "top_n": top_n,
        "summarise": summarise,
        "summarize": summarise,
        "outliers": outliers,
        "correlations": correlations,
        "trend": trend,
        "forecast": forecast,
        "describe_distribution": describe_distribution,
        "normalise_labels": normalise_labels,
        "normalize_labels": normalise_labels,
        "shorten": shorten,
        "snake": snake,
        "humanise": humanise,
        "humanize": humanise,
        "format_number": format_number,
    }
