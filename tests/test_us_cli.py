from pathlib import Path

from investment_tool import config as config_mod
from investment_tool import us_cli

FIX = Path(__file__).parent / "fixtures" / "sec"


def _cfg(conn):
    cfg = config_mod.load("v0.2")
    config_mod.register(conn, cfg)
    return cfg


def test_us_map_fixture_end_to_end(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(us_cli, "DEFAULT_DATA_DIR", tmp_path)
    cfg = _cfg(conn)
    for cid, t, ex in (("US:ALPH", "ALPH", "NASDAQ"), ("US:BETA", "BETA", "NYSE")):
        conn.execute("INSERT INTO company(company_id, created_asof)"
                     " VALUES(?, '2026-01-01T00:00:00Z')", (cid,))
        conn.execute("INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
                     " VALUES(?,?,?,?, 'USD')", (f"{ex}:{t}", cid, t, ex))
    audit = us_cli.run_us_map(conn, cfg, str(FIX / "company_tickers_exchange_sample.json"))
    assert audit["reconciliation"]["MATCHED"] == 2
    assert (tmp_path / "audit").exists()


def test_us_sync_fixture_pipeline_and_review(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(us_cli, "DEFAULT_DATA_DIR", tmp_path)
    cfg = _cfg(conn)
    audit = us_cli.run_us_sync(
        conn, cfg, "2026-08-27",
        str(FIX / "master_sample.idx"), str(FIX / "efts_8k_sample.json"),
        [str(FIX / "submissions_sample.json")], str(FIX / "getcurrent_sample.atom"),
    )
    assert audit["us_completeness"] == "INDEX_RECONCILED_AS_OF(2026-08-27)"
    assert audit["routing"]["EVENT"] >= 3
    assert audit["review_queue_pending"] >= 2
    review = us_cli.run_review(conn, cfg)
    assert len(review["sections"]["us_review_pending"]) >= 2
    assert review["aged_to_archive"] == 0  # fresh items don't age


def test_review_aging_archives_explicitly(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(us_cli, "DEFAULT_DATA_DIR", tmp_path)
    cfg = _cfg(conn)
    us_cli.run_us_sync(conn, cfg, "2026-08-27", str(FIX / "master_sample.idx"),
                       str(FIX / "efts_8k_sample.json"), [], None)
    conn.execute("UPDATE sec_filing SET first_seen_at_utc='2026-08-01T00:00:00Z'")
    conn.commit()
    review = us_cli.run_review(conn, cfg)
    assert review["aged_to_archive"] >= 2
    assert conn.execute("SELECT COUNT(*) FROM sec_filing"
                        " WHERE review_state='ARCHIVED_UNREVIEWED'").fetchone()[0] >= 2


def test_export_bundle(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(us_cli, "DEFAULT_DATA_DIR", tmp_path)
    conn.execute("INSERT INTO company(company_id, created_asof)"
                 " VALUES('CN:X','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO candidate(candidate_id, company_id, lane, state, profile_json,"
                 " gates_json, detected_at_utc, config_version)"
                 " VALUES('candX','CN:X','A','PENDING_ATTRIBUTION','{}','{}',"
                 " '2026-08-28T00:00:00Z','v0.2')")
    conn.commit()
    path = us_cli.run_export(conn, "candX")
    assert path.exists()
    import json
    bundle = json.loads(path.read_text())
    assert bundle["candidate"]["candidate_id"] == "candX"
