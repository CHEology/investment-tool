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

ROLES = ("search", "constructive", "adversarial", "rebuttal", "semantic_review",
         "adjudicator")
PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
PROMPT_VERSIONS = {role: f"{role}_v1" for role in ROLES}
PROMPT_VERSIONS.update({"search": "search_v2", "constructive": "constructive_v2",
                        "adversarial": "adversarial_v2",
                        "adjudicator": "adjudicator_v3"})
MAX_LOOPS = 2
# Opportunity states (H1.1/F-J): research sufficiency and opportunity ranking
# are separate axes. QUALIFIED requires complete coverage and full semantic
# review; CONDITIONAL/BEST_AVAILABLE surface the strongest leads with their
# uncertainty exposed; a BLOCKED channel is a veto only when the adjudicator
# names it indispensable.
DECISIONS = ("REJECTED", "UNRESOLVED", "RESEARCH_REQUESTED",
             "QUALIFIED_CANDIDATE", "CONDITIONAL_CANDIDATE",
             "BEST_AVAILABLE_WATCH")
FINAL_STATES = ("REJECTED", "UNRESOLVED", "QUALIFIED_CANDIDATE",
                "CONDITIONAL_CANDIDATE", "BEST_AVAILABLE_WATCH")

_ROLE_STATES = {  # states in which each role's import is accepted
    "search": ("OPENED", "EVIDENCE_SEARCH", "RESEARCH_REQUESTED"),
    "constructive": ("BUNDLE_FROZEN", "QUANT_READY"),
    "adversarial": ("UNDER_ADVERSARIAL",),
    "rebuttal": ("REBUTTAL",),
    "semantic_review": ("SEMANTIC_REVIEW",),
    "adjudicator": ("ADJUDICATION",),
}
_ROLE_NEXT = {"constructive": "UNDER_ADVERSARIAL", "adversarial": "REBUTTAL",
              "rebuttal": "SEMANTIC_REVIEW", "semantic_review": "ADJUDICATION"}


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
        "SELECT e.evidence_id, e.source_url, e.title, e.publisher_domain,"
        " e.source_class, e.published_at_utc, e.retrieved_at_utc,"
        " e.first_seen_at_utc, e.content_path, e.contradiction_state,"
        " e.access_note FROM evidence e"
        " JOIN case_evidence ce ON ce.evidence_id = e.evidence_id"
        " WHERE ce.case_id=? ORDER BY e.first_seen_at_utc", (case_id,))]
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
    """Freeze an immutable bundle version. The bundle CRYPTOGRAPHICALLY
    covers its evidence (H1.1/F-F): every referenced text is snapshotted
    into the bundle directory and its sha256 recorded in bundle.json, so the
    content hash of bundle.json binds the exact evidence content — mutating
    an original file later is detectable (verify_bundle) and never silently
    changes a frozen bundle."""
    case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    bundle = build_bundle(conn, case_id)
    version = case["bundle_version"] + 1
    bdir = case_dir(case_id) / f"bundle_v{version}"
    (bdir / "evidence").mkdir(parents=True, exist_ok=True)
    (bdir / "filings").mkdir(parents=True, exist_ok=True)
    for e in bundle["evidence"]:
        src = Path(e["content_path"]) if e.get("content_path") else None
        if src and src.exists():
            text = src.read_text()
            e["text_sha256"] = hashlib.sha256(text.encode()).hexdigest()
            snap = bdir / "evidence" / f"{e['evidence_id']}.txt"
            snap.write_text(text)
            e["snapshot_path"] = str(snap.relative_to(bdir))
    snap_filings = {}
    for key, path in (bundle.get("filing_texts") or {}).items():
        fp = Path(path)
        if fp.exists():
            text = fp.read_text()
            sha_f = hashlib.sha256(text.encode()).hexdigest()
            snap = bdir / "filings" / f"{key}.txt"
            snap.write_text(text)
            snap_filings[key] = {"snapshot_path": str(snap.relative_to(bdir)),
                                 "text_sha256": sha_f, "original_path": path}
    bundle["filing_texts"] = snap_filings
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
    conn.execute("UPDATE analysis_pair SET status='SUPERSEDED'"
                 " WHERE case_id=? AND bundle_version<?",
                 (case_id, version))
    conn.commit()
    return {"bundle_id": bundle_id, "version": version, "sha256": sha,
            "path": str(bdir), "sources": len(bundle["evidence"])}


def verify_bundle(conn: sqlite3.Connection, case_id: str,
                  version: int | None = None) -> dict:
    """Recompute every recorded hash of a frozen bundle: bundle.json against
    evidence_bundle.content_sha256, each snapshot against its recorded
    text_sha256, and each ORIGINAL evidence/filing file against the frozen
    hash (mutation detection). Old bundles stay verifiable after later
    versions are added."""
    row = conn.execute(
        "SELECT * FROM evidence_bundle WHERE case_id=? AND (? IS NULL OR"
        " version=?) ORDER BY version DESC LIMIT 1",
        (case_id, version, version)).fetchone()
    if row is None:
        return {"error": "no frozen bundle"}
    bdir = Path(row["path"])
    payload = (bdir / "bundle.json").read_text()
    out = {"bundle_id": row["bundle_id"], "version": row["version"],
           "bundle_json_ok":
               hashlib.sha256(payload.encode()).hexdigest() == row["content_sha256"],
           "snapshots_ok": True, "originals_mutated": []}
    bundle = json.loads(payload)
    for e in bundle.get("evidence", []):
        want = e.get("text_sha256")
        snap = e.get("snapshot_path")
        if not (want and snap):
            continue
        got = hashlib.sha256((bdir / snap).read_text().encode()).hexdigest()
        if got != want:
            out["snapshots_ok"] = False
        orig = e.get("content_path")
        if orig and Path(orig).exists():
            got_o = hashlib.sha256(Path(orig).read_text().encode()).hexdigest()
            if got_o != want:
                out["originals_mutated"].append(e["evidence_id"])
    for key, f in (bundle.get("filing_texts") or {}).items():
        want, snap = f.get("text_sha256"), f.get("snapshot_path")
        if want and snap:
            got = hashlib.sha256((bdir / snap).read_text().encode()).hexdigest()
            if got != want:
                out["snapshots_ok"] = False
        orig = f.get("original_path")
        if want and orig and Path(orig).exists():
            if hashlib.sha256(Path(orig).read_text().encode()).hexdigest() != want:
                out["originals_mutated"].append(key)
    out["ok"] = out["bundle_json_ok"] and out["snapshots_ok"]
    return out


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
    prompt_version = PROMPT_VERSIONS[role]
    contract = PROMPTS_DIR / f"{prompt_version}.md"
    view: dict = {"case_id": case_id, "role": role, "bundle_version": version,
                  "bundle_path": str(bdir / "bundle.json") if version else None,
                  "decision_cutoff_utc": case["decision_cutoff_utc"],
                  "prompt_version": prompt_version}
    if role == "rebuttal":
        view["challenge"] = _latest_output(case_id, "adversarial")
    if role == "adjudicator":
        view["claims"] = [dict(r) for r in conn.execute(
            "SELECT * FROM claim WHERE case_id=? AND bundle_version=?"
            " ORDER BY role, claim_id", (case_id, version))]
        view["analyst_verdicts"] = {}
        for analyst_role in ("constructive", "adversarial"):
            analyst_output = _latest_output(case_id, analyst_role) or {}
            view["analyst_verdicts"][analyst_role] = {
                "verdict": analyst_output.get("independent_verdict"),
                "confidence": analyst_output.get("verdict_confidence"),
                "reason_claim_ids": analyst_output.get("verdict_reason_claim_ids") or [],
            }
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
    e = conn.execute(
        "SELECT e.* FROM evidence e JOIN case_evidence ce"
        " ON ce.evidence_id = e.evidence_id"
        " WHERE e.evidence_id=? AND ce.case_id=?",
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


def _eval_abs_ratio(deriv: dict, quantpack: dict | None):
    """Shared derivation evaluator (claims + adjudicator reasons)."""
    num = deriv.get("numerator_value")
    if deriv.get("numerator_quant_ref") and quantpack is not None:
        num = _resolve_quant_ref(quantpack, deriv["numerator_quant_ref"])
    den = deriv.get("denominator_value")
    if deriv.get("denominator_quant_ref") and quantpack is not None:
        den = _resolve_quant_ref(quantpack, deriv["denominator_quant_ref"])
    try:
        return abs(float(num)) / abs(float(den))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _has_comparison_marker(text: str) -> bool:
    t = (text or "").lower()
    # comparison-asserting markers only — generic expectation language
    # ("预期" alone) is inference vocabulary, not a consensus comparison
    return any(w in t for w in ("consensus", "estimate", "beat", "miss",
                                "共识", "超预期", "低于预期", "超出预期",
                                "分析师预期", "市场预期"))


OK_VERIFICATIONS = ("SUPPORTED", "RECOMPUTED_OK", "JUDGMENT_LINKED",
                    "PARTIALLY_SUPPORTED")


def validate_claims(conn, case, claims: list[dict],
                    quantpack: dict | None) -> tuple[list[dict], list[str]]:
    """Mechanical validation with separated axes (H1.1/F-G):
    QUOTE_PRESENT / SOURCE_TEMPORALLY_ELIGIBLE / SOURCE_CLASS / SEMANTIC.
    Quote presence proves capture, not entailment — the semantic axis starts
    as PENDING_REVIEW and is settled by the semantic_review role; the
    adjudicator may only rely on material FACTUAL claims once that axis is
    SEMANTICALLY_SUPPORTED. Deterministic rule: a claim asserting a
    consensus/estimate comparison must quote a passage that itself carries
    the comparison — "revenue increased 8%" cannot support "beat consensus".
    Judgments must anchor to already-validated, non-self, decision-eligible
    claims. A problem on a MATERIAL claim rejects the whole import."""
    problems: list[str] = []
    prior = {}
    for r in conn.execute(
            "SELECT claim_id, verification, temporal_basis, claim_type"
            " FROM claim WHERE case_id=? AND bundle_version=?",
            (case["case_id"], case["bundle_version"])):
        short = r["claim_id"].split(":")[-1]
        prior[r["claim_id"]] = r
        prior.setdefault(short, r)

    judgments = [c for c in claims if c.get("type") == "JUDGMENT"]
    others = [c for c in claims if c.get("type") != "JUDGMENT"]

    for c in others:
        cid = c.get("id") or "?"
        ctype = c.get("type")
        material = bool(c.get("material"))
        text = c.get("text") or ""
        detail: dict = {}
        if ctype not in ("FACTUAL", "NUMERIC"):
            problems.append(f"{cid}: unknown claim type {ctype!r}")
            continue
        if not text:
            problems.append(f"{cid}: empty claim text")
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
            detail["quote_present"] = _norm(quote) in _norm(resolved["text"])
            if not detail["quote_present"]:
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = "quote not found in stored source text"
                c["verification_detail"] = detail
                if material:
                    problems.append(f"{cid}: quote not present in {src}")
                continue
            basis = _temporal_basis(resolved["published_at_utc"],
                                    case["decision_cutoff_utc"])
            c["temporal_basis"] = basis
            detail["temporal"] = basis
            detail["source_class"] = resolved["source_class"]
            if material and c.get("temporal_use", "DECISION") == "DECISION" \
                    and basis != "DECISION":
                problems.append(
                    f"{cid}: decision-bearing claim rests on a source published"
                    f" after the decision cutoff ({case['decision_cutoff_utc']})"
                    " or undated — mark temporal_use=HINDSIGHT or find an"
                    " in-time source")
                c["verification"] = "UNSUPPORTED"
                c["verification_note"] = "temporal violation"
                c["verification_detail"] = detail
                continue
            # deterministic comparison rule
            if _has_comparison_marker(text) and not _has_comparison_marker(quote):
                detail["semantic"] = "PARTIALLY_SUPPORTED"
                c["verification"] = "PARTIALLY_SUPPORTED"
                c["verification_note"] = ("claim asserts a consensus/estimate"
                                          " comparison the quote does not carry")
                if material:
                    problems.append(
                        f"{cid}: comparison claim needs a quote that itself"
                        " establishes the comparison (consensus/estimate"
                        " figure), not just the raw result")
                c["verification_detail"] = detail
                continue
            detail["semantic"] = "PENDING_REVIEW"
            c["verification"] = ("CONFLICTED"
                                 if resolved["contradiction_state"] != "UNCONTESTED"
                                 else "SUPPORTED")
            c["verification_detail"] = detail
        else:  # NUMERIC
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
            elif c.get("derivation", {}).get("op") == "abs_ratio":
                computed = _eval_abs_ratio(c["derivation"], quantpack)
                if computed is None:
                    problems.append(f"{cid}: derivation inputs unresolved")
                    continue
                if _num_close(float(val), computed):
                    c["verification"] = "RECOMPUTED_OK"
                    c["verification_note"] = f"abs_ratio={computed:.4g}"
                else:
                    c["verification"] = "RECOMPUTE_MISMATCH"
                    if material:
                        problems.append(f"{cid}: asserted {val} conflicts with"
                                        f" derived {computed:.4g} (abs_ratio)")
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

    # numeric assertions may not hide inside judgment prose (H1.1/F-H)
    _NUM_IN_JUDGMENT = re.compile(r"\d+(?:\.\d+)?\s*[x×倍]|\d+(?:\.\d+)?%")
    in_doc = {c.get("id"): c for c in others}
    for c in judgments:
        cid = c.get("id") or "?"
        material = bool(c.get("material"))
        if not (c.get("text") or ""):
            problems.append(f"{cid}: empty claim text")
            continue
        if material and _NUM_IN_JUDGMENT.search(c.get("text") or "") \
                and not c.get("derivation") and not c.get("quant_ref"):
            problems.append(
                f"{cid}: material judgment embeds a numeric assertion"
                " (ratio/percent) — move it to a NUMERIC claim with quant_ref"
                " or attach a derivation")
            continue
        support = c.get("support_claim_ids") or []
        resolved_ok, resolved_decision = 0, 0
        bad = []
        for sid in support:
            if sid == cid:
                bad.append(f"{sid} (self-reference)")
                continue
            target = in_doc.get(sid) or prior.get(sid) \
                or prior.get(f"{case['case_id']}:{sid}")
            if target is None:
                continue
            ver = target.get("verification") if isinstance(target, dict) \
                else target["verification"]
            tb = target.get("temporal_basis") if isinstance(target, dict) \
                else target["temporal_basis"]
            ctype_t = target.get("type") if isinstance(target, dict) \
                else target["claim_type"]
            if ver not in OK_VERIFICATIONS:
                bad.append(f"{sid} ({ver})")
                continue
            resolved_ok += 1
            if tb == "DECISION" or ctype_t == "NUMERIC":
                resolved_decision += 1
        if bad and material:
            problems.append(f"{cid}: judgment anchors to invalid supports:"
                            f" {', '.join(bad)}")
            continue
        if resolved_ok == 0:
            c["verification"] = "JUDGMENT_UNANCHORED"
            if material:
                problems.append(f"{cid}: material judgment must anchor to at"
                                " least one VALIDATED existing claim id")
            continue
        if material and c.get("temporal_use", "DECISION") == "DECISION" \
                and resolved_decision == 0:
            problems.append(f"{cid}: decision-bearing judgment anchored only"
                            " to HINDSIGHT claims")
            continue
        c["verification"] = "JUDGMENT_LINKED"
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
        "semantic_review": ("rulings",),
        "adjudicator": ("decision", "confidence", "rationale_zh",
                        "unresolved_questions", "decision_reasons",
                        "opportunity_confidence", "evidence_confidence",
                        "quant_confidence"),
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
    if role == "adjudicator":
        for k in ("opportunity_confidence", "evidence_confidence",
                  "quant_confidence"):
            if doc.get(k) not in ("LOW", "MEDIUM", "HIGH", None):
                p.append(f"{k} must be LOW|MEDIUM|HIGH")
    if role == "semantic_review":
        for r in doc.get("rulings") or []:
            if r.get("ruling") not in ("SEMANTICALLY_SUPPORTED",
                                       "PARTIALLY_SUPPORTED", "UNSUPPORTED",
                                       "CONFLICTED"):
                p.append(f"ruling[{r.get('claim_id')}] invalid: {r.get('ruling')}")
            if not (r.get("explanation") and r.get("passage")):
                p.append(f"ruling[{r.get('claim_id')}] needs explanation+passage")
    if role == "constructive" and doc.get("effect_classification") not in (
            "TEMPORARY", "BOUNDED", "STRUCTURAL", "UNKNOWN"):
        p.append("effect_classification must be TEMPORARY|BOUNDED|STRUCTURAL|UNKNOWN")
    if role in ("constructive", "adversarial"):
        if doc.get("independent_verdict") not in (
                "OPPORTUNITY_SUPPORTED", "MIXED", "MARKET_RATIONAL",
                "INSUFFICIENT"):
            p.append("independent_verdict must be OPPORTUNITY_SUPPORTED|MIXED|"
                     "MARKET_RATIONAL|INSUFFICIENT")
        if doc.get("verdict_confidence") not in ("LOW", "MEDIUM", "HIGH"):
            p.append("verdict_confidence must be LOW|MEDIUM|HIGH")
        verdict_claim_ids = doc.get("verdict_reason_claim_ids")
        if not isinstance(verdict_claim_ids, list):
            p.append("verdict_reason_claim_ids must be a list")
        elif doc.get("independent_verdict") != "INSUFFICIENT" and not verdict_claim_ids:
            p.append("a substantive independent verdict needs verdict_reason_claim_ids")
        own = doc.get("claims" if role == "constructive" else "counter_claims") or []
        own_ids = {c.get("id") for c in own}
        for cid in verdict_claim_ids or []:
            if cid not in own_ids:
                p.append(f"verdict reason {cid!r} is not this Agent's claim")
    return p


def import_role_output(conn: sqlite3.Connection, cfg, case_id: str, role: str,
                       json_path: str, *, model_id: str, provider: str,
                       runtime: str, tokens_in: int | None = None,
                       tokens_out: int | None = None,
                       cost_usd: float | None = None,
                       order_id: str | None = None,
                       pair_id: str | None = None,
                       bundle_version: int | None = None,
                       agent_instance_id: str | None = None,
                       context_id: str | None = None,
                       context_provenance: str | None = None,
                       role_input_sha256: str | None = None,
                       input_manifest_verified: bool = False,
                       visible_roles: list[str] | None = None,
                       output_path: str | None = None,
                       allow_legacy: bool = False) -> dict:
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
    paired_roles = ("constructive", "adversarial", "rebuttal",
                    "semantic_review", "adjudicator")
    if role in paired_roles and not pair_id and not allow_legacy:
        problems.append(f"{role} requires a current work-order analysis pair")
    if bundle_version is not None and bundle_version != case["bundle_version"]:
        problems.append("work order bundle version is stale")
    if pair_id:
        pair = conn.execute("SELECT * FROM analysis_pair WHERE pair_id=?",
                            (pair_id,)).fetchone()
        if pair is None or pair["case_id"] != case_id:
            problems.append("analysis pair does not belong to this case")
        elif (pair["bundle_version"] != case["bundle_version"] or
              pair["status"] == "SUPERSEDED"):
            problems.append("analysis pair does not match the current bundle")
        if not order_id:
            problems.append("paired role import requires order_id")
        if role in ("constructive", "adversarial"):
            if not context_id or context_provenance == "CONTEXT_UNKNOWN":
                problems.append("blind analyst requires a real Agent context ID")
            if not input_manifest_verified:
                problems.append("blind analyst input manifest was not verified")
            if visible_roles:
                problems.append("blind analyst role input exposed another role")
            other = conn.execute(
                "SELECT context_id FROM agent_run WHERE pair_id=? AND role!=?"
                " AND role IN ('constructive','adversarial')"
                " AND status='IMPORTED' ORDER BY ended_at_utc DESC LIMIT 1",
                (pair_id, role)).fetchone()
            if other and other["context_id"] == context_id:
                problems.append("blind analysts must use distinct Agent contexts")
        if role == "adjudicator":
            analyst_runs = conn.execute(
                "SELECT role, context_id, input_manifest_verified FROM agent_run"
                " WHERE pair_id=? AND role IN ('constructive','adversarial')"
                " AND status='IMPORTED'", (pair_id,)).fetchall()
            if pair is None or pair["status"] != "COMPLETE":
                problems.append("adjudication requires a completed analysis pair")
            if ({r["role"] for r in analyst_runs} !=
                    {"constructive", "adversarial"} or
                    len({r["context_id"] for r in analyst_runs
                         if r["context_id"]}) != 2 or
                    not all(r["input_manifest_verified"] for r in analyst_runs)):
                problems.append("adjudication requires two verified blind Agent runs")
    if role == "search":
        evidence_used = doc.get("evidence_used") or []
        for evidence_id in evidence_used:
            exists = conn.execute(
                "SELECT 1 FROM case_evidence WHERE case_id=? AND evidence_id=?",
                (case_id, evidence_id)).fetchone()
            if exists is None:
                problems.append(f"evidence_used contains uncaptured ID {evidence_id}")
        claimed_sources = {
            c.get("source_id") for c in (
                (doc.get("negative_findings") or []) +
                [claim for item in (doc.get("competing_explanations") or [])
                 for claim in (item.get("claims") or [])])
            if c.get("source_id") and not c["source_id"].startswith("filing:")
        }
        missing_used = claimed_sources - set(evidence_used)
        if missing_used:
            problems.append("claim sources missing from evidence_used: "
                            + ", ".join(sorted(missing_used)))
    qp_path = case_dir(case_id) / "quantpack_latest.json"
    quantpack = json.loads(qp_path.read_text()) if qp_path.exists() else None

    claim_fields = {"search": None, "constructive": "claims",
                    "adversarial": "counter_claims", "rebuttal": None,
                    "semantic_review": None, "adjudicator": None}[role]
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

    if role == "semantic_review":
        problems.extend(_apply_semantic_rulings(conn, case, doc,
                                                dry_run=True))
    if role == "adjudicator":
        problems.extend(_validate_decision_reasons(conn, case, doc, quantpack))

    bundle_sha = conn.execute(
        "SELECT content_sha256 FROM evidence_bundle WHERE case_id=?"
        " ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
    run_id = uuid.uuid4().hex[:16]
    status = "REJECTED_IMPORT" if problems else "IMPORTED"
    conn.execute(
        "INSERT INTO agent_run(run_id, case_id, role, model_id, provider, runtime,"
        " prompt_version, input_sha256, output_sha256, tokens_in, tokens_out,"
        " cost_usd, status, started_at_utc, ended_at_utc, note, order_id, pair_id,"
        " bundle_version, agent_instance_id, context_id, context_provenance,"
        " role_input_sha256, input_manifest_verified, visible_roles_json,"
        " output_path)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, case_id, role, model_id, provider, runtime,
         PROMPT_VERSIONS[role],
         role_input_sha256 or (bundle_sha["content_sha256"] if bundle_sha else None),
         hashlib.sha256(raw).hexdigest(), tokens_in, tokens_out, cost_usd,
         status, utc_now(), utc_now(),
         f"{len(problems)} problems" if problems else None,
         order_id, pair_id, bundle_version, agent_instance_id, context_id,
         context_provenance, role_input_sha256,
         1 if input_manifest_verified else 0,
         json.dumps(visible_roles or [], ensure_ascii=False), output_path))
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

    runs_dir = case_dir(case_id) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    immutable_output = runs_dir / f"{run_id}_{role}.json"
    immutable_output.write_bytes(raw)
    conn.execute("UPDATE agent_run SET output_path=? WHERE run_id=?",
                 (str(immutable_output), run_id))

    # persist claims + role output; advance state
    for c in claims:
        conn.execute(
            "INSERT OR REPLACE INTO claim(claim_id, case_id, bundle_version, role,"
            " claim_type, material, text, source_id, locator, quote, quant_ref,"
            " temporal_basis, verification, verification_note,"
            " verification_detail, origin_run_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{case_id}:{role}:{c.get('id')}", case_id,
             case["bundle_version"] + 1 if role == "search"
             else case["bundle_version"],
             role, c.get("type"), 1 if c.get("material") else 0, c.get("text"),
             c.get("source_id"), c.get("locator"), c.get("quote"),
             c.get("quant_ref"), c.get("temporal_basis"),
             c.get("verification", "SUPPORTED"), c.get("verification_note"),
             json.dumps(c.get("verification_detail"), ensure_ascii=False)
             if c.get("verification_detail") else None, run_id))
    (case_dir(case_id) / f"{role}_output_latest.json").write_bytes(raw)
    if role == "semantic_review":
        _apply_semantic_rulings(conn, case, doc, dry_run=False)
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
    if pair_id and role in ("constructive", "adversarial"):
        conn.execute(
            "UPDATE analysis_pair SET first_output_at_utc="
            "COALESCE(first_output_at_utc, ?), status=? WHERE pair_id=?",
            (utc_now(), "COMPLETE" if role == "adversarial" else "OPEN",
             pair_id))
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
    if case["state"] not in FINAL_STATES:
        return {"error": f"case state {case['state']} is not final"}
    adj = _latest_output(case_id, "adjudicator") or {}
    thesis = _latest_output(case_id, "constructive") or {}
    challenge = _latest_output(case_id, "adversarial") or {}
    claims = [dict(r) for r in conn.execute(
        "SELECT * FROM claim WHERE case_id=? AND bundle_version=?"
        " ORDER BY role, claim_id", (case_id, case["bundle_version"]))]
    runs = [dict(r) for r in conn.execute(
        "SELECT role, model_id, provider, runtime, status, pair_id,"
        " agent_instance_id, context_id, context_provenance, visible_roles_json"
        ", input_manifest_verified"
        " FROM agent_run WHERE case_id=? AND status='IMPORTED'"
        " ORDER BY started_at_utc", (case_id,))]
    pair = conn.execute(
        "SELECT * FROM analysis_pair WHERE case_id=? AND bundle_version=?"
        " AND loop_index=? ORDER BY created_at_utc DESC LIMIT 1",
        (case_id, case["bundle_version"], case["loop_count"])).fetchone()
    analyst_runs = [r for r in runs if pair and r["pair_id"] == pair["pair_id"]
                    and r["role"] in ("constructive", "adversarial")]
    by_role = {r["role"]: r for r in analyst_runs}
    pair_complete = set(by_role) == {"constructive", "adversarial"}
    contexts = {r["context_id"] for r in analyst_runs if r["context_id"]}
    agents = {r["agent_instance_id"] for r in analyst_runs
              if r["agent_instance_id"]}
    blind_inputs = all(not json.loads(r["visible_roles_json"] or "[]")
                       and r["input_manifest_verified"] for r in analyst_runs)
    independent = (pair_complete and len(contexts) == 2 and len(agents) == 2
                   and blind_inputs)
    provenances = {r["context_provenance"] for r in analyst_runs}
    context_proof = ("runtime-verified" if pair_complete and provenances <=
                     {"RUNTIME_VERIFIED", "FRESH_PROCESS_VERIFIED"}
                     else "caller-declared" if pair_complete
                     else "legacy/unknown")
    analyst_providers = {r["provider"] for r in analyst_runs}
    analyst_models = {r["model_id"] for r in analyst_runs}
    role_runs = ", ".join(f"{r['role']}={r['model_id']}" for r in runs)
    if pair:
        independence_line = (
            f"两个盲评 Agent：{'已完成' if pair_complete else '未完成'}"
            f" · 逻辑独立性：{'满足' if independent else '未验证'}"
            f" · 上下文凭证：{context_proof}"
            f" · provider/model 数：{len(analyst_providers)}/"
            f"{len(analyst_models)}（仅披露，不作门槛）"
        )
    else:
        independence_line = "LEGACY_CONTEXT_UNVERIFIED（旧运行无法追溯上下文隔离）"
    lines = [
        f"# 研究档案：{case['ticker']}（case {case_id}）",
        "",
        f"- **裁决**: {adj.get('decision', case['state'])}"
        f" · 置信 {adj.get('confidence', '?')}"
        f" · 决策截止 {case['decision_cutoff_utc']}"
        f" · 证据束 v{case['bundle_version']} · 研究环 {case['loop_count']}",
        f"- **独立性**: {independence_line} · 角色运行: {role_runs}",
        "",
        "## 裁决理由",
        adj.get("rationale_zh", "（无）"),
        "",
        "## 建设性论点（仅通过校验的声明）",
        thesis.get("thesis_summary_zh", "（无）"),
        f"- 独立判断: {thesis.get('independent_verdict', '?')}"
        f" · 置信 {thesis.get('verdict_confidence', '?')}",
        f"- 机制: {thesis.get('mechanism', '?')} · 效应分类: "
        f"{thesis.get('effect_classification', '?')}",
        "",
        "## 对抗性挑战",
        challenge.get("rationality_case", "（无）"),
        f"- 独立判断: {challenge.get('independent_verdict', '?')}"
        f" · 置信 {challenge.get('verdict_confidence', '?')}",
        "",
        "## 裁决理由（结构化，全部经校验）",
    ]
    for r in adj.get("decision_reasons") or []:
        w = r.get("weight", "-")
        lines.append(f"- [{r.get('reason_type')}/{w}] {r.get('conclusion')}"
                     + (f"（不确定性：{r['uncertainty']}）"
                        if r.get("uncertainty") else ""))
    lines += ["", "## 关键声明与来源"]
    src_index: dict[str, int] = {}
    src_lines: list[str] = []

    def _src_ref(c) -> str:
        sid = c["source_id"] or c["quant_ref"] or ""
        if not c["source_id"]:
            return f"QuantPack:{c['quant_ref']}" if c["quant_ref"] else ""
        if sid not in src_index:
            src_index[sid] = len(src_index) + 1
            if sid.startswith("filing:"):
                acc = sid.split(":", 1)[1].replace("_full", "")
                f = conn.execute("SELECT primary_doc_url, form, filing_date"
                                 " FROM sec_filing WHERE accession=?",
                                 (acc,)).fetchone()
                url = (f["primary_doc_url"] if f and f["primary_doc_url"] else
                       f"https://www.sec.gov/Archives/edgar/data/-/{acc}")
                src_lines.append(
                    f"[{src_index[sid]}] SEC {f['form'] if f else 'filing'}"
                    f" {acc}（{f['filing_date'] if f else '?'}）"
                    f" · PRIMARY_REGULATORY · {url}")
            else:
                e = conn.execute("SELECT * FROM evidence WHERE evidence_id=?",
                                 (sid,)).fetchone()
                if e:
                    src_lines.append(
                        f"[{src_index[sid]}] {e['title'] or e['source_url']}"
                        f" · {e['publisher_domain']} · 发布 "
                        f"{e['published_at_utc'] or '未知'} ·"
                        f" {e['source_class']} · {e['source_url']}"
                        f" · 内部ID {sid}")
                else:
                    src_lines.append(f"[{src_index[sid]}] {sid}")
        return f"[{src_index[sid]}]"

    for c in claims:
        if not c["material"]:
            continue
        detail = json.loads(c["verification_detail"] or "{}") \
            if "verification_detail" in c.keys() else {}
        mark = {"SUPPORTED": "✓", "RECOMPUTED_OK": "✓#", "CONFLICTED": "⚠",
                "PARTIALLY_SUPPORTED": "≈",
                "JUDGMENT_LINKED": "→"}.get(c["verification"], "?")
        sem = detail.get("semantic")
        semtag = {"SEMANTICALLY_SUPPORTED": "S✓", "PARTIALLY_SUPPORTED": "S≈",
                  "UNSUPPORTED": "S✗", "CONFLICTED": "S⚠"}.get(sem, "")
        loc = f" · 定位:{c['locator']}" if c["locator"] else ""
        lines.append(f"- [{mark}{semtag}] ({c['role']}/{c['claim_type']}"
                     f"/{c['temporal_basis'] or '-'}) {c['text']}"
                     f"  {_src_ref(c)}{loc}")
    if src_lines:
        lines += ["", "## 来源清单（直接链接）"] + [f"- {sl}" for sl in src_lines]
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


# ------------------------------------------- semantic review + reasons (H1.1)


def _lookup_claim(conn, case_id: str, cid: str,
                  bundle_version: int | None = None):
    row = conn.execute("SELECT * FROM claim WHERE case_id=? AND"
                       " (? IS NULL OR bundle_version=?) AND"
                       " (claim_id=? OR claim_id LIKE ?)",
                       (case_id, bundle_version, bundle_version, cid,
                        f"{case_id}:%:{cid}")).fetchone()
    return row


def _apply_semantic_rulings(conn, case, doc: dict, *, dry_run: bool) -> list[str]:
    """Settle the semantic axis for FACTUAL claims. Completeness is enforced:
    every MATERIAL FACTUAL claim of the case must be ruled — an unruled one
    blocks the import, so the adjudicator can never see half-reviewed
    evidence. UNSUPPORTED/CONFLICTED rulings downgrade the claim."""
    problems: list[str] = []
    ruled: dict[str, dict] = {}
    for r in doc.get("rulings") or []:
        cid = r.get("claim_id") or ""
        row = _lookup_claim(conn, case["case_id"], cid, case["bundle_version"])
        if row is None:
            problems.append(f"ruling references unknown claim {cid}")
            continue
        if row["claim_type"] != "FACTUAL":
            problems.append(f"ruling {cid}: semantic review applies to FACTUAL"
                            f" claims (got {row['claim_type']})")
            continue
        ruled[row["claim_id"]] = r
    unruled = [r["claim_id"] for r in conn.execute(
        "SELECT claim_id FROM claim WHERE case_id=? AND claim_type='FACTUAL'"
        " AND material=1 AND bundle_version=?",
        (case["case_id"], case["bundle_version"]))
        if r["claim_id"] not in ruled]
    if unruled:
        problems.append("material FACTUAL claims not ruled: "
                        + ", ".join(sorted(unruled)[:6])
                        + (" …" if len(unruled) > 6 else ""))
    if problems or dry_run:
        return problems
    for claim_id, r in ruled.items():
        row = _lookup_claim(conn, case["case_id"], claim_id,
                            case["bundle_version"])
        detail = json.loads(row["verification_detail"] or "{}")
        detail["semantic"] = r["ruling"]
        detail["semantic_explanation"] = r.get("explanation")
        detail["semantic_passage"] = r.get("passage")
        new_ver = row["verification"]
        if r["ruling"] in ("UNSUPPORTED", "CONFLICTED"):
            new_ver = r["ruling"]
        elif r["ruling"] == "PARTIALLY_SUPPORTED":
            new_ver = "PARTIALLY_SUPPORTED"
        conn.execute("UPDATE claim SET verification=?, verification_detail=?"
                     " WHERE claim_id=?",
                     (new_ver, json.dumps(detail, ensure_ascii=False), claim_id))
    conn.commit()
    return []


def _validate_decision_reasons(conn, case, doc: dict,
                               quantpack: dict | None) -> list[str]:
    """Every factual/numeric assertion the adjudicator relies on must be
    traceable (H1.1/F-H): FACTUAL/JUDGMENT reasons reference validated
    (and, for FACTUAL, semantically supported) claims; NUMERIC reasons match
    a quant_ref or recompute through an explicit derivation — a ratio
    asserted in prose that conflicts with the recomputation rejects the
    import."""
    problems: list[str] = []
    reasons = doc.get("decision_reasons") or []
    if not reasons:
        problems.append("decision_reasons must not be empty")
    for r in reasons:
        rid = r.get("reason_id") or "?"
        rtype = r.get("reason_type")
        if rtype not in ("FACTUAL", "NUMERIC", "COVERAGE", "JUDGMENT"):
            problems.append(f"reason {rid}: invalid reason_type {rtype!r}")
            continue
        if not r.get("conclusion"):
            problems.append(f"reason {rid}: conclusion required")
        if rtype in ("FACTUAL", "JUDGMENT"):
            cids = r.get("claim_ids") or []
            if not cids:
                problems.append(f"reason {rid}: claim_ids required")
            for cid in cids:
                row = _lookup_claim(conn, case["case_id"], cid,
                                    case["bundle_version"])
                if row is None:
                    problems.append(f"reason {rid}: unknown claim {cid}")
                    continue
                if row["verification"] not in OK_VERIFICATIONS:
                    problems.append(f"reason {rid}: claim {cid} is"
                                    f" {row['verification']}")
                if rtype == "FACTUAL" and row["claim_type"] == "FACTUAL":
                    detail = json.loads(row["verification_detail"] or "{}")
                    if detail.get("semantic") not in ("SEMANTICALLY_SUPPORTED",):
                        problems.append(
                            f"reason {rid}: claim {cid} lacks semantic support"
                            f" ({detail.get('semantic')})")
        elif rtype == "NUMERIC":
            val = r.get("value")
            deriv = r.get("derivation")
            if r.get("quant_ref") and quantpack is not None:
                ref = _resolve_quant_ref(quantpack, r["quant_ref"])
                if ref is None or val is None or not _num_close(float(val),
                                                                float(ref)):
                    problems.append(f"reason {rid}: value {val} does not match"
                                    f" quantpack {r.get('quant_ref')} ({ref})")
            elif deriv and deriv.get("op") == "abs_ratio":
                computed = _eval_abs_ratio(deriv, quantpack)
                if computed is None:
                    problems.append(f"reason {rid}: derivation inputs unresolved")
                    continue
                if val is None or not _num_close(float(val), computed):
                    problems.append(
                        f"reason {rid}: asserted ratio {val} conflicts with"
                        f" derived {computed:.4g} (abs_ratio)")
            else:
                problems.append(f"reason {rid}: NUMERIC reason needs quant_ref"
                                " or an abs_ratio derivation")
    decision = doc.get("decision")
    if decision == "QUALIFIED_CANDIDATE":
        if doc.get("indispensable_missing"):
            problems.append("QUALIFIED_CANDIDATE with indispensable_missing"
                            " non-empty — downgrade to CONDITIONAL_CANDIDATE"
                            " or resolve the gap")
        bad = conn.execute(
            "SELECT COUNT(*) FROM claim WHERE case_id=? AND material=1"
            " AND bundle_version=?"
            " AND claim_type='FACTUAL' AND (verification_detail IS NULL OR"
            " json_extract(verification_detail,'$.semantic')"
            " != 'SEMANTICALLY_SUPPORTED')",
            (case["case_id"], case["bundle_version"])).fetchone()[0]
        if bad:
            problems.append(f"QUALIFIED_CANDIDATE with {bad} material factual"
                            " claims lacking full semantic support")
    return problems


def rank_cases(conn: sqlite3.Connection) -> dict:
    """Run-level opportunity output (H1.1/F-J): separate from qualification.
    Returns the qualified list (possibly empty — say so plainly), the ranked
    best-available list with per-axis confidences, and open work."""
    order = {"QUALIFIED_CANDIDATE": 0, "CONDITIONAL_CANDIDATE": 1,
             "BEST_AVAILABLE_WATCH": 2, "UNRESOLVED": 3,
             "RESEARCH_REQUESTED": 4, "REJECTED": 9}
    conf_rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, None: 3}
    rows = []
    for rc in conn.execute("SELECT * FROM research_case").fetchall():
        adj = _latest_output(rc["case_id"], "adjudicator") or {}
        qp_path = case_dir(rc["case_id"]) / "quantpack_latest.json"
        qp = json.loads(qp_path.read_text()) if qp_path.exists() else {}
        rows.append({
            "case_id": rc["case_id"], "ticker": rc["ticker"],
            "state": rc["state"],
            "decision": adj.get("decision", rc["state"]),
            "opportunity_confidence": adj.get("opportunity_confidence"),
            "evidence_confidence": adj.get("evidence_confidence"),
            "quant_confidence": adj.get("quant_confidence"),
            "unresolved": adj.get("unresolved_questions"),
            "entry_gap": (qp.get("entry_analysis") or {}).get(
                "remaining_gap_at_entry"),
            "price_asof": qp.get("asof"),
            "reasons_top": [r.get("conclusion") for r in
                            (adj.get("decision_reasons") or [])[:3]],
        })
    rows.sort(key=lambda r: (order.get(r["state"], 8),
                             conf_rank.get(r["opportunity_confidence"], 3)))
    return {"generated_at": utc_now(),
            "qualified": [r for r in rows if r["state"] == "QUALIFIED_CANDIDATE"],
            "qualified_exists": any(r["state"] == "QUALIFIED_CANDIDATE"
                                    for r in rows),
            "best_available": [r for r in rows if r["state"] in
                               ("CONDITIONAL_CANDIDATE", "BEST_AVAILABLE_WATCH")],
            "unresolved": [r for r in rows if r["state"] in
                           ("UNRESOLVED", "RESEARCH_REQUESTED")],
            "rejected": [r for r in rows if r["state"] == "REJECTED"],
            "all": rows}
