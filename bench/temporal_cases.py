"""Shared numeric oracles. Keep this file identical in both agents."""
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    data: list[dict]
    code: str
    expected: dict[str, list]
    group: str | None = None
    chart_type: str = "line"
    x: str = "date"

    @property
    def config(self):
        return {"chart_type": self.chart_type, "x": self.x, "y": "value",
                "color": self.group, "title": self.question}


def cases():
    bank = [
        {"date": "2024-01-05", "account": "A", "amount": -25},
        {"date": "2024-01-01", "account": "A", "amount": 100},
        {"date": "2024-01-02", "account": "A", "amount": -40},
        {"date": "2024-01-01", "account": "B", "amount": 200},
        {"date": "2024-01-08", "account": "B", "amount": -50},
    ]
    series = [{"date": f"2024-0{i+1}-01", "amount": n} for i, n in enumerate([0, 10, None, 20, 40])]
    result = [
        Case("bank_balance", "Show our money over time per account from signed transactions, opening balance 1000 per account.", bank,
             "result = window(df, 'date', 'amount', by=['account'], opening_balance=1000)",
             {"A": [1100, 1060, 1035], "B": [1200, 1150]}, "account"),
        Case("balance_display_window", "Show account balances from January 5 onward, retaining earlier transaction history; opening balance 1000 per account.", bank,
             "result = window(df, 'date', 'amount', by=['account'], opening_balance=1000)\nresult = result[result['date'] >= pd.Timestamp('2024-01-05', tz='UTC')]",
             {"A": [1035], "B": [1150]}, "account"),
        Case("rolling_observations", "Show the rolling sum of the last 2 transactions per account, ordered by date.", bank,
             "result = window(df, 'date', 'amount', by=['account'], operation='rolling_sum', size=2)",
             {"A": [100, 60, -65], "B": [200, 150]}, "account"),
        Case("rolling_elapsed_days", "Show a rolling 3-day transaction sum per account, excluding exactly t minus 3 days.", bank,
             "result = window(df, 'date', 'amount', by=['account'], operation='rolling_sum', size='3D')",
             {"A": [100, 60, -25], "B": [200, -50]}, "account"),
        Case("rolling_mean", "Show the mean of the last 2 transactions per account over time.", bank,
             "result = window(df, 'date', 'amount', by=['account'], operation='rolling_mean', size=2)",
             {"A": [100, 30, -32.5], "B": [200, 75]}, "account"),
        Case("zero_missing_baseline", "Show month-on-month percentage change in amount; zero or missing baselines must be null, not infinity.", series,
             "result = window(df, 'date', 'amount', operation='percent_change')",
             {"": [None, None, None, None, 100]}),
    ]
    quarters = [
        {"date": date, "region": region, "amount": amount}
        for region, values in [("North", [100, 150, 80, 120, 200, 250]), ("South", [50, 25, 100, 80, 75, 150])]
        for date, amount in zip(["2024-01-01", "2024-04-01", "2024-07-01", "2024-10-01", "2025-01-01", "2025-04-01"], values)
    ][::-1]
    result.extend([
        Case("quarter_absolute_change", "Show quarter-on-quarter absolute amount changes by region for 2024 and 2025.", quarters,
             "result = window(df, 'date', 'amount', by=['region'], operation='difference')",
             {"North": [None, 50, -70, 40, 80, 50], "South": [None, -25, 75, -20, -5, 75]}, "region"),
        Case("quarter_percent_change", "Show quarter-on-quarter percentage amount changes per region across 2024 and 2025.", quarters,
             "result = window(df, 'date', 'amount', by=['region'], operation='percent_change')",
             {"North": [None, 50, -46.6666666667, 50, 66.6666666667, 25], "South": [None, -50, 300, -20, -6.25, 100]}, "region"),
        Case("q1_year_comparison", "Compare Q1 2025 amount with Q1 2024 by region as absolute change; exclude other quarters.", quarters,
             "df['date'] = pd.to_datetime(df['date'], utc=True)\nresult = df[df['date'].dt.quarter == 1]\nresult = window(result, 'date', 'amount', by=['region'], operation='difference')",
             {"North": [None, 100], "South": [None, 25]}, "region"),
        Case("vs_q1_same_year", "Show the absolute difference from Q1 of the SAME year for every quarter per region, resetting the baseline in 2025.", quarters,
             "result = df.copy()\nresult['date'] = pd.to_datetime(result['date'], utc=True)\nresult = result.sort_values('date', kind='stable')\nresult['year'] = result['date'].dt.year\nbaseline = result.groupby(['region', 'year'])['amount'].transform('first')\nresult['value'] = result['amount'] - baseline",
             {"North": [0, 50, -20, 20, 0, 50], "South": [0, -25, 50, 30, 0, 75]}, "region"),
    ])
    sparse = [{"date": "2024-01-01", "amount": 100}, {"date": "2024-07-01", "amount": 150}, {"date": "2024-10-01", "amount": 200}]
    result.append(Case("missing_quarter", "Show quarter-on-quarter amount differences for 2024 including missing Q2 as null; Q3 must not compare to Q1.", sparse,
        "df['date'] = pd.to_datetime(df['date'], utc=True)\nresult = df.set_index('date').reindex(pd.date_range('2024-01-01', '2024-10-01', freq='QS', tz='UTC')).rename_axis('date').reset_index()\nresult = window(result, 'date', 'amount', operation='difference')",
        {"": [None, None, None, 50]}))
    snapshots = [{"date": date, "amount": n} for date, n in [("2024-01-03", 1050), ("2024-01-01", 1000), ("2024-01-02", 900)]]
    result.append(Case("balance_snapshots", "Plot the existing balance snapshots over time. Do not accumulate them again.", snapshots,
        "result = df.sort_values('date', kind='stable').copy()\nresult['value'] = result['amount']",
        {"": [1000, 900, 1050]}))
    return result


def assert_frame(case, frame):
    for group, expected in case.expected.items():
        values = frame.loc[frame[case.group] == group, 'value'] if case.group else frame['value']
        np.testing.assert_allclose(values, [np.nan if n is None else n for n in expected], equal_nan=True, rtol=1e-8)
    assert len(frame) == sum(map(len, case.expected.values()))
