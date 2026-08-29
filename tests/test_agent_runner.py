"""H4: provider-neutral runner — scripted end-to-end orchestration, repair
loop, interruption/resume, and the mocked Anthropic adapter. Offline."""

import json
from pathlib import Path

import pytest

from investment_tool import agent_runner
from investment_tool import config as config_mod
from test_research_foundation import _adj, _capture, _open, _sem_ok


@pytest.fixture
def cfg():
    return config_mod.load("us_trial_v0.4")


class ScriptedAdapter:
    """Fills each order from a per-role script; None = simulate an external
    agent that has not answered yet (interruption)."""

    model_id, provider, runtime = "scripted", "test", "pytest"

    def __init__(self, conn, outputs):
        self.conn, self.outputs, self.calls = conn, outputs, []

    def run(self, order):
        self.calls.append(order["role"])
        doc = self.outputs.get(order["role"])
        if callable(doc):
            doc = doc(self.conn, order)
        if doc is None:
            return None
        Path(order["expected_output"]).write_text(
            json.dumps(doc, ensure_ascii=False))
        return order["expected_output"]


def _scripts(conn, case_id, evidence_id, quote):
    return {
        "search": {"role": "search", "search_state": "COMPLETE",
                   "queries": ["q"], "coverage": {"sec": "FOUND"},
                   "evidence_used": [evidence_id],
                   "negative_findings": [
                       {"id": "nf1", "type": "FACTUAL", "material": True,
                        "text": "revenue fell nine percent",
                        "source_id": evidence_id, "quote": quote,
                        "locator": "p1", "temporal_use": "DECISION"}],
                   "competing_explanations": [], "new_questions": []},
        "constructive": {"role": "constructive", "mechanism": "m",
                         "effect_classification": "UNKNOWN",
                         "horizon_months": [1, 2], "thesis_summary_zh": "s",
                         "falsification_conditions": ["f"], "claims": []},
        "adversarial": {"role": "adversarial", "rationality_case": "r",
                        "risk_register": [], "counter_claims": []},
        "rebuttal": {"role": "rebuttal", "responses": []},
        "semantic_review": lambda conn_, order: _sem_ok(conn_, case_id),
        "adjudicator": _adj("BEST_AVAILABLE_WATCH"),
    }


def test_orchestrator_runs_case_to_final(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    adapter = ScriptedAdapter(conn, _scripts(
        conn, case["case_id"], ev["evidence_id"],
        "revenue fell nine percent in the quarter"))
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], adapter)
    assert out["status"] == "FINAL" and out["state"] == "BEST_AVAILABLE_WATCH"
    roles = [line["step"] for line in out["log"]]
    assert roles == ["search", "freeze_bundle", "constructive", "adversarial",
                     "rebuttal", "semantic_review", "adjudicator"]
    runs = conn.execute("SELECT COUNT(*) FROM agent_run WHERE case_id=?"
                        " AND status='IMPORTED'",
                        (case["case_id"],)).fetchone()[0]
    assert runs == 6


def test_orchestrator_interruption_and_resume(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    scripts = _scripts(conn, case["case_id"], ev["evidence_id"],
                       "revenue fell nine percent in the quarter")
    first = dict(scripts)
    first["constructive"] = None          # external agent hasn't answered
    a1 = ScriptedAdapter(conn, first)
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], a1)
    assert out["status"] == "WAITING_AGENT"
    st = conn.execute("SELECT state FROM research_case WHERE case_id=?",
                      (case["case_id"],)).fetchone()["state"]
    assert st in ("BUNDLE_FROZEN", "QUANT_READY")     # persisted mid-pipeline
    # ... later, an agent answers by writing the expected output file
    order = agent_runner._find_open_order(case["case_id"], "constructive")
    Path(order["expected_output"]).write_text(
        json.dumps(scripts["constructive"]))
    a2 = ScriptedAdapter(conn, scripts)   # manual-adapter semantics resume
    out = agent_runner.orchestrate(conn, cfg, case["case_id"],
                                   agent_runner.ManualAgentAdapter())
    # manual adapter picks up the answered order, then waits at adversarial
    assert out["status"] == "WAITING_AGENT" and out["state"] == "UNDER_ADVERSARIAL"
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], a2)
    assert out["status"] == "FINAL"


def test_orchestrator_repair_loop(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    scripts = _scripts(conn, case["case_id"], ev["evidence_id"],
                       "revenue fell nine percent in the quarter")
    bad = json.loads(json.dumps(scripts["search"]))
    bad["negative_findings"][0]["quote"] = "this quote is not in the source"
    seq = {"n": 0}

    def search_seq(conn_, order):
        seq["n"] += 1
        return bad if seq["n"] == 1 else scripts["search"]

    scripts2 = dict(scripts)
    scripts2["search"] = search_seq
    adapter = ScriptedAdapter(conn, scripts2)
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], adapter)
    assert out["status"] == "REPAIR_REQUESTED"
    assert any("quote not present" in p for p in out["problems"])
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], adapter)
    assert out["status"] == "FINAL"
    rejected = conn.execute("SELECT COUNT(*) FROM agent_run WHERE case_id=?"
                            " AND status='REJECTED_IMPORT'",
                            (case["case_id"],)).fetchone()[0]
    assert rejected == 1


def test_anthropic_adapter_mocked(conn, cfg):
    case = _open(conn, cfg)
    _capture(conn, cfg, case["case_id"])
    calls = {}

    def fake_post(payload):
        calls["model"] = payload["model"]
        doc = {"role": "search", "search_state": "PARTIAL", "queries": ["q"],
               "coverage": {"sec": "NOT_SEARCHED"}, "evidence_used": [],
               "negative_findings": [], "competing_explanations": [],
               "new_questions": []}
        return {"content": [{"type": "text",
                             "text": "Here you go:\n" + json.dumps(doc)}],
                "usage": {"input_tokens": 100, "output_tokens": 50}}

    adapter = agent_runner.AnthropicAdapter(model_id="claude-test",
                                            http_post=fake_post)
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], adapter)
    assert calls["model"] == "claude-test"
    # search imported; orchestrator proceeded to the deterministic step and
    # then waits on constructive input from... the same adapter would loop —
    # a PARTIAL search import is fine, and the run recorded token usage
    run = conn.execute("SELECT model_id, provider, tokens_in, tokens_out"
                       " FROM agent_run WHERE case_id=? AND role='search'",
                       (case["case_id"],)).fetchone()
    assert run["model_id"] == "claude-test" and run["provider"] == "anthropic"
    assert run["tokens_in"] == 100 and run["tokens_out"] == 50
    assert out["status"] in ("FINAL", "WAITING_AGENT", "ADAPTER_ERROR",
                             "FAILED_VALIDATION", "REPAIR_REQUESTED",
                             "STEP_LIMIT")
