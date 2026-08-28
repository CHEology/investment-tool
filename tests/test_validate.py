import json

from investment_tool import validate


def _seed(conn, frozen_at, artifact_status="VALID", n_days=0):
    conn.execute("INSERT INTO company(company_id, created_asof)"
                 " VALUES('CN:000900','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO listing(listing_id, company_id, ticker, exchange, board, currency)"
                 " VALUES('SZSE:000900','CN:000900','000900','SZSE','MAIN','CNY')")
    for i, lid in enumerate(["SZSE:000900", "SZSE:000901", "SZSE:000902"]):
        if i:
            conn.execute("INSERT INTO company(company_id, created_asof)"
                         f" VALUES('CN:00090{i}','2026-01-01T00:00:00Z')")
            conn.execute(
                "INSERT INTO listing(listing_id, company_id, ticker, exchange, board, currency)"
                f" VALUES('{lid}','CN:00090{i}','00090{i}','SZSE','MAIN','CNY')")
        conn.execute(
            "INSERT INTO market_snapshot(listing_id, asof_date, industry, is_st, source, quality)"
            " VALUES(?,?,?,0,'test','PROVISIONAL')", (lid, "2026-08-01", "测试行业"))
        px = 10.0
        for d in range(n_days):
            px *= 1.01 if i == 0 else 1.002  # candidate outruns peers
            conn.execute(
                "INSERT INTO security_day(listing_id, trade_date, adj_close, ret, ret_basis,"
                " currency, limit_state, provider, quality, manifest_id)"
                " VALUES(?,?,?,?,?, 'CNY','FREE','test','PROVISIONAL','m')",
                (lid, f"2026-08-{d+2:02d}", f"{px:.4f}", 0.01, "QFQ_CONSEC"))
    conn.execute(
        "INSERT INTO candidate(candidate_id, company_id, lane, state, profile_json, gates_json,"
        " detected_at_utc, config_version)"
        " VALUES('cand9','CN:000900','A','NOT_ADMITTED_WITHIN_BRACKET','{}','{}',?, 'v0.1')",
        (frozen_at,))
    conn.execute(
        "INSERT INTO frozen_artifact(artifact_id, kind, candidate_id, version, frozen_at_utc,"
        " content_sha256, path, config_version, status)"
        " VALUES('a9','CARD','cand9',1,?,'x','p','v0.1',?)", (frozen_at, artifact_status))
    conn.commit()


def test_pending_first_session_when_frozen_today(conn):
    _seed(conn, "2026-08-28T16:00:00Z", n_days=0)
    audit = validate.run_validation(conn, asof="2026-08-28")
    assert audit["candidates_in_ledger"] == 1
    assert audit["states"] == {"PENDING_FIRST_SESSION": 1}
    row = conn.execute("SELECT metrics_json FROM validation_snapshot").fetchone()
    assert json.loads(row["metrics_json"])["state"] == "PENDING_FIRST_SESSION"


def test_tracked_with_peer_adjusted_return(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(validate, "DEFAULT_DATA_DIR", tmp_path)
    _seed(conn, "2026-08-01T16:00:00Z", n_days=20)
    audit = validate.run_validation(conn, asof="2026-08-28")
    assert audit["states"] == {"TRACKED": 1}
    snap = json.loads(conn.execute(
        "SELECT metrics_json FROM validation_snapshot").fetchone()["metrics_json"])
    assert snap["sessions_elapsed"] == 18
    assert snap["ret_raw"] > 0.15
    assert snap["ret_peer_adj"] is not None and snap["ret_peer_adj"] > 0.10
    assert snap["mae_raw"] <= snap["ret_raw"]


def test_invalidated_visible_but_never_tracked(conn):
    _seed(conn, "2026-08-28T16:00:00Z", artifact_status="INVALIDATED")
    audit = validate.run_validation(conn, asof="2026-08-28")
    assert audit["candidates_in_ledger"] == 1     # visible in the ledger
    assert audit["tracked"] == 0                  # never a control observation
    import json
    snap = json.loads(conn.execute(
        "SELECT metrics_json FROM validation_snapshot").fetchone()["metrics_json"])
    assert snap["state"] == "EXCLUDED_INVALIDATED"


def test_snapshot_idempotent_per_asof(conn):
    _seed(conn, "2026-08-28T16:00:00Z")
    validate.run_validation(conn, asof="2026-08-28")
    validate.run_validation(conn, asof="2026-08-28")
    n = conn.execute("SELECT COUNT(*) AS n FROM validation_snapshot").fetchone()["n"]
    assert n == 1


def test_peer_baseline_mismatch_is_skipped(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(validate, "DEFAULT_DATA_DIR", tmp_path)
    _seed(conn, "2026-08-01T16:00:00Z", n_days=20)
    # one peer loses its early sessions -> different baseline date -> skipped
    conn.execute("DELETE FROM security_day WHERE listing_id='SZSE:000901'"
                 " AND trade_date<'2026-08-10'")
    conn.commit()
    validate.run_validation(conn, asof="2026-08-28")
    import json
    snap = json.loads(conn.execute(
        "SELECT metrics_json FROM validation_snapshot").fetchone()["metrics_json"])
    assert snap["peers_skipped_baseline_mismatch"] == 1
    # only one comparable peer remains (<2) -> peer-adjusted honestly None
    assert snap["ret_peer_adj"] is None


def test_peer_membership_is_point_in_time(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(validate, "DEFAULT_DATA_DIR", tmp_path)
    _seed(conn, "2026-08-01T16:00:00Z", n_days=20)
    # industry reclassified AFTER tracking started: must not affect membership
    for lid in ("SZSE:000901", "SZSE:000902"):
        conn.execute("INSERT INTO market_snapshot(listing_id, asof_date, industry, is_st,"
                     " source, quality) VALUES(?,?,?,0,'test','PROVISIONAL')",
                     (lid, "2026-08-20", "改分类行业"))
    conn.commit()
    validate.run_validation(conn, asof="2026-08-28")
    import json
    snap = json.loads(conn.execute(
        "SELECT metrics_json FROM validation_snapshot").fetchone()["metrics_json"])
    assert snap["ret_peer_adj"] is not None  # pre-freeze membership still used
