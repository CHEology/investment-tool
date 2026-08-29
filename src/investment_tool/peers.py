"""Judgment-built peer baskets and peer-relative event residuals (H3/F-L).

SIC groups are often economically wrong (BURL's SIC neighbors are not TJX/
ROST), so baskets are constructed with judgment, and the JUDGMENT IS
RECORDED: every basket stores its composition, the selection rationale, who
set it, and when. The quantitative layer then computes price-based
peer-relative residuals for the case's own event windows — the missing
decomposition that kept the BURL sector question unanswerable.

Price-only by design (peer valuation comparisons need per-peer XBRL and are
explicitly NOT_IMPLEMENTED); every output names its dates and quality."""

from __future__ import annotations

import json
import sqlite3

from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now


def peers_path(case_id: str):
    d = DEFAULT_DATA_DIR / "research" / "cases" / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "peers.json"


def set_basket(conn: sqlite3.Connection, cfg, case_id: str, tickers: list[str],
               *, etf: str | None = None, rationale: str, set_by: str,
               live: bool = False) -> dict:
    """Record the basket (composition + rationale are part of the audit
    trail) and, in live mode, fetch peer/ETF price history."""
    doc = {"case_id": case_id, "tickers": sorted({t.upper() for t in tickers}),
           "etf": etf.upper() if etf else None, "rationale": rationale,
           "set_by": set_by, "set_at_utc": utc_now()}
    peers_path(case_id).write_text(json.dumps(doc, ensure_ascii=False, indent=2))
    fetched = None
    if live:
        from datetime import date, timedelta

        from investment_tool import us_prices
        pairs = {}
        for t in doc["tickers"] + ([doc["etf"]] if doc["etf"] else []):
            row = conn.execute(
                "SELECT listing_id, ticker FROM listing WHERE ticker=?"
                " AND exchange IN ('NASDAQ','NYSE','AMEX') LIMIT 1", (t,)
            ).fetchone()
            pairs[row["listing_id"] if row else f"PEER:{t}"] = t
        start = (date.today() - timedelta(days=400)).isoformat()
        fetched = us_prices.ensure_prices(conn, cfg, pairs, start,
                                          date.today().isoformat())
    return {"peers": doc, "fetched": fetched}


def _series(conn, ticker: str) -> dict[str, float]:
    row = conn.execute(
        "SELECT listing_id FROM listing WHERE ticker=? AND exchange IN"
        " ('NASDAQ','NYSE','AMEX') LIMIT 1", (ticker,)).fetchone()
    lid = row["listing_id"] if row else f"PEER:{ticker}"
    return {r["trade_date"]: float(r["adj_close"]) for r in conn.execute(
        "SELECT trade_date, adj_close FROM security_day WHERE listing_id=?"
        " AND adj_close IS NOT NULL ORDER BY trade_date", (lid,))}


def _window_ret(px: dict[str, float], d0: str, d1: str) -> float | None:
    dates = sorted(px)
    a = next((d for d in reversed(dates) if d <= d0), None)
    b = next((d for d in reversed(dates) if d <= d1), None)
    if a is None or b is None or a == b is None or px.get(a) in (None, 0):
        return None
    if a == b:
        return None
    return px[b] / px[a] - 1.0


def peer_analysis(conn: sqlite3.Connection, case_id: str, rx: dict,
                  asof: str) -> dict:
    """Peer-basket returns over the case's own event windows and the case's
    peer-relative residuals. Windows: pre-event 21 sessions, event session,
    pre-event-close -> asof (cumulative)."""
    path = peers_path(case_id)
    if not path.exists():
        return {"quality": "NO_BASKET",
                "note": "set one via `invest research peers`"}
    doc = json.loads(path.read_text())
    t0 = rx.get("t0_session")
    anchors = rx.get("anchors") or {}
    if not t0:
        return {"quality": "NO_EVENT_SESSION"}
    # dates: pre-event close date comes from the case's own series
    pre_d = None
    ev_sessions = sorted({d for d in _series_dates(conn, case_id)})
    for d in reversed(ev_sessions):
        if d < t0:
            pre_d = d
            break
    if pre_d is None:
        return {"quality": "NO_PRE_EVENT_DATE"}
    pre21_d = ev_sessions[max(0, ev_sessions.index(pre_d) - 21)]
    rows = []
    for t in doc["tickers"]:
        px = _series(conn, t)
        if not px:
            rows.append({"ticker": t, "quality": "NO_PRICES"})
            continue
        rows.append({
            "ticker": t,
            "pre21": _window_ret(px, pre21_d, pre_d),
            "event": _window_ret(px, pre_d, t0),
            "cum_asof": _window_ret(px, pre_d, asof),
            "quality": "OK",
        })
    etf_row = None
    if doc.get("etf"):
        px = _series(conn, doc["etf"])
        if px:
            etf_row = {"ticker": doc["etf"],
                       "pre21": _window_ret(px, pre21_d, pre_d),
                       "event": _window_ret(px, pre_d, t0),
                       "cum_asof": _window_ret(px, pre_d, asof)}
    ok = [r for r in rows if r.get("quality") == "OK"]

    def _median(key):
        vals = sorted(r[key] for r in ok if r.get(key) is not None)
        return vals[len(vals) // 2] if vals else None

    med = {k: _median(k) for k in ("pre21", "event", "cum_asof")}
    out = {
        "basket": doc, "windows": {"pre21_start": pre21_d,
                                   "pre_event_close": pre_d,
                                   "event_session": t0, "asof": asof},
        "peers": rows, "etf": etf_row, "peer_median": med,
        "case_vs_peers": {
            "event_residual":
                (rx.get("post_ret1") - med["event"])
                if rx.get("post_ret1") is not None and med["event"] is not None
                else None,
            "cum_residual_asof":
                (rx.get("post_cum") - med["cum_asof"])
                if rx.get("post_cum") is not None and med["cum_asof"] is not None
                else None,
            "pre21_residual":
                (rx.get("run_up_21") - med["pre21"])
                if rx.get("run_up_21") is not None and med["pre21"] is not None
                else None,
        },
        "quality": "OK" if len(ok) >= 2 else "PARTIAL",
        "note": "price-based only; peer valuation comparison NOT_IMPLEMENTED",
    }
    _ = anchors
    return out


def _series_dates(conn, case_id: str) -> list[str]:
    row = conn.execute(
        "SELECT l.listing_id FROM research_case rc JOIN listing l"
        " ON l.company_id=rc.company_id AND l.exchange IN"
        " ('NASDAQ','NYSE','AMEX') WHERE rc.case_id=? ORDER BY l.listing_id"
        " LIMIT 1", (case_id,)).fetchone()
    if row is None:
        return []
    return [r["trade_date"] for r in conn.execute(
        "SELECT trade_date FROM security_day WHERE listing_id=?"
        " AND adj_close IS NOT NULL ORDER BY trade_date",
        (row["listing_id"],))]
