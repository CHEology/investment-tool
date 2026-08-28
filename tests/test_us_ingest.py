from pathlib import Path

import pytest

from investment_tool import us_ingest

FIX = Path(__file__).parent / "fixtures" / "sec"


def _ingest(conn):
    return us_ingest.ingest_daily_index(
        conn, (FIX / "master_sample.idx").read_bytes(), "2026-08-27", "m_idx"
    )


def test_daily_index_normalizes_allowlist_only(conn):
    result = _ingest(conn)
    assert result["total_rows"] == 11
    assert result["normalized"] == 9  # everything except NPORT-P and 424B2 noise
    forms = {r["form"] for r in conn.execute("SELECT form FROM sec_filing")}
    assert "NPORT-P" not in forms and "424B2" not in forms
    assert {"8-K", "8-K/A", "NT 10-K", "6-K", "SC 13D", "4", "10-Q"} <= forms
    assert result["us_completeness"] == "COMPLETE(2026-08-27)"


def test_idempotent_reingest_preserves_first_seen(conn):
    _ingest(conn)
    before = conn.execute(
        "SELECT first_seen_at_utc FROM sec_filing WHERE accession='0001000001-26-000101'"
    ).fetchone()[0]
    _ingest(conn)
    rows = conn.execute("SELECT COUNT(*) FROM sec_filing").fetchone()[0]
    after = conn.execute(
        "SELECT first_seen_at_utc FROM sec_filing WHERE accession='0001000001-26-000101'"
    ).fetchone()[0]
    assert rows == 9 and after == before


def test_checkpoint_commits_atomically_with_rows(conn, monkeypatch):
    real = us_ingest._upsert_filing
    calls = {"n": 0}

    def explode_late(c, row, first_seen, quality, manifest_id):
        calls["n"] += 1
        if calls["n"] == 5:
            raise RuntimeError("simulated crash mid-batch")
        return real(c, row, first_seen, quality, manifest_id)

    monkeypatch.setattr(us_ingest, "_upsert_filing", explode_late)
    with pytest.raises(RuntimeError):
        _ingest(conn)
    assert conn.execute("SELECT COUNT(*) FROM sec_filing").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_checkpoint").fetchone()[0] == 0


def test_getcurrent_is_best_effort_and_deduped(conn):
    payload = (FIX / "getcurrent_sample.atom").read_bytes()
    r1 = us_ingest.poll_getcurrent(conn, payload, "m_atom")
    assert r1["us_completeness"] == "PENDING_EVENING_INDEX"
    assert r1["normalized"] == 2
    us_ingest.poll_getcurrent(conn, payload, "m_atom")
    assert conn.execute("SELECT COUNT(*) FROM sec_filing").fetchone()[0] == 2
    role = conn.execute(
        "SELECT role FROM filing_party WHERE accession='0001000004-26-000401'"
    ).fetchone()["role"]
    assert role == "FILER"  # '(Filed by)' hint captured


def test_enrichment_fills_only_nulls(conn):
    _ingest(conn)
    n = us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
    assert n == 3
    row = conn.execute(
        "SELECT items_csv FROM sec_filing WHERE accession='0001000002-26-000201'"
    ).fetchone()
    assert row["items_csv"] == "1.03"
    n2 = us_ingest.enrich_from_submissions(conn, (FIX / "submissions_sample.json").read_bytes())
    assert n2 == 3
    row = conn.execute(
        "SELECT accepted_at_utc, report_period FROM sec_filing"
        " WHERE accession='0001000001-26-000102'"
    ).fetchone()
    assert row["accepted_at_utc"] == "2026-08-27T18:02:11Z"
    assert row["report_period"] == "2026-08-25"


def test_no_lookahead_visibility(conn):
    _ingest(conn)
    conn.execute("UPDATE sec_filing SET first_seen_at_utc='2026-08-27T22:00:00Z'")
    conn.execute("UPDATE sec_filing SET first_seen_at_utc='2026-08-28T01:00:00Z'"
                 " WHERE accession='0001000002-26-000201'")
    conn.commit()
    visible = us_ingest.visible_filings(conn, "2026-08-27T23:59:59Z")
    accs = {r["accession"] for r in visible}
    assert "0001000002-26-000201" not in accs  # first seen after the replay cutoff
    assert len(accs) == 8


def test_removal_two_step_requires_confirmation(conn):
    _ingest(conn)
    acc = "0001000007-26-000701"
    assert us_ingest.mark_removal_suspected(conn, [acc]) == 1
    # direct fetch says it still exists -> back to ACTIVE + anomaly observation
    assert us_ingest.confirm_removal(conn, acc, 200) == "ACTIVE"
    assert conn.execute("SELECT COUNT(*) FROM observation WHERE"
                        " kind='removal_reconciliation_anomaly'").fetchone()[0] == 1
    # confirmed gone -> REMOVED
    us_ingest.mark_removal_suspected(conn, [acc])
    assert us_ingest.confirm_removal(conn, acc, 404) == "REMOVED"
