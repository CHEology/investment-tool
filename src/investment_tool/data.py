"""Market data fetching with an on-disk cache.

Prices are cached under the data directory (gitignored) so repeated runs and
notebook sessions do not re-hit the network for history that has not changed.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DEFAULT_DATA_DIR = Path(os.environ.get("INVESTMENT_TOOL_DATA_DIR", "data"))


def cache_dir() -> Path:
    """Return the price cache directory, creating it if needed."""
    path = DEFAULT_DATA_DIR / "prices"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path(ticker: str) -> Path:
    return cache_dir() / f"{ticker.upper().replace('/', '_')}.parquet"


def load_cached(ticker: str) -> pd.DataFrame | None:
    """Return cached history for `ticker`, or None if nothing is cached."""
    path = _cache_path(ticker)
    if not path.exists():
        return None
    return pd.read_parquet(path)


def covers(index: pd.Index, start, end, *, tolerance_days: int = 4) -> bool:
    """Whether a cached index spans the requested window.

    The tail gets a few days of slack: the most recent trading day is usually
    behind `end` because of weekends, holidays, and not-yet-closed sessions.
    Without this the cache would be discarded on almost every call.
    """
    if len(index) == 0:
        return False
    index = pd.DatetimeIndex(index).tz_localize(None) if getattr(index, "tz", None) else index
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    first, last = pd.Timestamp(index.min()), pd.Timestamp(index.max())
    return first <= start and last >= end - timedelta(days=tolerance_days)


def fetch_history(
    tickers: str | list[str],
    start: str | date | None = None,
    end: str | date | None = None,
    *,
    use_cache: bool = True,
) -> pd.DataFrame:
    """Fetch daily adjusted close prices as a DataFrame indexed by date.

    Columns are tickers. Missing days (holidays, halts) are left as NaN rather
    than forward-filled, so callers decide how to handle gaps.
    """
    if isinstance(tickers, str):
        tickers = [tickers]
    tickers = [t.upper() for t in tickers]

    if end is None:
        end = date.today()
    if start is None:
        start = pd.Timestamp(end) - timedelta(days=365 * 3)

    frames: dict[str, pd.Series] = {}
    to_fetch: list[str] = []

    for ticker in tickers:
        cached = load_cached(ticker) if use_cache else None
        if cached is not None and covers(cached.index, start, end):
            frames[ticker] = cached["close"]
        else:
            to_fetch.append(ticker)

    if to_fetch:
        for ticker, series in _download(to_fetch, start, end).items():
            frames[ticker] = series
            if use_cache:
                series.to_frame("close").to_parquet(_cache_path(ticker))

    prices = pd.DataFrame(frames).sort_index()
    return prices.loc[str(pd.Timestamp(start).date()) : str(pd.Timestamp(end).date())]


def _download(tickers: list[str], start, end) -> dict[str, pd.Series]:
    """Download from yfinance. Imported lazily so the package imports offline."""
    import yfinance as yf

    raw = yf.download(
        tickers,
        start=start,
        end=end,
        auto_adjust=True,
        progress=False,
        group_by="column",
    )
    if raw.empty:
        raise ValueError(f"no price data returned for {tickers}")

    close = raw["Close"]
    if isinstance(close, pd.Series):  # single ticker
        close = close.to_frame(tickers[0])
    return {ticker: close[ticker].dropna() for ticker in close.columns}


def to_returns(prices: pd.DataFrame, *, log: bool = False) -> pd.DataFrame:
    """Convert a price frame to periodic returns."""
    if log:
        import numpy as np

        return np.log(prices / prices.shift(1)).dropna(how="all")
    return prices.pct_change().dropna(how="all")
