"""A-share price spine ingestion (Eastmoney PROVISIONAL fallback).

Every row written here carries quality=PROVISIONAL and a manifest id; the
taint propagates to any candidate as verification debt (DESIGN 5.9).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from investment_tool.lineage import record_fetch, utc_now
from investment_tool.providers import eastmoney
from investment_tool.quality import Quality, QualityState

PROV = QualityState.PROVISIONAL.value

# Board price limits (frozen v0 config mirrors these; ST on main board is 5%).
BOARD_LIMITS = {"MAIN": "0.10", "CHINEXT": "0.20", "STAR": "0.20", "BSE": "0.30"}


@dataclass(frozen=True)
class BackfillResult:
    bars: int
    quality_state: str
    provider: str


def limit_state(pct_chg: float | None, board: str | None, is_st: bool) -> str:
    if pct_chg is None:
        return "NO_TRADE"
    limit = (0.05 if (is_st and board == "MAIN")
             else float(BOARD_LIMITS.get(board or "MAIN", "0.10")))
    frac = pct_chg / 100.0
    if frac >= limit - 0.002:
        return "LIMIT_UP"
    if frac <= -limit + 0.002:
        return "LIMIT_DOWN"
    return "FREE"


def _listings(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        "SELECT listing_id, ticker, exchange, board FROM listing WHERE exchange IN"
        " ('SSE','SZSE','BSE') AND status='LISTED'"
    ).fetchall()
    return {r["ticker"]: r for r in rows}


def ingest_snapshot(conn: sqlite3.Connection, config_version: str, asof_date: str) -> dict:
    """Full-market end-of-day snapshot -> security_day + market_snapshot for asof_date.

    Snapshot reflects the CURRENT session only; callers must pass today's date.
    """
    http = eastmoney.client()
    listings = _listings(conn)
    seen = 0
    pages = 0
    page = 1
    while True:
        payload, status, url = eastmoney.fetch_snapshot_page(http, page)
        quality = Quality(
            QualityState.PROVISIONAL if status == 200 else QualityState.ERROR, f"http={status}"
        )
        m = record_fetch(
            conn, provider="eastmoney", dataset="snapshot",
            params={"page": page, "date": asof_date},
            source_url=url, payload=payload, http_status=status, quality=quality,
            config_version=config_version,
        )
        if status != 200:
            return {"date": asof_date, "rows": seen, "pages": pages, "error": f"http={status}"}
        total, rows = eastmoney.parse_snapshot_page(payload)
        if not rows:
            break
        pages += 1
        for r in rows:
            lst = listings.get(r["code"])
            if lst is None:
                continue
            is_st = 1 if (r["name"] and "ST" in r["name"].upper()) else 0
            pct = float(r["pct_chg"]) if r["pct_chg"] is not None else None
            ret = pct / 100.0 if pct is not None else None
            ret_basis = "EXCHANGE_PCT" if ret is not None else None
            if lst["exchange"] == "BSE":
                # BSE history is raw-lineage (Sina). Keep the listing's basis
                # pure RAW_CONSEC: recompute today's return against the stored
                # prior raw close; NULL when unavailable (never mixed bases).
                prev_raw = conn.execute(
                    "SELECT close FROM security_day WHERE listing_id=? AND trade_date<?"
                    " AND close IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
                    (lst["listing_id"], asof_date),
                ).fetchone()
                if prev_raw and r["close"]:
                    try:
                        ret = float(r["close"]) / float(prev_raw["close"]) - 1.0
                        ret_basis = "RAW_CONSEC"
                    except (ValueError, ZeroDivisionError):
                        ret, ret_basis = None, None
                else:
                    ret, ret_basis = None, None
            vol_shares = None
            if r["volume"] is not None:
                try:
                    vol_shares = str(int(float(r["volume"]) * 100))  # lots -> shares
                except ValueError:
                    vol_shares = None
            # SYNTH continuation of the adjusted series: exchange pct is computed
            # against the adjusted prev close, so prev_adj x (1+ret) stays on the
            # listing's current basis epoch. Weekly qfq refresh cross-checks it.
            prev_adj = conn.execute(
                "SELECT adj_close, basis_epoch FROM security_day WHERE listing_id=?"
                " AND adj_close IS NOT NULL AND trade_date<? ORDER BY trade_date DESC LIMIT 1",
                (lst["listing_id"], asof_date),
            ).fetchone()
            adj_close = None
            epoch = 1
            if prev_adj and ret is not None and ret_basis == "EXCHANGE_PCT":
                adj_close = f"{float(prev_adj['adj_close']) * (1.0 + ret):.6f}"
                epoch = prev_adj["basis_epoch"]
            conn.execute(
                "INSERT OR REPLACE INTO security_day(listing_id, trade_date, open, high, low,"
                " close, prev_close, volume, amount, ret, ret_basis, adj_close, basis_epoch,"
                " adj_method, currency, limit_state, provider, quality, manifest_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    lst["listing_id"], asof_date, r["open"], r["high"], r["low"], r["close"],
                    r["prev_close"], vol_shares, r["amount"],
                    ret, ret_basis, adj_close, epoch,
                    "NONE", "CNY",
                    limit_state(pct, lst["board"], bool(is_st)), "eastmoney", PROV, m.manifest_id,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO market_snapshot(listing_id, asof_date, name, total_mcap,"
                " float_mcap, industry, is_st, source, quality, manifest_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    lst["listing_id"], asof_date, r["name"], r["total_mcap"], r["float_mcap"],
                    r["industry"], is_st, "eastmoney", PROV, m.manifest_id,
                ),
            )
            seen += 1
        conn.commit()
        if page * 100 >= total:
            break
        page += 1
    return {"date": asof_date, "rows": seen, "pages": pages}


def backfill_listing(conn, http_by_provider: dict, config_version: str, listing_row,
                     beg: str) -> BackfillResult:
    """Route by exchange: Tencent (SSE/SZSE, qfq) or Sina (BSE, raw). Both are
    PROVISIONAL scan-tier sources; pct_chg is derived from consecutive closes."""
    from investment_tool.providers import sina, tencent

    start = f"{beg[:4]}-{beg[4:6]}-{beg[6:]}"
    if listing_row["exchange"] == "BSE":
        provider, adj = "sina", "RAW_SINA"
        sym = sina.symbol(listing_row["ticker"])
        payload, status, url = sina.fetch_kline(http_by_provider["sina"], sym)
        params = {"symbol": sym}
    else:
        provider, adj = "tencent", "QFQ_TENCENT"
        sym = tencent.symbol(listing_row["exchange"], listing_row["ticker"])
        payload, status, url = tencent.fetch_kline(
            http_by_provider["tencent"], sym, start, "2050-01-01"
        )
        params = {"symbol": sym, "start": start}
    content_ok = status == 200 and payload.lstrip(b"\xef\xbb\xbf").startswith((b"{", b"["))
    if status == 200 and not content_ok:
        quality = Quality(QualityState.ERROR, "CONTENT_INVALID: non-JSON body")
    else:
        quality = Quality(
            QualityState.PROVISIONAL if status == 200 else QualityState.ERROR, f"http={status}"
        )
    m = record_fetch(
        conn, provider=provider, dataset="kline_daily", params=params, source_url=url,
        payload=payload, http_status=status, quality=quality, config_version=config_version,
    )
    if not (status == 200 and content_ok):
        return BackfillResult(0, QualityState.ERROR.value, provider)
    bars = (sina.parse_kline(payload) if provider == "sina"
            else tencent.parse_kline(payload, sym))
    return BackfillResult(
        store_kline_bars(conn, listing_row, provider, adj, bars, m.manifest_id),
        QualityState.PROVISIONAL.value,
        provider,
    )


def store_kline_bars(conn, listing_row, provider: str, adj: str, bars: list[dict],
                     manifest_id: str) -> int:
    """Persist parsed kline bars under basis-aware semantics. Shared by the
    network backfill and the raw-store replay (event-sourced recovery)."""
    if not bars:
        return 0
    lid = listing_row["listing_id"]
    st_row = conn.execute(
        "SELECT is_st FROM market_snapshot WHERE listing_id=? ORDER BY asof_date DESC LIMIT 1",
        (lid,),
    ).fetchone()
    is_st = bool(st_row["is_st"]) if st_row else False

    # Basis-epoch handling (adjusted lineage only): if fetched adjusted closes
    # disagree with stored ones on overlapping dates, the provider rewrote the
    # series after a corporate action -> bump the epoch and clear the old
    # analytical basis before writing the replacement.  Canonical raw
    # snapshot fields must survive this operation: adjusted-history rewrites
    # are not permission to delete a separately sourced raw observation.
    is_adjusted = provider == "tencent"
    epoch_row = conn.execute(
        "SELECT MAX(basis_epoch) AS e FROM security_day WHERE listing_id=?", (lid,)
    ).fetchone()
    epoch = int(epoch_row["e"] or 1)
    if is_adjusted:
        stored = dict(
            conn.execute(
                "SELECT trade_date, adj_close FROM security_day WHERE listing_id=?"
                " AND adj_close IS NOT NULL", (lid,),
            ).fetchall()
        )
        fetched = {b["date"]: b["close"] for b in bars if b.get("close")}
        overlap = set(stored) & set(fetched)
        mismatch = any(
            abs(float(fetched[d]) / float(stored[d]) - 1.0) > 0.001
            for d in overlap if stored[d] not in (None, "", "0")
        )
        if mismatch:
            epoch += 1
            conn.execute(
                "UPDATE security_day SET ret=NULL, ret_basis=NULL, adj_close=NULL,"
                " basis_epoch=? WHERE listing_id=?",
                (epoch, lid),
            )
            conn.execute(
                "INSERT INTO observation(obs_id, kind, listing_id, payload_json,"
                " first_seen_at_utc, state) VALUES(hex(randomblob(8)),"
                " 'corporate_action_detected', ?, ?, ?, 'NEW')",
                (lid, '{"note": "adjusted history rewritten by provider; epoch bumped"}',
                 utc_now()),
            )

    prev = None
    rows = []
    for b in bars:
        ret = None
        if prev not in (None, "", "0") and b["close"] not in (None, ""):
            try:
                ret = float(b["close"]) / float(prev) - 1.0
            except (ValueError, ZeroDivisionError):
                ret = None
        if is_adjusted:
            raw = (None, None, None, None)          # raw OHLC unknown from qfq feed
            adj_close, basis = b["close"], "QFQ_CONSEC"
        else:
            raw = (b["open"], b["high"], b["low"], b["close"])
            adj_close, basis = None, "RAW_CONSEC"
        rows.append(
            (
                lid, b["date"], raw[0], raw[1], raw[2], raw[3], prev if not is_adjusted else None,
                b["volume"], b["amount"],
                ret, basis if ret is not None else None, adj_close, epoch,
                adj, "CNY",
                limit_state(ret * 100.0 if ret is not None else None,
                            listing_row["board"], bool(is_st)),
                provider, PROV, manifest_id,
            )
        )
        prev = b["close"]
    # Upsert that never clobbers canonical raw fields already present from the
    # snapshot path: analytics fields come from this fetch; raw price/amount
    # fields keep their first non-NULL value.
    conn.executemany(
        "INSERT INTO security_day(listing_id, trade_date, open, high, low, close,"
        " prev_close, volume, amount, ret, ret_basis, adj_close, basis_epoch, adj_method,"
        " currency, limit_state, provider, quality, manifest_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(listing_id, trade_date) DO UPDATE SET"
        "  ret=excluded.ret, ret_basis=excluded.ret_basis, adj_close=excluded.adj_close,"
        "  basis_epoch=excluded.basis_epoch, adj_method=excluded.adj_method,"
        "  limit_state=excluded.limit_state, provider=excluded.provider,"
        "  quality=excluded.quality, manifest_id=excluded.manifest_id,"
        "  open=COALESCE(security_day.open, excluded.open),"
        "  high=COALESCE(security_day.high, excluded.high),"
        "  low=COALESCE(security_day.low, excluded.low),"
        "  close=COALESCE(security_day.close, excluded.close),"
        "  prev_close=COALESCE(security_day.prev_close, excluded.prev_close),"
        "  volume=COALESCE(excluded.volume, security_day.volume),"
        "  amount=COALESCE(security_day.amount, excluded.amount)",
        rows,
    )
    conn.commit()
    return len(rows)


def ingest_benchmarks(conn, config_version: str, beg: str) -> dict:
    from investment_tool.calendars import mark_trading_days

    http = eastmoney.client()
    out = {}
    for index_id, sid in eastmoney.BENCHMARKS.items():
        payload, status, url = eastmoney.fetch_kline(http, sid, beg=beg, index=True)
        quality = Quality(
            QualityState.PROVISIONAL if status == 200 else QualityState.ERROR, f"http={status}"
        )
        m = record_fetch(
            conn, provider="eastmoney", dataset="index_kline",
            params={"secid": sid, "beg": beg}, source_url=url, payload=payload,
            http_status=status, quality=quality, config_version=config_version,
        )
        if status != 200:
            out[index_id] = f"error http={status}"
            continue
        bars = eastmoney.parse_kline(payload, index=True)
        conn.executemany(
            "INSERT OR REPLACE INTO benchmark_day(index_id, trade_date, close, provider, quality,"
            " manifest_id) VALUES(?,?,?,?,?,?)",
            [(index_id, b["date"], b["close"], "eastmoney", PROV, m.manifest_id) for b in bars],
        )
        conn.commit()
        out[index_id] = len(bars)
        if index_id == "CSI300":
            # National trading calendar shared by SSE/SZSE/BSE (same holidays).
            dates = [b["date"] for b in bars]
            for ex in ("SSE", "SZSE", "BSE"):
                mark_trading_days(conn, ex, dates, "eastmoney_csi300")
    return out


def default_beg(days_back: int = 420) -> str:
    return (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y%m%d")
