import sqlite3

from investment_tool.db import connect
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


def test_market_snapshot_table_exists(conn):
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(market_snapshot)").fetchall()}
    assert {"listing_id", "asof_date", "total_mcap", "float_mcap", "industry", "is_st"} <= cols


def test_s0_database_is_migrated_without_losing_rows(tmp_path):
    """CREATE TABLE IF NOT EXISTS alone cannot upgrade an existing S0 DB."""
    path = tmp_path / "investment.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE security_day(
          listing_id TEXT NOT NULL, trade_date TEXT NOT NULL,
          open TEXT, high TEXT, low TEXT, close TEXT, prev_close TEXT,
          volume TEXT, amount TEXT, pct_chg REAL, adj_factor TEXT,
          adj_method TEXT NOT NULL DEFAULT 'NONE', currency TEXT NOT NULL,
          limit_state TEXT NOT NULL DEFAULT 'FREE', provider TEXT NOT NULL,
          quality TEXT NOT NULL, manifest_id TEXT NOT NULL,
          PRIMARY KEY(listing_id, trade_date)
        );
        CREATE TABLE announcement(
          ann_id TEXT PRIMARY KEY, exchange_column TEXT NOT NULL, sec_code TEXT,
          org_id TEXT, title TEXT NOT NULL, adjunct_url TEXT, published_at_utc TEXT,
          first_seen_at_utc TEXT NOT NULL, category TEXT, event_id TEXT,
          manifest_id TEXT NOT NULL
        );
        CREATE TABLE frozen_artifact(
          artifact_id TEXT PRIMARY KEY, kind TEXT NOT NULL, candidate_id TEXT,
          version INTEGER NOT NULL, frozen_at_utc TEXT NOT NULL,
          content_sha256 TEXT NOT NULL, path TEXT NOT NULL, config_version TEXT NOT NULL
        );
        INSERT INTO security_day(
          listing_id, trade_date, close, currency, provider, quality, manifest_id
        ) VALUES('SZSE:000001','2026-08-28','10.00','CNY','legacy','OK','m0');
        INSERT INTO frozen_artifact VALUES(
          'card_c1_v1','CARD','c1',1,'2026-08-27T00:00:00Z','s1','one.md','v0'
        );
        INSERT INTO frozen_artifact VALUES(
          'card_c1_v2','CARD','c1',2,'2026-08-28T00:00:00Z','s2','two.md','v0'
        );
        """
    )
    old.commit()
    old.close()

    upgraded = connect(tmp_path)
    sec_cols = {r["name"] for r in upgraded.execute("PRAGMA table_info(security_day)")}
    ann_cols = {r["name"] for r in upgraded.execute("PRAGMA table_info(announcement)")}
    art_cols = {r["name"] for r in upgraded.execute("PRAGMA table_info(frozen_artifact)")}
    assert {"ret", "ret_basis", "adj_close", "basis_epoch"} <= sec_cols
    assert {"ts_precision", "ts_anomaly", "relevance"} <= ann_cols
    assert {"status", "status_note"} <= art_cols
    row = upgraded.execute("SELECT close, basis_epoch FROM security_day").fetchone()
    assert row["close"] == "10.00" and row["basis_epoch"] == 1
    assert upgraded.execute(
        "SELECT COUNT(*) AS n FROM schema_migration WHERE migration_id='s1_additive_columns'"
    ).fetchone()["n"] == 1
    states = upgraded.execute(
        "SELECT version, status FROM frozen_artifact ORDER BY version"
    ).fetchall()
    assert [(r["version"], r["status"]) for r in states] == [
        (1, "SUPERSEDED"),
        (2, "VALID"),
    ]
    upgraded.close()


def test_s2a_tables_and_migration_row(conn):
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"sec_filing", "sec_filing_document", "filing_party", "cik_map",
            "source_checkpoint"} <= tables
    mids = {r["migration_id"] for r in conn.execute(
        "SELECT migration_id FROM schema_migration")}
    assert "s2a_us_tables" in mids


def test_s2a_migration_preserves_existing_data(tmp_path):
    from investment_tool.db import connect

    c1 = connect(tmp_path)
    c1.execute(
        "INSERT INTO company(company_id, created_asof) VALUES('CN:X','2026-01-01T00:00:00Z')"
    )
    c1.commit()
    c1.close()
    c2 = connect(tmp_path)  # reopen -> migrations rerun idempotently
    assert c2.execute("SELECT COUNT(*) FROM company").fetchone()[0] == 1
    assert c2.execute("SELECT COUNT(*) FROM sec_filing").fetchone()[0] == 0
    c2.close()
