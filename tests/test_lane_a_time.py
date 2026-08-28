import json

from investment_tool.lane_a import _visible_price_observations, _write_search_plan


def test_backdated_scan_cannot_see_later_trigger_observation(conn):
    conn.execute(
        "INSERT INTO observation(obs_id, kind, listing_id, payload_json, first_seen_at_utc)"
        " VALUES(?,?,?,?,?)",
        ("old", "price_trigger", "SZSE:000001", json.dumps({"t0": "2026-08-20"}),
         "2026-08-21T08:00:00Z"),
    )
    conn.execute(
        "INSERT INTO observation(obs_id, kind, listing_id, payload_json, first_seen_at_utc)"
        " VALUES(?,?,?,?,?)",
        ("late", "price_trigger", "SZSE:000002", json.dumps({"t0": "2026-08-20"}),
         "2026-08-29T08:00:00Z"),
    )
    visible = _visible_price_observations(conn, "2026-08-28")
    assert [r["obs_id"] for r in visible] == ["old"]


def test_search_plan_is_idempotent_per_trigger(conn):
    listing = {"ticker": "000001", "exchange": "SZSE"}
    detail = {"t0": "2026-08-20", "car_peer": -0.12}
    assert _write_search_plan(conn, listing, "2026-08-20", detail) is True
    assert _write_search_plan(conn, listing, "2026-08-20", detail) is False
    assert conn.execute("SELECT COUNT(*) AS n FROM search_plan").fetchone()["n"] == 1
