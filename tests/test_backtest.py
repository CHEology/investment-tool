import numpy as np
import pandas as pd
import pytest

from investment_tool.backtest import simulate
from investment_tool.config import ResearchConfig
from investment_tool.data import DataError


def setup(prices):
    frame = pd.DataFrame(prices, index=[f"2024-01-{i:02}" for i in range(2, 2 + len(prices["A"]))])
    scores = pd.DataFrame({"A": 2.0, "B": 1.0}, index=frame.index)
    config = ResearchConfig(top_k=1, max_weight=1.0, cost_bps=0.0, horizon=20)
    return frame, scores, config


def test_signal_cannot_capture_next_close_entry_day_jump():
    prices, scores, config = setup({"A": [10, 100, 110, 121], "B": [10] * 4, "SPY": [10] * 4})
    curve = simulate(prices, scores, config)
    assert curve.nav.iloc[1] == 1
    assert curve.nav.iloc[-1] == pytest.approx(1.21)


def test_entry_exit_fees_and_concentration():
    prices, scores, _ = setup({"A": [10] * 5, "B": [10] * 5, "SPY": [10] * 5})
    config = ResearchConfig(top_k=1, max_weight=0.5, cost_bps=100.0)
    curve = simulate(prices, scores, config)
    assert curve.gross_exposure.iloc[1] == 0.5
    assert curve.nav.iloc[-1] == pytest.approx((1 / 1.005) * 0.995)
    assert curve.gross_exposure.iloc[-1] == 0


def test_constant_ranks_do_not_create_daily_rebalancing():
    prices, scores, config = setup({"A": [10, 11, 12, 13], "B": [10] * 4, "SPY": [10] * 4})
    curve = simulate(prices, scores, config)
    assert curve.turnover.iloc[2] == 0


def test_missing_scores_fail():
    prices, scores, config = setup({"A": [10] * 5, "B": [10] * 5, "SPY": [10] * 5})
    scores.iloc[2, 0] = np.nan
    with pytest.raises(DataError, match="Incomplete"):
        simulate(prices, scores, config)


def test_future_signal_cannot_change_previous_nav():
    prices, scores, config = setup({"A": [10, 11, 12, 13, 14], "B": [10] * 5, "SPY": [10] * 5})
    first = simulate(prices, scores, config)
    scores.iloc[3:, 0] = -10
    second = simulate(prices, scores, config)
    pd.testing.assert_frame_equal(first.iloc[:4], second.iloc[:4])
