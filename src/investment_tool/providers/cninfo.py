"""CNInfo (巨潮资讯) — the CSRC-designated disclosure platform.

Role: EVIDENCE for announcement metadata/PDFs; REFERENCE for the security
mapping. The JSON endpoints are unofficial (the site's own XHR API), so
breakage is an expected quality state, throttling is mandatory, and raw
responses are always persisted before parsing.
"""

from __future__ import annotations

import json

from investment_tool.providers.base import BROWSER_UA, HttpClient

MAPPING_URL = "http://www.cninfo.com.cn/new/data/szse_stock.json"
QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"

# Code-prefix -> (exchange, board). B-shares (20/90) are out of scope (INV-1).
PREFIX_MAP = {
    "60": ("SSE", "MAIN"), "68": ("SSE", "STAR"),
    "00": ("SZSE", "MAIN"), "30": ("SZSE", "CHINEXT"),
    "43": ("BSE", "BSE"), "83": ("BSE", "BSE"), "87": ("BSE", "BSE"), "92": ("BSE", "BSE"),
}
B_SHARE_PREFIXES = ("20", "90")


def client() -> HttpClient:
    # Browser-like UA (endpoint rejects generic agents); >=2s politeness interval.
    return HttpClient(user_agent=BROWSER_UA, min_interval_s=2.0)


def fetch_security_mapping(http: HttpClient) -> tuple[bytes, int]:
    resp = http.get(MAPPING_URL)
    return resp.content, resp.status_code


def parse_security_mapping(payload: bytes) -> list[dict]:
    """Rows: {code, org_id, name_zh, pinyin, exchange, board} for A-shares only."""
    data = json.loads(payload)
    out = []
    for row in data.get("stockList", []):
        code = row.get("code", "")
        prefix = code[:2]
        if prefix in B_SHARE_PREFIXES or prefix not in PREFIX_MAP:
            continue
        exchange, board = PREFIX_MAP[prefix]
        out.append(
            {
                "code": code,
                "org_id": row.get("orgId"),
                "name_zh": row.get("zwjc"),
                "pinyin": row.get("pinyin"),
                "exchange": exchange,
                "board": board,
            }
        )
    return out
