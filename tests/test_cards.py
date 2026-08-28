import json

from investment_tool import cards


def _mk_candidate(conn):
    conn.execute(
        "INSERT INTO company(company_id, name_zh, created_asof)"
        " VALUES('CN:300274','阳光电源','2026-08-28T00:00:00Z')")
    conn.execute(
        "INSERT INTO listing(listing_id, company_id, ticker, exchange, board, currency)"
        " VALUES('SZSE:300274','CN:300274','300274','SZSE','CHINEXT','CNY')")
    profile = {
        "t0": "2026-08-27", "car_peer": -0.062, "car_mm": -0.121, "window_state": "PARTIAL",
        "sigma": 0.018,
        "liquidity": {"class": "NORMAL", "adv60_usd": 8.4e8},
        "announcements": [{"title": "回购公告", "type": "BUYBACK",
                           "published": "2026-08-27T12:00:00Z", "negative": False}],
        "damage": {"classification": "WITHIN_BRACKET", "excess_ratio": None,
                   "dmcap_abnormal": "1.4e10", "mcap_t0_minus_1": "2.3e11",
                   "damage_low": "2.9e9", "damage_high": "3.8e10",
                   "template": "market_access", "admitted": False},
        "verification_debt": ["eastmoney PROVISIONAL spine"],
    }
    conn.execute(
        "INSERT INTO candidate(candidate_id, company_id, lane, state, profile_json, gates_json,"
        " detected_at_utc, config_version) VALUES(?,?,?,?,?,?,?,?)",
        ("cand1", "CN:300274", "A", "NOT_ADMITTED_WITHIN_BRACKET",
         json.dumps(profile, ensure_ascii=False), "{}", "2026-08-28T16:00:00Z", "v0"),
    )
    conn.commit()
    return conn.execute("SELECT * FROM candidate WHERE candidate_id='cand1'").fetchone()


def test_card_renders_zh_and_freezes(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "CARDS_DIR", tmp_path / "cards")
    row = _mk_candidate(conn)
    content = cards.render_card_zh(conn, row)
    assert "阳光电源" in content and "WITHIN_BRACKET" in content
    assert "不构成任何投资建议" in content
    frozen = cards.freeze_card(conn, row, content)
    stored = conn.execute(
        "SELECT * FROM frozen_artifact WHERE artifact_id=?", (frozen["artifact_id"],)
    ).fetchone()
    assert stored["content_sha256"] == frozen["sha256"]
    # re-freeze -> new version, immutable prior artifact
    frozen2 = cards.freeze_card(conn, row, content + "\nupdate")
    assert frozen2["version"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM frozen_artifact").fetchone()["n"] == 2
