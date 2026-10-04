"""Explicit, chronological window calculations for transaction and period data."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

OPERATIONS = {"cumulative", "difference", "percent_change", "rolling_sum", "rolling_mean"}


def window(
    frame: pd.DataFrame,
    time_column: str,
    column: str,
    *,
    operation: str = "cumulative",
    by: list[str] | None = None,
    output: str = "value",
    size: int | str = 3,
    periods: int = 1,
    opening_balance: float = 0,
) -> pd.DataFrame:
    """Return a sorted copy; windows never cross dimension boundaries.

    Integer sizes count observations; '7D' counts elapsed time, (t-7D, t].
    Percent changes are percentages, not fractions. Missing/zero baselines
    yield null, never infinity. Missing values are not silently zero-filled.
    """
    groups = list(by or [])
    if operation not in OPERATIONS:
        raise ValueError(f"unsupported window operation: {operation}")
    if not output or output in {time_column, *groups}:
        raise ValueError("output must not overwrite the time or grouping columns")
    if isinstance(periods, bool) or not isinstance(periods, int) or periods <= 0:
        raise ValueError("periods must be a positive integer")
    if not np.isfinite(float(opening_balance)):
        raise ValueError("opening_balance must be finite")
    if operation.startswith("rolling"):
        if isinstance(size, str):
            if pd.Timedelta(size) <= pd.Timedelta(0):
                raise ValueError("time window must be positive")
        elif isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError("window size must be a positive integer or duration")
    out = frame.copy()
    out[time_column] = pd.to_datetime(out[time_column], errors="raise", utc=True)
    if out[time_column].isna().any():
        raise ValueError("window calculations require non-null timestamps")
    values = pd.to_numeric(out[column], errors="raise")
    if np.isinf(values.to_numpy(dtype=float, na_value=np.nan)).any():
        raise ValueError("window calculations require finite values")
    out[column] = values
    out = out.sort_values(time_column, kind="stable").reset_index(drop=True)

    def calculate(series: pd.Series) -> pd.Series:
        if operation == "cumulative":
            return series.cumsum() + float(opening_balance)
        if operation == "difference":
            return series.diff(periods)
        if operation == "percent_change":
            previous = series.shift(periods)
            return (series - previous).div(previous.where(previous != 0)) * 100
        working = series
        if isinstance(size, str):
            working = pd.Series(series.to_numpy(), index=pd.DatetimeIndex(out.loc[series.index, time_column]))
        rolled = working.rolling(size, min_periods=1)
        result = rolled.sum() if operation == "rolling_sum" else rolled.mean()
        return pd.Series(result.to_numpy(), index=series.index)

    if groups:
        out[output] = out.groupby(groups, dropna=False, observed=True)[column].transform(calculate)
    else:
        out[output] = calculate(out[column])
    return out
