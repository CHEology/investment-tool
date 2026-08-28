"""Frankfurter (ECB reference rates) — SCAN-grade FX for liquidity thresholds."""

from __future__ import annotations

import json

from investment_tool.providers.base import GENERIC_UA, HttpClient

BASE = "https://api.frankfurter.dev/v1"


def client() -> HttpClient:
    return HttpClient(user_agent=GENERIC_UA, min_interval_s=0.5)


def fetch_rate(http: HttpClient, date: str | None, base: str = "USD", symbol: str = "CNY"):
    """date=None -> latest. Returns (payload, status, url)."""
    url = f"{BASE}/{date or 'latest'}?base={base}&symbols={symbol}"
    resp = http.get(url)
    return resp.content, resp.status_code, url


def parse_rate(payload: bytes, symbol: str = "CNY") -> tuple[str, str]:
    """Returns (date, rate_as_source_string). Rate is re-serialized from the
    JSON number token via Decimal-safe parse of the original text."""
    text = payload.decode("utf-8")
    data = json.loads(text)
    rate = data["rates"][symbol]
    # json parses numbers to float; recover exact source token from the text.
    import re

    m = re.search(rf'"{symbol}"\s*:\s*([0-9.eE+-]+)', text)
    token = m.group(1) if m else repr(rate)
    return data["date"], token
