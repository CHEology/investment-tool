"""PR-G: catch-up scheduling, idempotency, partial-completion recovery, and
the soak report gates. Offline throughout (fixtures + fake manifests)."""

import json
from pathlib import Path

from investment_tool import config as config_mod
from investment_tool import us_cli, us_soak

FIX = Path(__file__).parent / "fixtures" / "sec"


def _cfg():
    return config_mod.load("v0.2")


def _mark_synced(conn, date):
    conn.execute(
        "INSERT INTO manifest(manifest_id, run_id, provider, dataset, params_json,"
        " source_url, retrieved_at_utc, http_status, schema_version,"
        " transform_version, code_git_sha, config_version, quality_state)"
        " VALUES(?, 'r', 'sec', 'daily_index', ?, 'u', ?, 200, 's','t','g','v0.2','OK')",
        (f"m_{date}", json.dumps({"date": date}), f"{date}T23:00:00Z"))
    conn.commit()


def test_pending_dates_catch_up_and_evening_threshold(conn):
    # Wed 2026-08-26 and Thu 08-27 synced; evaluate Fri 08-28 at 23:30Z
    # (19:30 ET, past the evening-index threshold) -> only 08-28 pending
    for d in ("2026-08-21", "2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27"):
        _mark_synced(conn, d)
    pending = us_soak.pending_sync_dates(conn, "2026-08-28T23:30:00Z")
    assert pending == ["2026-08-28"]
    # same evening BEFORE the threshold (21:00Z = 17:00 ET): today excluded
    pending = us_soak.pending_sync_dates(conn, "2026-08-28T21:00:00Z")
    assert pending == []
    # a crashed day heals: drop 08-26 from history -> it reappears
    conn.execute("DELETE FROM manifest WHERE manifest_id='m_2026-08-26'")
    conn.commit()
    pending = us_soak.pending_sync_dates(conn, "2026-08-28T23:30:00Z")
    assert pending == ["2026-08-26", "2026-08-28"]
    # weekend morning: nothing new expected (Sat 08-29 ET)
    pending = us_soak.pending_sync_dates(conn, "2026-08-29T15:00:00Z")
    assert "2026-08-29" not in pending


def test_fixture_sync_rerun_is_idempotent(conn):
    """Running the same day's sync twice adds nothing and never moves
    first_seen_at_utc (the earliest sighting survives)."""
    cfg = _cfg()
    us_cli.run_us_sync(conn, cfg, "2026-08-27", str(FIX / "master_sample.idx"),
                       str(FIX / "efts_8k_sample.json"),
                       [str(FIX / "submissions_sample.json")], None)
    snap1 = {r["accession"]: r["first_seen_at_utc"] for r in conn.execute(
        "SELECT accession, first_seen_at_utc FROM sec_filing")}
    events1 = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    us_cli.run_us_sync(conn, cfg, "2026-08-27", str(FIX / "master_sample.idx"),
                       str(FIX / "efts_8k_sample.json"),
                       [str(FIX / "submissions_sample.json")], None)
    snap2 = {r["accession"]: r["first_seen_at_utc"] for r in conn.execute(
        "SELECT accession, first_seen_at_utc FROM sec_filing")}
    events2 = conn.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    assert snap1 == snap2
    assert events1 == events2


def test_partial_then_full_run_recovers_and_preserves_first_seen(conn):
    """Crash-recovery semantics: a lossy getcurrent-only run followed by the
    full evening run converges to the complete state, and filings first seen
    in the partial run keep their earlier first_seen_at_utc."""
    cfg = _cfg()
    a1 = us_cli.run_us_sync(conn, cfg, "2026-08-27", None, None, [],
                            str(FIX / "getcurrent_sample.atom"))
    assert a1["us_completeness"] == "PENDING_EVENING_INDEX"
    early = {r["accession"]: r["first_seen_at_utc"] for r in conn.execute(
        "SELECT accession, first_seen_at_utc FROM sec_filing")}
    assert early  # the lossy channel did see something
    a2 = us_cli.run_us_sync(conn, cfg, "2026-08-27", str(FIX / "master_sample.idx"),
                            str(FIX / "efts_8k_sample.json"),
                            [str(FIX / "submissions_sample.json")], None)
    assert "INDEX_RECONCILED" in str(a2["us_completeness"])
    for acc, seen in early.items():
        now = conn.execute("SELECT first_seen_at_utc FROM sec_filing WHERE accession=?",
                           (acc,)).fetchone()["first_seen_at_utc"]
        assert now == seen
    assert conn.execute("SELECT COUNT(*) FROM sec_filing").fetchone()[0] > len(early)


def test_soak_report_gates_require_window_evidence(conn, tmp_path):
    """Superseded by the H0/F15 gate rework: an empty soak must fail, and the
    full acceptance path is covered in tests/test_h0_corrections.py."""
    report = us_soak.soak_report(conn)
    assert report["gates"]["all_passed"] is False
    assert report["gates"]["min_5_ledger_calendar_days"] is False
    assert report["gates"]["scheduled_run_observed"] is False
