"""Targeted US price coverage for the trial: event-linked companies plus
SPY/QQQ benchmarks. Raw and adjusted closes stay distinct in security_day
(ret from adjusted-consecutive, basis ADJ_CONSEC); benchmarks go to
benchmark_day on their adjusted series. Never a full-market backfill.
"""

from __future__ import annotations

import sqlite3

from investment_tool.lineage import record_fetch
from investment_tool.providers import yfinance_us
from investment_tool.quality import Quality, QualityState

BENCHMARKS = ("SPY", "QQQ")


def store_ticker_rows(conn: sqlite3.Connection, listing_id: str, rows: list[dict],
                      manifest_id: str, provider: str = "yfinance") -> int:
    prev_adj = None
    n = 0
    for r in rows:
        ret = None
        if prev_adj not in (None, "", "0") and r["adj_close"] not in (None, ""):
            try:
                ret = float(r["adj_close"]) / float(prev_adj) - 1.0
            except (ValueError, ZeroDivisionError):
                ret = None
        conn.execute(
            "INSERT INTO security_day(listing_id, trade_date, open, high, low, close,"
            " volume, ret, ret_basis, adj_close, adj_method, currency, limit_state,"
            " provider, quality, manifest_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(listing_id, trade_date) DO UPDATE SET"
            "  open=excluded.open, high=excluded.high, low=excluded.low,"
            "  close=excluded.close, volume=excluded.volume, ret=excluded.ret,"
            "  ret_basis=excluded.ret_basis, adj_close=excluded.adj_close,"
            "  provider=excluded.provider, quality=excluded.quality,"
            "  manifest_id=excluded.manifest_id",
            (
                listing_id, r["date"], r["open"], r["high"], r["low"], r["close"],
                r["volume"], ret, "ADJ_CONSEC" if ret is not None else None,
                r["adj_close"], "PROVIDER_ADJ", "USD", "FREE", provider,
                QualityState.PROVISIONAL.value, manifest_id,
            ),
        )
        prev_adj = r["adj_close"] or prev_adj
        n += 1
    conn.commit()
    return n


def ensure_prices(conn: sqlite3.Connection, cfg, tickers_by_listing: dict[str, str],
                  start: str, end: str) -> dict:
    """Fetch one yfinance batch for the given {listing_id: ticker} map plus
    benchmarks; store rows; return a coverage report."""
    tickers = sorted(set(tickers_by_listing.values()) | set(BENCHMARKS))
    frame, payload = yfinance_us.download_batch(tickers, start, end)
    m = record_fetch(
        conn, provider="yfinance", dataset="us_eod_batch",
        params={"tickers": len(tickers), "start": start, "end": end},
        source_url="https://query1.finance.yahoo.com/v8/finance/chart/ (batch via yfinance)",
        payload=payload, http_status=200,
        quality=Quality(QualityState.PROVISIONAL, "unofficial batch; scan tier"),
        config_version=cfg.id,
    )
    by_ticker = yfinance_us.frame_to_rows(frame, tickers)

    covered = empty = 0
    for listing_id, ticker in sorted(tickers_by_listing.items()):
        rows = by_ticker.get(ticker, [])
        if not rows:
            empty += 1
            continue
        store_ticker_rows(conn, listing_id, rows, m.manifest_id)
        covered += 1
    bench_rows = 0
    for b in BENCHMARKS:
        for r in by_ticker.get(b, []):
            if r["adj_close"] is None:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO benchmark_day(index_id, trade_date, close, provider,"
                " quality, manifest_id) VALUES(?,?,?,?,?,?)",
                (b, r["date"], r["adj_close"], "yfinance",
                 QualityState.PROVISIONAL.value, m.manifest_id),
            )
            bench_rows += 1
    conn.commit()
    return {"tickers_requested": len(tickers), "listings_covered": covered,
            "listings_empty": empty, "benchmark_rows": bench_rows,
            "manifest": m.manifest_id}


def adj_series(
    conn: sqlite3.Connection, listing_id: str, end: str
) -> list[tuple[str, float, float | None]]:
    """[(date, adj_close, volume)] ascending through `end` — adjusted lineage only."""
    out = []
    for r in conn.execute(
        "SELECT trade_date, adj_close, volume FROM security_day WHERE listing_id=?"
        " AND adj_close IS NOT NULL AND trade_date<=? ORDER BY trade_date",
        (listing_id, end),
    ):
        try:
            vol = float(r["volume"]) if r["volume"] is not None else None
        except ValueError:
            vol = None
        out.append((r["trade_date"], float(r["adj_close"]), vol))
    return out


def bench_series(conn: sqlite3.Connection, index_id: str, end: str) -> dict[str, float]:
    return {
        r["trade_date"]: float(r["close"]) for r in conn.execute(
            "SELECT trade_date, close FROM benchmark_day WHERE index_id=? AND trade_date<=?"
            " ORDER BY trade_date", (index_id, end),
        )
    }


def verify_with_eodhd(conn: sqlite3.Connection, cfg, pairs: list[tuple[str, str]],
                      start: str, budget: int = 8) -> list[dict]:
    """Cross-check finalists' recent adjusted closes against EODHD (VERIFY
    tier, small free-plan budget). Mismatch > 0.75% -> CONFLICT note."""
    import json as json_mod

    from investment_tool.providers import eodhd as eodhd_mod

    http = eodhd_mod.client()
    results = []
    for listing_id, ticker in pairs[:budget]:
        payload, status, url = eodhd_mod.fetch_eod(http, f"{ticker}.US", start)
        quality = Quality(QualityState.OK if status == 200 else QualityState.ERROR,
                          f"http={status}")
        record_fetch(conn, provider="eodhd", dataset="eod_verify",
                     params={"ticker": ticker, "from": start}, source_url=url,
                     payload=payload, http_status=status, quality=quality,
                     config_version=cfg.id)
        if status != 200:
            results.append({"ticker": ticker, "state": f"ERROR http={status}"})
            continue
        rows = json_mod.loads(payload)
        deltas = []
        for r in rows:
            stored = conn.execute(
                "SELECT adj_close FROM security_day WHERE listing_id=? AND trade_date=?",
                (listing_id, r["date"]),
            ).fetchone()
            if stored and stored["adj_close"] and r.get("adjusted_close"):
                d = abs(float(stored["adj_close"]) / float(r["adjusted_close"]) - 1.0)
                deltas.append(d)
        state = ("NO_OVERLAP" if not deltas
                 else "VERIFIED" if max(deltas) <= 0.0075 else "CONFLICT")
        results.append({"ticker": ticker, "state": state,
                        "overlap_days": len(deltas),
                        "max_delta": round(max(deltas), 5) if deltas else None})
    return results
