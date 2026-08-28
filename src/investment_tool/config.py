"""Versioned threshold/config registry (DESIGN 10, 13).

Every threshold lives in config/thresholds/<id>.yaml with EXPERIMENTAL status
until forward evidence supports validation. Changes are forward-only: a new
file (or dated changelog entry) creates a new config_version row; runs stamp
the version they used.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import yaml

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "thresholds"


class Config:
    def __init__(self, config_id: str, data: dict, yaml_sha256: str):
        self.id = config_id
        self.data = data
        self.yaml_sha256 = yaml_sha256

    def get(self, dotted: str):
        node = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                raise KeyError(f"config key missing: {dotted}")
            node = node[part]
        return node

    def value(self, dotted: str):
        """Threshold entries are {value, status, purpose, ...}; return value."""
        node = self.get(dotted)
        if isinstance(node, dict) and "value" in node:
            return node["value"]
        return node


def load(config_id: str = "v0", config_dir: Path | None = None) -> Config:
    path = (config_dir or CONFIG_DIR) / f"{config_id}.yaml"
    raw = path.read_bytes()
    return Config(config_id, yaml.safe_load(raw), hashlib.sha256(raw).hexdigest())


def register(conn: sqlite3.Connection, cfg: Config, changelog: str = "") -> str:
    row = conn.execute(
        "SELECT yaml_sha256 FROM config_version WHERE id=?", (cfg.id,)
    ).fetchone()
    if row is not None:
        if row["yaml_sha256"] != cfg.yaml_sha256:
            raise RuntimeError(
                f"config '{cfg.id}' content changed but id did not; thresholds are"
                " forward-only — create a new config id instead of editing in place"
            )
        return cfg.id
    from investment_tool.lineage import utc_now

    conn.execute(
        "INSERT INTO config_version(id, yaml_sha256, effective_from_utc, changelog)"
        " VALUES(?,?,?,?)",
        (cfg.id, cfg.yaml_sha256, utc_now(), changelog),
    )
    conn.commit()
    return cfg.id
