"""Free Yahoo chart snapshots, with exact responses preserved before parsing."""

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from curl_cffi import requests

from .data import DataError, save_snapshot, sessions
from .storage import digest, new_run, now_utc, write_json


def parse_chart(payload: bytes, symbol: str, start: str, end: str) -> pd.DataFrame:
    chart = json.loads(payload)["chart"]
    if chart.get("error") or not chart.get("result"):
        raise DataError(f"Yahoo returned no price series for {symbol}")
    result = chart["result"][0]
    meta = result["meta"]
    if meta["currency"] != "USD" or meta.get("symbol", "").upper() != symbol:
        raise DataError(f"Yahoo instrument or currency mismatch for {symbol}")
    if meta.get("instrumentType") not in {"EQUITY", "ETF"}:
        raise DataError("Only US equities and ETFs are supported")
    exchange = {"NMS": "XNAS", "NGM": "XNAS", "NCM": "XNAS", "NYQ": "XNYS", "PCX": "ARCX"}.get(
        meta["exchangeName"]
    )
    if exchange is None:
        raise DataError(f"Unsupported Yahoo exchange for {symbol}")
    quotes = result["indicators"]["quote"][0]
    adjusted = result["indicators"]["adjclose"][0]["adjclose"]
    dates = pd.to_datetime(result["timestamp"], unit="s", utc=True)
    dates = dates.tz_convert("America/New_York").strftime("%Y-%m-%d")
    frame = pd.DataFrame(quotes)
    frame["adj_close"] = adjusted
    frame["date"] = dates
    frame["symbol"], frame["currency"], frame["exchange"] = symbol, "USD", exchange
    return frame.loc[(frame.date >= start) & (frame.date <= end)]


def fetch_yahoo(symbols: list[str], start: str, end: str, root: Path) -> Path:
    calendar = sessions(start, end)
    if calendar.empty:
        raise DataError("No trading sessions in the requested range")
    expected_end = calendar[-1].strftime("%Y-%m-%d")
    # End date must be closed, allowing two hours for EOD publication.
    import exchange_calendars as xcals

    close = xcals.get_calendar("XNYS", start=start, end=end).session_close(calendar[-1])
    if pd.Timestamp.now(tz="UTC") < close + pd.Timedelta(hours=2):
        raise DataError("Requested session is not closed with the two-hour publication buffer")
    if not symbols or len(symbols) != len(set(symbols)):
        raise DataError("Provide a nonempty, unique symbol list")
    import re

    if not all(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", s) for s in symbols):
        raise DataError("Invalid symbol")
    period1 = int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp())
    period2 = int((datetime.fromisoformat(end).replace(tzinfo=UTC) + timedelta(days=1)).timestamp())
    raw, frames = [], []
    request_run = new_run(root, "fetches")
    request_meta = {
        "start": start,
        "end": end,
        "symbols": symbols,
        "status": "running",
        "responses": [],
    }
    write_json(request_run / "request.json", request_meta)
    for index, symbol in enumerate(symbols):
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        try:
            response = requests.get(
                url,
                params={
                    "period1": period1,
                    "period2": period2,
                    "interval": "1d",
                    "events": "div,splits",
                    "includeAdjustedClose": "true",
                },
                impersonate="chrome",
                timeout=30,
            )
        except requests.RequestsError:
            request_meta.update(status="failed", failed_symbol=symbol)
            write_json(request_run / "request.json", request_meta)
            raise DataError(f"Yahoo request failed for {symbol}; retry later") from None
        payload = response.content
        raw_path = request_run / f"{symbol}.json"
        raw_path.write_bytes(payload)
        retrieved_at = now_utc()
        request_meta["responses"].append(
            {
                "symbol": symbol,
                "url": url,
                "retrieved_at": retrieved_at,
                "sha256": digest(raw_path),
                "http_status": response.status_code,
                "path": raw_path.name,
            }
        )
        write_json(request_run / "request.json", request_meta)
        if response.status_code != 200:
            request_meta.update(status="failed", failed_symbol=symbol)
            write_json(request_run / "request.json", request_meta)
            raise DataError(
                f"Yahoo HTTP {response.status_code} for {symbol}; raw response retained"
            )
        raw.append(
            {
                "payload": payload,
                "provider": "Yahoo Finance",
                "dataset": symbol,
                "url": url,
                "retrieved_at": retrieved_at,
            }
        )
        try:
            frames.append(parse_chart(payload, symbol, start, end))
        except (ValueError, KeyError, TypeError):
            request_meta.update(status="failed", failed_symbol=symbol)
            write_json(request_run / "request.json", request_meta)
            raise DataError(f"Invalid Yahoo response for {symbol}; raw response retained") from None
        if index < len(symbols) - 1:
            time.sleep(0.3)
    request_meta.update(status="completed")
    write_json(request_run / "request.json", request_meta)
    return save_snapshot(pd.concat(frames), root, raw, expected_end=expected_end)
