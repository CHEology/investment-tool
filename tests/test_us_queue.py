"""PR-A: rank-before-budget, persistent research queue, resumable processing.

The falsifiable core: with a budget of 1 and two triggered events where the
FIRST-SEEN one is the WEAKER signal, the stronger event must win the read
slot (first-seen order deciding the budget was the F2 defect)."""

import json

import pytest

from investment_tool import config as config_mod
from investment_tool import ranking, us_queue

# ---------------------------------------------------------------- fixtures


@pytest.fixture
def tcfg():
    return config_mod.load("us_trial_v0.3")


def _mk_company(conn, cid, ticker, lid):
    conn.execute("INSERT OR IGNORE INTO company(company_id, cik, created_asof)"
                 " VALUES(?,?, '2026-01-01T00:00:00Z')", (cid, "70000" + ticker))
    conn.execute("INSERT OR IGNORE INTO listing(listing_id, company_id, ticker, exchange,"
                 " currency) VALUES(?,?,?,'NASDAQ','USD')", (lid, cid, ticker))


def _sessions(n=80, end="2026-08-28"):
    from investment_tool import calendars_us
    c = calendars_us.cal()
    return [s.strftime("%Y-%m-%d")
            for s in c.sessions_in_range("2026-01-02", end)][-n:]


def _mk_series(conn, lid, days=80, crash=-0.10, vol_spike=None):
    """Real-session series ending with a crash on the LAST session."""
    px, prev = 100.0, None
    sess = _sessions(days)
    for i, d in enumerate(sess):
        ret = crash if i == days - 1 else 0.001
        px *= (1 + ret)
        vol = vol_spike if (vol_spike and i == days - 1) else 1000000
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
    return sess


def _mk_event(conn, event_id, cid, accession, filing_date, first_seen, accepted,
              primary_doc="doc.htm"):
    conn.execute("INSERT INTO event(event_id, scope, type, first_seen_at_utc, state)"
                 " VALUES(?, 'COMPANY','ISSUER_8K', ?, 'VERIFIED')",
                 (event_id, first_seen))
    conn.execute("INSERT INTO event_company(event_id, company_id) VALUES(?,?)",
                 (event_id, cid))
    conn.execute(
        "INSERT INTO sec_filing(accession, cik, form, is_amendment, filing_date,"
        " first_seen_at_utc, quality, manifest_id, event_id, accepted_at_utc,"
        " items_csv, primary_doc_name)"
        " VALUES(?, '7000001','8-K',0,?,?, 'OK','m',?,?, '5.02', ?)",
        (accession, filing_date, first_seen, event_id, accepted, primary_doc))
    conn.commit()


def _two_triggered_events(conn):
    """WEAK (ticker AAW) is first-seen EARLIER; STRONG (ticker ZZS) later.
    Under first-seen budgeting AAW would win the slot; under ranking ZZS must."""
    _mk_company(conn, "US:AAW", "AAW", "NASDAQ:AAW")
    _mk_company(conn, "US:ZZS", "ZZS", "NASDAQ:ZZS")
    dates = _mk_series(conn, "NASDAQ:AAW", crash=-0.08)          # weak trigger
    _mk_series(conn, "NASDAQ:ZZS", crash=-0.25, vol_spike=9000000)  # strong
    last = dates[-1]
    _mk_event(conn, "ev_us_weak", "US:AAW", "acc-weak", last,
              f"{last}T10:00:00Z", f"{last}T11:00:00Z")
    _mk_event(conn, "ev_us_strong", "US:ZZS", "acc-strong", last,
              f"{last}T20:00:00Z", f"{last}T11:00:00Z")
    return last


def _fake_fetch(tmp_path, text="Item 5.02 departure of the Chief Financial Officer"
                               " who resigned."):
    """Monkeypatch stand-in for fetch_primary_document: writes a text file and
    reports FETCHED without any network."""
    def fetch(conn, cfg, http, accession):
        p = tmp_path / f"{accession}.txt"
        p.write_text(text)
        return {"state": "FETCHED", "text_path": str(p)}
    return fetch


# ---------------------------------------------------------------- ranking


def test_rank_v0_is_deterministic_and_explained():
    rx_strong = {"mkt_adj_post_ret1": -0.20, "mkt_adj_post_cum": -0.25,
                 "volume_ratio": 8.0}
    rx_weak = {"mkt_adj_post_ret1": -0.08, "mkt_adj_post_cum": -0.08,
               "volume_ratio": 1.0}
    items = [
        {"event_id": "e1", "ticker": "AAA", "rx": rx_weak, "hits": ["post1"]},
        {"event_id": "e2", "ticker": "BBB", "rx": rx_strong,
         "hits": ["post1", "cum5", "volume"]},
    ]
    ranked = ranking.rank_events(items)
    assert [it["event_id"] for it in ranked] == ["e2", "e1"]
    r = ranked[0]["rank"]
    assert r["version"] == ranking.RANK_VERSION
    assert set(r["components"]) == {"event_1d", "event_cum", "volume", "legs"}
    # deterministic tie-break: identical inputs order by ticker then event_id
    tie = ranking.rank_events([
        {"event_id": "e9", "ticker": "TTT", "rx": rx_weak, "hits": ["post1"]},
        {"event_id": "e8", "ticker": "TTT", "rx": rx_weak, "hits": ["post1"]},
    ])
    assert [it["event_id"] for it in tie] == ["e8", "e9"]


def test_rank_v0_ignores_trailing_windows():
    """Trailing asof windows (the F1 defect) must not raise a rank score."""
    rx_flat = {"mkt_adj_post_ret1": -0.10, "mkt_adj_post_cum": -0.10,
               "volume_ratio": 2.0}
    rx_trail = dict(rx_flat, mkt_adj_ret21=-0.60, mkt_adj_ret63=-0.80)
    s_flat = ranking.score_event(rx_flat, ["post1"])
    s_trail = ranking.score_event(rx_trail, ["post1"])
    assert s_flat["score"] == s_trail["score"]


# ------------------------------------------------- rank-before-budget core


def test_budget_goes_to_best_ranked_not_first_seen(conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_prices
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices", lambda *a, **k: {"manifest": "m"})
    monkeypatch.setattr(ut.us_filing_docs, "fetch_primary_document",
                        _fake_fetch(tmp_path))
    last = _two_triggered_events(conn)
    summary = ut.run_trial(conn, None, tcfg, last, content_cap=1,
                           http_factory=lambda: object())
    cov = summary["coverage"]
    assert cov["triggered"] == 2 and cov["ranked"] == 2
    assert cov["documents_reviewed"] == 1
    assert cov["research_pending_budget_deferred"] == 1
    assert cov["reconciled"] is True
    strong = conn.execute(
        "SELECT state FROM candidate WHERE json_extract(profile_json,'$.ticker')='ZZS'"
    ).fetchone()
    weak = conn.execute(
        "SELECT state FROM candidate WHERE json_extract(profile_json,'$.ticker')='AAW'"
    ).fetchone()
    assert strong["state"] == "US_TRIAL_LEAD"            # read despite later first_seen
    assert weak["state"] == "US_TRIAL_RESEARCH_PENDING"  # deferred, not dropped
    q = {r["ticker"]: r for r in conn.execute(
        "SELECT ticker, state, rank_score FROM research_queue").fetchall()}
    assert q["ZZS"]["state"] == "DOC_REVIEW_COMPLETED"
    assert q["AAW"]["state"] == "RESEARCH_PENDING"
    assert q["ZZS"]["rank_score"] > q["AAW"]["rank_score"]


# ------------------------------------------------------- queue semantics


def test_enqueue_never_downgrades_terminal_states(conn):
    _mk_company(conn, "US:TT", "TT", "NASDAQ:TT")
    kw = dict(event_id="ev1", candidate_id="c1", company_id="US:TT",
              listing_id="NASDAQ:TT", ticker="TT", asof="2026-08-28",
              config_version="us_trial_v0.1")
    us_queue.enqueue(conn, state="DOC_REVIEW_COMPLETED", rank={"score": 0.5}, **kw)
    us_queue.enqueue(conn, state="RESEARCH_PENDING", rank={"score": 0.9}, **kw)
    row = conn.execute("SELECT state, rank_score FROM research_queue").fetchone()
    assert row["state"] == "DOC_REVIEW_COMPLETED"   # protected
    assert row["rank_score"] == 0.9               # rank still refreshes
    # non-terminal states do move
    us_queue.enqueue(conn, state="FETCH_FAILED", rank={"score": 0.9},
                     **{**kw, "event_id": "ev2"})
    us_queue.enqueue(conn, state="RESEARCH_PENDING", rank={"score": 0.9},
                     **{**kw, "event_id": "ev2"})
    st = conn.execute("SELECT state FROM research_queue WHERE event_id='ev2'").fetchone()
    assert st["state"] == "RESEARCH_PENDING"


def test_process_queue_resumes_and_upgrades_candidate(conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_prices
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices", lambda *a, **k: {"manifest": "m"})
    monkeypatch.setattr(ut.us_filing_docs, "fetch_primary_document",
                        _fake_fetch(tmp_path))
    last = _two_triggered_events(conn)
    ut.run_trial(conn, None, tcfg, last, content_cap=1,
                 http_factory=lambda: object())   # AAW deferred
    audit = us_queue.process_queue(conn, tcfg, limit=5,
                                   http_factory=lambda: object())
    assert [p["outcome"] for p in audit["processed"]] == ["US_TRIAL_LEAD"]
    weak = conn.execute(
        "SELECT state, profile_json FROM candidate"
        " WHERE json_extract(profile_json,'$.ticker')='AAW'").fetchone()
    assert weak["state"] == "US_TRIAL_LEAD"
    assert json.loads(weak["profile_json"])["content"]["primary"] == "management_change"
    q = conn.execute("SELECT state, attempts FROM research_queue"
                     " WHERE ticker='AAW'").fetchone()
    assert q["state"] == "DOC_REVIEW_COMPLETED" and q["attempts"] == 1
    # nothing left to process; rerun is a no-op
    audit2 = us_queue.process_queue(conn, tcfg, limit=5,
                                    http_factory=lambda: object())
    assert audit2["processed"] == []


def test_process_queue_records_fetch_failure(conn, tcfg, tmp_path, monkeypatch):
    from investment_tool import us_prices
    from investment_tool import us_trial as ut

    monkeypatch.setattr(ut, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(us_prices, "ensure_prices", lambda *a, **k: {"manifest": "m"})
    monkeypatch.setattr(ut.us_filing_docs, "fetch_primary_document",
                        lambda *a, **k: {"state": "ERROR http=503"})
    last = _two_triggered_events(conn)
    ut.run_trial(conn, None, tcfg, last, content_cap=0,
                 http_factory=lambda: object())   # both deferred
    audit = us_queue.process_queue(conn, tcfg, limit=1,
                                   http_factory=lambda: object())
    assert audit["processed"][0]["outcome"] == "US_TRIAL_FETCH_FAILED"
    q = conn.execute("SELECT state, attempts, last_error FROM research_queue"
                     " WHERE state='FETCH_FAILED'").fetchone()
    assert q is not None and q["attempts"] == 1 and "503" in q["last_error"]


def test_research_queue_table_migrates(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "research_queue" in tables
    assert conn.execute("SELECT 1 FROM schema_migration"
                        " WHERE migration_id='pr_a_research_queue'").fetchone()
