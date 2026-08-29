"""Research-case lifecycle: bundle freeze, role views, claim-validated
import, and dossier freeze (H1).

The strictness lives HERE, not in the search: agents explore the open web
freely (via the evidence gateway), but nothing becomes a final conclusion
unless every material claim survives mechanical validation —

- FACTUAL: cited source captured in this case (or a filing already in the
  spine), the quoted passage actually present in the stored text, and the
  source publication time on the right side of the case's decision cutoff
  (DECISION vs HINDSIGHT is computed, never asserted);
- NUMERIC: the value matches the QuantPack reference, or is recomputed
  deterministically (damage templates); agents never originate numbers;
- JUDGMENT: material judgments must anchor to validated claims.

Import is all-or-nothing: a rejected import persists nothing but the
agent_run record and a repair report. Bundles, QuantPacks, and dossiers are
immutable versioned artifacts; new evidence makes a NEW bundle version.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from pathlib import Path

from investment_tool import us_filing_docs
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

ROLES = ("search", "constructive", "adversarial", "rebuttal", "adjudicator")
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
MAX_LOOPS = 2
DECISIONS = ("REJECTED", "UNRESOLVED", "RESEARCH_REQUESTED", "RESEARCH_CANDIDATE")

_ROLE_STATES = {  # states in which each role's import is accepted
    "search": ("OPENED", "EVIDENCE_SEARCH", "RESEARCH_REQUESTED"),
    "constructive": ("BUNDLE_FROZEN", "QUANT_READY"),
    "adversarial": ("UNDER_ADVERSARIAL",),
    "rebuttal": ("REBUTTAL",),
    "adjudicator": ("ADJUDICATION",),
}
_ROLE_NEXT = {"constructive": "UNDER_ADVERSARIAL", "adversarial": "REBUTTAL",
              "rebuttal": "ADJUDICATION"}


def case_dir(case_id: str) -> Path:
    d = DEFAULT_DATA_DIR / "research" / "cases" / case_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _set_state(conn, case_id: str, state: str) -> None:
    conn.execute("UPDATE research_case SET state=?, updated_at_utc=?"
                 " WHERE case_id=?", (state, utc_now(), case_id))


# ------------------------------------------------------------- case open


def open_case(conn: sqlite3.Connection, cfg, candidate_id: str) -> dict:
    cand = conn.execute("SELECT * FROM candidate WHERE candidate_id=?",
                        (candidate_id,)).fetchone()
    if cand is None:
        return {"error": f"no candidate {candidate_id}"}
    existing = conn.execute("SELECT case_id, state FROM research_case"
                            " WHERE candidate_id=?", (candidate_id,)).fetchone()
    if existing:
        return {"case_id": existing["case_id"], "state": existing["state"],
                "note": "existing case reused"}
    p = json.loads(cand["profile_json"])
    anchors = (p.get("reaction") or {}).get("anchors") or {}
    cutoff = _decision_cutoff(anchors, p.get("first_seen_at_utc"))
    case_id = uuid.uuid5(uuid.NAMESPACE_URL, f"case:{candidate_id}").hex[:16]
    conn.execute(
        "INSERT INTO research_case(case_id, candidate_id, company_id, ticker,"
        " state, decision_cutoff_utc, opened_at_utc, updated_at_utc,"
        " config_version) VALUES(?,?,?,?,?,?,?,?,?)",
        (case_id, candidate_id, cand["company_id"], p.get("ticker"), "OPENED",
         cutoff, utc_now(), utc_now(), cfg.id))
    conn.commit()
    return {"case_id": case_id, "state": "OPENED", "decision_cutoff_utc": cutoff,
            "ticker": p.get("ticker")}


def _decision_cutoff(anchors: dict, first_seen: str | None) -> str:
    """The historical decision moment: the OPEN of the first session the
    system could act at. Evidence published later is HINDSIGHT."""
    from investment_tool import calendars_us

    sess = anchors.get("first_actionable_session")
    if sess is None and first_seen:
        sess = calendars_us.decision_anchor(first_seen)["first_actionable_session"]
    if sess is None:
        return first_seen or utc_now()
    c = calendars_us.cal()
    return c.session_open(sess).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------- bundle


def build_bundle(conn: sqlite3.Connection, case_id: str) -> dict:
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    cand = conn.execute("SELECT * FROM candidate WHERE candidate_id=?",
                        (case["candidate_id"],)).fetchone()
    p = json.loads(cand["profile_json"])
    company = conn.execute(
        "SELECT c.name_en, c.cik, l.ticker, l.exchange, l.listing_id FROM company c"
        " JOIN listing l ON l.company_id=c.company_id WHERE c.company_id=?"
        " AND l.exchange IN ('NASDAQ','NYSE','AMEX') ORDER BY l.listing_id LIMIT 1",
        (cand["company_id"],)).fetchone()
    filings = [dict(r) for r in conn.execute(
        "SELECT accession, form, items_csv, filing_date, accepted_at_utc,"
        " first_seen_at_utc, primary_doc_url FROM sec_filing WHERE event_id=?"
        " OR cik=(SELECT cik FROM company WHERE company_id=?)"
        " ORDER BY accepted_at_utc", (p.get("event_id"), cand["company_id"]))]
    filing_texts = {}
    for f in filings:
        for suffix in ("", "_full"):
            tp = us_filing_docs.text_path(f["accession"] + suffix) if suffix == "" \
                else us_filing_docs.text_path(f["accession"]).with_name(
                    f"{f['accession']}_full.txt")
            if tp.exists():
                filing_texts[f["accession"] + suffix] = str(tp)
    evidence = [dict(r) for r in conn.execute(
        "SELECT evidence_id, source_url, title, publisher_domain, source_class,"
        " published_at_utc, retrieved_at_utc, first_seen_at_utc, content_path,"
        " contradiction_state, access_note FROM evidence WHERE case_id=?"
        " ORDER BY first_seen_at_utc", (case_id,))]
    for e in evidence:
        pub = e.get("published_at_utc")
        e["decision_eligible"] = bool(pub) and pub <= case["decision_cutoff_utc"]
    prices = [
        {"date": r["trade_date"], "adj_close": r["adj_close"],
         "volume": r["volume"]}
        for r in conn.execute(
            "SELECT trade_date, adj_close, volume FROM security_day"
            " WHERE listing_id=? AND adj_close IS NOT NULL ORDER BY trade_date",
            (company["listing_id"],))]
    latest_search = conn.execute(
        "SELECT output_sha256 FROM agent_run WHERE case_id=? AND role='search'"
        " AND status='IMPORTED' ORDER BY ended_at_utc DESC LIMIT 1",
        (case_id,)).fetchone()
    search_report = None
    sr_path = case_dir(case_id) / "search_report_latest.json"
    if latest_search and sr_path.exists():
        search_report = json.loads(sr_path.read_text())
    qp_path = case_dir(case_id) / "quantpack_latest.json"
    quantpack = json.loads(qp_path.read_text()) if qp_path.exists() else None
    return {
        "case": {"case_id": case_id, "candidate_id": case["candidate_id"],
                 "state": case["state"], "ticker": case["ticker"],
                 "decision_cutoff_utc": case["decision_cutoff_utc"],
                 "bundle_version_next": case["bundle_version"] + 1},
        "company": dict(company) if company else None,
        "event": {"event_id": p.get("event_id"), "event_type": p.get("event_type"),
                  "accession": p.get("accession"),
                  "accepted_at_utc": p.get("accepted_at_utc"),
                  "first_seen_at_utc": p.get("first_seen_at_utc"),
                  "episode": p.get("episode")},
        "anchors": (p.get("reaction") or {}).get("anchors"),
        "reaction": p.get("reaction"), "gate": p.get("gate"),
        "trigger_legs": p.get("trigger_legs"),
        "content_classification": p.get("content"),
        "rank": p.get("rank"),
        "filings": filings, "filing_texts": filing_texts,
        "evidence": evidence,
        "coverage": (search_report or {}).get("coverage"),
        "competing_explanations": (search_report or {}).get(
            "competing_explanations"),
        "negative_findings": (search_report or {}).get("negative_findings"),
        "conflicts": [e for e in evidence
                      if e["contradiction_state"] != "UNCONTESTED"],
        "quantpack": quantpack,
        "known_missing": _known_missing(quantpack),
        "unresolved_questions": p.get("unresolved_questions"),
        "prices": prices,
    }


def _known_missing(quantpack: dict | None) -> list[str]:
    if quantpack is None:
        return ["quantpack not built yet"]
    return [f"{k}: {v.get('quality')}" for k, v in quantpack.items()
            if isinstance(v, dict) and v.get("quality")
            and v.get("quality") not in ("OK", "COMPUTED")]


def freeze_bundle(conn: sqlite3.Connection, case_id: str) -> dict:
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    bundle = build_bundle(conn, case_id)
    version = case["bundle_version"] + 1
    bdir = case_dir(case_id) / f"bundle_v{version}"
    bdir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(bundle, ensure_ascii=False, indent=2, default=str)
    (bdir / "bundle.json").write_text(payload)
    sha = hashlib.sha256(payload.encode()).hexdigest()
    bundle_id = f"bnd_{case_id}_v{version}"
    conn.execute(
        "INSERT INTO evidence_bundle(bundle_id, case_id, version, content_sha256,"
        " path, frozen_at_utc, source_count, coverage_json) VALUES(?,?,?,?,?,?,?,?)",
        (bundle_id, case_id, version, sha, str(bdir), utc_now(),
         len(bundle["evidence"]),
         json.dumps(bundle.get("coverage"), ensure_ascii=False)))
    conn.execute(
        "INSERT INTO frozen_artifact(artifact_id, kind, candidate_id, version,"
        " frozen_at_utc, content_sha256, path, config_version, status)"
        " VALUES(?,?,?,?,?,?,?,?, 'VALID')",
        (bundle_id, "BUNDLE", case["candidate_id"], version, utc_now(), sha,
         str(bdir), case["config_version"] or ""))
    conn.execute("UPDATE research_case SET bundle_version=?, state=?,"
                 " updated_at_utc=? WHERE case_id=?",
                 (version, "QUANT_READY" if bundle["quantpack"] else
                  "BUNDLE_FROZEN", utc_now(), case_id))
    conn.commit()
    return {"bundle_id": bundle_id, "version": version, "sha256": sha,
            "path": str(bdir), "sources": len(bundle["evidence"])}


def export_role_view(conn: sqlite3.Connection, case_id: str, role: str) -> dict:
    """Write the role's working directory: the frozen bundle reference, the
    versioned prompt contract, and role-specific inputs. Constructive never
    sees adversarial output; the adjudicator sees claims, not prose."""
    if role not in ROLES:
        return {"error": f"unknown role {role}"}
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    version = case["bundle_version"]
    bdir = case_dir(case_id) / f"bundle_v{version}"
    if version == 0 and role != "search":
        return {"error": "freeze a bundle before exporting analyst views"}
    rdir = case_dir(case_id) / f"role_{role}_v{version}"
    rdir.mkdir(parents=True, exist_ok=True)
    contract = PROMPTS_DIR / f"{role}_v1.md"
    view: dict = {"case_id": case_id, "role": role, "bundle_version": version,
                  "bundle_path": str(bdir / "bundle.json") if version else None,
                  "decision_cutoff_utc": case["decision_cutoff_utc"],
                  "prompt_version": f"{role}_v1"}
    if role == "rebuttal":
        view["challenge"] = _latest_output(case_id, "adversarial")
    if role == "adjudicator":
        view["claims"] = [dict(r) for r in conn.execute(
            "SELECT * FROM claim WHERE case_id=? ORDER BY role, claim_id",
            (case_id,))]
        view["rebuttal"] = _latest_output(case_id, "rebuttal")
        view["validation_reports"] = sorted(
            str(p) for p in case_dir(case_id).glob("validation_*.json"))
    (rdir / "input.json").write_text(
        json.dumps(view, ensure_ascii=False, indent=2, default=str))
    if contract.exists():
        (rdir / "contract.md").write_text(contract.read_text())
    return {"role_dir": str(rdir), "bundle": view["bundle_path"],
            "contract": str(contract)}


def _latest_output(case_id: str, role: str) -> dict | None:
    p = case_dir(case_id) / f"{role}_output_latest.json"
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------- claim validation


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _resolve_source(conn, case, source_id: str) -> dict | None:
    """Sources: 'evd_*' rows captured for this case, or 'filing:<accession>'
    for documents already in the SEC spine (PRIMARY_REGULATORY)."""
    if source_id.startswith("filing:"):
        acc = source_id.split(":", 1)[1]
        base = acc.replace("_full", "")
        f = conn.execute("SELECT accession, accepted_at_utc, filing_date"
                         " FROM sec_filing WHERE accession=?", (base,)).fetchone()
        if f is None:
            return None
        tp = us_filing_docs.text_path(acc)
        if not tp.exists():
            return None
        return {"kind": "filing", "text": tp.read_text(),
                "published_at_utc": f["accepted_at_utc"]
                or (f["filing_date"] + "T00:00:00Z" if f["filing_date"] else None),
                "source_class": "PRIMARY_REGULATORY",
                "contradiction_state": "UNCONTESTED"}
    e = conn.execute("SELECT * FROM evidence WHERE evidence_id=? AND case_id=?",
                     (source_id, case["case_id"])).fetchone()
    if e is None or not e["content_path"] or not Path(e["content_path"]).exists():
        return None
    return {"kind": "evidence", "text": Path(e["content_path"]).read_text(),
            "published_at_utc": e["published_at_utc"],
            "source_class": e["source_class"] or "DISCOVERY_LEAD",
            "contradiction_state": e["contradiction_state"]}


def _temporal_basis(published: str | None, cutoff: str) -> str:
    if not published:
        return "HINDSIGHT"   # undated -> never decision-eligible
    return "DECISION" if published <= cutoff else "HINDSIGHT"


def _resolve_quant_ref(quantpack: dict, ref: str):
    node = quantpack
    for part in ref.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        elif isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _num_close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.01 * abs(b), 0.002)


def validate_claims(conn, case, claims: list[dict],
                    quantpack: dict | None) -> tuple[list[dict], list[str]]:
    """Mechanical validation. Returns (annotated_claims, problems). A problem
    on a MATERIAL claim rejects the whole import."""
    problems: list[str] = []
    ids = {c.get("id") for c in claims}
    prior_ids = {r["claim_id"] for r in conn.execute(
        "SELECT claim_id FROM claim WHERE case_id=?", (case["case_id"],))}
    # stored ids are '<case>:<role>:<doc_id>' — accept the short doc id too
    prior_short = {rid.split(":")[-1] for rid in prior_ids}
    for c in claims:
        cid = c.get("id") or "?"
        ctype = c.get("type")
        material = bool(c.get("material"))
        text = c.get("text") or ""
        if ctype not in ("FACTUAL", "NUMERIC", "JUDGMENT"):
            problems.append(f"{cid}: unknown claim type {ctype!r}")
            continue
        if ctype == "FACTUAL":
            src = c.get("source_id")
            quote = c.get("quote") or ""
            if not src or len(quote) < 10:
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = "missing source_id or quote too short"
                if material:
                    problems.append(f"{cid}: material factual claim without"
                                    " captured source+quote")
                continue
            resolved = _resolve_source(conn, case, src)
            if resolved is None:
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = f"source {src} not captured for this case"
                if material:
                    problems.append(f"{cid}: cites uncaptured source {src} —"
                                    " capture it via evidence-fetch first")
                continue
            if _norm(quote) not in _norm(resolved["text"]):
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = "quote not found in stored source text"
                if material:
                    problems.append(f"{cid}: quote not present in {src}")
                continue
            basis = _temporal_basis(resolved["published_at_utc"],
                                    case["decision_cutoff_utc"])
            c["temporal_basis"] = basis
            if material and c.get("temporal_use", "DECISION") == "DECISION" \
                    and basis != "DECISION":
                problems.append(
                    f"{cid}: decision-bearing claim rests on a source published"
                    f" after the decision cutoff ({case['decision_cutoff_utc']})"
                    " or undated — mark temporal_use=HINDSIGHT or find an"
                    " in-time source")
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = "temporal violation"
                continue
            c["verification"] = ("CONFLICTED"
                                 if resolved["contradiction_state"] != "UNCONTESTED"
                                 else "SUPPORTED")
        elif ctype == "NUMERIC":
            val = c.get("value")
            if val is None:
                problems.append(f"{cid}: numeric claim without value")
                continue
            if c.get("quant_ref"):
                if quantpack is None:
                    problems.append(f"{cid}: quant_ref but no quantpack attached")
                    continue
                ref = _resolve_quant_ref(quantpack, c["quant_ref"])
                if ref is None or not isinstance(ref, (int, float)):
                    c["verification"] = "UNSUPPORTED"
                    c["verification_note"] = f"quant_ref {c['quant_ref']} not found"
                    if material:
                        problems.append(f"{cid}: quant_ref unresolved")
                    continue
                if _num_close(float(val), float(ref)):
                    c["verification"] = "RECOMPUTED_OK"
                else:
                    c["verification"] = "RECOMPUTE_MISMATCH"
                    c["verification_note"] = f"claimed {val} vs quantpack {ref}"
                    if material:
                        problems.append(f"{cid}: value {val} != quantpack {ref}"
                                        f" at {c['quant_ref']}")
            elif c.get("recompute", {}).get("kind") == "damage_template":
                from investment_tool import damage
                spec = c["recompute"]
                try:
                    bracket = damage.run_template(spec["template"], spec["params"])
                except Exception as exc:
                    problems.append(f"{cid}: damage recompute failed: {exc}")
                    continue
                lo, hi = float(bracket.low), float(bracket.high)
                exp = c.get("expect") or {}
                if (_num_close(float(exp.get("low", lo)), lo)
                        and _num_close(float(exp.get("high", hi)), hi)):
                    c["verification"] = "RECOMPUTED_OK"
                    c["verification_note"] = f"bracket [{lo:.4g}, {hi:.4g}]"
                else:
                    c["verification"] = "RECOMPUTE_MISMATCH"
                    if material:
                        problems.append(f"{cid}: stated bracket != recomputed"
                                        f" [{lo:.4g}, {hi:.4g}]")
            else:
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = "numeric claim needs quant_ref or recompute"
                if material:
                    problems.append(f"{cid}: material numeric claim with neither"
                                    " quant_ref nor recompute spec")
        else:  # JUDGMENT
            support = c.get("support_claim_ids") or []
            known = [s for s in support
                     if s in ids or s in prior_ids or s in prior_short]
            if known:
                c["verification"] = "JUDGMENT_LINKED"
            else:
                c["verification"] = "JUDGMENT_UNANCHORED"
                if material:
                    problems.append(f"{cid}: material judgment must anchor to"
                                    " at least one existing claim id")
        if not text:
            problems.append(f"{cid}: empty claim text")
    return claims, problems


# ---------------------------------------------------------------- import


def _schema_problems(role: str, doc: dict) -> list[str]:
    p = []
    if doc.get("role") != role:
        p.append(f"output role={doc.get('role')!r} != {role}")
    req = {
        "search": ("search_state", "queries", "coverage", "evidence_used"),
        "constructive": ("mechanism", "effect_classification",
                         "thesis_summary_zh", "claims",
                         "falsification_conditions"),
        "adversarial": ("rationality_case", "counter_claims", "risk_register"),
        "rebuttal": ("responses",),
        "adjudicator": ("decision", "confidence", "rationale_zh",
                        "unresolved_questions"),
    }[role]
    for k in req:
        if k not in doc:
            p.append(f"missing required field: {k}")
    if role == "search" and doc.get("search_state") not in ("COMPLETE", "PARTIAL"):
        p.append("search_state must be COMPLETE or PARTIAL")
    if role == "search":
        for ch, st in (doc.get("coverage") or {}).items():
            if st not in ("FOUND", "ABSENT_CONFIRMED", "NOT_FOUND",
                          "NOT_SEARCHED", "BLOCKED", "PARTIAL"):
                p.append(f"coverage[{ch}] invalid status {st!r}")
    if role == "adjudicator" and doc.get("decision") not in DECISIONS:
        p.append(f"decision must be one of {DECISIONS}")
    if role == "constructive" and doc.get("effect_classification") not in (
            "TEMPORARY", "BOUNDED", "STRUCTURAL", "UNKNOWN"):
        p.append("effect_classification must be TEMPORARY|BOUNDED|STRUCTURAL|UNKNOWN")
    return p


def import_role_output(conn: sqlite3.Connection, cfg, case_id: str, role: str,
                       json_path: str, *, model_id: str, provider: str,
                       runtime: str, tokens_in: int | None = None,
                       tokens_out: int | None = None,
                       cost_usd: float | None = None) -> dict:
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    if case is None:
        return {"status": "ERROR", "problems": [f"no case {case_id}"]}
    if role not in ROLES:
        return {"status": "ERROR", "problems": [f"unknown role {role}"]}
    if case["state"] not in _ROLE_STATES[role]:
        return {"status": "ERROR",
                "problems": [f"case state {case['state']} does not accept"
                             f" a {role} import (needs {_ROLE_STATES[role]})"]}
    raw = Path(json_path).read_bytes()
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"status": "REJECTED_IMPORT", "problems": [f"invalid JSON: {exc}"]}
    problems = _schema_problems(role, doc)
    qp_path = case_dir(case_id) / "quantpack_latest.json"
    quantpack = json.loads(qp_path.read_text()) if qp_path.exists() else None

    claim_fields = {"search": None, "constructive": "claims",
                    "adversarial": "counter_claims", "rebuttal": None,
                    "adjudicator": None}[role]
    claims = list(doc.get(claim_fields) or []) if claim_fields else []
    if role == "rebuttal":
        for r in doc.get("responses", []):
            claims.extend(r.get("claims") or [])
    if role == "search":
        for ce in doc.get("competing_explanations") or []:
            claims.extend(ce.get("claims") or [])
        for nf in doc.get("negative_findings") or []:
            if isinstance(nf, dict) and nf.get("type"):
                claims.append(nf)
    if claims:
        claims, claim_problems = validate_claims(conn, case, claims, quantpack)
        problems.extend(claim_problems)

    bundle_sha = conn.execute(
        "SELECT content_sha256 FROM evidence_bundle WHERE case_id=?"
        " ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
    run_id = uuid.uuid4().hex[:16]
    status = "REJECTED_IMPORT" if problems else "IMPORTED"
    conn.execute(
        "INSERT INTO agent_run(run_id, case_id, role, model_id, provider, runtime,"
        " prompt_version, input_sha256, output_sha256, tokens_in, tokens_out,"
        " cost_usd, status, started_at_utc, ended_at_utc, note)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, case_id, role, model_id, provider, runtime, f"{role}_v1",
         bundle_sha["content_sha256"] if bundle_sha else None,
         hashlib.sha256(raw).hexdigest(), tokens_in, tokens_out, cost_usd,
         status, utc_now(), utc_now(),
         f"{len(problems)} problems" if problems else None))
    report = {"status": status, "run_id": run_id, "role": role,
              "problems": problems,
              "claims_validated": len(claims),
              "claim_states": _hist(claims)}
    stamp = utc_now().replace(":", "").replace("-", "")
    (case_dir(case_id) / f"validation_{role}_{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2))
    if problems:
        conn.commit()
        return report

    # persist claims + role output; advance state
    for c in claims:
        conn.execute(
            "INSERT OR REPLACE INTO claim(claim_id, case_id, bundle_version, role,"
            " claim_type, material, text, source_id, locator, quote, quant_ref,"
            " temporal_basis, verification, verification_note)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{case_id}:{role}:{c.get('id')}", case_id, case["bundle_version"],
             role, c.get("type"), 1 if c.get("material") else 0, c.get("text"),
             c.get("source_id"), c.get("locator"), c.get("quote"),
             c.get("quant_ref"), c.get("temporal_basis"),
             c.get("verification", "SUPPORTED"), c.get("verification_note")))
    (case_dir(case_id) / f"{role}_output_latest.json").write_bytes(raw)
    if role == "search":
        (case_dir(case_id) / "search_report_latest.json").write_bytes(raw)
        _set_state(conn, case_id, "EVIDENCE_SEARCH")
    elif role == "adjudicator":
        decision = doc["decision"]
        if decision == "RESEARCH_REQUESTED":
            if case["loop_count"] + 1 > MAX_LOOPS:
                decision = "UNRESOLVED"
                report["note"] = (f"research loop limit {MAX_LOOPS} reached —"
                                  " forced UNRESOLVED")
            else:
                conn.execute("UPDATE research_case SET loop_count=loop_count+1"
                             " WHERE case_id=?", (case_id,))
        _set_state(conn, case_id, decision)
        report["decision"] = decision
    else:
        _set_state(conn, case_id, _ROLE_NEXT[role])
    conn.commit()
    return report


def _hist(claims: list[dict]) -> dict:
    h: dict = {}
    for c in claims:
        v = c.get("verification", "?")
        h[v] = h.get(v, 0) + 1
    return h


# ---------------------------------------------------------------- dossier


def freeze_dossier(conn: sqlite3.Connection, case_id: str) -> dict:
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    if case["state"] not in ("REJECTED", "UNRESOLVED", "RESEARCH_CANDIDATE"):
        return {"error": f"case state {case['state']} is not final"}
    adj = _latest_output(case_id, "adjudicator") or {}
    thesis = _latest_output(case_id, "constructive") or {}
    challenge = _latest_output(case_id, "adversarial") or {}
    claims = [dict(r) for r in conn.execute(
        "SELECT * FROM claim WHERE case_id=? ORDER BY role, claim_id", (case_id,))]
    runs = [dict(r) for r in conn.execute(
        "SELECT role, model_id, provider, runtime, status FROM agent_run"
        " WHERE case_id=? AND status='IMPORTED' ORDER BY started_at_utc",
        (case_id,))]
    providers = {r["provider"] for r in runs if r["role"] in
                 ("constructive", "adversarial", "adjudicator")}
    role_runs = ", ".join(f"{r['role']}={r['model_id']}" for r in runs)
    lines = [
        f"# 研究档案：{case['ticker']}（case {case_id}）",
        "",
        f"- **裁决**: {adj.get('decision', case['state'])}"
        f" · 置信 {adj.get('confidence', '?')}"
        f" · 决策截止 {case['decision_cutoff_utc']}"
        f" · 证据束 v{case['bundle_version']} · 研究环 {case['loop_count']}",
        f"- **独立性**: "
        f"{'REDUCED_INDEPENDENCE（单一提供商 C1）' if len(providers) <= 1 else '多提供商'}"
        f" · 角色运行: {role_runs}",
        "",
        "## 裁决理由",
        adj.get("rationale_zh", "（无）"),
        "",
        "## 建设性论点（仅通过校验的声明）",
        thesis.get("thesis_summary_zh", "（无）"),
        f"- 机制: {thesis.get('mechanism', '?')} · 效应分类: "
        f"{thesis.get('effect_classification', '?')}",
        "",
        "## 对抗性挑战",
        challenge.get("rationality_case", "（无）"),
        "",
        "## 关键声明与来源",
    ]
    for c in claims:
        if not c["material"]:
            continue
        mark = {"SUPPORTED": "✓", "RECOMPUTED_OK": "✓#", "CONFLICTED": "⚠",
                "JUDGMENT_LINKED": "→"}.get(c["verification"], "?")
        src = c["source_id"] or c["quant_ref"] or ""
        lines.append(f"- [{mark}] ({c['role']}/{c['claim_type']}"
                     f"/{c['temporal_basis'] or '-'}) {c['text']}  ⟨{src}⟩")
    lines += [
        "",
        "## 未决问题",
        *[f"- {q}" for q in (adj.get("unresolved_questions") or ["（无）"])],
        "",
        "## 可证伪条件",
        *[f"- {f}" for f in (thesis.get("falsification_conditions") or ["（无）"])],
        "",
        f"*生成 {utc_now()} · 决策相关声明全部限于决策截止前公开的来源"
        "（HINDSIGHT 声明已标注）· 本档案为研究流程产物，不构成任何投资建议，"
        "无任何仓位或买卖指令。*",
    ]
    content = "\n".join(lines)
    version = 1 + conn.execute(
        "SELECT COALESCE(MAX(version),0) AS v FROM frozen_artifact"
        " WHERE candidate_id=? AND kind='DOSSIER'",
        (case["candidate_id"],)).fetchone()["v"]
    sha = hashlib.sha256(content.encode()).hexdigest()
    path = case_dir(case_id) / f"dossier_v{version}.md"
    path.write_text(content)
    artifact_id = f"dossier_{case_id}_v{version}"
    conn.execute(
        "UPDATE frozen_artifact SET status='SUPERSEDED',"
        " status_note=COALESCE(status_note, ?)"
        " WHERE candidate_id=? AND kind='DOSSIER' AND status='VALID'",
        (f"Superseded by {artifact_id}", case["candidate_id"]))
    conn.execute(
        "INSERT INTO frozen_artifact(artifact_id, kind, candidate_id, version,"
        " frozen_at_utc, content_sha256, path, config_version, status)"
        " VALUES(?,?,?,?,?,?,?,?, 'VALID')",
        (artifact_id, "DOSSIER", case["candidate_id"], version, utc_now(), sha,
         str(path), case["config_version"] or ""))
    conn.commit()
    return {"artifact_id": artifact_id, "path": str(path), "sha256": sha,
            "decision": adj.get("decision", case["state"])}
