"""Provider-neutral research runner (H4/F-N): the application drives the
role sequence; an Agent runtime fills work orders.

Honest capability statement: with the ManualAgentAdapter this is
SEMI-AUTOMATIC — the application owns sequencing, deterministic steps
(quantpack, bundle freeze), validation, repair requests, and resumable
state; the model side is any coding agent (Claude Code, Codex, a human) that
consumes `data/research/workqueue/` orders and writes the output file. The
AnthropicAdapter makes the same loop fully application-invoked when an API
key is configured (env ANTHROPIC_API_KEY); its absence never blocks the
manual path. No adapter can bypass import validation.

Resumability: orchestrate() derives everything from research_case.state and
the on-disk order files — interrupt anywhere, run again, it continues.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from investment_tool import quantpack, research
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

MAX_REPAIRS = 2

_STATE_ROLE = {
    "OPENED": "search",
    "EVIDENCE_SEARCH": None,        # deterministic step: quantpack + freeze
    "RESEARCH_REQUESTED": "search",
    "BUNDLE_FROZEN": "constructive",
    "QUANT_READY": "constructive",
    "UNDER_ADVERSARIAL": "adversarial",
    "REBUTTAL": "rebuttal",
    "SEMANTIC_REVIEW": "semantic_review",
    "ADJUDICATION": "adjudicator",
}


def workqueue_dir() -> Path:
    d = DEFAULT_DATA_DIR / "research" / "workqueue"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _order_path(order_id: str) -> Path:
    return workqueue_dir() / f"{order_id}.json"


def _find_open_order(case_id: str, role: str) -> dict | None:
    for p in sorted(workqueue_dir().glob("order_*.json")):
        if "_output" in p.stem:
            continue
        d = json.loads(p.read_text())
        if d["case_id"] == case_id and d["role"] == role \
                and d["status"] in ("PENDING", "CLAIMED"):
            return d
    return None


def _create_order(conn, case_id: str, role: str, problems: list[str] | None,
                  attempts: int) -> dict:
    view = research.export_role_view(conn, case_id, role)
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    order = {
        "order_id": order_id, "case_id": case_id, "role": role,
        "status": "PENDING", "attempts": attempts,
        "created_at_utc": utc_now(),
        "role_dir": view.get("role_dir"),
        "contract": view.get("contract"),
        "bundle": view.get("bundle"),
        "expected_output": str(workqueue_dir()
                               / f"{order_id}_{role}_output.json"),
        "repair_problems": problems or [],
        "instructions": (
            f"Read contract + input in {view.get('role_dir')} (and the bundle"
            f" it references), perform the {role} role, and write the"
            " structured JSON output to expected_output. Open-world search"
            " goes through `invest research fetch`. Repair problems, if any,"
            " list exactly what the validator rejected last attempt."),
    }
    _order_path(order_id).write_text(json.dumps(order, ensure_ascii=False,
                                                indent=2))
    return order


def _finish_order(order: dict, status: str, note: str | None = None) -> None:
    order["status"] = status
    order["finished_at_utc"] = utc_now()
    if note:
        order["note"] = note
    _order_path(order["order_id"]).write_text(
        json.dumps(order, ensure_ascii=False, indent=2))


class ManualAgentAdapter:
    """Writes the order and stops (WAITING_AGENT). An external coding agent
    fills expected_output; the next orchestrate() call imports it."""

    name = "manual"
    model_id = "external-agent"
    provider = "manual"
    runtime = "workqueue-file-contract"

    def run(self, order: dict) -> str | None:
        out = Path(order["expected_output"])
        return str(out) if out.exists() else None


class AnthropicAdapter:
    """Application-invoked model call over the same file contract. Requires
    ANTHROPIC_API_KEY; the HTTP layer is injectable for tests."""

    name = "anthropic"
    provider = "anthropic"
    runtime = "anthropic-messages-api"

    def __init__(self, model_id: str = "claude-sonnet-5", http_post=None,
                 max_tokens: int = 8000):
        import os
        self.model_id = model_id
        self.max_tokens = max_tokens
        self._key = os.environ.get("ANTHROPIC_API_KEY")
        self._post = http_post or self._default_post

    def _default_post(self, payload: dict) -> dict:
        import requests
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": self._key or "",
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=payload, timeout=300)
        resp.raise_for_status()
        return resp.json()

    def run(self, order: dict) -> str | None:
        if not self._key and self._post == self._default_post:
            raise RuntimeError("ANTHROPIC_API_KEY not configured — use the"
                               " manual adapter or set the key")
        contract = Path(order["contract"]).read_text() \
            if order.get("contract") and Path(order["contract"]).exists() else ""
        role_input = ""
        rd = order.get("role_dir")
        if rd and (Path(rd) / "input.json").exists():
            role_input = (Path(rd) / "input.json").read_text()
        bundle_text = ""
        if order.get("bundle") and Path(order["bundle"]).exists():
            bundle_text = Path(order["bundle"]).read_text()[:150000]
        prompt = (f"{contract}\n\n## input.json\n{role_input}\n\n"
                  f"## bundle.json (truncated)\n{bundle_text}\n\n"
                  f"## repair problems from last attempt\n"
                  f"{json.dumps(order.get('repair_problems'), ensure_ascii=False)}\n\n"
                  "Respond with ONLY the JSON object required by the contract.")
        out = self._post({"model": self.model_id,
                          "max_tokens": self.max_tokens,
                          "messages": [{"role": "user", "content": prompt}]})
        text = "".join(b.get("text", "") for b in out.get("content", []))
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("model returned no JSON object")
        path = Path(order["expected_output"])
        path.write_text(text[start:end + 1])
        usage = out.get("usage", {})
        order["tokens_in"] = usage.get("input_tokens")
        order["tokens_out"] = usage.get("output_tokens")
        return str(path)


ADAPTERS = {"manual": ManualAgentAdapter, "anthropic": AnthropicAdapter}


def orchestrate(conn: sqlite3.Connection, cfg, case_id: str, adapter,
                max_steps: int = 20) -> dict:
    """Advance one case as far as possible: deterministic steps run inline;
    role steps go through the adapter; every import is validated; rejected
    imports create repair orders (<= MAX_REPAIRS) with the exact problems."""
    log: list[dict] = []
    for _step in range(max_steps):
        case = conn.execute("SELECT * FROM research_case WHERE case_id=?",
                            (case_id,)).fetchone()
        if case is None:
            return {"status": "ERROR", "log": log,
                    "error": f"no case {case_id}"}
        state = case["state"]
        if state in research.FINAL_STATES:
            return {"status": "FINAL", "state": state, "log": log}
        if state == "EVIDENCE_SEARCH":
            quantpack.build_quantpack(conn, cfg, case_id)
            fb = research.freeze_bundle(conn, case_id)
            log.append({"step": "freeze_bundle", "version": fb.get("version")})
            continue
        role = _STATE_ROLE.get(state)
        if role is None:
            return {"status": "ERROR", "state": state, "log": log,
                    "error": f"no role mapped for state {state}"}
        order = _find_open_order(case_id, role) or _create_order(
            conn, case_id, role, None, attempts=0)
        try:
            out_path = adapter.run(order)
        except Exception as exc:
            _finish_order(order, "FAILED", repr(exc))
            return {"status": "ADAPTER_ERROR", "state": state, "log": log,
                    "error": repr(exc), "order": order["order_id"]}
        if out_path is None:
            return {"status": "WAITING_AGENT", "state": state, "log": log,
                    "order": order["order_id"],
                    "expected_output": order["expected_output"],
                    "role_dir": order.get("role_dir"),
                    "instructions": order["instructions"]}
        result = research.import_role_output(
            conn, cfg, case_id, role, out_path,
            model_id=getattr(adapter, "model_id", "?"),
            provider=getattr(adapter, "provider", "?"),
            runtime=getattr(adapter, "runtime", "?"),
            tokens_in=order.get("tokens_in"),
            tokens_out=order.get("tokens_out"))
        log.append({"step": role, "status": result.get("status"),
                    "problems": (result.get("problems") or [])[:4]})
        if result.get("status") == "IMPORTED":
            _finish_order(order, "DONE")
            continue
        attempts = order["attempts"] + 1
        _finish_order(order, "REJECTED", f"attempt {attempts}")
        if attempts > MAX_REPAIRS:
            return {"status": "FAILED_VALIDATION", "state": state, "log": log,
                    "problems": result.get("problems")}
        repair = _create_order(conn, case_id, role,
                               result.get("problems"), attempts)
        return {"status": "REPAIR_REQUESTED", "state": state, "log": log,
                "order": repair["order_id"],
                "expected_output": repair["expected_output"],
                "problems": result.get("problems")}
    return {"status": "STEP_LIMIT", "log": log}


def orders_status(case_id: str | None = None) -> list[dict]:
    out = []
    for p in sorted(workqueue_dir().glob("order_*.json")):
        if "_output" in p.stem:
            continue
        d = json.loads(p.read_text())
        if case_id and d["case_id"] != case_id:
            continue
        out.append({k: d.get(k) for k in ("order_id", "case_id", "role",
                                          "status", "attempts",
                                          "created_at_utc")})
    return out
