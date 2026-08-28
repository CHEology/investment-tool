import json

import pandas as pd
import pytest

from investment_tool.portfolio import Portfolio, max_drawdown, performance_summary


def test_holdings_are_normalized():
    portfolio = Portfolio({"aaa": 10})
    assert portfolio.holdings == {"AAA": 10.0}
    assert portfolio.tickers == ["AAA"]


def test_short_positions_rejected():
    with pytest.raises(ValueError, match="short positions"):
        Portfolio({"AAA": -1})


def test_market_value_includes_cash(prices):
    portfolio = Portfolio({"AAA": 10, "BBB": 4}, cash=100.0)
    value = portfolio.market_value(prices.iloc[0])
    assert value == pytest.approx(10 * 100 + 4 * 50 + 100)


def test_weights_sum_to_one(prices):
    portfolio = Portfolio({"AAA": 10, "BBB": 4}, cash=100.0)
    weights = portfolio.weights(prices.iloc[0])
    assert set(weights.index) == {"AAA", "BBB", "CASH"}
    assert weights.sum() == pytest.approx(1.0)
    assert weights.is_monotonic_decreasing


def test_weights_reject_empty_portfolio(prices):
    with pytest.raises(ValueError, match="no positive market value"):
        Portfolio({"AAA": 0}).weights(prices.iloc[0])


def test_equity_curve_tracks_prices(prices):
    portfolio = Portfolio({"AAA": 1}, name="single")
    curve = portfolio.equity_curve(prices)
    assert curve.name == "single"
    assert len(curve) == len(prices)
    assert curve.iloc[-1] == pytest.approx(prices["AAA"].iloc[-1])


def test_from_json(tmp_path):
    path = tmp_path / "h.json"
    path.write_text(json.dumps({"holdings": {"AAA": 3}, "cash": 50}))
    portfolio = Portfolio.from_json(path)
    assert portfolio.holdings == {"AAA": 3.0}
    assert portfolio.cash == 50.0
    assert portfolio.name == "h"


def test_max_drawdown():
    equity = pd.Series([100, 120, 60, 90])
    assert max_drawdown(equity) == pytest.approx(-0.5)


def test_max_drawdown_is_zero_when_monotonic():
    assert max_drawdown(pd.Series([1, 2, 3])) == pytest.approx(0.0)


def test_performance_summary(prices):
    summary = performance_summary(Portfolio({"AAA": 1}).equity_curve(prices))
    assert summary["total_return"] == pytest.approx(1.001**251 - 1)
    assert summary["cagr"] > 0
    assert summary["volatility"] == pytest.approx(0.0, abs=1e-9)
    assert summary["max_drawdown"] == pytest.approx(0.0)
    assert summary["observations"] == 252


def test_performance_summary_needs_two_points():
    with pytest.raises(ValueError, match="two observations"):
        performance_summary(pd.Series([100.0]))
