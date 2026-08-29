"""US session mapping with two explicitly separate clocks (PR-B).

CAUSAL EVENT ANCHOR — when the information became public to the market
(SEC acceptance time, halt timestamp, official publication). It answers
"which session could first react", and drives pre-event run-up, the
event-session return, post-event CARs, and causal attribution.

SYSTEM DECISION ANCHOR — first_seen_at_utc, when THIS system obtained the
information. It answers "when could we have acted", and drives lookahead
protection, research eligibility, forward-validation entry, and system
performance measurement. The market reaction between the two clocks is
observable history at decision time and may be analyzed — but the system
must never pretend it could have acted before first_seen.

Sessions come from exchange_calendars (XNYS: real holidays and early closes,
DST-correct), replacing the WEEKDAY_APPROX_NO_HOLIDAYS approximation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

import pandas as pd

CALENDAR_ID = "XNYS"

# session_relation values for the causal anchor
PRE_OPEN = "PRE_OPEN"
INTRA_SESSION = "INTRA_SESSION"
POST_CLOSE = "POST_CLOSE"
NON_SESSION_DAY = "NON_SESSION_DAY"
DATE_ONLY = "DATE_ONLY"


@lru_cache(maxsize=1)
def cal():
    import exchange_calendars as xcals
    return xcals.get_calendar(CALENDAR_ID)


def _parse_utc(ts: str) -> pd.Timestamp:
    dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return pd.Timestamp(dt)


def causal_anchor(published_at_utc: str | None, fallback_date: str | None) -> dict:
    """Map the public-information timestamp to the first session whose close
    postdates it. Pre-open -> that session (full window); intra-session ->
    that session with same_session_partial=True (the close-to-close event
    return also contains pre-release trading — flagged, never silent);
    post-close/weekend/holiday -> next session."""
    c = cal()
    if published_at_utc:
        ts = _parse_utc(published_at_utc)
        sess = c.minute_to_session(ts, direction="next")
        # classify by the exchange-local (ET) calendar day, DST-correct
        et_date = ts.tz_convert("America/New_York").strftime("%Y-%m-%d")
        if et_date == sess.strftime("%Y-%m-%d"):
            relation = PRE_OPEN if ts < c.session_open(sess) else INTRA_SESSION
        elif c.is_session(et_date):
            relation = POST_CLOSE      # on a session day, after its close
        else:
            relation = NON_SESSION_DAY  # weekend or holiday
        return {"event_session": sess.strftime("%Y-%m-%d"), "precision": "TIME",
                "session_relation": relation,
                "same_session_partial": relation == INTRA_SESSION,
                "calendar": CALENDAR_ID}
    if fallback_date:
        sess = c.date_to_session(fallback_date, direction="next")
        return {"event_session": sess.strftime("%Y-%m-%d"), "precision": "DATE",
                "session_relation": DATE_ONLY, "same_session_partial": None,
                "calendar": CALENDAR_ID}
    return {"event_session": None, "precision": "UNKNOWN", "session_relation": None,
            "same_session_partial": None, "calendar": CALENDAR_ID}


def decision_anchor(first_seen_at_utc: str) -> dict:
    """First session at whose OPEN the system could have acted after
    observation: the next session open strictly after first_seen. An EOD
    system observing post-close on day D can act at D+1's open; forward
    returns are measured from that session's close (the first close the
    system could realistically transact near, consistent with the
    forward-validation ledger)."""
    c = cal()
    ts = _parse_utc(first_seen_at_utc)
    nxt = c.next_open(ts)
    sess = c.minute_to_session(nxt)
    return {"first_actionable_session": sess.strftime("%Y-%m-%d"),
            "calendar": CALENDAR_ID}


def anchors_for_event(published_at_utc: str | None, fallback_date: str | None,
                      first_seen_at_utc: str | None) -> dict:
    """Both clocks plus detection latency, kept side by side on every record
    so causal analysis and system-performance analysis can never be
    conflated."""
    causal = causal_anchor(published_at_utc, fallback_date)
    out = {"causal_ts_utc": published_at_utc, "first_seen_at_utc": first_seen_at_utc,
           **causal, "first_actionable_session": None,
           "detection_latency_seconds": None, "detection_latency_sessions": None}
    if first_seen_at_utc:
        out.update(decision_anchor(first_seen_at_utc))
        if published_at_utc:
            out["detection_latency_seconds"] = int(
                (_parse_utc(first_seen_at_utc) - _parse_utc(published_at_utc))
                .total_seconds())
        if out["event_session"] and out["first_actionable_session"]:
            out["detection_latency_sessions"] = cal().sessions_distance(
                out["event_session"], out["first_actionable_session"]) - 1
    return out
