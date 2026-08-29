"""PR-B replay: re-evaluate the review's falsified 2026-08-28 cases (ABVC,
OTLK, SKIL, KTCC) with the event-anchored engine and report old-vs-new.

Reads the stored candidate profiles (old gates/legs under us_trial_v0) and
the stored price series; computes the new dual-anchor reaction and the
us_trial_v0.2 gates. Writes an audit artifact
data/audit/us_trial_2026-08-28_pr_b_replay.json and prints the comparison.
No candidate rows, cards, or historical audits are modified.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_tool import config as config_mod  # noqa: E402
from investment_tool import reaction, us_trial  # noqa: E402
from investment_tool.db import DEFAULT_DATA_DIR, connect  # noqa: E402
from investment_tool.lineage import utc_now  # noqa: E402

ASOF = "2026-08-28"
TICKERS = ("ABVC", "OTLK", "SKIL", "KTCC")


def main() -> int:
    conn = connect()
    tcfg = config_mod.load("us_trial_v0.2")
    out: dict = {"asof": ASOF, "generated_at": utc_now(),
                 "old_config": "us_trial_v0", "new_config": tcfg.id,
                 "note": "read-only replay; no states, cards or audits modified",
                 "cases": []}
    rows = conn.execute(
        """
        SELECT c.candidate_id, c.state,
               c.profile_json, e.event_id, e.type, e.published_at_utc,
               e.first_seen_at_utc, l.listing_id,
               f.accession, f.accepted_at_utc, f.filing_date
        FROM candidate c
        JOIN event e ON e.event_id = json_extract(c.profile_json, '$.event_id')
        JOIN listing l ON l.company_id = c.company_id
             AND l.exchange IN ('NASDAQ','NYSE','AMEX')
        LEFT JOIN sec_filing f ON f.event_id = e.event_id
        WHERE json_extract(c.profile_json, '$.ticker') IN ({})
        """.format(",".join("?" * len(TICKERS))), TICKERS,
    ).fetchall()
    for r in rows:
        old = json.loads(r["profile_json"])
        ev = {"event_id": r["event_id"], "type": r["type"],
              "published_at_utc": r["published_at_utc"],
              "first_seen_at_utc": r["first_seen_at_utc"],
              "accession": r["accession"], "accepted_at_utc": r["accepted_at_utc"],
              "filing_date": r["filing_date"]}
        anchors = us_trial.event_anchors(ev)
        rx = reaction.compute_event_reaction(conn, r["listing_id"], anchors, ASOF)
        gate, hits = us_trial.evaluate_gates(rx, tcfg)
        out["cases"].append({
            "ticker": old.get("ticker"), "event_id": r["event_id"],
            "accession": r["accession"], "current_state": r["state"],
            "old": {"gate": old.get("gate"), "legs": old.get("trigger_legs"),
                    "mkt_adj_post_ret1": (old.get("reaction") or {}).get(
                        "mkt_adj_post_ret1"),
                    "mkt_adj_ret21_trailing": (old.get("reaction") or {}).get(
                        "mkt_adj_ret21")},
            "new": {"gate": gate, "legs": hits,
                    "event_session": anchors.get("event_session"),
                    "first_actionable_session": anchors.get(
                        "first_actionable_session"),
                    "mkt_adj_post_ret1": rx.get("mkt_adj_post_ret1"),
                    "mkt_adj_car5": rx.get("mkt_adj_car5"),
                    "mkt_adj_run_up_21": rx.get("mkt_adj_run_up_21"),
                    "realized_before_entry": rx.get("realized_before_entry"),
                    "post_state": rx.get("post_state")},
        })
    out_path = DEFAULT_DATA_DIR / "audit" / f"us_trial_{ASOF}_pr_b_replay.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    for c in out["cases"]:
        o, n = c["old"], c["new"]
        print(f"{c['ticker']:5s} {c['event_id'][:22]:22s} old={o['gate']}:{o['legs']}"
              f" -> new={n['gate']}:{n['legs']}"
              f"  evt1={n['mkt_adj_post_ret1']!r} car5={n['mkt_adj_car5']!r}"
              f" run21={n['mkt_adj_run_up_21']!r}")
    print(f"audit: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
