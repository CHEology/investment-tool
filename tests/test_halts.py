from pathlib import Path

from investment_tool.providers.nasdaq_halts import parse_halts, route_halts

FIX = Path(__file__).parent / "fixtures" / "sec"


def _seed(conn):
    for cid, t, ex in (("US:ALPH", "ALPH", "NASDAQ"), ("US:BETA", "BETA", "NYSE")):
        conn.execute("INSERT INTO company(company_id, created_asof)"
                     " VALUES(?, '2026-01-01T00:00:00Z')", (cid,))
        conn.execute("INSERT INTO listing(listing_id, company_id, ticker, exchange, currency)"
                     " VALUES(?,?,?,?, 'USD')", (f"{ex}:{t}", cid, t, ex))
    conn.commit()


def test_parse_halts():
    halts = parse_halts((FIX / "tradehalts_sample.xml").read_bytes())
    assert [h["reason"] for h in halts] == ["T12", "H10", "LUDP"]


def test_halt_routing_separates_fact_from_cause(conn):
    _seed(conn)
    halts = parse_halts((FIX / "tradehalts_sample.xml").read_bytes())
    hist = route_halts(conn, halts)
    # H10 suspension -> HARD_NEGATIVE event; T12 news-pending -> review event;
    # LUDP -> observation only; GAMM unmatched
    assert hist == {"CONTENT_REVIEW_REQUIRED": 1, "HARD_NEGATIVE": 1, "HALT_UNMATCHED": 1}
    events = {r["type"] for r in conn.execute("SELECT type FROM event")}
    assert events == {"TRADING_SUSPENSION", "TRADING_HALT_NEWS"}
    obs = conn.execute("SELECT COUNT(*) FROM observation WHERE kind='trade_halt'").fetchone()[0]
    assert obs == 3  # every halt is an observation regardless of routing
    # idempotent re-poll
    route_halts(conn, halts)
    assert conn.execute("SELECT COUNT(*) FROM observation"
                        " WHERE kind='trade_halt'").fetchone()[0] == 3
    import json
    ev = conn.execute("SELECT dims_json, excerpt FROM evidence LIMIT 1").fetchone()
    assert json.loads(ev["dims_json"])["independence"] == 3  # exchange authority
    assert "NOT established" in ev["excerpt"]
