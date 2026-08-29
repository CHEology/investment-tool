"""H4: provider-neutral runner — scripted end-to-end orchestration, repair
loop, interruption/resume, and the mocked Anthropic adapter. Offline."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from investment_tool import agent_runner, research
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
        order["context_id"] = f"scripted:{order['agent_instance_id']}"
        order["context_provenance"] = "RUNTIME_VERIFIED"
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
                         "independent_verdict": "INSUFFICIENT",
                         "verdict_confidence": "LOW",
                         "verdict_reason_claim_ids": [],
                         "horizon_months": [1, 2], "thesis_summary_zh": "s",
                         "falsification_conditions": ["f"], "claims": []},
        "adversarial": {"role": "adversarial", "rationality_case": "r",
                        "independent_verdict": "INSUFFICIENT",
                        "verdict_confidence": "LOW",
                        "verdict_reason_claim_ids": [],
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
    order["context_id"] = "external-task-constructive"
    order["context_provenance"] = "CALLER_DECLARED"
    agent_runner._save_order(order)
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


def test_blind_pair_orders_are_frozen_before_first_analyst_output(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    scripts = _scripts(conn, case["case_id"], ev["evidence_id"],
                       "revenue fell nine percent in the quarter")
    scripts["constructive"] = None
    out = agent_runner.orchestrate(
        conn, cfg, case["case_id"], ScriptedAdapter(conn, scripts))
    assert out["status"] == "WAITING_AGENT"
    pair = conn.execute("SELECT * FROM analysis_pair WHERE case_id=?",
                        (case["case_id"],)).fetchone()
    constructive = agent_runner._find_pair_order(
        case["case_id"], "constructive", pair["pair_id"])
    adversarial = agent_runner._find_pair_order(
        case["case_id"], "adversarial", pair["pair_id"])
    assert constructive["created_at_utc"] and adversarial["created_at_utc"]
    assert constructive["visible_roles"] == adversarial["visible_roles"] == []
    assert constructive["agent_instance_id"] != adversarial["agent_instance_id"]
    assert constructive["bundle_sha256"] == adversarial["bundle_sha256"]
    assert Path(constructive["role_dir"]).parent == agent_runner.workqueue_dir()
    assert Path(adversarial["role_dir"]).parent == agent_runner.workqueue_dir()


def test_mutated_frozen_order_is_rejected_before_agent_runs(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    scripts = _scripts(conn, case["case_id"], ev["evidence_id"],
                       "revenue fell nine percent in the quarter")
    scripts["constructive"] = None
    agent_runner.orchestrate(conn, cfg, case["case_id"],
                             ScriptedAdapter(conn, scripts))
    order = agent_runner._find_open_order(case["case_id"], "constructive")
    (Path(order["role_dir"]) / "input.json").write_text('{"tampered":true}')
    out = agent_runner.orchestrate(
        conn, cfg, case["case_id"], agent_runner.ManualAgentAdapter())
    assert out["status"] == "ORDER_INPUT_CHANGED"


def test_manual_analyst_without_real_context_id_is_rejected(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    scripts = _scripts(conn, case["case_id"], ev["evidence_id"],
                       "revenue fell nine percent in the quarter")
    scripts["constructive"] = None
    agent_runner.orchestrate(conn, cfg, case["case_id"],
                             ScriptedAdapter(conn, scripts))
    order = agent_runner._find_open_order(case["case_id"], "constructive")
    Path(order["expected_output"]).write_text(json.dumps(
        _scripts(conn, case["case_id"], ev["evidence_id"],
                 "revenue fell nine percent in the quarter")["constructive"]))
    out = agent_runner.orchestrate(
        conn, cfg, case["case_id"], agent_runner.ManualAgentAdapter())
    assert out["status"] == "REPAIR_REQUESTED"
    assert any("real Agent context ID" in p for p in out["problems"])


def test_same_model_provider_distinct_agent_contexts_are_independent(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    adapter = ScriptedAdapter(conn, _scripts(
        conn, case["case_id"], ev["evidence_id"],
        "revenue fell nine percent in the quarter"))
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], adapter)
    assert out["status"] == "FINAL"
    runs = conn.execute(
        "SELECT provider, model_id, agent_instance_id, context_id, pair_id"
        " FROM agent_run WHERE case_id=? AND role IN"
        " ('constructive','adversarial') AND status='IMPORTED'",
        (case["case_id"],)).fetchall()
    assert len({r["provider"] for r in runs}) == 1
    assert len({r["model_id"] for r in runs}) == 1
    assert len({r["agent_instance_id"] for r in runs}) == 2
    assert len({r["context_id"] for r in runs}) == 2
    dossier = research.freeze_dossier(conn, case["case_id"])
    assert "逻辑独立性：满足" in Path(dossier["path"]).read_text()


def test_same_context_for_both_blind_agents_is_rejected(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])

    class SameContextAdapter(ScriptedAdapter):
        def run(self, order):
            result = super().run(order)
            order["context_id"] = "shared-context"
            order["context_provenance"] = "RUNTIME_VERIFIED"
            return result

    adapter = SameContextAdapter(conn, _scripts(
        conn, case["case_id"], ev["evidence_id"],
        "revenue fell nine percent in the quarter"))
    out = agent_runner.orchestrate(conn, cfg, case["case_id"], adapter)
    assert out["status"] == "REPAIR_REQUESTED"
    assert any("distinct Agent contexts" in p for p in out["problems"])


def test_adversarial_import_atomically_completes_pair(conn, cfg):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    scripts = _scripts(conn, case["case_id"], ev["evidence_id"],
                       "revenue fell nine percent in the quarter")
    adversarial_doc = scripts["adversarial"]
    scripts["adversarial"] = None
    out = agent_runner.orchestrate(
        conn, cfg, case["case_id"], ScriptedAdapter(conn, scripts))
    assert out["status"] == "WAITING_AGENT" and out["state"] == "UNDER_ADVERSARIAL"
    pair = agent_runner._current_pair(conn, case["case_id"])
    order = agent_runner._find_open_order(
        case["case_id"], "adversarial", pair["pair_id"], pair["bundle_version"])
    Path(order["expected_output"]).write_text(json.dumps(adversarial_doc))
    imported = research.import_role_output(
        conn, cfg, case["case_id"], "adversarial", order["expected_output"],
        model_id="same-model", provider="same-provider", runtime="test",
        order_id=order["order_id"], pair_id=pair["pair_id"],
        bundle_version=order["bundle_version"],
        agent_instance_id=order["agent_instance_id"],
        context_id="crash-window-adversarial-context",
        context_provenance="RUNTIME_VERIFIED",
        role_input_sha256=order["role_input_sha256"],
        input_manifest_verified=True, visible_roles=order["visible_roles"],
        output_path=order["expected_output"])
    assert imported["status"] == "IMPORTED"
    assert conn.execute("SELECT status FROM analysis_pair WHERE pair_id=?",
                        (pair["pair_id"],)).fetchone()["status"] == "COMPLETE"


def test_direct_analyst_import_cannot_bypass_pair_gate(conn, cfg, tmp_path):
    case = _open(conn, cfg)
    ev = _capture(conn, cfg, case["case_id"])
    search = _scripts(conn, case["case_id"], ev["evidence_id"],
                      "revenue fell nine percent in the quarter")["search"]
    p = tmp_path / "search.json"
    p.write_text(json.dumps(search))
    assert research.import_role_output(
        conn, cfg, case["case_id"], "search", str(p), model_id="m",
        provider="p", runtime="r")["status"] == "IMPORTED"
    research.freeze_bundle(conn, case["case_id"])
    p = tmp_path / "constructive.json"
    p.write_text(json.dumps(_scripts(
        conn, case["case_id"], ev["evidence_id"],
        "revenue fell nine percent in the quarter")["constructive"]))
    out = research.import_role_output(
        conn, cfg, case["case_id"], "constructive", str(p), model_id="m",
        provider="p", runtime="r")
    assert out["status"] == "REJECTED_IMPORT"
    assert any("requires a current work-order analysis pair" in problem
               for problem in out["problems"])


def test_codex_adapter_starts_ephemeral_web_enabled_agent(conn, cfg):
    case = _open(conn, cfg)
    order = agent_runner._create_order(
        conn, case["case_id"], "search", None, attempts=0)
    seen = {}

    def fake_run(command, prompt, timeout_s):
        seen.update(command=command, prompt=prompt, timeout=timeout_s)
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({
            "role": "search", "search_state": "PARTIAL", "queries": ["q"],
            "coverage": {"sec": "NOT_SEARCHED"}, "evidence_used": [],
            "negative_findings": [], "competing_explanations": [],
            "new_questions": []}))
        return SimpleNamespace(
            returncode=0, stdout="session id: 11111111-2222-3333-4444-555555555555",
            stderr="")

    adapter = agent_runner.CodexCLIAdapter(
        model_id="gpt-test", reasoning_effort="medium",
        command_runner=fake_run, codex_path="/usr/bin/true")
    out = adapter.run(order)
    assert Path(out).exists()
    assert "--ephemeral" in seen["command"]
    assert "--ignore-user-config" in seen["command"]
    assert "workspace-write" in seen["command"]
    assert "sandbox_workspace_write.network_access=true" in seen["command"]
    assert 'web_search="live"' in seen["command"]
    assert "--json" in seen["command"]
    add_dir = Path(seen["command"][seen["command"].index("--add-dir") + 1])
    assert add_dir.is_absolute()
    assert "Use Codex Web Search autonomously" in seen["prompt"]
    assert "research fetch '<URL>'" in seen["prompt"]
    assert order["context_id"] == "11111111-2222-3333-4444-555555555555"
    assert order["context_provenance"] == "RUNTIME_VERIFIED"


def test_completed_web_search_requires_completed_tool_event():
    assert not agent_runner._has_completed_web_search(
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"I used web_search"}}')
    assert agent_runner._has_completed_web_search(
        '{"type":"item.completed","item":{"type":"web_search",'
        '"action":{"type":"search","query":"q"}}}')


def test_agent_json_parser_collapses_only_identical_duplicate_objects():
    one = {"role": "search", "search_state": "COMPLETE", "queries": ["q"]}
    encoded = json.dumps(one)
    assert agent_runner._parse_agent_json(encoded + encoded) == one
    assert agent_runner._parse_agent_json(encoded + "}}") == one
    with pytest.raises(RuntimeError, match="multiple non-identical"):
        agent_runner._parse_agent_json(encoded + json.dumps({**one, "queries": ["x"]}))


def test_non_search_codex_agent_has_web_and_shell_network_disabled(conn, cfg):
    case = _open(conn, cfg)
    research.freeze_bundle(conn, case["case_id"])
    pair = agent_runner._ensure_blind_pair(conn, case["case_id"])
    order = agent_runner._find_pair_order(
        case["case_id"], "constructive", pair["pair_id"])
    seen = {}

    def fake_run(command, prompt, timeout_s):
        seen["command"] = command
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text(json.dumps({
            "role": "constructive", "mechanism": "m",
            "effect_classification": "UNKNOWN",
            "independent_verdict": "INSUFFICIENT", "verdict_confidence": "LOW",
            "verdict_reason_claim_ids": [], "horizon_months": [3, 6],
            "thesis_summary_zh": "证据不足", "claims": [],
            "falsification_conditions": ["new evidence"]}))
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"thread.started","thread_id":'
                   '"11111111-2222-3333-4444-555555555555"}', stderr="")

    adapter = agent_runner.CodexCLIAdapter(
        command_runner=fake_run, codex_path="/usr/bin/true")
    adapter.run(order)
    assert "sandbox_workspace_write.network_access=false" in seen["command"]
    assert 'web_search="disabled"' in seen["command"]
    assert "sandbox_workspace_write.network_access=true" not in seen["command"]
    assert 'web_search="live"' not in seen["command"]


def test_work_orders_use_current_versioned_contracts(conn, cfg):
    case = _open(conn, cfg)
    research.freeze_bundle(conn, case["case_id"])
    expected = {"search": "search_v2.md", "constructive": "constructive_v3.md",
                "adversarial": "adversarial_v3.md",
                "rebuttal": "rebuttal_v2.md",
                "adjudicator": "adjudicator_v3.md"}
    for role, filename in expected.items():
        view = research.export_role_view(conn, case["case_id"], role)
        assert view["contract"].endswith(filename)
        if role in ("constructive", "adversarial", "rebuttal"):
            contract = Path(view["contract"]).read_text()
            assert '`id`、`type`、`material`、`text`' in contract
            assert "不得**添加 `quantpack.` 前缀" in contract


def test_semantic_review_view_contains_every_material_factual_claim(conn, cfg):
    case = _open(conn, cfg)
    research.freeze_bundle(conn, case["case_id"])
    conn.execute(
        "INSERT INTO claim(claim_id,case_id,bundle_version,role,claim_type,"
        "material,text,source_id,quote,verification) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (f"{case['case_id']}:constructive:C1", case["case_id"], 1,
         "constructive", "FACTUAL", 1, "material fact", "evd_example",
         "exact passage", "SUPPORTED"))
    conn.execute(
        "INSERT INTO claim(claim_id,case_id,bundle_version,role,claim_type,"
        "material,text,verification) VALUES(?,?,?,?,?,?,?,?)",
        (f"{case['case_id']}:constructive:C2", case["case_id"], 1,
         "constructive", "NUMERIC", 1, "number", "RECOMPUTED_OK"))
    conn.commit()

    view = research.export_role_view(conn, case["case_id"], "semantic_review")
    payload = json.loads((Path(view["role_dir"]) / "input.json").read_text())

    assert [c["claim_id"] for c in payload["claims"]] == [
        f"{case['case_id']}:constructive:C1"]
    assert payload["claims"][0]["quote"] == "exact passage"


@pytest.mark.skipif(os.environ.get("RUN_CODEX_LIVE") != "1",
                    reason="explicit live Codex/network smoke only")
def test_codex_live_web_search_capture_and_import(conn, cfg):
    """Optional live gate: hosted search -> shell capture -> quote validation."""
    case = _open(conn, cfg)
    order = agent_runner._create_order(
        conn, case["case_id"], "search", None, attempts=0)
    Path(order["contract"]).write_text(
        "Use Codex Web Search at least once to locate the public Example Domain "
        "page. Capture https://example.com through the evidence command in your "
        "instructions, read the returned content_path, and output only this JSON "
        "shape with the real returned evidence ID: "
        '{"role":"search","search_state":"PARTIAL","queries":["..."],'
        '"coverage":{"web_smoke":"FOUND"},"evidence_used":["evd_..."],'
        '"negative_findings":[{"id":"smoke1","type":"FACTUAL",'
        '"material":true,"text":"The captured page identifies itself as the '
        'Example Domain","source_id":"evd_...","quote":"Example Domain",'
        '"locator":"page text","temporal_use":"HINDSIGHT"}],'
        '"competing_explanations":[],"new_questions":[]}')
    adapter = agent_runner.CodexCLIAdapter(reasoning_effort="low", timeout_s=300)
    out_path = adapter.run(order)
    imported = research.import_role_output(
        conn, cfg, case["case_id"], "search", out_path,
        model_id=adapter.model_id, provider=adapter.provider,
        runtime=adapter.runtime, order_id=order["order_id"],
        bundle_version=order["bundle_version"],
        agent_instance_id=order["agent_instance_id"],
        context_id=order["context_id"],
        context_provenance=order["context_provenance"],
        role_input_sha256=order["role_input_sha256"], output_path=out_path)
    assert imported["status"] == "IMPORTED", imported
    evidence = conn.execute(
        "SELECT e.content_path FROM evidence e JOIN case_evidence ce"
        " ON ce.evidence_id=e.evidence_id WHERE ce.case_id=?",
        (case["case_id"],)).fetchone()
    assert evidence and "Example Domain" in Path(evidence["content_path"]).read_text()
    manifested = conn.execute(
        "SELECT COUNT(*) FROM manifest WHERE provider='web'"
        " AND dataset='evidence_page'").fetchone()[0]
    assert manifested == 1
    assert Path(order["trace_path"]).exists()
    assert agent_runner._has_completed_web_search(
        Path(order["trace_path"]).read_text())
