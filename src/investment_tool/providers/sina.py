"""Sina kline endpoint — PROVISIONAL scan-tier source for BSE daily bars
(Tencent does not serve BSE history). Raw (unadjusted) closes; volume in
shares; no turnover amount (explicit None). BSE dividends are infrequent, so
raw-close returns are an accepted scan-tier approximation (documented)."""

from __future__ import annotations

import json

from investment_tool.providers.base import BROWSER_UA, HttpClient

KLINE_URL = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)


def client() -> HttpClient:
    return HttpClient(user_agent=BROWSER_UA, min_interval_s=0.40, keep_alive=True,
                      extra_headers={"Referer": "https://finance.sina.com.cn/"},
                      rotate_every=300)


def symbol(code: str) -> str:
    return "bj" + code


def fetch_kline(http: HttpClient, sym: str, datalen: int = 320):
    url = f"{KLINE_URL}?symbol={sym}&scale=240&ma=no&datalen={datalen}"
    resp = http.get(url)
    return resp.content, resp.status_code, url


def parse_kline(payload: bytes) -> list[dict]:
    data = json.loads(payload.decode("utf-8-sig"), parse_float=str)
    if not isinstance(data, list):
        return []
    return [
        {"date": r["day"], "open": r["open"], "close": r["close"], "high": r["high"],
         "low": r["low"], "volume": r.get("volume"), "amount": None}
        for r in data
    ]
