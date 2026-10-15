import numpy as np
import pandas as pd
import pytest

from investment_tool.backtest import select_weights, simulate
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


def test_tied_cutoff_scores_share_the_remaining_budget():
    signal = pd.Series({"A": 3.0, "B": 2.0, "C": 2.0, "D": 1.0})
    weights = select_weights(signal, top_k=2, max_weight=0.5)
    assert weights.to_dict() == {"A": 0.5, "B": 0.25, "C": 0.25, "D": 0.0}
    assert select_weights(signal * 0, top_k=2, max_weight=0.5).eq(0.25).all()
    assert select_weights(signal, top_k=3, max_weight=0.3)["D"] == 0


def test_tie_rule_does_not_depend_on_ticker_order():
    signal = pd.Series({"ZZZ": 1.0, "AAA": 1.0, "MMM": 0.5})
    shuffled = signal.iloc[[2, 0, 1]]
    pd.testing.assert_series_equal(
        select_weights(signal, 1, 1.0).sort_index(), select_weights(shuffled, 1, 1.0).sort_index()
    )
    assert select_weights(signal, 1, 1.0)[["AAA", "ZZZ"]].tolist() == [0.5, 0.5]


def test_simulated_ties_split_instead_of_picking_alphabetically():
    prices, scores, config = setup({"A": [10, 10, 20, 20], "B": [10] * 4, "SPY": [10] * 4})
    scores[:] = 1.0
    curve = simulate(prices, scores, config)
    assert curve.nav.iloc[-1] == pytest.approx(1.5)
    renamed = simulate(
        prices.rename(columns={"A": "B", "B": "A"}),
        scores.rename(columns={"A": "B", "B": "A"}),
        config,
    )
    assert renamed.nav.iloc[-1] == pytest.approx(1.5)


def test_twelve_minus_one_momentum_skips_the_last_month():
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2023-01-02", periods=304)]
    winner = np.r_[np.linspace(10, 20, 280), [5.0] * 22, [10.0] * 2]
    prices = pd.DataFrame({"A": winner, "B": 10.0, "SPY": 10.0}, index=dates)
    scores = pd.DataFrame({"A": 0.0, "B": 0.0}, index=dates[300:])
    config = ResearchConfig(top_k=1, max_weight=1.0, cost_bps=0.0)
    assert simulate(prices, scores, config, mode="momentum_12_1").nav.iloc[-1] == 2
    assert simulate(prices, scores, config, mode="momentum").nav.iloc[-1] == 1
