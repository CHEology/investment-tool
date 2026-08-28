from pathlib import Path

from investment_tool.providers import sec
from investment_tool.us_map import cik_for, reconcile, sync_cik_map

FIX = Path(__file__).parent / "fixtures" / "sec"


def _rows():
    return sec.parse_company_tickers_exchange(
        (FIX / "company_tickers_exchange_sample.json").read_bytes()
    )


def test_parse_and_states(conn):
    rows = _rows()
    assert {r["ticker"] for r in rows} >= {"ALPH", "MCM", "MCM.A", "DUPE", "OTCX", "BETA"}
    result = sync_cik_map(conn, rows, "2026-08-28", "sec_tickers_exchange")
    assert result["opened"] == 7
    states = {(r["ticker"]): r["state"] for r in conn.execute(
        "SELECT ticker, state FROM cik_map WHERE valid_to_date IS NULL")}
    assert states["MCM"] == "MULTI_CLASS" and states["MCM.A"] == "MULTI_CLASS"
    assert states["DUPE"] == "TICKER_CONFLICT"
    assert states["ALPH"] == "OK"


def test_versioning_never_rewrites_history(conn):
    rows = _rows()
    sync_cik_map(conn, rows, "2026-08-01", "sec")
    # ALPH moves exchange -> old interval closed, new row opened
    moved = [dict(r, exchange="NYSE") if r["ticker"] == "ALPH" else r for r in rows]
    sync_cik_map(conn, moved, "2026-08-20", "sec")
    alph = conn.execute(
        "SELECT * FROM cik_map WHERE ticker='ALPH' ORDER BY valid_from_date").fetchall()
    assert len(alph) == 2
    assert alph[0]["valid_to_date"] == "2026-08-20" and alph[0]["exchange"] == "Nasdaq"
    assert alph[1]["valid_to_date"] is None and alph[1]["exchange"] == "NYSE"
    # PIT resolution
    assert cik_for(conn, "ALPH", "2026-08-10")[0]["exchange"] == "Nasdaq"
    assert cik_for(conn, "ALPH", "2026-08-25")[0]["exchange"] == "NYSE"
    # first disappearance only SUSPECTS (two-strike; closure tested separately)
    gone = [r for r in moved if r["ticker"] != "OTCX"]
    sync_cik_map(conn, gone, "2026-08-28", "sec")
    otcx = conn.execute("SELECT valid_to_date, state FROM cik_map"
                        " WHERE ticker='OTCX'").fetchone()
    assert otcx["valid_to_date"] is None and otcx["state"] == "STALE_SUSPECTED"


def _seed_universe(conn):
    for cid, ticker, exchange in (
        ("US:ALPH", "ALPH", "NASDAQ"), ("US:BETA", "BETA", "NYSE"),
        ("US:MCM", "MCM", "NASDAQ"), ("US:ONLYUS", "ONLYUS", "NYSE"),
    ):
        conn.execute("INSERT INTO company(company_id, name_en, created_asof)"
                     " VALUES(?,?, '2026-01-01T00:00:00Z')", (cid, ticker))
        conn.execute(
            "INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
            " VALUES(?,?,?,?, 'USD')", (f"{exchange}:{ticker}", cid, ticker, exchange))
    conn.commit()


def test_reconcile_states_and_enrichment(conn):
    _seed_universe(conn)
    sync_cik_map(conn, _rows(), "2026-08-28", "sec")
    hist = reconcile(conn, "2026-08-28")
    assert hist["MATCHED"] == 3            # ALPH, BETA, MCM
    assert hist["TICKER_CONFLICT_UNMATCHED"] == 2  # both DUPE rows refused, no guessing
    assert hist["NOT_IN_UNIVERSE_EXCHANGE"] == 1   # OTCX (no exchange)
    assert hist["NOT_IN_UNIVERSE_TICKER"] == 1     # MCM.A listing absent from universe
    assert hist["UNIVERSE_CIK_UNRESOLVED"] == 1    # ONLYUS has no SEC row
    cik = conn.execute("SELECT cik FROM company WHERE company_id='US:ALPH'").fetchone()["cik"]
    assert cik == "1000001"


def test_user_agent_gate_rejects_placeholder(monkeypatch):
    import pytest

    monkeypatch.setenv("SEC_USER_AGENT", "Research contact@example.com")
    with pytest.raises(sec.SecConfigError):
        sec.require_user_agent()
    monkeypatch.setenv("SEC_USER_AGENT", "")
    with pytest.raises(sec.SecConfigError):
        sec.require_user_agent()
    monkeypatch.setenv("SEC_USER_AGENT", "Jane Doe Research jd@realdomain.net")
    assert sec.require_user_agent().startswith("Jane Doe")


def test_rate_limiter_paces_without_wall_clock():
    clock = {"t": 0.0}
    sleeps = []

    def fake_clock():
        return clock["t"]

    def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    lim = sec.GlobalRateLimiter(rate=4.0, burst=2, clock=fake_clock, sleeper=fake_sleep)
    for _ in range(6):
        lim.acquire()
    # 2 burst tokens free, then 4/s pacing -> total sleep ~= 1.0s for 4 more
    assert abs(sum(sleeps) - 1.0) < 0.05


def test_transient_disappearance_never_closes_interval(conn):
    rows = _rows()
    sync_cik_map(conn, rows, "2026-08-01", "sec")
    without_beta = [r for r in rows if r["ticker"] != "BETA"]
    r2 = sync_cik_map(conn, without_beta, "2026-08-02", "sec")
    assert r2["stale_marked"] == 1
    beta = conn.execute("SELECT * FROM cik_map WHERE ticker='BETA'").fetchone()
    assert beta["valid_to_date"] is None and beta["state"] == "STALE_SUSPECTED"
    # reappears -> suspicion cleared, interval still open, no history rewrite
    r3 = sync_cik_map(conn, rows, "2026-08-03", "sec")
    assert r3["stale_recovered"] == 1
    beta = conn.execute("SELECT * FROM cik_map WHERE ticker='BETA'").fetchone()
    assert beta["valid_to_date"] is None and beta["state"] == "OK"
    assert conn.execute("SELECT COUNT(*) FROM cik_map WHERE ticker='BETA'").fetchone()[0] == 1


def test_two_strike_absence_closes_and_ticker_reuse_reopens(conn):
    rows = _rows()
    sync_cik_map(conn, rows, "2026-08-01", "sec")
    without_beta = [r for r in rows if r["ticker"] != "BETA"]
    sync_cik_map(conn, without_beta, "2026-08-02", "sec")   # strike 1: suspected
    sync_cik_map(conn, without_beta, "2026-08-05", "sec")   # strike 2: closed
    beta = conn.execute("SELECT * FROM cik_map WHERE ticker='BETA'"
                        " ORDER BY valid_from_date").fetchall()
    assert len(beta) == 1 and beta[0]["valid_to_date"] == "2026-08-05"
    # ticker reused later by a DIFFERENT cik -> new open interval, old preserved
    reused = without_beta + [{"cik": "9999999", "name": "NEW BETA CO",
                              "ticker": "BETA", "exchange": "NYSE"}]
    sync_cik_map(conn, reused, "2026-08-10", "sec")
    beta = conn.execute("SELECT cik, valid_from_date, valid_to_date FROM cik_map"
                        " WHERE ticker='BETA' ORDER BY valid_from_date").fetchall()
    assert len(beta) == 2
    assert beta[0]["valid_to_date"] == "2026-08-05"
    assert beta[1]["cik"] == "9999999" and beta[1]["valid_to_date"] is None
