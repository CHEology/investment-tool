"""PR-G operational drills — explicitly FIXTURE-BASED, clearly labeled.

Both drills run the real pipeline code against the offline fixture corpus in
an isolated temporary data directory (never the live database), then write a
labeled artifact into data/audit/soak/ so `invest soak-report` can count them:

1. amendment drill: 8-K -> 8-K/A must link LINKED_UNIQUE, mark the original
   AMENDED_BY, and supersede its evidence — the drill the DESIGN live gate
   requires if no amendment occurs naturally in the soak window.
2. crash-recovery drill: a partial run (lossy getcurrent only, "crash" before
   the evening index) followed by a fresh full run must converge to the
   complete state while preserving the earliest first_seen_at_utc.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from investment_tool import db as db_mod  # noqa: E402
from investment_tool import us_cli, us_ingest, us_route  # noqa: E402
from investment_tool.db import DEFAULT_DATA_DIR  # noqa: E402
from investment_tool.lineage import utc_now  # noqa: E402

FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sec"


def _temp_conn(tmp: str):
    return db_mod.connect(Path(tmp))


def amendment_drill() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        conn = _temp_conn(tmp)
        us_ingest.ingest_daily_index(conn, (FIX / "master_sample.idx").read_bytes(),
                                     "2026-08-27", "m_drill")
        us_ingest.enrich_items_from_efts(conn, (FIX / "efts_8k_sample.json").read_bytes())
        us_ingest.enrich_from_submissions(
            conn, (FIX / "submissions_sample.json").read_bytes())
        us_route.route_unclassified(conn)
        hist = us_route.link_amendments(conn)
        row = conn.execute(
            "SELECT amends_accession, amend_link_state FROM sec_filing"
            " WHERE accession='0001000001-26-000103'").fetchone()
        orig = conn.execute(
            "SELECT supersession_state FROM sec_filing"
            " WHERE accession='0001000001-26-000101'").fetchone()
        passed = (hist.get("LINKED_UNIQUE", 0) >= 1
                  and row is not None
                  and row["amends_accession"] == "0001000001-26-000101"
                  and orig["supersession_state"] == "AMENDED_BY")
        conn.close()
    return {"kind": "AMENDMENT_DRILL", "basis": "FIXTURE (offline corpus)",
            "generated_at": utc_now(), "link_histogram": hist, "passed": passed}


def recovery_drill() -> dict:
    from investment_tool import config as config_mod

    cfg = config_mod.load("v0.2")
    real_dir = us_cli.DEFAULT_DATA_DIR
    with tempfile.TemporaryDirectory() as tmp:
        # the drill's own us_sync audits go to the temp dir — the real
        # data/audit/us_sync_*.json history is never touched
        us_cli.DEFAULT_DATA_DIR = Path(tmp)
        try:
            conn = _temp_conn(tmp)
            # phase 1: lossy freshness channel only, then a simulated crash
            # (no evening index yet)
            a1 = us_cli.run_us_sync(conn, cfg, "2026-08-27", None, None, [],
                                    str(FIX / "getcurrent_sample.atom"))
            early = {r["accession"]: r["first_seen_at_utc"] for r in conn.execute(
                "SELECT accession, first_seen_at_utc FROM sec_filing")}
            partial_state = a1["us_completeness"]
            # phase 2: fresh full run after the "crash"
            a2 = us_cli.run_us_sync(conn, cfg, "2026-08-27",
                                    str(FIX / "master_sample.idx"),
                                    str(FIX / "efts_8k_sample.json"),
                                    [str(FIX / "submissions_sample.json")], None)
            preserved = all(
                conn.execute(
                    "SELECT first_seen_at_utc FROM sec_filing WHERE accession=?",
                    (acc,)).fetchone()["first_seen_at_utc"] == seen
                for acc, seen in early.items())
            n = conn.execute("SELECT COUNT(*) FROM sec_filing").fetchone()[0]
            passed = (partial_state == "PENDING_EVENING_INDEX"
                      and "INDEX_RECONCILED" in str(a2["us_completeness"])
                      and preserved and n > len(early) > 0)
            conn.close()
        finally:
            us_cli.DEFAULT_DATA_DIR = real_dir
    return {"kind": "RECOVERY_DRILL", "basis": "FIXTURE (offline corpus)",
            "generated_at": utc_now(),
            "partial_completeness": partial_state,
            "final_completeness": a2["us_completeness"],
            "earliest_first_seen_preserved": preserved,
            "filings_partial_vs_final": [len(early), n], "passed": passed}


def main() -> int:
    out_dir = DEFAULT_DATA_DIR / "audit" / "soak"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().replace(":", "").replace("-", "")
    results = []
    for name, fn in (("amendment_drill", amendment_drill),
                     ("recovery_drill", recovery_drill)):
        res = fn()
        path = out_dir / f"{name}_{stamp}.json"
        path.write_text(json.dumps(res, ensure_ascii=False, indent=2))
        results.append({name: res["passed"], "artifact": str(path)})
    print(json.dumps(results, indent=2))
    return 0 if all(list(r.values())[0] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
