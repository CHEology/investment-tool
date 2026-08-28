"""Re-ingest kline bars from the raw store (no network) — the event-sourced
recovery path for schema migrations or accidental purges. Idempotent."""

import gzip
import json
import sys

sys.path.insert(0, "src")
from investment_tool.db import connect  # noqa: E402
from investment_tool.providers import sina, tencent  # noqa: E402
from investment_tool.spine import store_kline_bars  # noqa: E402

conn = connect()
listings = {r["ticker"]: r for r in conn.execute(
    "SELECT listing_id, ticker, exchange, board FROM listing"
    " WHERE exchange IN ('SSE','SZSE','BSE')")}
mans = conn.execute(
    "SELECT manifest_id, provider, params_json, raw_path FROM manifest"
    " WHERE dataset='kline_daily' AND quality_state='PROVISIONAL'"
    " AND provider IN ('tencent','sina') ORDER BY retrieved_at_utc").fetchall()
done = restored = 0
for m in mans:
    params = json.loads(m["params_json"])
    sym = params.get("symbol", "")
    code = sym[2:] if sym[:2] in ("sz", "sh", "bj") else sym
    lst = listings.get(code)
    if lst is None:
        continue
    try:
        payload = gzip.open("data/" + m["raw_path"], "rb").read()
    except OSError:
        continue
    try:
        bars = (sina.parse_kline(payload) if m["provider"] == "sina"
                else tencent.parse_kline(payload, sym))
    except ValueError:
        continue
    adj = "RAW_SINA" if m["provider"] == "sina" else "QFQ_TENCENT"
    n = store_kline_bars(conn, lst, m["provider"], adj, bars, m["manifest_id"])
    restored += n
    done += 1
print(f"re-ingested {done} payloads, {restored} bars from raw store")
