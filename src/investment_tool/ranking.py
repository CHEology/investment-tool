"""Deterministic deep-read ranking for triggered US trial events (PR-A).

Rank-before-budget: every TRIGGERED event gets a score BEFORE any deep-read
budget is applied, so budget exhaustion defers the weakest-ranked items, never
the latest-seen ones (review F2). The score orders research priority only —
it is not evidence of investment value and must never be presented as such.

Versioned like the content rules: weights live here under RANK_VERSION;
changing them requires a new version string (forward-only, mirroring the
threshold registry philosophy). Inputs are event-anchored reaction measures
plus the trigger-leg count; trailing asof windows are deliberately excluded
(they are the F1 defect and stay diagnostic-only).
"""

from __future__ import annotations

RANK_VERSION = "us_rank_v0"

# weight, clamped to [0, 1] before weighting
W_EVENT_1D = 0.45     # adverse market-adjusted event-session return
W_EVENT_CUM = 0.30    # adverse market-adjusted post-event cumulative return
W_VOLUME = 0.15       # event-session volume ratio vs 20-session median
W_LEGS = 0.10         # number of trigger legs hit

EVENT_1D_FULL = 0.20   # -20% mkt-adj 1-session reaction saturates the component
EVENT_CUM_FULL = 0.30  # -30% mkt-adj cumulative reaction saturates
VOLUME_FULL = 10.0     # 10x volume ratio saturates
LEGS_FULL = 5          # all five legs


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def score_event(rx: dict, hits: list[str]) -> dict:
    """Score one TRIGGERED event. Returns {score, version, components} where
    components explain every contribution (explainability requirement).
    Missing inputs contribute 0 and are recorded as null."""
    e1 = rx.get("mkt_adj_post_ret1")
    ec = rx.get("mkt_adj_post_cum")
    vr = rx.get("volume_ratio")
    comp = {
        "event_1d": None if e1 is None else _clamp01(max(0.0, -float(e1)) / EVENT_1D_FULL),
        "event_cum": None if ec is None else _clamp01(max(0.0, -float(ec)) / EVENT_CUM_FULL),
        "volume": None if vr is None else _clamp01(float(vr) / VOLUME_FULL),
        "legs": _clamp01(len(hits) / LEGS_FULL),
    }
    score = (W_EVENT_1D * (comp["event_1d"] or 0.0)
             + W_EVENT_CUM * (comp["event_cum"] or 0.0)
             + W_VOLUME * (comp["volume"] or 0.0)
             + W_LEGS * comp["legs"])
    return {"score": round(score, 6), "version": RANK_VERSION, "components": comp,
            "weights": {"event_1d": W_EVENT_1D, "event_cum": W_EVENT_CUM,
                        "volume": W_VOLUME, "legs": W_LEGS}}


def rank_events(items: list[dict]) -> list[dict]:
    """items: [{event_id, ticker, rx, hits, ...}] -> same items, each with a
    'rank' dict attached, sorted best-first with a fully deterministic
    tie-break (score desc, ticker asc, event_id asc)."""
    for it in items:
        it["rank"] = score_event(it["rx"], it["hits"])
    return sorted(items, key=lambda it: (-it["rank"]["score"],
                                         it.get("ticker") or "",
                                         it["event_id"]))
