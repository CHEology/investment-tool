"""QuantPack: the deterministic numbers an Agent may interpret but never
originate (H2). Assembled per research case, frozen as an immutable versioned
artifact, and referenced by NUMERIC claims via quant_ref dot-paths.

Every section carries a quality state; missing components produce explicit
PARTIAL/MISSING states instead of blocking the case (the claim validator
simply has nothing to match, so an agent cannot cite the missing number)."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from investment_tool import us_fundamentals
from investment_tool.lineage import utc_now

# EXPERIMENTAL investability reference thresholds (informational flags until
# a lead-trial config registers them)
MIN_PRICE = 1.0
MIN_MCAP = 100e6
MIN_ADV = 1e6

QUANTPACK_VERSION = "quantpack_v1"

# guidance ranges: "$1.411 billion to $1.421 billion", "$4.66 to $4.73",
# "between $500 million and $525 million"
_GUIDANCE_RX = re.compile(
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)?\s*(?:to|and|[-–])\s*"
    r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million)?", re.IGNORECASE)
_CONTEXT_WORDS = ("guidance", "outlook", "expect", "raise", "lower", "reaffirm",
                  "fiscal", "full year", "full-year")


def _to_number(num: str, unit: str | None) -> float:
    v = float(num.replace(",", ""))
    if unit:
        v *= 1e9 if unit.lower() == "billion" else 1e6
    return v


def extract_guidance(text: str) -> list[dict]:
    """Heuristic guidance-range extraction with byte offsets (locators).
    Deterministic and versioned; quality EXTRACTED_HEURISTIC — an agent must
    quote the underlying passage as a FACTUAL claim, and may reference these
    numbers as quant_refs."""
    out = []
    for m in _GUIDANCE_RX.finditer(text):
        lo = _to_number(m.group(1), m.group(2) or m.group(4))
        hi = _to_number(m.group(3), m.group(4) or m.group(2))
        # context = the current sentence only (". " bounds it; decimals like
        # "$1.411" have no following space so they survive)
        ctx = text[max(0, m.start() - 200):m.start()].split(". ")[-1]
        if not any(w in ctx.lower() for w in _CONTEXT_WORDS):
            continue
        if hi < lo:
            lo, hi = hi, lo
        out.append({"low": lo, "high": hi, "span": [m.start(), m.end()],
                    "context": re.sub(r"\s+", " ", ctx).strip(),
                    "quality": "EXTRACTED_HEURISTIC"})
    return out


def _close_at(conn, listing_id: str, op: str, date: str):
    row = conn.execute(
        f"SELECT trade_date, COALESCE(close, adj_close) AS c FROM security_day"
        f" WHERE listing_id=? AND trade_date {op} ? AND"
        f" COALESCE(close, adj_close) IS NOT NULL ORDER BY trade_date"
        f" {'DESC' if op in ('<', '<=') else 'ASC'} LIMIT 1",
        (listing_id, date)).fetchone()
    return (row["trade_date"], float(row["c"])) if row else (None, None)


def _mcap_timeline(conn, listing, fundamentals, rx, asof, case):
    """Entry-aware, date-explicit market-cap analysis (H2.1/F-B).

    Every comparison names its dates. The event-day calculation uses the
    ACTUAL pre-event close (never the asof mcap divided by the event
    return), and the opportunity is evaluated where the system can act: the
    first actionable entry session. When that session has not traded yet,
    entry fields are PENDING_SESSION and the asof residual stands in,
    explicitly labeled."""
    shares = ((fundamentals.get("market_cap") or {}).get("shares") or {})
    n_sh = shares.get("value")
    t0 = rx.get("t0_session")
    if not (n_sh and t0 and listing):
        return None, {"status": "MISSING_INPUTS"}
    lid = listing["listing_id"]
    pre_d, pre_c = _close_at(conn, lid, "<", t0)
    evt_d, evt_c = _close_at(conn, lid, ">=", t0)
    asof_d, asof_c = _close_at(conn, lid, "<=", asof)
    act = (rx.get("anchors") or {}).get("first_actionable_session")
    ent_d, ent_c = _close_at(conn, lid, ">=", act) if act else (None, None)
    if pre_c is None:
        return None, {"status": "NO_PRE_EVENT_CLOSE"}
    pre_mcap = n_sh * pre_c
    quality = shares.get("quality")
    tl = {
        "shares_used": {"value": n_sh, "quality": quality,
                        "grain": shares.get("grain")},
        "pre_event": {"date": pre_d, "close": pre_c, "mcap": pre_mcap},
        "event_close": {"date": evt_d, "close": evt_c,
                        "mcap": n_sh * evt_c if evt_c else None},
        "asof": {"date": asof_d, "close": asof_c,
                 "mcap": n_sh * asof_c if asof_c else None},
        "event_session_abnormal_change":
            pre_mcap * rx["mkt_adj_post_ret1"]
            if rx.get("mkt_adj_post_ret1") is not None else None,
        "cumulative_abnormal_change_asof":
            pre_mcap * rx["mkt_adj_post_cum"]
            if rx.get("mkt_adj_post_cum") is not None else None,
        "quality": quality,
        # legacy alias kept for claim continuity (now correctly pre-anchored)
        "pre_event_mcap_est": pre_mcap,
        "abnormal_change_est":
            pre_mcap * rx["mkt_adj_post_ret1"]
            if rx.get("mkt_adj_post_ret1") is not None else None,
    }
    if ent_c is not None:
        realized = rx.get("mkt_adj_realized_before_entry")
        entry = {
            "status": "OK", "entry_session": ent_d, "entry_close": ent_c,
            "entry_mcap": n_sh * ent_c,
            "realized_before_entry_change":
                pre_mcap * realized if realized is not None else None,
            "remaining_gap_at_entry":
                pre_mcap * realized if realized is not None else None,
            "basis": "pre-event mcap x mkt-adj return from pre-event close"
                     " to entry close (negative = repricing still standing)",
        }
    else:
        entry = {
            "status": "PENDING_SESSION",
            "first_actionable_session": act,
            "remaining_gap_at_entry": None,
            "provisional_residual_gap_asof":
                tl["cumulative_abnormal_change_asof"],
            "provisional_price_date": asof_d,
            "basis": "entry session has not traded yet; the asof residual"
                     " stands in, explicitly provisional",
        }
    entry["residual_gap_asof"] = tl["cumulative_abnormal_change_asof"]
    entry["quality"] = quality
    return tl, entry


def _peer_section(conn, case_id: str, rx: dict, asof: str) -> dict:
    from investment_tool import peers
    try:
        return peers.peer_analysis(conn, case_id, rx, asof)
    except Exception as exc:   # never blocks the pack; explicit state
        return {"quality": "ERROR", "error": repr(exc)}


def _expectation_state(rx: dict) -> dict:
    """Multi-horizon pre-event context (H3/F-L): one 21-session run-up never
    adequately represents prior expectations — expose the whole path with
    per-field presence, and leave interpretation to the research roles."""
    fields = {f"mkt_adj_run_up_{k}": rx.get(f"mkt_adj_run_up_{k}")
              for k in (5, 21, 63, 126, 252)}
    fields["event_session_mkt_adj"] = rx.get("mkt_adj_post_ret1")
    fields["post_event_continuation_next1"] = rx.get("mkt_adj_next_ret1")
    fields["post_event_continuation_car3"] = rx.get("mkt_adj_post_car3")
    fields["realized_before_entry"] = rx.get("mkt_adj_realized_before_entry")
    missing = [k for k, v in fields.items() if v is None]
    return {**fields,
            "quality": "OK" if not missing else "PARTIAL",
            "missing": missing,
            "note": "price-path expectation proxies; guidance trajectory in"
                    " guidance_extracted; consensus remains external evidence"}


def build_quantpack(conn: sqlite3.Connection, cfg, case_id: str, *,
                    live: bool = False, http_factory=None) -> dict:
    from investment_tool import research

    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    if case is None:
        return {"error": f"no case {case_id}"}
    cand = conn.execute("SELECT * FROM candidate WHERE candidate_id=?",
                        (case["candidate_id"],)).fetchone()
    p = json.loads(cand["profile_json"])
    listing = conn.execute(
        "SELECT l.listing_id, l.ticker, l.is_adr, c.cik FROM listing l"
        " JOIN company c ON c.company_id=l.company_id WHERE l.company_id=?"
        " AND l.exchange IN ('NASDAQ','NYSE','AMEX') ORDER BY l.listing_id"
        " LIMIT 1", (cand["company_id"],)).fetchone()
    rx = p.get("reaction") or {}
    asof = rx.get("last_session") or case["decision_cutoff_utc"][:10]

    if live and listing and listing["cik"]:
        from investment_tool.providers import sec as sec_mod
        http = (http_factory or sec_mod.client)()
        us_fundamentals.fetch_companyfacts(conn, cfg, http, listing["cik"])

    cik = listing["cik"] if listing else None
    fundamentals: dict = {"quality": "MISSING"}
    valuation: dict = {"quality": "MISSING"}
    investability: dict = {"quality": "MISSING"}
    if cik:
        mc = us_fundamentals.market_cap(conn, cik, listing["listing_id"], asof,
                                        is_adr=bool(listing["is_adr"]))
        rev = us_fundamentals.ttm_revenue(conn, cik, asof)
        ni = us_fundamentals.ttm_value(conn, cik, ("NetIncomeLoss",), asof)
        adv = us_fundamentals.adv60(conn, listing["listing_id"], asof)
        fundamentals = {
            "market_cap": mc, "ttm_revenue": rev, "ttm_net_income": ni,
            "net_margin_ttm": (float(ni["value"]) / float(rev["value"])
                               if ni.get("value") and rev.get("value")
                               and float(rev["value"]) != 0 else None),
            "adv60_usd": adv,
        }
        valuation = us_fundamentals.ps_ratio_history(
            conn, cik, listing["listing_id"], asof)
        px = us_fundamentals.price_close(conn, listing["listing_id"], asof)
        flags = {
            "price_ok": px is not None and px >= MIN_PRICE,
            "mcap_ok": bool(mc.get("value")) and mc["value"] >= MIN_MCAP,
            "adv_ok": bool(adv.get("value")) and adv["value"] >= MIN_ADV,
        }
        investability = {"price": px, "thresholds": {
            "min_price": MIN_PRICE, "min_mcap": MIN_MCAP, "min_adv": MIN_ADV,
            "status": "EXPERIMENTAL_REFERENCE"}, **flags,
            "quality": "OK" if mc.get("value") and adv.get("value")
            else "PARTIAL"}

    event_mcap_change, entry_analysis = _mcap_timeline(
        conn, listing, fundamentals, rx, asof, case)

    guidance: dict = {"filings": {}, "evidence": {},
                      "quality": "EXTRACTED_HEURISTIC"}
    for acc, path in (research.build_bundle(conn, case_id)
                      .get("filing_texts") or {}).items():
        found = extract_guidance(Path(path).read_text())
        if found:
            guidance["filings"][acc] = found
    for e in conn.execute("SELECT evidence_id, content_path FROM evidence"
                          " WHERE case_id=? AND content_path IS NOT NULL",
                          (case_id,)):
        if Path(e["content_path"]).exists():
            found = extract_guidance(Path(e["content_path"]).read_text())
            if found:
                guidance["evidence"][e["evidence_id"]] = found

    pack = {
        "quantpack_version": QUANTPACK_VERSION,
        "generated_at": utc_now(),
        "case_id": case_id, "ticker": case["ticker"], "asof": asof,
        "decision_cutoff_utc": case["decision_cutoff_utc"],
        "reaction": {k: v for k, v in rx.items() if k != "anchors"},
        "anchors": rx.get("anchors"),
        "fundamentals": fundamentals,
        "valuation": valuation,
        "investability": investability,
        "event_mcap_change": event_mcap_change,
        "entry_analysis": entry_analysis,
        "peer_analysis": _peer_section(conn, case_id, rx, asof),
        "expectation_state": _expectation_state(rx),
        "guidance_extracted": guidance,
        "damage": {"status": "AGENT_PARAMS_REQUIRED",
                   "templates": ["earnings_decomposition", "market_access",
                                 "dilution"],
                   "note": "agents propose sourced params; the import"
                           " validator recomputes via damage.run_template"},
    }
    version = 1 + conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM frozen_artifact"
        " WHERE candidate_id=? AND kind='QUANT_PACK'",
        (case["candidate_id"],)).fetchone()["v"]
    payload = json.dumps(pack, ensure_ascii=False, indent=2, default=str)
    cdir = research.case_dir(case_id)
    vpath = cdir / f"quantpack_v{version}.json"
    vpath.write_text(payload)
    (cdir / "quantpack_latest.json").write_text(payload)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    conn.execute(
        "INSERT INTO frozen_artifact(artifact_id, kind, candidate_id, version,"
        " frozen_at_utc, content_sha256, path, config_version, status)"
        " VALUES(?,?,?,?,?,?,?,?, 'VALID')",
        (f"qp_{case_id}_v{version}", "QUANT_PACK", case["candidate_id"],
         version, utc_now(), sha, str(vpath), case["config_version"] or ""))
    conn.commit()
    return {"quantpack_version": version, "sha256": sha, "path": str(vpath),
            "sections": {k: (v.get("quality") if isinstance(v, dict) else "n/a")
                         for k, v in pack.items()
                         if k in ("fundamentals", "valuation", "investability",
                                  "guidance_extracted")}}
