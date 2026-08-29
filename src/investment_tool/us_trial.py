"""US Lane A opportunity TRIAL: SEC events -> targeted prices -> multi-horizon
market-adjusted reactions -> selective filing content -> deterministic
routing categories -> LEADS / near-misses / research queue / explicit
rejections.

Output naming is deliberate: this layer produces experimental LEADS (price
trigger + keyword routing), never validated opportunities — it has no
expectation state and no damage quantification. Experimental throughout
(config us_trial_v0); zero leads is a valid result; A-share thresholds and
frozen conclusions untouched. Lookahead: event selection and filing content
are gated on first_seen_at_utc <= the asof cutoff; prices end at the asof
session.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date as date_cls
from datetime import timedelta

from investment_tool import us_filing_docs, us_prices
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

RELEVANT_TYPES = ("ISSUER_8K", "DELISTING_NOTICE", "DELISTING", "LATE_FILING",
                  "NON_RELIANCE", "BANKRUPTCY", "TRADING_HALT_NEWS", "TRADING_SUSPENSION")


def _cutoff(asof: str) -> str:
    return f"{asof}T23:59:59Z"


def select_events(conn: sqlite3.Connection, asof: str,
                  lookback_days: int | None = None) -> list[dict]:
    """US events visible by the cutoff, joined to company+listing+filing.
    `lookback_days` bounds re-gating (H0/F17): events first seen more than
    that many calendar days before asof are managed by the research queue
    lifecycle instead of being re-triggered every run. None = unbounded."""
    floor = "0000"
    if lookback_days is not None:
        floor = (date_cls.fromisoformat(asof)
                 - timedelta(days=lookback_days)).isoformat() + "T00:00:00Z"
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
          AND e.first_seen_at_utc >= ?
          AND e.type IN ({})
        ORDER BY e.first_seen_at_utc
        """.format(",".join("?" * len(RELEVANT_TYPES))),
        (_cutoff(asof), floor, *RELEVANT_TYPES),
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


def event_anchors(ev: dict) -> dict:
    """Both clocks for one event (calendars_us). Filing events anchor on SEC
    acceptance time with filing_date as DATE-precision fallback; halt events
    anchor on their published timestamp, falling back to first_seen's date."""
    from investment_tool import calendars_us

    if ev["accession"]:
        return calendars_us.anchors_for_event(
            ev["accepted_at_utc"], ev["filing_date"], ev["first_seen_at_utc"])
    published = ev.get("published_at_utc") or None
    fallback = (ev["first_seen_at_utc"] or "")[:10] or None
    return calendars_us.anchors_for_event(published, fallback, ev["first_seen_at_utc"])


def evaluate_gates(rx: dict, tcfg) -> tuple[str, list[str]]:
    """(TRIGGERED|TRIGGERED_PARTIAL_PRECISION|POSITIVE_MOVE|NEAR_MISS|
    NO_TRIGGER|POST_EVENT_PENDING|INSUFFICIENT_DATA, hits).

    Every leg is EVENT-ANCHORED. Contamination rule (H0/F13): when the event
    window is contaminated — the release was intra-session, so the
    close-to-close event return also contains pre-release trading, or the
    anchor has only DATE precision — the evt1/volume legs are recorded as
    *_contaminated and cannot trigger the lead track alone; a clean leg
    (car5, or next1 = the session after the event session) must corroborate.
    Contaminated legs without corroboration route to
    TRIGGERED_PARTIAL_PRECISION (review track). A positive event-session
    move is not discarded: POSITIVE_MOVE keeps it visible."""
    if rx.get("state") != "OK":
        return "INSUFFICIENT_DATA", []
    if rx.get("sessions", 0) < int(tcfg.value("data.min_price_sessions")):
        return "INSUFFICIENT_DATA", []
    post_state = rx.get("post_state")
    if post_state == "POST_EVENT_PENDING":
        return "POST_EVENT_PENDING", []
    if post_state != "OK":
        return "INSUFFICIENT_DATA", []
    contaminated = bool(rx.get("event_window_contaminated")) or (
        (rx.get("anchors") or {}).get("precision") == "DATE")
    e1 = rx.get("mkt_adj_post_ret1")
    legs = {
        "evt1": (e1, float(tcfg.value("triggers.mkt_adj_event_ret1_max"))),
        "car5": (rx.get("mkt_adj_car5"), float(tcfg.value("triggers.mkt_adj_car5_max"))),
    }
    hits = [name for name, (v, thr) in legs.items() if v is not None and v <= thr]
    vr = rx.get("volume_ratio")
    if (vr is not None and e1 is not None
            and vr >= float(tcfg.value("triggers.volume_ratio_min"))
            and e1 <= float(tcfg.value("triggers.volume_event_ret1_max"))):
        hits.append("volume")
    if contaminated:
        # ANY window containing the contaminated event session is tainted —
        # including car5, whose [t0-1, t0+5] span embeds the event session
        # (H0.1 loophole fix). Clean corroboration comes only from windows
        # measured strictly after the event close: next1 and post_car3.
        hits = [f"{h}_contaminated" if h in ("evt1", "volume", "car5") else h
                for h in hits]
        n1 = rx.get("mkt_adj_next_ret1")
        if (n1 is not None
                and n1 <= float(tcfg.value("triggers.mkt_adj_next_ret1_max"))):
            hits.append("next1")
        p3 = rx.get("mkt_adj_post_car3")
        try:
            p3_thr = float(tcfg.value("triggers.mkt_adj_post_car3_max"))
        except KeyError:
            p3_thr = None  # pre-v0.4 configs: post_car3 leg absent
        if p3 is not None and p3_thr is not None and p3 <= p3_thr:
            hits.append("post_car3")
        clean = [h for h in hits if not h.endswith("_contaminated")]
        if hits and not clean:
            return "TRIGGERED_PARTIAL_PRECISION", hits
        hits = hits if clean else []
    if hits:
        return "TRIGGERED", hits
    if (e1 is not None and e1 >= float(tcfg.value("triggers.positive_move_min"))):
        return "POSITIVE_MOVE", ["evt1_pos"]
    frac = float(tcfg.value("triggers.near_miss_fraction"))
    near = [name for name, (v, thr) in legs.items() if v is not None and v <= thr * frac]
    return ("NEAR_MISS", near) if near else ("NO_TRIGGER", [])


def group_episodes(evaluated: list[dict], window_sessions: int) -> None:
    """Company-level episode consolidation (review F7): events for the same
    company whose event sessions fall within `window_sessions` of the
    episode's first member form ONE episode. The primary is the earliest
    TRIGGERED member (else the earliest member); every other member is
    reported as US_TRIAL_EPISODE_MEMBER referencing the primary — visible,
    never silently dropped, and never a second lead for the same episode.
    Mutates each item: ev['episode'] = {episode_id, primary_event_id,
    member_count, is_primary}."""
    import uuid as uuid_mod

    from investment_tool import calendars_us

    by_company: dict[str, list[dict]] = {}
    for ev in evaluated:
        if ev["rx"].get("t0_session") or ev.get("anchors", {}).get("event_session"):
            by_company.setdefault(ev["company_id"], []).append(ev)
    c = calendars_us.cal()
    for _cid, evs in by_company.items():
        evs.sort(key=lambda e: (e["anchors"].get("event_session") or "9999",
                                e.get("accepted_at_utc") or "", e["event_id"]))
        clusters: list[list[dict]] = []
        for ev in evs:
            sess = ev["anchors"].get("event_session")
            if sess is None:
                continue
            placed = False
            for cl in clusters:
                first = cl[0]["anchors"]["event_session"]
                try:
                    dist = abs(c.sessions_distance(first, sess)) - 1
                except ValueError:
                    dist = 10 ** 6
                if dist <= window_sessions:
                    cl.append(ev)
                    placed = True
                    break
            if not placed:
                clusters.append([ev])
        for cl in clusters:
            if len(cl) == 0:
                continue
            triggered = [e for e in cl if e["gate"] == "TRIGGERED"]
            primary = triggered[0] if triggered else cl[0]
            eid = uuid_mod.uuid5(
                uuid_mod.NAMESPACE_URL,
                f"us_episode:{primary['company_id']}:{cl[0]['anchors']['event_session']}",
            ).hex
            for ev in cl:
                ev["episode"] = {"episode_id": eid,
                                 "primary_event_id": primary["event_id"],
                                 "member_count": len(cl),
                                 "is_primary": ev is primary}


REJECT_RATIONALE = {
    "non_reliance_restatement":
        "非可靠性/重述风险：信息不对称极端，按政策不入围（对应A股立案类政策）",
    "bankruptcy_distress": "破产/持续经营风险：跌幅可能与永久性损伤成比例，不构成过度反应假设",
}
# Routing rationale only: states WHY the category enters the research queue.
# It must never assert direction, boundedness, or over/under-reaction — those
# are research conclusions this layer has no evidence for (review F3).
ROUTING_RATIONALE = {
    "management_change": "管理层变动类：变动的性质（离任/任命/原因）与经济影响需研究层判断",
    "earnings_guidance": "业绩/指引类：意外方向与幅度需对照事前预期与指引差分，本层未接入",
    "acquisition_disposition": "并购/剥离类：交易条款与对价需研究层核对，本层未读取条款",
    "financing_dilution": "融资/摊薄类：摊薄比例可计算但本层未计算，需研究层定量",
    "auditor_change": "审计师更换类：正常轮换与风险信号的区分需研究层判断",
    "regulatory_halt": "监管性停牌：停牌事实已核实，根本原因未定，需人工确认",
}

# how content review ended for a TRIGGERED event
CONTENT_REVIEWED = "REVIEWED"
CONTENT_BUDGET_DEFERRED = "BUDGET_DEFERRED"
CONTENT_FETCH_FAILED = "FETCH_FAILED"


def assess_and_state(ev: dict, rx: dict, gate: str, hits: list[str], content: dict | None,
                     tcfg, content_state: str | None = None) -> tuple[str, dict]:
    profile = {"event_id": ev["event_id"], "event_type": ev["type"], "ticker": ev["ticker"],
               "accession": ev["accession"], "accepted_at_utc": ev["accepted_at_utc"],
               "first_seen_at_utc": ev["first_seen_at_utc"], "reaction": rx,
               "gate": gate, "trigger_legs": hits, "content": content,
               "content_state": content_state, "config_version": tcfg.id}
    if gate == "INSUFFICIENT_DATA":
        return "US_TRIAL_INSUFFICIENT_DATA", profile
    if gate == "POST_EVENT_PENDING":
        return "US_TRIAL_POST_EVENT_PENDING", profile
    if gate == "TRIGGERED_PARTIAL_PRECISION":
        # contaminated legs without clean corroboration: review track only
        return "US_TRIAL_PARTIAL_PRECISION", profile
    if gate == "POSITIVE_MOVE":
        # not excluded from discovery: recorded as an observation for a future
        # positive-surprise lane; the negative-reversal lane cannot admit it
        return "US_TRIAL_OBSERVED_POSITIVE_MOVE", profile
    if gate == "NO_TRIGGER":
        return "US_TRIAL_REJECTED_NO_TRIGGER", profile
    if gate == "NEAR_MISS":
        return "US_TRIAL_NEAR_MISS", profile
    if content is None:
        # A triggered event without content is NOT a data problem unless the
        # data is actually missing; budget exhaustion and fetch failures get
        # their own honest states (review F2).
        if content_state == CONTENT_BUDGET_DEFERRED:
            return "US_TRIAL_RESEARCH_PENDING", profile
        if content_state == CONTENT_FETCH_FAILED:
            return "US_TRIAL_FETCH_FAILED", profile
        return "US_TRIAL_INSUFFICIENT_DATA", profile
    cat = content["primary"]
    if cat in tcfg.value("content.reject_categories"):
        profile["reject_rationale"] = REJECT_RATIONALE.get(cat, cat)
        return f"US_TRIAL_REJECTED_{cat.upper()}", profile
    if cat in tcfg.value("content.review_only_categories"):
        return "US_TRIAL_NEAR_MISS", profile
    if cat in tcfg.value("content.candidate_eligible_categories"):
        profile["routing_rationale"] = ROUTING_RATIONALE.get(cat, cat)
        profile["unresolved_questions"] = [
            "事件的现金流影响是否有界（尚无XBRL基本面数据）",
            "是否存在未披露的关联负面信息",
            "同行业/同因子当期表现的对照是否充分（当前仅SPY/QQQ调整）",
        ]
        return "US_TRIAL_LEAD", profile
    return "US_TRIAL_REJECTED_CONTENT_UNCLASSIFIED", profile


# states that only reflect this run's budget/fetch situation — they must
# never overwrite a substantive assessment from an earlier run (H0/F14)
BUDGET_ARTIFACT_STATES = ("US_TRIAL_RESEARCH_PENDING", "US_TRIAL_FETCH_FAILED")
OVERWRITABLE_BY_BUDGET = BUDGET_ARTIFACT_STATES + (
    "US_TRIAL_INSUFFICIENT_DATA", "US_TRIAL_POST_EVENT_PENDING")


def _upsert_candidate(conn, company_id: str, state: str, profile: dict, cfg_id: str) -> str:
    t0 = profile.get("reaction", {}).get("t0_session") or profile.get("event_id")
    existing = conn.execute(
        "SELECT candidate_id, state FROM candidate WHERE company_id=? AND lane='A'"
        " AND json_extract(profile_json, '$.event_id')=?",
        (company_id, profile["event_id"]),
    ).fetchone()
    payload = json.dumps(profile, ensure_ascii=False, default=str)
    if existing:
        if (state in BUDGET_ARTIFACT_STATES
                and existing["state"] not in OVERWRITABLE_BY_BUDGET):
            return existing["candidate_id"]   # no cross-day state regression
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


def cached_content(conn: sqlite3.Connection, ev: dict) -> dict | None:
    """Reuse an already-fetched primary document (immutable per accession)
    without consuming any new-document budget (H0/F14)."""
    if not ev.get("accession"):
        return None
    row = conn.execute("SELECT 1 FROM sec_filing_document WHERE accession=?",
                       (ev["accession"],)).fetchone()
    tp = us_filing_docs.text_path(ev["accession"])
    if row and tp.exists():
        return us_filing_docs.assess_content(tp.read_text(), ev["items_csv"])
    return None


def review_filing_content(conn: sqlite3.Connection, trial_cfg, http,
                          ev: dict) -> tuple[dict | None, str, str | None]:
    """Fetch and assess the primary document for one filing event.

    Returns (content, content_state, error). Shared by run_trial and the
    resumable research queue so deferred items get the exact same review.
    Enriches primary_doc_name from submissions when the index alone did not
    carry it."""
    from investment_tool.providers import sec as sec_mod

    if ev.get("primary_doc_name") is None:
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
        text = open(fetched["text_path"], encoding="utf-8").read()
        return (us_filing_docs.assess_content(text, ev["items_csv"]),
                CONTENT_REVIEWED, None)
    return None, CONTENT_FETCH_FAILED, str(fetched.get("state"))


def run_trial(conn: sqlite3.Connection, registry_cfg, trial_cfg, asof: str,
              content_cap: int | None = None, http_factory=None) -> dict:
    from investment_tool import ranking, us_queue

    if http_factory is None:
        def http_factory():
            from investment_tool.providers import sec as sec_mod
            return sec_mod.client()
    if content_cap is None:
        content_cap = int(trial_cfg.value("research.content_budget"))
    summary: dict = {"asof": asof, "generated_at": utc_now(), "config": trial_cfg.id,
                     "counts": {}, "leads": [], "near_misses": [],
                     "rejections": {}, "degraded": []}
    try:
        lookback = int(trial_cfg.value("data.event_lookback_days"))
    except KeyError:
        lookback = None  # older configs: unbounded (historical replays)
    events = select_events(conn, asof, lookback_days=lookback)
    summary["counts"]["events_considered"] = len(events)

    tickers_by_listing = {e["listing_id"]: e["ticker"] for e in events}
    summary["counts"]["companies_linked"] = len(tickers_by_listing)
    start = (date_cls.fromisoformat(asof) - timedelta(days=200)).isoformat()
    end = (date_cls.fromisoformat(asof) + timedelta(days=1)).isoformat()
    coverage = us_prices.ensure_prices(conn, trial_cfg, tickers_by_listing, start, end)
    summary["price_coverage"] = coverage

    # Pass 1 — dual anchors, reactions and gates for EVERY event (no budget
    # applied yet), then company-level episode consolidation.
    from investment_tool import reaction as reaction_mod
    evaluated: list[dict] = []
    gates: dict[str, int] = {}
    for ev in events:
        anchors = event_anchors(ev)
        rx = reaction_mod.compute_event_reaction(conn, ev["listing_id"], anchors, asof)
        gate, hits = evaluate_gates(rx, trial_cfg)
        gates[gate] = gates.get(gate, 0) + 1
        evaluated.append({**ev, "anchors": anchors, "rx": rx, "gate": gate,
                          "hits": hits})
    group_episodes(evaluated, int(trial_cfg.value("episode.window_sessions")))

    # Rank ALL triggered episode-primary events, then apply the deep-read
    # budget to the best-ranked filing events (rank-before-budget, review
    # F2). Halt events carry synthetic content and consume no budget;
    # non-primary episode members never take a second read for the same
    # episode (review F7).
    def _is_primary(e: dict) -> bool:
        return e.get("episode", {}).get("is_primary", True)

    triggered = [e for e in evaluated if e["gate"] == "TRIGGERED" and _is_primary(e)]
    ranked = ranking.rank_events(triggered)
    # cached documents are free (immutable per accession, H0/F14): the
    # new-fetch budget applies only to events without a stored document
    for e in ranked:
        e["_cached_content"] = cached_content(conn, e)
    read_set = {id(e) for e in
                [e for e in ranked
                 if e["accession"] is not None and e["_cached_content"] is None
                 ][:content_cap]}
    summary["counts"]["ranked"] = len(ranked)

    # Pass 2 — content review for the read set; queue rows for every
    # triggered event so nothing disappears when the budget runs out.
    http = None
    docs_reviewed = 0
    docs_reused = 0
    states: dict[str, int] = {}
    for ev in evaluated:
        rx, gate, hits = ev["rx"], ev["gate"], ev["hits"]
        if gate == "TRIGGERED" and not _is_primary(ev):
            # visible episode member, never a second lead for the episode
            profile = {"event_id": ev["event_id"], "event_type": ev["type"],
                       "ticker": ev["ticker"], "accession": ev["accession"],
                       "accepted_at_utc": ev["accepted_at_utc"],
                       "first_seen_at_utc": ev["first_seen_at_utc"],
                       "reaction": rx, "gate": gate, "trigger_legs": hits,
                       "episode": ev.get("episode"),
                       "config_version": trial_cfg.id}
            state = "US_TRIAL_EPISODE_MEMBER"
            states[state] = states.get(state, 0) + 1
            cid = _upsert_candidate(conn, ev["company_id"], state, profile,
                                    trial_cfg.id)
            us_queue.enqueue(
                conn, event_id=ev["event_id"], candidate_id=cid,
                company_id=ev["company_id"], listing_id=ev["listing_id"],
                ticker=ev["ticker"], asof=asof, state="SUPERSEDED",
                rank=None, config_version=trial_cfg.id,
                last_error=f"episode member of {ev['episode']['primary_event_id']}")
            summary["rejections"][state] = summary["rejections"].get(state, 0) + 1
            continue
        content = None
        content_state = None
        if gate == "TRIGGERED":
            if ev["accession"] is None:
                content = {"primary": "regulatory_halt", "flags": [],
                           "content_version": us_filing_docs.CONTENT_VERSION}
                content_state = CONTENT_REVIEWED
            elif ev.get("_cached_content") is not None:
                content = ev["_cached_content"]
                content_state = CONTENT_REVIEWED
                docs_reused += 1
            elif id(ev) in read_set:
                if http is None:
                    http = http_factory()
                content, content_state, err = review_filing_content(
                    conn, trial_cfg, http, ev)
                if content_state == CONTENT_REVIEWED:
                    docs_reviewed += 1
                else:
                    summary["degraded"].append({"accession": ev["accession"],
                                                "doc": err})
            else:
                content_state = CONTENT_BUDGET_DEFERRED
        state, profile = assess_and_state(ev, rx, gate, hits, content, trial_cfg,
                                          content_state=content_state)
        if gate == "TRIGGERED":
            profile["rank"] = ev.get("rank")
        if ev.get("episode"):
            profile["episode"] = ev["episode"]
        states[state] = states.get(state, 0) + 1
        cid = _upsert_candidate(conn, ev["company_id"], state, profile, trial_cfg.id)
        if gate == "TRIGGERED":
            queue_state = {
                CONTENT_REVIEWED: "DOC_REVIEW_COMPLETED",
                CONTENT_BUDGET_DEFERRED: "RESEARCH_PENDING",
                CONTENT_FETCH_FAILED: "FETCH_FAILED",
            }.get(content_state, "DATA_UNAVAILABLE")
            us_queue.enqueue(
                conn, event_id=ev["event_id"], candidate_id=cid,
                company_id=ev["company_id"], listing_id=ev["listing_id"],
                ticker=ev["ticker"], asof=asof, state=queue_state,
                rank=ev.get("rank"), config_version=trial_cfg.id)
        if state == "US_TRIAL_LEAD":
            summary["leads"].append({"candidate_id": cid, "ticker": ev["ticker"],
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
    summary["counts"]["filing_documents_reused"] = docs_reused
    summary["counts"]["states"] = states
    # Coverage accounting: every considered event must land in exactly one
    # state; budget exhaustion and fetch failures are visible, never folded
    # into "insufficient data" (review F2/F5).
    rejected_total = sum(n for s, n in states.items() if s.startswith("US_TRIAL_REJECTED_"))
    coverage = {
        "events_considered": len(events),
        "price_eligible": len(events) - gates.get("INSUFFICIENT_DATA", 0),
        "triggered": gates.get("TRIGGERED", 0),
        "ranked": len(ranked),
        "read_set_budget": content_cap,
        "documents_reviewed": docs_reviewed,
        "documents_reused": docs_reused,
        "research_pending_budget_deferred": states.get("US_TRIAL_RESEARCH_PENDING", 0),
        "fetch_failed": states.get("US_TRIAL_FETCH_FAILED", 0),
        "genuine_missing_data": states.get("US_TRIAL_INSUFFICIENT_DATA", 0),
        "post_event_pending": states.get("US_TRIAL_POST_EVENT_PENDING", 0),
        "partial_precision": states.get("US_TRIAL_PARTIAL_PRECISION", 0),
        "positive_moves_observed": states.get("US_TRIAL_OBSERVED_POSITIVE_MOVE", 0),
        "episode_members": states.get("US_TRIAL_EPISODE_MEMBER", 0),
        "near_misses": states.get("US_TRIAL_NEAR_MISS", 0),
        "rejected": rejected_total,
        "leads": states.get("US_TRIAL_LEAD", 0),
    }
    coverage["accounted"] = (coverage["leads"] + coverage["near_misses"]
                             + coverage["rejected"] + coverage["genuine_missing_data"]
                             + coverage["research_pending_budget_deferred"]
                             + coverage["fetch_failed"] + coverage["post_event_pending"]
                             + coverage["partial_precision"]
                             + coverage["positive_moves_observed"]
                             + coverage["episode_members"])
    coverage["reconciled"] = coverage["accounted"] == coverage["events_considered"]
    summary["coverage"] = coverage
    out_dir = DEFAULT_DATA_DIR / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"us_trial_{asof}.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return summary
