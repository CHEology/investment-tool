"""`invest` CLI — the single operator surface. Agent environments call this;
they never touch the database directly (approved integration boundary)."""

from __future__ import annotations

import argparse
import sys

from investment_tool import config as config_mod
from investment_tool import entities
from investment_tool.db import connect
from investment_tool.lineage import record_fetch
from investment_tool.quality import Quality, QualityState


def _cfg(conn):
    cfg = config_mod.load("v0.2")
    config_mod.register(
        conn, cfg,
        changelog="v0.2: operational keys only; thresholds identical to v0.1/v0",
    )
    return cfg


def cmd_seed(args) -> int:
    conn = connect()
    cfg = _cfg(conn)
    market = args.market

    if market in ("a", "all"):
        from investment_tool.providers import cninfo

        http = cninfo.client()
        payload, status = cninfo.fetch_security_mapping(http)
        state = QualityState.OK if status == 200 else QualityState.ERROR
        quality = Quality(state, f"http={status}")
        m = record_fetch(
            conn, provider="cninfo", dataset="security_mapping", params={},
            source_url=cninfo.MAPPING_URL, payload=payload, http_status=status,
            quality=quality, config_version=cfg.id,
        )
        if not quality.usable_for_scan:
            print(f"cninfo mapping fetch failed: http={status} (manifest {m.manifest_id})")
            return 1
        rows = cninfo.parse_security_mapping(payload)
        n = entities.seed_a_share(conn, rows)
        by_ex = {}
        for r in rows:
            by_ex[r["exchange"]] = by_ex.get(r["exchange"], 0) + 1
        print(f"A-share seed: {n} listings {by_ex} (manifest {m.manifest_id})")

    if market in ("us", "all"):
        from investment_tool.providers import nasdaq
        from investment_tool.providers.base import GENERIC_UA, HttpClient

        http = HttpClient(user_agent=GENERIC_UA, min_interval_s=1.0)
        total = 0
        for url, dataset, parser in (
            (nasdaq.NASDAQ_LISTED_URL, "nasdaqlisted", nasdaq.parse_nasdaq_listed),
            (nasdaq.OTHER_LISTED_URL, "otherlisted", nasdaq.parse_other_listed),
        ):
            resp = http.get(url)
            quality = Quality(
                QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
                f"http={resp.status_code}",
            )
            m = record_fetch(
                conn, provider="nasdaq_trader", dataset=dataset, params={},
                source_url=url, payload=resp.content, http_status=resp.status_code,
                quality=quality, config_version=cfg.id,
            )
            if not quality.usable_for_scan:
                print(f"{dataset} fetch failed: http={resp.status_code}")
                return 1
            rows = parser(resp.content.decode("utf-8", errors="replace"))
            total += entities.seed_us(conn, rows)
            print(f"US seed [{dataset}]: {len(rows)} rows (manifest {m.manifest_id})")
        print(f"US seed total listings inserted-or-present: {total}")
        print("note: CIK enrichment deferred to S2 (requires SEC_USER_AGENT, decision D3)")

    return 0


def cmd_resolve(args) -> int:
    conn = connect()
    rows = entities.resolve(conn, args.ticker)
    if not rows:
        print(f"no listing found for {args.ticker!r}")
        return 1
    for r in rows:
        name = r["name_zh"] or r["name_en"] or ""
        print(
            f"{r['listing_id']}  company={r['company_id']}  {name}  board={r['board'] or '-'}"
            f"  ccy={r['currency']}  adr={r['is_adr']}  cik={r['cik'] or 'pending-D3'}"
        )
    return 0


def cmd_fx(args) -> int:
    conn = connect()
    cfg = _cfg(conn)
    from investment_tool.providers import frankfurter as fx

    http = fx.client()
    payload, status, url = fx.fetch_rate(http, args.date)
    quality = Quality(QualityState.OK if status == 200 else QualityState.ERROR, f"http={status}")
    m = record_fetch(
        conn, provider="frankfurter", dataset="usd_cny", params={"date": args.date or "latest"},
        source_url=url, payload=payload, http_status=status, quality=quality,
        config_version=cfg.id,
    )
    if not quality.usable_for_scan:
        print(f"fx fetch failed http={status}")
        return 1
    date, rate = fx.parse_rate(payload)
    conn.execute(
        "INSERT OR REPLACE INTO fx_day(pair, date, rate, source, manifest_id) VALUES(?,?,?,?,?)",
        ("USDCNY", date, rate, "frankfurter_ecb", m.manifest_id),
    )
    conn.commit()
    print(f"USDCNY {date} = {rate} (manifest {m.manifest_id})")
    return 0


def cmd_ingest_eod(args) -> int:
    from datetime import UTC, datetime

    from investment_tool import spine

    conn = connect()
    cfg = _cfg(conn)
    date = args.date or datetime.now(UTC).strftime("%Y-%m-%d")
    result = spine.ingest_snapshot(conn, cfg.id, date)
    print(f"snapshot ingest: {result}")
    return 0 if "error" not in result else 1


def cmd_ingest_benchmarks(args) -> int:
    from investment_tool import spine

    conn = connect()
    cfg = _cfg(conn)
    result = spine.ingest_benchmarks(conn, cfg.id, beg=spine.default_beg())
    print(f"benchmarks: {result}")
    return 0


def cmd_backfill(args) -> int:
    from investment_tool import spine
    from investment_tool.providers import sina, tencent

    conn = connect()
    cfg = _cfg(conn)
    q = (
        "SELECT l.listing_id, l.ticker, l.exchange, l.board FROM listing l"
        " WHERE l.exchange IN ('SSE','SZSE','BSE') AND l.status='LISTED'"
        " AND l.listing_id IN (SELECT listing_id FROM market_snapshot WHERE"
        " asof_date=(SELECT MAX(asof_date) FROM market_snapshot))"
    )
    params: list = []
    if args.ticker:
        q += " AND l.ticker=?"
        params.append(args.ticker)
    if args.industry:
        q += (" AND l.listing_id IN (SELECT listing_id FROM market_snapshot WHERE industry=?"
              " AND asof_date=(SELECT MAX(asof_date) FROM market_snapshot))")
        params.append(args.industry)
    rows = conn.execute(q + " ORDER BY l.ticker", params).fetchall()
    if args.shard:
        i, n = (int(x) for x in args.shard.split("/"))
        rows = [r for idx, r in enumerate(rows) if idx % n == i - 1]
    if args.limit:
        rows = rows[: args.limit]
    if args.skip_existing:
        min_history = int(cfg.value("universe.min_history_days"))
        have = {
            r["listing_id"]
            for r in conn.execute(
                "SELECT listing_id, COUNT(ret) AS n FROM security_day"
                " GROUP BY listing_id HAVING n>=?",
                (min_history,),
            )
        }
        rows = [r for r in rows if r["listing_id"] not in have]
    import time as time_mod

    http_by_provider = {"tencent": tencent.client(), "sina": sina.client()}
    beg = spine.default_beg()
    total_bars = 0
    failures = 0
    consecutive = 0
    breaker_trips = 0
    aborted = False
    processed = 0
    for i, lst in enumerate(rows, 1):
        processed = i
        try:
            result = spine.backfill_listing(conn, http_by_provider, cfg.id, lst, beg)
            total_bars += result.bars
            if result.bars > 0:
                consecutive = 0
            else:
                # empty history (delisted/suspended) is data, not provider
                # unhealth: only count toward the breaker on fetch ERRORs
                if result.quality_state == "ERROR":
                    failures += 1
                    consecutive += 1
                else:
                    consecutive = 0
        except Exception as exc:  # noqa: BLE001 - keep the run alive; each failure is manifested
            failures += 1
            consecutive += 1
            print(f"  ! {lst['ticker']}: {exc}")
        if consecutive >= 8:
            # circuit breaker: sustained failures mean the provider is refusing
            # us — cool down once, then stop safely (resume via --skip-existing).
            breaker_trips += 1
            if breaker_trips >= 2:
                print(f"circuit breaker: aborting at {i}/{len(rows)};"
                      " resume later with --skip-existing")
                aborted = True
                break
            print("circuit breaker: 8 consecutive empty/failed; cooling down 120s")
            time_mod.sleep(120)
            consecutive = 0
        if i % 250 == 0:
            print(f"  backfill progress: {i}/{len(rows)} listings, {total_bars} bars")
    q = conn.execute(
        "SELECT provider, quality_state, COUNT(*) AS n FROM manifest"
        " WHERE dataset='kline_daily' GROUP BY 1,2"
    ).fetchall()
    print("provider health:", [(r["provider"], r["quality_state"], r["n"]) for r in q])
    outcome = "incomplete" if aborted else "done"
    print(f"backfill {outcome}: {processed}/{len(rows)} listings processed, {total_bars} bars,"
          f" {failures} failures, breaker_trips={breaker_trips}")
    return 1 if aborted else 0


def cmd_scan(args) -> int:
    import json as json_mod

    from investment_tool import cards as cards_mod
    from investment_tool import lane_a

    conn = connect()
    cfg = _cfg(conn)
    audit = lane_a.run_scan(conn, cfg, args.date)
    if "error" in audit:
        print(f"scan aborted: {audit['error']}")
        return 1
    # freeze cards for exactly the candidates this scan touched (assessed states)
    touched = audit.get("touched_candidates") or []
    rows = []
    if touched:
        marks = ",".join("?" for _ in touched)
        rows = conn.execute(
            f"SELECT * FROM candidate WHERE candidate_id IN ({marks})"  # noqa: S608
            " AND state NOT IN ('PENDING_ATTRIBUTION','ATTRIBUTION_FETCH_DEGRADED')",
            touched,
        ).fetchall()
    for r in rows:
        content = cards_mod.render_card_zh(conn, r)
        frozen = cards_mod.freeze_card(conn, r, content)
        print(f"card frozen: {r['state']} -> {frozen['path']} (sha {frozen['sha256'][:12]})")
    print(json_mod.dumps({k: v for k, v in audit.items() if k != "trigger_detail"},
                         ensure_ascii=False, indent=2, default=str))
    print(f"trigger detail: {len(audit.get('trigger_detail', []))} rows (see audit file)")
    return 0


def cmd_credentials(args) -> int:
    """Store a provider token in the macOS Keychain via hidden interactive
    input. The token never appears in shell history, process listings, logs,
    fixtures, or manifests (review issue 3)."""
    import getpass

    try:
        import keyring
    except ImportError:
        print("keyring package missing; run: uv pip install -e '.[dev]'")
        return 1
    service = ("investment-tool-sec-ua" if args.provider == "sec"
               else f"investment-tool-{args.provider}")
    token = getpass.getpass(f"Value for {service} (input hidden): ")
    if not token.strip():
        print("empty token; nothing stored")
        return 1
    keyring.set_password(service, getpass.getuser(), token.strip())
    print(f"stored in Keychain service '{service}' (account {getpass.getuser()})")
    return 0


def cmd_validate(args) -> int:
    import json as json_mod

    from investment_tool import validate as validate_mod

    conn = connect()
    _cfg(conn)
    audit = validate_mod.run_validation(conn, args.asof)
    print(json_mod.dumps(audit, ensure_ascii=False, indent=2))
    return 0


def cmd_daily(args) -> int:
    """A-share daily operations glue over EXISTING steps only (S1.9V):
    benchmarks/calendar refresh -> trading-day check -> snapshot -> scan ->
    backup. Idempotent; degraded steps set exit code 2 and are named."""
    from datetime import UTC, datetime

    from investment_tool import spine
    from investment_tool import validate as validate_mod

    conn = connect()
    cfg = _cfg(conn)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    status: dict[str, str] = {}
    degraded = False

    try:
        bench = spine.ingest_benchmarks(conn, cfg.id, beg=spine.default_beg(45))
        status["benchmarks"] = str(bench)
    except Exception as exc:  # noqa: BLE001 - each step degrades independently
        status["benchmarks"] = f"DEGRADED: {exc}"
        degraded = True

    trading = conn.execute(
        "SELECT 1 FROM calendar_day WHERE exchange='SZSE' AND date=? AND is_trading=1",
        (today,),
    ).fetchone()
    if trading is None:
        status["trading_day"] = "NO (calendar); snapshot/scan skipped"
    else:
        try:
            result = spine.ingest_snapshot(conn, cfg.id, today)
            status["snapshot"] = str({k: result[k] for k in ("rows", "pages") if k in result})
            if "error" in result:
                degraded = True
        except Exception as exc:  # noqa: BLE001
            status["snapshot"] = f"DEGRADED: {exc}"
            degraded = True
        try:
            from investment_tool import lane_a

            audit = lane_a.run_scan(conn, cfg, today)
            status["scan"] = ("error: " + audit["error"]) if "error" in audit else (
                f"triggers={audit['triggers']} candidates={audit['candidates']}"
            )
        except Exception as exc:  # noqa: BLE001
            status["scan"] = f"DEGRADED: {exc}"
            degraded = True

    try:
        vaudit = validate_mod.run_validation(conn)
        status["validate"] = str(vaudit["states"])
    except Exception as exc:  # noqa: BLE001
        status["validate"] = f"DEGRADED: {exc}"
        degraded = True

    try:
        status["backup"] = validate_mod.backup_database(conn)
    except Exception as exc:  # noqa: BLE001
        status["backup"] = f"DEGRADED: {exc}"
        degraded = True

    for k, v in status.items():
        print(f"{k}: {v}")
    return 2 if degraded else 0


def cmd_us_map(args) -> int:
    import json as json_mod

    from investment_tool import us_cli

    conn = connect()
    cfg = _cfg(conn)
    audit = us_cli.run_us_map(conn, cfg, args.fixture)
    print(json_mod.dumps(audit, ensure_ascii=False, indent=2, default=str))
    return 0 if "error" not in audit else 2


def cmd_us_sync(args) -> int:
    import json as json_mod

    from investment_tool import us_cli

    conn = connect()
    cfg = _cfg(conn)
    audit = us_cli.run_us_sync(conn, cfg, args.date, args.index_fixture, args.efts_fixture,
                               args.submissions_fixture or [], args.getcurrent_fixture,
                               submissions_cap=args.submissions_cap)
    print(json_mod.dumps(audit, ensure_ascii=False, indent=2, default=str))
    degraded = any(isinstance(v, dict) and "error" in v
                   for v in audit.get("channels", {}).values())
    return 2 if degraded else 0


def cmd_halts(args) -> int:
    import json as json_mod
    from pathlib import Path

    from investment_tool.lineage import record_fetch
    from investment_tool.providers import nasdaq_halts
    from investment_tool.quality import Quality, QualityState

    conn = connect()
    cfg = _cfg(conn)
    if args.fixture:
        payload = Path(args.fixture).read_bytes()
    else:
        http = nasdaq_halts.client()
        resp = http.get(nasdaq_halts.HALTS_URL)
        quality = Quality(QualityState.OK if resp.status_code == 200 else QualityState.ERROR,
                          f"http={resp.status_code}")
        record_fetch(conn, provider="nasdaq_trader", dataset="trade_halts", params={},
                     source_url=nasdaq_halts.HALTS_URL, payload=resp.content,
                     http_status=resp.status_code, quality=quality, config_version=cfg.id)
        if resp.status_code != 200:
            print(f"halts fetch failed http={resp.status_code}")
            return 2
        payload = resp.content
    hist = nasdaq_halts.route_halts(conn, nasdaq_halts.parse_halts(payload))
    print(json_mod.dumps(hist, indent=2))
    return 0


def cmd_review(args) -> int:
    import json as json_mod

    from investment_tool import us_cli

    conn = connect()
    cfg = _cfg(conn)
    print(json_mod.dumps(us_cli.run_review(conn, cfg), ensure_ascii=False, indent=2,
                         default=str))
    return 0


def cmd_export(args) -> int:
    from investment_tool import us_cli

    conn = connect()
    _cfg(conn)
    path = us_cli.run_export(conn, args.candidate)
    print(f"exported: {path}")
    return 0


def cmd_trial(args) -> int:
    import json as json_mod

    from investment_tool import cards as cards_mod
    from investment_tool import config as config_mod2
    from investment_tool import us_trial

    if args.market != "us":
        print("only --market us is implemented in this trial")
        return 1
    conn = connect()
    cfg = _cfg(conn)
    trial_cfg = config_mod2.load("us_trial_v0.4")
    config_mod2.register(conn, trial_cfg,
                         changelog="US trial v0.3: contamination-aware gates,"
                                   " bounded event lookback (H0)")
    summary = us_trial.run_trial(conn, cfg, trial_cfg, args.asof)
    frozen = []
    for c in summary["leads"]:
        row = conn.execute("SELECT * FROM candidate WHERE candidate_id=?",
                           (c["candidate_id"],)).fetchone()
        content = cards_mod.render_us_card_zh(conn, row)
        frozen.append(cards_mod.freeze_card(conn, row, content))
    summary["frozen_cards"] = [{"artifact_id": f["artifact_id"], "sha256": f["sha256"][:12]}
                               for f in frozen]
    print(json_mod.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_research_queue(args) -> int:
    import json as json_mod

    from investment_tool import config as config_mod2
    from investment_tool import us_queue

    conn = connect()
    if args.process:
        trial_cfg = config_mod2.load("us_trial_v0.4")
        config_mod2.register(conn, trial_cfg,
                             changelog="US trial v0.3: contamination-aware gates,"
                                       " bounded event lookback (H0)")
        audit = us_queue.process_queue(conn, trial_cfg, args.process)
        print(json_mod.dumps(audit, ensure_ascii=False, indent=2, default=str))
        return 0
    rows = us_queue.pending(conn, args.limit)
    counts = conn.execute(
        "SELECT state, COUNT(*) AS n FROM research_queue GROUP BY state").fetchall()
    out = {"states": {r["state"]: r["n"] for r in counts}, "next_by_rank": rows}
    # H0/F16: pending candidates with an empty queue means the queue was
    # never backfilled — warn loudly instead of silently doing nothing
    orphans = conn.execute(
        "SELECT COUNT(*) FROM candidate WHERE state IN"
        " ('US_TRIAL_RESEARCH_PENDING','US_TRIAL_FETCH_FAILED')").fetchone()[0]
    queued = sum(out["states"].values())
    if orphans and not queued:
        out["warning"] = (f"{orphans} pending candidates exist but research_queue"
                          " is empty — run scripts/backfill_research_queue.py")
    print(json_mod.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_us_sync_daily(args) -> int:
    import json as json_mod

    from investment_tool import us_soak

    conn = connect()
    cfg = _cfg(conn)
    if args.list_pending:
        print(json_mod.dumps({"pending": us_soak.pending_sync_dates(conn)}, indent=2))
        return 0
    ledger = us_soak.run_daily(conn, cfg, verify_date=args.verify_idempotency,
                               origin="SCHEDULED" if args.scheduled else "MANUAL")
    print(json_mod.dumps(ledger, ensure_ascii=False, indent=2, default=str))
    return 0 if not ledger["errors"] else 2


def cmd_soak_report(args) -> int:
    import json as json_mod

    from investment_tool import us_soak

    conn = connect()
    report = us_soak.soak_report(conn)
    print(json_mod.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gates"]["all_passed"] else 2


def _research_cfg(conn):
    from investment_tool import config as config_mod2

    cfg = config_mod2.load("us_trial_v0.3")
    config_mod2.register(conn, cfg,
                         changelog="US trial v0.3: contamination-aware gates,"
                                   " bounded event lookback (H0)")
    return cfg


def cmd_research(args) -> int:
    import json as json_mod

    from investment_tool import evidence_gateway, research

    conn = connect()
    cfg = _research_cfg(conn)
    if args.rcmd == "open":
        out = research.open_case(conn, cfg, args.candidate)
    elif args.rcmd == "status":
        rows = [dict(r) for r in conn.execute(
            "SELECT case_id, ticker, state, bundle_version, loop_count,"
            " decision_cutoff_utc FROM research_case ORDER BY opened_at_utc")]
        out = {"cases": rows}
    elif args.rcmd == "freeze-bundle":
        out = research.freeze_bundle(conn, args.case)
    elif args.rcmd == "export":
        out = research.export_role_view(conn, args.case, args.role)
    elif args.rcmd == "fetch":
        out = evidence_gateway.capture(
            conn, cfg, args.case, args.url, published_at_utc=args.published_at,
            title=args.title, source_class=args.source_class, note=args.note)
    elif args.rcmd == "import":
        out = research.import_role_output(
            conn, cfg, args.case, args.role, args.file,
            model_id=args.model_id, provider=args.provider,
            runtime=args.runtime, tokens_in=args.tokens_in,
            tokens_out=args.tokens_out, cost_usd=args.cost_usd)
    elif args.rcmd == "dossier":
        out = research.freeze_dossier(conn, args.case)
    else:  # pragma: no cover
        out = {"error": f"unknown research subcommand {args.rcmd}"}
    print(json_mod.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if "error" not in out and out.get("status") != "REJECTED_IMPORT" else 2


def cmd_status(args) -> int:
    conn = connect()
    for table in ("company", "listing", "manifest", "security_day", "announcement",
                  "event", "candidate", "frozen_artifact"):
        n = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608
        print(f"{table:16s} {n}")
    q = conn.execute(
        "SELECT quality_state, COUNT(*) AS n FROM manifest GROUP BY quality_state"
    ).fetchall()
    if q:
        print("manifest quality:", {r["quality_state"]: r["n"] for r in q})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="invest", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("seed", help="seed the security universe from official identifier sources")
    s.add_argument("--market", choices=["a", "us", "all"], default="all")
    s.set_defaults(func=cmd_seed)

    r = sub.add_parser("resolve", help="resolve a ticker/code to company and listings")
    r.add_argument("ticker")
    r.set_defaults(func=cmd_resolve)

    f = sub.add_parser("fx", help="fetch and store the ECB USD/CNY reference rate")
    f.add_argument("--date", default=None, help="YYYY-MM-DD (default: latest)")
    f.set_defaults(func=cmd_fx)

    ie = sub.add_parser(
        "ingest-eod", help="ingest today's A-share EOD snapshot (Eastmoney PROVISIONAL)"
    )
    ie.add_argument("--date", default=None, help="asof date label (default: today UTC)")
    ie.set_defaults(func=cmd_ingest_eod)

    ib = sub.add_parser("ingest-benchmarks", help="ingest benchmark index history + CN calendar")
    ib.set_defaults(func=cmd_ingest_benchmarks)

    bf = sub.add_parser(
        "backfill", help="backfill daily bars (Tencent qfq / Sina BSE, PROVISIONAL)"
    )
    bf.add_argument("--ticker", default=None)
    bf.add_argument("--industry", default=None, help="restrict to one snapshot industry")
    bf.add_argument("--shard", default=None, help="i/n parallel shard, e.g. 2/4")
    bf.add_argument("--limit", type=int, default=None)
    bf.add_argument("--skip-existing", action="store_true")
    bf.set_defaults(func=cmd_backfill)

    sc = sub.add_parser("scan", help="run the Lane A daily scan for a date (frozen v0 rules)")
    sc.add_argument("--date", required=True, help="scan/trading date YYYY-MM-DD")
    sc.set_defaults(func=cmd_scan)

    cr = sub.add_parser("credentials", help="store a provider token via hidden input (Keychain)")
    cr.add_argument("provider", choices=["tushare", "eodhd", "sec"])
    cr.set_defaults(func=cmd_credentials)

    va = sub.add_parser("validate", help="write forward-validation snapshots (INV-10 ledger)")
    va.add_argument("--asof", default=None, help="YYYY-MM-DD (default today UTC)")
    va.set_defaults(func=cmd_validate)

    dy = sub.add_parser(
        "daily", help="A-share daily glue: benchmarks -> snapshot -> scan -> backup"
    )
    dy.add_argument("--market", choices=["a"], default="a")
    dy.set_defaults(func=cmd_daily)

    um = sub.add_parser("us-map", help="sync SEC CIK/ticker map + reconcile vs universe")
    um.add_argument("--fixture", default=None, help="offline tickers-exchange JSON path")
    um.set_defaults(func=cmd_us_map)

    us = sub.add_parser("us-sync", help="US filing discovery -> normalize -> route (audited)")
    us.add_argument("--date", required=True)
    us.add_argument("--index-fixture", default=None)
    us.add_argument("--efts-fixture", default=None)
    us.add_argument("--submissions-fixture", action="append", default=None)
    us.add_argument("--getcurrent-fixture", default=None)
    us.add_argument("--submissions-cap", type=int, default=40,
                    help="max targeted submissions fetches per live sync")
    us.set_defaults(func=cmd_us_sync)

    ha = sub.add_parser("halts", help="poll Nasdaq trade-halts RSS (observation/event routing)")
    ha.add_argument("--fixture", default=None)
    ha.set_defaults(func=cmd_halts)

    rv = sub.add_parser("review", help="operator review queue (US filings + A-share candidates)")
    rv.set_defaults(func=cmd_review)

    ex = sub.add_parser("export", help="export a candidate bundle (external-agent boundary)")
    ex.add_argument("--candidate", required=True)
    ex.set_defaults(func=cmd_export)

    tr = sub.add_parser("trial", help="run the US Lane A opportunity trial (EXPERIMENTAL)")
    tr.add_argument("--market", choices=["us"], required=True)
    tr.add_argument("--asof", required=True, help="evaluation date YYYY-MM-DD")
    tr.set_defaults(func=cmd_trial)

    rq = sub.add_parser("research-queue",
                        help="list or resume the deferred deep-read queue (PR-A)")
    rq.add_argument("--process", type=int, default=0,
                    help="process the N best-ranked pending items")
    rq.add_argument("--limit", type=int, default=50, help="rows to list")
    rq.set_defaults(func=cmd_research_queue)

    sd = sub.add_parser("us-sync-daily",
                        help="scheduled catch-up: sync pending SEC filing days,"
                             " poll halts, append the soak ledger (ingestion only)")
    sd.add_argument("--list-pending", action="store_true",
                    help="only list pending filing dates, no fetches")
    sd.add_argument("--verify-idempotency", metavar="DATE", default=None,
                    help="also re-sync DATE and prove nothing mutates")
    sd.add_argument("--scheduled", action="store_true",
                    help="mark this run as launchd-scheduled in the soak ledger")
    sd.set_defaults(func=cmd_us_sync_daily)

    sr = sub.add_parser("soak-report",
                        help="aggregate soak ledgers and evaluate the live-gate"
                             " criteria (PR-G)")
    sr.set_defaults(func=cmd_soak_report)

    rs = sub.add_parser("research", help="research-case lifecycle (H1):"
                                         " open/export/fetch/import/dossier")
    rsub = rs.add_subparsers(dest="rcmd", required=True)
    r_open = rsub.add_parser("open", help="open (or reuse) a case for a candidate")
    r_open.add_argument("--candidate", required=True)
    rsub.add_parser("status", help="list research cases")
    r_fb = rsub.add_parser("freeze-bundle", help="freeze the next EvidenceBundle")
    r_fb.add_argument("--case", required=True)
    r_ex = rsub.add_parser("export", help="write a role's working directory")
    r_ex.add_argument("--case", required=True)
    r_ex.add_argument("--role", required=True,
                      choices=["search", "constructive", "adversarial",
                               "rebuttal", "adjudicator"])
    r_f = rsub.add_parser("fetch", help="capture one URL as case evidence"
                                        " (the ONLY road from web to evidence)")
    r_f.add_argument("url")
    r_f.add_argument("--case", required=True)
    r_f.add_argument("--published-at", dest="published_at", default=None,
                     help="source publication time ISO-8601Z when known")
    r_f.add_argument("--title", default=None)
    r_f.add_argument("--source-class", dest="source_class", default=None)
    r_f.add_argument("--note", default=None)
    r_im = rsub.add_parser("import", help="validate + import a role's JSON output")
    r_im.add_argument("--case", required=True)
    r_im.add_argument("--role", required=True,
                      choices=["search", "constructive", "adversarial",
                               "rebuttal", "adjudicator"])
    r_im.add_argument("file")
    r_im.add_argument("--model-id", dest="model_id", required=True)
    r_im.add_argument("--provider", required=True)
    r_im.add_argument("--runtime", required=True)
    r_im.add_argument("--tokens-in", dest="tokens_in", type=int, default=None)
    r_im.add_argument("--tokens-out", dest="tokens_out", type=int, default=None)
    r_im.add_argument("--cost-usd", dest="cost_usd", type=float, default=None)
    r_d = rsub.add_parser("dossier", help="freeze the zh dossier for a final case")
    r_d.add_argument("--case", required=True)
    rs.set_defaults(func=cmd_research)

    st = sub.add_parser("status", help="table counts and manifest quality summary")
    st.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
