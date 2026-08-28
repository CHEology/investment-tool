"""Eastmoney push2 endpoints — PROVISIONAL scan-tier fallback for the A-share
price spine while no Tushare token exists (decision D2 pending).

Everything ingested through this module carries QualityState.PROVISIONAL and
creates verification debt: candidates built on it must be re-verified against
an evidence/verify-grade source before publication (DESIGN 5.9).

Exactness: kline rows arrive as comma-joined source strings (exact decimal
tokens). Snapshot JSON is parsed with parse_float=str so numeric tokens are
preserved verbatim rather than round-tripped through binary floats.
"""

from __future__ import annotations

import json

from investment_tool.providers.base import BROWSER_UA, HttpClient

# push2delay = delayed-quote mirror; equivalent for EOD use after the close and
# markedly more tolerant of pagination than the live host (which 502-blocked us).
SNAPSHOT_URL = "https://push2delay.eastmoney.com/api/qt/clist/get"
# push2delayhis: delayed-history mirror; slower per request but does not drop
# sustained request streams the way the live host does (probed 60/60 ok).
KLINE_URL = "https://push2delayhis.eastmoney.com/api/qt/stock/kline/get"

# All A-share boards incl. BSE (m:0 t:81 s:2048), per verified probes.
SNAPSHOT_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
SNAPSHOT_FIELDS = "f2,f3,f5,f6,f12,f14,f15,f16,f17,f18,f20,f21,f100"

BENCHMARKS = {
    "CSI300": "1.000300",
    "CSI500": "1.000905",
    "CSI1000": "1.000852",
    "BSE50": "0.899050",
}


def client() -> HttpClient:
    return HttpClient(
        user_agent=BROWSER_UA, min_interval_s=0.30, keep_alive=False,
        extra_headers={"Referer": "https://quote.eastmoney.com/"}, rotate_every=200,
    )


def secid(exchange: str, code: str) -> str:
    return f"{'1' if exchange == 'SSE' else '0'}.{code}"


def fetch_snapshot_page(http: HttpClient, page: int, page_size: int = 100):
    url = (
        f"{SNAPSHOT_URL}?pn={page}&pz={page_size}&po=0&np=1&fltt=2&invt=2&fid=f12"
        f"&fs={SNAPSHOT_FS}&fields={SNAPSHOT_FIELDS}"
    )
    resp = http.get(url)
    return resp.content, resp.status_code, url


def parse_snapshot_page(payload: bytes) -> tuple[int, list[dict]]:
    """Returns (total, rows). Missing numeric values ('-') stay None."""
    data = json.loads(payload.decode("utf-8-sig"), parse_float=str)
    d = data.get("data") or {}
    rows = []
    for r in d.get("diff") or []:
        def g(key, row=r):
            v = row.get(key)
            return None if v in ("-", "", None) else str(v)

        rows.append(
            {
                "code": str(r.get("f12")),
                "name": r.get("f14"),
                "close": g("f2"),
                "pct_chg": g("f3"),
                "volume": g("f5"),
                "amount": g("f6"),
                "high": g("f15"),
                "low": g("f16"),
                "open": g("f17"),
                "prev_close": g("f18"),
                "total_mcap": g("f20"),
                "float_mcap": g("f21"),
                "industry": r.get("f100") if r.get("f100") not in ("-", "") else None,
            }
        )
    return int(d.get("total") or 0), rows


def fetch_kline(http: HttpClient, secid_: str, beg: str, end: str = "20500101",
                index: bool = False):
    fields2 = "f51,f53" if index else "f51,f52,f53,f54,f55,f56,f57,f59"
    url = (
        f"{KLINE_URL}?secid={secid_}&klt=101&fqt=0&beg={beg}&end={end}"
        f"&fields1=f1,f2,f3&fields2={fields2}"
    )
    resp = http.get(url)
    return resp.content, resp.status_code, url


def parse_kline(payload: bytes, index: bool = False) -> list[dict]:
    """Kline strings are split into exact source tokens."""
    data = json.loads(payload.decode("utf-8-sig"))
    klines = (data.get("data") or {}).get("klines") or []
    out = []
    for line in klines:
        parts = line.split(",")
        if index:
            out.append({"date": parts[0], "close": parts[1]})
        else:
            out.append(
                {
                    "date": parts[0], "open": parts[1], "close": parts[2],
                    "high": parts[3], "low": parts[4], "volume": parts[5],
                    "amount": parts[6], "pct_chg": parts[7],
                }
            )
    return out
