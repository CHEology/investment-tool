"""H1.1 corrections: multi-case evidence immutability (F-F), semantic claim
axes (F-G), structured adjudicator reasons (F-H), opportunity states and
ranked output (F-J). Offline."""

import json
from pathlib import Path

import pytest

from investment_tool import config as config_mod
from investment_tool import research
from test_research_foundation import _adj, _capture, _import, _open


@pytest.fixture
def cfg():
    return config_mod.load("us_trial_v0.4")


def _open2(conn, cfg):
    """A second case for the same company (distinct candidate)."""
    conn.execute(
        "INSERT OR IGNORE INTO candidate(candidate_id, company_id, lane, state,"
        " profile_json, gates_json, detected_at_utc, config_version)"
        " VALUES('cand_y','US:TT','A','US_TRIAL_LEAD',?, '{}',"
        " '2026-08-28T23:00:00Z','us_trial_v0.4')",
        (json.dumps({"event_id": "ev_us_y", "ticker": "TT",
                     "accession": "acc-y",
                     "first_seen_at_utc": "2026-08-28T21:18:28Z",
                     "reaction": {"state": "OK",
                                  "anchors": {"event_session": "2026-08-27",
                                              "first_actionable_session":
                                                  "2026-08-31"}}}),))
    conn.commit()
    return research.open_case(conn, cfg, "cand_y")


# ------------------------------------------------------- F-F evidence model


def test_same_evidence_shared_by_two_cases_without_reassignment(conn, cfg):
    c1 = _open(conn, cfg)
    c2 = _open2(conn, cfg)
    e1 = _capture(conn, cfg, c1["case_id"])
    first_seen = conn.execute("SELECT first_seen_at_utc FROM evidence WHERE"
                              " evidence_id=?", (e1["evidence_id"],)
                              ).fetchone()["first_seen_at_utc"]
    e2 = _capture(conn, cfg, c2["case_id"])      # same content, second case
    assert e2["evidence_id"] == e1["evidence_id"]
    rows = conn.execute("SELECT case_id FROM case_evidence WHERE evidence_id=?",
                        (e1["evidence_id"],)).fetchall()
    assert {r["case_id"] for r in rows} == {c1["case_id"], c2["case_id"]}
    # the original row was NOT rewritten (immutability)
    again = conn.execute("SELECT first_seen_at_utc, case_id FROM evidence WHERE"
                         " evidence_id=?", (e1["evidence_id"],)).fetchone()
    assert again["first_seen_at_utc"] == first_seen
    assert again["case_id"] == c1["case_id"]     # provenance of first capture
    # both cases can cite it
    for case in (c1, c2):
        doc = {"role": "search", "search_state": "COMPLETE", "queries": ["q"],
               "coverage": {"sec": "FOUND"},
               "evidence_used": [e1["evidence_id"]],
               "negative_findings": [
                   {"id": "nf1", "type": "FACTUAL", "material": True,
                    "text": "revenue fell nine percent",
                    "source_id": e1["evidence_id"],
                    "quote": "revenue fell nine percent in the quarter",
                    "locator": "p1", "temporal_use": "DECISION"}],
               "competing_explanations": [], "new_questions": []}
        out = _import(conn, cfg, case["case_id"], "search", doc, Path(
            research.case_dir(case["case_id"])))
        assert out["status"] == "IMPORTED"


def test_bundle_binds_content_and_detects_mutation(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    b1 = research.freeze_bundle(conn, case["case_id"])
    v = research.verify_bundle(conn, case["case_id"], b1["version"])
    assert v["ok"] is True and v["originals_mutated"] == []
    # mutate the ORIGINAL evidence text after freezing
    row = conn.execute("SELECT content_path FROM evidence WHERE evidence_id=?",
                       (ev["evidence_id"],)).fetchone()
    Path(row["content_path"]).write_text("tampered content")
    v = research.verify_bundle(conn, case["case_id"], b1["version"])
    assert v["bundle_json_ok"] is True           # frozen json unchanged
    assert v["snapshots_ok"] is True             # snapshot copy intact
    assert ev["evidence_id"] in v["originals_mutated"]   # tamper detected
    # later versions do not break the old bundle's verifiability
    _capture(conn, cfg, case["case_id"], url="https://example-news.com/more",
             body=b"<p>additional</p>")
    research.freeze_bundle(conn, case["case_id"])
    v1_again = research.verify_bundle(conn, case["case_id"], b1["version"])
    assert v1_again["bundle_json_ok"] is True


# ------------------------------------------------------ F-G semantic axes


def test_comparison_claim_requires_comparison_quote(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"],
                  body=b"<p>Revenue increased 8% to $350.7 million in Q2.</p>")
    doc = {"role": "search", "search_state": "COMPLETE", "queries": ["q"],
           "coverage": {"sec": "FOUND"}, "evidence_used": [ev["evidence_id"]],
           "negative_findings": [
               {"id": "nf1", "type": "FACTUAL", "material": True,
                "text": "营收超出分析师共识预期",
                "source_id": ev["evidence_id"],
                "quote": "Revenue increased 8% to $350.7 million",
                "locator": "p1", "temporal_use": "DECISION"}],
           "competing_explanations": [], "new_questions": []}
    out = _import(conn, cfg, case["case_id"], "search", doc, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("comparison" in p for p in out["problems"])
    # with a quote that itself carries the estimate comparison it passes
    ev2 = _capture(conn, cfg, case["case_id"],
                   url="https://example-news.com/est",
                   body=b"<p>Revenue of $350.7M beat analyst estimates of"
                        b" $349.2 million.</p>")
    doc["negative_findings"][0]["source_id"] = ev2["evidence_id"]
    doc["negative_findings"][0]["quote"] = \
        "beat analyst estimates of $349.2 million"
    doc["evidence_used"] = [ev2["evidence_id"]]
    assert _import(conn, cfg, case["case_id"], "search", doc,
                   tmp_path)["status"] == "IMPORTED"


def test_semantic_review_downgrades_quote_present_but_not_entailed(
        conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    doc = {"role": "search", "search_state": "COMPLETE", "queries": ["q"],
           "coverage": {"sec": "FOUND"}, "evidence_used": [ev["evidence_id"]],
           "negative_findings": [
               {"id": "nf1", "type": "FACTUAL", "material": True,
                "text": "管理层隐含下调了下半年预期",   # inference, not in source
                "source_id": ev["evidence_id"],
                "quote": "revenue fell nine percent in the quarter",
                "locator": "p1", "temporal_use": "DECISION"}],
           "competing_explanations": [], "new_questions": []}
    assert _import(conn, cfg, case["case_id"], "search", doc,
                   tmp_path)["status"] == "IMPORTED"   # quote IS present
    research.freeze_bundle(conn, case["case_id"])
    base = {"role": "constructive", "mechanism": "m",
            "effect_classification": "UNKNOWN", "horizon_months": [1, 2],
            "thesis_summary_zh": "s", "falsification_conditions": ["f"],
            "claims": []}
    _import(conn, cfg, case["case_id"], "constructive", base, tmp_path)
    _import(conn, cfg, case["case_id"], "adversarial",
            {"role": "adversarial", "rationality_case": "r",
             "risk_register": [], "counter_claims": []}, tmp_path)
    _import(conn, cfg, case["case_id"], "rebuttal",
            {"role": "rebuttal", "responses": []}, tmp_path)
    cid = conn.execute("SELECT claim_id FROM claim WHERE case_id=?"
                       " AND claim_type='FACTUAL'",
                       (case["case_id"],)).fetchone()["claim_id"]
    review = {"role": "semantic_review", "rulings": [
        {"claim_id": cid, "ruling": "UNSUPPORTED",
         "explanation": "引语只说营收下降，未提及任何指引/预期含义",
         "passage": "revenue fell nine percent in the quarter"}]}
    assert _import(conn, cfg, case["case_id"], "semantic_review", review,
                   tmp_path)["status"] == "IMPORTED"
    row = conn.execute("SELECT verification, verification_detail FROM claim"
                       " WHERE claim_id=?", (cid,)).fetchone()
    assert row["verification"] == "UNSUPPORTED"
    assert json.loads(row["verification_detail"])["semantic"] == "UNSUPPORTED"
    # the adjudicator can no longer cite it
    bad = _adj("REJECTED", reasons=[
        {"reason_id": "r1", "reason_type": "FACTUAL", "claim_ids": [cid],
         "weight": "HIGH", "conclusion": "based on the downgraded claim"}])
    out = _import(conn, cfg, case["case_id"], "adjudicator", bad, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"


def test_incomplete_semantic_review_blocks(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    doc = {"role": "search", "search_state": "COMPLETE", "queries": ["q"],
           "coverage": {"sec": "FOUND"}, "evidence_used": [ev["evidence_id"]],
           "negative_findings": [
               {"id": "nf1", "type": "FACTUAL", "material": True,
                "text": "营收下降", "source_id": ev["evidence_id"],
                "quote": "revenue fell nine percent in the quarter",
                "locator": "p1", "temporal_use": "DECISION"}],
           "competing_explanations": [], "new_questions": []}
    _import(conn, cfg, case["case_id"], "search", doc, tmp_path)
    research.freeze_bundle(conn, case["case_id"])
    for role, d in (("constructive",
                     {"role": "constructive", "mechanism": "m",
                      "effect_classification": "UNKNOWN",
                      "horizon_months": [1, 2], "thesis_summary_zh": "s",
                      "falsification_conditions": ["f"], "claims": []}),
                    ("adversarial", {"role": "adversarial",
                                     "rationality_case": "r",
                                     "risk_register": [],
                                     "counter_claims": []}),
                    ("rebuttal", {"role": "rebuttal", "responses": []})):
        _import(conn, cfg, case["case_id"], role, d, tmp_path)
    out = _import(conn, cfg, case["case_id"], "semantic_review",
                  {"role": "semantic_review", "rulings": []}, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("not ruled" in p for p in out["problems"])


def test_numeric_hidden_in_judgment_is_rejected(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    doc = {"role": "search", "search_state": "COMPLETE", "queries": ["q"],
           "coverage": {"sec": "FOUND"}, "evidence_used": [ev["evidence_id"]],
           "negative_findings": [
               {"id": "j9", "type": "JUDGMENT", "material": True,
                "text": "缺口约 1.7 倍，反应与损害同量级",
                "support_claim_ids": ["nf_missing"]}],
           "competing_explanations": [], "new_questions": []}
    out = _import(conn, cfg, case["case_id"], "search", doc, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("numeric assertion" in p for p in out["problems"])


# ------------------------------------------------- F-H adjudicator reasons


def _run_to_adjudication(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    _capture(conn, cfg, case["case_id"])
    doc = {"role": "search", "search_state": "COMPLETE", "queries": ["q"],
           "coverage": {"sec": "FOUND"}, "evidence_used": [],
           "negative_findings": [], "competing_explanations": [],
           "new_questions": []}
    _import(conn, cfg, case["case_id"], "search", doc, tmp_path)
    (research.case_dir(case["case_id"]) / "quantpack_latest.json").write_text(
        json.dumps({"event_mcap_change": {"abnormal_change_est": -143646688.7},
                    "damage_high": 106000000.0}))
    research.freeze_bundle(conn, case["case_id"])
    for role, d in (("constructive",
                     {"role": "constructive", "mechanism": "m",
                      "effect_classification": "UNKNOWN",
                      "horizon_months": [1, 2], "thesis_summary_zh": "s",
                      "falsification_conditions": ["f"], "claims": []}),
                    ("adversarial", {"role": "adversarial",
                                     "rationality_case": "r",
                                     "risk_register": [],
                                     "counter_claims": []}),
                    ("rebuttal", {"role": "rebuttal", "responses": []}),
                    ("semantic_review", {"role": "semantic_review",
                                         "rulings": []})):
        _import(conn, cfg, case["case_id"], role, d, tmp_path)
    return case


def test_bbw_style_ratio_conflict_rejected(conn, cfg, tmp_path):
    """F-H regression modeled on the BBW inconsistency: a stated 1.7x ratio
    whose declared derivation actually yields ~1.355x must be rejected."""
    case = _run_to_adjudication(conn, cfg, tmp_path)
    bad = _adj("REJECTED", reasons=[
        {"reason_id": "n1", "reason_type": "NUMERIC", "value": 1.7,
         "derivation": {"op": "abs_ratio",
                        "numerator_quant_ref":
                            "event_mcap_change.abnormal_change_est",
                        "denominator_value": 106000000.0},
         "weight": "HIGH", "conclusion": "缺口约1.7倍"}])
    out = _import(conn, cfg, case["case_id"], "adjudicator", bad, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("conflicts with derived" in p for p in out["problems"])
    good = _adj("REJECTED", reasons=[
        {"reason_id": "n1", "reason_type": "NUMERIC", "value": 1.355,
         "derivation": {"op": "abs_ratio",
                        "numerator_quant_ref":
                            "event_mcap_change.abnormal_change_est",
                        "denominator_value": 106000000.0},
         "weight": "HIGH", "conclusion": "缺口约1.36倍"}])
    assert _import(conn, cfg, case["case_id"], "adjudicator", good,
                   tmp_path)["status"] == "IMPORTED"


# --------------------------------------------------- F-J opportunity states


def test_qualified_blocked_by_indispensable_missing(conn, cfg, tmp_path):
    case = _run_to_adjudication(conn, cfg, tmp_path)
    doc = _adj("QUALIFIED_CANDIDATE")
    doc["indispensable_missing"] = ["pre-event consensus"]
    out = _import(conn, cfg, case["case_id"], "adjudicator", doc, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    doc2 = _adj("CONDITIONAL_CANDIDATE")
    doc2["indispensable_missing"] = ["pre-event consensus"]
    out = _import(conn, cfg, case["case_id"], "adjudicator", doc2, tmp_path)
    assert out["status"] == "IMPORTED"
    st = conn.execute("SELECT state FROM research_case WHERE case_id=?",
                      (case["case_id"],)).fetchone()["state"]
    assert st == "CONDITIONAL_CANDIDATE"


def test_rank_output_with_zero_qualified_but_best_available(conn, cfg, tmp_path):
    case = _run_to_adjudication(conn, cfg, tmp_path)
    doc = _adj("BEST_AVAILABLE_WATCH")
    doc["opportunity_confidence"] = "MEDIUM"
    _import(conn, cfg, case["case_id"], "adjudicator", doc, tmp_path)
    out = research.rank_cases(conn)
    assert out["qualified_exists"] is False
    assert out["qualified"] == []
    assert len(out["best_available"]) == 1
    assert out["best_available"][0]["state"] == "BEST_AVAILABLE_WATCH"
