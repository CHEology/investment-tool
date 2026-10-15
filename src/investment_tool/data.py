"""Versioned daily prices. No inference from stale, duplicate or invalid input."""

import io
import re
from pathlib import Path

import exchange_calendars as xcals
import numpy as np
import pandas as pd

from .storage import digest, new_run, now_utc, read_json, write_json

FIELDS = [
    "date",
    "symbol",
    "exchange",
    "currency",
    "open",
    "high",
    "low",
    "close",
    "adj_close",
    "volume",
]
NUMERIC = ["open", "high", "low", "close", "adj_close", "volume"]


class DataError(ValueError):
    """A blocking evidence or data-quality failure."""


def sessions(start: str, end: str) -> pd.DatetimeIndex:
    # Calendar bounds are sessions, while requested bounds can be holidays.
    lower = pd.Timestamp(start) - pd.Timedelta(days=10)
    upper = pd.Timestamp(end) + pd.Timedelta(days=10)
    return xcals.get_calendar("XNYS", start=lower, end=upper).sessions_in_range(start, end)


def validate(frame: pd.DataFrame, expected_end: str | None = None) -> pd.DataFrame:
    if frame.empty or set(FIELDS) - set(frame.columns):
        raise DataError("Empty prices or missing required columns")
    frame = frame[FIELDS].copy()
    if frame.isna().any().any():
        raise DataError("Missing canonical values")
    if not frame.symbol.astype(str).str.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}").all():
        raise DataError("Invalid US instrument identifier")
    if not frame.date.astype(str).str.fullmatch(r"\d{4}-\d{2}-\d{2}").all():
        raise DataError("Dates must be ISO trading dates")
    try:
        pd.to_datetime(frame.date, format="%Y-%m-%d", errors="raise")
        frame[NUMERIC] = frame[NUMERIC].apply(pd.to_numeric, errors="raise")
    except (ValueError, TypeError) as error:
        raise DataError("Invalid date or numeric value") from error
    if frame.duplicated(["symbol", "date"]).any():
        raise DataError("Duplicate symbol/date observations")
    values = frame[NUMERIC].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (frame[NUMERIC[:-1]] <= 0).any().any():
        raise DataError("Non-finite or non-positive prices")
    if (frame.volume <= 0).any() or not np.equal(frame.volume, np.floor(frame.volume)).all():
        raise DataError("Non-positive or non-integer volume; halt handling is not implemented")
    if (
        (frame.low > frame[["open", "close"]].min(axis=1))
        | (frame.high < frame[["open", "close"]].max(axis=1))
    ).any():
        raise DataError("OHLC relationship violated")
    if set(frame.currency) != {"USD"}:
        raise DataError("This release accepts USD US equities only")
    if not set(frame.exchange) <= {"XNYS", "XNAS", "ARCX"}:
        raise DataError("Unsupported exchange")
    if (frame.groupby("symbol")[["exchange", "currency"]].nunique() > 1).any().any():
        raise DataError("Conflicting instrument metadata")
    calendar = sessions(frame.date.min(), frame.date.max()).strftime("%Y-%m-%d")
    if set(frame.date) != set(calendar):
        raise DataError("Missing sessions or non-session observations")
    for symbol, rows in frame.groupby("symbol"):
        if set(rows.date) != set(calendar):
            raise DataError(f"Incomplete history for {symbol}; no silent forward filling")
    if expected_end is not None and frame.date.max() != expected_end:
        raise DataError(f"Stale or future data: expected {expected_end}, got {frame.date.max()}")
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def save_snapshot(
    frame: pd.DataFrame,
    root: Path,
    raw_sources: list[dict],
    *,
    synthetic: bool = False,
    expected_end: str | None = None,
) -> Path:
    """raw_sources contains exact payload bytes; never persisted inside the manifest."""
    run = new_run(root, "snapshots")
    source_records = []
    for index, source in enumerate(raw_sources):
        raw_path = run / f"raw-{index:04d}.bin"
        raw_path.write_bytes(source["payload"])
        source_records.append(
            {key: source[key] for key in ("provider", "dataset", "url", "retrieved_at")}
            | {"path": raw_path.name, "sha256": digest(raw_path)}
        )
    manifest = {
        "schema_version": 1,
        "transform_version": "us-daily-v1",
        "created_at": now_utc(),
        "sources": source_records,
        "status": "building",
        "synthetic": synthetic,
        "universe_status": "static_research_sample",
        "adjustment": "provider_adjusted_close_ratio_applied_to_OHLC_inverse_to_volume",
        "warnings": [
            "Static selected universe: survivorship and selection bias are unresolved.",
            "Adjusted history may be revised by the provider; this is a retrieval-time snapshot.",
            "Daily VWAP is unavailable and remains missing in the Alpha158 feature set.",
        ],
    }
    try:
        if not source_records:
            raise DataError("Source evidence is required")
        for source in source_records:
            # Source URLs are public identities, never credential-bearing request URLs.
            if "?" in source["url"] or "@" in source["url"]:
                raise DataError("Source URL must not contain query parameters or credentials")
        clean = validate(frame, expected_end)
        path = run / "prices.csv"
        clean.to_csv(path, index=False, float_format="%.12g")
        manifest.update(
            {
                "status": "passed",
                "prices_sha256": digest(path),
                "start": clean.date.min(),
                "end": clean.date.max(),
                "symbols": sorted(clean.symbol.unique().tolist()),
                "rows": len(clean),
            }
        )
    except (ValueError, TypeError) as error:
        manifest.update(status="blocked", error=str(error))
        raise
    finally:
        write_json(run / "manifest.json", manifest)
    return run


def load_snapshot(path: Path) -> tuple[pd.DataFrame, dict]:
    meta = read_json(path / "manifest.json")
    if meta["status"] != "passed" or digest(path / "prices.csv") != meta["prices_sha256"]:
        raise DataError("Snapshot failed validation or its prices were modified")
    for source in meta["sources"]:
        if not re.fullmatch(r"raw-\d{4}\.bin", source["path"]):
            raise DataError("Invalid raw evidence path")
        if digest(path / source["path"]) != source["sha256"]:
            raise DataError("Raw evidence hash mismatch")
    return validate(pd.read_csv(path / "prices.csv", dtype=str), meta["end"]), meta


def import_csv(path: Path, root: Path) -> Path:
    payload = path.read_bytes()
    frame = pd.read_csv(io.BytesIO(payload), dtype=str)
    return save_snapshot(
        frame,
        root,
        [
            {
                "payload": payload,
                "provider": "user_import",
                "dataset": "US daily OHLCV",
                "url": "local:import",
                "retrieved_at": now_utc(),
            }
        ],
    )
