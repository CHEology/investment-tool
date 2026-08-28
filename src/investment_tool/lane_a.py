"""Lane A scan orchestration: triggers -> gates -> cause-hunt -> damage
classification -> candidates -> frozen cards -> audit. Frozen v0 rules only;
this module never mutates thresholds.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

import pandas as pd
import yaml

from investment_tool import analytics, annc, damage
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now
from investment_tool.numeric import dec

PARAMS_DIR = DEFAULT_DATA_DIR / "research" / "params"


def _sessions(conn, end_date: str, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT date FROM calendar_day WHERE exchange='SZSE' AND is_trading=1 AND date<=?"
        " ORDER BY date DESC LIMIT ?",
        (end_date, n),
    ).fetchall()
    return sorted(r["date"] for r in rows)


def run_scan(conn: sqlite3.Connection, cfg, scan_date: str, t0_lookback: int = 5) -> dict:
    """Daily Lane A scan for scan_date. Returns the audit dict."""
    sessions = _sessions(conn, scan_date, 170)
    if not sessions or sessions[-1] != scan_date:
        return {"scan_date": scan_date, "error": "NOT_A_TRADING_DAY_OR_NO_CALENDAR"}
    start = sessions[0]

    df = analytics.load_panel(conn, start, scan_date)
    cells = analytics.load_cells(conn)
    panel = analytics.build_ar_panel(
        df, cells,
        min_peers=int(cfg.value("peer_cells.min_peer_count")),
        contaminated_frac=float(cfg.value("peer_cells.limit_contaminated_fraction")),
    )
    car_max = float(cfg.value("lane_a.trigger.peer_adj_car_0_3_max"))
    sig_mult = float(cfg.value("lane_a.trigger.residual_sigma_mult"))
    shadow_frac = float(cfg.value("lane_a.trigger.shadow_fraction"))
    ratio_min = dec(str(cfg.value("lane_a.research_admission.excess_ratio_min")))
    min_hist = int(cfg.value("universe.min_history_days"))
    liq_excl = float(cfg.value("universe.liquidity_adv60_usd.exclude_below.value"))
    liq_warn = float(cfg.value("universe.liquidity_adv60_usd.warn_below.value"))

    benches = {b: analytics.load_benchmark(conn, i)
               for b, i in analytics.BENCH_BY_BUCKET.items()}
    st_map = dict(zip(cells["listing_id"], cells["is_st"], strict=False))
    bucket_map = dict(zip(cells["listing_id"], cells["size_bucket"], strict=False))
    listings = {r["listing_id"]: r for r in conn.execute(
        "SELECT listing_id, ticker, exchange, board, cninfo_org_id, company_id FROM listing"
    ).fetchall()}

    t0_candidates = sessions[-t0_lookback:]
    audit: dict = {
        "scan_date": scan_date, "config_version": cfg.id, "generated_at": utc_now(),
        "universe": len(listings), "scanned": int(panel.ar.shape[1]),
        "triggers": 0, "shadows": 0, "data_insufficient": 0,
        "gate_failures": {}, "candidates": {}, "open_plans": 0, "events_linked": 0,
        "trigger_detail": [],
    }

    def bump(d, k):
        d[k] = d.get(k, 0) + 1

    # Lookahead hygiene: a (possibly backdated) scan may only see trigger
    # observations whose t0 is at or before its own scan date — never
    # observations created by scans of later dates.
    seen_pairs = {
        (r["listing_id"], json.loads(r["payload_json"]).get("t0"))
        for r in conn.execute("SELECT listing_id, payload_json FROM observation"
                              " WHERE kind='price_trigger'")
        if (json.loads(r["payload_json"]).get("t0") or "9999") <= scan_date
    }
    # Episode cooldown: one anchor trigger per listing per 10 sessions — later
    # sessions of the same slide re-assess the anchored episode via rescans
    # instead of spawning near-duplicate candidates (multiple-testing control;
    # tightens, never loosens, admission).
    cooldown_floor = sessions[-10] if len(sessions) >= 10 else sessions[0]
    in_cooldown = {
        r["listing_id"]
        for r in conn.execute("SELECT listing_id, payload_json FROM observation"
                              " WHERE kind='price_trigger'")
        if cooldown_floor <= json.loads(r["payload_json"]).get("t0", "") <= scan_date
    }
    audit["basis_blocked"] = len(df.attrs.get("basis_blocked", []))
    audit["quality"] = {
        "spine": sorted(
            {f"{r['provider']}:{r['ret_basis']}" for r in conn.execute(
                "SELECT DISTINCT provider, ret_basis FROM security_day"
                " WHERE ret_basis IS NOT NULL")}
        ),
        "verification_debt": [
            "PROVISIONAL scan spine (tencent qfq / sina raw / eastmoney snapshot pct)",
            "industry & size cells retro-applied from latest snapshot (PIT forward-correct only)",
            "Lane A event-path trigger (verified negative event without price move) is"
            " configured but unwired until full-market announcement ingestion (S3)",
        ],
    }
    audit["touched_candidates"] = []

    for listing_id in panel.ar.columns:
        best = None
        for t0 in t0_candidates:
            cap = int(cfg.value("lane_a.limit_extension_cap_sessions"))
            w = analytics.car_window(panel, listing_id, t0, k=3, cap_sessions=cap)
            sigma = analytics.residual_sigma(panel, listing_id, t0)
            verdict = analytics.evaluate_trigger(w["car"], sigma, car_max, sig_mult, shadow_frac)
            if verdict in ("TRIGGER", "SHADOW") and (best is None or w["car"] < best[1]["car"]):
                best = (t0, w, sigma, verdict)
            elif verdict == "DATA_INSUFFICIENT" and best is None:
                best = (t0, w, sigma, verdict)
        if best is None:
            continue
        t0, w, sigma, verdict = best
        if verdict == "DATA_INSUFFICIENT":
            audit["data_insufficient"] += 1
            continue
        if verdict == "SHADOW":
            audit["shadows"] += 1
            if (listing_id, t0) not in seen_pairs:
                conn.execute(
                    "INSERT INTO observation(obs_id, kind, listing_id, payload_json,"
                    " first_seen_at_utc, state) VALUES(?,?,?,?,?,?)",
                    (uuid.uuid4().hex, "price_shadow", listing_id,
                     json.dumps({"t0": t0, **w, "sigma": sigma}), utc_now(), "NEW"),
                )
            continue

        if verdict == "TRIGGER" and listing_id in in_cooldown and (listing_id, t0) \
                not in seen_pairs:
            bump(audit.setdefault("cooldown_suppressed", {}), listing_id)
            continue

        # TRIGGER
        audit["triggers"] += 1
        lst = listings[listing_id]
        mm_car = analytics.market_model_car(
            panel, benches.get(bucket_map.get(listing_id, "SMALL"), pd.Series(dtype=float)),
            listing_id, t0, k=3,
        )
        detail = {"listing_id": listing_id, "ticker": lst["ticker"], "t0": t0,
                  "car_peer": w["car"], "car_mm": mm_car, "sigma": sigma,
                  "window_state": w["state"]}
        audit["trigger_detail"].append(detail)
        if (listing_id, t0) not in seen_pairs:
            conn.execute(
                "INSERT INTO observation(obs_id, kind, listing_id, payload_json,"
                " first_seen_at_utc, state) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, "price_trigger", listing_id,
                 json.dumps(detail), utc_now(), "NEW"),
            )

        # ---- hard gates ----
        bars = conn.execute(
            "SELECT COUNT(*) AS n FROM security_day WHERE listing_id=?", (listing_id,)
        ).fetchone()["n"]
        if bars < min_hist:
            bump(audit["gate_failures"], "HISTORY_LT_180D")
            continue
        if int(st_map.get(listing_id, 0) or 0) == 1:
            bump(audit["gate_failures"], "ST_INTEGRITY")
            continue
        liq, adv = analytics.liquidity_class(panel, conn, listing_id, t0, liq_excl, liq_warn)
        if liq == "EXCLUDE":
            bump(audit["gate_failures"], "LIQUIDITY_EXCLUDE")
            continue
        if liq in ("DATA_INSUFFICIENT", "FX_UNAVAILABLE"):
            bump(audit["gate_failures"], f"LIQUIDITY_{liq}")
            continue

        # ---- cause-hunt (candidate-driven CNInfo query) ----
        # Window: 3 sessions before t0 through scan_date + 1 calendar day —
        # A-share announcements published in the evening are stamped the NEXT
        # calendar day, and explanations legitimately arrive after t0.
        # Lookahead safety is governed by first_seen_at_utc, not this window.
        from datetime import date, timedelta

        idx = sessions.index(t0)
        se_start = sessions[max(0, idx - 3)]
        se_end = (date.fromisoformat(scan_date) + timedelta(days=1)).isoformat()
        anns = annc.ingest_for_listing(conn, cfg.id, lst, se_start, se_end)
        if anns is None:
            bump(audit["candidates"], "ATTRIBUTION_FETCH_DEGRADED")
            _write_candidate(conn, cfg, lst, "ATTRIBUTION_FETCH_DEGRADED",
                             {"t0": t0, "note": "CNInfo fetch failed; re-queue next scan"})
            continue
        # Temporal eligibility: an announcement can only explain sessions at or
        # after its first public availability (date precision -> Beijing date).
        eligible = [a for a in anns if a.get("eligible_from") and a["eligible_from"] <= t0]
        hard = [a for a in eligible if a["relevance"] == annc.HARD_NEGATIVE]
        review = [a for a in eligible if a["relevance"] == annc.CONTENT_REVIEW]
        # HARD_NEGATIVE announcements are events regardless of this episode's
        # attribution; CONTENT_REVIEW items become events only if confirmed.
        events = [annc.create_event_from_announcement(conn, lst["company_id"], a)
                  for a in anns if a["relevance"] == annc.HARD_NEGATIVE]
        audit["events_linked"] += len(events)

        cand_state, profile = _assess(conn, cfg, panel, listing_id, lst, t0, w, mm_car, sigma,
                                      liq, adv, anns, hard, review, ratio_min)
        bump(audit["candidates"], cand_state)
        if cand_state == "PENDING_ATTRIBUTION":
            _write_search_plan(conn, lst, t0, detail)
            audit["open_plans"] += 1
        cid = _write_candidate(conn, cfg, lst, cand_state, profile)
        audit["touched_candidates"].append(cid)

    # Multi-horizon shadow telemetry (observation-only; can never admit — the
    # candidate path above is untouched by these thresholds).
    tele = cfg.data.get("telemetry", {})
    car10_thr = float(tele.get("slow_drawdown_car10", {}).get("value", -0.15))
    car20_thr = float(tele.get("slow_drawdown_car20", {}).get("value", -0.20))
    slow = 0
    tail10 = panel.ar.tail(10).sum(min_count=8)
    tail20 = panel.ar.tail(20).sum(min_count=16)
    for lid_ in panel.ar.columns:
        c10 = float(tail10.get(lid_)) if pd.notna(tail10.get(lid_)) else None
        c20 = float(tail20.get(lid_)) if pd.notna(tail20.get(lid_)) else None
        if (c10 is not None and c10 <= car10_thr) or (c20 is not None and c20 <= car20_thr):
            slow += 1
            conn.execute(
                "INSERT INTO observation(obs_id, kind, listing_id, payload_json,"
                " first_seen_at_utc, state) VALUES(?,?,?,?,?,?)",
                (uuid.uuid4().hex, "slow_drawdown_shadow", lid_,
                 json.dumps({"scan_date": scan_date, "car10": c10, "car20": c20,
                             "telemetry_only": True}), utc_now(), "NEW"),
            )
    audit["slow_drawdown_shadows"] = slow

    conn.commit()
    _write_audit(audit)
    return audit


def _assess(conn, cfg, panel, listing_id, lst, t0, w, mm_car, sigma, liq, adv,
            anns, hard, review, ratio_min):
    ineligible_relevant = [
        a for a in anns
        if a["relevance"] in (annc.HARD_NEGATIVE, annc.CONTENT_REVIEW)
        and not (a.get("eligible_from") and a["eligible_from"] <= t0)
    ]
    profile = {
        "t0": t0, "car_peer": w["car"], "car_mm": mm_car, "window_state": w["state"],
        "sigma": sigma, "liquidity": {"class": liq, "adv60_usd": adv},
        "announcements": [
            {"title": a["title"], "type": a["event_type"], "published": a["published_at_utc"],
             "relevance": a["relevance"], "eligible_from": a.get("eligible_from"),
             "eligible_for_t0": bool(a.get("eligible_from") and a["eligible_from"] <= t0)}
            for a in anns
        ],
        "attribution": {
            "hard_negative_eligible": len(hard),
            "content_review_eligible": len(review),
            "relevant_but_temporally_ineligible": len(ineligible_relevant),
        },
        "verification_debt": [
            "PROVISIONAL scan spine (tencent qfq / sina raw / eastmoney snapshot)",
            "cells retro-applied from latest snapshot (PIT forward-correct only)",
        ],
    }
    pfile = PARAMS_DIR / f"{lst['ticker']}_{t0}.yaml"
    params = yaml.safe_load(pfile.read_text()) if pfile.exists() else None
    confirmation = (params or {}).get("attribution_confirmation")

    if not hard and not review:
        return "PENDING_ATTRIBUTION", profile
    if not hard and review and not confirmation:
        # content decides sign/materiality: an operator must confirm before the
        # damage stage (C0: attribution_confirmation block in the params file).
        return "AWAITING_CONTENT_REVIEW", profile
    if confirmation:
        profile["attribution"]["confirmation"] = confirmation
    if params is None:
        return "AWAITING_PARAMS", profile
    bracket = damage.run_template(params["template"], params["params"])

    # dMcap_abnormal = mcap(t0-1) x |peer-adjusted CAR|. mcap(t0-1) is scaled
    # from the latest snapshot via SAME-BASIS adjusted closes (never raw-vs-
    # adjusted mixing); shares implicitly constant (PROVISIONAL, on the card).
    snap = conn.execute(
        "SELECT total_mcap, asof_date FROM market_snapshot WHERE listing_id=?"
        " ORDER BY asof_date DESC LIMIT 1", (listing_id,),
    ).fetchone()
    adj_now = conn.execute(
        "SELECT adj_close FROM security_day WHERE listing_id=? AND adj_close IS NOT NULL"
        " ORDER BY trade_date DESC LIMIT 1", (listing_id,),
    ).fetchone()
    adj_t0m1 = conn.execute(
        "SELECT adj_close FROM security_day WHERE listing_id=? AND adj_close IS NOT NULL"
        " AND trade_date<? ORDER BY trade_date DESC LIMIT 1", (listing_id, t0),
    ).fetchone()
    if not (snap and snap["total_mcap"] and adj_now and adj_t0m1):
        profile["damage"] = {"error": "MCAP_DATA_UNAVAILABLE"}
        return "AWAITING_PARAMS", profile
    mcap_t0m1 = dec(snap["total_mcap"]) * dec(adj_t0m1["adj_close"]) / dec(adj_now["adj_close"])
    dmcap = mcap_t0m1 * dec(str(abs(w["car"])))
    result = damage.classify(dmcap, bracket, ratio_min)
    result["mcap_t0_minus_1"] = str(mcap_t0m1)
    profile["damage"] = result
    profile["damage_sources"] = params["params"]
    if result["admitted"]:
        return "PROFILED_EXCESS", profile
    return f"NOT_ADMITTED_{result['classification']}", profile


def _write_search_plan(conn, lst, t0, detail):
    plan = {
        "route": "PRICE_FIRST", "ticker": lst["ticker"], "exchange": lst["exchange"],
        "t0": t0, "trigger": detail,
        "query_packs": {
            "positive": [f"{lst['ticker']} 公告", f"{lst['ticker']} 利空",
                         f"{lst['ticker']} 下跌 原因"],
            "negative_disconfirming": [f"{lst['ticker']} 澄清", f"{lst['ticker']} 辟谣"],
        },
        "source_priority": ["cninfo.com.cn", "sse.com.cn", "szse.cn", "bse.cn", "csrc.gov.cn"],
        "budgets": {"max_queries": 10, "max_fetches": 15, "max_minutes": 20},
        "stop_conditions": ["primary_source_found", "budgets_exhausted", "packs_exhausted"],
        "mode": "C0_HUMAN_RUNNABLE",
    }
    conn.execute(
        "INSERT INTO search_plan(plan_id, route, created_from, company_id, plan_json, status)"
        " VALUES(?,?,?,?,?,?)",
        (uuid.uuid4().hex, "PRICE_FIRST", f"trigger:{lst['ticker']}:{t0}",
         None, json.dumps(plan, ensure_ascii=False), "OPEN"),
    )


def _write_candidate(conn, cfg, lst, state, profile):
    """One working candidate per (company, lane, t0); reruns update state in
    place — history is preserved by frozen card artifacts, not candidate rows."""
    t0 = profile.get("t0")
    existing = conn.execute(
        "SELECT candidate_id FROM candidate WHERE company_id=? AND lane='A'"
        " AND json_extract(profile_json, '$.t0')=?",
        (lst["company_id"], t0),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE candidate SET state=?, profile_json=?, config_version=? WHERE candidate_id=?",
            (state, json.dumps(profile, ensure_ascii=False, default=str), cfg.id,
             existing["candidate_id"]),
        )
        return existing["candidate_id"]
    cand_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO candidate(candidate_id, company_id, lane, state, profile_json, gates_json,"
        " detected_at_utc, config_version) VALUES(?,?,?,?,?,?,?,?)",
        (cand_id, lst["company_id"], "A", state,
         json.dumps(profile, ensure_ascii=False, default=str), json.dumps({}), utc_now(), cfg.id),
    )
    return cand_id


def _write_audit(audit: dict) -> Path:
    out_dir = DEFAULT_DATA_DIR / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"scan_{audit['scan_date']}.json"
    path.write_text(json.dumps(audit, ensure_ascii=False, indent=2, default=str))
    md = [
        f"# 每日扫描审计 {audit['scan_date']}",
        "",
        f"- 全市场证券数: {audit['universe']} · 有数据可扫: {audit['scanned']}",
        f"- 价格触发: {audit['triggers']} · 影子触发(近似未达): {audit['shadows']}"
        f" · 数据不足: {audit['data_insufficient']}",
        f"- 硬性闸门拒绝: {json.dumps(audit['gate_failures'], ensure_ascii=False)}",
        f"- 候选状态分布: {json.dumps(audit['candidates'], ensure_ascii=False)}",
        f"- 待执行搜索计划(C0人工): {audit['open_plans']} · 关联事件: {audit['events_linked']}",
        f"- 数据质量: {audit['quality']['spine']}",
        "- 核验债务:",
    ]
    md += [f"  - {d}" for d in audit["quality"]["verification_debt"]]
    admitted = sum(v for k, v in audit["candidates"].items() if k.startswith("PROFILED"))
    if admitted == 0:
        md.append("")
        md.append(
            "**零机会结果：本日无候选达到研究准入（PROFILED_EXCESS=0）。"
            "以上为完整漏斗与拒绝/待定分布——零输出是有效结果 (INV-6)。**"
        )
    md.append("")
    md.append(f"*配置 {audit['config_version']} · 生成 {audit['generated_at']} · 冻结v0规则*")
    (out_dir / f"scan_{audit['scan_date']}.md").write_text("\n".join(md))
    return path
