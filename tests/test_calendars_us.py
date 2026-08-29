"""PR-B falsification battery, calendar half: causal-anchor session mapping
(pre-open / intra-session / post-close / weekend / holiday / early close) and
the decision anchor derived from first_seen_at_utc. Uses the real XNYS
calendar shipped with exchange_calendars — offline, deterministic."""

from investment_tool import calendars_us


def test_pre_open_acceptance_maps_to_same_session_full_window():
    # 2026-08-27 12:02:52Z = 08:02 ET, a Thursday before the 09:30 open
    a = calendars_us.causal_anchor("2026-08-27T12:02:52Z", None)
    assert a["event_session"] == "2026-08-27"
    assert a["session_relation"] == calendars_us.PRE_OPEN
    assert a["same_session_partial"] is False


def test_intra_session_acceptance_flags_partial_window():
    # 15:00Z = 11:00 ET, mid-session
    a = calendars_us.causal_anchor("2026-08-27T15:00:00Z", None)
    assert a["event_session"] == "2026-08-27"
    assert a["session_relation"] == calendars_us.INTRA_SESSION
    assert a["same_session_partial"] is True


def test_post_close_acceptance_rolls_to_next_session():
    # 21:30Z = 17:30 ET, after the 16:00 close on Thursday
    a = calendars_us.causal_anchor("2026-08-27T21:30:00Z", None)
    assert a["event_session"] == "2026-08-28"
    assert a["session_relation"] == calendars_us.POST_CLOSE
    # Friday post-close rolls over the weekend to Monday (not Saturday)
    b = calendars_us.causal_anchor("2026-08-28T21:30:00Z", None)
    assert b["event_session"] == "2026-08-31"
    assert b["session_relation"] == calendars_us.POST_CLOSE


def test_holiday_and_weekend_map_to_next_real_session():
    # 2026-07-03 (Friday) is the observed Independence Day holiday on XNYS;
    # the next session is Monday 2026-07-06
    a = calendars_us.causal_anchor("2026-07-03T15:00:00Z", None)
    assert a["event_session"] == "2026-07-06"
    assert a["session_relation"] == calendars_us.NON_SESSION_DAY
    b = calendars_us.causal_anchor("2026-08-29T15:00:00Z", None)  # Saturday
    assert b["event_session"] == "2026-08-31"
    assert b["session_relation"] == calendars_us.NON_SESSION_DAY


def test_early_close_shortened_session():
    # 2026-11-27 (day after Thanksgiving) closes 13:00 ET = 18:00Z;
    # 14:00 ET is AFTER that early close even though a normal day would be open
    a = calendars_us.causal_anchor("2026-11-27T19:00:00Z", None)
    assert a["event_session"] == "2026-11-30"
    assert a["session_relation"] == calendars_us.POST_CLOSE
    # while 12:00 ET the same day is intra-session
    b = calendars_us.causal_anchor("2026-11-27T17:00:00Z", None)
    assert b["event_session"] == "2026-11-27"
    assert b["session_relation"] == calendars_us.INTRA_SESSION


def test_date_precision_fallback():
    a = calendars_us.causal_anchor(None, "2026-08-29")  # Saturday date
    assert a["event_session"] == "2026-08-31"
    assert a["precision"] == "DATE" and a["same_session_partial"] is None


def test_decision_anchor_and_detection_latency():
    """The system saw a Wednesday post-close filing two days later: the causal
    clock says the market could react Thursday; the decision clock says WE
    could act only the following Monday. Both are preserved, never conflated."""
    anc = calendars_us.anchors_for_event(
        "2026-08-26T21:10:00Z",            # Wed after close -> event session Thu 08-27
        "2026-08-26",
        "2026-08-28T21:18:28Z",            # seen Fri after close -> actionable Mon
    )
    assert anc["event_session"] == "2026-08-27"
    assert anc["first_actionable_session"] == "2026-08-31"
    assert anc["detection_latency_sessions"] == 2
    # 2026-08-26T21:10:00Z -> 2026-08-28T21:18:28Z = 2 days, 8 min, 28 s
    assert anc["detection_latency_seconds"] == 2 * 86400 + 8 * 60 + 28


def test_decision_anchor_same_session_when_seen_pre_open():
    """Seen before the open of the event session: the system can act that
    same session (latency 0)."""
    anc = calendars_us.anchors_for_event(
        "2026-08-26T21:10:00Z", "2026-08-26", "2026-08-27T11:00:00Z")
    assert anc["event_session"] == "2026-08-27"
    assert anc["first_actionable_session"] == "2026-08-27"
    assert anc["detection_latency_sessions"] == 0
