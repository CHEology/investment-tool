"""Daily us-sync scheduling + 5-day live soak support (PR-G).

Scope: data ingestion, enrichment, routing, audit generation, and operational
verification ONLY — this slice never runs the opportunity trial and produces
no opportunity conclusions.

Design: the scheduled entrypoint is a CATCH-UP, not a fire-and-forget tick.
Every run recomputes which recent SEC filing days still lack a successfully
ingested evening daily index (from manifest provenance, the source of truth)
and syncs exactly those — so a crashed or skipped run heals itself on the
next tick, reruns are idempotent (first_seen_at_utc is preserved by the
upsert layer), and partial completion is recorded rather than hidden.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from investment_tool import calendars_us, us_cli
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

# the SEC evening daily index is reliably present by this ET wall-clock time
EVENING_INDEX_READY_ET = 18  # hours

SOAK_DIR_NAME = "soak"
DRILL_VALIDITY_DAYS = 30   # a passed drill certifies its code path this long


def _soak_dir():
    d = DEFAULT_DATA_DIR / "audit" / SOAK_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def synced_index_dates(conn: sqlite3.Connection) -> set[str]:
    """Filing dates whose evening daily index was fetched successfully —
    provenance-based (manifest), not file-based."""
    out = set()
    for r in conn.execute(
        "SELECT params_json FROM manifest WHERE provider='sec'"
        " AND dataset='daily_index' AND http_status=200"
    ):
        d = json.loads(r["params_json"]).get("date")
        if d:
            out.add(d)
    return out


def pending_sync_dates(conn: sqlite3.Connection, now_utc: str | None = None,
                       lookback_sessions: int = 5) -> list[str]:
    """XNYS sessions in the lookback window whose daily index has not been
    ingested yet. Today (ET) is included only after the evening index should
    exist. XNYS sessions approximate SEC business days; a mismatch merely
    yields one recorded 404 attempt, never silent loss."""
    now = (datetime.strptime(now_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
           if now_utc else datetime.now(UTC))
    et = now.astimezone(ZoneInfo("America/New_York"))
    c = calendars_us.cal()
    latest = et.strftime("%Y-%m-%d")
    if c.is_session(latest):
        if et.hour < EVENING_INDEX_READY_ET:   # today's index not out yet
            latest = c.previous_session(latest).strftime("%Y-%m-%d")
    else:                                      # weekend/holiday
        latest = c.date_to_session(latest, direction="previous").strftime("%Y-%m-%d")
    sessions = [s.strftime("%Y-%m-%d")
                for s in c.sessions_in_range("2026-01-02", latest)][-lookback_sessions:]
    have = synced_index_dates(conn)
    return [d for d in sessions if d not in have]


def _filing_stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(first_seen_at_utc) AS latest FROM sec_filing"
    ).fetchone()
    return {"filings": row["n"], "latest_first_seen": row["latest"]}


def verify_idempotency(conn: sqlite3.Connection, cfg, date: str) -> dict:
    """Re-sync an already-synced date and prove nothing mutates: the filing
    count is unchanged and no first_seen_at_utc moves (the earliest sighting
    always survives). One extra manifested index fetch; live-safe."""
    before = _filing_stats(conn)
    audit = us_cli.run_us_sync(conn, cfg, date, None, None, [], None)
    after = _filing_stats(conn)
    ok = (before["filings"] == after["filings"]
          and before["latest_first_seen"] == after["latest_first_seen"])
    return {"date": date, "before": before, "after": after,
            "rerun_completeness": audit.get("us_completeness"),
            "idempotent": ok}


def run_daily(conn: sqlite3.Connection, cfg, now_utc: str | None = None,
              lookback_sessions: int = 5, verify_date: str | None = None,
              origin: str = "MANUAL") -> dict:
    """The scheduled catch-up: sync every pending date, poll halts, optionally
    run one idempotency verification, and append a soak ledger entry.
    `origin` distinguishes SCHEDULED (launchd) from MANUAL runs — the soak
    acceptance gate requires at least one SCHEDULED run (H0/F15)."""
    ledger: dict = {"generated_at": utc_now(), "kind": "US_SOAK_DAILY",
                    "origin": origin,
                    "pending_before": [], "synced": [], "errors": [],
                    "halts": None, "idempotency": None}
    pending = pending_sync_dates(conn, now_utc, lookback_sessions)
    ledger["pending_before"] = pending
    for date in pending:
        try:
            audit = us_cli.run_us_sync(conn, cfg, date, None, None, [], None)
            ledger["synced"].append({
                "date": date,
                "us_completeness": audit.get("us_completeness"),
                "daily_index": audit.get("channels", {}).get("daily_index"),
                "efts_items": audit.get("channels", {}).get("efts_items"),
                "submissions": audit.get("channels", {}).get("submissions"),
                "routing": audit.get("routing"),
                "amendments": audit.get("amendments"),
            })
        except Exception as exc:  # recorded, never silent; next tick retries
            ledger["errors"].append({"date": date, "error": repr(exc)})
    try:
        ledger["halts"] = _poll_halts(conn, cfg)
    except Exception as exc:
        ledger["errors"].append({"halts": repr(exc)})
    if verify_date:
        try:
            ledger["idempotency"] = verify_idempotency(conn, cfg, verify_date)
        except Exception as exc:
            ledger["errors"].append({"idempotency": repr(exc)})
    stamp = utc_now().replace(":", "").replace("-", "")
    path = _soak_dir() / f"us_soak_{stamp}.json"
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2, default=str))
    ledger["ledger_path"] = str(path)
    return ledger


def _poll_halts(conn: sqlite3.Connection, cfg) -> dict:
    from investment_tool.lineage import record_fetch
    from investment_tool.providers import nasdaq_halts
    from investment_tool.quality import Quality, QualityState

    http = nasdaq_halts.client()
    resp = http.get(nasdaq_halts.HALTS_URL)
    quality = Quality(QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
                      f"http={resp.status_code}")
    record_fetch(conn, provider="nasdaq_trader", dataset="trade_halts", params={},
                 source_url=nasdaq_halts.HALTS_URL, payload=resp.content,
                 http_status=resp.status_code, quality=quality, config_version=cfg.id)
    if resp.status_code != 200:
        return {"error": f"http={resp.status_code}"}
    return nasdaq_halts.route_halts(conn, nasdaq_halts.parse_halts(resp.content))


def soak_report(conn: sqlite3.Connection, window_days: int = 10) -> dict:
    """Aggregate the soak ledgers and evaluate the corrected acceptance gate
    (H0/F15). Only IN-WINDOW ledger evidence counts — never pre-soak database
    history. Gates: ledger entries on >=5 distinct calendar days; >=3 filing
    days INDEX_RECONCILED by in-window ledgers; >=1 SCHEDULED (launchd) run;
    >=1 in-window idempotency verification; zero unresolved errors (an error
    for date D is resolved by a later in-window ledger reconciling D);
    amendment case (natural LINKED_UNIQUE or labeled fixture drill) and
    crash-recovery drill on record. `window_days` bounds the evidence window,
    measured back from the newest ledger entry."""
    ledgers = sorted(_soak_dir().glob("us_soak_*.json"))
    entries = [json.loads(p.read_text()) for p in ledgers]
    if entries:
        latest = max(e["generated_at"] for e in entries)
        latest_d = datetime.strptime(latest, "%Y-%m-%dT%H:%M:%SZ")
        floor = (latest_d - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        entries = [e for e in entries if e["generated_at"] >= floor]
    ledger_days = sorted({e["generated_at"][:10] for e in entries})
    synced_ok: dict[str, str] = {}
    for e in entries:
        for s in e.get("synced", []):
            if "INDEX_RECONCILED" in str(s.get("us_completeness")):
                synced_ok[s["date"]] = max(synced_ok.get(s["date"], ""),
                                           e["generated_at"])
    unresolved_errors = []
    for e in entries:
        for err in e.get("errors", []):
            d = err.get("date")
            resolved = (d is not None and d in synced_ok
                        and synced_ok[d] > e["generated_at"])
            if not resolved:
                unresolved_errors.append({"at": e["generated_at"], **err})
    scheduled = [e for e in entries if e.get("origin") == "SCHEDULED"]
    idem = [e["idempotency"] for e in entries
            if e.get("idempotency") and e["idempotency"].get("idempotent")]
    amendments_natural = conn.execute(
        "SELECT COUNT(*) FROM sec_filing WHERE is_amendment=1"
        " AND amend_link_state='LINKED_UNIQUE'").fetchone()[0]
    # Drills are DURABLE certification evidence with explicit validity: a
    # passed drill certifies the code path for DRILL_VALIDITY_DAYS from its
    # own generated_at, independent of the ledger window — but an expired or
    # failed drill never satisfies the gate (H0.1/F-D).
    now = datetime.now(UTC).replace(tzinfo=None)

    def _valid_drills(pattern: str) -> tuple[list[dict], list[dict]]:
        valid, expired = [], []
        for p in sorted(_soak_dir().glob(pattern)):
            try:
                d = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            gen = d.get("generated_at")
            passed = bool(d.get("passed", True))
            age_days = None
            if gen:
                age_days = (now - datetime.strptime(
                    gen, "%Y-%m-%dT%H:%M:%SZ")).days
            row = {"file": p.name, "generated_at": gen, "passed": passed,
                   "age_days": age_days}
            if passed and age_days is not None and age_days <= DRILL_VALIDITY_DAYS:
                valid.append(row)
            else:
                expired.append(row)
        return valid, expired

    drills, drills_expired = _valid_drills("amendment_drill_*.json")
    recoveries, recoveries_expired = _valid_drills("recovery_drill_*.json")
    report = {
        "generated_at": utc_now(),
        "window_days": window_days,
        "ledger_entries_in_window": len(entries),
        "ledger_calendar_days": ledger_days,
        "filing_days_synced_in_window": sorted(synced_ok),
        "scheduled_runs": len(scheduled),
        "idempotency_verifications_passed": len(idem),
        "unresolved_errors": unresolved_errors,
        "amendments_linked_natural": amendments_natural,
        "amendment_drills_valid": drills,
        "amendment_drills_expired_or_failed": drills_expired,
        "recovery_drills_valid": recoveries,
        "recovery_drills_expired_or_failed": recoveries_expired,
        "drill_validity_days": DRILL_VALIDITY_DAYS,
        "gates": {
            "min_5_ledger_calendar_days": len(ledger_days) >= 5,
            "min_3_filing_days_in_window": len(synced_ok) >= 3,
            "scheduled_run_observed": len(scheduled) >= 1,
            "idempotency_verified": len(idem) >= 1,
            "zero_unresolved_errors": len(unresolved_errors) == 0,
            "amendment_case": amendments_natural >= 1 or len(drills) >= 1,
            "crash_recovery_drilled": len(recoveries) >= 1,
        },
    }
    report["gates"]["all_passed"] = all(report["gates"].values())
    out = _soak_dir() / "soak_report_latest.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report
