"""Daily marked-to-market, long-only research simulation with delayed execution."""

import numpy as np
import pandas as pd

from .config import ResearchConfig
from .data import DataError

MODES = ("model", "equal_weight", "momentum", "momentum_12_1", "benchmark")


def select_weights(signal: pd.Series, top_k: int, max_weight: float) -> pd.Series:
    """Target weights for the top_k highest signals, independent of symbol order.

    Tie rule: names strictly above the cutoff score get max_weight each. All names
    whose score equals the cutoff (the top_k-th highest) share the remaining
    (top_k - n_above) * max_weight equally, so total exposure and the per-name cap
    are unchanged and no name wins a tie by its ticker. Equality is exact.
    """
    if top_k > len(signal):
        raise DataError("Not enough candidates for top_k")
    cutoff = signal.nlargest(top_k, keep="all").min()
    above = signal > cutoff
    tied = signal == cutoff
    weights = pd.Series(0.0, index=signal.index)
    weights[above] = max_weight
    weights[tied] = (top_k - int(above.sum())) * max_weight / int(tied.sum())
    return weights


def simulate(
    prices: pd.DataFrame,
    scores: pd.DataFrame,
    config: ResearchConfig,
    *,
    mode: str = "model",
    cost_bps: float | None = None,
) -> pd.DataFrame:
    """Observe ranks after close t; rebalance at close t+1, every horizon sessions.

    Weights drift with returns. Fees apply to absolute buy+sell notional. Shares
    may be fractional; cash earns zero. Final NAV includes liquidation fees.
    """
    if mode not in MODES:
        raise ValueError("Unknown strategy")
    cost = (config.cost_bps if cost_bps is None else cost_bps) / 10000
    if not 0 <= cost < 0.1:
        raise ValueError("Invalid cost")
    if scores.empty or not scores.index.is_unique or not scores.columns.is_unique:
        raise DataError("Empty or duplicate score panel")
    if not np.isfinite(scores.to_numpy()).all():
        raise DataError("Incomplete score panel")
    if config.benchmark not in prices or config.benchmark in scores:
        raise DataError("Benchmark must be present in prices and excluded from candidates")
    if len(scores.columns) < config.top_k:
        raise DataError("Not enough candidates for top_k")
    missing = set(scores.columns) - set(prices.columns)
    if missing:
        raise DataError("Scores lack executable price histories")
    dates = prices.index
    first = dates.get_loc(scores.index[0])
    final = dates.get_loc(scores.index[-1])
    if final - first < 2:
        raise DataError("At least three out-of-sample sessions required")
    if not scores.index.equals(dates[first : final + 1]):
        raise DataError("Score panel is not contiguous")
    returns = prices.pct_change(fill_method=None)
    momentum = {
        "momentum": prices / prices.shift(60) - 1,
        # 12-1: trailing 252-session return, skipping the most recent 21 sessions.
        "momentum_12_1": prices.shift(21) / prices.shift(252) - 1,
    }
    weights = pd.Series(0.0, index=prices.columns)
    nav = 1.0
    pending = None
    records = []
    for offset, day in enumerate(dates[first : final + 1]):
        daily_return = 0.0
        turnover = 0.0
        if offset:
            r = returns.loc[day]
            if not np.isfinite(r).all():
                raise DataError("Missing mark-to-market return")
            gross = float((weights * r).sum())
            nav *= 1 + gross
            weights = weights * (1 + r) / (1 + gross)
            daily_return = gross
        if pending is not None:
            # Solve post-fee NAV: x = 1 - cost * sum(abs(x*target-current)).
            scale = 1.0
            for _ in range(40):
                scale = 1 - cost * float((pending * scale - weights).abs().sum())
            turnover = float((pending * scale - weights).abs().sum())
            nav *= scale
            daily_return = (1 + daily_return) * scale - 1
            weights = pending
            pending = None
        if day == dates[final]:
            liquidation = float(weights.abs().sum())
            nav *= 1 - cost * liquidation
            daily_return = (1 + daily_return) * (1 - cost * liquidation) - 1
            turnover += liquidation
            weights *= 0
        elif offset % config.horizon == 0:
            target = pd.Series(0.0, index=prices.columns)
            if mode == "benchmark":
                target[config.benchmark] = 1.0
            elif mode == "equal_weight":
                target.loc[scores.columns] = min(1 / len(scores.columns), config.max_weight)
            else:
                signal = (
                    scores.loc[day] if mode == "model" else momentum[mode].loc[day, scores.columns]
                )
                if not np.isfinite(signal).all():
                    raise DataError("Missing ranking signal")
                target.loc[signal.index] = select_weights(signal, config.top_k, config.max_weight)
            pending = target
        records.append(
            {
                "date": day,
                "nav": nav,
                "return": daily_return,
                "turnover": turnover,
                "gross_exposure": float(weights.sum()),
            }
        )
    return pd.DataFrame(records).set_index("date")


def metrics(curve: pd.DataFrame) -> dict:
    returns = curve["return"].iloc[1:]
    years = len(returns) / 252
    volatility = returns.std(ddof=1)
    return {
        "total_return": float(curve.nav.iloc[-1] - 1),
        "annualized_return": float(curve.nav.iloc[-1] ** (1 / years) - 1),
        "annualized_volatility": float(volatility * np.sqrt(252)),
        "sharpe_zero_cash_rate": float(returns.mean() / volatility * np.sqrt(252))
        if volatility > 0
        else None,
        "max_drawdown": float((curve.nav / curve.nav.cummax() - 1).min()),
        "total_traded_notional_over_nav": float(curve.turnover.sum()),
        "sessions": len(curve),
    }
