"""US Lane A opportunity TRIAL: SEC events -> targeted prices -> multi-horizon
market-adjusted reactions -> selective filing content -> deterministic
materiality categories -> candidates / near-misses / explicit rejections.

Experimental throughout (config us_trial_v0); zero candidates is a valid
result; A-share thresholds and frozen conclusions untouched. Lookahead: event
selection and filing content are gated on first_seen_at_utc <= the asof
cutoff; prices end at the asof session.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date as date_cls
from datetime import timedelta

from investment_tool import us_filing_docs, us_prices, us_route
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

RELEVANT_TYPES = ("ISSUER_8K", "DELISTING_NOTICE", "DELISTING", "LATE_FILING",
                  "NON_RELIANCE", "BANKRUPTCY", "TRADING_HALT_NEWS", "TRADING_SUSPENSION")


def _cutoff(asof: str) -> str:
    return f"{asof}T23:59:59Z"


def select_events(conn: sqlite3.Connection, asof: str) -> list[dict]:
    """US events visible by the cutoff, joined to company+listing+filing."""
    rows = conn.execute(
        """
        SELECT e.event_id, e.type, e.published_at_utc, e.first_seen_at_utc,
               ec.company_id, l.listing_id, l.ticker, l.exchange,
               f.accession, f.accepted_at_utc, f.filing_date, f.items_csv,
               f.primary_doc_name, f.cik
        FROM event e
        JOIN event_company ec ON ec.event_id = e.event_id
        JOIN listing l ON l.company_id = ec.company_id
             AND l.exchange IN ('NASDAQ','NYSE','AMEX')
        LEFT JOIN sec_filing f ON f.event_id = e.event_id
        WHERE (e.event_id LIKE 'ev_us_%' OR e.event_id LIKE 'ev_halt_%')
          AND e.first_seen_at_utc <= ?
          AND e.type IN ({})
        ORDER BY e.first_seen_at_utc
        """.format(",".join("?" * len(RELEVANT_TYPES))),
        (_cutoff(asof), *RELEVANT_TYPES),
    ).fetchall()
    out = []
    seen = set()
    for r in rows:
        key = (r["event_id"], r["listing_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
    return out


def _t0_date(ev: dict, asof: str) -> tuple[str | None, str]:
    if ev["accession"]:
        elig = us_route.eligible_session_us(ev["accepted_at_utc"], ev["filing_date"])
        d = elig["eligible_from_date"]
        return d, elig["precision"]
    # halt events: first_seen date (UTC) as DATE-precision anchor
    return (ev["first_seen_at_utc"] or "")[:10] or None, "DATE"


def _window_ret(series: list[tuple], k: int) -> float | None:
    if len(series) < k + 1:
        return None
    return series[-1][1] / series[-1 - k][1] - 1.0


def _bench_window_ret(bench: dict[str, float], dates: list[str], k: int) -> float | None:
    if len(dates) < k + 1:
        return None
    a, b = bench.get(dates[-1 - k]), bench.get(dates[-1])
    return (b / a - 1.0) if a and b else None


def compute_reaction(conn, listing_id: str, t0: str | None, asof: str) -> dict:
    series = us_prices.adj_series(conn, listing_id, asof)
    spy = us_prices.bench_series(conn, "SPY", asof)
    qqq = us_prices.bench_series(conn, "QQQ", asof)
    if not series:
        return {"state": "NO_PRICES"}
    dates = [d for d, _p, _v in series]
    out: dict = {"state": "OK", "sessions": len(series), "last_session": dates[-1]}

    def madj(raw: float | None, k: int) -> float | None:
        if raw is None:
            return None
        b = _bench_window_ret(spy, dates, k)
        return raw - b if b is not None else None

    for k, name in ((1, "ret1"), (5, "ret5"), (21, "ret21"), (63, "ret63")):
        raw = _window_ret(series, k)
        out[name] = raw
        out[f"mkt_adj_{name}"] = madj(raw, k)
        qb = _bench_window_ret(qqq, dates, k)
        out[f"qqq_adj_{name}"] = (raw - qb) if (raw is not None and qb is not None) else None

    # event-anchored reaction
    if t0 is None:
        out["post_state"] = "NO_T0"
        return out
    idx = next((i for i, d in enumerate(dates) if d >= t0), None)
    if idx is None:
        out["post_state"] = "POST_EVENT_PENDING"  # eligibility after asof
        return out
    if idx == 0:
        out["post_state"] = "NO_PRE_EVENT_BASELINE"
        return out
    out["t0_session"] = dates[idx]
    out["post_ret1"] = series[idx][1] / series[idx - 1][1] - 1.0
    out["post_cum"] = series[-1][1] / series[idx - 1][1] - 1.0
    b0, b1 = spy.get(dates[idx - 1]), spy.get(dates[idx])
    bl = spy.get(dates[-1])
    out["mkt_adj_post_ret1"] = (out["post_ret1"] - (b1 / b0 - 1.0)) if (b0 and b1) else None
    out["mkt_adj_post_cum"] = (out["post_cum"] - (bl / b0 - 1.0)) if (b0 and bl) else None
    vols = [v for _d, _p, v in series[max(0, idx - 20):idx] if v]
    v0 = series[idx][2]
    if vols and v0:
        med = sorted(vols)[len(vols) // 2]
        out["volume_ratio"] = v0 / med if med else None
    out["post_state"] = "OK"
    return out


def evaluate_gates(rx: dict, tcfg) -> tuple[str, list[str]]:
    """Returns (TRIGGERED|NEAR_MISS|NO_TRIGGER|INSUFFICIENT_DATA, hits)."""
    if rx.get("state") != "OK":
        return "INSUFFICIENT_DATA", []
    if rx.get("sessions", 0) < int(tcfg.value("data.min_price_sessions")):
        return "INSUFFICIENT_DATA", []
    legs = {
        "post1": (rx.get("mkt_adj_post_ret1"), float(tcfg.value("triggers.mkt_adj_ret1_max"))),
        "cum5": (rx.get("mkt_adj_ret5"), float(tcfg.value("triggers.mkt_adj_ret5_max"))),
        "slow21": (rx.get("mkt_adj_ret21"), float(tcfg.value("triggers.mkt_adj_ret21_max"))),
        "slow63": (rx.get("mkt_adj_ret63"), float(tcfg.value("triggers.mkt_adj_ret63_max"))),
    }
    hits = [name for name, (v, thr) in legs.items() if v is not None and v <= thr]
    vr = rx.get("volume_ratio")
    p1 = rx.get("post_ret1")
    if (vr is not None and p1 is not None
            and vr >= float(tcfg.value("triggers.volume_ratio_min"))
            and p1 <= float(tcfg.value("triggers.volume_ret1_max"))):
        hits.append("volume")
    if hits:
        return "TRIGGERED", hits
    frac = float(tcfg.value("triggers.near_miss_fraction"))
    near = [name for name, (v, thr) in legs.items() if v is not None and v <= thr * frac]
    return ("NEAR_MISS", near) if near else ("NO_TRIGGER", [])


REJECT_RATIONALE = {
    "non_reliance_restatement":
        "非可靠性/重述风险：信息不对称极端，按政策不入围（对应A股立案类政策）",
    "bankruptcy_distress": "破产/持续经营风险：跌幅可能与永久性损伤成比例，不构成过度反应假设",
}
ELIGIBLE_RATIONALE = {
    "management_change": "管理层变动通常为有界成本；若无同时披露的基本面恶化，市场折价可能过度",
    "earnings_guidance": "业绩/指引类下调需区分一次性因素与结构性恶化；反应可能超出经常性影响",
    "acquisition_disposition": "并购/剥离公告的重定价常包含情绪成分，需对照交易条款",
    "financing_dilution": "融资摊薄为可计算的有界稀释；若跌幅显著超过摊薄比例则可能过度",
    "auditor_change": "审计师更换需区分正常轮换与风险信号",
    "regulatory_halt": "监管性停牌事件本身已核实，但根本原因未定，需人工确认",
}


def assess_and_state(ev: dict, rx: dict, gate: str, hits: list[str], content: dict | None,
                     tcfg) -> tuple[str, dict]:
    profile = {"event_id": ev["event_id"], "event_type": ev["type"], "ticker": ev["ticker"],
               "accession": ev["accession"], "accepted_at_utc": ev["accepted_at_utc"],
               "first_seen_at_utc": ev["first_seen_at_utc"], "reaction": rx,
               "gate": gate, "trigger_legs": hits, "content": content,
               "config_version": tcfg.id}
    if gate == "INSUFFICIENT_DATA":
        return "US_TRIAL_INSUFFICIENT_DATA", profile
    if gate == "NO_TRIGGER":
        return "US_TRIAL_REJECTED_NO_TRIGGER", profile
    if gate == "NEAR_MISS":
        return "US_TRIAL_NEAR_MISS", profile
    if content is None:
        return "US_TRIAL_INSUFFICIENT_DATA", profile
    cat = content["primary"]
    if cat in tcfg.value("content.reject_categories"):
        profile["reject_rationale"] = REJECT_RATIONALE.get(cat, cat)
        return f"US_TRIAL_REJECTED_{cat.upper()}", profile
    if cat in tcfg.value("content.review_only_categories"):
        return "US_TRIAL_NEAR_MISS", profile
    if cat in tcfg.value("content.candidate_eligible_categories"):
        profile["excess_rationale"] = ELIGIBLE_RATIONALE.get(cat, cat)
        profile["unresolved_questions"] = [
            "事件的现金流影响是否有界（尚无XBRL基本面数据）",
            "是否存在未披露的关联负面信息",
            "同行业/同因子当期表现的对照是否充分（当前仅SPY/QQQ调整）",
        ]
        return "US_TRIAL_CANDIDATE", profile
    return "US_TRIAL_REJECTED_CONTENT_UNCLASSIFIED", profile


def _upsert_candidate(conn, company_id: str, state: str, profile: dict, cfg_id: str) -> str:
    t0 = profile.get("reaction", {}).get("t0_session") or profile.get("event_id")
    existing = conn.execute(
        "SELECT candidate_id FROM candidate WHERE company_id=? AND lane='A'"
        " AND json_extract(profile_json, '$.event_id')=?",
        (company_id, profile["event_id"]),
    ).fetchone()
    payload = json.dumps(profile, ensure_ascii=False, default=str)
    if existing:
        conn.execute("UPDATE candidate SET state=?, profile_json=?, config_version=?"
                     " WHERE candidate_id=?",
                     (state, payload, cfg_id, existing["candidate_id"]))
        return existing["candidate_id"]
    cid = uuid.uuid5(uuid.NAMESPACE_URL, f"us_trial:{company_id}:{profile['event_id']}").hex
    conn.execute(
        "INSERT OR REPLACE INTO candidate(candidate_id, company_id, lane, state, profile_json,"
        " gates_json, detected_at_utc, config_version) VALUES(?,?,?,?,?,?,?,?)",
        (cid, company_id, "A", state, payload, json.dumps({"t0": t0}), utc_now(), cfg_id),
    )
    return cid


def run_trial(conn: sqlite3.Connection, registry_cfg, trial_cfg, asof: str,
              content_cap: int = 40) -> dict:
    from investment_tool.providers import sec as sec_mod

    summary: dict = {"asof": asof, "generated_at": utc_now(), "config": trial_cfg.id,
                     "counts": {}, "candidates": [], "near_misses": [],
                     "rejections": {}, "degraded": []}
    events = select_events(conn, asof)
    summary["counts"]["events_considered"] = len(events)

    tickers_by_listing = {e["listing_id"]: e["ticker"] for e in events}
    summary["counts"]["companies_linked"] = len(tickers_by_listing)
    start = (date_cls.fromisoformat(asof) - timedelta(days=200)).isoformat()
    end = (date_cls.fromisoformat(asof) + timedelta(days=1)).isoformat()
    coverage = us_prices.ensure_prices(conn, trial_cfg, tickers_by_listing, start, end)
    summary["price_coverage"] = coverage

    http = None
    docs_reviewed = 0
    states: dict[str, int] = {}
    for ev in events:
        t0, _prec = _t0_date(ev, asof)
        rx = compute_reaction(conn, ev["listing_id"], t0, asof)
        gate, hits = evaluate_gates(rx, trial_cfg)
        content = None
        if gate == "TRIGGERED":
            if ev["accession"] is None:
                content = {"primary": "regulatory_halt", "flags": [],
                           "content_version": us_filing_docs.CONTENT_VERSION}
            elif docs_reviewed < content_cap:
                if http is None:
                    http = sec_mod.client()
                if ev["primary_doc_name"] is None:
                    url = sec_mod.SUBMISSIONS_URL.format(cik10=str(ev["cik"]).zfill(10))
                    resp = http.get(url)
                    if resp.status_code == 200:
                        from investment_tool import us_ingest
                        us_ingest.enrich_from_submissions(conn, resp.content)
                        row = conn.execute("SELECT primary_doc_name, accepted_at_utc"
                                           " FROM sec_filing WHERE accession=?",
                                           (ev["accession"],)).fetchone()
                        ev["primary_doc_name"] = row["primary_doc_name"]
                        ev["accepted_at_utc"] = ev["accepted_at_utc"] or row["accepted_at_utc"]
                fetched = us_filing_docs.fetch_primary_document(conn, trial_cfg, http,
                                                               ev["accession"])
                if fetched.get("text_path"):
                    docs_reviewed += 1
                    text = open(fetched["text_path"], encoding="utf-8").read()
                    content = us_filing_docs.assess_content(text, ev["items_csv"])
                else:
                    summary["degraded"].append({"accession": ev["accession"],
                                                "doc": fetched.get("state")})
        state, profile = assess_and_state(ev, rx, gate, hits, content, trial_cfg)
        states[state] = states.get(state, 0) + 1
        cid = _upsert_candidate(conn, ev["company_id"], state, profile, trial_cfg.id)
        if state == "US_TRIAL_CANDIDATE":
            summary["candidates"].append({"candidate_id": cid, "ticker": ev["ticker"],
                                          "event_type": ev["type"],
                                          "category": (content or {}).get("primary"),
                                          "mkt_adj_post_cum": rx.get("mkt_adj_post_cum"),
                                          "legs": hits})
        elif state == "US_TRIAL_NEAR_MISS":
            summary["near_misses"].append({"ticker": ev["ticker"], "legs": hits,
                                           "category": (content or {}).get("primary")})
        else:
            summary["rejections"][state] = summary["rejections"].get(state, 0) + 1
    conn.commit()
    summary["counts"]["filing_documents_reviewed"] = docs_reviewed
    summary["counts"]["states"] = states
    out_dir = DEFAULT_DATA_DIR / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"us_trial_{asof}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return summary
