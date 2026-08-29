"""SEC XBRL companyfacts: point-in-time fundamentals for US cases (H2).

Source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
(official, free; same identity gate and global rate limiter as all SEC
access). Facts are stored with their `filed` date so every query can be
point-in-time: a fact is usable at asof only when filed_date <= asof —
revised figures never leak backwards.

Known approximations (carried as explicit quality notes, never silently):
- dei:EntityCommonStockSharesOutstanding is the cover-page share count;
  multi-class issuers may report per-class rows (we sum same-date classes)
  and ADR ratios are NOT resolved — quality drops to APPROX for those.
- TTM revenue prefers 4 distinct ~quarterly durations; when only an annual
  duration is available the annual value stands in (quality PARTIAL_ANNUAL).
"""

from __future__ import annotations

import json
import sqlite3

from investment_tool.lineage import record_fetch
from investment_tool.quality import Quality, QualityState

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"

REVENUE_TAGS = ("RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues", "RevenueFromContractWithCustomerIncludingAssessedTax")
SHARES_TAGS = ("EntityCommonStockSharesOutstanding",
               "CommonStockSharesOutstanding", "CommonStockSharesIssued",
               "WeightedAverageNumberOfDilutedSharesOutstanding")
KEEP_TAGS = {
    ("dei", "EntityCommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesOutstanding"),
    ("us-gaap", "CommonStockSharesIssued"),
    ("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
    ("us-gaap", "Revenues"),
    ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
    ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"),
    ("us-gaap", "NetIncomeLoss"),
    ("us-gaap", "EarningsPerShareDiluted"),
    ("us-gaap", "StockholdersEquity"),
    ("us-gaap", "Liabilities"),
    ("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
}


def store_companyfacts(conn: sqlite3.Connection, payload: bytes,
                       manifest_id: str) -> int:
    doc = json.loads(payload)
    cik = str(int(doc["cik"]))
    n = 0
    for taxonomy, tags in (doc.get("facts") or {}).items():
        for tag, spec in tags.items():
            if (taxonomy, tag) not in KEEP_TAGS:
                continue
            for unit, rows in (spec.get("units") or {}).items():
                for r in rows:
                    if r.get("end") is None or r.get("val") is None \
                            or r.get("filed") is None:
                        continue
                    conn.execute(
                        "INSERT OR REPLACE INTO xbrl_fact(cik, taxonomy, tag,"
                        " unit, period_start, period_end, value, fy, fp, form,"
                        " accn, filed_date, frame, manifest_id)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cik, taxonomy, tag, unit, r.get("start") or "",
                         r["end"], str(r["val"]), str(r.get("fy") or ""),
                         r.get("fp") or "", r.get("form") or "",
                         r.get("accn") or "", r["filed"], r.get("frame"),
                         manifest_id))
                    n += 1
    conn.commit()
    return n


def fetch_companyfacts(conn: sqlite3.Connection, cfg, http, cik: str) -> dict:
    url = COMPANYFACTS_URL.format(cik10=str(int(cik)).zfill(10))
    resp = http.get(url)
    quality = Quality(QualityState.OK if resp.status_code == 200
                      else QualityState.ERROR, f"http={resp.status_code}")
    m = record_fetch(conn, provider="sec", dataset="companyfacts",
                     params={"cik": cik}, source_url=url, payload=resp.content,
                     http_status=resp.status_code, quality=quality,
                     config_version=cfg.id)
    if resp.status_code != 200:
        return {"error": f"http={resp.status_code}", "manifest": m.manifest_id}
    n = store_companyfacts(conn, resp.content, m.manifest_id)
    return {"facts_stored": n, "manifest": m.manifest_id}


def shares_outstanding(conn: sqlite3.Connection, cik: str, asof: str) -> dict:
    """Grain-correct share count (H2.1/F-A).

    Fact grains are NOT interchangeable and must never be summed across
    grains:
    - INSTANT facts (no period_start) are point-in-time counts — the
      issuer's cover page or balance sheet. Distinct simultaneous instant
      values indicate genuine share classes and are summed
      (APPROX_MULTI_CLASS); identical repeats deduplicate.
    - DURATION facts (weighted-average shares) are averages over a window.
      A quarterly row and a year-to-date row share the same period_end and
      filing — they are ALTERNATIVE views of one share count, so the
      fallback selects the SHORTEST current-period duration and never sums
      (the old sum produced ~1.102B for HRL instead of ~551M).
    """
    from datetime import date as _date

    for tag in SHARES_TAGS:
        rows = conn.execute(
            "SELECT period_start, period_end, value, filed_date FROM xbrl_fact"
            " WHERE cik=? AND tag=? AND filed_date<=?"
            " ORDER BY period_end DESC, filed_date DESC",
            (str(int(cik)), tag, asof)).fetchall()
        if not rows:
            continue
        instants = [r for r in rows if not r["period_start"]]
        if instants:
            latest_end = instants[0]["period_end"]
            same = [r for r in instants if r["period_end"] == latest_end]
            newest_filed = max(r["filed_date"] for r in same)
            values = sorted({float(r["value"]) for r in same
                             if r["filed_date"] == newest_filed})
            total = sum(values)
            quality = "OK" if len(values) == 1 else "APPROX_MULTI_CLASS"
            if tag != "EntityCommonStockSharesOutstanding" and quality == "OK":
                quality = "APPROX_BALANCE_SHEET_TAG"
            return {"value": total, "period_end": latest_end,
                    "filed_date": newest_filed, "tag": tag, "grain": "INSTANT",
                    "class_rows": len(values), "quality": quality}
        # duration fallback: shortest current-period window, never a sum
        latest_end = rows[0]["period_end"]
        same = [r for r in rows if r["period_end"] == latest_end]
        newest_filed = max(r["filed_date"] for r in same)
        cands = [r for r in same if r["filed_date"] == newest_filed]

        def _days(r):
            try:
                return (_date.fromisoformat(r["period_end"])
                        - _date.fromisoformat(r["period_start"])).days
            except ValueError:
                return 10 ** 6
        best = min(cands, key=_days)
        return {"value": float(best["value"]), "period_end": latest_end,
                "filed_date": newest_filed, "tag": tag, "grain": "DURATION",
                "duration_days": _days(best),
                "quality": "APPROX_WEIGHTED_DILUTED"}
    return {"quality": "MISSING", "value": None}


def ttm_revenue(conn: sqlite3.Connection, cik: str, asof: str) -> dict:
    """Sum of the 4 most recent distinct ~quarterly revenue durations filed
    on/before asof; falls back to the latest annual duration."""
    return ttm_value(conn, cik, REVENUE_TAGS, asof)


def ttm_value(conn: sqlite3.Connection, cik: str, tags, asof: str) -> dict:
    """Generic trailing-twelve-month sum over duration facts (see
    ttm_revenue)."""
    for tag in tags:
        rows = conn.execute(
            "SELECT period_start, period_end, value, filed_date, form"
            " FROM xbrl_fact WHERE cik=? AND tag=? AND unit='USD'"
            " AND filed_date<=? AND period_start!=''"
            " ORDER BY period_end DESC, filed_date DESC",
            (str(int(cik)), tag, asof)).fetchall()
        if not rows:
            continue
        from datetime import date

        def _days(r):
            a = date.fromisoformat(r["period_start"])
            b = date.fromisoformat(r["period_end"])
            return (b - a).days

        quarters: dict[str, float] = {}
        for r in rows:
            if 75 <= _days(r) <= 100 and r["period_end"] not in quarters:
                quarters[r["period_end"]] = float(r["value"])
        ends = sorted(quarters, reverse=True)[:4]
        if len(ends) == 4:
            return {"value": sum(quarters[e] for e in ends), "tag": tag,
                    "quarters": ends, "quality": "OK"}
        annual = [r for r in rows if 350 <= _days(r) <= 380]
        if annual:
            r = annual[0]
            return {"value": float(r["value"]), "tag": tag,
                    "period_end": r["period_end"],
                    "quality": "PARTIAL_ANNUAL"}
        if ends:
            return {"value": sum(quarters[e] for e in ends), "tag": tag,
                    "quarters": ends, "quality": f"PARTIAL_{len(ends)}Q"}
    return {"quality": "MISSING", "value": None}


def price_close(conn: sqlite3.Connection, listing_id: str, asof: str) -> float | None:
    r = conn.execute(
        "SELECT COALESCE(close, adj_close) AS c FROM security_day"
        " WHERE listing_id=? AND trade_date<=? AND COALESCE(close, adj_close)"
        " IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
        (listing_id, asof)).fetchone()
    return float(r["c"]) if r else None


def market_cap(conn: sqlite3.Connection, cik: str, listing_id: str,
               asof: str, is_adr: bool = False) -> dict:
    sh = shares_outstanding(conn, cik, asof)
    px = price_close(conn, listing_id, asof)
    if sh["value"] is None or px is None:
        return {"quality": "MISSING", "value": None, "shares": sh}
    q = sh["quality"]
    if is_adr:
        q = "APPROX_ADR"
    return {"value": sh["value"] * px, "price": px, "shares": sh,
            "quality": q}


def adv60(conn: sqlite3.Connection, listing_id: str, asof: str) -> dict:
    rows = conn.execute(
        "SELECT COALESCE(close, adj_close) AS c, volume FROM security_day"
        " WHERE listing_id=? AND trade_date<=? AND volume IS NOT NULL"
        " ORDER BY trade_date DESC LIMIT 60", (listing_id, asof)).fetchall()
    vals = []
    for r in rows:
        try:
            vals.append(float(r["c"]) * float(r["volume"]))
        except (TypeError, ValueError):
            continue
    if len(vals) < 20:
        return {"quality": "MISSING", "value": None, "sessions": len(vals)}
    vals.sort()
    return {"value": vals[len(vals) // 2], "sessions": len(vals),
            "quality": "OK" if len(vals) >= 55 else "PARTIAL"}


def ps_ratio_history(conn: sqlite3.Connection, cik: str, listing_id: str,
                     asof: str) -> dict:
    """Current P/S (TTM) and its percentile against the issuer's own history
    at each historical quarter end (PIT shares and PIT revenue). Needs
    extended price history; quality reflects sample size."""
    ends = [r["period_end"] for r in conn.execute(
        "SELECT DISTINCT period_end FROM xbrl_fact WHERE cik=?"
        " AND tag IN ({}) AND filed_date<=?"
        " ORDER BY period_end".format(",".join("?" * len(SHARES_TAGS))),
        (str(int(cik)), *SHARES_TAGS, asof))]
    history = []
    for end in ends:
        rev = ttm_revenue(conn, cik, end)
        mc = market_cap(conn, cik, listing_id, end)
        if rev["value"] and mc["value"] and float(rev["value"]) > 0:
            history.append({"date": end, "ps": mc["value"] / float(rev["value"])})
    rev_now = ttm_revenue(conn, cik, asof)
    mc_now = market_cap(conn, cik, listing_id, asof)
    if not (rev_now["value"] and mc_now["value"] and float(rev_now["value"]) > 0):
        return {"quality": "MISSING", "current_ps": None}
    ps_now = mc_now["value"] / float(rev_now["value"])
    below = sum(1 for h in history if h["ps"] <= ps_now)
    pct = (below / len(history) * 100) if history else None
    return {"current_ps": ps_now, "history_points": len(history),
            "percentile_vs_history": pct,
            "history": history[-12:],
            "quality": ("OK" if len(history) >= 8 else
                        "PARTIAL" if history else "NO_HISTORY")}
