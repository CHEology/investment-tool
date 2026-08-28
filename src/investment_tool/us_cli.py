"""US-spine CLI operations (invoked from cli.py). Fixture mode is fully
offline; live mode is gated by the SEC identity check and shares the global
rate limiter. External agents consume `export` output — never the database.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from investment_tool import us_ingest, us_map, us_route
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import record_fetch, utc_now
from investment_tool.providers import sec
from investment_tool.quality import Quality, QualityState


def _audit_write(name: str, audit: dict) -> Path:
    out = DEFAULT_DATA_DIR / "audit"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
    return path


def run_us_map(conn, cfg, fixture: str | None) -> dict:
    asof = datetime.now(UTC).strftime("%Y-%m-%d")
    if fixture:
        payload = Path(fixture).read_bytes()
        source = f"fixture:{Path(fixture).name}"
    else:
        http = sec.client()  # raises SecConfigError without a real identity
        resp = http.get(sec.TICKERS_EXCHANGE_URL)
        quality = Quality(
            QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
            f"http={resp.status_code}",
        )
        m = record_fetch(
            conn, provider="sec", dataset="company_tickers_exchange", params={},
            source_url=sec.TICKERS_EXCHANGE_URL, payload=resp.content,
            http_status=resp.status_code, quality=quality, config_version=cfg.id,
        )
        if resp.status_code != 200:
            return {"error": f"http={resp.status_code}", "manifest": m.manifest_id}
        payload, source = resp.content, "sec_tickers_exchange"
    rows = sec.parse_company_tickers_exchange(payload)
    sync = us_map.sync_cik_map(conn, rows, asof, source)
    hist = us_map.reconcile(conn, asof)
    audit = {"asof": asof, "generated_at": utc_now(), "source": source,
             "sync": sync, "reconciliation": hist}
    _audit_write(f"us_map_{asof}", audit)
    return audit


def run_us_sync(conn, cfg, date: str, index_fixture: str | None,
                efts_fixture: str | None, submissions_fixtures: list[str],
                getcurrent_fixture: str | None) -> dict:
    audit: dict = {"date": date, "generated_at": utc_now(), "config_version": cfg.id,
                   "channels": {}, "us_completeness": "PENDING_EVENING_INDEX"}
    if getcurrent_fixture:
        payload = Path(getcurrent_fixture).read_bytes()
        audit["channels"]["getcurrent"] = us_ingest.poll_getcurrent(conn, payload, "fixture")
    if index_fixture:
        payload = Path(index_fixture).read_bytes()
        result = us_ingest.ingest_daily_index(conn, payload, date, "fixture")
        audit["channels"]["daily_index"] = result
        audit["us_completeness"] = result["us_completeness"]
    elif not getcurrent_fixture:
        # live mode: identity-gated fetch of the day's master index
        http = sec.client()
        y, q = date[:4], (int(date[5:7]) + 2) // 3
        url = sec.DAILY_INDEX_URL.format(year=y, q=q, ymd=date.replace("-", ""))
        resp = http.get(url)
        quality = Quality(
            QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
            f"http={resp.status_code}",
        )
        m = record_fetch(
            conn, provider="sec", dataset="daily_index", params={"date": date},
            source_url=url, payload=resp.content, http_status=resp.status_code,
            quality=quality, config_version=cfg.id,
        )
        if resp.status_code == 200:
            result = us_ingest.ingest_daily_index(conn, resp.content, date, m.manifest_id)
            audit["channels"]["daily_index"] = result
            audit["us_completeness"] = result["us_completeness"]
        else:
            audit["channels"]["daily_index"] = {
                "error": f"http={resp.status_code}",
                "note": "index may not exist yet (evening artifact)",
            }
    if efts_fixture:
        audit["channels"]["efts_items"] = us_ingest.enrich_items_from_efts(
            conn, Path(efts_fixture).read_bytes())
    for sf in submissions_fixtures:
        audit.setdefault("channels", {}).setdefault("submissions", 0)
        audit["channels"]["submissions"] += us_ingest.enrich_from_submissions(
            conn, Path(sf).read_bytes())
    audit["routing"] = us_route.route_unclassified(conn)
    audit["amendments"] = us_route.link_amendments(conn)
    conn.execute(
        "UPDATE sec_filing SET review_state='PENDING' WHERE review_state IS NULL"
        " AND relevance='CONTENT_REVIEW_REQUIRED'")
    conn.commit()
    audit["review_queue_pending"] = conn.execute(
        "SELECT COUNT(*) FROM sec_filing WHERE review_state='PENDING'").fetchone()[0]
    _audit_write(f"us_sync_{date}", audit)
    return audit


def run_review(conn, cfg) -> dict:
    aging_days = int(cfg.value("operational.review_aging_days"))
    horizon = (datetime.now(UTC) - timedelta(days=aging_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    aged = conn.execute(
        "UPDATE sec_filing SET review_state='ARCHIVED_UNREVIEWED'"
        " WHERE review_state='PENDING' AND first_seen_at_utc < ?", (horizon,),
    ).rowcount
    conn.commit()
    out: dict = {"generated_at": utc_now(), "aged_to_archive": aged, "sections": {}}
    out["sections"]["us_review_pending"] = [
        dict(r) for r in conn.execute(
            "SELECT accession, cik, form, items_csv, filing_date, first_seen_at_utc"
            " FROM sec_filing WHERE review_state='PENDING' ORDER BY first_seen_at_utc DESC"
            " LIMIT 50")
    ]
    out["sections"]["us_ambiguous_amendments"] = [
        dict(r) for r in conn.execute(
            "SELECT accession, cik, form, report_period FROM sec_filing"
            " WHERE amend_link_state='AMBIGUOUS'")
    ]
    out["sections"]["a_share_awaiting_content_review"] = [
        {"candidate_id": r["candidate_id"], "company_id": r["company_id"]}
        for r in conn.execute(
            "SELECT candidate_id, company_id FROM candidate"
            " WHERE state='AWAITING_CONTENT_REVIEW'")
    ]
    out["sections"]["open_search_plans"] = conn.execute(
        "SELECT COUNT(*) FROM search_plan WHERE status='OPEN'").fetchone()[0]
    return out


def run_export(conn, candidate_id: str) -> Path:
    cand = conn.execute("SELECT * FROM candidate WHERE candidate_id=?",
                        (candidate_id,)).fetchone()
    if cand is None:
        raise SystemExit(f"no candidate {candidate_id}")
    bundle = {
        "exported_at": utc_now(),
        "candidate": dict(cand),
        "artifacts": [dict(r) for r in conn.execute(
            "SELECT artifact_id, version, frozen_at_utc, content_sha256, status"
            " FROM frozen_artifact WHERE candidate_id=?", (candidate_id,))],
        "validation": [dict(r) for r in conn.execute(
            "SELECT asof, metrics_json FROM validation_snapshot WHERE candidate_id=?",
            (candidate_id,))],
        "note": "external agents consume this bundle; the database is not an interface",
    }
    out_dir = DEFAULT_DATA_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"candidate_{candidate_id}.json"
    path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str))
    return path
