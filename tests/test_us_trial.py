"""US trial: horizons, gates, admission/rejection, lookahead, idempotency."""


import pytest

from investment_tool import config as config_mod
from investment_tool import us_filing_docs, us_prices, us_trial


@pytest.fixture
def tcfg():
    return config_mod.load("us_trial_v0")


def _mk_series(conn, lid="NASDAQ:TT", days=80, crash_at=None, crash=-0.20, vol_spike=None):
    conn.execute("INSERT OR IGNORE INTO company(company_id, cik, created_asof)"
                 " VALUES('US:TT','7000001','2026-01-01T00:00:00Z')")
    conn.execute("INSERT OR IGNORE INTO listing(listing_id, company_id, ticker, exchange,"
                 " currency) VALUES(?, 'US:TT','TT','NASDAQ','USD')", (lid,))
    import datetime

    d0 = datetime.date(2026, 5, 1)
    px = 100.0
    prev = None
    n = 0
    d = d0
    while n < days:
        if d.weekday() < 5:
            ret = crash if (crash_at is not None and n == crash_at) else 0.001
            px *= (1 + ret)
            vol = (vol_spike if (vol_spike and n == crash_at) else 1000000)
            conn.execute(
                "INSERT OR REPLACE INTO security_day(listing_id, trade_date, close, adj_close,"
                " volume, ret, ret_basis, adj_method, currency, limit_state, provider,"
                " quality, manifest_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (lid, d.isoformat(), f"{px:.4f}", f"{px:.4f}", str(vol),
                 None if prev is None else px / prev - 1.0, "ADJ_CONSEC", "PROVIDER_ADJ",
                 "USD", "FREE", "yfinance", "PROVISIONAL", "m"))
            # flat SPY/QQQ benchmarks
            for b in ("SPY", "QQQ"):
                conn.execute("INSERT OR REPLACE INTO benchmark_day(index_id, trade_date, close,"
                             " provider, quality, manifest_id)"
                             " VALUES(?,?,?, 'yfinance','PROVISIONAL','m')",
                             (b, d.isoformat(), "500.0"))
            prev = px
            n += 1
        d += datetime.timedelta(days=1)
    conn.commit()
    dates = [r[0] for r in conn.execute(
        "SELECT trade_date FROM security_day WHERE listing_id=? ORDER BY trade_date", (lid,))]
    return dates


def test_horizon_math_and_market_adjustment(conn, tcfg):
    dates = _mk_series(conn, days=80, crash_at=79, crash=-0.15)
    asof = dates[-1]
    rx = us_trial.compute_reaction(conn, "NASDAQ:TT", dates[79], asof)
    assert rx["state"] == "OK"
    assert rx["ret1"] == pytest.approx(-0.15, abs=1e-6)     # crash IS the last session
    assert rx["post_ret1"] == pytest.approx(-0.15, abs=1e-6)
    # flat benchmark -> market adjustment changes nothing
    assert rx["mkt_adj_post_ret1"] == pytest.approx(rx["post_ret1"], abs=1e-9)
    assert rx["ret5"] == pytest.approx((1.001 ** 4) * 0.85 - 1, abs=1e-6)
    assert rx["ret21"] is not None and rx["ret63"] is not None
    assert rx["mkt_adj_ret5"] == pytest.approx(rx["ret5"], abs=1e-9)


def test_gate_triggers_and_near_miss(conn, tcfg):
    dates = _mk_series(conn, days=80, crash_at=79, crash=-0.10, vol_spike=5000000)
    rx = us_trial.compute_reaction(conn, "NASDAQ:TT", dates[79], dates[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED" and "post1" in hits and "volume" in hits


def test_near_miss_band(conn, tcfg):
    dates = _mk_series(conn, days=80, crash_at=79, crash=-0.055)
    rx = us_trial.compute_reaction(conn, "NASDAQ:TT", dates[79], dates[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "NEAR_MISS"  # 5.5% < 7% but >= 70% of it


def test_missing_prices_is_insufficient_data(conn, tcfg):
    rx = us_trial.compute_reaction(conn, "NASDAQ:NONE", "2026-08-27", "2026-08-28")
    assert rx["state"] == "NO_PRICES"
    gate, _ = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "INSUFFICIENT_DATA"


def test_post_event_pending_when_eligibility_after_asof(conn, tcfg):
    dates = _mk_series(conn, days=40)
    rx = us_trial.compute_reaction(conn, "NASDAQ:TT", "2027-01-05", dates[-1])
    assert rx["post_state"] == "POST_EVENT_PENDING"


def test_admission_requires_eligible_category(conn, tcfg):
    ev = {"event_id": "e1", "type": "ISSUER_8K", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    rx = {"state": "OK", "sessions": 80}
    st, prof = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["post1"],
        {"primary": "management_change", "flags": [], "content_version": "v"}, tcfg)
    assert st == "US_TRIAL_LEAD" and prof["unresolved_questions"]
    # routing rationale must never assert direction or boundedness
    assert "routing_rationale" in prof
    for banned in ("有界", "过度", "下调"):
        assert banned not in prof["routing_rationale"]
    st, prof = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["post1"],
        {"primary": "non_reliance_restatement", "flags": [], "content_version": "v"}, tcfg)
    assert st == "US_TRIAL_REJECTED_NON_RELIANCE_RESTATEMENT"
    assert "reject_rationale" in prof
    st, _ = us_trial.assess_and_state(ev, rx, "TRIGGERED", ["post1"],
                                      {"primary": "delisting_compliance", "flags": [],
                                       "content_version": "v"}, tcfg)
    assert st == "US_TRIAL_NEAR_MISS"


def test_budget_deferred_is_research_pending_not_insufficient(conn, tcfg):
    """A TRIGGERED event pushed out by the deep-read budget is RESEARCH_PENDING;
    calling it insufficient data was the F2 mislabel."""
    ev = {"event_id": "e_b", "type": "ISSUER_8K", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    rx = {"state": "OK", "sessions": 80}
    st, prof = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["post1"], None, tcfg,
        content_state=us_trial.CONTENT_BUDGET_DEFERRED)
    assert st == "US_TRIAL_RESEARCH_PENDING"
    assert prof["content_state"] == "BUDGET_DEFERRED"


def test_fetch_failure_is_its_own_state(conn, tcfg):
    ev = {"event_id": "e_f", "type": "ISSUER_8K", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    rx = {"state": "OK", "sessions": 80}
    st, _ = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["post1"], None, tcfg,
        content_state=us_trial.CONTENT_FETCH_FAILED)
    assert st == "US_TRIAL_FETCH_FAILED"
    # no content_state at all (nothing more knowable) stays INSUFFICIENT_DATA
    st, _ = us_trial.assess_and_state(ev, rx, "TRIGGERED", ["post1"], None, tcfg)
    assert st == "US_TRIAL_INSUFFICIENT_DATA"


def test_hard_negative_alone_does_not_admit(conn, tcfg):
    """A hard-negative filing without a price trigger is NOT a candidate."""
    ev = {"event_id": "e2", "type": "LATE_FILING", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    st, _ = us_trial.assess_and_state(ev, {"state": "OK", "sessions": 80}, "NO_TRIGGER",
                                      [], None, tcfg)
    assert st == "US_TRIAL_REJECTED_NO_TRIGGER"


def test_candidate_upsert_idempotent(conn, tcfg):
    conn.execute("INSERT INTO company(company_id, created_asof)"
                 " VALUES('US:TT','2026-01-01T00:00:00Z')")
    conn.commit()
    profile = {"event_id": "ev_x", "reaction": {"t0_session": "2026-08-27"}}
    c1 = us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_NEAR_MISS", profile, "us_trial_v0")
    c2 = us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_CANDIDATE", profile, "us_trial_v0")
    assert c1 == c2
    assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM candidate").fetchone()[0] == "US_TRIAL_CANDIDATE"


def test_no_lookahead_event_selection(conn, tcfg):
    conn.execute("INSERT INTO company(company_id, cik, created_asof)"
                 " VALUES('US:TT','7000001','2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
                 " VALUES('NASDAQ:TT','US:TT','TT','NASDAQ','USD')")
    for eid, seen in (("ev_us_aaa", "2026-08-27T20:00:00Z"),
                      ("ev_us_bbb", "2026-08-29T02:00:00Z")):
        conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc, state)"
                     " VALUES(?, 'COMPANY','ISSUER_8K', ?, 'VERIFIED')", (eid, seen))
        conn.execute("INSERT INTO event_company(event_id, company_id) VALUES(?, 'US:TT')",
                     (eid,))
    conn.commit()
    evs = us_trial.select_events(conn, "2026-08-28")
    assert [e["event_id"] for e in evs] == ["ev_us_aaa"]  # later first_seen invisible


def test_filing_document_parse_and_content():
    html_doc = (b"<html><head><style>x{}</style></head><body><h1>Form 8-K</h1>"
                b"<p>Item 5.02. Departure of Directors; the Chief Financial Officer"
                b" resigned effective immediately.</p></body></html>")
    text = us_filing_docs.extract_text(html_doc)
    assert "resigned" in text and "<" not in text
    content = us_filing_docs.assess_content(text, "5.02,9.01")
    assert content["primary"] == "management_change"
    assert "management_change" in content["flags"]


def test_zero_lead_summary_shape_and_coverage(conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices",
                        lambda *a, **k: {"tickers_requested": 2, "listings_covered": 0,
                                         "listings_empty": 0, "benchmark_rows": 0,
                                         "manifest": "m"})
    summary = ut.run_trial(conn, None, tcfg, "2026-08-28")
    assert summary["counts"]["events_considered"] == 0
    assert summary["leads"] == [] and summary["near_misses"] == []
    assert summary["coverage"]["reconciled"] is True
    assert (tmp_path / "audit" / "us_trial_2026-08-28.json").exists()


def test_coverage_accounts_for_budget_deferral(conn, tcfg, tmp_path, monkeypatch):
    """Every considered event lands in exactly one coverage bucket; with a
    content budget of zero, triggered filing events become RESEARCH_PENDING
    and the summary reconciles (no silent drops)."""
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices",
                        lambda *a, **k: {"manifest": "m"})
    dates = _mk_series(conn, days=80, crash_at=79, crash=-0.10, vol_spike=5000000)
    conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc, state)"
                 " VALUES('ev_us_cv1','COMPANY','ISSUER_8K',?, 'VERIFIED')",
                 (f"{dates[-1]}T20:00:00Z",))
    conn.execute("INSERT INTO event_company(event_id, company_id)"
                 " VALUES('ev_us_cv1','US:TT')")
    conn.execute(
        "INSERT INTO sec_filing(accession, cik, form, is_amendment, filing_date,"
        " first_seen_at_utc, quality, manifest_id, event_id, accepted_at_utc)"
        " VALUES('acc-cv1','7000001','8-K',0,?,?, 'OK','m','ev_us_cv1',?)",
        (dates[-1], f"{dates[-1]}T20:00:00Z", f"{dates[-1]}T12:00:00Z"))
    conn.commit()
    summary = ut.run_trial(conn, None, tcfg, dates[-1], content_cap=0)
    cov = summary["coverage"]
    assert cov["events_considered"] == 1
    assert cov["triggered"] == 1
    assert cov["research_pending_budget_deferred"] == 1
    assert cov["genuine_missing_data"] == 0
    assert cov["reconciled"] is True
    row = conn.execute("SELECT state FROM candidate").fetchone()
    assert row["state"] == "US_TRIAL_RESEARCH_PENDING"
