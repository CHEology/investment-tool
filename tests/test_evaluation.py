import json

import numpy as np
import pandas as pd
import pytest

from investment_tool import evaluation
from investment_tool.config import ResearchConfig
from investment_tool.data import DataError
from investment_tool.demo import make_demo
from investment_tool.pipeline import check_universe, research


def dated(values, start="2023-12-27"):
    index = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start, periods=len(values))]
    return pd.Series(values, index=index, dtype=float)


def test_rank_ic_uses_next_close_entry_window():
    dates = [d.strftime("%Y-%m-%d") for d in pd.bdate_range("2024-01-01", periods=12)]
    rng = np.random.default_rng(0)
    prices = pd.DataFrame(
        np.exp(np.cumsum(rng.normal(0, 0.02, (12, 4)), axis=0)), index=dates, columns=list("ABCD")
    )
    forward = prices.shift(-3) / prices.shift(-1) - 1
    panel = forward.iloc[:9]
    ics = evaluation.rank_ics(panel, prices, horizon=2)
    assert len(ics) == 9 and np.allclose(ics, 1.0)
    assert np.allclose(evaluation.rank_ics(-panel, prices, horizon=2), -1.0)


def test_summary_t_stat_and_non_overlapping_sampling():
    ics = dated([0.1, 0.3, 0.1, 0.3, np.nan, 0.2])
    stats = evaluation.summarize(ics.dropna())
    assert stats["mean"] == pytest.approx(0.2)
    expected = 0.2 / (np.std([0.1, 0.3, 0.1, 0.3, 0.2], ddof=1) / np.sqrt(5))
    assert stats["t_stat"] == pytest.approx(expected)
    assert stats["positive_share"] == 1 and stats["dates"] == 5
    sparse = evaluation.non_overlapping(ics.dropna(), ics.index, horizon=2)
    assert sparse.index.tolist() == [ics.index[0], ics.index[2]]
    assert evaluation.summarize(pd.Series(dtype=float))["mean"] is None
    assert evaluation.summarize(dated([0.1]))["t_stat"] is None


def test_ic_by_year_and_period():
    ics = dated([0.1, 0.2, 0.3, 0.4, 0.5])
    years = evaluation.by_year(ics)
    assert {year: stats["dates"] for year, stats in years.items()} == {"2023": 3, "2024": 2}
    assert years["2024"]["mean"] == pytest.approx(0.45)
    periods = evaluation.by_period(ics, [("f0", ics.index[1], ics.index[3])])
    assert periods["f0"]["mean"] == pytest.approx(0.3)


def test_active_return_tracking_error_and_information_ratio():
    base = pd.DataFrame({"return": [0.0, 0.01, -0.01, 0.02, 0.0]})
    active = np.array([0.001, 0.003, -0.001, 0.001])
    curve = pd.DataFrame({"return": base["return"] + np.r_[0.5, active]})
    result = evaluation.relative(curve, base)
    tracking = active.std(ddof=1) * np.sqrt(252)
    assert result["annualized_active_return"] == pytest.approx(active.mean() * 252)
    assert result["tracking_error"] == pytest.approx(tracking)
    assert result["information_ratio"] == pytest.approx(active.mean() * 252 / tracking)
    assert result["sessions"] == 4
    assert evaluation.relative(base, base)["information_ratio"] is None
    with pytest.raises(ValueError):
        evaluation.relative(curve.iloc[1:], base)


def test_universe_must_be_much_larger_than_top_k():
    config = ResearchConfig(top_k=4, max_weight=0.25)
    assert config.min_universe() == 14
    assert ResearchConfig(top_k=3, max_weight=0.33).min_universe() == 10
    check_universe([f"S{i}" for i in range(14)], config)
    with pytest.raises(DataError, match="at least 14 candidates"):
        check_universe([f"S{i}" for i in range(13)], config)
    with pytest.raises(DataError, match="Benchmark"):
        check_universe(["SPY"] + [f"S{i}" for i in range(20)], config)
    with pytest.raises(ValueError, match="max_selection_fraction"):
        ResearchConfig(max_selection_fraction=0)


def test_research_blocks_near_equal_weight_selection(tmp_path):
    snapshot = make_demo(tmp_path)
    with pytest.raises(DataError, match="indistinguishable from equal weight"):
        research(snapshot, tmp_path, ResearchConfig(top_k=5, max_weight=0.2))
    (run,) = (tmp_path / "runs").iterdir()
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["status"] == "failed" and manifest["error_type"] == "DataError"
