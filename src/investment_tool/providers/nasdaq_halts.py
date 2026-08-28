"""Nasdaq trade-halt RSS (DISCOVERY role, exchange authority).

Halt model (adversarial-review C2): a halt message is exchange-authoritative
proof THAT a halt occurred and zero evidence about WHY. Volatility pauses
stay observations; regulatory reason codes create a TRADING_SUSPENSION /
TRADING_HALT_NEWS event whose type is the halt itself, never an inferred
underlying cause.

Reason-code map v1 (glossary pinning against Nasdaq's published code list is
a live-gate task; codes here are the conservative, well-known core).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from investment_tool.lineage import utc_now
from investment_tool.providers.base import GENERIC_UA, HttpClient

HALTS_URL = "https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts"

SUSPENSION_CODES = {"H10", "H9"}       # SEC/regulatory suspension: adverse on face
NEWS_PENDING_CODES = {"T12", "T1"}     # news pending/dissemination: sign unknown
# LUDP / T2 / resumptions etc.: observation only.


def client() -> HttpClient:
    return HttpClient(user_agent=GENERIC_UA, min_interval_s=60.0)


def parse_halts(payload: bytes) -> list[dict]:
    text = payload.decode("utf-8", errors="replace")
    out = []
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        def g(tag, item=item):
            m = re.search(rf"<ndaq:{tag}>([^<]*)</ndaq:{tag}>", item)
            return (m.group(1).strip() or None) if m else None

        out.append(
            {
                "symbol": g("IssueSymbol"), "name": g("IssueName"), "market": g("Market"),
                "reason": g("ReasonCode"), "halt_date": g("HaltDate"), "halt_time": g("HaltTime"),
                "resumption": g("ResumptionTradeTime"),
            }
        )
    return [r for r in out if r["symbol"]]


def _obs_id(h: dict) -> str:
    key = f"halt:{h['symbol']}:{h['halt_date']}:{h['halt_time']}:{h['reason']}"
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def route_halts(conn: sqlite3.Connection, halts: list[dict]) -> dict:
    hist: dict[str, int] = {}
    now = utc_now()
    for h in halts:
        listing = conn.execute(
            "SELECT listing_id, company_id FROM listing WHERE ticker=?"
            " AND exchange IN ('NASDAQ','NYSE','AMEX')", (h["symbol"],),
        ).fetchone()
        matched = listing is not None
        payload = {**h, "matched": matched, "telemetry_only": h["reason"] not in
                   (SUSPENSION_CODES | NEWS_PENDING_CODES)}
        conn.execute(
            "INSERT INTO observation(obs_id, kind, listing_id, payload_json,"
            " first_seen_at_utc, state) VALUES(?,?,?,?,?, 'NEW')"
            " ON CONFLICT(obs_id) DO NOTHING",
            (_obs_id(h), "trade_halt", listing["listing_id"] if matched else None,
             json.dumps(payload), now),
        )
        if not matched:
            hist["HALT_UNMATCHED"] = hist.get("HALT_UNMATCHED", 0) + 1
            continue
        if h["reason"] in SUSPENSION_CODES:
            etype, relevance = "TRADING_SUSPENSION", "HARD_NEGATIVE"
        elif h["reason"] in NEWS_PENDING_CODES:
            etype, relevance = "TRADING_HALT_NEWS", "CONTENT_REVIEW_REQUIRED"
        else:
            hist["OBSERVATION_ONLY"] = hist.get("OBSERVATION_ONLY", 0) + 1
            continue
        event_id = "ev_halt_" + _obs_id(h)[:16]
        conn.execute(
            "INSERT OR IGNORE INTO event(event_id, scope, type, published_at_utc,"
            " first_seen_at_utc, state, lane_relevance) VALUES(?, 'COMPANY', ?, NULL, ?,"
            " 'VERIFIED', 'A')",
            (event_id, etype, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO event_company(event_id, company_id) VALUES(?,?)",
            (event_id, listing["company_id"]),
        )
        dims = {"authority": 3, "independence": 3, "directness": 3, "specificity": 2,
                "bindingness": 0, "reproducibility": 1, "freshness": 2}
        conn.execute(
            "INSERT OR IGNORE INTO evidence(evidence_id, event_id, source_url,"
            " publisher_domain, retrieved_at_utc, first_seen_at_utc, retention_class,"
            " excerpt, dims_json) VALUES(?,?,?,?,?,?, 'LINK_ONLY', ?, ?)",
            ("evd_halt_" + _obs_id(h)[:16], event_id, HALTS_URL, "nasdaqtrader.com",
             now, now,
             f"halt {h['reason']} {h['symbol']} {h['halt_date']} {h['halt_time']}"
             " (the halt is the verified fact; the underlying cause is NOT established)",
             json.dumps(dims)),
        )
        hist[relevance] = hist.get(relevance, 0) + 1
    conn.commit()
    return hist
