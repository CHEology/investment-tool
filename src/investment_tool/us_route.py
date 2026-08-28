"""Deterministic US filing routing (classification version us_v1).

Detection (form + SEC item codes) is separated from interpretation: only the
adverse-on-face set creates HARD_NEGATIVE events; content-dependent classes go
to the review queue; bulk-metadata forms stay reference-only so the queue
cannot flood (verified daily volumes: Form 4 ~870/day, 6-K ~103/day).

Evidence independence is derived from the filing party's role relative to the
company the event concerns: ISSUER->0, INSIDER->1, third-party FILER about a
SUBJECT->2, regulator/exchange->3. An issuer filing can never be independent
confirmation of its own claims; a 13D can be, for the subject company.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

from investment_tool.lineage import utc_now

CLASSIFICATION_VERSION = "us_v1"

HARD_ITEMS = {"4.02": "NON_RELIANCE", "1.03": "BANKRUPTCY", "3.01": "DELISTING_NOTICE"}
REVIEW_ITEMS = {"2.02", "2.03", "2.04", "5.02", "7.01", "8.01", "1.01", "2.01", "4.01"}
HARD_FORMS = {"NT 10-K": "LATE_FILING", "NT 10-Q": "LATE_FILING",
              "25": "DELISTING", "25-NSE": "DELISTING"}
REVIEW_FORMS = {"SC 13D": "STAKE_ACTIVIST", "SC 13D/A": "STAKE_ACTIVIST"}
OBSERVATION_FORMS = {"6-K", "6-K/A", "10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A"}
REFERENCE_FORMS = {"3", "4", "5", "SC 13G", "SC 13G/A", "DEF 14A", "S-1", "S-3", "F-1", "F-3"}


def classify(form: str, items_csv: str | None) -> tuple[str, str | None, str]:
    """Returns (relevance, event_type|None, action) — action in
    {EVENT, REVIEW_EVENT, OBSERVATION, REFERENCE}."""
    items = [i.strip() for i in (items_csv or "").split(",") if i.strip()]
    base = form[:-2] if form.endswith("/A") else form
    if base == "8-K":
        hard = [it for it in items if it in HARD_ITEMS]
        if hard:
            return "HARD_NEGATIVE", HARD_ITEMS[hard[0]], "EVENT"
        if any(it in REVIEW_ITEMS for it in items):
            return "CONTENT_REVIEW_REQUIRED", "ISSUER_8K", "REVIEW_EVENT"
        return "NEUTRAL", None, "OBSERVATION"  # items unknown or unlisted
    if form in HARD_FORMS:
        return "HARD_NEGATIVE", HARD_FORMS[form], "EVENT"
    if form in REVIEW_FORMS:
        return "CONTENT_REVIEW_REQUIRED", REVIEW_FORMS[form], "REVIEW_EVENT"
    if form in OBSERVATION_FORMS:
        return "NEUTRAL", None, "OBSERVATION"
    if form in REFERENCE_FORMS:
        return "NEUTRAL", None, "REFERENCE"
    return "NEUTRAL", None, "REFERENCE"


def _independence(roles: set[str], event_concerns_subject: bool) -> tuple[int, str]:
    if event_concerns_subject and "FILER" in roles:
        return 2, "third-party filer about subject company"
    if "INSIDER" in roles:
        return 1, "issuer-adjacent insider"
    if roles == {"UNKNOWN_INDEX"} or not roles:
        return 0, "PARTY_ROLES_UNRESOLVED (conservative floor)"
    return 0, "issuer-primary"


def _event_id(accession: str, event_type: str) -> str:
    return "ev_us_" + hashlib.sha256(f"{accession}|{event_type}".encode()).hexdigest()[:16]


def route_unclassified(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT * FROM sec_filing WHERE classification_version IS NULL"
    ).fetchall()
    hist: dict[str, int] = {}
    for f in rows:
        base = f["form"][:-2] if f["form"].endswith("/A") else f["form"]
        if base == "8-K" and f["items_csv"] is None:
            # Staged classification: an 8-K without item codes is NOT neutral —
            # it is unclassifiable. It stays pending until enrichment arrives
            # (a silently-neutral material 8-K was the failure mode here).
            hist["PENDING_ITEMS"] = hist.get("PENDING_ITEMS", 0) + 1
            continue
        relevance, event_type, action = classify(f["form"], f["items_csv"])
        hist[action] = hist.get(action, 0) + 1
        event_id = None
        if action in ("EVENT", "REVIEW_EVENT") and event_type:
            event_id = _create_event(conn, f, event_type, relevance)
        conn.execute(
            "UPDATE sec_filing SET classification_version=?, relevance=?, event_id=?"
            " WHERE accession=?",
            (CLASSIFICATION_VERSION, relevance, event_id, f["accession"]),
        )
    conn.commit()
    return hist


def _create_event(conn, filing: sqlite3.Row, event_type: str, relevance: str) -> str:
    event_id = _event_id(filing["accession"], event_type)
    published = filing["accepted_at_utc"] or (
        f"{filing['filing_date']}T00:00:00Z" if filing["filing_date"] else None
    )
    conn.execute(
        "INSERT OR IGNORE INTO event(event_id, scope, type, published_at_utc,"
        " first_seen_at_utc, state, lane_relevance)"
        " VALUES(?, 'COMPANY', ?, ?, ?, 'VERIFIED', ?)",
        (event_id, event_type, published, filing["first_seen_at_utc"],
         "A" if relevance == "HARD_NEGATIVE" else "A/B"),
    )
    roles = {
        r["role"] for r in conn.execute(
            "SELECT role FROM filing_party WHERE accession=?", (filing["accession"],)
        )
    }
    subjectish = event_type == "STAKE_ACTIVIST"
    indep, indep_note = _independence(roles, subjectish)
    dims = {"authority": 2, "independence": indep, "directness": 3, "specificity": 2,
            "bindingness": 0, "reproducibility": 1, "freshness": 2}
    conn.execute(
        "INSERT OR IGNORE INTO evidence(evidence_id, event_id, source_url, publisher_domain,"
        " published_at_utc, retrieved_at_utc, first_seen_at_utc, retention_class, excerpt,"
        " dims_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "evd_us_" + filing["accession"], event_id,
            filing["primary_doc_url"] or f"edgar:accession:{filing['accession']}",
            "sec.gov", published, utc_now(), filing["first_seen_at_utc"], "LINK_ONLY",
            f"{filing['form']} items={filing['items_csv'] or '?'} ({indep_note})",
            json.dumps(dims),
        ),
    )
    # company linkage where the CIK resolves (issuer forms); unresolved stays visible
    company = conn.execute(
        "SELECT c.company_id FROM company c JOIN listing l ON l.company_id=c.company_id"
        " WHERE c.cik=? LIMIT 1", (filing["cik"],),
    ).fetchone()
    if company:
        conn.execute(
            "INSERT OR IGNORE INTO event_company(event_id, company_id) VALUES(?,?)",
            (event_id, company["company_id"]),
        )
    return event_id


def link_amendments(conn: sqlite3.Connection) -> dict:
    """Deterministic-only linkage: unique (cik, base form, report_period) match.
    Anything else is AMBIGUOUS or UNLINKED — never guessed."""
    hist = {"LINKED_UNIQUE": 0, "AMBIGUOUS": 0, "UNLINKED": 0}
    rows = conn.execute(
        "SELECT * FROM sec_filing WHERE is_amendment=1 AND amend_link_state IS NULL"
    ).fetchall()
    for a in rows:
        base = a["form"][:-2]
        if not a["report_period"]:
            state, target = "UNLINKED", None
        else:
            originals = conn.execute(
                "SELECT accession FROM sec_filing WHERE cik=? AND form=? AND report_period=?"
                " AND is_amendment=0", (a["cik"], base, a["report_period"]),
            ).fetchall()
            if len(originals) == 1:
                state, target = "LINKED_UNIQUE", originals[0]["accession"]
            elif len(originals) > 1:
                state, target = "AMBIGUOUS", None
            else:
                state, target = "UNLINKED", None
        conn.execute(
            "UPDATE sec_filing SET amend_link_state=?, amends_accession=? WHERE accession=?",
            (state, target, a["accession"]),
        )
        if target:
            conn.execute(
                "UPDATE sec_filing SET supersession_state='AMENDED_BY' WHERE accession=?"
                " AND supersession_state='ACTIVE'", (target,),
            )
            conn.execute(
                "UPDATE evidence SET contradiction_state='SUPERSEDED'"
                " WHERE evidence_id=? AND contradiction_state='UNCONTESTED'",
                ("evd_us_" + target,),
            )
        hist[state] += 1
    conn.commit()
    return hist


def eligible_session_us(accepted_at_utc: str | None, filing_date: str | None) -> dict:
    """US temporal eligibility from acceptance time (second precision when
    enriched). RTH acceptance -> same ET session (t0_partial); after-hours ->
    next calendar day (session resolution against a US calendar arrives with
    the price slice). Date-only fallback carries explicit DATE precision."""
    if accepted_at_utc:
        dt = datetime.strptime(accepted_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        et = dt - timedelta(hours=4)  # EDT; refined with a proper tz map at the price slice
        if et.hour < 16:
            return {"eligible_from_date": et.strftime("%Y-%m-%d"), "precision": "TIME",
                    "same_session_partial": True}
        return {"eligible_from_date": (et + timedelta(days=1)).strftime("%Y-%m-%d"),
                "precision": "TIME", "same_session_partial": False}
    if filing_date:
        return {"eligible_from_date": filing_date, "precision": "DATE",
                "same_session_partial": None}
    return {"eligible_from_date": None, "precision": "UNKNOWN", "same_session_partial": None}
