"""Portfolio construction and performance metrics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

TRADING_DAYS = 252


@dataclass
class Portfolio:
    """A set of holdings, expressed as share counts per ticker."""

    holdings: dict[str, float]
    cash: float = 0.0
    name: str = "portfolio"
    _prices: pd.DataFrame | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.holdings = {t.upper(): float(q) for t, q in self.holdings.items()}
        if any(q < 0 for q in self.holdings.values()):
            raise ValueError("short positions are not supported yet")

    @property
    def tickers(self) -> list[str]:
        return sorted(self.holdings)

    @classmethod
    def from_json(cls, path: str | Path) -> Portfolio:
        """Load holdings from a JSON file: {"holdings": {...}, "cash": 0}."""
        data = json.loads(Path(path).read_text())
        return cls(
            holdings=data["holdings"],
            cash=float(data.get("cash", 0.0)),
            name=data.get("name", Path(path).stem),
        )

    def market_value(self, prices: pd.Series | pd.DataFrame) -> float | pd.Series:
        """Total value at a point in time (Series) or over time (DataFrame)."""
        if isinstance(prices, pd.Series):
            return sum(prices[t] * q for t, q in self.holdings.items()) + self.cash
        weighted = prices[self.tickers].mul(pd.Series(self.holdings), axis=1)
        return weighted.sum(axis=1) + self.cash

    def weights(self, prices: pd.Series) -> pd.Series:
        """Current allocation as fractions of total value, including cash."""
        values = {t: prices[t] * q for t, q in self.holdings.items()}
        if self.cash:
            values["CASH"] = self.cash
        total = sum(values.values())
        if total <= 0:
            raise ValueError("portfolio has no positive market value")
        return (pd.Series(values) / total).sort_values(ascending=False)

    def equity_curve(self, prices: pd.DataFrame) -> pd.Series:
        """Portfolio value over time, assuming a static share count."""
        return self.market_value(prices).rename(self.name)


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough decline, as a negative fraction."""
    running_peak = equity.cummax()
    return float((equity / running_peak - 1).min())


def performance_summary(equity: pd.Series, *, risk_free_rate: float = 0.0) -> dict[str, float]:
    """Headline risk/return statistics for an equity curve.

    `risk_free_rate` is annualized and used only for the Sharpe ratio.
    """
    equity = equity.dropna()
    if len(equity) < 2:
        raise ValueError("need at least two observations")

    returns = equity.pct_change().dropna()
    years = len(returns) / TRADING_DAYS
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
    volatility = float(returns.std(ddof=1) * np.sqrt(TRADING_DAYS))

    cagr = float((equity.iloc[-1] / equity.iloc[0]) ** (1 / years) - 1) if years > 0 else 0.0
    sharpe = (cagr - risk_free_rate) / volatility if volatility > 0 else float("nan")

    return {
        "start_value": float(equity.iloc[0]),
        "end_value": float(equity.iloc[-1]),
        "total_return": total_return,
        "cagr": cagr,
        "volatility": volatility,
        "sharpe": float(sharpe),
        "max_drawdown": max_drawdown(equity),
        "observations": int(len(equity)),
    }
