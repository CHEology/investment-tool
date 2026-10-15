import json

import pandas as pd
import pytest

from investment_tool.brief import portfolio_html, render_brief
from investment_tool.config import ResearchConfig
from investment_tool.daily import (
    latest_fold,
    model_due,
    prepare_daily,
    price_summary,
    rank_candidates,
)
from investment_tool.data import DataError, load_snapshot
from investment_tool.demo import make_demo
from investment_tool.storage import digest, read_json, write_json


def test_daily_training_labels_end_before_prediction():
    config = ResearchConfig()
    fold = latest_fold(1500, config)
    assert fold.test_start == fold.test_end == 1499
    assert fold.valid_end + config.horizon + 1 < fold.test_start
    assert fold.train_end + config.horizon + 1 < fold.valid_start
    assert fold.train_end - fold.train_start + 1 == 504
    with pytest.raises(DataError, match="history"):
        latest_fold(500, config)


def test_retraining_counts_sessions_and_configuration():
    dates = pd.bdate_range("2024-01-01", periods=23).strftime("%Y-%m-%d").tolist()
    meta = {"key": {"universe": "A"}, "trained_as_of": dates[0]}
    assert not model_due(meta, meta["key"], dates[:21], 21)
    assert model_due(meta, meta["key"], dates[:22], 21)
    assert model_due(meta, {"universe": "B"}, dates[:2], 21)


def test_returns_use_adjusted_prices_and_ranking_keeps_ties():
    prices = pd.DataFrame(
        {
            "symbol": ["A"] * 201,
            "date": pd.bdate_range("2024-01-01", periods=201),
            "close": [100.0] * 200 + [50.0],
            "adj_close": [50.0] * 201,
        }
    )
    summary = price_summary(prices)["A"]
    assert summary["close"] == 50 and summary["return_1d"] == 0
    idx = pd.MultiIndex.from_product(
        [[pd.Timestamp("2024-01-01")], ["A", "B", "C"]], names=["datetime", "instrument"]
    )
    ranks = rank_candidates(
        pd.Series([3.0, 2.0, 2.0], index=idx), ResearchConfig(top_k=2, max_weight=0.5)
    )
    assert [r["rank"] for r in ranks] == [1, 2, 2]
    assert all(r["selected"] for r in ranks)


@pytest.mark.quant
def test_daily_reuses_real_model_and_session_is_idempotent(tmp_path, monkeypatch):
    pytest.importorskip("qlib")
    from investment_tool import daily

    snapshot = make_demo(tmp_path)
    prices, meta = load_snapshot(snapshot)
    dates = sorted(prices.date.unique())
    symbols = sorted(set(prices.symbol) - {"SPY"})
    config_path = tmp_path / "daily.toml"
    config_path.write_text(
        '[daily]\nstart="2018-01-01"\nretrain_sessions=21\n'
        f'symbols={json.dumps(symbols)}\nmarket_symbols=["SPY"]\n'
        "[research]\ntrain_sessions=252\nvalid_sessions=63\ntop_k=4\n"
        "max_weight=0.25\nnum_boost_round=5\n"
    )

    # Only this isolated test bypasses the production synthetic-data rejection.
    def fixture_snapshot(_):
        return prices[prices.date <= end].copy(), meta | {"synthetic": False, "end": end}

    monkeypatch.setattr(daily, "load_snapshot", fixture_snapshot)
    root = tmp_path / "daily"
    end = dates[-2]
    first = prepare_daily(root, config_path, snapshot, now=pd.Timestamp(end + "T23:59:00Z"))
    a = read_json(first / "packet.json")
    prior_manifest = read_json(first / "manifest.json")
    prior_manifest["request"]["price_symbols"].append("FORMER_HOLDING")
    write_json(first / "manifest.json", prior_manifest)
    end = dates[-1]
    now = pd.Timestamp(end + "T23:59:00Z")
    second = prepare_daily(root, config_path, snapshot, now=now)
    b = read_json(second / "packet.json")
    assert a["retrained"] and not b["retrained"]
    assert a["model"]["sha256"] == b["model"]["sha256"]
    assert not b["changes"]["baseline"]
    assert all(c["previous_rank"] is not None for c in b["candidates"])
    before = (second / "manifest.json").read_bytes()
    assert prepare_daily(root, config_path, snapshot, now=now) == second
    assert (second / "manifest.json").read_bytes() == before
    write_json(second / "packet.json", {})
    with pytest.raises(DataError, match="hash"):
        prepare_daily(root, config_path, snapshot, now=now)


def test_broker_values_are_not_silently_reconciled():
    portfolio = {
        "retrieved_at": "2026-10-15T02:00:00Z",
        "summary": {
            "currency": "USD",
            "net_liquidation": "2000",
            "total_cash_value": "1000",
            "gross_position_value": "990",
        },
        "positions": [
            {
                "contract_description": "TEST",
                "position": "10",
                "average_price": "90",
                "market_price": "100",
                "market_value": "1000",
                "currency": "USD",
                "unrealized_pnl": "100",
            }
        ],
    }
    html = portfolio_html(portfolio)
    assert "+10.00 USD" in html and "50.0%" in html
    assert "2026-10-14 22:00" in html


def test_brief_requires_matching_session_and_evidence(tmp_path):
    day = tmp_path / "days" / "2024-01-01"
    write_json(day / "packet.json", {"as_of": "2024-01-01"})
    write_json(day / "manifest.json", {"packet_sha256": digest(day / "packet.json")})
    write_json(day / "analysis.json", {"as_of": "2024-01-02"})
    with pytest.raises(DataError, match="session"):
        render_brief(day)
    (day / "evidence.txt").write_text("changed evidence")
    write_json(
        day / "analysis.json",
        {
            "as_of": "2024-01-01",
            "sources": {
                "test": {"evidence": "evidence.txt", "sha256": "incorrect"},
            },
        },
    )
    with pytest.raises(DataError, match="evidence hash"):
        render_brief(day)
