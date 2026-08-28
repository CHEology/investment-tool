"""Late-enrichment propagation and checkpoint monotonicity regressions."""

from pathlib import Path

from investment_tool import us_ingest, us_route

FIX = Path(__file__).parent / "fixtures" / "sec"


def _ingest_day(conn, date="2026-08-27"):
    us_ingest.ingest_daily_index(conn, (FIX / "master_sample.idx").read_bytes(), date, "m")


def test_event_published_null_until_acceptance_then_propagates(conn):
    _ingest_day(conn)
    us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
    us_route.route_unclassified(conn)
    ev = conn.execute(
        "SELECT e.published_at_utc FROM event e JOIN sec_filing f ON f.event_id=e.event_id"
        " WHERE f.accession='0001000002-26-000201'").fetchone()
    assert ev["published_at_utc"] is None  # no acceptance yet: honest NULL, no midnight
    # acceptance arrives later -> propagation updates event AND evidence
    conn.execute("UPDATE sec_filing SET accepted_at_utc='2026-08-27T19:11:05Z'"
                 " WHERE accession='0001000002-26-000201'")
    conn.commit()
    out = us_route.propagate_enrichment(conn)
    assert out["timestamps_updated"] >= 1
    ev = conn.execute(
        "SELECT e.published_at_utc FROM event e JOIN sec_filing f ON f.event_id=e.event_id"
        " WHERE f.accession='0001000002-26-000201'").fetchone()
    assert ev["published_at_utc"] == "2026-08-27T19:11:05Z"
    evd = conn.execute("SELECT published_at_utc FROM evidence"
                       " WHERE evidence_id='evd_us_0001000002-26-000201'").fetchone()
    assert evd["published_at_utc"] == "2026-08-27T19:11:05Z"


def test_legacy_midnight_timestamps_cleared_when_no_acceptance(conn):
    _ingest_day(conn)
    us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
    us_route.route_unclassified(conn)
    conn.execute("""UPDATE event SET published_at_utc='2026-08-27T00:00:00Z'
                    WHERE event_id IN (SELECT event_id FROM sec_filing
                                       WHERE accession='0001000002-26-000201')""")
    conn.commit()
    out = us_route.propagate_enrichment(conn)
    assert out["midnight_cleared"] == 1
    ev = conn.execute(
        "SELECT e.published_at_utc FROM event e JOIN sec_filing f ON f.event_id=e.event_id"
        " WHERE f.accession='0001000002-26-000201'").fetchone()
    assert ev["published_at_utc"] is None


def test_unlinked_amendment_reconsidered_when_original_arrives_later(conn):
    # amendment day ingested FIRST; its original is not in the database yet
    idx_amend = b"\n".join([
        b"CIK|Company Name|Form Type|Date Filed|File Name", b"--------",
        b"1000001|ALPHA TEST CORP|8-K/A|20260827|edgar/data/1000001/0001000001-26-000103.txt",
    ])
    us_ingest.ingest_daily_index(conn, idx_amend, "2026-08-27", "m1")
    us_ingest.enrich_from_submissions(conn, (FIX / "submissions_sample.json").read_bytes())
    assert us_route.link_amendments(conn)["UNLINKED"] == 1
    # original arrives on a later backfill of the earlier day
    idx_orig = b"\n".join([
        b"CIK|Company Name|Form Type|Date Filed|File Name", b"--------",
        b"1000001|ALPHA TEST CORP|8-K|20260820|edgar/data/1000001/0001000001-26-000101.txt",
    ])
    us_ingest.ingest_daily_index(conn, idx_orig, "2026-08-20", "m2")
    us_ingest.enrich_from_submissions(conn, (FIX / "submissions_sample.json").read_bytes())
    hist = us_route.link_amendments(conn)  # UNLINKED rows are reconsidered
    assert hist["LINKED_UNIQUE"] == 1
    row = conn.execute("SELECT amends_accession FROM sec_filing"
                       " WHERE accession='0001000001-26-000103'").fetchone()
    assert row["amends_accession"] == "0001000001-26-000101"


def test_subject_link_resolved_by_late_role_information(conn):
    conn.execute("INSERT INTO company(company_id, cik, created_asof)"
                 " VALUES('US:TGT9','2000009','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
                 " VALUES('NYSE:TGT9','US:TGT9','TGT9','NYSE','USD')")
    conn.commit()
    idx = b"\n".join([
        b"CIK|Company Name|Form Type|Date Filed|File Name", b"--------",
        b"3000001|ACTIVIST LP|SC 13D|20260827|edgar/data/3000001/0003000001-26-000900.txt",
        b"2000009|TARGET NINE INC|SC 13D|20260827|edgar/data/2000009/0003000001-26-000900.txt",
    ])
    us_ingest.ingest_daily_index(conn, idx, "2026-08-27", "m")
    us_route.route_unclassified(conn)
    assert conn.execute("SELECT COUNT(*) FROM event_company").fetchone()[0] == 0
    # freshness channel later identifies the filer -> propagation resolves subject
    conn.execute("UPDATE filing_party SET role='FILER'"
                 " WHERE accession='0003000001-26-000900' AND cik='3000001'")
    conn.commit()
    out = us_route.propagate_enrichment(conn)
    assert out["subjects_resolved"] == 1
    linked = conn.execute("SELECT company_id FROM event_company").fetchone()
    assert linked["company_id"] == "US:TGT9"
    import json
    ev = conn.execute("SELECT dims_json FROM evidence"
                      " WHERE evidence_id='evd_us_0003000001-26-000900'").fetchone()
    assert json.loads(ev["dims_json"])["independence"] == 2


def test_checkpoint_is_monotonic(conn):
    _ingest_day(conn, "2026-08-27")
    assert conn.execute("SELECT cursor FROM source_checkpoint"
                        " WHERE source_id='sec_daily_index'").fetchone()[0] == "2026-08-27"
    _ingest_day(conn, "2026-08-26")  # older backfill day
    assert conn.execute("SELECT cursor FROM source_checkpoint"
                        " WHERE source_id='sec_daily_index'").fetchone()[0] == "2026-08-27"
