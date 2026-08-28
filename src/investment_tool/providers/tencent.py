"""Tencent ifzq kline endpoint — PROVISIONAL scan-tier source for SSE/SZSE
daily bars (adopted after Eastmoney's kline hosts IP-blocked sustained
ingestion; same fallback role, same quality state, documented in the S1 slice
report). Serves forward-adjusted (qfq) bars; volume arrives in lots (手) and
is normalized to shares at parse time; no turnover amount (explicit None).
BSE history is NOT served here — the Sina adapter covers it.
"""

from __future__ import annotations

import json

from investment_tool.providers.base import BROWSER_UA, HttpClient

KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def client() -> HttpClient:
    return HttpClient(user_agent=BROWSER_UA, min_interval_s=0.30, keep_alive=True,
                      rotate_every=500)


def symbol(exchange: str, code: str) -> str:
    return ("sh" if exchange == "SSE" else "sz") + code


def fetch_kline(http: HttpClient, sym: str, start: str, end: str, count: int = 320):
    url = f"{KLINE_URL}?param={sym},day,{start},{end},{count},qfq"
    resp = http.get(url)
    return resp.content, resp.status_code, url


def parse_kline(payload: bytes, sym: str) -> list[dict]:
    data = json.loads(payload.decode("utf-8-sig"), parse_float=str)
    node = (data.get("data") or {}).get(sym) or {}
    rows = node.get("qfqday") or node.get("day") or []
    out = []
    for r in rows:
        # [date, open, close, high, low, volume(lots)] as string tokens
        vol_lots = r[5]
        try:
            vol_shares = str(int(float(vol_lots) * 100))
        except (TypeError, ValueError):
            vol_shares = None
        out.append(
            {"date": r[0], "open": r[1], "close": r[2], "high": r[3], "low": r[4],
             "volume": vol_shares, "amount": None}
        )
    return out
