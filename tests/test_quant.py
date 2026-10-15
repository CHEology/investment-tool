import json

import numpy as np
import pandas as pd
import pytest

from investment_tool.config import ResearchConfig
from investment_tool.data import load_snapshot
from investment_tool.demo import make_demo
from investment_tool.qlib_engine import export_qlib, features, folds


def test_purge_covers_full_label_and_rolling_test_is_disjoint():
    config = ResearchConfig()
    windows = folds(1500, config)
    seen = set()
    for fold in windows:
        assert fold.train_end + config.horizon + 1 < fold.valid_start
        assert fold.valid_end + config.horizon + 1 < fold.test_start
        assert fold.train_start >= config.warmup_sessions
        current = set(range(fold.test_start, fold.test_end + 1))
        assert not seen & current
        seen |= current


@pytest.mark.quant
def test_real_qlib_export_labels_and_causality(tmp_path):
    pytest.importorskip("qlib")
    snapshot = make_demo(tmp_path)
    prices, _ = load_snapshot(snapshot)
    dates = sorted(prices.date.unique())
    symbols = ["DEMO00", "DEMO01"]
    prices = prices[prices.symbol.isin(symbols)]
    provider = tmp_path / "qlib"
    export_qlib(prices, provider)
    frame = features(provider, symbols, dates, 20)
    assert frame["feature"].shape[1] == 158
    close = prices[prices.symbol == "DEMO00"].set_index("date").adj_close
    actual = frame.loc[(pd.Timestamp(dates[100]), "DEMO00"), ("label", "LABEL0")]
    assert actual == pytest.approx(close.iloc[121] / close.iloc[101] - 1, abs=1e-6)
    assert frame.loc[(pd.Timestamp(dates[-1]), "DEMO00"), "feature"].notna().sum() > 150
    changed = prices.copy()
    changed.loc[changed.date > dates[200], ["open", "high", "low", "close", "adj_close"]] *= 3
    export_qlib(changed, tmp_path / "changed")
    later = features(tmp_path / "changed", symbols, dates, 20)
    pd.testing.assert_frame_equal(
        frame["feature"].loc[: dates[200]], later["feature"].loc[: dates[200]]
    )


@pytest.mark.quant
def test_end_to_end_real_engines(tmp_path):
    pytest.importorskip("qlib")
    pytest.importorskip("lightgbm")
    from investment_tool.pipeline import research

    snapshot = make_demo(tmp_path)
    config = ResearchConfig(
        train_sessions=252,
        valid_sessions=63,
        test_sessions=200,
        top_k=4,
        max_weight=0.25,
        num_boost_round=5,
    )
    run = research(snapshot, tmp_path, config)
    manifest = json.loads((run / "manifest.json").read_text())
    ranking = json.loads((run / "ranking.json").read_text())
    assert manifest["status"] == "completed" and manifest["synthetic"]
    assert not manifest["live_ready"]
    assert len(ranking["candidates"]) == 16
    assert "SPY" not in {c["symbol"] for c in ranking["candidates"]}
    scores = pd.read_csv(run / "scores.csv", index_col=0)
    assert np.isfinite(scores).all().all()
    evaluated = json.loads((run / "evaluation.json").read_text())
    assert set(evaluated["strategies"]) >= {"momentum", "momentum_12_1", "equal_weight"}
    assert set(evaluated["relative_to_equal_weight"]) == {"model", "momentum", "momentum_12_1"}
    assert evaluated["rank_ic_non_overlapping"]["step_sessions"] == 20
    assert evaluated["rank_ic_by_year"]
    diagnostics = evaluated["fold_diagnostics"]
    assert diagnostics["did_not_learn"] == sum(f["did_not_learn"] for f in evaluated["folds"])
    for fold in evaluated["folds"]:
        assert fold["did_not_learn"] == (fold["best_iteration"] <= 5)
        assert 0 < fold["mean_unique_score_ratio"] <= 1 and "rank_ic" in fold
    selected = [c for c in ranking["candidates"] if c["selected"]]
    assert sum(c["target_weight"] for c in selected) == pytest.approx(1.0)
    report = (run / "report.html").read_text()
    assert "相对等权股票池" in report and "12-1 月动量" in report and "训练窗口" in report
