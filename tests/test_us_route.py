from pathlib import Path

from investment_tool import us_ingest, us_route

FIX = Path(__file__).parent / "fixtures" / "sec"


def _pipeline(conn):
    us_ingest.ingest_daily_index(conn, (FIX / "master_sample.idx").read_bytes(),
                                 "2026-08-27", "m_idx")
    us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
    us_ingest.enrich_from_submissions(conn, (FIX / "submissions_sample.json").read_bytes())
    return us_route.route_unclassified(conn)


def test_classify_table():
    assert us_route.classify("8-K", "4.02,9.01") == ("HARD_NEGATIVE", "NON_RELIANCE", "EVENT")
    assert us_route.classify("8-K", "1.03") == ("HARD_NEGATIVE", "BANKRUPTCY", "EVENT")
    assert us_route.classify("8-K", "2.02,9.01")[0] == "CONTENT_REVIEW_REQUIRED"
    assert us_route.classify("8-K", None) == ("NEUTRAL", None, "OBSERVATION")
    assert us_route.classify("NT 10-K", None) == ("HARD_NEGATIVE", "LATE_FILING", "EVENT")
    assert us_route.classify("SC 13D", None)[2] == "REVIEW_EVENT"
    assert us_route.classify("SC 13G", None)[2] == "REFERENCE"   # passive stake: no queue
    assert us_route.classify("6-K", None)[2] == "OBSERVATION"    # queue-flood protection
    assert us_route.classify("4", None)[2] == "REFERENCE"
    assert us_route.classify("DEF 14A", None)[2] == "REFERENCE"


def test_route_creates_events_and_review_queue(conn):
    hist = _pipeline(conn)
    assert hist["EVENT"] >= 3          # 4.02 8-K, 1.03 8-K, NT 10-K (+ 8-K/A 4.02)
    assert hist["REVIEW_EVENT"] >= 2   # 2.02 8-K, SC 13D
    events = {r["type"] for r in conn.execute("SELECT type FROM event")}
    assert {"NON_RELIANCE", "BANKRUPTCY", "LATE_FILING", "ISSUER_8K", "STAKE_ACTIVIST"} <= events
    # published_at prefers second-precision acceptance when enriched
    ev = conn.execute(
        "SELECT e.published_at_utc FROM event e JOIN sec_filing f ON f.event_id=e.event_id"
        " WHERE f.accession='0001000001-26-000102'").fetchone()
    assert ev["published_at_utc"] == "2026-08-27T18:02:11Z"


def test_route_idempotent(conn):
    _pipeline(conn)
    n1 = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    us_route.route_unclassified(conn)  # nothing left unclassified
    assert conn.execute("SELECT COUNT(*) FROM event").fetchone()[0] == n1


def test_independence_role_derived(conn):
    _pipeline(conn)
    # SC 13D from the index alone: party roles unresolved -> conservative 0
    import json
    ev13d = conn.execute(
        "SELECT ev.dims_json, ev.excerpt FROM evidence ev JOIN sec_filing f"
        " ON ev.event_id=f.event_id WHERE f.form='SC 13D'").fetchone()
    assert json.loads(ev13d["dims_json"])["independence"] == 0
    assert "PARTY_ROLES_UNRESOLVED" in ev13d["excerpt"]
    # with a FILER role hint (atom channel), a subject-stake event reaches 2
    conn.execute("INSERT OR IGNORE INTO filing_party(accession, cik, role)"
                 " VALUES('0001000004-26-000401','1000004','FILER')")
    conn.execute("DELETE FROM evidence WHERE evidence_id='evd_us_0001000004-26-000401'")
    conn.execute("UPDATE sec_filing SET classification_version=NULL, event_id=NULL"
                 " WHERE accession='0001000004-26-000401'")
    conn.commit()
    us_route.route_unclassified(conn)
    ev = conn.execute("SELECT dims_json FROM evidence"
                      " WHERE evidence_id='evd_us_0001000004-26-000401'").fetchone()
    assert json.loads(ev["dims_json"])["independence"] == 2


def test_amendment_linkage_states(conn):
    _pipeline(conn)
    hist = us_route.link_amendments(conn)
    # 8-K/A reportDate 2026-08-20 matches exactly one original (…101; …102 is 08-25)
    assert hist["LINKED_UNIQUE"] == 1
    row = conn.execute("SELECT amends_accession, amend_link_state FROM sec_filing"
                       " WHERE accession='0001000001-26-000103'").fetchone()
    assert row["amends_accession"] == "0001000001-26-000101"
    orig = conn.execute("SELECT supersession_state FROM sec_filing"
                        " WHERE accession='0001000001-26-000101'").fetchone()
    assert orig["supersession_state"] == "AMENDED_BY"
    evd = conn.execute("SELECT contradiction_state FROM evidence"
                       " WHERE evidence_id='evd_us_0001000001-26-000101'").fetchone()
    assert evd["contradiction_state"] == "SUPERSEDED"


def test_amendment_ambiguous_when_two_originals_share_period(conn):
    _pipeline(conn)
    conn.execute("UPDATE sec_filing SET report_period='2026-08-20'"
                 " WHERE accession='0001000001-26-000102'")
    conn.execute("UPDATE sec_filing SET amend_link_state=NULL, amends_accession=NULL"
                 " WHERE accession='0001000001-26-000103'")
    conn.commit()
    hist = us_route.link_amendments(conn)
    assert hist["AMBIGUOUS"] == 1


def test_eligible_session_us():
    r = us_route.eligible_session_us("2026-08-27T18:02:11Z", None)  # 14:02 ET -> same session
    assert r["eligible_from_date"] == "2026-08-27" and r["same_session_partial"] is True
    r = us_route.eligible_session_us("2026-08-27T21:30:00Z", None)  # 17:30 ET -> next day
    assert r["eligible_from_date"] == "2026-08-28" and r["same_session_partial"] is False
    r = us_route.eligible_session_us(None, "2026-08-27")
    assert r["precision"] == "DATE"


def test_staged_classification_8k_without_items_stays_pending(conn):
    from investment_tool import us_ingest, us_route

    us_ingest.ingest_daily_index(conn, (FIX / "master_sample.idx").read_bytes(),
                                 "2026-08-27", "m_idx")
    hist = us_route.route_unclassified(conn)
    # no items enrichment yet: all four 8-K/8-K/A rows pend; nothing neutralized
    assert hist["PENDING_ITEMS"] == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM sec_filing WHERE form LIKE '8-K%'"
        " AND classification_version IS NOT NULL").fetchone()[0] == 0
    # enrichment arrives -> re-route classifies the material filings
    us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
    hist2 = us_route.route_unclassified(conn)
    assert hist2.get("EVENT", 0) >= 2  # 4.02 and 1.03 8-Ks become HARD events
    events = {r["type"] for r in conn.execute("SELECT type FROM event")}
    assert "NON_RELIANCE" in events and "BANKRUPTCY" in events


def test_late_enrichment_reclassifies_previously_neutral_8k(conn):
    """Regression for the silent-neutralization defect: a legacy row classified
    while items were unknown must be re-routed when items arrive."""
    from investment_tool import us_ingest, us_route

    us_ingest.ingest_daily_index(conn, (FIX / "master_sample.idx").read_bytes(),
                                 "2026-08-27", "m_idx")
    # simulate the pre-fix state: force-classify one 8-K as neutral observation
    conn.execute("UPDATE sec_filing SET classification_version='us_v1', relevance='NEUTRAL'"
                 " WHERE accession='0001000002-26-000201'")
    conn.commit()
    us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
    row = conn.execute("SELECT classification_version FROM sec_filing"
                       " WHERE accession='0001000002-26-000201'").fetchone()
    assert row["classification_version"] is None  # cleared for re-routing
    us_route.route_unclassified(conn)
    row = conn.execute("SELECT relevance, event_id FROM sec_filing"
                       " WHERE accession='0001000002-26-000201'").fetchone()
    assert row["relevance"] == "HARD_NEGATIVE" and row["event_id"] is not None
