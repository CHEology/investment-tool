"""Event-anchored reaction engine with dual time anchors (PR-B).

Replaces the asof-trailing windows that PR review F1 falsified (pre-event
declines were attributed to newer filings). Every measure here is anchored to
one of two explicit clocks from calendars_us:

CAUSAL anchor (event_session): pre-event run-up/run-down, the event-session
return, event-window CARs, post-event cumulative return, volume response.

DECISION anchor (first_actionable_session): what had already happened before
the system could act (realized_before_entry) versus what came after
(forward_from_decision). Forward validation and system-performance metrics
start here, never at the event session.

Trailing asof windows survive only as clearly named diagnostics
(asof_trail_*) and must never gate or rank anything.
"""

from __future__ import annotations

import sqlite3

from investment_tool import us_prices


def _ret(a: float, b: float) -> float:
    return b / a - 1.0


def _bench_ret(bench: dict[str, float], d0: str, d1: str) -> float | None:
    a, b = bench.get(d0), bench.get(d1)
    return (b / a - 1.0) if a and b else None


def _madj(raw: float | None, bench: dict[str, float], d0: str, d1: str) -> float | None:
    if raw is None:
        return None
    br = _bench_ret(bench, d0, d1)
    return raw - br if br is not None else None


def compute_event_reaction(conn: sqlite3.Connection, listing_id: str,
                           anchors: dict, asof: str) -> dict:
    """All price reactions for one event, from stored adjusted series through
    `asof`. Returns a dict whose keys are grouped by anchor; see module
    docstring. post_ret1/post_cum keep their historical names (they were
    already event-anchored); everything trailing is asof_trail_*."""
    series = us_prices.adj_series(conn, listing_id, asof)
    spy = us_prices.bench_series(conn, "SPY", asof)
    qqq = us_prices.bench_series(conn, "QQQ", asof)
    if not series:
        return {"state": "NO_PRICES"}
    dates = [d for d, _p, _v in series]
    out: dict = {"state": "OK", "sessions": len(series), "last_session": dates[-1],
                 "anchors": anchors}

    # ---- asof-trailing diagnostics (NEVER gate/rank inputs) ----
    for k, name in ((1, "asof_trail_ret1"), (5, "asof_trail_ret5"),
                    (21, "asof_trail_ret21"), (63, "asof_trail_ret63")):
        raw = _ret(series[-1 - k][1], series[-1][1]) if len(series) > k else None
        out[name] = raw
        out[f"mkt_adj_{name}"] = (_madj(raw, spy, dates[-1 - k], dates[-1])
                                  if raw is not None else None)

    t0 = anchors.get("event_session")
    if t0 is None:
        out["post_state"] = "NO_T0"
        return out
    i0 = next((i for i, d in enumerate(dates) if d >= t0), None)
    if i0 is None:
        out["post_state"] = "POST_EVENT_PENDING"
        return out
    if i0 == 0:
        out["post_state"] = "NO_PRE_EVENT_BASELINE"
        return out
    base_d, evt_d = dates[i0 - 1], dates[i0]
    out["t0_session"] = evt_d
    out["event_window_contaminated"] = bool(anchors.get("same_session_partial"))

    # ---- causal anchor: pre-event run-up (feature, not a trigger) ----
    for k, name in ((5, "run_up_5"), (21, "run_up_21")):
        if i0 - 1 - k >= 0:
            raw = _ret(series[i0 - 1 - k][1], series[i0 - 1][1])
            out[name] = raw
            out[f"mkt_adj_{name}"] = _madj(raw, spy, dates[i0 - 1 - k], base_d)
        else:
            out[name] = out[f"mkt_adj_{name}"] = None

    # ---- causal anchor: event window ----
    out["post_ret1"] = _ret(series[i0 - 1][1], series[i0][1])
    out["mkt_adj_post_ret1"] = _madj(out["post_ret1"], spy, base_d, evt_d)
    out["qqq_adj_post_ret1"] = _madj(out["post_ret1"], qqq, base_d, evt_d)
    i5 = min(i0 + 5, len(series) - 1)
    out["car5"] = _ret(series[i0 - 1][1], series[i5][1])
    out["mkt_adj_car5"] = _madj(out["car5"], spy, base_d, dates[i5])
    out["car5_window_sessions"] = i5 - i0 + 1
    # clean post-disclosure legs: measured FROM the event-session close
    # forward, so they can never contain pre-release trading even when the
    # release was intra-session (H0/F13; loophole fix H0.1 — car5 contains
    # the event session and is therefore NOT clean)
    if i0 + 1 < len(series):
        out["next_ret1"] = _ret(series[i0][1], series[i0 + 1][1])
        out["mkt_adj_next_ret1"] = _madj(out["next_ret1"], spy, evt_d, dates[i0 + 1])
    else:
        out["next_ret1"] = out["mkt_adj_next_ret1"] = None
    i3 = min(i0 + 3, len(series) - 1)
    if i3 > i0:
        out["post_car3"] = _ret(series[i0][1], series[i3][1])
        out["mkt_adj_post_car3"] = _madj(out["post_car3"], spy, evt_d, dates[i3])
        out["post_car3_window_sessions"] = i3 - i0
    else:
        out["post_car3"] = out["mkt_adj_post_car3"] = None
        out["post_car3_window_sessions"] = 0
    out["post_cum"] = _ret(series[i0 - 1][1], series[-1][1])
    out["mkt_adj_post_cum"] = _madj(out["post_cum"], spy, base_d, dates[-1])
    vols = [v for _d, _p, v in series[max(0, i0 - 20):i0] if v]
    v0 = series[i0][2]
    if vols and v0:
        med = sorted(vols)[len(vols) // 2]
        out["volume_ratio"] = v0 / med if med else None

    # ---- decision anchor: what the system could actually have acted on ----
    act = anchors.get("first_actionable_session")
    if act:
        ia = next((i for i, d in enumerate(dates) if d >= act), None)
        if ia is None:
            out["decision_state"] = "ENTRY_PENDING"  # actionable session after asof
        else:
            entry_d = dates[ia]
            out["decision_state"] = "OK"
            out["entry_session"] = entry_d
            out["realized_before_entry"] = _ret(series[i0 - 1][1], series[ia][1])
            out["mkt_adj_realized_before_entry"] = _madj(
                out["realized_before_entry"], spy, base_d, entry_d)
            out["forward_from_decision"] = (_ret(series[ia][1], series[-1][1])
                                            if ia < len(series) - 1 else 0.0)
            out["mkt_adj_forward_from_decision"] = (
                _madj(out["forward_from_decision"], spy, entry_d, dates[-1])
                if ia < len(series) - 1 else 0.0)
    else:
        out["decision_state"] = "NO_DECISION_ANCHOR"
    out["post_state"] = "OK"
    return out
