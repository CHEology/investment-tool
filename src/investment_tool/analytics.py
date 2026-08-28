"""Deterministic scan engine: peer cells, abnormal returns, CAR windows with
limit-pause, trigger/shadow evaluation. Frozen v0 rules; floats are fine here
(derived analytics), reported values never round-trip through this module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

LARGE, MID, SMALL, BSE_BUCKET = "LARGE", "MID", "SMALL", "BSE"
BENCH_BY_BUCKET = {LARGE: "CSI300", MID: "CSI500", SMALL: "CSI1000", BSE_BUCKET: "BSE50"}


# Return-basis compatibility groups: adjusted-lineage bases may mix inside one
# analytical window; RAW_CONSEC (no corporate-action adjustment) may not mix
# with them. A window violating this is BLOCKED, not warned (review issue 2).
ADJUSTED_BASES = {"EXCHANGE_PCT", "QFQ_CONSEC", "SYNTH_COMPOUND"}
RAW_BASES = {"RAW_CONSEC"}


def load_panel(conn: sqlite3.Connection, start: str, end: str) -> pd.DataFrame:
    """Long panel of daily rows for CN listings in [start, end].

    Basis gate: listings whose rows in the window mix adjusted-lineage and
    raw-lineage return bases, or span more than one basis_epoch, are excluded
    from analytics entirely and reported via the `basis_blocked` attribute.
    """
    q = (
        "SELECT sd.listing_id, sd.trade_date, sd.ret, sd.ret_basis, sd.basis_epoch,"
        " sd.limit_state, sd.close, sd.adj_close, sd.amount,"
        " l.exchange, l.board, l.ticker"
        " FROM security_day sd JOIN listing l ON l.listing_id = sd.listing_id"
        " WHERE l.exchange IN ('SSE','SZSE','BSE') AND sd.trade_date>=? AND sd.trade_date<=?"
    )
    df = pd.read_sql_query(q, conn, params=(start, end))
    df["amount_f"] = pd.to_numeric(df["amount"], errors="coerce")

    grp = df[df["ret_basis"].notna()].groupby("listing_id").agg(
        bases=("ret_basis", lambda x: frozenset(x)),
        epochs=("basis_epoch", "nunique"),
    )
    blocked = grp[
        (grp["epochs"] > 1)
        | grp["bases"].apply(lambda b: bool(b & ADJUSTED_BASES) and bool(b & RAW_BASES))
    ].index.tolist()
    df = df[~df["listing_id"].isin(blocked)].copy()
    df.attrs["basis_blocked"] = blocked
    return df


def load_cells(conn: sqlite3.Connection, asof_date: str | None = None) -> pd.DataFrame:
    """Cell assignment from the latest snapshot available by ``asof_date``.

    A missing date keeps the original latest-snapshot behavior for interactive
    analysis.  Scans pass their date explicitly so a replay cannot consume a
    later industry or size observation.
    """
    snapshot_filter = (
        " WHERE ms.asof_date = (SELECT MAX(asof_date) FROM market_snapshot"
        " WHERE asof_date<=?)"
        if asof_date
        else " WHERE ms.asof_date = (SELECT MAX(asof_date) FROM market_snapshot)"
    )
    snap = pd.read_sql_query(
        "SELECT ms.listing_id, ms.industry, ms.float_mcap, ms.is_st, ms.name, l.exchange"
        " FROM market_snapshot ms JOIN listing l ON l.listing_id = ms.listing_id"
        + snapshot_filter,
        conn,
        params=(asof_date,) if asof_date else (),
    )
    if snap.empty:
        return pd.DataFrame(
            columns=["listing_id", "industry", "size_bucket", "cell", "is_st", "name"]
        )
    snap["float_mcap_f"] = pd.to_numeric(snap["float_mcap"], errors="coerce")
    non_bse = snap[snap["exchange"] != "BSE"]
    terciles = non_bse["float_mcap_f"].quantile([1 / 3, 2 / 3]).values

    def bucket(row):
        if row["exchange"] == "BSE":
            return BSE_BUCKET
        v = row["float_mcap_f"]
        if pd.isna(v):
            return SMALL
        if v <= terciles[0]:
            return SMALL
        if v <= terciles[1]:
            return MID
        return LARGE

    snap["size_bucket"] = snap.apply(bucket, axis=1)
    snap["industry"] = snap["industry"].fillna("UNKNOWN")
    snap["cell"] = snap["industry"] + "|" + snap["size_bucket"]
    return snap[["listing_id", "industry", "size_bucket", "cell", "is_st", "name"]]


@dataclass
class ARPanel:
    ar: pd.DataFrame          # index=trade_date, columns=listing_id, peer-adjusted returns
    ret: pd.DataFrame         # raw returns, same shape
    limit: pd.DataFrame       # limit_state strings, same shape
    cells: pd.DataFrame
    dates: list[str] = field(default_factory=list)
    cell_flags: pd.DataFrame | None = None  # LIMIT_CONTAMINATED flags per (date, cell)


def build_ar_panel(df: pd.DataFrame, cells: pd.DataFrame, min_peers: int = 8,
                   contaminated_frac: float = 0.20) -> ARPanel:
    df = df.merge(cells[["listing_id", "cell", "size_bucket", "industry"]], on="listing_id",
                  how="left")
    df["cell"] = df["cell"].fillna("UNKNOWN|" + SMALL)
    trading = df.dropna(subset=["ret"]).copy()

    # Cell medians with fallback chain: cell -> industry -> market (per date),
    # fully vectorized (row-wise apply is prohibitive at ~1.6M rows).
    med_cell = (trading.groupby(["trade_date", "cell"])["ret"].agg(["median", "count"])
                .rename(columns={"median": "cell_med", "count": "cell_n"}).reset_index())
    med_ind = (trading.groupby(["trade_date", "industry"])["ret"].median()
               .rename("ind_med").reset_index())
    med_mkt = trading.groupby("trade_date")["ret"].median().rename("mkt_med").reset_index()

    locked = trading["limit_state"].isin(["LIMIT_UP", "LIMIT_DOWN"])
    lock_frac = trading.assign(locked=locked).groupby(["trade_date", "cell"])["locked"].mean()

    trading = (trading.merge(med_cell, on=["trade_date", "cell"], how="left")
               .merge(med_ind, on=["trade_date", "industry"], how="left")
               .merge(med_mkt, on="trade_date", how="left"))
    use_cell = trading["cell_n"] >= min_peers
    trading["peer_med"] = np.where(
        use_cell, trading["cell_med"],
        np.where(trading["ind_med"].notna(), trading["ind_med"], trading["mkt_med"]),
    )
    trading["ar"] = trading["ret"] - trading["peer_med"]

    ar = trading.pivot_table(index="trade_date", columns="listing_id", values="ar",
                             aggfunc="first")
    ret = trading.pivot_table(index="trade_date", columns="listing_id", values="ret",
                              aggfunc="first")
    limit = df.pivot_table(index="trade_date", columns="listing_id", values="limit_state",
                           aggfunc="first")
    flags = (lock_frac > contaminated_frac).rename("LIMIT_CONTAMINATED").reset_index()
    return ARPanel(ar=ar, ret=ret, limit=limit, cells=cells,
                   dates=sorted(ar.index.tolist()), cell_flags=flags)


def residual_sigma(panel: ARPanel, listing_id: str, t0: str, window: int = 120,
                   min_obs: int = 60) -> float | None:
    if listing_id not in panel.ar.columns:
        return None
    series = panel.ar[listing_id]
    hist = series[series.index < t0].dropna().tail(window)
    if len(hist) < min_obs:
        return None
    return float(hist.std(ddof=1))


def car_window(panel: ARPanel, listing_id: str, t0: str, k: int,
               cap_sessions: int = 15) -> dict:
    """Cumulative peer-adjusted AR from t0 until (k+1) FREE sessions elapsed
    (limit-locked sessions pause the clock but their ARs are included), capped.
    """
    if listing_id not in panel.ar.columns:
        return {"car": None, "state": "NO_DATA", "sessions": 0, "free": 0}
    dates = [d for d in panel.dates if d >= t0]
    car = 0.0
    free = 0
    used = 0
    locked_last = False
    for d in dates:
        a = panel.ar.at[d, listing_id] if d in panel.ar.index else None
        lim = panel.limit.at[d, listing_id] if d in panel.limit.index else None
        if a is None or (isinstance(a, float) and np.isnan(a)):
            continue  # suspended day: no return observed
        car += float(a)
        used += 1
        locked_last = lim in ("LIMIT_UP", "LIMIT_DOWN")
        if not locked_last:
            free += 1
        if free >= k + 1 or used >= cap_sessions:
            break
    if used == 0:
        return {"car": None, "state": "NO_DATA", "sessions": 0, "free": 0}
    complete = free >= k + 1 and not locked_last
    state = "COMPLETE" if complete else ("INCOMPLETE_LIMIT" if locked_last else "PARTIAL")
    return {"car": car, "state": state, "sessions": used, "free": free}


def evaluate_trigger(car: float | None, sigma: float | None, car_max: float,
                     sigma_mult: float, shadow_fraction: float) -> str:
    """Frozen v0 rule: trigger iff car <= car_max AND car <= -sigma_mult*sigma.
    Shadow iff both conditions hold at shadow_fraction scale but not in full.
    Returns 'TRIGGER' | 'SHADOW' | 'NONE' | 'DATA_INSUFFICIENT'."""
    if car is None:
        return "DATA_INSUFFICIENT"
    if sigma is None:
        return "DATA_INSUFFICIENT"
    if car <= car_max and car <= -sigma_mult * sigma:
        return "TRIGGER"
    if car <= car_max * shadow_fraction and car <= -sigma_mult * sigma * shadow_fraction:
        return "SHADOW"
    return "NONE"


def market_model_car(panel: ARPanel, bench: pd.Series, listing_id: str, t0: str, k: int,
                     est_window: int = 250, min_obs: int = 120) -> float | None:
    """Secondary estimator: CAR of OLS residuals vs the size-matched benchmark."""
    if listing_id not in panel.ret.columns:
        return None
    r = panel.ret[listing_id].dropna()
    b = bench.dropna()
    if len(b) < min_obs:
        return None
    joined = pd.concat([r, b], axis=1, join="inner")
    joined.columns = ["r", "b"]
    est = joined[joined.index < t0].tail(est_window)
    if len(est) < min_obs:
        return None
    est = est.clip(lower=est.quantile(0.01), upper=est.quantile(0.99), axis=1).dropna()
    if len(est) < min_obs or float(est["b"].std()) == 0.0:
        return None
    try:
        beta, alpha = np.polyfit(est["b"], est["r"], 1)
    except np.linalg.LinAlgError:
        return None
    win = joined[joined.index >= t0].head(k + 1)
    if win.empty:
        return None
    resid = win["r"] - (alpha + beta * win["b"])
    return float(resid.sum())


def liquidity_class(panel: ARPanel, conn: sqlite3.Connection, listing_id: str, t0: str,
                    exclude_usd: float, warn_usd: float) -> tuple[str, float | None]:
    row = conn.execute(
        "SELECT rate FROM fx_day WHERE pair='USDCNY' ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return "FX_UNAVAILABLE", None
    rate = float(row["rate"])
    amounts = pd.read_sql_query(
        "SELECT amount, COALESCE(close, adj_close) AS px, volume FROM security_day"
        " WHERE listing_id=? AND trade_date<? ORDER BY trade_date DESC LIMIT 60",
        conn, params=(listing_id, t0),
    )
    vals = pd.to_numeric(amounts["amount"], errors="coerce")
    # scan-tier sources without turnover: approximate amount = price x volume(shares);
    # adj_close stands in for raw close where raw is unknown (scan-tier tolerance)
    approx = (pd.to_numeric(amounts["px"], errors="coerce")
              * pd.to_numeric(amounts["volume"], errors="coerce"))
    vals = vals.fillna(approx).dropna()
    if len(vals) < 20:
        return "DATA_INSUFFICIENT", None
    adv_usd = float(vals.median()) / rate
    if adv_usd < exclude_usd:
        return "EXCLUDE", adv_usd
    if adv_usd < warn_usd:
        return "WARN", adv_usd
    return "NORMAL", adv_usd


def load_benchmark(conn: sqlite3.Connection, index_id: str) -> pd.Series:
    df = pd.read_sql_query(
        "SELECT trade_date, close FROM benchmark_day WHERE index_id=? ORDER BY trade_date",
        conn, params=(index_id,),
    )
    close = pd.to_numeric(df["close"], errors="coerce")
    ret = close.pct_change()
    return pd.Series(ret.values, index=df["trade_date"].values).dropna()
