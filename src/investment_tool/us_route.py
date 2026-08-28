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
    if event_concerns_subject and "FILER" in roles and "SUBJECT" in roles:
        return 2, ("independent for the filer's OWN ownership/stake/intent claim;"
                   " NOT for claims about the subject company's operations")
    if "INSIDER" in roles:
        return 1, "issuer-adjacent insider"
    if roles <= {"UNKNOWN_INDEX"} or not roles:
        return 0, "PARTY_ROLES_UNRESOLVED (conservative floor)"
    return 0, "issuer-primary"


def resolve_subject_roles(conn: sqlite3.Connection, accession: str) -> str | None:
    """Subject-form filings are indexed under both the reporting person and the
    subject issuer. With exactly two known parties and one FILER (e.g. from the
    getcurrent '(Filed by)' hint), the other is deterministically the SUBJECT.
    Anything else stays unresolved — never guessed."""
    parties = conn.execute(
        "SELECT cik, role FROM filing_party WHERE accession=?", (accession,)
    ).fetchall()
    subject = next((p["cik"] for p in parties if p["role"] == "SUBJECT"), None)
    if subject:
        return subject
    ciks = {p["cik"] for p in parties}
    filers = {p["cik"] for p in parties if p["role"] == "FILER"}
    if len(ciks) == 2 and len(filers) == 1:
        subject = next(iter(ciks - filers))
        conn.execute(
            "UPDATE filing_party SET role='SUBJECT' WHERE accession=? AND cik=?"
            " AND role='UNKNOWN_INDEX'", (accession, subject),
        )
        return subject
    return None


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
    # Publication time is the SEC acceptance time or nothing. A filing DATE is
    # date-precision information (kept on sec_filing); it is never dressed up
    # as a midnight timestamp. first_seen governs replay either way.
    published = filing["accepted_at_utc"]
    conn.execute(
        "INSERT OR IGNORE INTO event(event_id, scope, type, published_at_utc,"
        " first_seen_at_utc, state, lane_relevance)"
        " VALUES(?, 'COMPANY', ?, ?, ?, 'VERIFIED', ?)",
        (event_id, event_type, published, filing["first_seen_at_utc"],
         "A" if relevance == "HARD_NEGATIVE" else "A/B"),
    )
    subjectish = event_type == "STAKE_ACTIVIST"
    subject_cik = resolve_subject_roles(conn, filing["accession"]) if subjectish else None
    roles = {
        r["role"] for r in conn.execute(
            "SELECT role FROM filing_party WHERE accession=?", (filing["accession"],)
        )
    }
    indep, indep_note = _independence(roles, subjectish)
    if subjectish and subject_cik is None:
        indep_note += "; SUBJECT_UNRESOLVED (no company linked)"
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
    # Company linkage: subject-form events belong to the SUBJECT company, never
    # to whichever index row arrived first; issuer forms link the filing CIK.
    # Unresolved subjects stay unlinked and visible.
    link_cik = subject_cik if subjectish else filing["cik"]
    company = None
    if link_cik is not None:
        company = conn.execute(
            "SELECT c.company_id FROM company c JOIN listing l ON l.company_id=c.company_id"
            " WHERE c.cik=? ORDER BY c.company_id LIMIT 1", (link_cik,),
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
        "SELECT * FROM sec_filing WHERE is_amendment=1"
        " AND (amend_link_state IS NULL OR amend_link_state='UNLINKED')"
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


def _next_weekday(d):
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d = d + timedelta(days=1)
    return d


def eligible_session_us(accepted_at_utc: str | None, filing_date: str | None) -> dict:
    """US temporal eligibility from acceptance time, in real America/New_York
    time (DST-correct via zoneinfo — never a hard-coded offset). Weekends roll
    to the next weekday. US holidays and early closes are NOT yet modeled
    (explicit session_calendar flag); a true session calendar arrives with the
    US price slice. Replay visibility remains governed by first_seen_at_utc.
    Same-session eligibility is flagged partial and must never be mixed with
    full-session return windows."""
    from zoneinfo import ZoneInfo

    calendar_note = "WEEKDAY_APPROX_NO_HOLIDAYS"
    if accepted_at_utc:
        dt = datetime.strptime(accepted_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        et = dt.astimezone(ZoneInfo("America/New_York"))
        if et.weekday() >= 5:
            d = _next_weekday(et.date())
            return {"eligible_from_date": d.isoformat(), "precision": "TIME",
                    "same_session_partial": False, "session_calendar": calendar_note}
        if et.hour < 16:
            return {"eligible_from_date": et.date().isoformat(), "precision": "TIME",
                    "same_session_partial": True, "session_calendar": calendar_note}
        return {"eligible_from_date": _next_weekday(et.date()).isoformat(),
                "precision": "TIME", "same_session_partial": False,
                "session_calendar": calendar_note}
    if filing_date:
        return {"eligible_from_date": filing_date, "precision": "DATE",
                "same_session_partial": None, "session_calendar": calendar_note}
    return {"eligible_from_date": None, "precision": "UNKNOWN",
            "same_session_partial": None, "session_calendar": calendar_note}


def propagate_enrichment(conn: sqlite3.Connection) -> dict:
    """Late-arriving enrichment must reach derived records (idempotent):
    - acceptance times update linked event/evidence publication timestamps;
    - fabricated legacy midnight timestamps are cleared when no acceptance
      time exists (honest NULL rather than false precision);
    - STAKE_ACTIVIST events gain their subject-company link (and corrected
      evidence independence) when party roles become resolvable;
    - issuer events gain company links when a CIK later resolves via us-map.
    """
    out = {"timestamps_updated": 0, "midnight_cleared": 0,
           "subjects_resolved": 0, "issuer_links_added": 0}

    for f in conn.execute(
        "SELECT f.accession, f.event_id, f.accepted_at_utc FROM sec_filing f"
        " JOIN event e ON e.event_id=f.event_id"
        " WHERE f.accepted_at_utc IS NOT NULL"
        " AND (e.published_at_utc IS NULL OR e.published_at_utc != f.accepted_at_utc)"
    ).fetchall():
        conn.execute("UPDATE event SET published_at_utc=? WHERE event_id=?",
                     (f["accepted_at_utc"], f["event_id"]))
        conn.execute("UPDATE evidence SET published_at_utc=? WHERE evidence_id=?",
                     (f["accepted_at_utc"], "evd_us_" + f["accession"]))
        out["timestamps_updated"] += 1

    for f in conn.execute(
        "SELECT f.accession, f.event_id FROM sec_filing f"
        " JOIN event e ON e.event_id=f.event_id"
        " WHERE f.accepted_at_utc IS NULL AND e.published_at_utc LIKE '%T00:00:00Z'"
    ).fetchall():
        conn.execute("UPDATE event SET published_at_utc=NULL WHERE event_id=?",
                     (f["event_id"],))
        conn.execute("UPDATE evidence SET published_at_utc=NULL WHERE evidence_id=?",
                     ("evd_us_" + f["accession"],))
        out["midnight_cleared"] += 1

    for f in conn.execute(
        "SELECT f.* FROM sec_filing f JOIN event e ON e.event_id=f.event_id"
        " WHERE e.type='STAKE_ACTIVIST' AND NOT EXISTS"
        " (SELECT 1 FROM event_company ec WHERE ec.event_id=e.event_id)"
    ).fetchall():
        subject_cik = resolve_subject_roles(conn, f["accession"])
        if subject_cik is None:
            continue
        company = conn.execute(
            "SELECT c.company_id FROM company c JOIN listing l ON l.company_id=c.company_id"
            " WHERE c.cik=? ORDER BY c.company_id LIMIT 1", (subject_cik,),
        ).fetchone()
        if company is None:
            continue
        conn.execute("INSERT OR IGNORE INTO event_company(event_id, company_id) VALUES(?,?)",
                     (f["event_id"], company["company_id"]))
        roles = {r["role"] for r in conn.execute(
            "SELECT role FROM filing_party WHERE accession=?", (f["accession"],))}
        indep, note = _independence(roles, True)
        conn.execute(
            "UPDATE evidence SET dims_json=json_set(dims_json,'$.independence',?),"
            " excerpt=? WHERE evidence_id=?",
            (indep, f"{f['form']} items={f['items_csv'] or '?'} ({note})",
             "evd_us_" + f["accession"]),
        )
        out["subjects_resolved"] += 1

    for f in conn.execute(
        "SELECT f.accession, f.event_id, f.cik FROM sec_filing f"
        " JOIN event e ON e.event_id=f.event_id"
        " WHERE e.type != 'STAKE_ACTIVIST' AND NOT EXISTS"
        " (SELECT 1 FROM event_company ec WHERE ec.event_id=e.event_id)"
    ).fetchall():
        company = conn.execute(
            "SELECT c.company_id FROM company c JOIN listing l ON l.company_id=c.company_id"
            " WHERE c.cik=? ORDER BY c.company_id LIMIT 1", (f["cik"],),
        ).fetchone()
        if company:
            conn.execute(
                "INSERT OR IGNORE INTO event_company(event_id, company_id) VALUES(?,?)",
                (f["event_id"], company["company_id"]))
            out["issuer_links_added"] += 1

    conn.commit()
    return out
