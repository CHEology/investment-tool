from investment_tool.entities import resolve, seed_a_share, seed_us

A_ROWS = [
    {"code": "300274", "org_id": "9900021300", "name_zh": "阳光电源", "pinyin": "ygdy",
     "exchange": "SZSE", "board": "CHINEXT"},
    {"code": "920000", "org_id": "o5", "name_zh": "安徽凤凰", "pinyin": "ahfh",
     "exchange": "BSE", "board": "BSE"},
]
US_ROWS = [
    {"ticker": "AAPL", "name_en": "Apple Inc. - Common Stock", "exchange": "NASDAQ", "is_adr": 0},
    {"ticker": "AACG", "name_en": "ATA Creativity Global - ADS", "exchange": "NASDAQ", "is_adr": 1},
]


def test_seed_and_resolve_a_share(conn):
    assert seed_a_share(conn, A_ROWS) == 2
    rows = resolve(conn, "300274")
    assert len(rows) == 1
    r = rows[0]
    assert r["company_id"] == "CN:300274"
    assert r["name_zh"] == "阳光电源"
    assert r["exchange"] == "SZSE" and r["board"] == "CHINEXT" and r["currency"] == "CNY"


def test_seed_and_resolve_us(conn):
    assert seed_us(conn, US_ROWS) == 2
    r = resolve(conn, "AAPL")[0]
    assert r["company_id"] == "US:AAPL"
    assert r["currency"] == "USD" and r["is_adr"] == 0
    assert r["cik"] is None  # enrichment deferred to S2 (D3)
    assert resolve(conn, "AACG")[0]["is_adr"] == 1


def test_seed_idempotent(conn):
    seed_a_share(conn, A_ROWS)
    seed_a_share(conn, A_ROWS)
    n = conn.execute("SELECT COUNT(*) AS n FROM listing").fetchone()["n"]
    assert n == 2


def test_bse_listing_present(conn):
    seed_a_share(conn, A_ROWS)
    assert resolve(conn, "920000")[0]["exchange"] == "BSE"
