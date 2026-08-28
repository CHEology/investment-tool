import json

from investment_tool.providers import cninfo, frankfurter, nasdaq


def test_cninfo_mapping_parse_covers_all_boards_and_drops_b_shares():
    payload = json.dumps({"stockList": [
        {"code": "600000", "orgId": "o1", "zwjc": "浦发银行", "pinyin": "p", "category": "A股"},
        {"code": "688001", "orgId": "o2", "zwjc": "华兴源创", "pinyin": "p", "category": "A股"},
        {"code": "000001", "orgId": "o3", "zwjc": "平安银行", "pinyin": "p", "category": "A股"},
        {"code": "300274", "orgId": "9900021300", "zwjc": "阳光电源",
         "pinyin": "p", "category": "A股"},
        {"code": "920000", "orgId": "o5", "zwjc": "安徽凤凰", "pinyin": "p", "category": "A股"},
        {"code": "900001", "orgId": "o6", "zwjc": "B股例", "pinyin": "p", "category": "B股"},
        {"code": "200001", "orgId": "o7", "zwjc": "B股例2", "pinyin": "p", "category": "B股"},
    ]}).encode()
    rows = cninfo.parse_security_mapping(payload)
    by_code = {r["code"]: r for r in rows}
    assert set(by_code) == {"600000", "688001", "000001", "300274", "920000"}
    assert by_code["600000"]["exchange"] == "SSE" and by_code["600000"]["board"] == "MAIN"
    assert by_code["688001"]["board"] == "STAR"
    assert by_code["300274"]["exchange"] == "SZSE" and by_code["300274"]["board"] == "CHINEXT"
    assert by_code["920000"]["exchange"] == "BSE"
    assert by_code["300274"]["org_id"] == "9900021300"


NASDAQ_HEADER = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status"
    "|Round Lot Size|ETF|NextShares"
)
NASDAQ_FIXTURE = NASDAQ_HEADER + """
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
AACG|ATA Creativity Global - American Depositary Shares|S|N|N|100|N|N
ZAZZT|Test Issue Co|G|Y|N|100|N|N
AAAP|Some ETF Trust|G|N|N|100|Y|N
ABCW|ABC Corp Warrants|G|N|N|100|N|N
File Creation Time: 0828202617:01|||||||
"""

OTHER_HEADER = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF"
    "|Round Lot Size|Test Issue|NASDAQ Symbol"
)
OTHER_FIXTURE = OTHER_HEADER + """
BRK.B|Berkshire Hathaway Inc. Class B|N|BRK/B|N|100|N|BRK.B
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
XYZ$A|XYZ Corp Preferred Series A|N|XYZ-A|N|100|N|XYZ$A
GME|GameStop Corp|N|GME|N|100|N|GME
File Creation Time: 0828202617:01|||||||
"""


def test_nasdaq_parse_filters():
    rows = nasdaq.parse_nasdaq_listed(NASDAQ_FIXTURE)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"AAPL", "AACG"}
    adr = {r["ticker"]: r["is_adr"] for r in rows}
    assert adr["AACG"] == 1 and adr["AAPL"] == 0


def test_other_listed_parse_filters():
    rows = nasdaq.parse_other_listed(OTHER_FIXTURE)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"BRK.B", "GME"}  # ETF (Arca) and preferred dropped
    assert all(r["exchange"] == "NYSE" for r in rows)


def test_frankfurter_parse_preserves_source_token():
    payload = b'{"amount":1.0,"base":"USD","date":"2026-08-27","rates":{"CNY":6.7203}}'
    date, rate = frankfurter.parse_rate(payload)
    assert date == "2026-08-27"
    assert rate == "6.7203"  # exact source token, not float repr


def test_tencent_parse_volume_lots_to_shares():
    from investment_tool.providers import tencent

    payload = (b'{"code":0,"msg":"","data":{"sz300274":{"qfqday":['
               b'["2026-08-28","97.000","97.690","98.880","96.100","616243.000"]]}}}')
    bars = tencent.parse_kline(payload, "sz300274")
    assert len(bars) == 1
    # verified fixture: 300274 on 2026-08-28 traded 616,243 lots = 61,624,300 shares
    assert bars[0]["volume"] == "61624300"
    assert bars[0]["close"] == "97.690"  # exact source token
    assert bars[0]["amount"] is None      # explicitly missing, never zero


def test_sina_parse_bse_raw():
    from investment_tool.providers import sina

    payload = (b'[{"day":"2026-08-28","open":"13.59","high":"13.87","low":"13.44",'
               b'"close":"13.81","volume":"8133"}]')
    bars = sina.parse_kline(payload)
    assert bars[0]["close"] == "13.81" and bars[0]["volume"] == "8133"
    assert bars[0]["amount"] is None


def test_tencent_html_body_yields_no_bars():
    from investment_tool.providers import tencent

    try:
        bars = tencent.parse_kline(b"\xef\xbb\xbf<!DOCTYPE html><html></html>", "sz000001")
    except ValueError:
        bars = []
    assert bars == []
