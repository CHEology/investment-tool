"""Temporally versioned CIK<->ticker map and reconciliation against the
Nasdaq-file universe. History is never rewritten: attribute changes close the
open interval (valid_to_date) and append a new row; point-in-time resolution
selects valid_from <= asof < COALESCE(valid_to, '9999').

No fuzzy matching: the only permitted normalizations are case and the
deterministic '.'<->'-' share-class separator variant, each labeled with its
own match state.
"""

from __future__ import annotations

import sqlite3

EXCHANGE_NORMALIZE = {
    "Nasdaq": "NASDAQ", "NASDAQ": "NASDAQ",
    "NYSE": "NYSE",
    "NYSE American": "AMEX", "NYSE MKT": "AMEX",
}


def sync_cik_map(conn: sqlite3.Connection, rows: list[dict], asof_date: str,
                 source: str) -> dict:
    by_cik: dict[str, list[dict]] = {}
    by_ticker: dict[str, set[str]] = {}
    for r in rows:
        by_cik.setdefault(r["cik"], []).append(r)
        by_ticker.setdefault(r["ticker"], set()).add(r["cik"])

    def state_for(r: dict) -> str:
        if len(by_ticker[r["ticker"]]) > 1:
            return "TICKER_CONFLICT"
        if len(by_cik[r["cik"]]) > 1:
            return "MULTI_CLASS"
        return "OK"

    incoming = {(r["cik"], r["ticker"]): r for r in rows}
    open_rows = {
        (r["cik"], r["ticker"]): r
        for r in conn.execute("SELECT * FROM cik_map WHERE valid_to_date IS NULL")
    }
    opened = closed = unchanged = 0
    stale_marked = stale_recovered = 0
    for key, r in incoming.items():
        state = state_for(r)
        cur = open_rows.get(key)
        if cur is not None and cur["stale_since_date"] is not None:
            # reappearance clears a transient-absence suspicion without closing
            conn.execute(
                "UPDATE cik_map SET stale_since_date=NULL, state=? WHERE cik=? AND ticker=?"
                " AND valid_from_date=?",
                (state, cur["cik"], cur["ticker"], cur["valid_from_date"]),
            )
            stale_recovered += 1
            continue
        if cur is not None and (cur["exchange"], cur["name"], cur["state"]) == (
            r["exchange"], r["name"], state
        ):
            unchanged += 1
            continue
        if cur is not None:
            conn.execute(
                "UPDATE cik_map SET valid_to_date=? WHERE cik=? AND ticker=?"
                " AND valid_from_date=?",
                (asof_date, cur["cik"], cur["ticker"], cur["valid_from_date"]),
            )
            closed += 1
        conn.execute(
            "INSERT OR REPLACE INTO cik_map(cik, ticker, exchange, name, state, source,"
            " valid_from_date, valid_to_date, stale_since_date)"
            " VALUES(?,?,?,?,?,?,?,NULL,NULL)",
            (r["cik"], r["ticker"], r["exchange"], r["name"], state, source, asof_date),
        )
        opened += 1
    for key, cur in open_rows.items():
        if key in incoming:
            continue
        # Two-strike absence: a single disappearance from the SEC file marks
        # the mapping STALE_SUSPECTED (interval stays open); only a SECOND
        # sync on a later date with the row still absent closes it. A
        # transient source glitch is never treated as a confirmed delisting.
        if cur["stale_since_date"] is None:
            conn.execute(
                "UPDATE cik_map SET stale_since_date=?, state='STALE_SUSPECTED'"
                " WHERE cik=? AND ticker=? AND valid_from_date=?",
                (asof_date, cur["cik"], cur["ticker"], cur["valid_from_date"]),
            )
            stale_marked += 1
        elif cur["stale_since_date"] < asof_date:
            conn.execute(
                "UPDATE cik_map SET valid_to_date=? WHERE cik=? AND ticker=?"
                " AND valid_from_date=?",
                (asof_date, cur["cik"], cur["ticker"], cur["valid_from_date"]),
            )
            closed += 1
    conn.commit()
    return {"opened": opened, "closed": closed, "unchanged": unchanged,
            "stale_marked": stale_marked, "stale_recovered": stale_recovered,
            "incoming": len(incoming)}


def cik_for(conn: sqlite3.Connection, ticker: str, asof: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM cik_map WHERE ticker=? AND valid_from_date<=?"
        " AND (valid_to_date IS NULL OR valid_to_date>?)",
        (ticker.upper(), asof, asof),
    ).fetchall()


def reconcile(conn: sqlite3.Connection, asof: str) -> dict:
    """Match open SEC map rows to the US listing universe; enrich company.cik
    for unambiguous matches; report every non-match state explicitly."""
    open_rows = conn.execute(
        "SELECT * FROM cik_map WHERE valid_to_date IS NULL"
    ).fetchall()
    listings = {
        (r["ticker"], r["exchange"]): r
        for r in conn.execute(
            "SELECT l.listing_id, l.ticker, l.exchange, l.company_id, c.cik AS existing_cik"
            " FROM listing l JOIN company c ON c.company_id=l.company_id"
            " WHERE l.exchange IN ('NASDAQ','NYSE','AMEX') AND l.status='LISTED'"
        )
    }
    hist: dict[str, int] = {}

    def bump(k):
        hist[k] = hist.get(k, 0) + 1

    matched_listing_ids = set()
    for r in open_rows:
        if r["state"] == "TICKER_CONFLICT":
            # refused before any lookup: a conflicted mapping never matches or
            # enriches, in or out of universe
            bump("TICKER_CONFLICT_UNMATCHED")
            continue
        ex = EXCHANGE_NORMALIZE.get(r["exchange"] or "")
        if ex is None:
            bump("NOT_IN_UNIVERSE_EXCHANGE")  # OTC / no exchange in SEC file
            continue
        key = (r["ticker"], ex)
        alt = (r["ticker"].replace("-", "."), ex)
        row = listings.get(key)
        state = "MATCHED"
        if row is None and alt != key:
            row = listings.get(alt)
            state = "MATCHED_SEPARATOR_VARIANT"
        if row is None:
            bump("NOT_IN_UNIVERSE_TICKER")
            continue
        bump(state)
        matched_listing_ids.add(row["listing_id"])
        if row["existing_cik"] is None:
            conn.execute("UPDATE company SET cik=? WHERE company_id=?",
                         (r["cik"], row["company_id"]))
        elif row["existing_cik"] != r["cik"]:
            bump("CIK_CHANGED_REVIEW")
    for _key, row in listings.items():
        if row["listing_id"] not in matched_listing_ids and row["existing_cik"] is None:
            bump("UNIVERSE_CIK_UNRESOLVED")
    conn.commit()
    hist["universe_listings"] = len(listings)
    hist["sec_open_rows"] = len(open_rows)
    return hist
