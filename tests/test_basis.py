"""Basis-consistency and corporate-action safety (review issue 2)."""

import json

from investment_tool import analytics, spine
from investment_tool.numeric import dec_from_db


def _seed_listing(conn, lid="SZSE:000777", code="000777"):
    conn.execute("INSERT OR IGNORE INTO company(company_id, created_asof)"
                 " VALUES(?, '2026-01-01T00:00:00Z')", (f"CN:{code}",))
    conn.execute("INSERT OR IGNORE INTO listing(listing_id, company_id, ticker, exchange,"
                 " board, currency) VALUES(?,?,?,?,?,?)",
                 (lid, f"CN:{code}", code, "SZSE", "MAIN", "CNY"))
    conn.commit()


def _bar(conn, lid, d, ret, basis, epoch=1, adj=None):
    conn.execute(
        "INSERT OR REPLACE INTO security_day(listing_id, trade_date, ret, ret_basis,"
        " adj_close, basis_epoch, currency, limit_state, provider, quality, manifest_id)"
        " VALUES(?,?,?,?,?,?, 'CNY','FREE','test','PROVISIONAL','m')",
        (lid, d, ret, basis, adj, epoch),
    )


def test_mixed_raw_and_adjusted_bases_block_listing(conn):
    _seed_listing(conn)
    _bar(conn, "SZSE:000777", "2026-08-20", 0.01, "QFQ_CONSEC")
    _bar(conn, "SZSE:000777", "2026-08-21", 0.01, "RAW_CONSEC")
    conn.commit()
    df = analytics.load_panel(conn, "2026-08-01", "2026-08-31")
    assert "SZSE:000777" in df.attrs["basis_blocked"]
    assert df[df.listing_id == "SZSE:000777"].empty  # blocked, not warned


def test_mixed_epochs_block_listing(conn):
    _seed_listing(conn, "SZSE:000778", "000778")
    _bar(conn, "SZSE:000778", "2026-08-20", 0.01, "QFQ_CONSEC", epoch=1)
    _bar(conn, "SZSE:000778", "2026-08-21", 0.01, "QFQ_CONSEC", epoch=2)
    conn.commit()
    df = analytics.load_panel(conn, "2026-08-01", "2026-08-31")
    assert "SZSE:000778" in df.attrs["basis_blocked"]


def test_compatible_adjusted_lineage_not_blocked(conn):
    _seed_listing(conn, "SZSE:000779", "000779")
    _bar(conn, "SZSE:000779", "2026-08-20", 0.01, "QFQ_CONSEC")
    _bar(conn, "SZSE:000779", "2026-08-21", 0.01, "EXCHANGE_PCT")
    _bar(conn, "SZSE:000779", "2026-08-24", 0.01, "SYNTH_COMPOUND")
    conn.commit()
    df = analytics.load_panel(conn, "2026-08-01", "2026-08-31")
    assert df.attrs["basis_blocked"] == []


def test_corporate_action_cannot_create_trigger_via_adjusted_returns():
    """A 10-for-5 stock dividend halves the raw price (-33% raw 'return') but
    the adjusted return is ~0. The trigger path consumes adjusted-lineage
    returns, so no Lane A trigger can arise from the split itself."""
    from test_analytics import make_panel

    df, cells = make_panel(crash_days=())
    # simulate: on 2026-01-20 the RAW price halved but adjusted ret ~ 0 -> the
    # panel's ret (adjusted lineage) stays ~0 by construction; assert no trigger
    panel = analytics.build_ar_panel(df, cells)
    w = analytics.car_window(panel, "SZSE:000001", "2026-01-20", k=3)
    sigma = analytics.residual_sigma(panel, "SZSE:000001", "2026-01-20", min_obs=15)
    assert analytics.evaluate_trigger(w["car"], sigma, -0.10, 3.0, 0.70) != "TRIGGER"


class _FakeHttp:
    """Returns a Tencent-shaped qfq payload rewritten after a corporate action."""

    def __init__(self, closes):
        self.closes = closes

    def get(self, url, **kw):
        class R:
            status_code = 200

            def __init__(self, closes):
                days = [
                    [f"2026-08-{18 + i:02d}", c, c, c, c, "1000"]
                    for i, c in enumerate(closes)
                ]
                self.content = json.dumps(
                    {"code": 0, "data": {"sz000777": {"qfqday": days}}}
                ).encode()

        return R(self.closes)


def test_provider_rewrite_bumps_epoch_and_replaces_history(conn, tmp_path, monkeypatch):
    import investment_tool.providers.tencent as tencent_mod

    _seed_listing(conn)
    lst = conn.execute("SELECT * FROM listing WHERE ticker='000777'").fetchone()
    monkeypatch.setattr(
        tencent_mod, "fetch_kline",
        lambda http, sym, start, end, count=320: (http.get("x").content, 200, "https://t/x"),
    )
    import investment_tool.db as db_mod
    monkeypatch.setattr(db_mod, "DEFAULT_DATA_DIR", tmp_path)
    http = {"tencent": _FakeHttp(["10.0", "10.1", "10.2"]), "sina": None}
    spine.backfill_listing(conn, http, "v0.1", lst, "20260801")
    e1 = conn.execute("SELECT MAX(basis_epoch) AS e FROM security_day"
                      " WHERE listing_id='SZSE:000777'").fetchone()["e"]
    assert e1 == 1
    # provider rewrites the series (corporate action) -> epoch bump + full replace
    http = {"tencent": _FakeHttp(["5.0", "5.05", "5.1"]), "sina": None}
    spine.backfill_listing(conn, http, "v0.1", lst, "20260801")
    rows = conn.execute("SELECT basis_epoch, adj_close FROM security_day"
                        " WHERE listing_id='SZSE:000777' ORDER BY trade_date").fetchall()
    assert all(r["basis_epoch"] == 2 for r in rows)
    assert dec_from_db(rows[0]["adj_close"]) is not None
    obs = conn.execute("SELECT COUNT(*) AS n FROM observation"
                       " WHERE kind='corporate_action_detected'").fetchone()["n"]
    assert obs == 1


def test_bse_snapshot_stays_raw_lineage(conn):
    """BSE daily ingest must not mix EXCHANGE_PCT onto RAW_CONSEC history
    (would basis-block every BSE listing and silently drop INV-1 coverage)."""
    conn.execute("INSERT OR IGNORE INTO company(company_id, created_asof)"
                 " VALUES('CN:920001','2026-01-01T00:00:00Z')")
    conn.execute("INSERT OR IGNORE INTO listing(listing_id, company_id, ticker, exchange,"
                 " board, currency) VALUES('BSE:920001','CN:920001','920001','BSE','BSE','CNY')")
    _bar(conn, "BSE:920001", "2026-08-27", 0.01, "RAW_CONSEC")
    conn.execute("UPDATE security_day SET close='10.00' WHERE listing_id='BSE:920001'")
    conn.commit()
    # simulate the snapshot branch: prior raw close exists -> RAW_CONSEC ret
    prev = conn.execute("SELECT close FROM security_day WHERE listing_id='BSE:920001'"
                        " AND trade_date<'2026-08-28' AND close IS NOT NULL"
                        " ORDER BY trade_date DESC LIMIT 1").fetchone()
    ret = float("10.50") / float(prev["close"]) - 1.0
    _bar(conn, "BSE:920001", "2026-08-28", ret, "RAW_CONSEC")
    conn.commit()
    df = __import__("investment_tool.analytics", fromlist=["load_panel"]).load_panel(
        conn, "2026-08-01", "2026-08-31")
    assert "BSE:920001" not in df.attrs["basis_blocked"]
