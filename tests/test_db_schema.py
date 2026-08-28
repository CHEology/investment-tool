from investment_tool.numeric import dec, dec_from_db, dec_text


def test_all_core_tables_exist(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    required = {
        "manifest", "config_version", "company", "listing", "alias", "security_day",
        "benchmark_day", "fx_day", "calendar_day", "industry_snapshot", "announcement",
        "observation", "event", "event_company", "evidence", "candidate", "search_plan",
        "frozen_artifact", "validation_snapshot",
    }
    assert required <= tables


def test_security_day_decimal_round_trip(conn):
    conn.execute(
        "INSERT INTO company(company_id, created_asof) VALUES('CN:300274','2026-08-28T00:00:00Z')")
    conn.execute(
        "INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
        " VALUES('SZSE:300274','CN:300274','300274','SZSE','CNY')")
    conn.execute(
        "INSERT INTO security_day(listing_id, trade_date, close, volume, amount, currency,"
        " provider, quality, manifest_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("SZSE:300274", "2026-08-28", dec_text(dec("97.69")), dec_text(dec("616243")),
         dec_text(dec("6025872209.95")), "CNY", "eastmoney", "PROVISIONAL", "m1"),
    )
    row = conn.execute(
        "SELECT close, amount FROM security_day WHERE listing_id='SZSE:300274'").fetchone()
    assert dec_from_db(row["close"]) == dec("97.69")
    assert str(dec_from_db(row["amount"])) == "6025872209.95"


def test_missing_close_stays_null_not_zero(conn):
    conn.execute(
        "INSERT INTO company(company_id, created_asof) VALUES('CN:X','2026-08-28T00:00:00Z')")
    conn.execute(
        "INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
        " VALUES('SSE:X','CN:X','X','SSE','CNY')")
    conn.execute(
        "INSERT INTO security_day(listing_id, trade_date, close, currency, provider, quality,"
        " manifest_id) VALUES('SSE:X','2026-08-28',NULL,'CNY','test','PARTIAL','m1')")
    row = conn.execute("SELECT close FROM security_day WHERE listing_id='SSE:X'").fetchone()
    assert row["close"] is None
