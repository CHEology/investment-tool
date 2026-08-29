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
  -- Canonical RAW reported prices (NULL when the source serves only adjusted).
  open TEXT, high TEXT, low TEXT, close TEXT, prev_close TEXT,
  volume TEXT,           -- shares (normalized; lot-based sources multiplied out)
  amount TEXT,           -- turnover in listing currency; NULL if source lacks it
  -- Analytical primitives: daily return + adjusted close, basis-labeled.
  ret REAL,              -- daily return fraction; basis per ret_basis
  ret_basis TEXT,        -- EXCHANGE_PCT | QFQ_CONSEC | SYNTH_COMPOUND | RAW_CONSEC
  adj_close TEXT,        -- adjusted close (qfq lineage); NULL when unknown
  basis_epoch INTEGER NOT NULL DEFAULT 1,  -- bumped when adjusted history is rewritten
  pct_chg REAL,          -- legacy; superseded by ret
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
  ts_precision TEXT NOT NULL DEFAULT 'DATE',  -- DATE | TIME
  ts_anomaly TEXT,                            -- e.g. FIRST_SEEN_BEFORE_PUBLISHED
  first_seen_at_utc TEXT NOT NULL,
  category TEXT,
  relevance TEXT,        -- HARD_NEGATIVE | CONTENT_REVIEW_REQUIRED | POSITIVE | NEUTRAL
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
  config_version TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'VALID',   -- VALID | SUPERSEDED | INVALIDATED
  status_note TEXT
);
CREATE TABLE IF NOT EXISTS validation_snapshot(
  candidate_id TEXT NOT NULL,
  asof TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  PRIMARY KEY(candidate_id, asof)
);
CREATE TABLE IF NOT EXISTS research_queue(
  queue_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  candidate_id TEXT,
  company_id TEXT NOT NULL,
  listing_id TEXT NOT NULL,
  ticker TEXT,
  asof TEXT NOT NULL,
  state TEXT NOT NULL,      -- RESEARCH_PENDING | RESEARCH_IN_PROGRESS |
                            -- DOC_REVIEW_COMPLETED | FETCH_FAILED |
                            -- DATA_UNAVAILABLE | REJECTED | SUPERSEDED
  rank_score REAL,
  rank_version TEXT,
  rank_inputs_json TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  enqueued_at_utc TEXT NOT NULL,
  updated_at_utc TEXT NOT NULL,
  config_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_rq_state_rank
  ON research_queue(state, rank_score DESC);
CREATE TABLE IF NOT EXISTS schema_migration(
  migration_id TEXT PRIMARY KEY,
  applied_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS market_snapshot(
  listing_id TEXT NOT NULL,
  asof_date TEXT NOT NULL,
  name TEXT,
  total_mcap TEXT,
  float_mcap TEXT,
  industry TEXT,
  is_st INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL,
  quality TEXT NOT NULL,
  manifest_id TEXT,
  PRIMARY KEY(listing_id, asof_date)
);
CREATE TABLE IF NOT EXISTS sec_filing(
  accession TEXT PRIMARY KEY,
  cik TEXT NOT NULL,
  form TEXT NOT NULL,
  is_amendment INTEGER NOT NULL DEFAULT 0,
  amends_accession TEXT,
  amend_link_state TEXT,
  filing_date TEXT NOT NULL,
  report_period TEXT,
  accepted_at_utc TEXT,
  items_csv TEXT,
  primary_doc_name TEXT,
  primary_doc_url TEXT,
  source_updated_at_utc TEXT,
  first_seen_at_utc TEXT NOT NULL,
  classification_version TEXT,
  relevance TEXT,
  event_id TEXT,
  supersession_state TEXT NOT NULL DEFAULT 'ACTIVE',
  quality TEXT NOT NULL,
  manifest_id TEXT NOT NULL,
  review_state TEXT
);
CREATE INDEX IF NOT EXISTS idx_secfiling_cik ON sec_filing(cik, form, filing_date);
CREATE INDEX IF NOT EXISTS idx_secfiling_seen ON sec_filing(first_seen_at_utc);
CREATE TABLE IF NOT EXISTS sec_filing_document(
  accession TEXT NOT NULL,
  filename TEXT NOT NULL,
  doc_type TEXT,
  url TEXT NOT NULL,
  sha256 TEXT,
  manifest_id TEXT,
  PRIMARY KEY(accession, filename)
);
CREATE TABLE IF NOT EXISTS filing_party(
  accession TEXT NOT NULL,
  cik TEXT NOT NULL,
  role TEXT NOT NULL,
  listing_match_json TEXT,
  PRIMARY KEY(accession, cik, role)
);
CREATE TABLE IF NOT EXISTS cik_map(
  cik TEXT NOT NULL,
  ticker TEXT NOT NULL,
  exchange TEXT,
  name TEXT,
  state TEXT NOT NULL,
  source TEXT NOT NULL,
  valid_from_date TEXT NOT NULL,
  valid_to_date TEXT,
  stale_since_date TEXT,
  PRIMARY KEY(cik, ticker, valid_from_date)
);
CREATE TABLE IF NOT EXISTS source_checkpoint(
  source_id TEXT PRIMARY KEY,
  cursor TEXT,
  updated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_secday_date ON security_day(trade_date);
CREATE INDEX IF NOT EXISTS idx_ann_code ON announcement(sec_code, published_at_utc);
CREATE INDEX IF NOT EXISTS idx_listing_company ON listing(company_id);
"""

# SQLite's CREATE TABLE IF NOT EXISTS does not add columns to an existing
# table.  Keep additive upgrades explicit so an S0 data directory can be
# opened safely by S1 without a manual one-off ALTER TABLE session.
ADDITIVE_MIGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "security_day": (
        ("ret", "REAL"),
        ("ret_basis", "TEXT"),
        ("adj_close", "TEXT"),
        ("basis_epoch", "INTEGER NOT NULL DEFAULT 1"),
    ),
    "announcement": (
        ("ts_precision", "TEXT NOT NULL DEFAULT 'DATE'"),
        ("ts_anomaly", "TEXT"),
        ("relevance", "TEXT"),
    ),
    "frozen_artifact": (
        ("status", "TEXT NOT NULL DEFAULT 'VALID'"),
        ("status_note", "TEXT"),
    ),
    "sec_filing": (
        ("review_state", "TEXT"),
    ),
    "cik_map": (
        ("stale_since_date", "TEXT"),
    ),
}


def db_path(data_dir: Path | None = None) -> Path:
    root = data_dir or DEFAULT_DATA_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root / "investment.db"


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply idempotent, additive migrations needed by the current code.

    Identifiers and SQL fragments come only from the trusted constant above;
    no external input is interpolated here.
    """
    for table, columns in ADDITIVE_MIGRATIONS.items():
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, declaration in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration(migration_id, applied_at_utc)"
        " VALUES('s1_additive_columns', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration(migration_id, applied_at_utc)"
        " VALUES('s2a_us_tables', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration(migration_id, applied_at_utc)"
        " VALUES('pr_a_research_queue', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
    )
    rename_done = conn.execute(
        "SELECT 1 FROM schema_migration WHERE migration_id='h0_doc_review_rename'"
    ).fetchone()
    if rename_done is None:
        conn.execute(
            "UPDATE research_queue SET state='DOC_REVIEW_COMPLETED'"
            " WHERE state='RESEARCH_COMPLETED'"
        )
        conn.execute(
            "INSERT INTO schema_migration(migration_id, applied_at_utc)"
            " VALUES('h0_doc_review_rename', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
    lifecycle_done = conn.execute(
        "SELECT 1 FROM schema_migration WHERE migration_id='s1_artifact_lifecycle'"
    ).fetchone()
    if lifecycle_done is None:
        conn.execute(
            "UPDATE frozen_artifact AS older SET status='SUPERSEDED',"
            " status_note=COALESCE(status_note, 'Superseded by a later frozen version')"
            " WHERE status='VALID' AND EXISTS (SELECT 1 FROM frozen_artifact AS newer"
            " WHERE newer.candidate_id=older.candidate_id AND newer.kind=older.kind"
            " AND newer.version>older.version AND newer.status='VALID')"
        )
        conn.execute(
            "INSERT INTO schema_migration(migration_id, applied_at_utc)"
            " VALUES('s1_artifact_lifecycle', strftime('%Y-%m-%dT%H:%M:%SZ','now'))"
        )
    conn.commit()


def connect(data_dir: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(data_dir))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(DDL)
    _migrate_schema(conn)
    return conn
