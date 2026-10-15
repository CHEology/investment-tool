import json

import pandas as pd
import pytest

from investment_tool.data import DataError, load_snapshot, save_snapshot, validate
from investment_tool.storage import now_utc


@pytest.fixture
def prices():
    return pd.DataFrame(
        [
            {
                "date": day,
                "symbol": symbol,
                "exchange": "XNYS",
                "currency": "USD",
                "open": "10",
                "high": "12",
                "low": "9",
                "close": "11",
                "adj_close": "5.5",
                "volume": "10000",
            }
            for day in ["2024-01-02", "2024-01-03", "2024-01-04"]
            for symbol in ["AAA", "BBB"]
        ]
    )


@pytest.mark.parametrize(
    "column,value",
    [
        ("open", "NaN"),
        ("adj_close", "0"),
        ("high", "8"),
        ("low", "13"),
        ("volume", "0"),
        ("volume", "1.2"),
        ("currency", "CNY"),
        ("exchange", "OTC"),
        ("symbol", "../secret"),
        ("date", "2024-1-2"),
    ],
)
def test_invalid_inputs_block(prices, column, value):
    prices.loc[0, column] = value
    with pytest.raises(DataError):
        validate(prices)


def test_duplicate_missing_and_stale_block(prices):
    with pytest.raises(DataError, match="Duplicate"):
        validate(pd.concat([prices, prices.iloc[:1]]))
    with pytest.raises(DataError, match="Incomplete"):
        validate(prices.iloc[1:])
    with pytest.raises(DataError, match="Stale"):
        validate(prices, "2024-01-05")
    with pytest.raises(DataError, match="Missing sessions"):
        validate(prices[prices.date != "2024-01-03"])


def test_evidence_tamper_blocks(prices, tmp_path):
    path = save_snapshot(
        prices,
        tmp_path,
        [
            {
                "payload": b"exact raw response",
                "provider": "fixture",
                "dataset": "test",
                "url": "local:fixture",
                "retrieved_at": now_utc(),
            }
        ],
    )
    loaded, meta = load_snapshot(path)
    assert len(loaded) == 6 and meta["status"] == "passed"
    (path / "raw-0000.bin").write_bytes(b"changed")
    with pytest.raises(DataError, match="Raw evidence"):
        load_snapshot(path)


def test_failure_manifest_survives(prices, tmp_path):
    prices.loc[0, "close"] = "0"
    with pytest.raises(DataError):
        save_snapshot(
            prices,
            tmp_path,
            [
                {
                    "payload": b"bad",
                    "provider": "fixture",
                    "dataset": "test",
                    "url": "local:fixture",
                    "retrieved_at": now_utc(),
                }
            ],
        )
    manifest = next(tmp_path.glob("snapshots/*/manifest.json"))
    assert json.loads(manifest.read_text())["status"] == "blocked"
