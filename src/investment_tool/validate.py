"""Forward-validation ledger (INV-10): point-in-time snapshots of how frozen
candidates evolved after freezing. The clock starts at first publication; a
candidate frozen today gets an explicit PENDING_FIRST_SESSION row, never a
silent absence.

Cohorts: every candidate whose LATEST frozen artifact is not INVALIDATED.
Invalidated artifacts are excluded from the ledger by rule (an invalidly
attributed replay is not a valid control observation).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pandas as pd

from investment_tool.db import DEFAULT_DATA_DIR
from investment_tool.lineage import utc_now

WINDOWS = (30, 90, 180, 365)


def _beijing_date(ts_utc: str) -> str:
    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def _ledger_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.candidate_id, c.company_id, c.state, fa.frozen_at_utc, fa.status
        FROM candidate c
        JOIN frozen_artifact fa ON fa.candidate_id = c.candidate_id
          AND fa.version = (SELECT MAX(version) FROM frozen_artifact
                            WHERE candidate_id = c.candidate_id)
        WHERE fa.status != 'INVALIDATED'
        """
    ).fetchall()


def _adj_series(conn, listing_id: str, start: str, end: str) -> pd.Series:
    df = pd.read_sql_query(
        "SELECT trade_date, adj_close, basis_epoch FROM security_day"
        " WHERE listing_id=? AND adj_close IS NOT NULL AND trade_date>=? AND trade_date<=?"
        " ORDER BY trade_date",
        conn, params=(listing_id, start, end),
    )
    if df.empty or df["basis_epoch"].nunique() > 1:
        return pd.Series(dtype=float)
    return pd.Series(
        pd.to_numeric(df["adj_close"], errors="coerce").values, index=df["trade_date"].values
    ).dropna()


def _cell_members(conn, listing_id: str) -> list[str]:
    row = conn.execute(
        "SELECT industry FROM market_snapshot WHERE listing_id=?"
        " ORDER BY asof_date DESC LIMIT 1", (listing_id,),
    ).fetchone()
    if row is None or row["industry"] is None:
        return []
    return [
        r["listing_id"] for r in conn.execute(
            "SELECT DISTINCT listing_id FROM market_snapshot WHERE industry=?"
            " AND listing_id != ?", (row["industry"], listing_id),
        )
    ]


def _snapshot_for(conn, cand: sqlite3.Row, asof: str) -> dict:
    listing = conn.execute(
        "SELECT listing_id FROM listing WHERE company_id=? LIMIT 1", (cand["company_id"],)
    ).fetchone()
    if listing is None:
        return {"state": "NO_LISTING"}
    lid = listing["listing_id"]
    frozen_date = _beijing_date(cand["frozen_at_utc"])
    ref_row = conn.execute(
        "SELECT MIN(trade_date) AS d FROM security_day WHERE listing_id=? AND trade_date>?"
        " AND adj_close IS NOT NULL", (lid, frozen_date),
    ).fetchone()
    if ref_row is None or ref_row["d"] is None:
        return {"state": "PENDING_FIRST_SESSION", "frozen_date": frozen_date,
                "artifact_status": cand["status"]}
    ref_date = ref_row["d"]
    series = _adj_series(conn, lid, ref_date, asof)
    if len(series) < 1:
        return {"state": "BASIS_BLOCKED_OR_NO_DATA", "ref_date": ref_date}
    cum = series / series.iloc[0] - 1.0

    peers = _cell_members(conn, lid)
    peer_final = None
    if len(peers) >= 2:
        finals = []
        for p in peers:
            s = _adj_series(conn, p, ref_date, asof)
            if len(s) >= len(series) // 2 and s.iloc[0] > 0:
                finals.append(float(s.iloc[-1] / s.iloc[0] - 1.0))
        if len(finals) >= 2:
            peer_final = float(pd.Series(finals).median())

    out = {
        "state": "TRACKED",
        "artifact_status": cand["status"],
        "candidate_state": cand["state"],
        "ref_date": ref_date,
        "last_date": str(series.index[-1]),
        "sessions_elapsed": int(len(series) - 1),
        "ret_raw": float(cum.iloc[-1]),
        "ret_peer_adj": (float(cum.iloc[-1]) - peer_final) if peer_final is not None else None,
        "peer_median_ret": peer_final,
        "mae_raw": float(cum.min()),
    }
    for w in WINDOWS:
        out[f"ret_raw_{w}s"] = float(cum.iloc[w]) if len(cum) > w else None
    return out


def run_validation(conn: sqlite3.Connection, asof: str | None = None) -> dict:
    asof = asof or datetime.now(UTC).strftime("%Y-%m-%d")
    rows = _ledger_candidates(conn)
    states: dict[str, int] = {}
    for cand in rows:
        snap = _snapshot_for(conn, cand, asof)
        states[snap["state"]] = states.get(snap["state"], 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO validation_snapshot(candidate_id, asof, metrics_json)"
            " VALUES(?,?,?)",
            (cand["candidate_id"], asof, json.dumps(snap, ensure_ascii=False)),
        )
    conn.commit()
    audit = {"asof": asof, "generated_at": utc_now(), "candidates_in_ledger": len(rows),
             "states": states,
             "excluded_invalidated": conn.execute(
                 "SELECT COUNT(DISTINCT candidate_id) FROM frozen_artifact"
                 " WHERE status='INVALIDATED'").fetchone()[0]}
    out_dir = DEFAULT_DATA_DIR / "audit"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"validate_{asof}.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2)
    )
    return audit


def backup_database(conn: sqlite3.Connection, keep: int = 8) -> str:
    import sqlite3 as sq

    out_dir = DEFAULT_DATA_DIR / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"investment_{stamp}.db"
    dest = sq.connect(path)
    with dest:
        conn.backup(dest)
    dest.close()
    backups = sorted(out_dir.glob("investment_*.db"))
    for old in backups[:-keep]:
        old.unlink()
    return str(path)
