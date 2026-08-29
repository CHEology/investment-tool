"""H1 research foundation: case lifecycle, open-world gateway, bundle
freezing, and the mechanical claim validators. Offline throughout."""

import json

import pytest

from investment_tool import config as config_mod
from investment_tool import evidence_gateway, research


@pytest.fixture
def cfg():
    return config_mod.load("us_trial_v0.3")


class _FakeResp:
    def __init__(self, content=b"", status=200):
        self.content, self.status_code = content, status


class _FakeHttp:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        page = self.pages.get(url)
        return _FakeResp(*page) if page else _FakeResp(b"not found", 404)


def _mk_candidate(conn, ticker="TT"):
    conn.execute("INSERT OR IGNORE INTO company(company_id, name_en, cik,"
                 " created_asof) VALUES('US:TT','Test Corp','7000001',"
                 " '2026-01-01T00:00:00Z')")
    conn.execute("INSERT OR IGNORE INTO listing(listing_id, company_id, ticker,"
                 " exchange, currency) VALUES('NASDAQ:TT','US:TT',?,"
                 " 'NASDAQ','USD')", (ticker,))
    profile = {
        "event_id": "ev_us_x", "event_type": "ISSUER_8K", "ticker": ticker,
        "accession": "acc-x", "accepted_at_utc": "2026-08-27T12:02:52Z",
        "first_seen_at_utc": "2026-08-28T21:18:28Z",
        "reaction": {"state": "OK", "mkt_adj_post_ret1": -0.11,
                     "anchors": {"event_session": "2026-08-27",
                                 "first_actionable_session": "2026-08-31",
                                 "precision": "TIME"}},
        "gate": "TRIGGERED", "trigger_legs": ["evt1"],
        "config_version": "us_trial_v0.3",
    }
    conn.execute(
        "INSERT OR IGNORE INTO candidate(candidate_id, company_id, lane, state,"
        " profile_json, gates_json, detected_at_utc, config_version)"
        " VALUES('cand_x','US:TT','A','US_TRIAL_LEAD',?, '{}',"
        " '2026-08-28T23:00:00Z','us_trial_v0.3')",
        (json.dumps(profile),))
    conn.commit()
    return "cand_x"


def _open(conn, cfg):
    _mk_candidate(conn)
    return research.open_case(conn, cfg, "cand_x")


def _capture(conn, cfg, case_id, url="https://example-news.com/story",
             body=b"<html><p>Sample story: revenue fell nine percent in the"
                  b" quarter, management said.</p></html>",
             published="2026-08-27T13:00:00Z", source_class=None):
    return evidence_gateway.capture(
        conn, cfg, case_id, url, published_at_utc=published,
        source_class=source_class, http=_FakeHttp({url: (body, 200)}))


# ------------------------------------------------------------------ case


def test_open_case_computes_decision_cutoff_at_actionable_open(conn, cfg):
    out = _open(conn, cfg)
    # first actionable session 2026-08-31: XNYS open 13:30Z
    assert out["decision_cutoff_utc"] == "2026-08-31T13:30:00Z"
    again = research.open_case(conn, cfg, "cand_x")
    assert again["case_id"] == out["case_id"]     # idempotent reuse


# --------------------------------------------------------------- gateway


def test_gateway_captures_unknown_domain_as_discovery_lead(conn, cfg):
    case = _open(conn, cfg)
    out = _capture(conn, cfg, case["case_id"],
                   url="https://obscure-trade-blog.example.net/post/1")
    assert "error" not in out
    assert out["source_class"] == "DISCOVERY_LEAD"   # captured, not rejected
    assert out["decision_eligible"] is True
    row = conn.execute("SELECT * FROM evidence WHERE evidence_id=?",
                       (out["evidence_id"],)).fetchone()
    assert row["case_id"] == case["case_id"] and row["sha256"]


def test_gateway_classifies_known_domains_and_records_failures(conn, cfg):
    case = _open(conn, cfg)
    assert evidence_gateway.classify_domain(
        "https://www.sec.gov/x") == "PRIMARY_REGULATORY"
    assert evidence_gateway.classify_domain(
        "https://www.reuters.com/x") == "INDEPENDENT_REPORTING"
    out = evidence_gateway.capture(
        conn, cfg, case["case_id"], "https://blocked.example.com/x",
        http=_FakeHttp({}))
    assert "error" in out and "BLOCKED" in out["hint"]
    # the failed fetch still left a manifest (provenance, never silent)
    n = conn.execute("SELECT COUNT(*) FROM manifest WHERE provider='web'"
                     " AND http_status IS NULL OR http_status=404").fetchone()[0]
    assert n >= 1


# ---------------------------------------------------------------- bundle


def test_bundle_freeze_is_versioned_and_immutable(conn, cfg):
    case = _open(conn, cfg)
    _capture(conn, cfg, case["case_id"])
    b1 = research.freeze_bundle(conn, case["case_id"])
    assert b1["version"] == 1 and b1["sources"] == 1
    _capture(conn, cfg, case["case_id"],
             url="https://example-news.com/story2", body=b"<p>more</p>")
    b2 = research.freeze_bundle(conn, case["case_id"])
    assert b2["version"] == 2 and b2["sources"] == 2
    assert b1["sha256"] != b2["sha256"]
    rows = conn.execute("SELECT version FROM evidence_bundle WHERE case_id=?"
                        " ORDER BY version", (case["case_id"],)).fetchall()
    assert [r["version"] for r in rows] == [1, 2]


# ------------------------------------------------------------ validators


def _sem_ok(conn, case_id):
    """Rule every material factual claim SEMANTICALLY_SUPPORTED (test helper)."""
    rows = conn.execute("SELECT claim_id, quote FROM claim WHERE case_id=?"
                        " AND claim_type='FACTUAL' AND material=1",
                        (case_id,)).fetchall()
    return {"role": "semantic_review", "rulings": [
        {"claim_id": r["claim_id"], "ruling": "SEMANTICALLY_SUPPORTED",
         "explanation": "quote entails claim (test)",
         "passage": r["quote"] or "-"} for r in rows]}


def _adj(decision="UNRESOLVED", reasons=None, **kw):
    return {"role": "adjudicator", "decision": decision,
            "confidence": "LOW", "opportunity_confidence": "LOW",
            "evidence_confidence": "LOW", "quant_confidence": "LOW",
            "rationale_zh": "r", "unresolved_questions": [],
            "required_evidence": kw.get("required_evidence", []),
            "decision_reasons": reasons or [
                {"reason_id": "r1", "reason_type": "COVERAGE",
                 "weight": "LOW", "conclusion": "test"}]}


def _import(conn, cfg, case_id, role, doc, tmp_path):
    p = tmp_path / f"{role}_out.json"
    p.write_text(json.dumps(doc, ensure_ascii=False))
    return research.import_role_output(
        conn, cfg, case_id, role, str(p), model_id="test-model",
        provider="test", runtime="pytest")


def _search_doc(evidence_id, quote, extra_claims=(), state="COMPLETE"):
    return {"role": "search", "search_state": state,
            "queries": ["q1"], "coverage": {"sec": "FOUND", "ir": "FOUND"},
            "evidence_used": [evidence_id],
            "negative_findings": [
                {"id": "nf1", "type": "FACTUAL", "material": True,
                 "text": "revenue fell nine percent",
                 "source_id": evidence_id, "quote": quote,
                 "locator": "para 1", "temporal_use": "DECISION"},
                *extra_claims],
            "competing_explanations": [], "new_questions": []}


def test_factual_claim_quote_must_match_stored_text(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    ok = _import(conn, cfg, case["case_id"], "search",
                 _search_doc(ev["evidence_id"],
                             "revenue fell nine percent in the quarter"),
                 tmp_path)
    assert ok["status"] == "IMPORTED"
    assert ok["claim_states"].get("SUPPORTED") == 1
    bad = _import(conn, cfg, case["case_id"], "search",
                  _search_doc(ev["evidence_id"],
                              "revenue rose forty percent"), tmp_path)
    assert bad["status"] == "REJECTED_IMPORT"
    assert any("quote not present" in p for p in bad["problems"])


def test_uncaptured_source_rejected_with_capture_hint(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    out = _import(conn, cfg, case["case_id"], "search",
                  _search_doc("evd_nonexistent", "whatever quote this is"),
                  tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("evidence-fetch" in p for p in out["problems"])


def test_temporal_cutoff_enforced_mechanically(conn, cfg, tmp_path):
    """A decision-bearing material claim citing a post-cutoff source must be
    rejected; the same claim marked HINDSIGHT imports."""
    case = _open(conn, cfg)
    late = _capture(conn, cfg, case["case_id"],
                    url="https://example-news.com/late",
                    published="2026-09-02T10:00:00Z")   # after 08-31 open
    doc = _search_doc(late["evidence_id"],
                      "revenue fell nine percent in the quarter")
    out = _import(conn, cfg, case["case_id"], "search", doc, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("decision cutoff" in p for p in out["problems"])
    doc["negative_findings"][0]["temporal_use"] = "HINDSIGHT"
    out = _import(conn, cfg, case["case_id"], "search", doc, tmp_path)
    assert out["status"] == "IMPORTED"
    row = conn.execute("SELECT temporal_basis FROM claim WHERE case_id=?",
                       (case["case_id"],)).fetchone()
    assert row["temporal_basis"] == "HINDSIGHT"


def test_numeric_claims_must_match_quantpack_or_recompute(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    (research.case_dir(case["case_id"]) / "quantpack_latest.json").write_text(
        json.dumps({"reaction": {"mkt_adj_post_ret1": -0.1122}}))
    _import(conn, cfg, case["case_id"], "search",
            _search_doc(ev["evidence_id"],
                        "revenue fell nine percent in the quarter"), tmp_path)
    research.freeze_bundle(conn, case["case_id"])
    base = {"role": "constructive", "mechanism": "m",
            "effect_classification": "BOUNDED", "horizon_months": [3, 9],
            "thesis_summary_zh": "s", "falsification_conditions": ["f"],
            "damage_params": None}
    good = dict(base, claims=[
        {"id": "n1", "type": "NUMERIC", "material": True,
         "text": "事件日市场调整后收益 -11.2%", "value": -0.112,
         "quant_ref": "reaction.mkt_adj_post_ret1"}])
    out = _import(conn, cfg, case["case_id"], "constructive", good, tmp_path)
    assert out["status"] == "IMPORTED"
    assert out["claim_states"].get("RECOMPUTED_OK") == 1
    # a fabricated number is caught (state moved on, so use a fresh case ...):
    case2 = research.open_case(conn, cfg, "cand_x")
    assert case2["state"] == "UNDER_ADVERSARIAL"   # same case advanced
    bad = {"role": "adversarial", "rationality_case": "r",
           "risk_register": [],
           "counter_claims": [
               {"id": "n2", "type": "NUMERIC", "material": True,
                "text": "跌幅其实是 -25%", "value": -0.25,
                "quant_ref": "reaction.mkt_adj_post_ret1"}]}
    out = _import(conn, cfg, case["case_id"], "adversarial", bad, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    assert any("!= quantpack" in p for p in out["problems"])


def test_damage_recompute_path(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    _import(conn, cfg, case["case_id"], "search",
            _search_doc(ev["evidence_id"],
                        "revenue fell nine percent in the quarter"), tmp_path)
    research.freeze_bundle(conn, case["case_id"])
    params = {"components": [{"name": "structural", "classification": "STRUCTURAL",
                              "annual_profit_delta": "-1000000",
                              "source": "test fixture"}],
              "discount_rate": {"value": "0.10", "source": "test"},
              "duration_years_temporary": {"value": "3", "source": "test"}}
    doc = {"role": "constructive", "mechanism": "m",
           "effect_classification": "BOUNDED", "horizon_months": [3, 9],
           "thesis_summary_zh": "s", "falsification_conditions": ["f"],
           "damage_params": None,
           "claims": [{"id": "d1", "type": "NUMERIC", "material": True,
                       "text": "损害区间约 [2.49M, 10M]",
                       "value": 0.0,
                       "recompute": {"kind": "damage_template",
                                     "template": "earnings_decomposition",
                                     "params": params},
                       "expect": {"low": 2486851.99, "high": 10000000}}]}
    out = _import(conn, cfg, case["case_id"], "constructive", doc, tmp_path)
    assert out["status"] == "IMPORTED"
    assert out["claim_states"].get("RECOMPUTED_OK") == 1


def test_material_judgment_needs_anchor_and_flow_reaches_final(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    _import(conn, cfg, case["case_id"], "search",
            _search_doc(ev["evidence_id"],
                        "revenue fell nine percent in the quarter"), tmp_path)
    research.freeze_bundle(conn, case["case_id"])
    bad = {"role": "constructive", "mechanism": "m",
           "effect_classification": "UNKNOWN", "horizon_months": [3, 9],
           "thesis_summary_zh": "s", "falsification_conditions": ["f"],
           "claims": [{"id": "j1", "type": "JUDGMENT", "material": True,
                       "text": "这是过度反应"}]}
    out = _import(conn, cfg, case["case_id"], "constructive", bad, tmp_path)
    assert out["status"] == "REJECTED_IMPORT"
    good = dict(bad)
    good["claims"] = [{"id": "j1", "type": "JUDGMENT", "material": True,
                       "text": "反应弱于披露恶化幅度",
                       "support_claim_ids": [f"{case['case_id']}:search:nf1"
                                             .split(":", 1)[1]]}]
    # support ids reference claims by their in-document id or stored id;
    # use the stored one:
    good["claims"][0]["support_claim_ids"] = ["nf1"]
    out = _import(conn, cfg, case["case_id"], "constructive", good, tmp_path)
    assert out["status"] == "IMPORTED"
    # adversarial -> rebuttal -> adjudicator flow with state gating
    adv = {"role": "adversarial", "rationality_case": "可能合理",
           "risk_register": [{"category": "expectation", "severity": "MEDIUM",
                              "text": "t", "claim_id": None}],
           "counter_claims": []}
    assert _import(conn, cfg, case["case_id"], "adversarial", adv,
                   tmp_path)["status"] == "IMPORTED"
    reb = {"role": "rebuttal", "responses": [
        {"counter_claim_id": "-", "stance": "CONCEDE", "response": "ok",
         "claims": []}]}
    assert _import(conn, cfg, case["case_id"], "rebuttal", reb,
                   tmp_path)["status"] == "IMPORTED"
    assert _import(conn, cfg, case["case_id"], "semantic_review",
                   _sem_ok(conn, case["case_id"]),
                   tmp_path)["status"] == "IMPORTED"
    out = _import(conn, cfg, case["case_id"], "adjudicator", _adj(), tmp_path)
    assert out["status"] == "IMPORTED" and out["decision"] == "UNRESOLVED"
    st = conn.execute("SELECT state FROM research_case WHERE case_id=?",
                      (case["case_id"],)).fetchone()["state"]
    assert st == "UNRESOLVED"
    dossier = research.freeze_dossier(conn, case["case_id"])
    assert "artifact_id" in dossier and dossier["decision"] == "UNRESOLVED"


def test_role_state_machine_rejects_out_of_order_imports(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    doc = {"role": "constructive", "mechanism": "m",
           "effect_classification": "UNKNOWN", "horizon_months": [1, 2],
           "thesis_summary_zh": "s", "falsification_conditions": [],
           "claims": []}
    out = _import(conn, cfg, case["case_id"], "constructive", doc, tmp_path)
    assert out["status"] == "ERROR"
    assert any("does not accept" in p for p in out["problems"])


def test_research_request_loop_is_bounded(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    for _round in range(3):
        _import(conn, cfg, case["case_id"], "search",
                _search_doc(ev["evidence_id"],
                            "revenue fell nine percent in the quarter"),
                tmp_path)
        research.freeze_bundle(conn, case["case_id"])
        doc = {"role": "constructive", "mechanism": "m",
               "effect_classification": "UNKNOWN", "horizon_months": [1, 2],
               "thesis_summary_zh": "s", "falsification_conditions": ["f"],
               "claims": []}
        assert _import(conn, cfg, case["case_id"], "constructive", doc,
                       tmp_path)["status"] == "IMPORTED"
        adv = {"role": "adversarial", "rationality_case": "r",
               "risk_register": [], "counter_claims": []}
        _import(conn, cfg, case["case_id"], "adversarial", adv, tmp_path)
        reb = {"role": "rebuttal", "responses": []}
        _import(conn, cfg, case["case_id"], "rebuttal", reb, tmp_path)
        _import(conn, cfg, case["case_id"], "semantic_review",
                _sem_ok(conn, case["case_id"]), tmp_path)
        out = _import(conn, cfg, case["case_id"], "adjudicator",
                      _adj("RESEARCH_REQUESTED", required_evidence=["x"]),
                      tmp_path)
        assert out["status"] == "IMPORTED"
    # third request exceeds MAX_LOOPS=2 -> forced UNRESOLVED
    st = conn.execute("SELECT state, loop_count FROM research_case"
                      " WHERE case_id=?", (case["case_id"],)).fetchone()
    assert st["state"] == "UNRESOLVED" and st["loop_count"] == 2
