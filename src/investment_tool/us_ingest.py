"""US filing ingestion: daily-index completeness channel + lossy getcurrent
freshness channel + items/acceptance enrichment.

Channel semantics (verified by probe): the daily index for day D does not
exist intraday — it is the evening completeness artifact; getcurrent is
best-effort and demonstrably lossy. Audits therefore carry an explicit
us_completeness state; nothing pretends same-day completeness before the
index lands. All writes for a batch commit in ONE transaction together with
the source checkpoint (crash-safe resume).
"""

from __future__ import annotations

import sqlite3

from investment_tool.lineage import utc_now
from investment_tool.providers import sec

# Forms normalized into sec_filing. The raw index is preserved whole in the
# raw store; fund/structured-product noise (NPORT-P, N-PX, 424B2 shelf spam —
# the verified volume majority) stays out of the normalized table.
FORM_ALLOWLIST = {
    "8-K", "8-K/A", "6-K", "6-K/A", "20-F", "20-F/A", "10-K", "10-K/A", "10-Q", "10-Q/A",
    "NT 10-K", "NT 10-Q", "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
    "3", "4", "5", "S-1", "S-3", "F-1", "F-3", "DEF 14A", "25", "25-NSE",
}


def _upsert_filing(conn: sqlite3.Connection, row: dict, first_seen: str, quality: str,
                   manifest_id: str) -> None:
    """Idempotent: INSERT new accessions; on conflict fill only NULL fields so
    the earliest first_seen_at_utc always survives (lookahead protection)."""
    conn.execute(
        "INSERT INTO sec_filing(accession, cik, form, is_amendment, filing_date,"
        " first_seen_at_utc, quality, manifest_id)"
        " VALUES(?,?,?,?,?,?,?,?)"
        " ON CONFLICT(accession) DO UPDATE SET"
        "  filing_date=COALESCE(sec_filing.filing_date, excluded.filing_date),"
        "  quality=excluded.quality, manifest_id=sec_filing.manifest_id",
        (
            row["accession"], row["cik"], row["form"],
            1 if row["form"].endswith("/A") else 0,
            row.get("filing_date"), first_seen, quality, manifest_id,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO filing_party(accession, cik, role) VALUES(?,?,?)",
        (row["accession"], row["cik"], row.get("role", "UNKNOWN_INDEX")),
    )


def ingest_daily_index(conn: sqlite3.Connection, payload: bytes, index_date: str,
                       manifest_id: str) -> dict:
    """Completeness channel. One transaction: rows + checkpoint together."""
    rows = sec.parse_master_idx(payload)
    kept = 0
    seen = utc_now()
    try:
        conn.execute("BEGIN")
        for r in rows:
            if r["form"] not in FORM_ALLOWLIST:
                continue
            _upsert_filing(conn, r, seen, "OK", manifest_id)
            kept += 1
        conn.execute(
            "INSERT INTO source_checkpoint(source_id, cursor, updated_at_utc)"
            " VALUES('sec_daily_index', ?, ?)"
            " ON CONFLICT(source_id) DO UPDATE SET cursor=excluded.cursor,"
            " updated_at_utc=excluded.updated_at_utc",
            (index_date, seen),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"index_date": index_date, "total_rows": len(rows), "normalized": kept,
            "us_completeness": f"INDEX_RECONCILED_AS_OF({index_date})"}


def poll_getcurrent(conn: sqlite3.Connection, payload: bytes, manifest_id: str) -> dict:
    """Freshness channel: BEST_EFFORT only — lossy by evidence; dedupe by
    accession; completeness is still PENDING until the evening index."""
    entries = sec.parse_getcurrent_atom(payload)
    seen = utc_now()
    kept = 0
    for e in entries:
        if not e.get("form") or e["form"] not in FORM_ALLOWLIST or not e.get("cik"):
            continue
        role = "FILER" if e.get("role_hint") == "Filed by" else "UNKNOWN_INDEX"
        _upsert_filing(
            conn,
            {"accession": e["accession"], "cik": e["cik"], "form": e["form"],
             "filing_date": (e.get("updated") or "")[:10] or None, "role": role},
            seen, "OK", manifest_id,
        )
        kept += 1
    conn.commit()
    return {"entries": len(entries), "normalized": kept, "us_completeness": "PENDING_EVENING_INDEX"}


def _reclassify_if_item_arrived(conn: sqlite3.Connection, accession: str) -> None:
    """A row classified while items were unknown could only have been routed
    NEUTRAL/OBSERVATION (no event). When items arrive later, clear the
    classification so deterministic re-routing runs. Rows classified WITH
    items never change (enrichment fills NULLs only)."""
    conn.execute(
        "UPDATE sec_filing SET classification_version=NULL, relevance=NULL, event_id=NULL"
        " WHERE accession=? AND items_csv IS NOT NULL AND classification_version IS NOT NULL"
        " AND event_id IS NULL AND form LIKE '8-K%'",
        (accession,),
    )


def enrich_items_from_efts(conn: sqlite3.Connection, payload: bytes) -> int:
    items_by_acc = sec.parse_efts_items(payload)
    n = 0
    for acc, rec in items_by_acc.items():
        cur = conn.execute(
            "UPDATE sec_filing SET items_csv=COALESCE(items_csv, ?) WHERE accession=?",
            (",".join(rec["items"]) if rec["items"] else None, acc),
        )
        if cur.rowcount:
            _reclassify_if_item_arrived(conn, acc)
        n += cur.rowcount
    conn.commit()
    return n


def enrich_from_submissions(conn: sqlite3.Connection, payload: bytes) -> int:
    """Fill acceptance time (second precision, UTC — verified), report period,
    items fallback, and primary document for known accessions."""
    n = 0
    for r in sec.parse_submissions_recent(payload):
        acc = r.get("accessionNumber")
        if not acc:
            continue
        cur = conn.execute(
            "UPDATE sec_filing SET"
            "  accepted_at_utc=COALESCE(accepted_at_utc, ?),"
            "  report_period=COALESCE(report_period, ?),"
            "  items_csv=COALESCE(items_csv, ?),"
            "  primary_doc_name=COALESCE(primary_doc_name, ?)"
            " WHERE accession=?",
            (
                (r.get("acceptanceDateTime") or "").replace(".000Z", "Z") or None,
                r.get("reportDate") or None,
                r.get("items") or None,
                r.get("primaryDocument") or None,
                acc,
            ),
        )
        if cur.rowcount:
            _reclassify_if_item_arrived(conn, acc)
        n += cur.rowcount
    conn.commit()
    return n


def visible_filings(conn: sqlite3.Connection, cutoff_utc: str) -> list[sqlite3.Row]:
    """Lookahead protection: replays may only see filings first seen at or
    before their cutoff."""
    return conn.execute(
        "SELECT * FROM sec_filing WHERE first_seen_at_utc<=? ORDER BY first_seen_at_utc",
        (cutoff_utc,),
    ).fetchall()


def mark_removal_suspected(conn: sqlite3.Connection, missing_accessions: list[str]) -> int:
    n = 0
    for acc in missing_accessions:
        cur = conn.execute(
            "UPDATE sec_filing SET supersession_state='REMOVAL_SUSPECTED'"
            " WHERE accession=? AND supersession_state='ACTIVE'", (acc,),
        )
        n += cur.rowcount
    conn.commit()
    return n


def confirm_removal(conn: sqlite3.Connection, accession: str, http_status: int) -> str:
    """Two-step removal: absence alone is never proof. 404/410 on the direct
    accession fetch confirms; anything else restores ACTIVE with an anomaly
    observation."""
    if http_status in (404, 410):
        conn.execute(
            "UPDATE sec_filing SET supersession_state='REMOVED' WHERE accession=?", (accession,)
        )
        conn.execute(
            "UPDATE evidence SET contradiction_state='WITHDRAWN'"
            " WHERE event_id IN (SELECT event_id FROM sec_filing WHERE accession=?)",
            (accession,),
        )
        state = "REMOVED"
    else:
        conn.execute(
            "UPDATE sec_filing SET supersession_state='ACTIVE' WHERE accession=?", (accession,)
        )
        import json as json_mod
        import uuid

        conn.execute(
            "INSERT INTO observation(obs_id, kind, payload_json, first_seen_at_utc, state)"
            " VALUES(?,?,?,?, 'NEW')",
            (uuid.uuid4().hex, "removal_reconciliation_anomaly",
             json_mod.dumps({"accession": accession, "http_status": http_status}), utc_now()),
        )
        state = "ACTIVE"
    conn.commit()
    return state
