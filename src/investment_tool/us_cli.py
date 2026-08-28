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


def _fetch_manifested(conn, cfg, http, url: str, provider: str, dataset: str,
                      params: dict):
    resp = http.get(url)
    quality = Quality(
        QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
        f"http={resp.status_code}",
    )
    m = record_fetch(
        conn, provider=provider, dataset=dataset, params=params, source_url=url,
        payload=resp.content, http_status=resp.status_code, quality=quality,
        config_version=cfg.id,
    )
    return resp, m


def _live_efts_items(conn, cfg, http, date: str, audit: dict) -> None:
    """Items batch for the day's 8-Ks: paginated efts query (verified to
    reconcile with the daily index within ~2%)."""
    total = 0
    for start in range(0, 1000, 100):
        url = (f"{sec.EFTS_URL}?q=&forms=8-K&dateRange=custom&startdt={date}&enddt={date}"
               f"&from={start}")
        resp, _m = _fetch_manifested(conn, cfg, http, url, "sec", "efts_items",
                                     {"date": date, "from": start})
        if resp.status_code != 200:
            audit["channels"]["efts_items_error"] = f"http={resp.status_code} at from={start}"
            break
        total += us_ingest.enrich_items_from_efts(conn, resp.content)
        import json as json_mod

        page = len(json_mod.loads(resp.content.decode("utf-8"))
                   .get("hits", {}).get("hits", []))
        if page < 100:
            break
    audit["channels"]["efts_items"] = total


def _live_submissions_enrichment(conn, cfg, http, date: str, cap: int, audit: dict) -> None:
    """Acceptance/reportDate enrichment for filers whose filings can create or
    amend events — bounded, targeted, never universe-wide."""
    ciks = [r["cik"] for r in conn.execute(
        "SELECT DISTINCT cik FROM sec_filing WHERE filing_date=?"
        " AND (form LIKE '8-K%' OR form LIKE 'NT %' OR form IN ('25','25-NSE')"
        "      OR form LIKE 'SC 13D%' OR is_amendment=1)"
        " AND accepted_at_utc IS NULL LIMIT ?", (date, cap),
    )]
    done = 0
    for cik in ciks:
        url = sec.SUBMISSIONS_URL.format(cik10=str(cik).zfill(10))
        resp, _m = _fetch_manifested(conn, cfg, http, url, "sec", "submissions",
                                     {"cik": cik})
        if resp.status_code == 200:
            us_ingest.enrich_from_submissions(conn, resp.content)
            done += 1
    audit["channels"]["submissions"] = {"filers_fetched": done, "cap": cap,
                                        "eligible": len(ciks)}


def run_us_sync(conn, cfg, date: str, index_fixture: str | None,
                efts_fixture: str | None, submissions_fixtures: list[str],
                getcurrent_fixture: str | None, submissions_cap: int = 40) -> dict:
    """One pipeline for fixture and live modes: discover -> enrich -> route.
    Routing always runs AFTER enrichment; 8-Ks without items stay PENDING_ITEMS
    rather than degrading to neutral observations (staged classification)."""
    audit: dict = {"date": date, "generated_at": utc_now(), "config_version": cfg.id,
                   "channels": {}, "us_completeness": "PENDING_EVENING_INDEX"}
    fixture_mode = any([index_fixture, efts_fixture, submissions_fixtures,
                        getcurrent_fixture])
    http = None if fixture_mode else sec.client()  # identity gate up front in live mode

    if getcurrent_fixture:
        payload = Path(getcurrent_fixture).read_bytes()
        audit["channels"]["getcurrent"] = us_ingest.poll_getcurrent(conn, payload, "fixture")

    if index_fixture:
        result = us_ingest.ingest_daily_index(
            conn, Path(index_fixture).read_bytes(), date, "fixture")
        audit["channels"]["daily_index"] = result
        audit["us_completeness"] = result["us_completeness"]
    elif not fixture_mode:
        y, q = date[:4], (int(date[5:7]) + 2) // 3
        url = sec.DAILY_INDEX_URL.format(year=y, q=q, ymd=date.replace("-", ""))
        resp, m = _fetch_manifested(conn, cfg, http, url, "sec", "daily_index",
                                    {"date": date})
        if resp.status_code == 200:
            result = us_ingest.ingest_daily_index(conn, resp.content, date, m.manifest_id)
            audit["channels"]["daily_index"] = result
            audit["us_completeness"] = result["us_completeness"]
        else:
            audit["channels"]["daily_index"] = {
                "error": f"http={resp.status_code}",
                "note": "index may not exist yet (evening artifact)",
            }

    # enrichment BEFORE routing, in both modes
    if efts_fixture:
        audit["channels"]["efts_items"] = us_ingest.enrich_items_from_efts(
            conn, Path(efts_fixture).read_bytes())
    elif not fixture_mode and "error" not in audit["channels"].get("daily_index", {}):
        _live_efts_items(conn, cfg, http, date, audit)
    for sf in submissions_fixtures:
        audit["channels"].setdefault("submissions", 0)
        audit["channels"]["submissions"] += us_ingest.enrich_from_submissions(
            conn, Path(sf).read_bytes())
    if not fixture_mode and "error" not in audit["channels"].get("daily_index", {}):
        _live_submissions_enrichment(conn, cfg, http, date, submissions_cap, audit)

    audit["routing"] = us_route.route_unclassified(conn)
    audit["propagation"] = us_route.propagate_enrichment(conn)
    audit["amendments"] = us_route.link_amendments(conn)
    conn.execute(
        "UPDATE sec_filing SET review_state='PENDING' WHERE review_state IS NULL"
        " AND relevance='CONTENT_REVIEW_REQUIRED'")
    conn.commit()
    audit["review_queue_pending"] = conn.execute(
        "SELECT COUNT(*) FROM sec_filing WHERE review_state='PENDING'").fetchone()[0]
    audit["pending_items_8k"] = conn.execute(
        "SELECT COUNT(*) FROM sec_filing WHERE classification_version IS NULL"
        " AND form LIKE '8-K%'").fetchone()[0]
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
