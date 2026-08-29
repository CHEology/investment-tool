"""One-off, idempotent correction pass for the 2026-08-28 US trial run
(PR-3' honesty corrections; approved review F1/F2/F3).

What it does — and what it deliberately does not:
- Relabels candidate rows whose state misused US_TRIAL_INSUFFICIENT_DATA for
  budget exhaustion (gate=TRIGGERED with OK prices) to
  US_TRIAL_RESEARCH_PENDING, annotating content_state=BUDGET_DEFERRED.
- Relabels US_TRIAL_CANDIDATE rows to US_TRIAL_LEAD (leads, not validated
  opportunities).
- Freezes a v2 corrected card for every lead (v1 stays on disk untouched;
  frozen_artifact marks it SUPERSEDED with a note — the designed lifecycle).
- Writes data/audit/us_trial_2026-08-28_correction.json documenting every
  change. The original us_trial_2026-08-28.json audit is NEVER modified.

Idempotent: rerunning changes nothing once states are relabeled and a v2+
card exists per lead.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_tool import cards  # noqa: E402
from investment_tool.db import DEFAULT_DATA_DIR, connect  # noqa: E402
from investment_tool.lineage import utc_now  # noqa: E402

RUN_ASOF = "2026-08-28"

GENERIC_NOTE = (
    "v1 状态『试验候选（可能过度反应）』更正为『试验线索』；v1 自动打印的"
    "『倾向暂时性/有界（初步）』结论撤回（无证据支撑，审读 F3）；"
    "cum5/slow21/slow63 触发腿为截至评估日的回溯窗口而非事件锚定（审读 F1），"
    "事件锚定引擎（PR-B）落地后将按新引擎重评。"
)

TICKER_NOTES: dict[str, list[str]] = {
    "ABVC": ["v1 定性不成立：事件日 +11.95%、事件后累计 +16.74%，触发腿 slow21/slow63 "
             "全部来自事件前回撤（−26.9%/−43.1%）。该线索的价格触发依据无效（审读 F1），"
             "保留仅因关键词路由，待 PR-B 引擎重评。"],
    "OTLK": ["v1 触发腿 slow21 为事件前回撤（−41.7%），事件日 +0.61%。"
             "价格触发依据无效（审读 F1）。"],
    "SKIL": ["v1 触发腿 cum5 为回溯窗口；事件后累计 +1.02%。价格触发依据存疑（审读 F1）。"],
    "KTCC": ["事件日 +3.22%，大幅下跌发生于事件次日（post_cum −26.0%）；"
             "回溯窗口腿与事件归因混淆（审读 F1），因果归属未定。"],
    "HQY": ["定价判定：UNRESOLVED（未决）。一手 8-K（accession 0001428336-26-000040，"
            "已缓存并 manifest）证实 Q2 FY27 营收超共识且上调 FY27 指引"
            "（营收 $1.411–1.421B、Non-GAAP EPS $4.66–4.73）；系统存储事件日收盘对收盘 "
            "−10.56%（EODHD 抽验一致），盘中口径约 −13.6%（二手报道，口径不同须并列）。",
            "备择解释未排除：托管收益率指引收窄至 3.85–3.9%、未给 FY28 展望"
            "（Yahoo Finance 电话会纪要，二手）；系统无共识/估值/仓位数据，"
            "无法区分『过度反应』与『预期重置』。在新证据出现前不得升级该标签。"],
}


def main() -> int:
    conn = connect()
    audit: dict = {"run_asof": RUN_ASOF, "generated_at": utc_now(),
                   "original_audit_untouched": f"us_trial_{RUN_ASOF}.json",
                   "relabeled_research_pending": [], "relabeled_leads": [],
                   "v2_cards": [], "skipped_existing_v2": []}

    # 1) budget-exhausted rows mislabeled as insufficient data
    rows = conn.execute(
        "SELECT candidate_id, profile_json FROM candidate"
        " WHERE state='US_TRIAL_INSUFFICIENT_DATA'"
        " AND json_extract(profile_json,'$.gate')='TRIGGERED'"
        " AND json_extract(profile_json,'$.reaction.state')='OK'"
    ).fetchall()
    for r in rows:
        p = json.loads(r["profile_json"])
        p["content_state"] = "BUDGET_DEFERRED"
        p["correction"] = ("state relabeled from US_TRIAL_INSUFFICIENT_DATA"
                           " (budget exhaustion, not missing data); review F2")
        conn.execute(
            "UPDATE candidate SET state='US_TRIAL_RESEARCH_PENDING', profile_json=?"
            " WHERE candidate_id=?",
            (json.dumps(p, ensure_ascii=False), r["candidate_id"]))
        audit["relabeled_research_pending"].append(
            {"candidate_id": r["candidate_id"], "ticker": p.get("ticker")})

    # 2) candidates -> leads
    for r in conn.execute(
        "SELECT candidate_id, profile_json FROM candidate"
        " WHERE state='US_TRIAL_CANDIDATE'"
    ).fetchall():
        p = json.loads(r["profile_json"])
        conn.execute("UPDATE candidate SET state='US_TRIAL_LEAD' WHERE candidate_id=?",
                     (r["candidate_id"],))
        audit["relabeled_leads"].append(
            {"candidate_id": r["candidate_id"], "ticker": p.get("ticker")})
    conn.commit()

    # 3) v2 corrected cards for every lead (skip if a v2+ already exists)
    leads = conn.execute(
        "SELECT * FROM candidate WHERE state='US_TRIAL_LEAD'").fetchall()
    for row in leads:
        maxv = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM frozen_artifact"
            " WHERE candidate_id=? AND kind='CARD'", (row["candidate_id"],),
        ).fetchone()["v"]
        if maxv >= 2:
            audit["skipped_existing_v2"].append(row["candidate_id"])
            continue
        p = json.loads(row["profile_json"])
        notes = [GENERIC_NOTE] + TICKER_NOTES.get(p.get("ticker", ""), [])
        content = cards.render_us_card_zh(conn, row, correction_note=notes)
        frozen = cards.freeze_card(conn, row, content)
        audit["v2_cards"].append({"candidate_id": row["candidate_id"],
                                  "ticker": p.get("ticker"),
                                  "artifact_id": frozen["artifact_id"],
                                  "sha256": frozen["sha256"]})

    out = DEFAULT_DATA_DIR / "audit" / f"us_trial_{RUN_ASOF}_correction.json"
    out.write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    print(json.dumps({"relabeled_research_pending": len(audit["relabeled_research_pending"]),
                      "relabeled_leads": len(audit["relabeled_leads"]),
                      "v2_cards": len(audit["v2_cards"]),
                      "skipped_existing_v2": len(audit["skipped_existing_v2"]),
                      "audit": str(out)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
