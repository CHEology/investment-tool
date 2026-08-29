"""Provider-neutral research runner (H4/F-N): the application drives the
role sequence; an Agent runtime fills work orders.

Capability statement: with the ManualAgentAdapter this is
SEMI-AUTOMATIC — the application owns sequencing, deterministic steps
(quantpack, bundle freeze), validation, repair requests, and resumable
state; the model side is any coding agent (Claude Code, Codex, a human) that
consumes `data/research/workqueue/` orders and writes the output file. The
AnthropicAdapter makes the same loop application-invoked when an API key is
configured, but has no web tools. CodexCLIAdapter starts a fresh Codex Agent
context for every role; the search role can use Codex Web Search and must
capture every cited page through the local evidence gateway. No adapter can
bypass import validation.

Resumability: orchestrate() derives everything from research_case.state and
the on-disk order files — interrupt anywhere, run again, it continues.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

from investment_tool import quantpack, research
from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

MAX_REPAIRS = 2
PROJECT_ROOT = Path(__file__).resolve().parents[2]

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


def _save_order(order: dict) -> None:
    _order_path(order["order_id"]).write_text(
        json.dumps(order, ensure_ascii=False, indent=2))


def _find_open_order(case_id: str, role: str,
                     pair_id: str | None = None,
                     bundle_version: int | None = None) -> dict | None:
    for p in sorted(workqueue_dir().glob("order_*.json")):
        if "_output" in p.stem:
            continue
        d = json.loads(p.read_text())
        if d["case_id"] == case_id and d["role"] == role \
                and d["status"] in ("PENDING", "CLAIMED") \
                and (pair_id is None or d.get("pair_id") == pair_id) \
                and (bundle_version is None or
                     d.get("bundle_version") == bundle_version):
            return d
    return None


def _find_pair_order(case_id: str, role: str, pair_id: str) -> dict | None:
    for p in sorted(workqueue_dir().glob("order_*.json")):
        if "_output" in p.stem:
            continue
        d = json.loads(p.read_text())
        if (d["case_id"] == case_id and d["role"] == role and
                d.get("pair_id") == pair_id):
            return d
    return None


def _create_order(conn, case_id: str, role: str, problems: list[str] | None,
                  attempts: int, *, pair_id: str | None = None,
                  agent_instance_id: str | None = None) -> dict:
    view = research.export_role_view(conn, case_id, role)
    order_id = f"order_{uuid.uuid4().hex[:12]}"
    case = conn.execute("SELECT bundle_version FROM research_case WHERE case_id=?",
                        (case_id,)).fetchone()
    bundle_version = case["bundle_version"] if case else 0
    source_contract = Path(view.get("contract") or "")
    source_input = Path(view.get("role_dir") or "") / "input.json"
    contract_bytes = (source_contract.read_bytes()
                      if source_contract.is_file() else b"")
    input_bytes = source_input.read_bytes() if source_input.is_file() else b""
    snapshot_dir = workqueue_dir() / f"{order_id}_input"
    snapshot_dir.mkdir(parents=True, exist_ok=False)
    snapshot_contract = snapshot_dir / "contract.md"
    snapshot_input = snapshot_dir / "input.json"
    snapshot_contract.write_bytes(contract_bytes)
    snapshot_input.write_bytes(input_bytes)
    bundle_sha_row = conn.execute(
        "SELECT content_sha256 FROM evidence_bundle WHERE case_id=? AND version=?",
        (case_id, bundle_version)).fetchone()
    bundle_sha = bundle_sha_row["content_sha256"] if bundle_sha_row else None
    repair_bytes = json.dumps(problems or [], ensure_ascii=False,
                              sort_keys=True).encode()
    role_input_sha = hashlib.sha256(
        contract_bytes + b"\0" + input_bytes + b"\0" +
        (bundle_sha or "").encode() + b"\0" + repair_bytes).hexdigest()
    if agent_instance_id is None:
        slot = f"{pair_id or case_id}:{role}"
        agent_instance_id = f"agent_{uuid.uuid5(uuid.NAMESPACE_URL, slot).hex[:12]}"
    visible_roles = ({"rebuttal": ["adversarial"],
                      "adjudicator": ["constructive", "adversarial",
                                      "rebuttal", "semantic_review"]}.get(role, []))
    context_requirement = (
        " For a manual/external analyst, also write the real task/thread/session"
        " ID into this order's context_id and set context_provenance to"
        " CALLER_DECLARED; a role-slot placeholder is not accepted."
        if role in ("constructive", "adversarial") else "")
    order = {
        "order_id": order_id, "case_id": case_id, "role": role,
        "status": "PENDING", "attempts": attempts,
        "created_at_utc": utc_now(),
        "bundle_version": bundle_version, "bundle_sha256": bundle_sha,
        "pair_id": pair_id, "agent_instance_id": agent_instance_id,
        "analysis_lane": ("analyst_a" if role in ("constructive", "rebuttal")
                          else "analyst_b" if role == "adversarial"
                          else role),
        "role_input_sha256": role_input_sha,
        "input_manifest_verified": True,
        "visible_roles": visible_roles,
        "source_role_dir": view.get("role_dir"),
        "role_dir": str(snapshot_dir),
        "contract": str(snapshot_contract),
        "bundle": view.get("bundle"),
        "expected_output": str(workqueue_dir()
                               / f"{order_id}_{role}_output.json"),
        "repair_problems": problems or [],
        "instructions": (
            f"Read the frozen contract + input in {snapshot_dir} (and the bundle"
            f" it references), perform the {role} role, and write the"
            " structured JSON output to expected_output. Open-world search"
            " goes through `invest research fetch`. Repair problems, if any,"
            " list exactly what the validator rejected last attempt."
            + context_requirement),
    }
    _save_order(order)
    return order


def _verify_order_input(order: dict) -> bool:
    try:
        contract = Path(order["contract"]).read_bytes()
        role_input = (Path(order["role_dir"]) / "input.json").read_bytes()
    except (OSError, KeyError, TypeError):
        return False
    bundle_sha = order.get("bundle_sha256") or ""
    if order.get("bundle"):
        try:
            bundle_bytes = Path(order["bundle"]).read_bytes()
        except OSError:
            return False
        actual_bundle_sha = hashlib.sha256(bundle_bytes).hexdigest()
        if actual_bundle_sha != bundle_sha:
            return False
    repair_bytes = json.dumps(order.get("repair_problems") or [],
                              ensure_ascii=False, sort_keys=True).encode()
    actual = hashlib.sha256(
        contract + b"\0" + role_input + b"\0" + bundle_sha.encode() +
        b"\0" + repair_bytes).hexdigest()
    return actual == order.get("role_input_sha256")


def _has_completed_web_search(jsonl: str) -> bool:
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        action = item.get("action") or {}
        if (event.get("type") == "item.completed" and
                item.get("type") == "web_search" and
                action.get("type") == "search"):
            return True
    return False


def _ensure_blind_pair(conn, case_id: str) -> dict:
    """Freeze both analyst work orders before either analyst output exists.

    Calls may execute sequentially. Independence is defined by blind inputs
    plus distinct Agent contexts, not by provider or model diversity.
    """
    case = conn.execute("SELECT bundle_version, loop_count FROM research_case"
                        " WHERE case_id=?", (case_id,)).fetchone()
    bundle = conn.execute(
        "SELECT content_sha256 FROM evidence_bundle WHERE case_id=? AND version=?",
        (case_id, case["bundle_version"])).fetchone()
    if bundle is None:
        raise RuntimeError("cannot create analyst pair without frozen bundle")
    pair_id = "pair_" + uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"{case_id}:{case['bundle_version']}:{case['loop_count']}").hex[:12]
    conn.execute(
        "INSERT OR IGNORE INTO analysis_pair(pair_id, case_id, bundle_version,"
        " bundle_sha256, loop_index, status, created_at_utc)"
        " VALUES(?,?,?,?,?,'OPEN',?)",
        (pair_id, case_id, case["bundle_version"], bundle["content_sha256"],
         case["loop_count"], utc_now()))
    for role in ("constructive", "adversarial"):
        if _find_pair_order(case_id, role, pair_id) is None:
            _create_order(conn, case_id, role, None, 0, pair_id=pair_id)
    conn.commit()
    return {"pair_id": pair_id, "bundle_version": case["bundle_version"],
            "bundle_sha256": bundle["content_sha256"]}


def _current_pair(conn, case_id: str) -> dict | None:
    case = conn.execute("SELECT bundle_version, loop_count FROM research_case"
                        " WHERE case_id=?", (case_id,)).fetchone()
    row = conn.execute(
        "SELECT * FROM analysis_pair WHERE case_id=? AND bundle_version=?"
        " AND loop_index=? ORDER BY created_at_utc DESC LIMIT 1",
        (case_id, case["bundle_version"], case["loop_count"])).fetchone()
    if row and row["status"] != "SUPERSEDED":
        roles = {r["role"] for r in conn.execute(
            "SELECT role FROM agent_run WHERE pair_id=? AND status='IMPORTED'"
            " AND role IN ('constructive','adversarial')", (row["pair_id"],))}
        if roles == {"constructive", "adversarial"} and row["status"] != "COMPLETE":
            conn.execute("UPDATE analysis_pair SET status='COMPLETE' WHERE pair_id=?",
                         (row["pair_id"],))
            conn.commit()
            row = conn.execute("SELECT * FROM analysis_pair WHERE pair_id=?",
                               (row["pair_id"],)).fetchone()
    return dict(row) if row else None


def _finish_order(order: dict, status: str, note: str | None = None) -> None:
    order["status"] = status
    order["finished_at_utc"] = utc_now()
    if note:
        order["note"] = note
    _save_order(order)


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
        if out.get("id"):
            order["context_id"] = out["id"]
            order["context_provenance"] = "RUNTIME_VERIFIED"
        _save_order(order)
        return str(path)


class CodexCLIAdapter:
    """Run one fresh Codex Agent per work order using the signed-in CLI.

    The search Agent may browse broadly with Codex Web Search. A web result is
    only a lead: any source used in the returned claims must first be captured
    by ``invest research fetch``, after which the normal importer verifies its
    evidence ID, quote, and timestamp. All later roles are explicitly offline
    and constrained to the frozen bundle.
    """

    name = "codex"
    provider = "openai"
    runtime = "codex-cli-ephemeral"

    def __init__(self, model_id: str = "gpt-5.6-sol", *,
                 reasoning_effort: str = "medium", timeout_s: int = 900,
                 command_runner=None, codex_path: str | None = None):
        self.model_id = model_id
        self.reasoning_effort = reasoning_effort
        self.timeout_s = timeout_s
        self._run = command_runner or self._default_run
        self.codex_path = (codex_path or shutil.which("codex") or
                           "/Applications/ChatGPT.app/Contents/Resources/codex")

    @staticmethod
    def _default_run(command: list[str], prompt: str, timeout_s: int):
        return subprocess.run(command, input=prompt, text=True,
                              capture_output=True, timeout=timeout_s,
                              check=False)

    def _prompt(self, order: dict) -> str:
        base = (
            "You are one isolated research Agent. This invocation is a fresh "
            "context; do not infer or seek another analyst's conclusions. "
            f"Your role is {order['role']}. Read the exact contract at "
            f"{order.get('contract')} and input.json in {order.get('role_dir')}. "
            f"The frozen bundle is {order.get('bundle')}. "
            "Do not edit application source code, configuration, or tests. "
        )
        if order["role"] == "search":
            capture = (shutil.which("invest") or
                       str(PROJECT_ROOT / ".venv" / "bin" / "invest"))
            data_env = shlex.quote(str(DEFAULT_DATA_DIR.resolve()))
            capture_cmd = shlex.quote(str(capture))
            base += (
                "Use Codex Web Search autonomously and iteratively. Follow "
                "important leads beyond any suggested source list; search for "
                "both supporting evidence and hidden adverse explanations. "
                "A search result or snippet is discovery only. Before citing "
                "a page, capture it with this base command: "
                f"INVESTMENT_TOOL_DATA_DIR={data_env} {capture_cmd} research "
                f"fetch '<URL>' --case {order['case_id']}\n"
                "Optional flags are --published-at, --title, --source-class, "
                "and --note. Read the returned content_path and "
                "quote the stored page exactly. Every source_id in your final "
                "answer must be an evd_* ID returned by that command (or an "
                "eligible filing:* source already supplied by the contract). "
                "Use --published-at only when the publication time is visible "
                "in the source; otherwise omit it rather than guessing. "
                "Do not cite uncaptured URLs as evidence. If a page is blocked, "
                "find another primary or reputable source or mark that channel "
                "BLOCKED/PARTIAL honestly. You may choose any public sources; "
                "the listed coverage channels are a floor, not a whitelist. "
            )
        else:
            base += (
                "Do not browse the web and do not use sources outside the "
                "frozen bundle. This preserves the blind, point-in-time review. "
            )
        if order.get("repair_problems"):
            base += ("The previous output was rejected for these exact reasons: "
                     + json.dumps(order["repair_problems"], ensure_ascii=False)
                     + ". Correct them without relaxing the contract. ")
        return base + "Return only the single JSON object required by the contract."

    def run(self, order: dict) -> str | None:
        codex = Path(self.codex_path)
        if not codex.exists():
            raise RuntimeError("Codex CLI not found; install/open Codex or pass"
                               " --adapter manual")
        output = Path(order["expected_output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        role_dir = Path(order.get("role_dir") or PROJECT_ROOT)
        command = [
            str(codex), "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "workspace-write", "--color", "never",
            "--model", self.model_id,
            "--config", f'model_reasoning_effort="{self.reasoning_effort}"',
        ]
        if order["role"] == "search":
            command += ["--config", "sandbox_workspace_write.network_access=true",
                        "--config", 'web_search="live"']
        else:
            command += ["--config", "sandbox_workspace_write.network_access=false",
                        "--config", 'web_search="disabled"']
        command += [
            "--json", "--cd", str(role_dir), "--add-dir",
            str(DEFAULT_DATA_DIR.resolve()), "--output-last-message", str(output), "-",
        ]
        result = self._run(command, self._prompt(order), self.timeout_s)
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
        transcript = "\n".join((stdout, stderr))
        match = re.search(r"session id:\s*([0-9a-f-]{20,})", transcript,
                          flags=re.IGNORECASE)
        if match is None:
            match = re.search(r'"thread_id"\s*:\s*"([0-9a-f-]{20,})"',
                              transcript, flags=re.IGNORECASE)
        order["context_id"] = (match.group(1) if match else
                               f"codex_exec_{uuid.uuid4().hex}")
        order["context_provenance"] = (
            "RUNTIME_VERIFIED" if match else "FRESH_PROCESS_VERIFIED")
        order["adapter_exit_code"] = getattr(result, "returncode", None)
        trace_path = workqueue_dir() / f"{order['order_id']}_trace.jsonl"
        trace_path.write_text(stdout)
        order["trace_path"] = str(trace_path)
        order["trace_sha256"] = hashlib.sha256(stdout.encode()).hexdigest()
        if stderr:
            stderr_path = workqueue_dir() / f"{order['order_id']}_stderr.txt"
            stderr_path.write_text(stderr)
            order["stderr_path"] = str(stderr_path)
            order["stderr_sha256"] = hashlib.sha256(stderr.encode()).hexdigest()
        _save_order(order)
        if getattr(result, "returncode", 0) != 0:
            tail = transcript[-2000:].replace("\n", " ")
            raise RuntimeError(f"Codex Agent failed: {tail}")
        if not output.exists():
            raise RuntimeError("Codex Agent returned no output file")
        raw = output.read_text().strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("Codex Agent returned no JSON object")
        final_json = raw[start:end + 1]
        try:
            doc = json.loads(final_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Codex Agent returned invalid JSON: {exc}") from exc
        if (order["role"] == "search" and doc.get("search_state") == "COMPLETE"
                and not _has_completed_web_search(stdout)):
            raise RuntimeError("search claimed COMPLETE without a Web Search trace")
        output.write_text(final_json)
        return str(output)


ADAPTERS = {"manual": ManualAgentAdapter, "anthropic": AnthropicAdapter,
            "codex": CodexCLIAdapter}


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
        pair = None
        if role in ("constructive", "adversarial"):
            pair = _ensure_blind_pair(conn, case_id)
        elif role in ("rebuttal", "semantic_review", "adjudicator"):
            pair = _current_pair(conn, case_id)
        pair_id = pair["pair_id"] if pair else None
        order = _find_open_order(
            case_id, role, pair_id, case["bundle_version"]) or _create_order(
                conn, case_id, role, None, attempts=0, pair_id=pair_id)
        if not _verify_order_input(order):
            _finish_order(order, "FAILED", "frozen order input hash mismatch")
            return {"status": "ORDER_INPUT_CHANGED", "state": state, "log": log,
                    "error": "frozen work-order input changed",
                    "order": order["order_id"]}
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
        if not _verify_order_input(order):
            _finish_order(order, "FAILED", "frozen order input changed during run")
            return {"status": "ORDER_INPUT_CHANGED", "state": state, "log": log,
                    "error": "frozen work-order input changed during run",
                    "order": order["order_id"]}
        context_id = order.get("context_id")
        context_provenance = order.get("context_provenance") or "CONTEXT_UNKNOWN"
        result = research.import_role_output(
            conn, cfg, case_id, role, out_path,
            model_id=getattr(adapter, "model_id", "?"),
            provider=getattr(adapter, "provider", "?"),
            runtime=getattr(adapter, "runtime", "?"),
            tokens_in=order.get("tokens_in"),
            tokens_out=order.get("tokens_out"),
            order_id=order["order_id"], pair_id=pair_id,
            bundle_version=order.get("bundle_version"),
            agent_instance_id=order.get("agent_instance_id"),
            context_id=context_id,
            context_provenance=context_provenance,
            role_input_sha256=order.get("role_input_sha256"),
            input_manifest_verified=True,
            visible_roles=order.get("visible_roles"),
            output_path=out_path)
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
        repair = _create_order(
            conn, case_id, role, result.get("problems"), attempts,
            pair_id=pair_id, agent_instance_id=order.get("agent_instance_id"))
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
                                          "status", "attempts", "pair_id",
                                          "agent_instance_id", "context_id",
                                          "context_provenance",
                                          "created_at_utc")})
    return out
