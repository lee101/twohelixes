import numpy as np
import pandas as pd
import pytest

from twohelixes.interpreter.temporal import window


@pytest.fixture
def transactions():
    return pd.DataFrame({
        "date": ["2024-01-05", "2024-01-01", "2024-01-02", "2024-01-01", "2024-01-08"],
        "account": ["A", "A", "A", "B", "B"],
        "amount": [-25, 100, -40, 200, -50],
    }, index=[3, 3, 1, 8, 0])


def test_balances_are_chronological_and_accounts_independent(transactions):
    before = transactions.copy(deep=True)
    result = window(transactions, "date", "amount", by=["account"], opening_balance=1000)
    assert result.loc[result.account == "A", "value"].tolist() == [1100, 1060, 1035]
    assert result.loc[result.account == "B", "value"].tolist() == [1200, 1150]
    pd.testing.assert_frame_equal(transactions, before)


def test_balance_history_precedes_display_filter(transactions):
    result = window(transactions, "date", "amount", by=["account"], opening_balance=1000)
    visible = result[result.date >= pd.Timestamp("2024-01-05", tz="UTC")]
    assert visible.value.tolist() == [1035, 1150]


@pytest.mark.parametrize("operation,expected", [
    ("difference", [None, -140, 15]),
    ("percent_change", [None, -140, -37.5]),
    ("rolling_sum", [100, 60, -65]),
    ("rolling_mean", [100, 30, -32.5]),
])
def test_window_families(transactions, operation, expected):
    result = window(transactions, "date", "amount", by=["account"], operation=operation, size=2)
    np.testing.assert_allclose(result.loc[result.account == "A", "value"],
                               [np.nan if x is None else x for x in expected], equal_nan=True)


def test_elapsed_window_is_not_observation_window(transactions):
    result = window(transactions, "date", "amount", by=["account"], operation="rolling_sum", size="3D")
    assert result.loc[result.account == "A", "value"].tolist() == [100, 60, -25]
    assert result.loc[result.account == "B", "value"].tolist() == [200, -50]


def test_missing_and_zero_baselines_are_not_filled():
    frame = pd.DataFrame({"date": pd.date_range("2024-01-01", periods=5), "n": [0, 10, None, 20, 40]})
    result = window(frame, "date", "n", operation="percent_change")
    assert result.value.iloc[:4].isna().all()
    assert result.value.iloc[4] == 100
    assert not np.isinf(result.value).any()


def test_missing_group_labels_are_kept():
    frame = pd.DataFrame({"date": ["2024-01-01", "2024-01-02"], "account": [None, None], "n": [1, 2]})
    assert window(frame, "date", "n", by=["account"]).value.tolist() == [1, 3]


@pytest.mark.parametrize("kwargs", [
    {"size": 0, "operation": "rolling_mean"},
    {"size": -1, "operation": "rolling_sum"},
    {"size": "0D", "operation": "rolling_sum"},
    {"periods": 0}, {"periods": True}, {"operation": "made_up"},
    {"output": "date"}, {"output": "account", "by": ["account"]},
    {"opening_balance": float("inf")},
])
def test_invalid_parameters_are_refused(transactions, kwargs):
    with pytest.raises(ValueError):
        window(transactions, "date", "amount", **kwargs)


@pytest.mark.parametrize("date,amount", [(None, 1), ("not-a-date", 1), ("2024-01-01", "bad"), ("2024-01-01", float("inf"))])
def test_invalid_data_is_not_silently_changed(date, amount):
    with pytest.raises((ValueError, TypeError)):
        window(pd.DataFrame({"date": [date], "amount": [amount]}), "date", "amount")


def test_duplicate_timestamps_keep_transaction_order():
    frame = pd.DataFrame({"date": ["2024-01-01"] * 3, "n": [10, -3, 5]})
    assert window(frame, "date", "n").value.tolist() == [10, 7, 12]
    assert window(frame, "date", "n", operation="rolling_sum", size="1D").value.tolist() == [10, 7, 12]


def test_empty_input_preserves_schema():
    frame = pd.DataFrame({"date": [], "n": []})
    assert window(frame, "date", "n").empty
    assert "value" in window(frame, "date", "n").columns
