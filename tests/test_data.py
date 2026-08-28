import pandas as pd
import pytest

from investment_tool.data import covers, to_returns


def test_simple_returns(prices):
    returns = to_returns(prices)
    assert len(returns) == len(prices) - 1
    assert returns["AAA"].iloc[0] == pytest.approx(0.001)
    assert (returns["BBB"] == 0).all()


def test_log_returns_are_smaller_than_simple(prices):
    simple = to_returns(prices)["AAA"]
    log = to_returns(prices, log=True)["AAA"]
    assert (log <= simple + 1e-12).all()


def test_covers_accepts_a_superset_window(prices):
    assert covers(prices.index, "2024-02-01", "2024-06-01")


def test_covers_rejects_a_missing_head(prices):
    assert not covers(prices.index, "2023-01-01", "2024-06-01")


def test_covers_rejects_a_missing_tail(prices):
    assert not covers(prices.index, "2024-02-01", "2025-06-01")


def test_covers_tolerates_a_weekend_gap_at_the_tail(prices):
    end = prices.index[-1] + pd.Timedelta(days=3)
    assert covers(prices.index, "2024-02-01", end)


def test_covers_rejects_an_empty_index():
    assert not covers(pd.DatetimeIndex([]), "2024-01-01", "2024-02-01")
