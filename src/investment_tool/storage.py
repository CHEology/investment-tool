"""Immutable evidence and atomic, credential-free JSON artifacts."""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    os.replace(temporary, path)


def new_run(root: Path, kind: str) -> Path:
    now = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = root / kind / f"{now}-{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def now_utc() -> str:
    return datetime.now(UTC).isoformat()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())
