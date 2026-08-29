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

    mcap_val = (fundamentals.get("market_cap") or {}).get("value")
    event_mcap_change = None
    if mcap_val and rx.get("mkt_adj_post_ret1") is not None:
        pre = mcap_val / (1 + rx["post_ret1"]) if rx.get("post_ret1") not in (
            None, -1) else mcap_val
        event_mcap_change = {
            "pre_event_mcap_est": pre,
            "abnormal_change_est": pre * rx["mkt_adj_post_ret1"],
            "basis": "pre-event mcap x mkt-adj event-session return",
            "quality": (fundamentals.get("market_cap") or {}).get("quality"),
        }

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
