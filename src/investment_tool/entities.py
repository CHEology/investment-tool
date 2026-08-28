"""Company-centric entity model and identifier seeds (DESIGN 5.3, 10).

Deterministic IDs keep seeding idempotent:
- A-share: company 'CN:<code>', listing '<EXCHANGE>:<code>'
- US:      company 'US:<ticker>', listing '<EXCHANGE>:<ticker>'
One-company-per-listing at seed time; cross-listing merges (A+H, ADR<->local)
are a later, evidence-driven enrichment, not a seed-time guess.
"""

from __future__ import annotations

import sqlite3

from investment_tool.lineage import utc_now


def seed_a_share(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = utc_now()
    n = 0
    for r in rows:
        company_id = f"CN:{r['code']}"
        listing_id = f"{r['exchange']}:{r['code']}"
        conn.execute(
            "INSERT OR IGNORE INTO company(company_id, name_zh, created_asof) VALUES(?,?,?)",
            (company_id, r["name_zh"], now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO listing(listing_id, company_id, ticker, exchange, board,"
            " currency, cninfo_org_id) VALUES(?,?,?,?,?,?,?)",
            (listing_id, company_id, r["code"], r["exchange"], r["board"], "CNY", r["org_id"]),
        )
        if r.get("name_zh"):
            conn.execute(
                "INSERT OR IGNORE INTO alias(company_id, text, kind) VALUES(?,?,?)",
                (company_id, r["name_zh"], "short_name_zh"),
            )
        n += 1
    conn.commit()
    return n


def seed_us(conn: sqlite3.Connection, rows: list[dict]) -> int:
    now = utc_now()
    n = 0
    for r in rows:
        company_id = f"US:{r['ticker']}"
        listing_id = f"{r['exchange']}:{r['ticker']}"
        conn.execute(
            "INSERT OR IGNORE INTO company(company_id, name_en, created_asof) VALUES(?,?,?)",
            (company_id, r["name_en"], now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO listing(listing_id, company_id, ticker, exchange, currency,"
            " is_adr) VALUES(?,?,?,?,?,?)",
            (listing_id, company_id, r["ticker"], r["exchange"], "USD", r["is_adr"]),
        )
        n += 1
    conn.commit()
    return n


def resolve(conn: sqlite3.Connection, ticker: str) -> list[sqlite3.Row]:
    """Resolve a ticker/code to (company, listing) rows across exchanges."""
    return conn.execute(
        "SELECT c.company_id, c.name_zh, c.name_en, c.cik, l.listing_id, l.ticker, l.exchange,"
        " l.board, l.currency, l.is_adr, l.status"
        " FROM listing l JOIN company c ON c.company_id = l.company_id WHERE l.ticker = ?",
        (ticker.upper(),),
    ).fetchall()
