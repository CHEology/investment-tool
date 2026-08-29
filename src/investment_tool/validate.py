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


US_EXCHANGES = ("NASDAQ", "NYSE", "AMEX")


def _freeze_local_date(ts_utc: str, exchange: str | None) -> str:
    """Freeze-date in the LISTING's market timezone (H0 correction of the
    Beijing-only anchor): US listings use America/New_York, A-share listings
    keep UTC+8. The first tracked session is the first trade date strictly
    after this local date."""
    from zoneinfo import ZoneInfo

    dt = datetime.strptime(ts_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    if exchange in US_EXCHANGES:
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d")


def _ledger_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """ALL candidates with frozen artifacts. Invalidated ones stay VISIBLE in
    the historical ledger with an explicit excluded state — they are never
    tracked as control observations and never silently absent."""
    return conn.execute(
        """
        SELECT c.candidate_id, c.company_id, c.state, fa.frozen_at_utc, fa.status
        FROM candidate c
        JOIN frozen_artifact fa ON fa.candidate_id = c.candidate_id
          AND fa.version = (SELECT MAX(version) FROM frozen_artifact
                            WHERE candidate_id = c.candidate_id)
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


def _industry_asof(conn, listing_id: str, ref_date: str) -> str | None:
    row = conn.execute(
        "SELECT industry FROM market_snapshot WHERE listing_id=? AND asof_date<=?"
        " ORDER BY asof_date DESC LIMIT 1", (listing_id, ref_date),
    ).fetchone()
    return row["industry"] if row else None


def _cell_members(conn, listing_id: str, ref_date: str) -> list[str]:
    """True point-in-time membership: the candidate AND every peer are
    classified by their own latest snapshot at or before ref_date. No
    fall-forward — a company without a pre-reference snapshot (or one that
    joined the industry only later) is simply not a peer for this track."""
    industry = _industry_asof(conn, listing_id, ref_date)
    if industry is None:
        return []
    return [
        r["listing_id"] for r in conn.execute(
            """
            SELECT ms.listing_id FROM market_snapshot ms
            JOIN (SELECT listing_id, MAX(asof_date) AS d FROM market_snapshot
                  WHERE asof_date<=? GROUP BY listing_id) latest
              ON latest.listing_id=ms.listing_id AND latest.d=ms.asof_date
            WHERE ms.industry=? AND ms.listing_id != ?
            """,
            (ref_date, industry, listing_id),
        )
    ]


def _snapshot_for(conn, cand: sqlite3.Row, asof: str) -> dict:
    if cand["status"] == "INVALIDATED":
        return {"state": "EXCLUDED_INVALIDATED", "artifact_status": "INVALIDATED",
                "note": "visible in the ledger; never a control observation"}
    listing = conn.execute(
        "SELECT listing_id, exchange FROM listing WHERE company_id=?"
        " ORDER BY listing_id LIMIT 1",
        (cand["company_id"],)
    ).fetchone()
    if listing is None:
        return {"state": "NO_LISTING"}
    lid = listing["listing_id"]
    frozen_date = _freeze_local_date(cand["frozen_at_utc"], listing["exchange"])
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

    peers = _cell_members(conn, lid, ref_date)
    peer_final = None
    peers_skipped_baseline = 0
    peers_skipped_endpoint = 0
    end_date = str(series.index[-1])
    if len(peers) >= 2:
        finals = []
        for p in peers:
            s = _adj_series(conn, p, ref_date, asof)
            # comparable windows: a peer must share the candidate's exact
            # baseline session AND final session, else its cumulative return
            # covers a different period and is not comparable
            if len(s) == 0 or str(s.index[0]) != ref_date:
                peers_skipped_baseline += 1
                continue
            if str(s.index[-1]) != end_date:
                peers_skipped_endpoint += 1
                continue
            if s.iloc[0] > 0:
                finals.append(float(s.iloc[-1] / s.iloc[0] - 1.0))
        if len(finals) >= 2:
            peer_final = float(pd.Series(finals).median())

    # market adjustment for US listings: same-window SPY return from
    # benchmark_day (H0 correction — US snapshots previously had raw only)
    mkt_adj = None
    if listing["exchange"] in US_EXCHANGES:
        spy = {
            r["trade_date"]: float(r["close"]) for r in conn.execute(
                "SELECT trade_date, close FROM benchmark_day WHERE index_id='SPY'"
                " AND trade_date>=? AND trade_date<=? ORDER BY trade_date",
                (ref_date, str(series.index[-1])),
            )
        }
        b0, b1 = spy.get(str(series.index[0])), spy.get(str(series.index[-1]))
        if b0 and b1:
            mkt_adj = float(cum.iloc[-1]) - (b1 / b0 - 1.0)

    out = {
        "state": "TRACKED",
        "artifact_status": cand["status"],
        "candidate_state": cand["state"],
        "ref_date": ref_date,
        "last_date": str(series.index[-1]),
        "sessions_elapsed": int(len(series) - 1),
        "ret_raw": float(cum.iloc[-1]),
        "ret_mkt_adj": mkt_adj,
        "ret_peer_adj": (float(cum.iloc[-1]) - peer_final) if peer_final is not None else None,
        "peer_median_ret": peer_final,
        "mae_raw": float(cum.min()),
        "peers_skipped_baseline_mismatch": peers_skipped_baseline,
        "peers_skipped_endpoint_mismatch": peers_skipped_endpoint,
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
    audit = {"asof": asof, "generated_at": utc_now(),
             "candidates_in_ledger": len(rows),
             "tracked": sum(v for k, v in states.items()
                            if k not in ("EXCLUDED_INVALIDATED",)),
             "states": states}
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
