"""Deterministic synthetic prices for integration checks, never market evidence."""

from pathlib import Path

import numpy as np
import pandas as pd

from .data import save_snapshot, sessions
from .storage import now_utc


def make_demo(root: Path) -> Path:
    rng = np.random.default_rng(42)
    dates = sessions("2021-01-04", "2023-12-29")
    market = rng.normal(0.0002, 0.009, len(dates))
    rows = []
    for index, symbol in enumerate(["SPY"] + [f"DEMO{n:02d}" for n in range(16)]):
        returns = market + rng.normal(0.00005 * (index % 3), 0.012, len(dates))
        closes = (40 + index * 7) * np.exp(np.cumsum(returns))
        opens = closes * np.exp(rng.normal(0, 0.003, len(dates)))
        for day, opening, close in zip(dates, opens, closes, strict=True):
            rows.append(
                {
                    "date": day.strftime("%Y-%m-%d"),
                    "symbol": symbol,
                    "currency": "USD",
                    "exchange": "XNYS",
                    "open": opening,
                    "high": max(opening, close) * 1.01,
                    "low": min(opening, close) * 0.99,
                    "close": close,
                    "adj_close": close,
                    "volume": int(rng.integers(1e6, 1e7)),
                }
            )
    frame = pd.DataFrame(rows)
    return save_snapshot(
        frame,
        root,
        [
            {
                "payload": frame.to_csv(index=False).encode(),
                "provider": "synthetic_seed_42",
                "dataset": "integration_fixture",
                "url": "local:synthetic",
                "retrieved_at": now_utc(),
            }
        ],
        synthetic=True,
    )
