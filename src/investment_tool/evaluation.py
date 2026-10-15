"""Rank IC and relative-performance statistics that do not overstate the evidence."""

import numpy as np
import pandas as pd


def rank_ics(panel: pd.DataFrame, prices: pd.DataFrame, horizon: int) -> pd.Series:
    """Daily Spearman IC of scores at t against adj_close[t+1+horizon] / adj_close[t+1] - 1."""
    forward = prices.shift(-(horizon + 1)) / prices.shift(-1) - 1
    forward = forward.reindex(index=panel.index, columns=panel.columns)
    return panel.corrwith(forward, axis=1, method="spearman").dropna()


def summarize(ics: pd.Series) -> dict:
    count = len(ics)
    mean = float(ics.mean()) if count else None
    std = float(ics.std(ddof=1)) if count > 1 else None
    t_stat = mean / (std / np.sqrt(count)) if std else None
    return {
        "mean": mean,
        "std": std,
        "t_stat": None if t_stat is None else float(t_stat),
        "positive_share": float((ics > 0).mean()) if count else None,
        "dates": count,
    }


def non_overlapping(ics: pd.Series, dates: pd.Index, horizon: int) -> pd.Series:
    """Keep every horizon-th signal date so consecutive label windows do not overlap.

    Signal t's label spans closes t+1..t+1+horizon; t+horizon's starts at t+1+horizon.
    """
    return ics.reindex(dates[::horizon]).dropna()


def by_period(ics: pd.Series, periods: list[tuple[str, str, str]]) -> dict:
    """Summaries keyed by label for inclusive [start, end] date-string windows."""
    return {label: summarize(ics.loc[start:end]) for label, start, end in periods}


def by_year(ics: pd.Series) -> dict:
    years = pd.Index(ics.index).str[:4]
    return {year: summarize(group) for year, group in ics.groupby(years)}


def relative(curve: pd.DataFrame, base: pd.DataFrame) -> dict:
    """Active return, tracking error and information ratio of curve versus base."""
    if not curve.index.equals(base.index):
        raise ValueError("Curves cover different sessions")
    active = (curve["return"] - base["return"]).iloc[1:]
    tracking = float(active.std(ddof=1) * np.sqrt(252)) if len(active) > 1 else None
    annual = float(active.mean() * 252)
    return {
        "annualized_active_return": annual,
        "tracking_error": tracking,
        "information_ratio": annual / tracking if tracking else None,
        # Daily active returns; serial correlation makes this optimistic, not conservative.
        "active_t_stat": float(annual / tracking * np.sqrt(len(active) / 252))
        if tracking
        else None,
        "sessions": len(active),
    }
