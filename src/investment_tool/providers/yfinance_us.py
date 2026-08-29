"""yfinance batch EOD — PROVISIONAL scan-tier source for the US trial.

Tradeoff, stated plainly: unofficial Yahoo endpoints via a community library;
no SLA and restrictive upstream terms (personal research use; never
redistributed — the raw store and DB stay out of Git). Every row is labeled
quality=PROVISIONAL and provider=yfinance; finalist prices are cross-checked
against EODHD (VERIFY tier). auto_adjust=False keeps raw Close and Adj Close
distinct.
"""

from __future__ import annotations

import io


def download_batch(tickers: list[str], start: str, end: str):
    """Returns (frame, csv_payload_bytes). Lazy import so offline tests never
    need the library."""
    import yfinance as yf

    frame = yf.download(
        tickers, start=start, end=end, auto_adjust=False, actions=False,
        progress=False, group_by="column", threads=True,
    )
    buf = io.StringIO()
    frame.to_csv(buf)
    return frame, buf.getvalue().encode()


def frame_to_rows(frame, tickers: list[str]) -> dict[str, list[dict]]:
    """Normalize the multi-index frame into per-ticker daily rows with raw and
    adjusted closes kept separate; missing values stay None."""
    import pandas as pd

    out: dict[str, list[dict]] = {}
    single = len(tickers) == 1
    for t in tickers:
        rows = []
        try:
            close = frame["Close"] if single else frame["Close"][t]
            adj = frame["Adj Close"] if single else frame["Adj Close"][t]
            vol = frame["Volume"] if single else frame["Volume"][t]
            op = frame["Open"] if single else frame["Open"][t]
            hi = frame["High"] if single else frame["High"][t]
            lo = frame["Low"] if single else frame["Low"][t]
        except KeyError:
            out[t] = []
            continue
        for idx in frame.index:
            c, a = close.get(idx), adj.get(idx)
            if pd.isna(c) and pd.isna(a):
                continue
            rows.append(
                {
                    "date": idx.strftime("%Y-%m-%d"),
                    "open": None if pd.isna(op.get(idx)) else f"{float(op.get(idx)):.6f}",
                    "high": None if pd.isna(hi.get(idx)) else f"{float(hi.get(idx)):.6f}",
                    "low": None if pd.isna(lo.get(idx)) else f"{float(lo.get(idx)):.6f}",
                    "close": None if pd.isna(c) else f"{float(c):.6f}",
                    "adj_close": None if pd.isna(a) else f"{float(a):.6f}",
                    "volume": None if pd.isna(vol.get(idx)) else str(int(vol.get(idx))),
                }
            )
        out[t] = rows
    return out
