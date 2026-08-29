"""C1 trial preparation: recompute the reaction profiles of the trial-case
candidates with the corrected event-anchored engine (us_trial_v0.3) at the
ORIGINAL asof (2026-08-28).

The stored profiles carry the v0 measurements whose trailing windows the
review falsified (F1). This is a methodology upgrade at the same information
cutoff, not new information: the old reaction is preserved under
`reaction_v0`, the engine version is recorded, and an audit artifact lists
every change. Idempotent."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_tool import reaction, us_trial  # noqa: E402
from investment_tool.db import DEFAULT_DATA_DIR, connect  # noqa: E402
from investment_tool.lineage import utc_now  # noqa: E402

ASOF = "2026-08-28"
TICKERS = ("HQY", "BURL", "HRL", "BBW")


def main() -> int:
    conn = connect()
    audit = {"generated_at": utc_now(), "asof": ASOF, "engine": "us_trial_v0.3",
             "refreshed": [], "skipped": []}
    for tk in TICKERS:
        row = conn.execute(
            "SELECT c.candidate_id, c.profile_json, l.listing_id FROM candidate c"
            " JOIN listing l ON l.company_id=c.company_id"
            " AND l.exchange IN ('NASDAQ','NYSE','AMEX')"
            " WHERE json_extract(c.profile_json,'$.ticker')=?"
            " AND c.state='US_TRIAL_LEAD' ORDER BY l.listing_id LIMIT 1",
            (tk,)).fetchone()
        if row is None:
            audit["skipped"].append({"ticker": tk, "reason": "no lead candidate"})
            continue
        p = json.loads(row["profile_json"])
        if p.get("reaction_engine") == "us_trial_v0.3":
            audit["skipped"].append({"ticker": tk, "reason": "already refreshed"})
            continue
        ev = {"accession": p.get("accession"),
              "accepted_at_utc": p.get("accepted_at_utc"),
              "filing_date": (p.get("accepted_at_utc") or "")[:10] or None,
              "published_at_utc": p.get("accepted_at_utc"),
              "first_seen_at_utc": p.get("first_seen_at_utc")}
        anchors = us_trial.event_anchors(ev)
        rx = reaction.compute_event_reaction(conn, row["listing_id"], anchors, ASOF)
        p["reaction_v0"] = p.get("reaction")
        p["reaction"] = rx
        p["reaction_engine"] = "us_trial_v0.3"
        conn.execute("UPDATE candidate SET profile_json=? WHERE candidate_id=?",
                     (json.dumps(p, ensure_ascii=False, default=str),
                      row["candidate_id"]))
        audit["refreshed"].append({
            "ticker": tk, "candidate_id": row["candidate_id"],
            "event_session": anchors.get("event_session"),
            "first_actionable_session": anchors.get("first_actionable_session"),
            "mkt_adj_post_ret1": rx.get("mkt_adj_post_ret1"),
            "mkt_adj_car5": rx.get("mkt_adj_car5"),
            "mkt_adj_run_up_21": rx.get("mkt_adj_run_up_21")})
    conn.commit()
    out = DEFAULT_DATA_DIR / "audit" / "c1_reaction_refresh.json"
    out.write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
