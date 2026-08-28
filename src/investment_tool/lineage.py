"""Raw-first lineage: every provider fetch persists its exact raw response
(gzip) plus a manifest row before any parsing is trusted (INV-7, DESIGN 10).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from investment_tool import SCHEMA_VERSION, TRANSFORM_VERSION
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.quality import Quality


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def code_git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() or "unknown"
    except OSError:
        return "unknown"


@dataclass
class Manifest:
    manifest_id: str
    run_id: str
    provider: str
    dataset: str
    params: dict
    source_url: str
    retrieved_at_utc: str
    http_status: int | None
    raw_sha256: str | None
    raw_path: str | None
    raw_bytes: int | None
    quality: Quality


class RawStore:
    def __init__(self, data_dir: Path | None = None):
        self.root = (data_dir or DEFAULT_DATA_DIR) / "raw"

    def write(
        self, provider: str, dataset: str, run_id: str, payload: bytes
    ) -> tuple[str, str, int]:
        """Persist raw payload; returns (sha256, relative path, size)."""
        sha = hashlib.sha256(payload).hexdigest()
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        rel = Path(provider) / dataset / day / f"{run_id}.gz"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wb") as fh:
            fh.write(payload)
        return sha, str(Path("raw") / rel), len(payload)


def record_fetch(
    conn: sqlite3.Connection,
    *,
    provider: str,
    dataset: str,
    params: dict,
    source_url: str,
    payload: bytes | None,
    http_status: int | None,
    quality: Quality,
    config_version: str,
    run_id: str | None = None,
    data_dir: Path | None = None,
) -> Manifest:
    """Write raw payload + manifest row. Call BEFORE parsing is trusted.

    `source_url` must be credential-free: the caller strips tokens before
    passing it here; an assertion guards the common query-string patterns.
    """
    for marker in ("token=", "api_key=", "apikey=", "key=", "secret="):
        if marker in source_url.lower():
            raise ValueError(f"source_url appears to contain a credential ({marker})")
    run_id = run_id or uuid.uuid4().hex
    manifest_id = uuid.uuid4().hex
    sha = path = None
    size = None
    if payload is not None:
        sha, path, size = RawStore(data_dir).write(provider, dataset, run_id, payload)
    m = Manifest(
        manifest_id=manifest_id, run_id=run_id, provider=provider, dataset=dataset,
        params=params, source_url=source_url, retrieved_at_utc=utc_now(),
        http_status=http_status, raw_sha256=sha, raw_path=path, raw_bytes=size,
        quality=quality,
    )
    conn.execute(
        "INSERT INTO manifest(manifest_id, run_id, provider, dataset, params_json, source_url,"
        " retrieved_at_utc, http_status, raw_sha256, raw_path, raw_bytes, schema_version,"
        " transform_version, code_git_sha, config_version, quality_state, quality_detail)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            m.manifest_id, m.run_id, provider, dataset, json.dumps(params, sort_keys=True),
            source_url, m.retrieved_at_utc, http_status, sha, path, size,
            SCHEMA_VERSION, TRANSFORM_VERSION, code_git_sha(), config_version,
            quality.state.value, quality.detail,
        ),
    )
    conn.commit()
    return m
