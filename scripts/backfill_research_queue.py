"""H0/F16: enqueue the pending candidates that predate the research_queue.

The 2026-08-28 correction pass relabeled 35 budget-deferred candidates to
US_TRIAL_RESEARCH_PENDING but the queue table arrived later (PR-A), so
`invest research-queue --process` had nothing to resume. This idempotent
backfill enqueues every US_TRIAL_RESEARCH_PENDING / US_TRIAL_FETCH_FAILED
candidate with its stored rank (recomputing us_rank_v0 from the frozen
reaction profile when the profile predates ranking), and writes an audit
artifact. Protected queue states are never downgraded (enqueue semantics).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_tool import ranking, us_queue  # noqa: E402
from investment_tool.db import DEFAULT_DATA_DIR, connect  # noqa: E402
from investment_tool.lineage import utc_now  # noqa: E402

STATE_MAP = {"US_TRIAL_RESEARCH_PENDING": "RESEARCH_PENDING",
             "US_TRIAL_FETCH_FAILED": "FETCH_FAILED"}


def main() -> int:
    conn = connect()
    audit = {"generated_at": utc_now(), "enqueued": [], "skipped_no_listing": []}
    rows = conn.execute(
        "SELECT candidate_id, company_id, state, profile_json, config_version"
        " FROM candidate WHERE state IN ('US_TRIAL_RESEARCH_PENDING',"
        " 'US_TRIAL_FETCH_FAILED')").fetchall()
    for r in rows:
        p = json.loads(r["profile_json"])
        listing = conn.execute(
            "SELECT listing_id FROM listing WHERE company_id=?"
            " AND exchange IN ('NASDAQ','NYSE','AMEX') ORDER BY listing_id LIMIT 1",
            (r["company_id"],)).fetchone()
        if listing is None:
            audit["skipped_no_listing"].append(r["candidate_id"])
            continue
        rank = p.get("rank")
        if not rank:
            rank = ranking.score_event(p.get("reaction", {}),
                                       p.get("trigger_legs", []))
        qid = us_queue.enqueue(
            conn, event_id=p["event_id"], candidate_id=r["candidate_id"],
            company_id=r["company_id"], listing_id=listing["listing_id"],
            ticker=p.get("ticker"), asof=(p.get("reaction", {}).get("last_session")
                                          or "2026-08-28"),
            state=STATE_MAP[r["state"]], rank=rank,
            config_version=r["config_version"])
        audit["enqueued"].append({"queue_id": qid, "ticker": p.get("ticker"),
                                  "state": STATE_MAP[r["state"]],
                                  "rank_score": rank.get("score")})
    conn.commit()
    out = DEFAULT_DATA_DIR / "audit" / "research_queue_backfill.json"
    out.write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    print(json.dumps({"enqueued": len(audit["enqueued"]),
                      "skipped": len(audit["skipped_no_listing"]),
                      "audit": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
