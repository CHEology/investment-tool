"""H0 correctness package regression tests (review F13–F18 + validate fix).

One test per confirmed issue, each written to FAIL against the pre-H0
behavior."""

import json

import pytest

from investment_tool import config as config_mod
from investment_tool import reaction as reaction_mod
from investment_tool import us_prices, us_soak, us_trial
from test_us_trial import _anchors, _mk_series  # shared real-session helpers


@pytest.fixture
def tcfg():
    return config_mod.load("us_trial_v0.3")


# ------------------------------------------------------------- F13 gating


def test_f13_contaminated_evt1_alone_is_partial_precision(conn, tcfg):
    """Intra-session release, -10% event day, calm afterwards: the
    contaminated leg must not enter the lead track alone."""
    sess = _mk_series(conn, moves={75: -0.10})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[75], partial=True), sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED_PARTIAL_PRECISION"
    assert hits == ["evt1_contaminated"]
    ev = {"event_id": "e", "type": "ISSUER_8K", "ticker": "TT", "accession": "a",
          "accepted_at_utc": None, "first_seen_at_utc": "x"}
    st, _ = us_trial.assess_and_state(ev, rx, gate, hits, None, tcfg)
    assert st == "US_TRIAL_PARTIAL_PRECISION"


def test_f13_contaminated_with_clean_corroboration_triggers(conn, tcfg):
    """Same intra-session release but the decline continues: car5 (and next1)
    corroborate, so the event still triggers — with contamination visible in
    the leg names."""
    sess = _mk_series(conn, moves={75: -0.10, 76: -0.06})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[75], partial=True), sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED"
    assert "evt1_contaminated" in hits and "car5" in hits and "next1" in hits


def test_f13_date_precision_treated_as_contaminated(conn, tcfg):
    sess = _mk_series(conn, moves={79: -0.10})
    anchors = _anchors(sess[79])
    anchors["precision"] = "DATE"
    anchors["same_session_partial"] = None
    rx = reaction_mod.compute_event_reaction(conn, "NASDAQ:TT", anchors, sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED_PARTIAL_PRECISION"


def test_f13_clean_pre_open_release_unaffected(conn, tcfg):
    sess = _mk_series(conn, moves={79: -0.10}, vol_spikes={79: 5000000})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED" and "evt1" in hits and "volume" in hits


def test_next1_leg_computed(conn, tcfg):
    sess = _mk_series(conn, moves={75: -0.02, 76: -0.08})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[75]), sess[-1])
    assert rx["mkt_adj_next_ret1"] == pytest.approx(-0.08, abs=1e-3)


# --------------------------------------------------- F14 rerun regression


def _mk_filing_event(conn, sess, eid="ev_us_rr", acc="acc-rr"):
    last = sess[-1]
    conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc, state)"
                 " VALUES(?, 'COMPANY','ISSUER_8K', ?, 'VERIFIED')",
                 (eid, f"{last}T21:00:00Z"))
    conn.execute("INSERT INTO event_company(event_id, company_id) VALUES(?, 'US:TT')",
                 (eid,))
    conn.execute(
        "INSERT INTO sec_filing(accession, cik, form, is_amendment, filing_date,"
        " first_seen_at_utc, quality, manifest_id, event_id, accepted_at_utc,"
        " items_csv, primary_doc_name)"
        " VALUES(?, '7000001','8-K',0,?,?, 'OK','m',?,?, '5.02','d.htm')",
        (acc, last, f"{last}T21:00:00Z", eid, f"{last}T12:00:00Z"))
    conn.commit()


def test_f14_rerun_reuses_cached_content_and_never_regresses(
        conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_filing_docs
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices", lambda *a, **k: {"manifest": "m"})
    fetches = {"n": 0}

    def fake_fetch(conn_, cfg, http, accession):
        fetches["n"] += 1
        p = tmp_path / f"{accession}.txt"
        p.write_text("Item 5.02 departure; the Chief Financial Officer resigned.")
        return {"state": "FETCHED", "text_path": str(p)}

    monkeypatch.setattr(ut.us_filing_docs, "fetch_primary_document", fake_fetch)
    # cached_content must see the stored document: register it like the real
    # fetch path does and point text_path at the tmp file
    monkeypatch.setattr(us_filing_docs, "text_path",
                        lambda acc: tmp_path / f"{acc}.txt")
    sess = _mk_series(conn, moves={79: -0.10})
    _mk_filing_event(conn, sess)
    s1 = ut.run_trial(conn, None, tcfg, sess[-1], content_cap=1,
                      http_factory=lambda: object())
    conn.execute("INSERT OR REPLACE INTO sec_filing_document(accession, filename,"
                 " doc_type, url, sha256, manifest_id)"
                 " VALUES('acc-rr','d.htm','primary','u','s','m')")
    conn.commit()
    assert s1["coverage"]["leads"] == 1 and fetches["n"] == 1
    # day 2: budget zero — WITHOUT the fix the lead regresses to
    # RESEARCH_PENDING; with cache reuse it stays a lead and fetches nothing
    s2 = ut.run_trial(conn, None, tcfg, sess[-1], content_cap=0,
                      http_factory=lambda: object())
    assert fetches["n"] == 1                       # no refetch
    assert s2["coverage"]["documents_reused"] == 1
    st = conn.execute("SELECT state FROM candidate").fetchone()["state"]
    assert st == "US_TRIAL_LEAD"


def test_f14_budget_state_never_overwrites_substantive_state(conn, tcfg):
    conn.execute("INSERT INTO company(company_id, created_asof)"
                 " VALUES('US:TT','2026-01-01T00:00:00Z')")
    conn.commit()
    profile = {"event_id": "ev_g", "reaction": {"t0_session": "2026-08-27"}}
    us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_LEAD", profile, "c")
    us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_RESEARCH_PENDING", profile, "c")
    assert conn.execute("SELECT state FROM candidate").fetchone()["state"] == \
        "US_TRIAL_LEAD"
    # but a budget state may replace another budget state
    us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_FETCH_FAILED",
                               {**profile, "event_id": "ev_g2"}, "c")
    us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_RESEARCH_PENDING",
                               {**profile, "event_id": "ev_g2"}, "c")
    st = conn.execute("SELECT state FROM candidate WHERE"
                      " json_extract(profile_json,'$.event_id')='ev_g2'").fetchone()
    assert st["state"] == "US_TRIAL_RESEARCH_PENDING"


# ------------------------------------------------------------ F15 soak gate


def _ledger(tmp_path, ts, origin="MANUAL", synced=(), errors=(), idem=None):
    d = tmp_path / "audit" / "soak"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"us_soak_{ts.replace(':', '').replace('-', '')}.json").write_text(
        json.dumps({"generated_at": ts, "origin": origin,
                    "synced": list(synced), "errors": list(errors),
                    "idempotency": idem}))


def test_f15_single_manual_ledger_must_not_pass(conn, tmp_path):
    _ledger(tmp_path, "2026-08-29T01:00:00Z", origin="MANUAL",
            synced=[{"date": "2026-08-24",
                     "us_completeness": "INDEX_RECONCILED_AS_OF(2026-08-24)"}],
            idem={"idempotent": True})
    report = us_soak.soak_report(conn)
    g = report["gates"]
    assert g["min_5_ledger_calendar_days"] is False
    assert g["scheduled_run_observed"] is False
    assert report["gates"]["all_passed"] is False


def test_f15_pre_soak_db_history_not_counted(conn, tmp_path):
    """A pre-soak daily_index manifest in the DB must not count toward the
    in-window filing-day gate."""
    conn.execute(
        "INSERT INTO manifest(manifest_id, run_id, provider, dataset, params_json,"
        " source_url, retrieved_at_utc, http_status, schema_version,"
        " transform_version, code_git_sha, config_version, quality_state)"
        " VALUES('m_old','r','sec','daily_index','{\"date\": \"2026-08-20\"}','u',"
        " '2026-08-20T23:00:00Z', 200, 's','t','g','v0.2','OK')")
    conn.commit()
    _ledger(tmp_path, "2026-08-29T01:00:00Z")
    report = us_soak.soak_report(conn)
    assert "2026-08-20" not in report["filing_days_synced_in_window"]


def test_f15_full_soak_passes_and_errors_block(conn, tmp_path):
    days = ["2026-08-29", "2026-08-30", "2026-08-31", "2026-09-01", "2026-09-02"]
    for i, d in enumerate(days):
        _ledger(tmp_path, f"{d}T23:35:00Z",
                origin="SCHEDULED" if i else "MANUAL",
                synced=[{"date": d,
                         "us_completeness": f"INDEX_RECONCILED_AS_OF({d})"}],
                idem={"idempotent": True} if i == 2 else None)
    (tmp_path / "audit" / "soak" / "amendment_drill_x.json").write_text("{}")
    (tmp_path / "audit" / "soak" / "recovery_drill_x.json").write_text("{}")
    report = us_soak.soak_report(conn)
    assert report["gates"]["all_passed"] is True
    # an unresolved error flips the gate
    _ledger(tmp_path, "2026-09-03T23:35:00Z", origin="SCHEDULED",
            errors=[{"date": "2026-09-03", "error": "http=500"}])
    report = us_soak.soak_report(conn)
    assert report["gates"]["zero_unresolved_errors"] is False
    assert report["gates"]["all_passed"] is False
    # a later ledger reconciling that date resolves it
    _ledger(tmp_path, "2026-09-04T23:35:00Z", origin="SCHEDULED",
            synced=[{"date": "2026-09-03",
                     "us_completeness": "INDEX_RECONCILED_AS_OF(2026-09-03)"}])
    report = us_soak.soak_report(conn)
    assert report["gates"]["zero_unresolved_errors"] is True


# ------------------------------------------------------------ F17 lookback


def test_f17_lookback_bounds_event_selection(conn, tcfg):
    conn.execute("INSERT INTO company(company_id, cik, created_asof)"
                 " VALUES('US:TT','7000001','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO listing(listing_id, company_id, ticker, exchange,"
                 " currency) VALUES('NASDAQ:TT','US:TT','TT','NASDAQ','USD')")
    for eid, seen in (("ev_us_old", "2026-07-01T20:00:00Z"),
                      ("ev_us_new", "2026-08-27T20:00:00Z")):
        conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc,"
                     " state) VALUES(?, 'COMPANY','ISSUER_8K', ?, 'VERIFIED')",
                     (eid, seen))
        conn.execute("INSERT INTO event_company(event_id, company_id)"
                     " VALUES(?, 'US:TT')", (eid,))
    conn.commit()
    evs = us_trial.select_events(conn, "2026-08-28", lookback_days=15)
    assert [e["event_id"] for e in evs] == ["ev_us_new"]
    evs = us_trial.select_events(conn, "2026-08-28")   # unbounded: both
    assert len(evs) == 2


# ------------------------------------------------------- F18 rename


def test_f18_doc_review_completed_naming(conn, tcfg):
    from investment_tool import us_queue
    assert "DOC_REVIEW_COMPLETED" in us_queue.PROTECTED_STATES
    assert "RESEARCH_COMPLETED" not in us_queue.PROTECTED_STATES


# ------------------------------------------------ validate.py US semantics


def test_validate_us_freeze_uses_new_york_date(conn):
    from investment_tool import validate

    # 2026-08-28T23:24Z = 19:24 ET on 08-28 (NOT 08-29 as the Beijing rule said)
    assert validate._freeze_local_date("2026-08-28T23:24:04Z", "NASDAQ") == \
        "2026-08-28"
    assert validate._freeze_local_date("2026-08-28T23:24:04Z", "SZSE") == \
        "2026-08-29"


def test_validate_us_snapshot_gets_market_adjustment(conn, tmp_path, monkeypatch):
    from investment_tool import validate

    monkeypatch.setattr(validate, "DEFAULT_DATA_DIR", tmp_path)
    sess = _mk_series(conn, moves={79: -0.05})
    cand_id = "cval1"
    conn.execute("INSERT INTO candidate(candidate_id, company_id, lane, state,"
                 " profile_json, gates_json, detected_at_utc, config_version)"
                 " VALUES(?,?,?,?,?,?,?,?)",
                 (cand_id, "US:TT", "A", "US_TRIAL_LEAD", "{}", "{}",
                  f"{sess[70]}T23:00:00Z", "us_trial_v0.3"))
    conn.execute("INSERT INTO frozen_artifact(artifact_id, kind, candidate_id,"
                 " version, frozen_at_utc, content_sha256, path, config_version,"
                 " status) VALUES('a1','CARD',?,1,?,'s','p','c','VALID')",
                 (cand_id, f"{sess[70]}T23:00:00Z"))
    conn.commit()
    audit = validate.run_validation(conn, asof=sess[-1])
    assert audit["tracked"] >= 1
    snap = json.loads(conn.execute(
        "SELECT metrics_json FROM validation_snapshot WHERE candidate_id=?",
        (cand_id,)).fetchone()["metrics_json"])
    assert snap["state"] == "TRACKED"
    assert snap["ret_mkt_adj"] is not None   # SPY-adjusted, not raw-only
