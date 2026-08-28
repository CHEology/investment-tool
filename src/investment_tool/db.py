"""SQLite storage: schema DDL and connection handling.

Conventions: timestamps TEXT ISO-8601 UTC with trailing 'Z'; dates TEXT
YYYY-MM-DD; reported financial values TEXT canonical decimals (numeric.py);
derived analytics REAL. Append-only tables (event, evidence, frozen_artifact,
validation_snapshot) are never updated in place except for state fields whose
transitions are themselves recorded.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("INVESTMENT_TOOL_DATA_DIR", "data"))

DDL = """
CREATE TABLE IF NOT EXISTS manifest(
  manifest_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  provider TEXT NOT NULL,
  dataset TEXT NOT NULL,
  params_json TEXT NOT NULL,
  source_url TEXT NOT NULL,
  retrieved_at_utc TEXT NOT NULL,
  http_status INTEGER,
  raw_sha256 TEXT,
  raw_path TEXT,
  raw_bytes INTEGER,
  schema_version TEXT NOT NULL,
  transform_version TEXT NOT NULL,
  code_git_sha TEXT NOT NULL,
  config_version TEXT NOT NULL,
  quality_state TEXT NOT NULL,
  quality_detail TEXT,
  normalized_path TEXT
);
CREATE TABLE IF NOT EXISTS config_version(
  id TEXT PRIMARY KEY,
  yaml_sha256 TEXT NOT NULL,
  effective_from_utc TEXT NOT NULL,
  changelog TEXT
);
CREATE TABLE IF NOT EXISTS company(
  company_id TEXT PRIMARY KEY,
  name_zh TEXT,
  name_en TEXT,
  uscc TEXT,
  cik TEXT,
  created_asof TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS listing(
  listing_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES company(company_id),
  ticker TEXT NOT NULL,
  exchange TEXT NOT NULL,
  board TEXT,
  currency TEXT NOT NULL,
  list_date TEXT,
  delist_date TEXT,
  status TEXT NOT NULL DEFAULT 'LISTED',
  is_adr INTEGER NOT NULL DEFAULT 0,
  cninfo_org_id TEXT,
  UNIQUE(ticker, exchange)
);
CREATE TABLE IF NOT EXISTS alias(
  company_id TEXT NOT NULL REFERENCES company(company_id),
  text TEXT NOT NULL,
  kind TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  UNIQUE(company_id, text, kind)
);
CREATE TABLE IF NOT EXISTS security_day(
  listing_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  open TEXT, high TEXT, low TEXT, close TEXT, prev_close TEXT,
  volume TEXT, amount TEXT,
  pct_chg REAL,
  adj_factor TEXT,
  adj_method TEXT NOT NULL DEFAULT 'NONE',
  currency TEXT NOT NULL,
  limit_state TEXT NOT NULL DEFAULT 'FREE',
  provider TEXT NOT NULL,
  quality TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  PRIMARY KEY(listing_id, trade_date)
);
CREATE TABLE IF NOT EXISTS benchmark_day(
  index_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  close TEXT NOT NULL,
  provider TEXT NOT NULL,
  quality TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  PRIMARY KEY(index_id, trade_date)
);
CREATE TABLE IF NOT EXISTS fx_day(
  pair TEXT NOT NULL,
  date TEXT NOT NULL,
  rate TEXT NOT NULL,
  source TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  PRIMARY KEY(pair, date)
);
CREATE TABLE IF NOT EXISTS calendar_day(
  exchange TEXT NOT NULL,
  date TEXT NOT NULL,
  is_trading INTEGER NOT NULL,
  source TEXT NOT NULL,
  PRIMARY KEY(exchange, date)
);
CREATE TABLE IF NOT EXISTS industry_snapshot(
  listing_id TEXT NOT NULL,
  asof_date TEXT NOT NULL,
  industry TEXT,
  source TEXT NOT NULL,
  quality TEXT NOT NULL,
  PRIMARY KEY(listing_id, asof_date)
);
CREATE TABLE IF NOT EXISTS announcement(
  ann_id TEXT PRIMARY KEY,
  exchange_column TEXT NOT NULL,
  sec_code TEXT,
  org_id TEXT,
  title TEXT NOT NULL,
  adjunct_url TEXT,
  published_at_utc TEXT,
  first_seen_at_utc TEXT NOT NULL,
  category TEXT,
  event_id TEXT,
  manifest_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation(
  obs_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  listing_id TEXT,
  payload_json TEXT NOT NULL,
  first_seen_at_utc TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'NEW',
  reason TEXT
);
CREATE TABLE IF NOT EXISTS event(
  event_id TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  type TEXT NOT NULL,
  published_at_utc TEXT,
  source_updated_at_utc TEXT,
  first_seen_at_utc TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'HYPOTHESIZED',
  lane_relevance TEXT
);
CREATE TABLE IF NOT EXISTS event_company(
  event_id TEXT NOT NULL REFERENCES event(event_id),
  company_id TEXT NOT NULL REFERENCES company(company_id),
  exposure_note TEXT,
  PRIMARY KEY(event_id, company_id)
);
CREATE TABLE IF NOT EXISTS evidence(
  evidence_id TEXT PRIMARY KEY,
  event_id TEXT,
  source_url TEXT NOT NULL,
  publisher_domain TEXT,
  published_at_utc TEXT,
  source_updated_at_utc TEXT,
  retrieved_at_utc TEXT NOT NULL,
  first_seen_at_utc TEXT NOT NULL,
  sha256 TEXT,
  retention_class TEXT NOT NULL,
  excerpt TEXT,
  dims_json TEXT NOT NULL,
  contradiction_state TEXT NOT NULL DEFAULT 'UNCONTESTED',
  contradiction_refs TEXT
);
CREATE TABLE IF NOT EXISTS candidate(
  candidate_id TEXT PRIMARY KEY,
  company_id TEXT NOT NULL REFERENCES company(company_id),
  lane TEXT NOT NULL,
  state TEXT NOT NULL,
  profile_json TEXT NOT NULL,
  gates_json TEXT NOT NULL,
  detected_at_utc TEXT NOT NULL,
  config_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS search_plan(
  plan_id TEXT PRIMARY KEY,
  route TEXT NOT NULL,
  created_from TEXT,
  company_id TEXT,
  plan_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',
  spend_json TEXT
);
CREATE TABLE IF NOT EXISTS frozen_artifact(
  artifact_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  candidate_id TEXT,
  version INTEGER NOT NULL,
  frozen_at_utc TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  path TEXT NOT NULL,
  config_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_snapshot(
  candidate_id TEXT NOT NULL,
  asof TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  PRIMARY KEY(candidate_id, asof)
);
CREATE INDEX IF NOT EXISTS idx_secday_date ON security_day(trade_date);
CREATE INDEX IF NOT EXISTS idx_ann_code ON announcement(sec_code, published_at_utc);
CREATE INDEX IF NOT EXISTS idx_listing_company ON listing(company_id);
"""


def db_path(data_dir: Path | None = None) -> Path:
    root = data_dir or DEFAULT_DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root / "investment.db"


def connect(data_dir: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(data_dir))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(DDL)
    return conn
