"""US trial: dual-anchor reaction engine, event-anchored gates, episodes,
admission/rejection, lookahead, idempotency — including the PR-B
falsification battery for the defects the review confirmed (F1/F6/F7)."""


import pytest

from investment_tool import calendars_us, us_filing_docs, us_prices, us_trial
from investment_tool import config as config_mod
from investment_tool import reaction as reaction_mod


@pytest.fixture
def tcfg():
    # v0.2 = event-anchored gates + dual anchors + episodes (PR-B)
    return config_mod.load("us_trial_v0.2")


def _sessions(n: int, end: str = "2026-08-28") -> list[str]:
    """The last n real XNYS sessions ending at `end` (offline calendar)."""
    c = calendars_us.cal()
    sess = c.sessions_in_range("2026-01-02", end)
    return [s.strftime("%Y-%m-%d") for s in sess][-n:]


def _mk_series(conn, lid="NASDAQ:TT", sessions=None, moves=None, vol_spikes=None,
               company="US:TT", ticker="TT"):
    """Price series on REAL sessions. moves: {index: ret}; vol_spikes:
    {index: volume}. Flat SPY/QQQ so market adjustment is the identity."""
    conn.execute("INSERT OR IGNORE INTO company(company_id, cik, created_asof)"
                 " VALUES(?, '7000001','2026-01-01T00:00:00Z')", (company,))
    conn.execute("INSERT OR IGNORE INTO listing(listing_id, company_id, ticker,"
                 " exchange, currency) VALUES(?,?,?,'NASDAQ','USD')",
                 (lid, company, ticker))
    sessions = sessions or _sessions(80)
    moves = moves or {}
    vol_spikes = vol_spikes or {}
    px, prev = 100.0, None
    for i, d in enumerate(sessions):
        px *= (1 + moves.get(i, 0.001))
        vol = vol_spikes.get(i, 1000000)
        conn.execute(
            "INSERT OR REPLACE INTO security_day(listing_id, trade_date, close,"
            " adj_close, volume, ret, ret_basis, adj_method, currency, limit_state,"
            " provider, quality, manifest_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, d, f"{px:.4f}", f"{px:.4f}", str(vol),
             None if prev is None else px / prev - 1.0, "ADJ_CONSEC",
             "PROVIDER_ADJ", "USD", "FREE", "yfinance", "PROVISIONAL", "m"))
        for b in ("SPY", "QQQ"):
            conn.execute("INSERT OR REPLACE INTO benchmark_day(index_id, trade_date,"
                         " close, provider, quality, manifest_id)"
                         " VALUES(?,?,?, 'yfinance','PROVISIONAL','m')",
                         (b, d, "500.0"))
        prev = px
    conn.commit()
    return sessions


def _anchors(event_session, actionable=None, partial=False):
    return {"event_session": event_session, "same_session_partial": partial,
            "first_actionable_session": actionable or event_session,
            "session_relation": "PRE_OPEN", "precision": "TIME",
            "calendar": "XNYS"}


# ---------------------------------------------------- engine falsifications


def test_f1_pre_event_crash_with_post_event_rise_never_triggers(conn, tcfg):
    """Falsification #1 (review F1, the ABVC case): a crash 10 sessions
    BEFORE the event followed by a post-event RISE must not become a negative
    event reaction — under the old trailing windows it did."""
    sess = _mk_series(conn, moves={69: -0.35, 79: +0.04})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    assert rx["mkt_adj_post_ret1"] == pytest.approx(0.039, abs=1e-3)
    # the old defect is visible in the diagnostic, but diagnostics cannot gate
    assert rx["mkt_adj_asof_trail_ret21"] < -0.20
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate not in ("TRIGGERED", "NEAR_MISS")
    assert hits == [] or hits == ["evt1_pos"]


def test_f2_next_session_decline_lands_in_car5_not_evt1(conn, tcfg):
    """Falsification #2 (the KTCC pattern): flat on the event session, crash
    on the following session — the reaction belongs to the post-event window
    (car5), not the event-session leg."""
    sess = _mk_series(conn, moves={75: 0.0, 76: -0.20})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[75]), sess[-1])
    assert abs(rx["mkt_adj_post_ret1"]) < 0.01
    assert rx["mkt_adj_car5"] < -0.15
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED" and hits == ["car5"]


def test_f10_forward_return_starts_at_decision_anchor(conn, tcfg):
    """Falsification #7/#9/#10: event on session 75, system actionable only on
    session 77 — the crash on 76 is 'realized before entry', and the forward
    return from the decision anchor excludes it."""
    sess = _mk_series(conn, moves={75: -0.10, 76: -0.10, 78: +0.05})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[75], actionable=sess[77]), sess[-1])
    # realized before entry: sessions 75..77 relative to the 74 baseline
    assert rx["realized_before_entry"] < -0.18
    # forward from entry close (77) through asof: only the +5% on 78 and drift
    assert rx["forward_from_decision"] == pytest.approx(
        (1.05 * 1.001 * 1.001) - 1, abs=5e-3)
    assert rx["entry_session"] == sess[77]
    # anchors carried on the record
    assert rx["anchors"]["event_session"] == sess[75]
    assert rx["anchors"]["first_actionable_session"] == sess[77]


def test_f11_positive_event_move_stays_visible(conn, tcfg):
    """Falsification #11: a strong positive event-session move is recorded as
    POSITIVE_MOVE — distinct from NO_TRIGGER, never silently discarded."""
    sess = _mk_series(conn, moves={79: +0.12})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "POSITIVE_MOVE" and hits == ["evt1_pos"]
    ev = {"event_id": "e", "type": "ISSUER_8K", "ticker": "TT", "accession": "a",
          "accepted_at_utc": None, "first_seen_at_utc": "x"}
    st, _ = us_trial.assess_and_state(ev, rx, gate, hits, None, tcfg)
    assert st == "US_TRIAL_OBSERVED_POSITIVE_MOVE"


def test_f12_sharp_negative_move_alone_is_never_a_lead(conn, tcfg):
    """Falsification #12: a -20% event-session crash without an eligible
    content category must not become a lead."""
    sess = _mk_series(conn, moves={79: -0.20})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED"
    ev = {"event_id": "e", "type": "ISSUER_8K", "ticker": "TT", "accession": "a",
          "accepted_at_utc": None, "first_seen_at_utc": "x"}
    st, _ = us_trial.assess_and_state(
        ev, rx, gate, hits,
        {"primary": "neutral_or_unclassified", "flags": [], "content_version": "v"},
        tcfg)
    assert st == "US_TRIAL_REJECTED_CONTENT_UNCLASSIFIED"
    st, _ = us_trial.assess_and_state(ev, rx, gate, hits, None, tcfg,
                                      content_state=us_trial.CONTENT_BUDGET_DEFERRED)
    assert st == "US_TRIAL_RESEARCH_PENDING"


def test_intra_session_release_flags_contaminated_window(conn, tcfg):
    """Falsification #4: an intra-session release keeps same_session_partial
    and the reaction record carries event_window_contaminated."""
    sess = _mk_series(conn, moves={79: -0.10})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79], partial=True), sess[-1])
    assert rx["event_window_contaminated"] is True


def test_run_up_is_a_feature_not_a_trigger(conn, tcfg):
    """A -25% pre-event run-down with a quiet event session must not trigger;
    the run-down is exposed as a feature for the expectation layer."""
    sess = _mk_series(conn, moves={70: -0.25})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    assert rx["mkt_adj_run_up_21"] < -0.20
    gate, _hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate in ("NO_TRIGGER", "NEAR_MISS")


def test_gate_triggers_and_near_miss(conn, tcfg):
    sess = _mk_series(conn, moves={79: -0.10}, vol_spikes={79: 5000000})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    gate, hits = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "TRIGGERED" and "evt1" in hits and "volume" in hits
    # near-miss band: 5.5% < 7% but >= 70% of it
    conn.execute("DELETE FROM security_day")
    conn.execute("DELETE FROM benchmark_day")
    sess = _mk_series(conn, moves={79: -0.055})
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors(sess[79]), sess[-1])
    gate, _ = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "NEAR_MISS"


def test_missing_prices_and_pending_states(conn, tcfg):
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:NONE", _anchors("2026-08-27"), "2026-08-28")
    assert rx["state"] == "NO_PRICES"
    gate, _ = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "INSUFFICIENT_DATA"
    # event session after asof -> POST_EVENT_PENDING, its own state
    sess = _mk_series(conn)
    rx = reaction_mod.compute_event_reaction(
        conn, "NASDAQ:TT", _anchors("2027-01-05"), sess[-1])
    assert rx["post_state"] == "POST_EVENT_PENDING"
    gate, _ = us_trial.evaluate_gates(rx, tcfg)
    assert gate == "POST_EVENT_PENDING"
    ev = {"event_id": "e", "type": "ISSUER_8K", "ticker": "TT", "accession": "a",
          "accepted_at_utc": None, "first_seen_at_utc": "x"}
    st, _ = us_trial.assess_and_state(ev, rx, gate, [], None, tcfg)
    assert st == "US_TRIAL_POST_EVENT_PENDING"


# --------------------------------------------------------------- episodes


def test_f8_same_company_events_form_one_episode(conn, tcfg):
    """Falsification #8: two filings for one company within the window form
    ONE episode; the triggered member is primary, the other is a member."""
    sess = _mk_series(conn, moves={78: -0.12})
    evaluated = []
    for eid, s_idx in (("ev_a", 77), ("ev_b", 78)):
        rx = reaction_mod.compute_event_reaction(
            conn, "NASDAQ:TT", _anchors(sess[s_idx]), sess[-1])
        g, hits = us_trial.evaluate_gates(rx, tcfg)
        evaluated.append({"event_id": eid, "company_id": "US:TT",
                          "listing_id": "NASDAQ:TT", "ticker": "TT",
                          "accepted_at_utc": None,
                          "anchors": _anchors(sess[s_idx]), "rx": rx,
                          "gate": g, "hits": hits})
    us_trial.group_episodes(evaluated, window_sessions=5)
    eps = {e["event_id"]: e["episode"] for e in evaluated}
    assert eps["ev_a"]["episode_id"] == eps["ev_b"]["episode_id"]
    assert eps["ev_b"]["is_primary"] is True      # the triggered member leads
    assert eps["ev_a"]["is_primary"] is False
    assert eps["ev_b"]["member_count"] == 2


def test_far_apart_events_stay_separate_episodes(conn, tcfg):
    sess = _mk_series(conn)
    evaluated = []
    for eid, s_idx in (("ev_a", 30), ("ev_b", 78)):
        rx = reaction_mod.compute_event_reaction(
            conn, "NASDAQ:TT", _anchors(sess[s_idx]), sess[-1])
        g, hits = us_trial.evaluate_gates(rx, tcfg)
        evaluated.append({"event_id": eid, "company_id": "US:TT",
                          "listing_id": "NASDAQ:TT", "ticker": "TT",
                          "accepted_at_utc": None,
                          "anchors": _anchors(sess[s_idx]), "rx": rx,
                          "gate": g, "hits": hits})
    us_trial.group_episodes(evaluated, window_sessions=5)
    eps = {e["event_id"]: e["episode"] for e in evaluated}
    assert eps["ev_a"]["episode_id"] != eps["ev_b"]["episode_id"]


# ----------------------------------------------- admission and state rules


def test_admission_requires_eligible_category(conn, tcfg):
    ev = {"event_id": "e1", "type": "ISSUER_8K", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    rx = {"state": "OK", "sessions": 80, "post_state": "OK"}
    st, prof = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["evt1"],
        {"primary": "management_change", "flags": [], "content_version": "v"}, tcfg)
    assert st == "US_TRIAL_LEAD" and prof["unresolved_questions"]
    assert "routing_rationale" in prof
    for banned in ("有界", "过度", "下调"):
        assert banned not in prof["routing_rationale"]
    st, prof = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["evt1"],
        {"primary": "non_reliance_restatement", "flags": [], "content_version": "v"}, tcfg)
    assert st == "US_TRIAL_REJECTED_NON_RELIANCE_RESTATEMENT"
    assert "reject_rationale" in prof
    st, _ = us_trial.assess_and_state(ev, rx, "TRIGGERED", ["evt1"],
                                      {"primary": "delisting_compliance", "flags": [],
                                       "content_version": "v"}, tcfg)
    assert st == "US_TRIAL_NEAR_MISS"


def test_budget_deferred_is_research_pending_not_insufficient(conn, tcfg):
    ev = {"event_id": "e_b", "type": "ISSUER_8K", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    rx = {"state": "OK", "sessions": 80, "post_state": "OK"}
    st, prof = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["evt1"], None, tcfg,
        content_state=us_trial.CONTENT_BUDGET_DEFERRED)
    assert st == "US_TRIAL_RESEARCH_PENDING"
    assert prof["content_state"] == "BUDGET_DEFERRED"


def test_fetch_failure_is_its_own_state(conn, tcfg):
    ev = {"event_id": "e_f", "type": "ISSUER_8K", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    rx = {"state": "OK", "sessions": 80, "post_state": "OK"}
    st, _ = us_trial.assess_and_state(
        ev, rx, "TRIGGERED", ["evt1"], None, tcfg,
        content_state=us_trial.CONTENT_FETCH_FAILED)
    assert st == "US_TRIAL_FETCH_FAILED"
    st, _ = us_trial.assess_and_state(ev, rx, "TRIGGERED", ["evt1"], None, tcfg)
    assert st == "US_TRIAL_INSUFFICIENT_DATA"


def test_hard_negative_alone_does_not_admit(conn, tcfg):
    ev = {"event_id": "e2", "type": "LATE_FILING", "ticker": "TT",
          "accession": "a", "accepted_at_utc": None, "first_seen_at_utc": "x"}
    st, _ = us_trial.assess_and_state(ev, {"state": "OK", "sessions": 80,
                                           "post_state": "OK"},
                                      "NO_TRIGGER", [], None, tcfg)
    assert st == "US_TRIAL_REJECTED_NO_TRIGGER"


def test_candidate_upsert_idempotent(conn, tcfg):
    conn.execute("INSERT INTO company(company_id, created_asof)"
                 " VALUES('US:TT','2026-01-01T00:00:00Z')")
    conn.commit()
    profile = {"event_id": "ev_x", "reaction": {"t0_session": "2026-08-27"}}
    c1 = us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_NEAR_MISS", profile,
                                    "us_trial_v0.2")
    c2 = us_trial._upsert_candidate(conn, "US:TT", "US_TRIAL_LEAD", profile,
                                    "us_trial_v0.2")
    assert c1 == c2
    assert conn.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 1
    assert conn.execute("SELECT state FROM candidate").fetchone()[0] == "US_TRIAL_LEAD"


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


# ------------------------------------------------------- run_trial shapes


def test_zero_lead_summary_shape_and_coverage(conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices",
                        lambda *a, **k: {"tickers_requested": 2, "listings_covered": 0,
                                         "listings_empty": 0, "benchmark_rows": 0,
                                         "manifest": "m"})
    summary = ut.run_trial(conn, None, tcfg, "2026-08-28",
                           http_factory=lambda: object())
    assert summary["counts"]["events_considered"] == 0
    assert summary["leads"] == [] and summary["near_misses"] == []
    assert summary["coverage"]["reconciled"] is True
    assert (tmp_path / "audit" / "us_trial_2026-08-28.json").exists()


def test_coverage_accounts_for_budget_deferral(conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices", lambda *a, **k: {"manifest": "m"})
    sess = _mk_series(conn, moves={79: -0.10}, vol_spikes={79: 5000000})
    last = sess[-1]
    conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc, state)"
                 " VALUES('ev_us_cv1','COMPANY','ISSUER_8K',?, 'VERIFIED')",
                 (f"{last}T21:00:00Z",))
    conn.execute("INSERT INTO event_company(event_id, company_id)"
                 " VALUES('ev_us_cv1','US:TT')")
    conn.execute(
        "INSERT INTO sec_filing(accession, cik, form, is_amendment, filing_date,"
        " first_seen_at_utc, quality, manifest_id, event_id, accepted_at_utc)"
        " VALUES('acc-cv1','7000001','8-K',0,?,?, 'OK','m','ev_us_cv1',?)",
        (last, f"{last}T21:00:00Z", f"{last}T12:00:00Z"))
    conn.commit()
    summary = ut.run_trial(conn, None, tcfg, last, content_cap=0,
                           http_factory=lambda: object())
    cov = summary["coverage"]
    assert cov["events_considered"] == 1
    assert cov["triggered"] == 1
    assert cov["research_pending_budget_deferred"] == 1
    assert cov["genuine_missing_data"] == 0
    assert cov["reconciled"] is True
    row = conn.execute("SELECT state FROM candidate").fetchone()
    assert row["state"] == "US_TRIAL_RESEARCH_PENDING"


def test_run_trial_consolidates_episode_members(conn, tcfg, tmp_path, monkeypatch):
    """End-to-end #8: two triggered filings for one company in one episode
    produce ONE lead-track item plus one visible episode member."""
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices", lambda *a, **k: {"manifest": "m"})

    def fake_fetch(conn_, cfg, http, accession):
        p = tmp_path / f"{accession}.txt"
        p.write_text("Item 5.02 departure; the Chief Financial Officer resigned.")
        return {"state": "FETCHED", "text_path": str(p)}

    monkeypatch.setattr(ut.us_filing_docs, "fetch_primary_document", fake_fetch)
    sess = _mk_series(conn, moves={78: -0.12, 79: -0.08})
    last = sess[-1]
    for eid, acc, accepted in (("ev_us_m1", "acc-m1", f"{sess[78]}T12:00:00Z"),
                               ("ev_us_m2", "acc-m2", f"{sess[79]}T12:00:00Z")):
        conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc,"
                     " state) VALUES(?, 'COMPANY','ISSUER_8K', ?, 'VERIFIED')",
                     (eid, f"{last}T21:00:00Z"))
        conn.execute("INSERT INTO event_company(event_id, company_id)"
                     " VALUES(?, 'US:TT')", (eid,))
        conn.execute(
            "INSERT INTO sec_filing(accession, cik, form, is_amendment, filing_date,"
            " first_seen_at_utc, quality, manifest_id, event_id, accepted_at_utc,"
            " items_csv, primary_doc_name)"
            " VALUES(?, '7000001','8-K',0,?,?, 'OK','m',?,?, '5.02','d.htm')",
            (acc, last, f"{last}T21:00:00Z", eid, accepted))
    conn.commit()
    summary = ut.run_trial(conn, None, tcfg, last, http_factory=lambda: object())
    cov = summary["coverage"]
    assert cov["triggered"] == 2
    assert cov["episode_members"] == 1
    assert cov["leads"] == 1
    assert cov["reconciled"] is True
    member = conn.execute("SELECT state, profile_json FROM candidate"
                          " WHERE state='US_TRIAL_EPISODE_MEMBER'").fetchone()
    assert member is not None
    import json as json_mod
    prof = json_mod.loads(member["profile_json"])
    assert prof["episode"]["is_primary"] is False
    assert prof["episode"]["primary_event_id"] in ("ev_us_m1", "ev_us_m2")
